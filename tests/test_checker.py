import asyncio
import unittest
from types import SimpleNamespace

from dgis.config import Config
from dgis.normalize import site_note
from dgis.pipeline import runner as runner_mod
from dgis.pipeline.checker import check_url, classify_error, is_transient
from dgis.pipeline.runner import Runner
from dgis.sources.base import Card
from dgis.pipeline.ingest import store_card
from dgis.storage import Storage


def exc(name, msg=""):
    return type(name, (Exception,), {})(msg)


class TestClassify(unittest.TestCase):
    def test_reasons(self):
        cases = [(exc("ReadTimeout"), "timeout"), (exc("ConnectTimeout"), "timeout"),
                 (exc("ConnectError", "[Errno 11001] getaddrinfo failed"), "dns"),
                 (exc("ConnectError", "[Errno -2] Name or service not known"), "dns"),
                 (exc("ConnectError", "[SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED] unsafe legacy"), "ssl"),
                 (exc("ConnectError", "SSL handshake failure"), "ssl"),
                 (exc("RemoteProtocolError", "Server disconnected without sending a response."), "reset"),
                 (exc("ReadError", ""), "reset"),
                 (exc("TooManyRedirects", "Exceeded maximum allowed redirects."), "redirects"),
                 (exc("ConnectError", "[WinError 10061] connection refused"), "connect"),
                 (exc("WeirdThing"), "weirdthing")]
        for e, want in cases:
            self.assertEqual(classify_error(e), want, (type(e).__name__, str(e)))

    def test_transient(self):
        self.assertTrue(is_transient("timeout"))
        self.assertTrue(is_transient("ssl/timeout"))
        self.assertFalse(is_transient("ssl"))
        self.assertFalse(is_transient("redirects"))
        self.assertFalse(is_transient(None))


class FakeClient:
    """Сценарий ответов по порядку: исключение или (status, url)."""
    def __init__(self, script):
        self.script, self.urls = list(script), []

    async def get(self, url):
        self.urls.append(url)
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(status_code=r[0], url=r[1])


class TestCheckUrl(unittest.TestCase):
    def run_check(self, script):
        c = FakeClient(script)
        return asyncio.run(check_url(c, "https://a.ru/")), c

    def test_https_ok(self):
        (res, c) = self.run_check([(200, "https://a.ru/")])
        self.assertEqual(res, (200, "https://a.ru/", None))
        self.assertEqual(c.urls, ["https://a.ru/"])

    def test_falls_back_to_http(self):
        res, c = self.run_check([exc("ConnectError", "[SSL] handshake"), (200, "http://a.ru/")])
        self.assertEqual(res, (200, "http://a.ru/", None))
        self.assertEqual(c.urls, ["https://a.ru/", "http://a.ru/"])

    def test_any_http_answer_counts_as_alive(self):
        res, _ = self.run_check([(403, "https://a.ru/")])
        self.assertEqual(res[:2], (403, "https://a.ru/"))

    def test_both_fail(self):
        res, _ = self.run_check([exc("ReadTimeout"), exc("ReadTimeout")])
        self.assertEqual(res, (0, None, "timeout"))
        res, _ = self.run_check([exc("ConnectError", "SSL error"), exc("ConnectError", "refused")])
        self.assertEqual(res, (0, None, "ssl/connect"))


class TestNote(unittest.TestCase):
    def test_note(self):
        self.assertEqual(site_note("a.ru", None, 0, "timeout"), "error:timeout")
        self.assertEqual(site_note("a.ru", "https://a.ru/", 403, None), "protected")
        self.assertEqual(site_note("a.ru", "https://a.ru/", 429, None), "protected")
        self.assertIsNone(site_note("a.ru", "https://a.ru/", 200, None))
        self.assertEqual(site_note("a.ru", "https://b.ru/", 200, None), "redirect:b.ru")
        self.assertIsNone(site_note("a.ru", None))                    # старый вызов без статуса


class TestRetries(unittest.TestCase):
    def setUp(self):
        self._old = runner_mod.CHECK_RETRY_BASE
        runner_mod.CHECK_RETRY_BASE = 0.01                            # не ждём 20/60 секунд

    def tearDown(self):
        runner_mod.CHECK_RETRY_BASE = self._old

    def run_with(self, results):
        db = Storage(":memory:")
        store_card(db, Card("1", "A", "", [], ["https://a.ru"]), "К", "r")
        calls = []

        async def checker(url):
            calls.append(url)
            return results[min(len(calls) - 1, len(results) - 1)]
        asyncio.run(asyncio.wait_for(
            Runner(Config(concurrency=1, check_concurrency=1), db, source=object(), checker=checker).run(), 30))
        return db, calls

    def test_transient_then_success(self):
        db, calls = self.run_with([(0, None, "timeout"), (0, None, "reset"), (200, "https://a.ru/", None)])
        row = db.one("SELECT http_status, note FROM sites")
        self.assertEqual((len(calls), row["http_status"], row["note"]), (3, 200, None))

    def test_transient_persistent_gives_reason(self):
        db, calls = self.run_with([(0, None, "dns")])
        row = db.one("SELECT http_status, note FROM sites")
        self.assertEqual((len(calls), row["http_status"], row["note"]), (3, 0, "error:dns"))
        self.assertEqual(db.job_counts(), {"done": 1})

    def test_permanent_error_no_retry(self):
        db, calls = self.run_with([(0, None, "ssl")])
        self.assertEqual((len(calls), db.one("SELECT note FROM sites")["note"]), (1, "error:ssl"))

    def test_old_two_tuple_checker_still_works(self):
        db, calls = self.run_with([(0, None)])
        self.assertEqual((len(calls), db.one("SELECT http_status FROM sites")["http_status"]), (1, 0))


class TestStatuses(unittest.TestCase):
    def test_filters_and_stats(self):
        db = Storage(":memory:")
        for i, (dom, st, note) in enumerate([("good-clinic.ru", 200, None), ("prot.ru", 403, "protected"),
                                             ("dead.ru", 0, "error:dns"), ("slow.ru", 0, "error:timeout"),
                                             ("gone.ru", 404, None), ("park.ru", 200, "parked")]):
            store_card(db, Card(str(i), dom, "", [], ["https://" + dom]), "К", "r")
            db.set_site_check(dom, st, "https://" + dom + "/", note)
        doms = lambda s: sorted(r["domain"] for r in db.export_sites({"status": s}))
        self.assertEqual(doms("ok"), ["good-clinic.ru"])
        self.assertEqual(doms("alive"), ["good-clinic.ru", "prot.ru"])
        self.assertEqual(doms("protected"), ["prot.ru"])
        self.assertEqual(doms("error"), ["dead.ru", "slow.ru"])
        self.assertEqual(doms("bad"), ["dead.ru", "gone.ru", "slow.ru"])   # защищённые сюда не попадают
        self.assertEqual(doms("parked"), ["park.ru"])
        s = db.stats()
        self.assertEqual((s["sites_protected"], s["sites_error"]), (1, 2))
        self.assertEqual(s["error_reasons"], {"dns": 1, "timeout": 1})


if __name__ == "__main__":
    unittest.main()
