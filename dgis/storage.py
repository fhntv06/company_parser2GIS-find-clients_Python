from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .normalize import normalize_phone


def now_iso(offset: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S")


SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses(
  dgis_id TEXT PRIMARY KEY, name TEXT, address TEXT, city TEXT, rubric TEXT,
  phones TEXT, raw_url TEXT, site_kind TEXT DEFAULT 'none', collected_at TEXT
);
CREATE TABLE IF NOT EXISTS sites(            -- только собственные сайты (kind=own)
  domain TEXT PRIMARY KEY, url TEXT, http_status INT, final_url TEXT, checked_at TEXT, note TEXT,
  analysis_status TEXT DEFAULT 'new'         -- контракт с этапом 2: new -> analyzed
);
CREATE TABLE IF NOT EXISTS business_sites(dgis_id TEXT, domain TEXT, PRIMARY KEY(dgis_id, domain));
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, payload TEXT, uniq TEXT UNIQUE,
  priority INT DEFAULT 5, status TEXT DEFAULT 'queued',
  attempts INT DEFAULT 0, max_attempts INT DEFAULT 5,
  run_after TEXT, locked_by TEXT, locked_at TEXT, error TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS geocache(city TEXT PRIMARY KEY, bbox TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_biz_city_rubric ON businesses(city, rubric);
CREATE INDEX IF NOT EXISTS ix_biz_kind ON businesses(site_kind);
CREATE INDEX IF NOT EXISTS ix_biz_collected ON businesses(collected_at);
CREATE INDEX IF NOT EXISTS ix_sites_status ON sites(analysis_status, http_status);
CREATE INDEX IF NOT EXISTS ix_bs_domain ON business_sites(domain);
CREATE INDEX IF NOT EXISTS ix_jobs_pick ON jobs(status, run_after, priority);
"""

SORTS = {
    "domain": "s.domain", "name": "name", "city": "city", "rubric": "rubric",
    "http_status": "s.http_status", "analysis_status": "s.analysis_status", "cards": "cards",
}


class Storage:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(sites)")]
        if "note" not in cols:                       # миграция старых баз
            self.conn.execute("ALTER TABLE sites ADD COLUMN note TEXT")

    # ---- низкий уровень
    def exec(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params)

    def rows(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        r = self.rows(sql, params)
        return r[0] if r else None

    # ---- данные
    def upsert_business(self, c, city: str, rubric: str, raw_url: str | None, kind: str) -> None:
        self.exec(
            """INSERT INTO businesses(dgis_id,name,address,city,rubric,phones,raw_url,site_kind,collected_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dgis_id) DO UPDATE SET name=excluded.name, address=excluded.address,
                 phones=excluded.phones, raw_url=excluded.raw_url, site_kind=excluded.site_kind""",
            (c.dgis_id, c.name, c.address, city, rubric, json.dumps(c.phones, ensure_ascii=False),
             raw_url, kind, now_iso()),
        )

    def upsert_site(self, domain: str, url: str) -> None:
        self.exec(
            "INSERT INTO sites(domain,url) VALUES(?,?) ON CONFLICT(domain) DO UPDATE SET url=excluded.url",
            (domain, url),
        )

    def link(self, dgis_id: str, domain: str) -> None:
        self.exec("INSERT OR IGNORE INTO business_sites(dgis_id,domain) VALUES(?,?)", (dgis_id, domain))

    def site_needs_check(self, domain: str) -> bool:
        r = self.one("SELECT checked_at FROM sites WHERE domain=?", (domain,))
        return r is None or r["checked_at"] is None

    def set_site_check(self, domain: str, status: int, final_url: str | None, note: str | None = None) -> None:
        self.exec("UPDATE sites SET http_status=?, final_url=?, note=?, checked_at=? WHERE domain=?",
                  (status, final_url, note, now_iso(), domain))

    def requeue_checks(self) -> int:
        n = 0
        for r in self.rows("SELECT domain,url FROM sites"):
            if self.enqueue("check_site", {"domain": r["domain"], "url": r["url"]},
                            uniq=f"check:{r['domain']}", force=True):
                n += 1
        return n

    # ---- очередь
    def enqueue(self, type_: str, payload: dict, uniq: str, priority: int = 5, force: bool = False,
                max_attempts: int = 5) -> bool:
        ts = now_iso()
        data = (type_, json.dumps(payload, ensure_ascii=False), uniq, priority, max_attempts, ts, ts, ts)
        with self.lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO jobs(type,payload,uniq,priority,max_attempts,run_after,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""", data)
            if cur.rowcount:
                return True
            if force:
                cur = self.conn.execute(
                    """UPDATE jobs SET status='queued', attempts=0, error=NULL, run_after=?, updated_at=?
                       WHERE uniq=? AND status IN ('done','failed','cancelled')""", (ts, ts, uniq))
                return bool(cur.rowcount)
        return False

    def claim(self, types: tuple[str, ...], worker: str) -> dict | None:
        marks = ",".join("?" * len(types))
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                r = self.conn.execute(
                    f"""SELECT * FROM jobs WHERE status='queued' AND run_after<=? AND type IN ({marks})
                        ORDER BY priority, id LIMIT 1""", (now_iso(), *types)).fetchone()
                if not r:
                    self.conn.execute("COMMIT")
                    return None
                ts = now_iso()
                self.conn.execute(
                    "UPDATE jobs SET status='running', locked_by=?, locked_at=?, updated_at=? WHERE id=?",
                    (worker, ts, ts, r["id"]))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        job = dict(r)
        job["payload"] = json.loads(job["payload"])
        return job

    def finish(self, job_id: int) -> None:
        self.exec("UPDATE jobs SET status='done', error=NULL, updated_at=? WHERE id=?", (now_iso(), job_id))

    def retry(self, job: dict, error: str, delay: float) -> str:
        attempts = job["attempts"] + 1
        if attempts >= job["max_attempts"]:
            status, run_after = "failed", job.get("run_after") or now_iso()
        else:
            status, run_after = "queued", now_iso(delay)
        self.exec(
            "UPDATE jobs SET status=?, attempts=?, error=?, run_after=?, locked_by=NULL, updated_at=? WHERE id=?",
            (status, attempts, error[:500], run_after, now_iso(), job["id"]))
        return status

    def requeue_running(self) -> int:
        return self.exec("UPDATE jobs SET status='queued', locked_by=NULL WHERE status='running'").rowcount

    def retry_failed(self) -> int:
        return self.exec(
            "UPDATE jobs SET status='queued', attempts=0, error=NULL, run_after=? WHERE status='failed'",
            (now_iso(),)).rowcount

    def active_jobs(self) -> int:
        return self.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running')")["n"]

    def job_counts(self) -> dict:
        return {r["status"]: r["n"] for r in self.rows("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}

    def recent_jobs(self, type_: str, limit: int = 10) -> list[dict]:
        rows = self.rows("SELECT id,status,payload,attempts,error,updated_at FROM jobs WHERE type=? "
                         "ORDER BY id DESC LIMIT ?", (type_, limit))
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def get_geocache(self, city: str) -> str | None:
        r = self.one("SELECT bbox FROM geocache WHERE city=?", (city,))
        return r["bbox"] if r else None

    def set_geocache(self, city: str, bbox: str) -> None:
        self.exec("INSERT OR REPLACE INTO geocache(city,bbox,created_at) VALUES(?,?,?)", (city, bbox, now_iso()))

    def failed_jobs(self, limit: int = 50) -> list[dict]:
        return self.rows("SELECT id,type,payload,attempts,error,updated_at FROM jobs "
                         "WHERE status='failed' ORDER BY id DESC LIMIT ?", (limit,))

    # ---- статистика
    def stats(self) -> dict:
        kinds = {r["site_kind"]: r["n"] for r in
                 self.rows("SELECT site_kind, COUNT(*) n FROM businesses GROUP BY site_kind")}
        total = sum(kinds.values())
        s = self.one("""SELECT COUNT(*) sites,
                SUM(CASE WHEN http_status BETWEEN 200 AND 399 THEN 1 ELSE 0 END) ok,
                SUM(CASE WHEN checked_at IS NULL THEN 1 ELSE 0 END) unchecked,
                SUM(CASE WHEN note='protected' THEN 1 ELSE 0 END) protected,
                SUM(CASE WHEN note LIKE 'error:%' THEN 1 ELSE 0 END) errors FROM sites""")
        per_min = self.one("SELECT COUNT(*) n FROM businesses WHERE collected_at>=?", (now_iso(-60),))["n"]
        jobs = self.job_counts()
        return {
            "cards": total, "own": kinds.get("own", 0), "social": kinds.get("social", 0),
            "marketplace": kinds.get("marketplace", 0), "none": kinds.get("none", 0),
            "with_site": total - kinds.get("none", 0),
            "sites": s["sites"] or 0, "sites_ok": s["ok"] or 0, "sites_unchecked": s["unchecked"] or 0,
            "sites_protected": s["protected"] or 0, "sites_error": s["errors"] or 0,
            "error_reasons": {r["note"][6:]: r["n"] for r in self.rows(
                "SELECT note, COUNT(*) n FROM sites WHERE note LIKE 'error:%' GROUP BY note ORDER BY n DESC")},
            "per_min": per_min, "jobs": jobs, "errors": jobs.get("failed", 0),
        }

    # ---- выборка для UI и экспорта
    @staticmethod
    def _where(f: dict) -> tuple[str, list]:
        w, p = ["1=1"], []
        if f.get("city"):
            w.append("b.city=?"); p.append(f["city"])
        if f.get("rubric"):
            w.append("b.rubric=?"); p.append(f["rubric"])
        st = f.get("status")
        if st == "ok":
            w.append("s.http_status BETWEEN 200 AND 399 AND COALESCE(s.note,'')<>'parked'")
        elif st == "parked":
            w.append("s.note='parked'")
        elif st == "redirect":
            w.append("s.note LIKE 'redirect:%'")
        elif st == "alive":      # открывается или просто не пускает бота (protected), не парковка
            w.append("(s.http_status BETWEEN 200 AND 399 OR s.note='protected') AND COALESCE(s.note,'')<>'parked'")
        elif st == "protected":
            w.append("s.note='protected'")
        elif st == "error":
            w.append("s.note LIKE 'error:%'")
        elif st == "bad":
            w.append("s.checked_at IS NOT NULL AND (s.http_status IS NULL OR s.http_status NOT BETWEEN 200 AND 399)"
                     " AND COALESCE(s.note,'')<>'protected'")
        elif st == "unchecked":
            w.append("s.checked_at IS NULL")
        if f.get("analysis"):
            w.append("s.analysis_status=?"); p.append(f["analysis"])
        if f.get("q"):
            w.append("(s.domain LIKE ? OR b.name LIKE ?)"); p += [f"%{f['q']}%"] * 2
        return " AND ".join(w), p

    _FROM = ("FROM sites s JOIN business_sites bs ON bs.domain=s.domain "
             "JOIN businesses b ON b.dgis_id=bs.dgis_id")

    def count_sites(self, f: dict) -> int:
        w, p = self._where(f)
        return self.one(f"SELECT COUNT(DISTINCT s.domain) n {self._FROM} WHERE {w}", p)["n"]

    def list_sites(self, f: dict, limit: int = 50, offset: int = 0, sort: str = "domain", desc: bool = False):
        w, p = self._where(f)
        order = SORTS.get(sort, "s.domain") + (" DESC" if desc else " ASC")
        return self.rows(
            f"""SELECT s.domain, s.url, s.http_status, s.analysis_status, s.note,
                MIN(b.name) name, MIN(b.city) city, MIN(b.rubric) rubric, MIN(b.phones) phones,
                COUNT(b.dgis_id) cards {self._FROM} WHERE {w} GROUP BY s.domain
                ORDER BY {order} LIMIT ? OFFSET ?""", (*p, limit, offset))

    def export_sites(self, f: dict) -> list[dict]:
        w, p = self._where(f)
        out = []
        for r in self.rows(
            f"""SELECT s.domain, s.url, s.http_status, s.final_url, s.analysis_status, s.note,
                MIN(b.city) city, MIN(b.rubric) rubric,
                json_group_array(b.name) names, json_group_array(b.phones) phones
                {self._FROM} WHERE {w} GROUP BY s.domain ORDER BY s.domain""", p):
            names = list(dict.fromkeys(json.loads(r["names"])))
            phones: list[str] = []
            for blob in json.loads(r["phones"]):
                for ph in json.loads(blob or "[]"):
                    ph = normalize_phone(ph) or ph
                    if ph not in phones:
                        phones.append(ph)
            note = r["note"]
            # для анализа: если сайт переехал на другой домен, берём конечный адрес
            target = r["final_url"] if (note or "").startswith("redirect:") and r["final_url"] else r["url"]
            out.append({"domain": r["domain"], "url": r["url"], "business_names": names,
                        "city": r["city"], "rubric": r["rubric"], "phones": phones,
                        "http_status": r["http_status"], "final_url": r["final_url"],
                        "note": note, "target_url": target, "analysis_status": r["analysis_status"]})
        return out

    def filter_values(self) -> dict:
        return {"cities": [r["city"] for r in self.rows("SELECT DISTINCT city FROM businesses ORDER BY city")],
                "rubrics": [r["rubric"] for r in self.rows("SELECT DISTINCT rubric FROM businesses ORDER BY rubric")]}
