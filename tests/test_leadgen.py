import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from dgis.config import Config
from dgis.pipeline.ingest import store_card
from dgis.pipeline.leadgen import LeadgenClient, LeadgenError, make_client, submit_new, sync_statuses
from dgis.sources.base import Card
from dgis.storage import Storage
from dgis.ui.security import ui_security_problem

TOKEN = "secret-token"


class FakeApi(ThreadingHTTPServer):
    """Имитация Laravel: POST /api/internal/lead-targets, GET /api/internal/lead-targets/{domain}."""
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.posts, self.fail_next, self.item_status, self.scan_status = [], [], {}, {}
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/api/internal"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"message": "Unauthenticated"})
            return False
        return True

    def do_POST(self):
        if not self._auth():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        s = self.server
        if s.fail_next:
            code, headers = s.fail_next.pop(0)
            return self._send(code, {}, headers)
        s.posts.append(body)
        results = []
        for it in body["items"]:
            st = s.item_status.get(it["domain"], "queued")
            r = {"domain": it["domain"], "status": st}
            if st == "rejected":
                r["reason"] = "invalid_url"
            else:
                r["scan_id"] = "scan-" + it["domain"]
            results.append(r)
        self._send(202, {"batch_id": body["batch_id"], "results": results})

    def do_GET(self):
        if not self._auth():
            return
        domain = unquote(self.path.rsplit("/", 1)[1])
        st = self.server.scan_status.get(domain)
        if st is None:
            return self._send(404, {"message": "not found"})
        self._send(200, {"domain": domain, "status": st})


def make_db(sites):
    db = Storage(":memory:")
    for i, (dom, status, note) in enumerate(sites):
        store_card(db, Card(str(i), "Орг " + dom, "", ["8 (843) 245-21-12"], ["https://" + dom]), "Казань", "dental")
        db.set_site_check(dom, status, f"https://{dom}/", note)
    return db


class TestLeadgen(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.slept = []
        self.client = LeadgenClient(self.api.url, TOKEN, timeout=5, sleep=self.slept.append)

    def tearDown(self):
        self.api.shutdown()
        self.api.server_close()

    def test_submit_only_alive_and_payload(self):
        db = make_db([("good-clinic.ru", 200, None), ("prot-clinic.ru", 403, "protected"),
                      ("park-clinic.ru", 200, "parked"), ("dead-clinic.ru", 0, "error:dns")])
        res = submit_new(db, self.client)
        self.assertEqual((res["selected"], res["queued"], res["error"]), (2, 2, None))
        items = {i["domain"]: i for p in self.api.posts for i in p["items"]}
        self.assertEqual(set(items), {"good-clinic.ru", "prot-clinic.ru"})
        it = items["prot-clinic.ru"]
        self.assertEqual((it["scan_hint"], it["city"], it["phones"], it["target_url"]),
                         ("protected", "Казань", ["+78432452112"], "https://prot-clinic.ru/"))
        self.assertEqual(items["good-clinic.ru"]["scan_hint"], "ok")
        rows = {r["domain"]: r for r in db.rows("SELECT * FROM sites")}
        self.assertEqual(rows["good-clinic.ru"]["analysis_status"], "submitted")
        self.assertEqual(rows["good-clinic.ru"]["scan_ref"], "scan-good-clinic.ru")
        self.assertEqual(rows["park-clinic.ru"]["analysis_status"], "new")
        self.assertEqual(submit_new(db, self.client)["selected"], 0)          # повтор ничего не шлёт

    def test_batches(self):
        db = make_db([(f"c{i}-clinic.ru", 200, None) for i in range(5)])
        res = submit_new(db, self.client, batch_size=2)
        self.assertEqual((res["queued"], len(self.api.posts), [len(p["items"]) for p in self.api.posts]),
                         (5, 3, [2, 2, 1]))
        self.assertEqual(len({p["batch_id"] for p in self.api.posts}), 3)
        db2 = make_db([(f"d{i}-clinic.ru", 200, None) for i in range(5)])
        self.assertEqual(submit_new(db2, self.client, limit=3)["selected"], 3)

    def test_retry_after_then_ok(self):
        db = make_db([("good-clinic.ru", 200, None)])
        self.api.fail_next = [(429, {"Retry-After": "7"}), (503, {})]
        res = submit_new(db, self.client)
        self.assertEqual((res["queued"], res["error"]), (1, None))
        self.assertEqual(self.slept, [7.0, 10.0])                               # Retry-After, затем 5*(попытка+1)

    def test_auth_failure_keeps_new(self):
        db = make_db([("good-clinic.ru", 200, None)])
        bad = LeadgenClient(self.api.url, "wrong", timeout=5, sleep=self.slept.append)
        res = submit_new(db, bad)
        self.assertIn("LEADGEN_TOKEN", res["error"])
        self.assertEqual(db.one("SELECT analysis_status s FROM sites")["s"], "new")

    def test_duplicate_and_rejected(self):
        db = make_db([("a-clinic.ru", 200, None), ("b-clinic.ru", 200, None), ("c-clinic.ru", 200, None)])
        self.api.item_status = {"a-clinic.ru": "duplicate", "b-clinic.ru": "rejected"}
        res = submit_new(db, self.client)
        self.assertEqual((res["queued"], res["duplicate"], res["rejected"]), (1, 1, 1))
        r = {x["domain"]: x for x in db.rows("SELECT * FROM sites")}
        self.assertEqual(r["a-clinic.ru"]["analysis_status"], "submitted")
        self.assertEqual((r["b-clinic.ru"]["analysis_status"], r["b-clinic.ru"]["scan_status"]),
                         ("rejected", "rejected:invalid_url"))

    def test_waiting_counts_as_submitted(self):
        db = make_db([("w-clinic.ru", 200, None)])
        self.api.item_status = {"w-clinic.ru": "waiting"}
        res = submit_new(db, self.client)
        self.assertEqual((res["waiting"], res["missing"]), (1, 0))
        row = db.one("SELECT analysis_status s, scan_status t FROM sites")
        self.assertEqual((row["s"], row["t"]), ("submitted", "waiting"))

    def test_network_error_keeps_new(self):
        db = make_db([("good-clinic.ru", 200, None)])
        dead = LeadgenClient("http://127.0.0.1:1/api/internal", TOKEN, timeout=1, sleep=self.slept.append)
        res = submit_new(db, dead)
        self.assertIn("нет связи", res["error"])
        self.assertEqual(db.one("SELECT analysis_status s FROM sites")["s"], "new")

    def test_sync(self):
        db = make_db([(f"{n}-clinic.ru", 200, None) for n in ("done", "block", "fail", "wait", "lost")])
        submit_new(db, self.client)
        self.api.scan_status = {"done-clinic.ru": "done", "block-clinic.ru": "blocked", "fail-clinic.ru": "failed",
                                "wait-clinic.ru": "scanning"}
        res = sync_statuses(db, self.client)
        self.assertEqual((res["done"], res["blocked"], res["failed"], res["pending"], res["lost"]), (1, 1, 1, 1, 1))
        st = {r["domain"]: r["analysis_status"] for r in db.rows("SELECT domain, analysis_status FROM sites")}
        self.assertEqual(st, {"done-clinic.ru": "done", "block-clinic.ru": "blocked", "fail-clinic.ru": "failed",
                              "wait-clinic.ru": "submitted", "lost-clinic.ru": "submitted"})
        self.assertEqual(db.one("SELECT scan_status s FROM sites WHERE domain='wait-clinic.ru'")["s"], "scanning")

    def test_config_and_security(self):
        with self.assertRaises(LeadgenError):
            make_client(Config())
        self.assertIsNotNone(make_client(Config(leadgen_url="http://backend:8000/api/internal", leadgen_token="x")))
        self.assertIsNone(ui_security_problem(Config(ui_host="127.0.0.1")))
        self.assertIn("UI_USER", ui_security_problem(Config(ui_host="0.0.0.0")))
        self.assertIsNone(ui_security_problem(Config(ui_host="0.0.0.0", ui_user="a", ui_password="b")))

    def test_migration_adds_submit_columns(self):
        db = Storage(":memory:")
        cols = [r["name"] for r in db.rows("PRAGMA table_info(sites)")]
        for c in ("submitted_at", "scan_ref", "scan_status", "note"):
            self.assertIn(c, cols)


if __name__ == "__main__":
    unittest.main()
