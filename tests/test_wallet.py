import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import create_app
from wallet import (
    ensure_wallet_schema,
    ensure_pending_payment_schema,
    create_pending_payment,
    complete_pending_payment,
    credit_verified_payment,
)


class WalletTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "wallet-test-secret",
            "DATABASE": str(Path(self.tmp.name) / "wallet-test.db"),
        })

        self.connection = sqlite3.connect(
            self.app.config["DATABASE"],
            isolation_level=None,
        )
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.connection.close)

        ensure_wallet_schema(self.connection)

        self.connection.execute(
            """
            INSERT INTO users (email, password_hash, created_at)
            VALUES (?, ?, ?)
            """,
            ("wallet-test@example.com", "test-hash", "2026-01-01 00:00:00"),
        )
        self.user_id = self.connection.execute(
            "SELECT id FROM users WHERE email=?",
            ("wallet-test@example.com",),
        ).fetchone()[0]

    def test_verified_payment_credits_wallet_once(self):
        credited = credit_verified_payment(
            self.connection,
            self.user_id,
            "paystack-ref-0001",
            100000,
            "2026-01-01 00:00:00",
        )

        self.assertTrue(credited)

        balance = self.connection.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM ledger WHERE user_id=?",
            (self.user_id,),
        ).fetchone()[0]

        self.assertEqual(balance, 100000)

    def test_duplicate_reference_does_not_credit_twice(self):
        args = (
            self.connection,
            self.user_id,
            "paystack-ref-0002",
            250000,
            "2026-01-01 00:00:00",
        )

        self.assertTrue(credit_verified_payment(*args))
        self.assertFalse(credit_verified_payment(*args))

        count = self.connection.execute(
            "SELECT COUNT(*) FROM ledger WHERE user_id=?",
            (self.user_id,),
        ).fetchone()[0]

        balance = self.connection.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM ledger WHERE user_id=?",
            (self.user_id,),
        ).fetchone()[0]

        self.assertEqual(count, 1)
        self.assertEqual(balance, 250000)

    def test_invalid_amount_is_rejected(self):
        for amount in (0, -1, 1.5, True, "1000"):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    credit_verified_payment(
                        self.connection,
                        self.user_id,
                        "paystack-ref-invalid",
                        amount,
                        "2026-01-01 00:00:00",
                    )

    def test_invalid_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            credit_verified_payment(
                self.connection,
                self.user_id,
                "bad ref",
                1000,
                "2026-01-01 00:00:00",
            )

    def test_nonexistent_user_is_rejected_without_ledger_entry(self):
        with self.assertRaises(ValueError):
            credit_verified_payment(
                self.connection,
                999999,
                "paystack-ref-0003",
                1000,
                "2026-01-01 00:00:00",
            )

        count = self.connection.execute(
            "SELECT COUNT(*) FROM ledger",
        ).fetchone()[0]

        self.assertEqual(count, 0)

    def test_failed_ledger_write_rolls_back_payment_reference(self):
        self.connection.execute("""
            CREATE TRIGGER reject_wallet_ledger_insert
            BEFORE INSERT ON ledger
            BEGIN
                SELECT RAISE(ABORT, 'forced ledger failure');
            END
        """)

        with self.assertRaises(sqlite3.IntegrityError):
            credit_verified_payment(
                self.connection,
                self.user_id,
                "paystack-ref-0004",
                1000,
                "2026-01-01 00:00:00",
            )

        refs = self.connection.execute(
            "SELECT COUNT(*) FROM wallet_payment_refs",
        ).fetchone()[0]

        ledger = self.connection.execute(
            "SELECT COUNT(*) FROM ledger",
        ).fetchone()[0]

        self.assertEqual(refs, 0)
        self.assertEqual(ledger, 0)


    def test_pending_payment_does_not_credit_wallet(self):
        ensure_pending_payment_schema(self.connection)
        create_pending_payment(
            self.connection, self.user_id, "pending-ref-0001",
            100000, "NGN", "2026-01-01 00:00:00",
        )
        balance = self.connection.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM ledger WHERE user_id=?",
            (self.user_id,),
        ).fetchone()[0]
        self.assertEqual(balance, 0)

    def test_pending_payment_rejects_amount_or_currency_mismatch(self):
        ensure_pending_payment_schema(self.connection)
        create_pending_payment(
            self.connection, self.user_id, "pending-ref-0002",
            100000, "NGN", "2026-01-01 00:00:00",
        )
        self.assertFalse(complete_pending_payment(
            self.connection, "pending-ref-0002", 99999, "NGN",
            "2026-01-01 00:01:00",
        ))
        self.assertFalse(complete_pending_payment(
            self.connection, "pending-ref-0002", 100000, "USD",
            "2026-01-01 00:01:00",
        ))
        balance = self.connection.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM ledger WHERE user_id=?",
            (self.user_id,),
        ).fetchone()[0]
        self.assertEqual(balance, 0)

    def test_verified_pending_payment_is_credited_only_once(self):
        ensure_pending_payment_schema(self.connection)
        create_pending_payment(
            self.connection, self.user_id, "pending-ref-0003",
            100000, "NGN", "2026-01-01 00:00:00",
        )
        args = (
            self.connection, "pending-ref-0003", 100000, "NGN",
            "2026-01-01 00:01:00",
        )
        self.assertTrue(complete_pending_payment(*args))
        self.assertFalse(complete_pending_payment(*args))
        balance = self.connection.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM ledger WHERE user_id=?",
            (self.user_id,),
        ).fetchone()[0]
        self.assertEqual(balance, 100000)
        status = self.connection.execute(
            "SELECT status FROM wallet_pending_payments WHERE reference=?",
            ("pending-ref-0003",),
        ).fetchone()[0]
        self.assertEqual(status, "credited")

    def test_failed_pending_payment_ledger_write_rolls_back_everything(self):
        ensure_pending_payment_schema(self.connection)
        create_pending_payment(
            self.connection, self.user_id, "pending-ref-0004",
            100000, "NGN", "2026-01-01 00:00:00",
        )
        self.connection.execute("""
            CREATE TRIGGER reject_pending_ledger_insert
            BEFORE INSERT ON ledger
            BEGIN
                SELECT RAISE(ABORT, 'forced ledger failure');
            END
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            complete_pending_payment(
                self.connection, "pending-ref-0004", 100000, "NGN",
                "2026-01-01 00:01:00",
            )
        ref_count = self.connection.execute(
            "SELECT COUNT(*) FROM wallet_payment_refs WHERE reference=?",
            ("pending-ref-0004",),
        ).fetchone()[0]
        status = self.connection.execute(
            "SELECT status FROM wallet_pending_payments WHERE reference=?",
            ("pending-ref-0004",),
        ).fetchone()[0]
        balance = self.connection.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM ledger WHERE user_id=?",
            (self.user_id,),
        ).fetchone()[0]
        self.assertEqual(ref_count, 0)
        self.assertEqual(status, "pending")
        self.assertEqual(balance, 0)


if __name__ == "__main__":
    unittest.main()
