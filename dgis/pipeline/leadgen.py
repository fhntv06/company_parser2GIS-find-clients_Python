"""Передача сайтов этапу 2 (Laravel) по внутреннему API и синхронизация статусов.
Клиент на стандартной библиотеке (urllib): без лишних зависимостей и легко тестируется."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from ..storage import Storage


class LeadgenError(Exception):
    pass


class LeadgenClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30, retries: int = 3,
                 opener=urllib.request.urlopen, sleep=time.sleep):
        self.base, self.token, self.timeout, self.retries = base_url.rstrip("/"), token, timeout, retries
        self._open, self.sleep = opener, sleep

    def _call(self, method: str, path: str, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json",
            "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout) as r:
                return r.status, self._json(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, self._json(e.read()), e.headers
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise LeadgenError(f"нет связи с {self.base}: {getattr(e, 'reason', e)}")

    @staticmethod
    def _json(raw: bytes) -> dict:
        try:
            v = json.loads(raw.decode("utf-8") or "{}")
            return v if isinstance(v, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def _explain(status: int, body: dict) -> str:
        if status in (401, 403):
            return f"доступ отклонён (HTTP {status}): проверьте LEADGEN_TOKEN"
        if status == 422:
            return "ошибка валидации: " + json.dumps(body.get("errors") or body, ensure_ascii=False)[:300]
        return f"HTTP {status}"

    def submit_batch(self, batch_id: str, items: list[dict]) -> dict:
        for attempt in range(self.retries + 1):
            status, body, headers = self._call("POST", "/lead-targets", {"batch_id": batch_id, "items": items})
            if status in (200, 201, 202):
                return body
            if status in (429, 503) and attempt < self.retries:
                try:
                    wait = float(headers.get("Retry-After"))
                except (TypeError, ValueError):
                    wait = 5.0 * (attempt + 1)
                self.sleep(min(wait, 60))
                continue
            raise LeadgenError(self._explain(status, body))
        raise LeadgenError("исчерпаны повторы")

    def status(self, domain: str) -> dict | None:
        code, body, _ = self._call("GET", "/lead-targets/" + urllib.parse.quote(domain, safe=""))
        if code == 404:
            return None
        if code != 200:
            raise LeadgenError(self._explain(code, body))
        return body


def make_client(cfg) -> LeadgenClient:
    if not cfg.leadgen_url or not cfg.leadgen_token:
        raise LeadgenError("не заданы LEADGEN_API_URL и LEADGEN_TOKEN (.env)")
    return LeadgenClient(cfg.leadgen_url, cfg.leadgen_token, cfg.leadgen_timeout)


def build_item(r: dict) -> dict:
    return {k: r[k] for k in ("domain", "target_url", "business_names", "city", "rubric", "phones", "scan_hint")}


def submit_new(db: Storage, client: LeadgenClient, limit: int | None = None, batch_size: int = 50,
               status: str = "alive") -> dict:
    """Отправляет сайты со статусом analysis=new. При сбое связи останавливается, остальные остаются new."""
    rows = db.export_sites({"status": status, "analysis": "new"})
    rows = rows[:limit] if limit else rows
    res = {"selected": len(rows), "queued": 0, "waiting": 0, "duplicate": 0, "rejected": 0, "missing": 0, "error": None}
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        try:
            body = client.submit_batch(str(uuid.uuid4()), [build_item(r) for r in chunk])
        except LeadgenError as e:
            res["error"] = str(e)
            break
        by = {x.get("domain"): x for x in body.get("results", [])}
        for r in chunk:
            x = by.get(r["domain"])
            st = (x or {}).get("status")
            if st in ("queued", "duplicate", "waiting"):   # waiting: этап 2 ждет лимита подписки, сам запустит
                db.set_analysis(r["domain"], "submitted", x.get("scan_id"), st)
                res[st] += 1
            elif st == "rejected":
                db.set_analysis(r["domain"], "rejected", None, "rejected:" + str(x.get("reason")))
                res["rejected"] += 1
            else:
                res["missing"] += 1
    return res


def sync_statuses(db: Storage, client: LeadgenClient, limit: int = 500) -> dict:
    res = {"done": 0, "blocked": 0, "failed": 0, "pending": 0, "lost": 0, "error": None}
    for r in db.rows("SELECT domain FROM sites WHERE analysis_status='submitted' LIMIT ?", (limit,)):
        try:
            s = client.status(r["domain"])
        except LeadgenError as e:
            res["error"] = str(e)
            break
        if s is None:
            res["lost"] += 1
            continue
        st = s.get("status")
        if st in ("done", "blocked", "failed"):
            db.set_analysis(r["domain"], st, None, st)
            res[st] += 1
        else:
            db.exec("UPDATE sites SET scan_status=? WHERE domain=?", (st, r["domain"]))
            res["pending"] += 1
    return res


def run_submit(cfg, db: Storage, limit: int | None = None) -> dict:
    return submit_new(db, make_client(cfg), limit, cfg.leadgen_batch_size)


def run_sync(cfg, db: Storage) -> dict:
    return sync_statuses(db, make_client(cfg))
