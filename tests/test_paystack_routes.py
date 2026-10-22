import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import create_app


class PaystackRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.env = mock.patch.dict(
            os.environ,
            {
                "PUBLIC_BASE_URL": "https://numberhub-ixo3.onrender.com",
                "PAYSTACK_CURRENCY": "NGN",
                "WALLET_MIN_DEPOSIT_KOBO": "10000",
                "WALLET_MAX_DEPOSIT_KOBO": "50000000",
                "PAYSTACK_SECRET_KEY": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "paystack-route-test-secret",
            "DATABASE": str(Path(self.tmp.name) / "test.db"),
        })
        self.client = self.app.test_client()

        self._post(
            "/register",
            email="payer@example.com",
            password="password123",
        )
        self._post(
            "/login",
            email="payer@example.com",
            password="password123",
        )

        with self.client.session_transaction() as session:
            self.user_id = session["uid"]

    def _token(self):
        self.client.get("/login")
        with self.client.session_transaction() as session:
            return session["csrf"]

    def _post(self, path, **data):
        data["csrf_token"] = self._token()
        return self.client.post(path, data=data)

    def _database(self):
        connection = sqlite3.connect(self.app.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        return connection

    def _balance(self):
        connection = self._database()
        try:
            row = connection.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) "
                "FROM ledger WHERE user_id=?",
                (self.user_id,),
            ).fetchone()
            return row[0]
        finally:
            connection.close()

    def _create_pending_payment(self, reference="nh_test_reference_123"):
        connection = self._database()
        try:
            connection.execute(
                """
                INSERT INTO wallet_pending_payments
                    (reference, user_id, amount_kobo, currency,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, 'NGN', 'pending', ?, ?)
                """,
                (
                    reference,
                    self.user_id,
                    25000,
                    "2026-10-22 12:00:00",
                    "2026-10-22 12:00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return reference

    def test_deposit_requires_login(self):
        with self.client.session_transaction() as session:
            session.clear()

        response = self.client.post(
            "/wallet/deposit",
            data={"amount": "250", "csrf_token": self._token()},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_invalid_amount_does_not_initialize_payment(self):
        for amount in ("0", "-10", "99", "500001", "1.234", "abc"):
            with self.subTest(amount=amount):
                with mock.patch("app.initialize_transaction") as initialize:
                    self._post("/wallet/deposit", amount=amount)
                    initialize.assert_not_called()
                self.assertEqual(self._balance(), 0)

    def test_initialization_failure_does_not_credit_wallet(self):
        from paystack import PaystackError

        with mock.patch(
            "app.initialize_transaction",
            side_effect=PaystackError("simulated failure"),
        ):
            self._post("/wallet/deposit", amount="250")

        self.assertEqual(self._balance(), 0)

    def test_initialization_success_redirects_to_checkout(self):
        with mock.patch(
            "app.initialize_transaction",
            return_value={
                "authorization_url": "https://checkout.paystack.com/test",
                "reference": "nh_test_reference_123",
            },
        ):
            response = self._post("/wallet/deposit", amount="250")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            "https://checkout.paystack.com/test",
        )
        self.assertEqual(self._balance(), 0)

    def test_invalid_callback_reference_does_not_credit(self):
        response = self.client.get(
            "/payments/paystack/callback?reference=bad"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._balance(), 0)

    def test_failed_verification_does_not_credit(self):
        reference = self._create_pending_payment()

        with mock.patch(
            "app.verify_transaction",
            side_effect=__import__("app").PaystackError("not successful"),
        ):
            self.client.get(
                "/payments/paystack/callback",
                query_string={"reference": reference},
            )

        self.assertEqual(self._balance(), 0)

    def test_successful_verification_credits_wallet_once(self):
        reference = self._create_pending_payment()

        verified = {
            "reference": reference,
            "status": "success",
            "amount": 25000,
            "currency": "NGN",
            "customer": {"email": "payer@example.com"},
        }

        with mock.patch(
            "app.verify_transaction",
            return_value=verified,
        ) as verify:
            self.client.get(
                "/payments/paystack/callback",
                query_string={"reference": reference},
            )
            self.assertEqual(self._balance(), 25000)

            self.client.get(
                "/payments/paystack/callback",
                query_string={"reference": reference},
            )
            self.assertEqual(self._balance(), 25000)

        verify.assert_called_once()

    def test_currency_mismatch_does_not_credit(self):
        reference = self._create_pending_payment()

        with mock.patch(
            "app.verify_transaction",
            return_value={
                "reference": reference,
                "status": "success",
                "amount": 25000,
                "currency": "USD",
                "customer": {"email": "payer@example.com"},
            },
        ):
            self.client.get(
                "/payments/paystack/callback",
                query_string={"reference": reference},
            )

        self.assertEqual(self._balance(), 0)


if __name__ == "__main__":
    unittest.main()
