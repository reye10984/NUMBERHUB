"""NumberHub wallet primitives.

All amounts are integer kobo.
This module does not initiate payments or real provider purchases.
"""

import re
import sqlite3


REFERENCE_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def ensure_wallet_schema(connection):
    """Create the unique-reference registry used to prevent duplicate credits."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS wallet_payment_refs (
            id INTEGER PRIMARY KEY,
            reference TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount_kobo INTEGER NOT NULL CHECK (amount_kobo > 0),
            status TEXT NOT NULL CHECK (status IN ('credited')),
            created_at TEXT NOT NULL
        )
    """)


def credit_verified_payment(
    connection,
    user_id,
    reference,
    amount_kobo,
    created_at,
    note="Verified Paystack deposit",
):
    """Credit a verified payment once, atomically.

    The caller must verify the payment directly with the payment provider
    before calling this function. This function does not verify Paystack.

    Returns True if newly credited, False if the reference was already used.
    Raises ValueError for invalid input. Other database failures are raised
    after rolling back the transaction.
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid user ID")

    if not isinstance(reference, str) or not REFERENCE_RE.fullmatch(reference):
        raise ValueError("Invalid payment reference")

    if (
        isinstance(amount_kobo, bool)
        or not isinstance(amount_kobo, int)
        or amount_kobo <= 0
    ):
        raise ValueError("Amount must be a positive integer number of kobo")

    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("A creation timestamp is required")

    if not isinstance(note, str) or not note.strip():
        raise ValueError("A ledger note is required")

    try:
        connection.execute("BEGIN IMMEDIATE")

        existing = connection.execute(
            "SELECT id FROM wallet_payment_refs WHERE reference = ?",
            (reference,),
        ).fetchone()

        if existing:
            connection.rollback()
            return False

        user = connection.execute(
            "SELECT id FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if user is None:
            raise ValueError("Wallet user does not exist")

        connection.execute(
            """
            INSERT INTO wallet_payment_refs
                (reference, user_id, amount_kobo, status, created_at)
            VALUES (?, ?, ?, 'credited', ?)
            """,
            (reference, user_id, amount_kobo, created_at),
        )

        connection.execute(
            """
            INSERT INTO ledger
                (user_id, amount_cents, kind, note, created_at)
            VALUES (?, ?, 'deposit', ?, ?)
            """,
            (user_id, amount_kobo, note.strip(), created_at),
        )

        connection.commit()
        return True

    except Exception:
        connection.rollback()
        raise


def ensure_pending_payment_schema(connection):
    """Create storage for Paystack deposits awaiting server verification."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS wallet_pending_payments (
            id INTEGER PRIMARY KEY,
            reference TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount_kobo INTEGER NOT NULL CHECK (amount_kobo > 0),
            currency TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('pending', 'credited', 'failed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)


def create_pending_payment(
    connection, user_id, reference, amount_kobo, currency, created_at
):
    """Record a deposit attempt without adding money to the wallet."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid user ID.")
    if not isinstance(reference, str) or not REFERENCE_RE.fullmatch(reference):
        raise ValueError("Invalid payment reference.")
    if isinstance(amount_kobo, bool) or not isinstance(amount_kobo, int) or amount_kobo <= 0:
        raise ValueError("Invalid payment amount.")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Invalid currency.")
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("Invalid creation timestamp.")

    connection.execute("BEGIN IMMEDIATE")
    try:
        user = connection.execute(
            "SELECT id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user is None:
            raise ValueError("User does not exist.")

        connection.execute("""
            INSERT INTO wallet_pending_payments
                (reference, user_id, amount_kobo, currency, status,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """, (reference, user_id, amount_kobo, currency, created_at, created_at))
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


def complete_pending_payment(
    connection, reference, verified_amount_kobo, verified_currency, created_at
):
    """Atomically credit a matching pending payment once, after server verification."""
    if not isinstance(reference, str) or not REFERENCE_RE.fullmatch(reference):
        raise ValueError("Invalid payment reference.")
    if (
        isinstance(verified_amount_kobo, bool)
        or not isinstance(verified_amount_kobo, int)
        or verified_amount_kobo <= 0
    ):
        raise ValueError("Invalid verified amount.")
    if not isinstance(verified_currency, str) or not re.fullmatch(
        r"[A-Z]{3}", verified_currency
    ):
        raise ValueError("Invalid verified currency.")
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("Invalid completion timestamp.")

    connection.execute("BEGIN IMMEDIATE")
    try:
        payment = connection.execute("""
            SELECT user_id, amount_kobo, currency, status
            FROM wallet_pending_payments
            WHERE reference = ?
        """, (reference,)).fetchone()

        if payment is None:
            connection.rollback()
            return False

        # Support SQLite connections that return ordinary tuples.
        user_id, amount_kobo, currency, status = payment

        if status == "credited":
            connection.rollback()
            return False

        if (
            status != "pending"
            or amount_kobo != verified_amount_kobo
            or currency != verified_currency
        ):
            connection.rollback()
            return False

        # The reference registry prevents a second credit for this payment.
        connection.execute("""
            INSERT INTO wallet_payment_refs
                (reference, user_id, amount_kobo, status, created_at)
            VALUES (?, ?, ?, 'credited', ?)
        """, (reference, user_id, verified_amount_kobo, created_at))

        connection.execute("""
            INSERT INTO ledger
                (user_id, amount_cents, kind, note, created_at)
            VALUES (?, ?, 'deposit', ?, ?)
        """, (
            user_id,
            verified_amount_kobo,
            "Verified Paystack deposit",
            created_at,
        ))

        cursor = connection.execute("""
            UPDATE wallet_pending_payments
            SET status = 'credited', updated_at = ?
            WHERE reference = ? AND status = 'pending'
        """, (created_at, reference))

        if cursor.rowcount != 1:
            raise RuntimeError(
                "Pending payment status update affected an unexpected number of rows."
            )

        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
