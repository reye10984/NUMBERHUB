import os, tempfile, unittest
from pathlib import Path
from unittest import mock
from app import create_app, provider

class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"FIVE_SIM_API_KEY": "", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_WEBHOOK_SECRET": ""}, clear=False)
        self.env.start(); self.addCleanup(self.env.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app = create_app({"DATABASE": str(Path(self.tmp.name) / "test.db"), "TESTING": True, "SECRET_KEY": "test-secret"})
        self.c = self.app.test_client()
    def token(self):
        self.c.get("/login")
        with self.c.session_transaction() as s: return s["csrf"]
    def post(self, url, **data):
        data["csrf_token"] = self.token()
        return self.c.post(url, data=data, follow_redirects=True)
    def signup(self):
        self.post("/register", email="a@example.com", password="password123")
        return self.post("/login", email="a@example.com", password="password123")

class AppTests(Base):
    def test_health_does_not_claim_5sim_configured_without_key(self):
        self.assertEqual(self.c.get("/health").get_json()["mode"], "unconfigured")
    def test_dashboard_requires_login(self):
        self.assertEqual(self.c.get("/dashboard").status_code, 302)
    def test_password_hashed_and_login(self):
        self.signup()
        import sqlite3
        h=sqlite3.connect(self.app.config["DATABASE"]).execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertNotIn("password123", h)
        self.assertIn(b"Dashboard", self.c.get("/dashboard").data)
    def test_csrf_enforced(self):
        self.assertEqual(self.c.post("/login", data={"email":"a","password":"b"}).status_code, 400)
    def test_old_request_route_redirects_to_5sim(self):
        self.signup()
        r=self.c.get("/request")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/catalogue", r.headers["Location"])
    def test_demo_endpoints_are_removed(self):
        self.signup()
        self.assertEqual(self.c.post("/wallet/demo-credit", data={"csrf_token":self.token()}).status_code, 404)
        self.assertEqual(self.c.post("/demo/simulate", data={"csrf_token":self.token()}).status_code, 404)
    def test_5sim_provider_mode_when_key_set(self):
        with mock.patch.dict(os.environ, {"FIVE_SIM_API_KEY":"test-key"}):
            self.assertEqual(provider()["mode"], "5sim")

if __name__ == "__main__": unittest.main()
