"""Wallet-funded 5SIM purchase tests. All provider calls are mocked; no real orders."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from five_sim import FiveSimError


class PurchaseFlowTests(unittest.TestCase):
    PRICE_KOBO = 54600  # $0.28 * ₦1,500/USD * 1.30 markup, rounded to kobo

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "purchases.db")
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "purchase-test-secret",
            "DATABASE": self.db_path,
        })
        self.client = self.app.test_client()
        self.client.get("/register")
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        response = self.client.post("/register", data={
            "email": "purchase-test@example.com",
            "password": "TestPassword123!",
            "csrf_token": csrf,
        })
        self.assertEqual(response.status_code, 302)
        self.client.get("/login")
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        response = self.client.post("/login", data={
            "email": "purchase-test@example.com",
            "password": "TestPassword123!",
            "csrf_token": csrf,
        })
        self.assertEqual(response.status_code, 302)
        # Recreate CSRF token after login clears the session.
        self.client.get("/wallet")
        with self.client.session_transaction() as session:
            self.csrf = session["csrf"]
        with sqlite3.connect(self.db_path) as conn:
            self.user_id = conn.execute(
                "SELECT id FROM users WHERE email=?", ("purchase-test@example.com",)
            ).fetchone()[0]

    def tearDown(self):
        self.tmp.cleanup()

    def fund_wallet(self, amount=100000):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO ledger(user_id,amount_cents,kind,note,created_at) VALUES(?,?,?,?,?)",
                (self.user_id, amount, "test_funding", "test-only wallet funding", "2026-01-01T00:00:00Z"),
            )

    def wallet_balance(self):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) FROM ledger WHERE user_id=?",
                (self.user_id,),
            ).fetchone()[0]

    def attempts(self):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT status, charge_kobo, refund_applied, provider_order_id FROM five_sim_purchase_attempts WHERE user_id=?",
                (self.user_id,),
            ).fetchall()

    def order_count(self):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM five_sim_orders WHERE user_id=?", (self.user_id,)
            ).fetchone()[0]

    def submit(self, token="test-purchase-token"):
        return self.client.post("/catalogue/buy", data={
            "csrf_token": self.csrf,
            "country": "nigeria",
            "product": "whatsapp",
            "purchase_token": token,
        })

    def provider_patches(self, buy_result=None, buy_error=None):
        products = patch("five_sim.get_products", return_value={
            "whatsapp": {"Category": "activation", "Qty": 10, "Price": 0.28},
        })
        profile = patch("five_sim.get_profile", return_value={"balance": 10})
        if buy_error is not None:
            buy = patch("five_sim.buy_activation", side_effect=buy_error)
        else:
            buy = patch("five_sim.buy_activation", return_value=buy_result or {
                "id": 123456, "phone": "+2348000000000", "status": "PENDING", "sms": [],
            })
        return products, profile, buy

    def test_success_debits_wallet_and_saves_order(self):
        self.fund_wallet()
        products, profile, buy = self.provider_patches()
        with products, profile, buy as mock_buy:
            response = self.submit()
        self.assertEqual(response.status_code, 302)
        mock_buy.assert_called_once_with("nigeria", "any", "whatsapp")
        self.assertEqual(self.wallet_balance(), 100000 - self.PRICE_KOBO)
        self.assertEqual(self.order_count(), 1)
        self.assertEqual(self.attempts(), [("succeeded", self.PRICE_KOBO, 0, "123456")])
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT customer_charge_kobo FROM five_sim_orders WHERE provider_order_id='123456'"
            ).fetchone()[0], self.PRICE_KOBO)

    def test_insufficient_wallet_does_not_call_purchase_or_debit(self):
        self.fund_wallet(10000)
        products, profile, buy = self.provider_patches()
        with products, profile, buy as mock_buy:
            response = self.submit()
        self.assertEqual(response.status_code, 302)
        mock_buy.assert_not_called()
        self.assertEqual(self.wallet_balance(), 10000)
        self.assertEqual(self.attempts(), [])

    def test_definitive_rejection_refunds_once_and_duplicate_token_is_blocked(self):
        self.fund_wallet()
        products, profile, buy = self.provider_patches(buy_error=FiveSimError("rejected", definitive=True))
        with products, profile, buy as mock_buy:
            self.submit()
            self.submit()
        mock_buy.assert_called_once()
        self.assertEqual(self.wallet_balance(), 100000)
        self.assertEqual(self.attempts(), [("failed", self.PRICE_KOBO, 1, None)])
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE user_id=? AND kind='purchase_refund'", (self.user_id,)
            ).fetchone()[0], 1)

    def test_uncertain_provider_result_keeps_funds_held_and_blocks_retry(self):
        self.fund_wallet()
        products, profile, buy = self.provider_patches(buy_error=FiveSimError("timeout", definitive=False))
        with products, profile, buy as mock_buy:
            self.submit()
            self.submit()
        mock_buy.assert_called_once()
        self.assertEqual(self.wallet_balance(), 100000 - self.PRICE_KOBO)
        self.assertEqual(self.attempts(), [("uncertain", self.PRICE_KOBO, 0, None)])
        self.assertEqual(self.order_count(), 0)

    def test_duplicate_success_submission_does_not_buy_or_charge_twice(self):
        self.fund_wallet()
        products, profile, buy = self.provider_patches()
        with products, profile, buy as mock_buy:
            self.submit()
            self.submit()
        mock_buy.assert_called_once()
        self.assertEqual(self.wallet_balance(), 100000 - self.PRICE_KOBO)
        self.assertEqual(self.order_count(), 1)
        self.assertEqual(len(self.attempts()), 1)


if __name__ == "__main__":
    unittest.main()
