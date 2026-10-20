"""NumberHub - small Flask app: accounts, wallet ledger, number requests, SMS inbox.
Modes: demo (default, no credentials, all data labelled DEMO), live (Africa's Talking), unconfigured."""
import hmac, json, os, re, secrets, sqlite3, time, urllib.request
from datetime import datetime, timezone
from functools import wraps
from flask import (Flask, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

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
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_links(
  telegram_user_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
  linked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_link_codes(
  code TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  expires_at INTEGER NOT NULL
);

"""
LIVE_VARS = ["AT_USERNAME", "AT_API_KEY", "AT_NUMBER", "WEBHOOK_SECRET"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def provider():
    """Read provider config from the environment on every call. Never returns secret values."""
    env = {k: os.environ.get(k, "").strip() for k in LIVE_VARS}
    missing = [k for k, v in env.items() if not v]
    if os.environ.get("DEMO_MODE", "1") != "0":
        return {"mode": "demo", "missing": missing, "number": "",
                "message": "DEMO mode: numbers and messages are simulated and labelled DEMO."}
    if missing:
        return {"mode": "unconfigured", "missing": missing, "number": "",
                "message": "Provider not configured. Missing environment variables: "
                           + ", ".join(missing) + ". Set them, or set DEMO_MODE=1 to try the app with DEMO data."}
    return {"mode": "live", "missing": [], "number": env["AT_NUMBER"],
            "message": "Live mode: Africa's Talking credentials found."}


def db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], isolation_level=None)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


def balance(d, uid):
    return d.execute("SELECT COALESCE(SUM(amount_cents),0) FROM ledger WHERE user_id=?", (uid,)).fetchone()[0]


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("DATABASE_PATH", os.path.join(app.instance_path, "numberhub.db")),
        NUMBER_PRICE_CENTS=int(os.environ.get("NUMBER_PRICE_CENTS", "100")),
        DEMO_CREDIT_CENTS=500,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    if config:
        app.config.update(config)
    os.makedirs(os.path.dirname(os.path.abspath(app.config["DATABASE"])), exist_ok=True)
    boot = sqlite3.connect(app.config["DATABASE"])
    boot.executescript(SCHEMA)
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
        if request.method == "POST" and request.endpoint not in ("webhook_sms", "telegram_webhook"):
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
        db().execute("SELECT 1")
        return jsonify(status="ok", mode=provider()["mode"])

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

    @app.post("/wallet/demo-credit")
    @login_required
    def demo_credit():
        if provider()["mode"] != "demo":
            flash("Demo credit is only available in DEMO mode. Real top-ups are not part of this version.", "error")
        else:
            db().execute("INSERT INTO ledger(user_id,amount_cents,kind,note,created_at) VALUES(?,?,?,?,?)",
                         (g.user["id"], app.config["DEMO_CREDIT_CENTS"], "demo_credit", "DEMO credit (not real money)", now()))
            flash("DEMO credit added.", "ok")
        return redirect(url_for("dashboard"))

    @app.get("/request")
    @login_required
    def request_page():
        return render_template("request.html", price=app.config["NUMBER_PRICE_CENTS"],
                               bal=balance(db(), g.user["id"]))

    @app.post("/request")
    @login_required
    def request_number():
        p, price, d = provider(), app.config["NUMBER_PRICE_CENTS"], db()
        back = redirect(url_for("request_page"))
        if p["mode"] == "unconfigured":
            flash(p["message"], "error")
            return back
        d.execute("BEGIN IMMEDIATE")
        try:
            err = None
            if balance(d, g.user["id"]) < price:
                err = "Insufficient wallet balance for this request."
            elif p["mode"] == "demo":
                number, is_demo = None, 1
                while number is None or d.execute("SELECT 1 FROM numbers WHERE number=?", (number,)).fetchone():
                    number = "DEMO-%06d" % secrets.randbelow(10**6)
            else:
                number, is_demo = p["number"], 0
                if d.execute("SELECT 1 FROM numbers WHERE number=?", (number,)).fetchone():
                    err = ("The account's Africa's Talking number is already assigned. Automated provisioning of "
                           "new numbers is not supported by this app; add numbers in your Africa's Talking dashboard.")
            if err:
                d.execute("ROLLBACK")
                flash(err, "error")
                return back
            d.execute("INSERT INTO numbers(user_id,number,is_demo,status,created_at) VALUES(?,?,?,?,?)",
                      (g.user["id"], number, is_demo, "active", now()))
            if price > 0:
                d.execute("INSERT INTO ledger(user_id,amount_cents,kind,note,created_at) VALUES(?,?,?,?,?)",
                          (g.user["id"], -price, "number_request", "Number request " + number, now()))
            d.execute("COMMIT")
        except Exception:
            d.execute("ROLLBACK")
            raise
        flash(("DEMO number %s created (simulated, not a real number)." if is_demo else "Number %s assigned.") % number, "ok")
        return redirect(url_for("history"))

    @app.get("/history")
    @login_required
    def history():
        rows = db().execute("SELECT * FROM numbers WHERE user_id=? ORDER BY id DESC", (g.user["id"],)).fetchall()
        return render_template("history.html", rows=rows)

    def inbox_rows(uid):
        return db().execute(
            "SELECT m.id,m.sender,m.body,m.is_demo,m.received_at,n.number FROM messages m "
            "JOIN numbers n ON n.id=m.number_id WHERE n.user_id=? ORDER BY m.id DESC LIMIT 100", (uid,)).fetchall()

    @app.get("/inbox")
    @login_required
    def inbox():
        nums = db().execute("SELECT * FROM numbers WHERE user_id=? AND is_demo=1", (g.user["id"],)).fetchall()
        return render_template("inbox.html", msgs=inbox_rows(g.user["id"]), demo_numbers=nums)

    @app.get("/api/messages")
    @login_required
    def api_messages():
        return jsonify([dict(r) for r in inbox_rows(g.user["id"])])

    @app.post("/demo/simulate")
    @login_required
    def simulate():
        if provider()["mode"] != "demo":
            flash("Simulated messages are only available in DEMO mode.", "error")
            return redirect(url_for("inbox"))
        n = db().execute("SELECT id FROM numbers WHERE id=? AND user_id=? AND is_demo=1",
                         (request.form.get("number_id", type=int), g.user["id"])).fetchone()
        if not n:
            flash("Choose one of your DEMO numbers.", "error")
        else:
            db().execute("INSERT INTO messages(number_id,sender,body,is_demo,received_at) VALUES(?,?,?,?,?)",
                         (n["id"], "DEMO-SENDER", "DEMO message: simulated text, not a real SMS.", 1, now()))
            flash("DEMO message added.", "ok")
        return redirect(url_for("inbox"))

    @app.get("/provider")
    @login_required
    def provider_page():
        p = provider()
        status = {k: bool(os.environ.get(k, "").strip()) for k in LIVE_VARS}  # set/unset only, never values
        return render_template("provider.html", p=p, status=status,
                               webhook=url_for("webhook_sms", _external=True) + "?secret=YOUR_WEBHOOK_SECRET")

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

        return render_template(
            "catalogue.html",
            countries=countries,
            services=services,
            selected_country=selected_country,
            query=query,
            error=error,
        )


    @app.post("/catalogue/buy")
    @login_required
    def five_sim_buy():
        """Buy a 5SIM number only after an explicit, confirmed form submission."""
        import five_sim

        if os.environ.get("DEMO_MODE", "1") != "0":
            flash("Demo mode is enabled; real 5SIM purchases are disabled.", "error")
            return redirect(url_for("five_sim_catalogue"))

        country = request.form.get("country", "").strip().lower()
        product = request.form.get("product", "").strip()

        if not country or not product:
            flash("Choose a valid country and service.", "error")
            return redirect(url_for("five_sim_catalogue"))

        try:
            profile = five_sim.get_profile()
            try:
                available_balance = float(profile.get("balance", 0) or 0)
            except (TypeError, ValueError):
                available_balance = 0

            if available_balance <= 0:
                flash(
                    "Your 5SIM balance is zero or unavailable. Add funds to your 5SIM account before ordering.",
                    "error",
                )
                return redirect(url_for("five_sim_catalogue"))

            products = five_sim.get_products(country, "any")
            details = products.get(product) if isinstance(products, dict) else None

            if not isinstance(details, dict):
                flash("That service is not listed for the selected country. Reload the catalogue.", "error")
                return redirect(url_for("five_sim_catalogue") + "?country=" + country)

            try:
                quantity = int(details.get("Qty", 0) or 0)
            except (TypeError, ValueError):
                quantity = 0

            if quantity <= 0:
                flash("5SIM reports no available numbers for that service.", "error")
                return redirect(url_for("five_sim_catalogue") + "?country=" + country)

            # This is the only route that initiates a real purchase.
            result = five_sim.buy_activation(country, "any", product)

            if not isinstance(result, dict) or result.get("id") is None or not result.get("phone"):
                flash(
                    "5SIM returned an unexpected order response. Check your 5SIM account before retrying.",
                    "error",
                )
                return redirect(url_for("five_sim_orders"))

            provider_order_id = str(result["id"])
            phone = str(result["phone"])
            status = str(result.get("status", "PENDING"))
            sms_json = json.dumps(result.get("sms", []), ensure_ascii=False)

            db().execute(
                """INSERT INTO five_sim_orders
                   (user_id, provider_order_id, phone, country, product, status,
                    sms_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    g.user["id"], provider_order_id, phone, country, product,
                    status, sms_json, now(), now(),
                ),
            )

            flash("5SIM order created successfully.", "ok")
            return redirect(url_for("five_sim_orders"))

        except five_sim.FiveSimError as exc:
            flash("5SIM order failed: " + str(exc), "error")
            return redirect(url_for("five_sim_catalogue") + "?country=" + country)

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
            sms_json = json.dumps(result.get("sms", []), ensure_ascii=False)

            d.execute(
                """UPDATE five_sim_orders
                   SET status=?, phone=?, sms_json=?, updated_at=?
                   WHERE id=? AND user_id=?""",
                (status, phone, sms_json, now(), order_row_id, g.user["id"]),
            )
            flash("5SIM order status refreshed.", "ok")

        except five_sim.FiveSimError as exc:
            flash("Could not refresh 5SIM order: " + str(exc), "error")

        return redirect(url_for("five_sim_orders"))

    @app.post("/webhook/sms")
    def webhook_sms():
        """Africa's Talking inbound SMS callback (form fields: from, to, text, id, date)."""
        p = provider()
        secret = os.environ.get("WEBHOOK_SECRET", "")
        if p["mode"] != "live":
            return jsonify(error="Webhook disabled: provider not in live mode / not configured."), 503
        if not hmac.compare_digest(secret, request.args.get("secret", "")):
            return jsonify(error="forbidden"), 403
        to, sender = request.form.get("to", "").strip(), request.form.get("from", "").strip()
        text, mid = request.form.get("text", ""), request.form.get("id") or None
        n = db().execute("SELECT id FROM numbers WHERE number=? AND is_demo=0", (to,)).fetchone()
        if not n or not sender:
            return jsonify(status="ignored"), 200
        try:
            db().execute("INSERT INTO messages(number_id,sender,body,is_demo,provider_id,received_at) VALUES(?,?,?,?,?,?)",
                         (n["id"], sender, text, 0, mid, now()))
        except sqlite3.IntegrityError:
            pass  # duplicate delivery
        return jsonify(status="ok"), 200


    def telegram_send(chat_id, message, token):
        payload = json.dumps({"chat_id": chat_id, "text": message}).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                return response.status == 200
        except Exception:
            app.logger.warning("Telegram sendMessage failed.")
            return False

    @app.get("/telegram/link")
    @login_required
    def telegram_link_page():
        d = db()
        d.execute(
            "DELETE FROM telegram_link_codes WHERE expires_at < ?",
            (int(time.time()),),
        )
        code = secrets.token_hex(4).upper()
        d.execute(
            "INSERT INTO telegram_link_codes(code,user_id,expires_at) "
            "VALUES(?,?,?)",
            (code, g.user["id"], int(time.time()) + 600),
        )
        return render_template("telegram_link.html", code=code)

    @app.post("/telegram/webhook")
    def telegram_webhook():
        expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
        supplied = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )
        if not expected or not hmac.compare_digest(expected, supplied):
            return jsonify(error="forbidden"), 403

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            return jsonify(error="bot not configured"), 503

        update = request.get_json(silent=True) or {}
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = (message.get("text") or "").strip()

        if chat.get("type") != "private" or not sender.get("id") or not text:
            return jsonify(ok=True)

        chat_id = chat.get("id")
        telegram_id = str(sender["id"])
        parts = text.split(maxsplit=1)
        command = parts[0].split("@")[0].lower()
        argument = parts[1].strip().upper() if len(parts) > 1 else ""

        if command in ("/start", "/help"):
            reply = (
                "Welcome to NOVA NUMBER HUB!\n\n"
                "Commands: /link CODE, /account, /balance, "
                "/requests, /support.\n"
                "Balances and numbers may be DEMO simulations."
            )

        elif command == "/link":
            if not re.fullmatch(r"[A-F0-9]{8}", argument):
                reply = (
                    "Log in to NumberHub, open /telegram/link, then "
                    "send /link CODE here. The code expires in 10 minutes."
                )
            else:
                d = db()
                d.execute("BEGIN IMMEDIATE")
                row = d.execute(
                    "SELECT user_id FROM telegram_link_codes "
                    "WHERE code=? AND expires_at>=?",
                    (argument, int(time.time())),
                ).fetchone()

                if not row:
                    d.execute("ROLLBACK")
                    reply = "Invalid or expired code. Generate a new one on the website."
                else:
                    uid = row["user_id"]
                    by_tg = d.execute(
                        "SELECT user_id FROM telegram_links WHERE telegram_user_id=?",
                        (telegram_id,),
                    ).fetchone()
                    by_user = d.execute(
                        "SELECT telegram_user_id FROM telegram_links WHERE user_id=?",
                        (uid,),
                    ).fetchone()

                    if by_tg or by_user:
                        if (by_tg and by_user
                                and by_tg["user_id"] == uid
                                and by_user["telegram_user_id"] == telegram_id):
                            reply = "Your Telegram account is already linked."
                            d.execute(
                                "DELETE FROM telegram_link_codes WHERE code=?",
                                (argument,),
                            )
                            d.execute("COMMIT")
                        else:
                            d.execute("ROLLBACK")
                            reply = "An account is already linked. Contact website support."
                    else:
                        d.execute(
                            "INSERT INTO telegram_links "
                            "(telegram_user_id,user_id,linked_at) VALUES(?,?,?)",
                            (telegram_id, uid, now()),
                        )
                        d.execute(
                            "DELETE FROM telegram_link_codes WHERE code=?",
                            (argument,),
                        )
                        d.execute("COMMIT")
                        reply = "Account linked! Try /account, /balance, or /requests."

        elif command in ("/account", "/balance", "/requests"):
            d = db()
            linked = d.execute(
                "SELECT u.id,u.email FROM telegram_links t "
                "JOIN users u ON u.id=t.user_id WHERE t.telegram_user_id=?",
                (telegram_id,),
            ).fetchone()

            if not linked:
                reply = "Your account is not linked. Follow the /link instructions."
            elif command == "/account":
                reply = "Linked NumberHub account: " + linked["email"]
            elif command == "/balance":
                cents = balance(d, linked["id"])
                label = "DEMO wallet balance" if provider()["mode"] == "demo" else "Wallet balance"
                reply = "%s: %.2f credits" % (label, cents / 100)
            else:
                rows = d.execute(
                    "SELECT number,is_demo,status,created_at FROM numbers "
                    "WHERE user_id=? ORDER BY id DESC LIMIT 10",
                    (linked["id"],),
                ).fetchall()
                reply = "\n".join(
                    "%s%s — %s — %s" % (
                        item["number"],
                        " [DEMO]" if item["is_demo"] else "",
                        item["status"], item["created_at"],
                    )
                    for item in rows
                ) if rows else "You have no number requests yet."

        elif command == "/support":
            reply = "NOVA NUMBER HUB support: visit the NumberHub website for contact information."
        else:
            reply = "Unknown command. Send /help."

        telegram_send(chat_id, reply, token)
        return jsonify(ok=True)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
