"""NumberHub: real 5SIM catalogue/orders with Telegram account linking and SMS forwarding."""
import hmac, json, os, re, secrets, sqlite3, time, urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timezone
from functools import wraps
from flask import (Flask, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from db_backend import connect_database
from wallet import (
    ensure_wallet_schema, ensure_pending_payment_schema,
    create_pending_payment, complete_pending_payment,
)
from paystack import (
    initialize_transaction, verify_transaction, PaystackError,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  amount_cents INTEGER NOT NULL, kind TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS numbers(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  number TEXT UNIQUE NOT NULL, is_demo INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, number_id INTEGER NOT NULL REFERENCES numbers(id),
  sender TEXT NOT NULL, body TEXT NOT NULL, is_demo INTEGER NOT NULL, provider_id TEXT, received_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_provider ON messages(provider_id) WHERE provider_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS five_sim_orders(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  provider_order_id TEXT NOT NULL UNIQUE,
  phone TEXT NOT NULL,
  country TEXT NOT NULL,
  product TEXT NOT NULL,
  status TEXT NOT NULL,
  sms_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  customer_charge_kobo INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS five_sim_purchase_attempts(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  token TEXT NOT NULL UNIQUE, country TEXT NOT NULL, product TEXT NOT NULL,
  charge_kobo INTEGER NOT NULL, status TEXT NOT NULL, provider_order_id TEXT,
  error TEXT, refund_applied INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""
PROVIDER_VARS = ["FIVE_SIM_API_KEY"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def customer_price_kobo(provider_price):
    """Convert 5SIM USD price to NGN kobo with a 30% markup and safe rounding."""
    try:
        price = Decimal(str(provider_price))
        rate = Decimal(os.environ.get("USD_TO_NGN_RATE", "1500"))
        markup = Decimal(os.environ.get("FIVE_SIM_MARKUP_PERCENT", "30"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid price configuration") from None
    if not price.is_finite() or price <= 0 or not rate.is_finite() or rate <= 0 or not markup.is_finite() or markup < 0:
        raise ValueError("Invalid price")
    return int((price * rate * (Decimal(1) + markup / Decimal(100)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def provider():
    """Report 5SIM readiness without exposing credentials or implying an order exists."""
    missing = [k for k in PROVIDER_VARS if not os.environ.get(k, "").strip()]
    if not os.environ.get("FIVE_SIM_API_KEY", "").strip():
        return {"mode": "unconfigured", "missing": missing,
                "message": "5SIM is not configured. Set FIVE_SIM_API_KEY in the hosting environment."}
    return {"mode": "5sim", "missing": missing,
            "message": "5SIM is configured. Number availability and SMS delivery must be confirmed per order."}


def db():
    if "db" not in g:
        g.db = connect_database(
            current_app.config.get("DATABASE_URL"),
            current_app.config["DATABASE"],
        )
    return g.db


def balance(d, uid):
    return d.execute("SELECT COALESCE(SUM(amount_cents),0) FROM ledger WHERE user_id=?", (uid,)).fetchone()[0]


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("DATABASE_PATH", os.path.join(app.instance_path, "numberhub.db")),
        DATABASE_URL=os.environ.get("DATABASE_URL", "").strip(),
        NUMBER_PRICE_CENTS=int(os.environ.get("NUMBER_PRICE_CENTS", "100")),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    if config:
        app.config.update(config)
        # Test configurations specifying DATABASE explicitly continue to use SQLite
        # unless they also explicitly provide DATABASE_URL.
        if "DATABASE" in config and "DATABASE_URL" not in config:
            app.config["DATABASE_URL"] = None

    database_url = app.config.get("DATABASE_URL")
    if not database_url:
        os.makedirs(
            os.path.dirname(os.path.abspath(app.config["DATABASE"])),
            exist_ok=True,
        )

    boot = connect_database(database_url, app.config["DATABASE"])
    try:
        boot.execute("PRAGMA foreign_keys=ON")
        boot.executescript(SCHEMA)
        if database_url:
            columns = {
                row[0]
                for row in boot.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = ?",
                    ("five_sim_orders",),
                )
            }
        else:
            columns = {
                row[1]
                for row in boot.execute(
                    "PRAGMA table_info(five_sim_orders)"
                )
            }
        if "customer_charge_kobo" not in columns:
            boot.execute("ALTER TABLE five_sim_orders ADD COLUMN customer_charge_kobo INTEGER NOT NULL DEFAULT 0")
        ensure_wallet_schema(boot)
        ensure_pending_payment_schema(boot)
        boot.commit()
    finally:
        boot.close()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    app.jinja_env.globals.update(csrf_token=csrf_token, provider=provider)
    app.jinja_env.filters["money"] = lambda c: "%.2f" % (c / 100)

    @app.teardown_appcontext
    def close(_):
        d = g.pop("db", None)
        if d is not None:
            d.close()

    @app.before_request
    def load_user():
        g.user = None
        if session.get("uid"):
            g.user = db().execute("SELECT id,email FROM users WHERE id=?", (session["uid"],)).fetchone()
        if request.method == "POST" and request.endpoint != "webhook_sms":
            if not hmac.compare_digest(session.get("csrf", "x"), request.form.get("csrf_token", "")):
                abort(400, "Invalid or missing form token. Reload the page and try again.")

    def login_required(f):
        @wraps(f)
        def w(*a, **k):
            if not g.user:
                return redirect(url_for("login"))
            return f(*a, **k)
        return w

    @app.get("/health")
    def health():
        database_backend = (
            "postgresql"
            if app.config.get("DATABASE_URL")
            else "sqlite"
        )
        db().execute("SELECT 1")
        return jsonify(
            status="ok",
            mode=provider()["mode"],
            database=database_backend,
            database_connected=True,
        )

    @app.get("/")
    def index():
        return redirect(url_for("dashboard" if g.user else "login"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            pw = request.form.get("password", "")
            if not EMAIL_RE.match(email):
                flash("Enter a valid email address.", "error")
            elif len(pw) < 8:
                flash("Password must be at least 8 characters.", "error")
            else:
                try:
                    db().execute("INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?)",
                                 (email, generate_password_hash(pw), now()))
                except sqlite3.IntegrityError:
                    flash("That email is already registered.", "error")
                else:
                    flash("Account created. Please log in.", "ok")
                    return redirect(url_for("login"))
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
                session.clear()
                session["uid"] = u["id"]
                return redirect(url_for("dashboard"))
            flash("Incorrect email or password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        d = db()
        led = d.execute("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 20", (g.user["id"],)).fetchall()
        n = d.execute("SELECT COUNT(*) FROM numbers WHERE user_id=?", (g.user["id"],)).fetchone()[0]
        return render_template("dashboard.html", bal=balance(d, g.user["id"]), ledger=led, count=n,
                               price=app.config["NUMBER_PRICE_CENTS"])

    @app.get("/wallet")
    @login_required
    def wallet_page():
        d = db()
        uid = g.user["id"]
        rows = d.execute(
            "SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 50",
            (uid,),
        ).fetchall()
        pending = d.execute(
            """SELECT reference, amount_kobo, status, created_at
               FROM wallet_pending_payments
               WHERE user_id=? ORDER BY id DESC LIMIT 10""",
            (uid,),
        ).fetchall()
        return render_template(
            "wallet.html", bal=balance(d, uid), ledger=rows, pending=pending
        )

    @app.post("/wallet/deposit")
    @login_required
    def wallet_deposit():
        from decimal import Decimal, InvalidOperation

        raw = request.form.get("amount", "").strip()
        try:
            value = Decimal(raw)
            if not value.is_finite() or value <= 0 or value * 100 != (value * 100).to_integral_value():
                raise ValueError
            amount_kobo = int(value * 100)
        except (InvalidOperation, ValueError):
            flash("Enter a valid deposit amount with at most two decimal places.", "error")
            return redirect(url_for("wallet_page"))

        minimum = int(os.environ.get("WALLET_MIN_DEPOSIT_KOBO", "10000"))
        maximum = int(os.environ.get("WALLET_MAX_DEPOSIT_KOBO", "50000000"))
        if minimum <= 0 or maximum < minimum or not minimum <= amount_kobo <= maximum:
            flash(
                f"Deposit must be between ₦{minimum / 100:.2f} and ₦{maximum / 100:.2f}.",
                "error",
            )
            return redirect(url_for("wallet_page"))

        from urllib.parse import urlparse

        base_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        try:
            parsed_base_url = urlparse(base_url)
            base_port = parsed_base_url.port
        except ValueError:
            parsed_base_url = None
            base_port = None

        if (
            parsed_base_url is None
            or parsed_base_url.scheme != "https"
            or not parsed_base_url.hostname
            or parsed_base_url.username is not None
            or parsed_base_url.password is not None
            or base_port not in (None, 443)
            or parsed_base_url.path not in ("", "/")
            or parsed_base_url.query
            or parsed_base_url.fragment
            or any(char.isspace() for char in base_url)
        ):
            flash("Secure payment callback is not configured. Please try again later.", "error")
            return redirect(url_for("wallet_page"))

        reference = "nh_" + secrets.token_urlsafe(24).replace("-", "x").replace("_", "y")
        timestamp = now()
        currency = os.environ.get("PAYSTACK_CURRENCY", "NGN").strip().upper()

        try:
            create_pending_payment(
                db(), int(g.user["id"]), reference, amount_kobo, currency, timestamp
            )
            result = initialize_transaction(
                g.user["email"], amount_kobo, reference,
                base_url + url_for("paystack_callback"),
            )
            return redirect(result["authorization_url"])
        except (PaystackError, ValueError, sqlite3.Error):
            current_app.logger.exception("Paystack deposit initialization failed")
            flash("Could not start the payment. Your wallet has not been credited.", "error")
            return redirect(url_for("wallet_page"))

    @app.get("/payments/paystack/callback")
    def paystack_callback():
        reference = request.args.get("reference", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", reference):
            flash("Invalid payment reference.", "error")
            return redirect(url_for("login"))

        payment = db().execute(
            """SELECT p.user_id, p.amount_kobo, p.currency, p.status, u.email
               FROM wallet_pending_payments p
               JOIN users u ON u.id=p.user_id
               WHERE p.reference=?""",
            (reference,),
        ).fetchone()

        if payment is None:
            flash("Payment reference was not found.", "error")
            return redirect(url_for("login"))

        if payment["status"] == "credited":
            flash("This payment has already been credited.", "ok")
            if session.get("uid") == payment["user_id"]:
                return redirect(url_for("wallet_page"))
            return redirect(url_for("login"))

        try:
            verified = verify_transaction(
                reference,
                expected_amount_kobo=payment["amount_kobo"],
                expected_email=payment["email"],
            )
            if verified.get("currency") != payment["currency"]:
                raise PaystackError("Payment currency does not match the pending deposit.")

            credited = complete_pending_payment(
                db(), reference, payment["amount_kobo"],
                payment["currency"], now(),
            )
            if credited:
                flash("Payment verified and wallet credited successfully.", "ok")
            else:
                flash("Payment was already processed or could not be matched.", "error")
        except (PaystackError, ValueError, sqlite3.Error):
            current_app.logger.exception("Paystack callback verification failed")
            flash("Payment could not be verified. Your wallet has not been credited.", "error")

        if session.get("uid") == payment["user_id"]:
            return redirect(url_for("wallet_page"))
        return redirect(url_for("login"))

    @app.get("/request")
    @login_required
    def request_page():
        return redirect(url_for("five_sim_catalogue"))

    @app.post("/request")
    @login_required
    def request_number():
        return redirect(url_for("five_sim_catalogue"))

    @app.get("/history")
    @login_required
    def history():
        rows = db().execute("SELECT * FROM numbers WHERE user_id=? ORDER BY id DESC", (g.user["id"],)).fetchall()
        return render_template("history.html", rows=rows)

    def inbox_rows(uid):
        return db().execute(
            "SELECT m.id,m.sender,m.body,m.is_demo,m.received_at,n.number FROM messages m "
            "JOIN numbers n ON n.id=m.number_id WHERE n.user_id=? AND m.is_demo=0 ORDER BY m.id DESC LIMIT 100", (uid,)).fetchall()

    @app.get("/inbox")
    @login_required
    def inbox():
        return render_template("inbox.html", msgs=inbox_rows(g.user["id"]))

    @app.get("/api/messages")
    @login_required
    def api_messages():
        return jsonify([dict(r) for r in inbox_rows(g.user["id"])])

    @app.get("/provider")
    @login_required
    def provider_page():
        p = provider()
        status = {k: bool(os.environ.get(k, "").strip()) for k in PROVIDER_VARS}  # presence only, never values
        return render_template("provider.html", p=p, status=status)

    @app.get("/catalogue")
    @login_required
    def five_sim_catalogue():
        import five_sim

        countries = {}
        services = {}
        error = ""
        selected_country = request.args.get("country", "nigeria").strip().lower()
        query = request.args.get("q", "").strip().lower()

        try:
            countries = five_sim.get_countries()
            if not isinstance(countries, dict):
                countries = {}
                error = "5SIM returned an unexpected country catalogue."

            if not error and selected_country not in countries:
                selected_country = "nigeria" if "nigeria" in countries else (
                    next(iter(countries), "")
                )

            if not error and selected_country:
                result = five_sim.get_products(selected_country, "any")
                if isinstance(result, dict):
                    services = {
                        name: details for name, details in result.items()
                        if isinstance(details, dict)
                        and (not query or query in str(name).lower())
                    }
                else:
                    error = "5SIM returned an unexpected services catalogue."

        except five_sim.FiveSimError as exc:
            error = str(exc)

        customer_prices = {}
        for name, details in services.items():
            try:
                customer_prices[name] = customer_price_kobo(details.get("Price"))
            except (ValueError, InvalidOperation):
                customer_prices[name] = None
        return render_template(
            "catalogue.html", countries=countries, services=services,
            selected_country=selected_country, query=query, error=error,
            customer_prices=customer_prices, purchase_token=secrets.token_urlsafe(24),
        )


    @app.post("/catalogue/buy")
    @login_required
    def five_sim_buy():
        """Charge the NumberHub wallet before calling 5SIM; never trust client prices."""
        import five_sim
        country = request.form.get("country", "").strip().lower()
        product = request.form.get("product", "").strip()
        token = request.form.get("purchase_token", "").strip()
        if not country or not product or not token or len(token) > 200:
            flash("Purchase form expired or invalid. Reload the catalogue.", "error")
            return redirect(url_for("five_sim_catalogue"))
        d = db(); uid = g.user["id"]
        prior = d.execute("SELECT status FROM five_sim_purchase_attempts WHERE token=? AND user_id=?", (token, uid)).fetchone()
        if prior:
            flash("This purchase submission was already processed. Check your orders and wallet history.", "error")
            return redirect(url_for("five_sim_orders"))
        try:
            products = five_sim.get_products(country, "any")
            details = products.get(product) if isinstance(products, dict) else None
            if not isinstance(details, dict) or str(details.get("Category", "")).lower() != "activation":
                flash("That service is not available for the selected country.", "error")
                return redirect(url_for("five_sim_catalogue") + "?country=" + country)
            try:
                qty = int(details.get("Qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                flash("5SIM reports no available numbers for that service.", "error")
                return redirect(url_for("five_sim_catalogue") + "?country=" + country)
            charge = customer_price_kobo(details.get("Price"))
            profile = five_sim.get_profile()
            provider_balance = Decimal(str(profile.get("balance", "0")))
            provider_price = Decimal(str(details.get("Price")))
            if not provider_balance.is_finite() or provider_balance < provider_price:
                flash("The 5SIM provider account does not have enough balance to fulfil this order.", "error")
                return redirect(url_for("five_sim_catalogue") + "?country=" + country)
        except (five_sim.FiveSimError, ValueError, InvalidOperation, TypeError, AttributeError) as exc:
            flash("Could not validate this purchase with 5SIM. No wallet funds were taken.", "error")
            return redirect(url_for("five_sim_catalogue") + "?country=" + country)

        # Reserve the customer's funds and idempotency token before the external call.
        try:
            d.execute("BEGIN IMMEDIATE")
            existing = d.execute("SELECT id FROM five_sim_purchase_attempts WHERE token=?", (token,)).fetchone()
            if existing:
                d.execute("ROLLBACK")
                flash("This purchase submission was already processed.", "error")
                return redirect(url_for("five_sim_orders"))
            current_balance = balance(d, uid)
            if current_balance < charge:
                d.execute("ROLLBACK")
                flash(f"Insufficient wallet balance. This order costs ₦{charge / 100:.2f}.", "error")
                return redirect(url_for("wallet_page"))
            stamp = now()
            d.execute("INSERT INTO five_sim_purchase_attempts(user_id,token,country,product,charge_kobo,status,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)", (uid, token, country, product, charge, stamp, stamp))
            d.execute("INSERT INTO ledger(user_id,amount_cents,kind,note,created_at) VALUES(?,?,?,?,?)", (uid, -charge, "number_purchase", f"5SIM {product} ({country}) purchase", stamp))
            d.execute("COMMIT")
        except sqlite3.IntegrityError:
            if d.in_transaction: d.execute("ROLLBACK")
            flash("This purchase submission was already processed.", "error")
            return redirect(url_for("five_sim_orders"))
        except Exception:
            if d.in_transaction: d.execute("ROLLBACK")
            current_app.logger.exception("Could not reserve wallet funds for purchase")
            flash("Could not reserve wallet funds. No provider order was submitted.", "error")
            return redirect(url_for("wallet_page"))

        try:
            result = five_sim.buy_activation(country, "any", product)
            if not isinstance(result, dict) or result.get("id") is None or not result.get("phone"):
                d.execute("UPDATE five_sim_purchase_attempts SET status='uncertain',error=?,updated_at=? WHERE token=?", ("Unclear provider response; manual reconciliation required", now(), token))
                flash("5SIM response is uncertain. Your order is flagged for reconciliation; do not submit it again.", "error")
                return redirect(url_for("five_sim_orders"))
            order_id = str(result["id"]); phone = str(result["phone"])
            d.execute("BEGIN IMMEDIATE")
            d.execute("INSERT INTO five_sim_orders(user_id,provider_order_id,phone,country,product,status,sms_json,created_at,updated_at,customer_charge_kobo) VALUES(?,?,?,?,?,?,?,?,?,?)", (uid, order_id, phone, country, product, str(result.get("status", "PENDING")), json.dumps(result.get("sms", []), ensure_ascii=False), now(), now(), charge))
            d.execute("UPDATE five_sim_purchase_attempts SET status='succeeded',provider_order_id=?,updated_at=? WHERE token=?", (order_id, now(), token))
            d.execute("COMMIT")
            flash(f"Number purchased successfully. Wallet charged ₦{charge / 100:.2f}.", "ok")
            return redirect(url_for("five_sim_orders"))
        except five_sim.FiveSimError as exc:
            # Only explicitly definitive provider rejections are refundable.
            if getattr(exc, "definitive", False):
                d.execute("BEGIN IMMEDIATE")
                attempt = d.execute("SELECT refund_applied FROM five_sim_purchase_attempts WHERE token=?", (token,)).fetchone()
                if attempt and not attempt["refund_applied"]:
                    stamp = now()
                    d.execute("INSERT INTO ledger(user_id,amount_cents,kind,note,created_at) VALUES(?,?,?,?,?)", (uid, charge, "purchase_refund", f"Refund for rejected 5SIM {product} order", stamp))
                    d.execute("UPDATE five_sim_purchase_attempts SET status='failed',error=?,refund_applied=1,updated_at=? WHERE token=?", (str(exc)[:300], stamp, token))
                d.execute("COMMIT")
                flash("5SIM rejected the order. Your wallet has been refunded.", "error")
            else:
                d.execute("UPDATE five_sim_purchase_attempts SET status='uncertain',error=?,updated_at=? WHERE token=?", (str(exc)[:300], now(), token))
                flash("5SIM could not confirm the result. The purchase is marked uncertain; funds are held to prevent a duplicate order.", "error")
            return redirect(url_for("five_sim_orders"))
        except Exception:
            current_app.logger.exception("5SIM purchase outcome needs reconciliation")
            if d.in_transaction: d.execute("ROLLBACK")
            d.execute("UPDATE five_sim_purchase_attempts SET status='uncertain',error=?,updated_at=? WHERE token=?", ("Unexpected response or local persistence failure; reconcile with 5SIM before refunding", now(), token))
            flash("The purchase outcome is uncertain. Do not retry; contact support to reconcile with 5SIM.", "error")
            return redirect(url_for("five_sim_orders"))

    @app.get("/5sim/orders")
    @login_required
    def five_sim_orders():
        rows = db().execute(
            "SELECT * FROM five_sim_orders WHERE user_id=? ORDER BY id DESC",
            (g.user["id"],),
        ).fetchall()
        return render_template("five_sim_orders.html", rows=rows)

    @app.post("/5sim/orders/<int:order_row_id>/refresh")
    @login_required
    def five_sim_refresh_order(order_row_id):
        """Refresh only an order belonging to the currently logged-in user."""
        import five_sim

        d = db()
        row = d.execute(
            "SELECT * FROM five_sim_orders WHERE id=? AND user_id=?",
            (order_row_id, g.user["id"]),
        ).fetchone()

        if not row:
            abort(404)

        try:
            result = five_sim.check_order(row["provider_order_id"])
            if not isinstance(result, dict):
                flash("5SIM returned an unexpected order status.", "error")
                return redirect(url_for("five_sim_orders"))

            status = str(result.get("status", row["status"]))
            phone = str(result.get("phone") or row["phone"])
            sms_items = result.get("sms", [])
            if not isinstance(sms_items, list):
                sms_items = []
            try:
                old_items = json.loads(row["sms_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                old_items = []
            if not isinstance(old_items, list):
                old_items = []
            old_keys = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in old_items}
            new_items = [item for item in sms_items
                         if json.dumps(item, sort_keys=True, ensure_ascii=False) not in old_keys]
            sms_json = json.dumps(sms_items, ensure_ascii=False)

            d.execute(
                """UPDATE five_sim_orders
                   SET status=?, phone=?, sms_json=?, updated_at=?
                   WHERE id=? AND user_id=?""",
                (status, phone, sms_json, now(), order_row_id, g.user["id"]),
            )
            flash("5SIM order status refreshed. Any SMS returned by 5SIM is available in your order.", "ok")

        except five_sim.FiveSimError as exc:
            flash("Could not refresh 5SIM order: " + str(exc), "error")

        return redirect(url_for("five_sim_orders"))


    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
