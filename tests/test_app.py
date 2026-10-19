import os, re, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app

LIVE = {"DEMO_MODE": "0", "AT_USERNAME": "u", "AT_API_KEY": "k", "AT_NUMBER": "+254700000000", "WEBHOOK_SECRET": "s3"}


class Base(unittest.TestCase):
    env = {"DEMO_MODE": "1"}

    def setUp(self):
        p = mock.patch.dict(os.environ, self.env, clear=False)
        p.start(); self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app = create_app({"DATABASE": os.path.join(self.tmp.name, "t.db"), "TESTING": True})
        self.c = self.app.test_client()

    def tok(self):
        self.c.get("/login")
        with self.c.session_transaction() as s:
            return s["csrf"]

    def post(self, url, **data):
        data["csrf_token"] = self.tok()
        return self.c.post(url, data=data, follow_redirects=True)

    def signup(self, email="a@b.co", pw="password1"):
        self.post("/register", email=email, password=pw)
        return self.post("/login", email=email, password=pw)


class DemoTests(Base):
    def test_health(self):
        self.assertEqual(self.c.get("/health").get_json(), {"status": "ok", "mode": "demo"})

    def test_dashboard_requires_login(self):
        self.assertEqual(self.c.get("/dashboard").status_code, 302)

    def test_password_hashed_and_login(self):
        self.signup()
        import sqlite3
        h = sqlite3.connect(self.app.config["DATABASE"]).execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertNotIn("password1", h)
        self.assertIn(b"Dashboard", self.c.get("/dashboard").data)

    def test_bad_login_and_short_password(self):
        self.assertIn(b"at least 8", self.post("/register", email="x@y.co", password="short").data)
        self.assertIn(b"Incorrect", self.post("/login", email="x@y.co", password="whatever1").data)

    def test_csrf_enforced(self):
        self.assertEqual(self.c.post("/login", data={"email": "a", "password": "b"}).status_code, 400)

    def test_insufficient_balance_blocks_request(self):
        self.signup()
        self.assertIn(b"Insufficient", self.post("/request").data)

    def test_request_debits_ledger(self):
        self.signup()
        self.post("/wallet/demo-credit")
        r = self.post("/request")
        self.assertIn(b"DEMO-", r.data)
        self.assertIn(b"4.00 credits", self.c.get("/dashboard").data)  # 5.00 - 1.00

    def test_demo_message_labelled(self):
        self.signup(); self.post("/wallet/demo-credit"); self.post("/request")
        self.post("/demo/simulate", number_id="1")
        self.assertIn(b"DEMO message", self.c.get("/inbox").data)
        self.assertTrue(self.c.get("/api/messages").get_json()[0]["is_demo"])

    def test_webhook_disabled_in_demo(self):
        self.assertEqual(self.c.post("/webhook/sms").status_code, 503)


class UnconfiguredTests(Base):
    env = {"DEMO_MODE": "0", "AT_USERNAME": "", "AT_API_KEY": "", "AT_NUMBER": "", "WEBHOOK_SECRET": ""}

    def test_clear_error(self):
        self.signup()
        self.assertIn(b"Provider not configured", self.post("/request").data)
        self.assertEqual(self.c.get("/health").get_json()["mode"], "unconfigured")


class LiveTests(Base):
    env = LIVE

    def test_webhook_auth_and_delivery(self):
        self.signup()
        self.assertEqual(self.c.post("/webhook/sms", data={}).status_code, 403)
        self.app.config["NUMBER_PRICE_CENTS"] = 0
        self.post("/request")
        form = {"from": "+254711111111", "to": "+254700000000", "text": "hello", "id": "ATXid_1"}
        self.assertEqual(self.c.post("/webhook/sms?secret=s3", data=form).status_code, 200)
        self.c.post("/webhook/sms?secret=s3", data=form)  # duplicate delivery ignored
        msgs = self.c.get("/api/messages").get_json()
        self.assertEqual(len(msgs), 1); self.assertFalse(msgs[0]["is_demo"])

    def test_no_demo_credit_in_live(self):
        self.signup()
        self.assertIn(b"only available in DEMO", self.post("/wallet/demo-credit").data)


if __name__ == "__main__":
    unittest.main()
