"""5SIM API client for catalogue, purchase, and order status operations."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


API_BASE = "https://5sim.net/v1"


class FiveSimError(Exception):
    """Provider error with a flag for definitive HTTP rejections."""
    def __init__(self, message, definitive=False):
        super().__init__(message)
        self.definitive = definitive


def api_request(path, authenticated=False):
    """Make a bounded-time GET request to the 5SIM API."""
    if not path.startswith("/") or ".." in path.split("/"):
        raise FiveSimError("Invalid API path.")

    headers = {
        "Accept": "application/json",
        "User-Agent": "NOVA-NUMBER-HUB/1.0",
    }

    if authenticated:
        api_key = os.environ.get("FIVE_SIM_API_KEY", "").strip()
        if not api_key:
            raise FiveSimError("The 5SIM API key is not configured.")
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        API_BASE + path,
        headers=headers,
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            if response.status != 200:
                raise FiveSimError(
                    f"5SIM returned HTTP status {response.status}."
                )
            return json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403) and authenticated:
            raise FiveSimError(
                "5SIM authentication failed. Check the API key in Render."
            ) from None
        if exc.code in (401, 403):
            raise FiveSimError(
                f"5SIM rejected the unauthenticated catalogue request (HTTP {exc.code})."
            ) from None
        if exc.code == 429:
            raise FiveSimError(
                "5SIM rate limit reached. Try again later."
            ) from None
        raise FiveSimError(
            f"5SIM returned HTTP status {exc.code}.", definitive=(400 <= exc.code < 500)
        ) from None

    except (urllib.error.URLError, TimeoutError, OSError):
        raise FiveSimError(
            "Could not connect to 5SIM. Please try again later."
        ) from None

    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FiveSimError(
            "5SIM returned an unreadable response."
        ) from None


def configured():
    """Return whether an API key exists without exposing it."""
    return bool(os.environ.get("FIVE_SIM_API_KEY", "").strip())


def get_profile():
    """Retrieve the authenticated account profile."""
    return api_request("/user/profile", authenticated=True)


def get_countries():
    """Retrieve the public country catalogue."""
    return api_request("/guest/countries")


def get_products(country, operator="any"):
    """Retrieve products for a country and operator."""
    country = urllib.parse.quote(str(country), safe="")
    operator = urllib.parse.quote(str(operator), safe="")
    return api_request(
        f"/guest/products/{country}/{operator}"
    )


def buy_activation(country, operator, product):
    """Purchase one activation number. Calling this function spends 5SIM balance."""
    country = urllib.parse.quote(str(country), safe="")
    operator = urllib.parse.quote(str(operator), safe="")
    product = urllib.parse.quote(str(product), safe="")

    if not country or not operator or not product:
        raise FiveSimError("Country, operator, and product are required.")

    return api_request(
        f"/user/buy/activation/{country}/{operator}/{product}",
        authenticated=True,
    )


def check_order(order_id):
    """Retrieve the current status and SMS messages for an activation order."""
    try:
        order_id = int(order_id)
        if order_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise FiveSimError("Invalid 5SIM order ID.") from None

    return api_request(
        f"/user/check/{order_id}",
        authenticated=True,
    )


def cancel_order(order_id):
    """Cancel an activation order. This changes the real 5SIM order."""
    try:
        order_id = int(order_id)
        if order_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise FiveSimError("Invalid 5SIM order ID.") from None

    return api_request(
        f"/user/cancel/{order_id}",
        authenticated=True,
    )
