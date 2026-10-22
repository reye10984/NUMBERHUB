"""Server-side Paystack API helpers. Never trust browser payment claims."""
import os
import re
from urllib.parse import urlparse

import requests

API_BASE = "https://api.paystack.co"
REFERENCE_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
TIMEOUT_SECONDS = 15


class PaystackError(Exception):
    """Raised when Paystack cannot safely initialize or verify a transaction."""


def _secret_key():
    key = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
    if not key:
        raise PaystackError("Paystack is not configured.")
    return key


def _api_request(method, path, *, payload=None):
    try:
        response = requests.request(
            method,
            API_BASE + path,
            headers={
                "Authorization": "Bearer " + _secret_key(),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        result = response.json()
    except PaystackError:
        raise
    except (requests.RequestException, ValueError) as exc:
        raise PaystackError("Paystack could not be reached or returned an invalid response.") from exc

    if not isinstance(result, dict) or result.get("status") is not True:
        raise PaystackError("Paystack did not confirm the request.")
    data = result.get("data")
    if not isinstance(data, dict):
        raise PaystackError("Paystack returned an unexpected response.")
    return data


def initialize_transaction(email, amount_kobo, reference, callback_url):
    """Initialize a transaction; amount is an integer number of kobo."""
    if not isinstance(email, str) or not email.strip() or len(email) > 254:
        raise ValueError("A valid customer email is required.")
    if isinstance(amount_kobo, bool) or not isinstance(amount_kobo, int) or amount_kobo <= 0:
        raise ValueError("Amount must be a positive integer number of kobo.")
    if not isinstance(reference, str) or not REFERENCE_RE.fullmatch(reference):
        raise ValueError("Invalid payment reference.")
    if not isinstance(callback_url, str):
        raise ValueError("A secure HTTPS callback URL is required.")

    try:
        callback = urlparse(callback_url)
        callback_port = callback.port
    except ValueError as exc:
        raise ValueError("Invalid callback URL.") from exc

    if (
        callback.scheme != "https"
        or not callback.hostname
        or callback.username is not None
        or callback.password is not None
        or callback_port not in (None, 443)
        or callback.query
        or callback.fragment
        or any(char.isspace() for char in callback_url)
    ):
        raise ValueError("A valid secure HTTPS callback URL is required.")

    data = _api_request(
        "POST",
        "/transaction/initialize",
        payload={
            "email": email.strip().lower(),
            "amount": amount_kobo,
            "currency": os.environ.get("PAYSTACK_CURRENCY", "NGN").strip().upper(),
            "reference": reference,
            "callback_url": callback_url,
        },
    )
    authorization_url = data.get("authorization_url")
    returned_reference = data.get("reference")
    try:
        parsed_url = (
            urlparse(authorization_url)
            if isinstance(authorization_url, str)
            else None
        )
        checkout_port = parsed_url.port if parsed_url is not None else None
    except ValueError as exc:
        raise PaystackError(
            "Paystack returned invalid transaction details."
        ) from exc

    if (
        parsed_url is None
        or parsed_url.scheme != "https"
        or parsed_url.hostname != "checkout.paystack.com"
        or parsed_url.username is not None
        or parsed_url.password is not None
        or checkout_port not in (None, 443)
        or not isinstance(returned_reference, str)
        or returned_reference != reference
    ):
        raise PaystackError("Paystack returned invalid transaction details.")
    return {"authorization_url": authorization_url, "reference": returned_reference}


def verify_transaction(reference, *, expected_amount_kobo, expected_email):
    """Verify directly with Paystack and return data only if all expected fields match."""
    if not isinstance(reference, str) or not REFERENCE_RE.fullmatch(reference):
        raise ValueError("Invalid payment reference.")
    if (
        isinstance(expected_amount_kobo, bool)
        or not isinstance(expected_amount_kobo, int)
        or expected_amount_kobo <= 0
    ):
        raise ValueError("Expected amount must be positive integer kobo.")
    if not isinstance(expected_email, str) or not expected_email.strip():
        raise ValueError("Expected customer email is required.")

    data = _api_request("GET", "/transaction/verify/" + reference)
    customer = data.get("customer")
    currency = os.environ.get("PAYSTACK_CURRENCY", "NGN").strip().upper()

    if data.get("reference") != reference:
        raise PaystackError("Payment reference does not match.")
    if data.get("status") != "success":
        raise PaystackError("Paystack has not marked this payment successful.")
    if data.get("amount") != expected_amount_kobo:
        raise PaystackError("Verified payment amount does not match.")
    if data.get("currency") != currency:
        raise PaystackError("Verified payment currency does not match.")
    if not isinstance(customer, dict) or not isinstance(customer.get("email"), str):
        raise PaystackError("Paystack did not return a valid customer.")
    if customer["email"].strip().lower() != expected_email.strip().lower():
        raise PaystackError("Verified customer email does not match.")

    return data
