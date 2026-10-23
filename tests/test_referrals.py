import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import create_app


class ReferralTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(
            os.environ,
            {
                "FIVE_SIM_API_KEY": "",
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_WEBHOOK_SECRET": "",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.app = create_app({
            "DATABASE": str(Path(self.tmp.name) / "referrals.db"),
            "TESTING": True,
            "SECRET_KEY": "referral-test-secret",
        })
        self.client = self.app.test_client()

    def csrf_token(self):
        self.client.get("/register")
        with self.client.session_transaction() as session:
            return session["csrf"]

    def register(self, email, ref=""):
        return self.client.post(
            "/register",
            data={
                "csrf_token": self.csrf_token(),
                "email": email,
                "password": "password123",
                "ref": ref,
            },
            follow_redirects=True,
        )

    def login(self, email):
        return self.client.post(
            "/login",
            data={
                "csrf_token": self.csrf_token(),
                "email": email,
                "password": "password123",
            },
            follow_redirects=True,
        )

    def get_user(self, email):
        with self.app.app_context():
            from flask import current_app
            from app import db

            row = db().execute(
                "SELECT id, referral_code, referred_by FROM users WHERE email=?",
                (email,),
            ).fetchone()
            return dict(row) if row else None

    def test_duplicate_email_does_not_create_second_account(self):
        self.register("duplicate@example.com")
        self.register("duplicate@example.com")

        with self.app.app_context():
            from app import db

            count = db().execute(
                "SELECT COUNT(*) FROM users WHERE email=?",
                ("duplicate@example.com",),
            ).fetchone()[0]

        self.assertEqual(count, 1)

    def test_new_accounts_receive_unique_referral_codes(self):
        self.register("first@example.com")
        self.register("second@example.com")

        first = self.get_user("first@example.com")
        second = self.get_user("second@example.com")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertTrue(first["referral_code"])
        self.assertTrue(second["referral_code"])
        self.assertNotEqual(
            first["referral_code"],
            second["referral_code"],
        )

    def test_registration_through_referral_link_is_attributed(self):
        self.register("inviter@example.com")
        inviter = self.get_user("inviter@example.com")

        response = self.register(
            "friend@example.com",
            ref=inviter["referral_code"],
        )
        self.assertEqual(response.status_code, 200)

        friend = self.get_user("friend@example.com")
        self.assertEqual(friend["referred_by"], inviter["id"])

    def test_invalid_referral_code_does_not_create_attribution(self):
        self.register(
            "friend@example.com",
            ref="this-code-does-not-exist",
        )

        friend = self.get_user("friend@example.com")
        self.assertIsNotNone(friend)
        self.assertIsNone(friend["referred_by"])

    def test_dashboard_displays_link_and_referral_count(self):
        self.register("inviter@example.com")
        inviter = self.get_user("inviter@example.com")

        self.register("friend@example.com", ref=inviter["referral_code"])

        self.client.get("/login")
        self.login("inviter@example.com")
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your referral link", response.data)
        self.assertIn(b"1 successful registration", response.data)
        self.assertIn(inviter["referral_code"].encode(), response.data)
        self.assertIn(b"nh-copy-referral", response.data)
        self.assertIn(b"wa.me", response.data)

    def test_registration_page_preserves_referral_code(self):
        self.register("inviter@example.com")
        inviter = self.get_user("inviter@example.com")

        response = self.client.get(
            "/register?ref=" + inviter["referral_code"]
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            f'name="ref" value="{inviter["referral_code"]}"'.encode(),
            response.data,
        )


    def test_existing_database_keeps_users_and_adds_referral_codes(self):
        database_path = Path(self.tmp.name) / "existing.db"

        # Simulate a database created before referral support.
        old_db = sqlite3.connect(database_path)
        old_db.execute(
            """CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        old_db.execute(
            """INSERT INTO users
               (id, email, password_hash, created_at)
               VALUES (1, ?, ?, ?)""",
            ("existing@example.com", "existing-hash", "2026-01-01 00:00:00"),
        )
        old_db.commit()
        old_db.close()

        # App startup should migrate this isolated test database.
        migrated_app = create_app({
            "DATABASE": str(database_path),
            "TESTING": True,
            "SECRET_KEY": "migration-test-secret",
        })

        with migrated_app.app_context():
            from app import db

            user = db().execute(
                """SELECT id, email, password_hash, referral_code, referred_by
                   FROM users WHERE id=?""",
                (1,),
            ).fetchone()

            self.assertIsNotNone(user)
            self.assertEqual(user["email"], "existing@example.com")
            self.assertEqual(user["password_hash"], "existing-hash")
            self.assertTrue(user["referral_code"])
            self.assertIsNone(user["referred_by"])

            columns = {
                row[1]
                for row in db().execute("PRAGMA table_info(users)")
            }
            self.assertIn("referral_code", columns)
            self.assertIn("referred_by", columns)

if __name__ == "__main__":
    unittest.main()
