"""Local, resettable shop fixture. Fault controls exist only in the Python test harness."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
from threading import Thread
import time

from flask import Flask, abort, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.serving import make_server

FAULTS = {"valid_login_denied", "invalid_password_accepted", "cart_quantity_wrong", "cart_lost_on_refresh",
          "session_lost", "ambiguous_buttons", "delayed_response", "stale_locator", "service_error"}
CONTRACT = {
    "registration": "A new username and a password of at least six characters create an account without logging in.",
    "login": "Only a registered username with its correct password may log in.",
    "login_success_ui": "Successful login redirects to products and displays testid:identity with Signed in followed by the account. Rejection remains on the login form with Invalid credentials and creates no authenticated session.",
    "password_rejection": "An incorrect password is rejected with message Invalid credentials (testid:message), without creating a login session.",
    "anonymous_cart": "An unauthenticated cart GET returns HTTP 401 and message Log in to view the cart (testid:message); its initial state is empty, in an independent scene.",
    "cart": "A logged-in user adding one Agent Handbook sees quantity 1, retained after refresh.",
    "sms": "A code is valid once for its phone, challenge, browser owner and run for 60 seconds; three wrong attempts lock it.",
}


class ShopStore:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.faults, self.codes = {}, {}
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (ns TEXT PRIMARY KEY, login_count INTEGER DEFAULT 0, send_count INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS accounts (ns TEXT, username TEXT, password TEXT, PRIMARY KEY(ns, username));
                CREATE TABLE IF NOT EXISTS carts (ns TEXT, username TEXT, sku TEXT, quantity INTEGER, PRIMARY KEY(ns, username, sku));
                CREATE TABLE IF NOT EXISTS challenges (id TEXT PRIMARY KEY, ns TEXT, phone TEXT, owner TEXT,
                    digest TEXT, expires REAL, attempts INTEGER DEFAULT 0, cancelled INTEGER DEFAULT 0, used INTEGER DEFAULT 0);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def prepare(self, namespace, faults=()):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", namespace) or set(faults) - FAULTS:
            raise ValueError("Invalid test namespace or fault configuration")
        with self.connection() as db:
            challenge_ids = [r[0] for r in db.execute("SELECT id FROM challenges WHERE ns=?", (namespace,))]
            for table in ("accounts", "carts", "challenges", "runs"):
                db.execute(f"DELETE FROM {table} WHERE ns=?", (namespace,))
            db.execute("INSERT INTO runs(ns) VALUES (?)", (namespace,))
        for identity in challenge_ids:
            self.codes.pop(identity, None)
        self.faults[namespace] = frozenset(faults)
        return namespace

    def stats(self, namespace):
        with self.connection() as db:
            row = db.execute("SELECT login_count,send_count FROM runs WHERE ns=?", (namespace,)).fetchone()
            return dict(row)

    def account(self, namespace, username, password="TestPass123!"):
        with self.connection() as db:
            db.execute("INSERT INTO accounts VALUES (?,?,?)", (namespace, username, generate_password_hash(password)))

    def account_state(self, namespace, username, password):
        """Read-only test evidence; expose neither account values nor stored hashes."""
        with self.connection() as db:
            row = db.execute("SELECT password FROM accounts WHERE ns=? AND username=?", (namespace, username)).fetchone()
        return {"account_exists": row is not None, "password_matches": row is not None and check_password_hash(row[0], password)}

    def test_code(self, challenge_id):
        """Local fake-SMS delivery, deliberately unavailable as a website route."""
        return self.codes[challenge_id]

    def issue(self, ns, phone, owner):
        identity, code = secrets.token_hex(12), f"{secrets.randbelow(1000000):06d}"
        with self.connection() as db:
            db.execute("UPDATE challenges SET cancelled=1 WHERE ns=? AND phone=? AND owner=? AND used=0", (ns, phone, owner))
            db.execute("INSERT INTO challenges(id,ns,phone,owner,digest,expires) VALUES (?,?,?,?,?,?)",
                       (identity, ns, phone, owner, hashlib.sha256(code.encode()).hexdigest(), self.clock() + 60))
            db.execute("UPDATE runs SET send_count=send_count+1 WHERE ns=?", (ns,))
        self.codes[identity] = code
        return identity

    def verify_code(self, ns, phone, owner, identity, code):
        with self.connection() as db:
            row = db.execute("SELECT * FROM challenges WHERE id=?", (identity,)).fetchone()
            if row is None or (row["ns"], row["phone"], row["owner"]) != (ns, phone, owner):
                return False
            if row["cancelled"] or row["used"] or row["attempts"] >= 3 or self.clock() >= row["expires"]:
                return False
            if not secrets.compare_digest(row["digest"], hashlib.sha256(code.encode()).hexdigest()):
                db.execute("UPDATE challenges SET attempts=attempts+1 WHERE id=?", (identity,))
                return False
            db.execute("UPDATE challenges SET used=1 WHERE id=?", (identity,))
            return True


SHELL = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Local Test Shop</title>
<style>body{font:17px system-ui;max-width:800px;margin:40px auto;padding:24px;background:#f4f7fc}main{background:white;padding:28px;border-radius:12px}label{display:block;margin:16px 0}input,button{font:inherit;padding:8px}nav a{margin-right:16px}p[role=alert]{color:#b22222}</style>
<h1>Local Test Shop</h1><nav>
{% for path,label in [('register','Register'),('login','Log in'),('sms','SMS login'),('products','Products'),('cart','Cart')] %}<a href="{{ url_for(path,ns=ns) }}">{{label}}</a>{% endfor %}</nav>
{% if username %}<p data-testid="identity">Signed in: {{username}}</p><form action="{{url_for('logout',ns=ns)}}" method="post"><button>Log out</button></form>{% endif %}
<main>{% if message %}<p role="{{ 'alert' if error else 'status' }}" data-testid="message">{{message}}</p>{% endif %}BODY</main></html>"""


def create_shop(store):
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    app.config.update(TESTING=False, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

    @app.before_request
    def check_run():
        ns = (request.view_args or {}).get("ns")
        if ns is None or ns not in store.faults:
            abort(404)
        if "service_error" in store.faults[ns]:
            abort(503)
        session.setdefault("owner", secrets.token_hex(16))

    def username(ns):
        return session.get("username") if session.get("ns") == ns else None

    def page(ns, body, message="", error=False, **values):
        return render_template_string(SHELL.replace("BODY", body), ns=ns, username=username(ns),
                                      message=message, error=error, **values)

    @app.route("/shop/<ns>/register", methods=["GET", "POST"])
    def register(ns):
        message, error = "", False
        if request.method == "POST":
            name, password = request.form.get("username", "").strip(), request.form.get("password", "")
            if not name or len(password) < 6:
                message, error = "Username and a password of at least six characters are required", True
            else:
                try:
                    store.account(ns, name, password)
                    message = "Registration successful"
                except sqlite3.IntegrityError:
                    message, error = "Username already exists", True
        return page(ns, '<h2>Register</h2><form method="post" data-testid="register-form"><label>Username<input name="username" required></label><label>Password<input name="password" type="password" required minlength="6"></label><button data-testid="register-submit">Register account</button></form>', message, error)

    @app.route("/shop/<ns>/login", methods=["GET", "POST"])
    def login(ns):
        message, error = "", False
        if request.method == "POST":
            name, password = request.form.get("username", ""), request.form.get("password", "")
            with store.connection() as db:
                row = db.execute("SELECT password FROM accounts WHERE ns=? AND username=?", (ns, name)).fetchone()
                valid = row is not None and check_password_hash(row[0], password)
                if "invalid_password_accepted" in store.faults[ns] and row is not None:
                    valid = True
                if "valid_login_denied" in store.faults[ns]:
                    valid = False
                if valid:
                    session.update(ns=ns, username=name)
                    db.execute("UPDATE runs SET login_count=login_count+1 WHERE ns=?", (ns,))
                    return redirect(url_for("products", ns=ns))
            message, error = "Invalid credentials", True
        button_id = "login-current" if "stale_locator" in store.faults[ns] else "login-submit"
        body = '<h2>Log in</h2><form method="post" data-testid="login-form"><label>Username<input name="username" required></label><label>Password<input name="password" type="password" required></label><button data-testid="' + button_id + '">Log in</button></form>'
        if "ambiguous_buttons" in store.faults[ns]:
            body += '<aside><button type="button">Log in</button></aside>'
        return page(ns, body, message, error)

    @app.route("/shop/<ns>/sms", methods=["GET", "POST"])
    def sms(ns):
        challenge = request.form.get("challenge", "")
        phone = request.form.get("phone", "")
        message, error = "", False
        if request.method == "POST":
            op = request.form.get("op")
            if op == "send" and re.fullmatch(r"1\d{10}", phone):
                challenge = store.issue(ns, phone, session["owner"])
                message = "Simulation: code sent through the local test channel"
            elif op == "cancel":
                with store.connection() as db:
                    db.execute("UPDATE challenges SET cancelled=1 WHERE id=? AND ns=? AND owner=?", (challenge, ns, session["owner"]))
                message = "Challenge cancelled"
            elif op == "verify" and store.verify_code(ns, phone, session["owner"], challenge, request.form.get("code", "")):
                name = "sms_" + phone
                session.update(ns=ns, username=name)
                with store.connection() as db:
                    db.execute("UPDATE runs SET login_count=login_count+1 WHERE ns=?", (ns,))
                return redirect(url_for("products", ns=ns))
            else:
                message, error = "Invalid, expired or unavailable code", True
        return page(ns, '''<h2>Simulated SMS login</h2><p>No real SMS is sent.</p><form method="post" data-testid="sms-form"><label>Phone<input name="phone" value="{{phone}}" required></label><input name="challenge" type="hidden" value="{{challenge}}" data-testid="challenge"><button name="op" value="send">Send code</button><label>Code<input name="code" autocomplete="off"></label><button name="op" value="verify">Verify code</button><button name="op" value="cancel">Cancel challenge</button></form>''', message, error, phone=phone, challenge=challenge)

    @app.get("/shop/<ns>/products")
    def products(ns):
        if "session_lost" in store.faults[ns]:
            session.pop("username", None)
        body = '<h2>Products</h2><article data-testid="product-book"><h3>Agent Handbook</h3><p>29.00</p><form method="post" data-testid="add-form" action="{{url_for(\'add\',ns=ns)}}"><input type="hidden" name="sku" value="book"><button>Add to cart</button></form></article>'
        if "delayed_response" in store.faults[ns]:
            body = '<div id="delayed" hidden>' + body + '</div><script>setTimeout(()=>document.getElementById("delayed").hidden=false,350)</script>'
        return page(ns, body)

    @app.post("/shop/<ns>/add")
    def add(ns):
        user = username(ns)
        if not user:
            return page(ns, "<h2>Authentication required</h2>", "Log in before adding to cart", True), 401
        if request.form.get("sku") != "book":
            abort(400)
        amount = 2 if "cart_quantity_wrong" in store.faults[ns] else 1
        with store.connection() as db:
            db.execute("INSERT INTO carts VALUES (?,?,?,?) ON CONFLICT(ns,username,sku) DO UPDATE SET quantity=quantity+excluded.quantity", (ns, user, "book", amount))
        return redirect(url_for("cart", ns=ns))

    @app.get("/shop/<ns>/cart")
    def cart(ns):
        user = username(ns)
        if not user:
            return page(ns, "<h2>Authentication required</h2>", "Log in to view the cart", True), 401
        with store.connection() as db:
            row = db.execute("SELECT quantity FROM carts WHERE ns=? AND username=? AND sku='book'", (ns, user)).fetchone()
            quantity = row[0] if row else 0
            if "cart_lost_on_refresh" in store.faults[ns]:
                db.execute("DELETE FROM carts WHERE ns=? AND username=?", (ns, user))
        return page(ns, '<h2>Cart</h2>{% if quantity %}<article data-testid="cart-book">Agent Handbook <span data-testid="quantity">{{quantity}}</span></article>{% else %}<p data-testid="empty-cart">Cart is empty</p>{% endif %}', quantity=quantity)

    @app.post("/shop/<ns>/logout")
    def logout(ns):
        session.pop("username", None)
        session.pop("ns", None)
        return redirect(url_for("login", ns=ns))

    return app


@contextmanager
def shop_server(store=None, port=0):
    with tempfile.TemporaryDirectory(prefix="e2e-shop-") as directory:
        store = store or ShopStore(Path(directory) / "shop.sqlite3")
        server = make_server("127.0.0.1", port, create_shop(store), threaded=True)
        thread = Thread(target=server.serve_forever, name="test-shop", daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", store
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
