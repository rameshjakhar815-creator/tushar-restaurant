"""
Tusar Hotel Group â€” Restaurant ordering website.

Run:
    pip install -r requirements.txt
    python3 app.py

Then open http://127.0.0.1:5000

First run auto-creates tusar_restaurant.db (SQLite) with sample menu data
and one admin account:
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, g, flash, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import uuid
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tusar_restaurant.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "images", "menu")
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

TAX_RATE = 0.05  # 5% â€” adjust to your local tax rate in one place

ORDER_STATUSES = ["pending", "confirmed", "preparing", "out_for_delivery", "delivered", "cancelled"]
STATUS_LABELS = {
    "pending": "Order received",
    "confirmed": "Confirmed",
    "preparing": "In the kitchen",
    "out_for_delivery": "Out for delivery",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}
PAYMENT_LABELS = {"pending": "Payment pending", "received": "Payment received"}
# Order the "tandoor timeline" tracker walks through (cancelled is shown separately).
STATUS_SEQUENCE = ["pending", "confirmed", "preparing", "out_for_delivery", "delivered"]

app = Flask(__name__)
app.secret_key = os.environ.get("TUSAR_SECRET_KEY", "dev-secret-change-me-in-production")

# ------------------------------------------------------------------ restaurant settings
# These defaults can be changed from Admin â†’ Settings.
DEFAULT_SETTINGS = {
    "restaurant_name": "Group of Tushar Restaurant",
    "location": "Aashiyana Umang near Mahindra Sez, Bhankrota, Jaipur, Rajasthan 302042",
    "delivery_radius_km": "4",
    "upi_id": os.environ.get("TUSAR_UPI_ID", "groupoftusar@ybl"),
    "whatsapp_number": os.environ.get("TUSAR_WHATSAPP_NUMBER", ""),
    "restaurant_phone": os.environ.get("TUSAR_RESTAURANT_PHONE", ""),
    "restaurant_open": "1",
    "cod_enabled": "0",
    "upi_qr_path": "",
    "whatsapp_auto_enabled": "0",
    "whatsapp_api_token": "",
    "whatsapp_phone_number_id": "",
}

def get_site_settings(db):
    rows = db.execute("SELECT key, value FROM site_settings").fetchall()
    settings = dict(DEFAULT_SETTINGS)
    settings.update({r["key"]: r["value"] for r in rows})
    return settings

def build_whatsapp_messages(order, items, settings=None):
    """Build clear customer WhatsApp messages for each order stage."""
    item_lines = "\n".join(
        f"{item['quantity']} x {item['item_name']} - â‚¹{item['unit_price'] * item['quantity']:.0f}"
        for item in items
    )
    customer = order["customer_name"] or "Customer"
    order_type = "Delivery" if order["order_type"] == "delivery" else "Dine-in / Pickup"
    settings = settings or DEFAULT_SETTINGS
    address = settings.get("location", DEFAULT_SETTINGS["location"])
    common = (
        f"Dear {customer},\n\n"
        f"Your Order No: #{order['id']:04d}\n"
        f"Order Details:\n{item_lines}\n\n"
        f"Amount: â‚¹{order['total']:.2f}\n"
        f"Payment: Received\n"
        f"Order Type: {order_type}\n"
    )
    footer = (
        f"\nGroup of Tushar Restaurant\n"
        f"{address}\n"
        f"Phone: {settings.get('restaurant_phone', '')}\n\n"
        f"Thank you for your order.\n\n"
        f"Group of Tushar Restaurant\n"
        f"Salute to everyone who fights for our country\n"
        f"Jai Hind"
    )
    return {
        "confirmed": common + "Order Status: Confirmed\n\nYour order has been confirmed and is being prepared." + footer,
        "preparing": common + "Order Status: In Kitchen\n\nYour order is now being prepared fresh in our kitchen." + footer,
        "out_for_delivery": common + "Order Status: Out for Delivery\n\nYour order is on the way to you." + footer,
        "delivered": common + "Order Status: Delivered\n\nYour order has been delivered. We hope you enjoyed your meal." + footer,
    }

def whatsapp_link(number, message):
    import urllib.parse
    digits = re.sub(r"\\D", "", number or "")
    if not digits:
        return ""
    return "https://wa.me/" + digits + "?text=" + urllib.parse.quote(message)

def upi_link(upi_id, amount, order_id):
    import urllib.parse
    if not upi_id:
        return ""
    params = {
        "pa": upi_id,
        "pn": "Group of Tushar Restaurant",
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": f"Order #{order_id}",
    }
    return "upi://pay?" + urllib.parse.urlencode(params)


def send_whatsapp_cloud_text(number, message, settings):
    """Best-effort official WhatsApp Cloud API send when credentials are configured.

    If API credentials are not configured, returns False and the normal wa.me
    link remains available. No third-party WhatsApp automation is used.
    """
    token = (settings.get("whatsapp_api_token") or "").strip()
    phone_number_id = (settings.get("whatsapp_phone_number_id") or "").strip()
    if not token or not phone_number_id:
        return False
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return False
    try:
        import urllib.request, json as _json
        url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
        payload = _json.dumps({
            "messaging_product": "whatsapp",
            "to": digits,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            return 200 <= response.status < 300
    except Exception:
        return False



# ------------------------------------------------------------------ database
class PGDatabase:
    def __init__(self, url):
        self.conn = psycopg2.connect(url)
        self.conn.autocommit = False

    def execute(self, sql, params=()):
        # Convert SQLite ? placeholders to PostgreSQL %s.
        sql = sql.replace("?", "%s")

        # PostgreSQL does not support SQLite's INSERT OR IGNORE.
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")

        cur = self.conn.cursor(cursor_factory=RealDictCursor)

        # PostgreSQL needs RETURNING id for code that uses lastrowid.
        stripped = sql.strip().upper()
        if stripped.startswith("INSERT") and " RETURNING " not in stripped and "SITE_SETTINGS" not in stripped:
            sql = sql.rstrip().rstrip(";") + " RETURNING id"

        cur.execute(sql, params)

        return PGCursor(cur, self.conn)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


class PGCursor:
    def __init__(self, cursor, conn):
        self.cursor = cursor
        self.conn = conn
        self._lastrowid = None

        try:
            if cursor.description:
                row = cursor.fetchone()
                if row and "id" in row:
                    self._lastrowid = row["id"]
        except Exception:
            self._lastrowid = None

    @property
    def lastrowid(self):
        return self._lastrowid

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor.fetchall())


def get_db():
    if "db" not in g:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        g.db = PGDatabase(database_url)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()

    # PostgreSQL database was populated from the protected SQLite backup.
    # Keep this initialization lightweight and non-destructive.

    for key, value in DEFAULT_SETTINGS.items():
        db.execute(
            """
            INSERT INTO site_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT (key) DO NOTHING
            """,
            (key, value),
        )

    db.execute("UPDATE users SET password_hash = ? WHERE email = ? AND role = ?", ("scrypt:32768:8:1$xVtHJ3AiFCRy5vfW$9dcf2d6770667cf0c45093444f8092da3a3292c24f14ac04bdef5a8a3e5b6d53175ea0d05c0f0004623963d54c83a3a51876852850a12e288b09568387f41de9", "admin@tusarhotel.com", "admin"))
    db.commit()


def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def save_menu_image(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_image(file_storage.filename):
        return None
    ext = secure_filename(file_storage.filename).rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(UPLOAD_FOLDER, filename))
    return f"images/menu/{filename}"

def delete_menu_image(image_path):
    if not image_path:
        return
    full_path = os.path.join(BASE_DIR, "static", image_path.replace("/", os.sep))
    if os.path.isfile(full_path):
        try:
            os.remove(full_path)
        except OSError:
            pass


def seed_db(db):
    admin_hash = "scrypt:32768:8:1$xVtHJ3AiFCRy5vfW$9dcf2d6770667cf0c45093444f8092da3a3292c24f14ac04bdef5a8a3e5b6d53175ea0d05c0f0004623963d54c83a3a51876852850a12e288b09568387f41de9"
    db.execute(
        "INSERT INTO users (name, email, phone, address, password_hash, role) VALUES (?,?,?,?,?,?)",
        ("Tusar Admin", "admin@tusarhotel.com", "9999999999", "Tusar Hotel HQ", admin_hash, "admin"),
    )

    categories = ["Starters", "Main Course", "Breads", "Rice & Biryani", "Desserts", "Beverages"]
    cat_ids = {}
    for i, name in enumerate(categories):
        cur = db.execute("INSERT INTO categories (name, sort_order) VALUES (?,?)", (name, i))
        cat_ids[name] = cur.lastrowid

    items = [
        # (category, name, description, price, is_veg, emoji)
        ("Starters", "Paneer Tikka", "Chargrilled cottage cheese marinated in spiced yogurt.", 220, 1, "ðŸ§€"),
        ("Starters", "Chicken 65", "Fiery South Indian fried chicken with curry leaves.", 260, 0, "ðŸ—"),
        ("Starters", "Veg Spring Rolls", "Crisp rolls with julienned vegetables.", 180, 1, "ðŸ¥Ÿ"),
        ("Main Course", "Butter Chicken", "Tandoori chicken simmered in a velvety tomato-butter gravy.", 320, 0, "ðŸ›"),
        ("Main Course", "Dal Makhani", "Slow-cooked black lentils finished with cream.", 240, 1, "ðŸ²"),
        ("Main Course", "Palak Paneer", "Cottage cheese in a smooth spiced spinach gravy.", 260, 1, "ðŸ¥¬"),
        ("Main Course", "Mutton Rogan Josh", "Kashmiri-style slow-braised mutton curry.", 380, 0, "ðŸ–"),
        ("Breads", "Butter Naan", "Tandoor-baked leavened bread brushed with butter.", 60, 1, "ðŸ«“"),
        ("Breads", "Garlic Naan", "Naan topped with fresh garlic and coriander.", 70, 1, "ðŸ«“"),
        ("Breads", "Tandoori Roti", "Whole-wheat bread from the clay oven.", 40, 1, "ðŸ«“"),
        ("Rice & Biryani", "Hyderabadi Chicken Biryani", "Layered basmati rice with slow-cooked spiced chicken.", 300, 0, "ðŸš"),
        ("Rice & Biryani", "Veg Biryani", "Fragrant basmati rice with garden vegetables and saffron.", 240, 1, "ðŸš"),
        ("Rice & Biryani", "Jeera Rice", "Basmati rice tempered with cumin.", 150, 1, "ðŸš"),
        ("Desserts", "Gulab Jamun", "Warm milk-solid dumplings in rose-cardamom syrup.", 90, 1, "ðŸ®"),
        ("Desserts", "Rasmalai", "Soft cottage-cheese discs in saffron milk.", 110, 1, "ðŸ¥›"),
        ("Beverages", "Masala Chai", "Spiced Indian tea.", 40, 1, "â˜•"),
        ("Beverages", "Sweet Lassi", "Chilled churned yogurt drink.", 70, 1, "ðŸ¥¤"),
        ("Beverages", "Fresh Lime Soda", "Lime, soda, and a pinch of salt or sugar.", 60, 1, "ðŸ¥¤"),
    ]
    for cat, name, desc, price, is_veg, emoji in items:
        db.execute(
            "INSERT INTO menu_items (category_id, name, description, price, is_veg, emoji, image_path) VALUES (?,?,?,?,?,?,?)",
            (cat_ids[cat], name, desc, price, is_veg, emoji, None),
        )
    db.commit()


# ------------------------------------------------------------------- helpers
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


@app.context_processor
def inject_globals():
    cart = session.get("cart", {})
    cart_count = sum(cart.values()) if cart else 0
    db = get_db()
    settings = get_site_settings(db)
    return {
        "current_user": current_user(),
        "cart_count": cart_count,
        "STATUS_LABELS": STATUS_LABELS,
        "site_settings": settings,
        "DELIVERY_RADIUS_KM": settings.get("delivery_radius_km", "4"),
    }


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u or u["role"] != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def get_cart_details(db):
    """Resolve session cart {item_id: qty} into full line items + totals."""
    cart = session.get("cart", {})
    lines = []
    subtotal = 0.0
    for item_id_str, qty in cart.items():
        item = db.execute("SELECT * FROM menu_items WHERE id = ?", (int(item_id_str),)).fetchone()
        if item is None or not item["is_available"]:
            continue
        line_total = item["price"] * qty
        subtotal += line_total
        lines.append({"item": item, "qty": qty, "line_total": line_total})
    tax = round(subtotal * TAX_RATE, 2)
    total = round(subtotal + tax, 2)
    return lines, round(subtotal, 2), tax, total


# =============================================================================
# Public / customer routes
# =============================================================================
@app.route("/health")
def health():
    return {"status":"ok"}, 200

@app.route("/")
def home():
    db = get_db()
    featured = db.execute(
        "SELECT * FROM menu_items WHERE is_available = 1 ORDER BY RANDOM() LIMIT 6"
    ).fetchall()
    hero = db.execute(
        "SELECT * FROM hero_slides "
        "WHERE ("
        "      (start_at IS NOT NULL "
        "       AND start_at <= CURRENT_TIMESTAMP "
        "       AND (end_at IS NULL OR end_at >= CURRENT_TIMESTAMP))"
        "   OR (is_active = 1 AND (start_at IS NULL OR start_at <= CURRENT_TIMESTAMP) "
        "       AND (end_at IS NULL OR end_at >= CURRENT_TIMESTAMP))"
        ") "
        "ORDER BY "
        "CASE WHEN start_at IS NOT NULL AND start_at <= CURRENT_TIMESTAMP THEN 0 ELSE 1 END, "
        "COALESCE(start_at, TIMESTAMP '1970-01-01 00:00:00') DESC, id DESC LIMIT 1"
    ).fetchone()
    return render_template("index.html", featured=featured, hero=hero)


@app.route("/menu")
def menu():
    db = get_db()
    categories = db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()
    menu_filter = request.args.get("type", "all").lower()
    if menu_filter not in {"all", "veg", "nonveg"}:
        menu_filter = "all"
    items_by_cat = {}
    for cat in categories:
        if menu_filter == "veg":
            query = "SELECT * FROM menu_items WHERE category_id = ? AND is_available = 1 AND is_veg = 1 ORDER BY name"
        elif menu_filter == "nonveg":
            query = "SELECT * FROM menu_items WHERE category_id = ? AND is_available = 1 AND is_veg = 0 ORDER BY name"
        else:
            query = "SELECT * FROM menu_items WHERE category_id = ? AND is_available = 1 ORDER BY name"
        items_by_cat[cat["id"]] = db.execute(query, (cat["id"],)).fetchall()
    return render_template("menu.html", categories=categories, items_by_cat=items_by_cat, menu_filter=menu_filter)


@app.route("/cart/add/<int:item_id>", methods=["POST"])
def cart_add(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM menu_items WHERE id = ? AND is_available = 1", (item_id,)).fetchone()
    if item is None:
        abort(404)
    cart = session.get("cart", {})
    key = str(item_id)
    cart[key] = cart.get(key, 0) + 1
    session["cart"] = cart
    flash(f"Added {item['name']} to your cart.", "success")
    return redirect(request.referrer or url_for("menu"))


@app.route("/cart/update/<int:item_id>", methods=["POST"])
def cart_update(item_id):
    qty = request.form.get("qty", type=int)
    cart = session.get("cart", {})
    key = str(item_id)
    if qty is None or qty <= 0:
        cart.pop(key, None)
    else:
        cart[key] = qty
    session["cart"] = cart
    return redirect(url_for("cart_view"))


@app.route("/cart")
def cart_view():
    db = get_db()
    lines, subtotal, tax, total = get_cart_details(db)
    return render_template("cart.html", lines=lines, subtotal=subtotal, tax=tax, total=total)


@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    db = get_db()
    site_settings = get_site_settings(db)
    if str(site_settings.get("restaurant_open", "1")) != "1":
        flash("Restaurant is currently closed. Please try again when ordering is open.", "error")
        return redirect(url_for("home"))
    lines, subtotal, tax, total = get_cart_details(db)
    if not lines:
        flash("Your cart is empty.", "error")
        return redirect(url_for("menu"))

    user = current_user()
    if request.method == "POST":
        # Delivery only â€” pickup and Cash on Delivery are intentionally disabled.
        order_type = "delivery"
        address = request.form.get("address", "").strip()
        phone = request.form.get("phone", "").strip()
        notes = request.form.get("notes", "").strip()
        payment_method = request.form.get("payment_method", "upi").strip().lower()

        if not address:
            flash("Please enter a delivery address. Delivery is available within 4 km only.", "error")
            return render_template("checkout.html", lines=lines, subtotal=subtotal, tax=tax, total=total, user=user)

        cod_enabled = str(site_settings.get("cod_enabled", "0")) == "1"
        if payment_method not in ({"upi", "cod"} if cod_enabled else {"upi"}):
            flash("Please select a valid payment method.", "error")
            return render_template("checkout.html", lines=lines, subtotal=subtotal, tax=tax, total=total, user=user, site_settings=site_settings)

        # Keep the selected payment method in the order note for backward compatibility.
        if payment_method == "cod":
            notes = (notes + "\nPayment Method: Cash on Delivery").strip()

        cur = db.execute(
            "INSERT INTO orders (user_id, status, order_type, address, phone, notes, subtotal, tax, total) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user["id"], "pending", order_type, address, phone, notes, subtotal, tax, total),
        )
        order_id = cur.lastrowid
        for line in lines:
            db.execute(
                "INSERT INTO order_items (order_id, menu_item_id, item_name, unit_price, quantity) VALUES (?,?,?,?,?)",
                (order_id, line["item"]["id"], line["item"]["name"], line["item"]["price"], line["qty"]),
            )
        db.commit()
        session["cart"] = {}
        return redirect(url_for("order_detail", order_id=order_id))

    return render_template("checkout.html", lines=lines, subtotal=subtotal, tax=tax, total=total, user=user)


@app.route("/orders")
@login_required
def my_orders():
    db = get_db()
    user = current_user()
    orders = db.execute(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
    ).fetchall()
    table_bookings = db.execute(
        "SELECT * FROM table_bookings WHERE user_id = ? ORDER BY booking_date DESC, booking_time DESC, created_at DESC",
        (user["id"],)
    ).fetchall()
    return render_template("my_orders.html", orders=orders, table_bookings=table_bookings)


@app.route("/orders/<int:order_id>")
@login_required
def order_detail(order_id):
    db = get_db()
    user = current_user()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        abort(404)
    if order["user_id"] != user["id"] and user["role"] != "admin":
        abort(403)
    items = db.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    step_index = STATUS_SEQUENCE.index(order["status"]) if order["status"] in STATUS_SEQUENCE else -1
    settings = get_site_settings(db)
    payment_link = upi_link(settings.get("upi_id", ""), order["total"], order_id)
    item_lines = "\n".join(
        f"{item['quantity']}Ã— {item['item_name']} â€” â‚¹{item['unit_price'] * item['quantity']:.0f}"
        for item in items
    )
    wa_message = (
        f"Dear {user['name']},\n\n"
        f"Thank you for your order with Group of Tushar Restaurant. ðŸ™\n\n"
        f"ðŸ§¾ Order No.: {order_id}\n\n"
        f"ðŸ½ï¸ Your Order:\n{item_lines}\n\n"
        f"ðŸ’° Total Amount: â‚¹{order['total']:.2f}\n"
        f"ðŸ’³ Payment: Received Successfully âœ…\n"
        f"ðŸ“¦ Order: Confirmed âœ…\n\n"
        f"Your order is now being prepared. We will keep you updated about your order status.\n\n"
        f"Thank you for choosing Group of Tushar Restaurant. â¤ï¸\n\n"
        f"Group of Tushar Restaurant\n"
        f"Salute to everyone who fights for our country.\n"
        f"Jai Hind!\n"
        f"I Love My India â¤ï¸"
    )
    wa_link = whatsapp_link(settings.get("whatsapp_number", ""), wa_message)
    return render_template(
        "order_detail.html", order=order, items=items,
        status_sequence=STATUS_SEQUENCE, step_index=step_index,
        payment_link=payment_link, wa_link=wa_link, settings=settings,
        PAYMENT_LABELS=PAYMENT_LABELS,
    )


# ------------------------------------------------------------------ auth
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("Name, email, and password are required.", "error")
            return render_template("register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")

        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            flash("An account with that email already exists.", "error")
            return render_template("register.html")

        db.execute(
            "INSERT INTO users (name, email, phone, password_hash, role) VALUES (?,?,?,?,'customer')",
            (name, email, phone, generate_password_hash(password)),
        )
        db.commit()
        flash("Account created. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.", "error")
            return render_template("login.html")
        session["user_id"] = user["id"]
        flash(f"Welcome back, {user['name']}.", "success")
        next_url = request.args.get("next") or (url_for("admin_dashboard") if user["role"] == "admin" else url_for("home"))
        return redirect(next_url)
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("home"))


@app.route("/api/orders/<int:order_id>/status")
@login_required
def order_status_api(order_id):
    db = get_db()
    user = current_user()
    order = db.execute(
        "SELECT id, user_id, status, payment_status, updated_at, total FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    if order is None:
        abort(404)
    if order["user_id"] != user["id"] and user["role"] != "admin":
        abort(403)
    return jsonify({
        "id": order["id"],
        "status": order["status"],
        "payment_status": order["payment_status"],
        "updated_at": order["updated_at"],
        "total": order["total"],
        "status_label": STATUS_LABELS.get(order["status"], order["status"]),
        "payment_label": "Payment Received" if order["payment_status"] == "received" else "Payment Pending",
    })



# =============================================================================
# Table booking
# =============================================================================
@app.route("/table-booking", methods=["GET", "POST"])
@login_required
def table_booking():
    if request.method == "POST":
        name = request.form.get("customer_name", "").strip()
        phone = request.form.get("phone", "").strip()
        booking_date = request.form.get("booking_date", "").strip()
        booking_time = request.form.get("booking_time", "").strip()
        try:
            guests = int(request.form.get("guests", "2"))
        except ValueError:
            guests = 0
        notes = request.form.get("notes", "").strip()
        if not name or not phone or not booking_date or not booking_time or guests < 1:
            flash("Please fill all required booking details.", "error")
            return redirect(url_for("table_booking"))
        if guests > 50:
            flash("For more than 50 guests, please call the restaurant.", "error")
            return redirect(url_for("table_booking"))
        db = get_db()
        user = current_user()
        cur = db.execute("""INSERT INTO table_bookings
            (user_id, customer_name, phone, booking_date, booking_time, guests, notes)
            VALUES (?,?,?,?,?,?,?)""",
            (user["id"], name, phone, booking_date, booking_time, guests, notes))
        booking_id = cur.lastrowid
        db.commit()
        flash(f"Table booking #{booking_id} received. You can track it anytime from My Orders.", "success")
        return redirect(url_for("my_orders"))
    return render_template("table_booking.html", site_settings=get_site_settings(get_db()))

@app.route("/table-booking/<int:booking_id>")
@login_required
def table_booking_detail(booking_id):
    db = get_db()
    user = current_user()
    booking = db.execute("SELECT * FROM table_bookings WHERE id = ?", (booking_id,)).fetchone()
    if booking is None:
        abort(404)
    if booking["user_id"] != user["id"] and user["role"] != "admin":
        abort(403)
    return render_template("table_booking_detail.html", booking=booking)

@app.route("/admin/table-bookings")
@admin_required
def admin_table_bookings():
    db = get_db()
    bookings = db.execute("SELECT * FROM table_bookings ORDER BY booking_date, booking_time, created_at DESC").fetchall()
    return render_template("admin/table_bookings.html", bookings=bookings, site_settings=get_site_settings(db))

@app.route("/admin/table-bookings/<int:booking_id>/status", methods=["POST"])
@admin_required
def admin_table_booking_status(booking_id):
    status = request.form.get("status", "confirmed")
    if status not in {"pending", "confirmed", "cancelled", "completed"}:
        abort(400)
    db = get_db()
    db.execute("UPDATE table_bookings SET status=? WHERE id=?", (status, booking_id))
    db.commit()
    flash("Table booking status updated.", "success")
    return redirect(url_for("admin_table_bookings"))

# =============================================================================
# Admin table-booking live notification API
# =============================================================================
@app.route("/admin/api/table-bookings/latest")
@admin_required
def admin_table_booking_latest():
    db = get_db()
    latest = db.execute("SELECT COALESCE(MAX(id), 0) AS id FROM table_bookings").fetchone()["id"]
    return jsonify({"latest_id": latest})


@app.route("/admin/api/table-bookings/changes")
@admin_required
def admin_table_booking_changes():
    try:
        since_id = int(request.args.get("since_id", "0"))
    except ValueError:
        since_id = 0
    db = get_db()
    rows = db.execute(
        """SELECT id, customer_name, phone, booking_date, booking_time, guests, notes, status, created_at
           FROM table_bookings WHERE id > ? ORDER BY id ASC""",
        (since_id,)
    ).fetchall()
    latest = db.execute("SELECT COALESCE(MAX(id),0) AS id FROM table_bookings").fetchone()["id"]
    return jsonify({
        "bookings": [dict(r) for r in rows],
        "latest_id": latest
    })

# =============================================================================
# Admin routes
# =============================================================================
@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    orders_today = db.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(total),0) t FROM orders WHERE date(created_at) = date('now')"
    ).fetchone()
    pending_count = db.execute(
        "SELECT COUNT(*) c FROM orders WHERE status IN ('pending','confirmed','preparing','out_for_delivery')"
    ).fetchone()["c"]
    total_items = db.execute("SELECT COUNT(*) c FROM menu_items").fetchone()["c"]
    recent_orders = db.execute(
        "SELECT o.*, u.name as customer_name FROM orders o JOIN users u ON u.id = o.user_id "
        "ORDER BY o.created_at DESC LIMIT 8"
    ).fetchall()
    latest_booking = db.execute(
        "SELECT COALESCE(MAX(id), 0) AS id FROM table_bookings"
    ).fetchone()["id"]
    table_bookings_count = db.execute(
        "SELECT COUNT(*) AS c FROM table_bookings WHERE status != 'cancelled'"
    ).fetchone()["c"]
    return render_template(
        "admin/dashboard.html",
        orders_today_count=orders_today["c"], orders_today_revenue=orders_today["t"],
        pending_count=pending_count, total_items=total_items, recent_orders=recent_orders,
        latest_booking_id=latest_booking,
        table_bookings_count=table_bookings_count,
    )



@app.route("/admin/api/orders/changes")
@admin_required
def admin_order_changes():
    """Return orders created after the supplied order id.

    The admin browser polls this endpoint every few seconds so a new order
    appears without pressing Refresh. The page itself also refreshes its
    visible data every 60 seconds.
    """
    since_id = request.args.get("since_id", "0")
    try:
        since_id = int(since_id)
    except (TypeError, ValueError):
        since_id = 0

    db = get_db()
    rows = db.execute(
        """SELECT o.id, u.name AS customer_name, o.phone, o.total, o.status,
                  o.payment_status, o.order_type, o.created_at
           FROM orders o
           JOIN users u ON u.id = o.user_id
           WHERE o.id > ?
           ORDER BY o.id ASC""",
        (since_id,),
    ).fetchall()

    return {
        "orders": [dict(r) for r in rows],
        "latest_id": max([r["id"] for r in rows], default=since_id),
    }

@app.route("/admin/front-page", methods=["GET", "POST"])
@admin_required
def admin_front_page():
    db = get_db()
    if request.method == "POST":
        image = request.files.get("image")
        if not image or not image.filename:
            flash("Please select a front-page image.", "error")
            return redirect(url_for("admin_front_page"))
        if not allowed_image(image.filename):
            flash("Invalid image. Use PNG, JPG, JPEG, WEBP, or GIF.", "error")
            return redirect(url_for("admin_front_page"))

        ext = secure_filename(image.filename).rsplit(".", 1)[1].lower()
        filename = f"hero_{uuid.uuid4().hex}.{ext}"
        upload_dir = os.path.join(BASE_DIR, "static", "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        image.save(os.path.join(upload_dir, filename))

        title = request.form.get("title", "Group of Tushar Restaurant").strip()
        subtitle = request.form.get("subtitle", "Near Mahindra SEZ, Jaipur â€¢ Delivery within 4 km").strip()
        start_at = request.form.get("start_at", "").strip() or None
        end_at = request.form.get("end_at", "").strip() or None
        activate = request.form.get("activate") == "on"

        # A future scheduled front should NOT remove today's live front.
        # It automatically takes over when its start time arrives.
        from datetime import datetime as _dt
        is_future = False
        if start_at:
            try:
                is_future = _dt.fromisoformat(start_at) > _dt.now()
            except ValueError:
                pass

        make_live_now = activate and not is_future
        if make_live_now:
            db.execute("UPDATE hero_slides SET is_active = 0")

        db.execute(
            "INSERT INTO hero_slides (image_path,title,subtitle,start_at,end_at,is_active) VALUES (?,?,?,?,?,?)",
            (f"uploads/{filename}", title, subtitle, start_at, end_at, 1 if make_live_now else 0),
        )
        db.commit()
        flash("Front page saved successfully.", "success")
        return redirect(url_for("admin_front_page"))

    slides = db.execute("SELECT * FROM hero_slides ORDER BY id DESC").fetchall()
    return render_template("admin/front_page.html", slides=slides)


@app.route("/admin/front-page/activate/<int:slide_id>", methods=["POST"])
@admin_required
def admin_front_page_activate(slide_id):
    db = get_db()
    slide = db.execute("SELECT * FROM hero_slides WHERE id = ?", (slide_id,)).fetchone()
    if slide is None:
        abort(404)
    db.execute("UPDATE hero_slides SET is_active = 0")
    db.execute("UPDATE hero_slides SET is_active = 1 WHERE id = ?", (slide_id,))
    db.commit()
    flash("Selected front page is now LIVE.", "success")
    return redirect(url_for("admin_front_page"))


@app.route("/admin/front-page/delete/<int:slide_id>", methods=["POST"])
@admin_required
def admin_front_page_delete(slide_id):
    db = get_db()
    slide = db.execute("SELECT * FROM hero_slides WHERE id = ?", (slide_id,)).fetchone()
    if slide is None:
        abort(404)
    db.execute("DELETE FROM hero_slides WHERE id = ?", (slide_id,))
    db.commit()
    if slide["image_path"] != "uploads/tushar_front.png":
        full = os.path.join(BASE_DIR, "static", slide["image_path"].replace("/", os.sep))
        if os.path.isfile(full):
            try:
                os.remove(full)
            except OSError:
                pass
    flash("Front page image deleted.", "success")
    return redirect(url_for("admin_front_page"))


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    db = get_db()
    if request.method == "POST":
        values = {
            "restaurant_name": request.form.get("restaurant_name", "").strip(),
            "location": request.form.get("location", "").strip(),
            "delivery_radius_km": request.form.get("delivery_radius_km", "4").strip(),
            "upi_id": request.form.get("upi_id", "").strip(),
            "whatsapp_number": request.form.get("whatsapp_number", "").strip(),
            "restaurant_phone": request.form.get("restaurant_phone", "").strip(),
            "restaurant_open": "1" if request.form.get("restaurant_open") == "1" else "0",
            "cod_enabled": "1" if request.form.get("cod_enabled") == "1" else "0",
            "whatsapp_auto_enabled": "1" if request.form.get("whatsapp_auto_enabled") == "1" else "0",
            "whatsapp_api_token": request.form.get("whatsapp_api_token", "").strip(),
            "whatsapp_phone_number_id": request.form.get("whatsapp_phone_number_id", "").strip(),
        }
        try:
            radius = float(values["delivery_radius_km"])
            if radius <= 0 or radius > 100:
                raise ValueError
        except ValueError:
            flash("Delivery radius must be a positive number.", "error")
            return redirect(url_for("admin_settings"))

        for key, value in values.items():
            db.execute(
                "INSERT INTO site_settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

        qr = request.files.get("upi_qr")
        if qr and qr.filename:
            if not allowed_image(qr.filename):
                flash("UPI QR must be JPG, JPEG, PNG, WEBP or GIF.", "error")
                db.rollback()
                return redirect(url_for("admin_settings"))
            ext = secure_filename(qr.filename).rsplit(".", 1)[1].lower()
            qr_dir = os.path.join(BASE_DIR, "static", "uploads", "upi_qr")
            os.makedirs(qr_dir, exist_ok=True)
            filename = "upi_qr." + ext
            qr.save(os.path.join(qr_dir, filename))
            qr_path = "uploads/upi_qr/" + filename
            db.execute(
                "INSERT INTO site_settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("upi_qr_path", qr_path),
            )

        db.commit()
        flash("Restaurant settings updated.", "success")
        return redirect(url_for("admin_settings"))
    return render_template("admin/settings.html", settings=get_site_settings(db))


@app.route("/admin/change-password", methods=["GET", "POST"])
@admin_required
def admin_change_password():
    db = get_db()
    user = current_user()
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not check_password_hash(user["password_hash"], current_password):
            flash("Current password is incorrect.", "error")
            return redirect(url_for("admin_change_password"))
        if len(new_password) < 6:
            flash("New password must be at least 6 characters.", "error")
            return redirect(url_for("admin_change_password"))
        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "error")
            return redirect(url_for("admin_change_password"))
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_password), user["id"]))
        db.commit()
        flash("Admin password changed successfully.", "success")
        return redirect(url_for("admin_settings"))
    return render_template("admin/change_password.html")


@app.route("/admin/menu")
@admin_required
def admin_menu():
    db = get_db()
    categories = db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()
    items = db.execute(
        "SELECT m.*, c.name as category_name FROM menu_items m JOIN categories c ON c.id = m.category_id "
        "ORDER BY c.sort_order, m.name"
    ).fetchall()
    return render_template("admin/menu.html", categories=categories, items=items)


@app.route("/admin/categories", methods=["GET", "POST"])
@admin_required
def admin_categories():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Category name is required.", "error")
        else:
            try:
                db.execute("INSERT INTO categories (name, sort_order) VALUES (?, ?)", (name, 0))
                db.commit()
                flash(f"Category '{name}' added.", "success")
            except psycopg2.IntegrityError:
                db.rollback()
                flash("That category already exists.", "error")
        return redirect(url_for("admin_categories"))
    categories = db.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    return render_template("admin/categories.html", categories=categories)

@app.route("/admin/categories/delete/<int:category_id>", methods=["POST"])
@admin_required
def admin_category_delete(category_id):
    db = get_db()
    count = db.execute("SELECT COUNT(*) AS c FROM menu_items WHERE category_id = ?", (category_id,)).fetchone()["c"]
    if count:
        flash("This category has menu items. Move/delete those items before removing the category.", "error")
    else:
        db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        db.commit()
        flash("Category removed.", "success")
    return redirect(url_for("admin_categories"))

@app.route("/admin/menu/add", methods=["GET", "POST"])
@admin_required
def admin_menu_add():
    db = get_db()
    categories = db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        price = request.form.get("price", type=float)
        category_id = request.form.get("category_id", type=int)
        is_veg = 1 if request.form.get("is_veg") == "on" else 0
        emoji = request.form.get("emoji", "ðŸ½ï¸").strip() or "ðŸ½ï¸"
        image_path = save_menu_image(request.files.get("image"))
        if request.files.get("image") and not image_path:
            flash("Invalid image. Use PNG, JPG, JPEG, WEBP, or GIF.", "error")
            return render_template("admin/menu_form.html", categories=categories, item=None)

        if not name or price is None or price <= 0 or not category_id:
            flash("Name, a valid price, and category are required.", "error")
            return render_template("admin/menu_form.html", categories=categories, item=None)

        db.execute(
            "INSERT INTO menu_items (category_id, name, description, price, is_veg, emoji, image_path) VALUES (?,?,?,?,?,?,?)",
            (category_id, name, description, price, is_veg, emoji, image_path),
        )
        db.commit()
        flash(f"Added {name} to the menu.", "success")
        return redirect(url_for("admin_menu"))
    return render_template("admin/menu_form.html", categories=categories, item=None)


@app.route("/admin/menu/edit/<int:item_id>", methods=["GET", "POST"])
@admin_required
def admin_menu_edit(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    categories = db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        price = request.form.get("price", type=float)
        category_id = request.form.get("category_id", type=int)
        is_veg = 1 if request.form.get("is_veg") == "on" else 0
        is_available = 1 if request.form.get("is_available") == "on" else 0
        emoji = request.form.get("emoji", "ðŸ½ï¸").strip() or "ðŸ½ï¸"
        new_image_path = save_menu_image(request.files.get("image"))
        if request.files.get("image") and not new_image_path:
            flash("Invalid image. Use PNG, JPG, JPEG, WEBP, or GIF.", "error")
            return render_template("admin/menu_form.html", categories=categories, item=item)

        if not name or price is None or price <= 0 or not category_id:
            flash("Name, a valid price, and category are required.", "error")
            return render_template("admin/menu_form.html", categories=categories, item=item)

        image_path = new_image_path or item["image_path"]
        db.execute(
            "UPDATE menu_items SET name=?, description=?, price=?, category_id=?, is_veg=?, is_available=?, emoji=?, image_path=? WHERE id=?",
            (name, description, price, category_id, is_veg, is_available, emoji, image_path, item_id),
        )
        if new_image_path and item["image_path"]:
            delete_menu_image(item["image_path"])
        db.commit()
        flash(f"Updated {name}.", "success")
        return redirect(url_for("admin_menu"))
    return render_template("admin/menu_form.html", categories=categories, item=item)


@app.route("/admin/menu/delete/<int:item_id>", methods=["POST"])
@admin_required
def admin_menu_delete(item_id):
    db = get_db()
    db.execute("UPDATE menu_items SET is_available = 0 WHERE id = ?", (item_id,))
    db.commit()
    flash("Item removed from the live menu.", "success")
    return redirect(url_for("admin_menu"))


@app.route("/admin/orders")
@admin_required
def admin_orders():
    status_filter = request.args.get("status", "")
    db = get_db()
    if status_filter:
        orders = db.execute(
            "SELECT o.*, u.name as customer_name FROM orders o JOIN users u ON u.id = o.user_id "
            "WHERE o.status = ? ORDER BY o.created_at DESC",
            (status_filter,),
        ).fetchall()
    else:
        orders = db.execute(
            "SELECT o.*, u.name as customer_name FROM orders o JOIN users u ON u.id = o.user_id "
            "ORDER BY o.created_at DESC"
        ).fetchall()
    return render_template("admin/orders.html", orders=orders, statuses=ORDER_STATUSES, status_filter=status_filter, PAYMENT_LABELS=PAYMENT_LABELS)


@app.route("/admin/orders/<int:order_id>")
@admin_required
def admin_order_detail(order_id):
    db = get_db()
    order = db.execute(
        "SELECT o.*, u.name as customer_name, u.email as customer_email FROM orders o "
        "JOIN users u ON u.id = o.user_id WHERE o.id = ?",
        (order_id,),
    ).fetchone()
    if order is None:
        abort(404)
    items = db.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    item_lines = "\n".join(
        f"{item['quantity']}Ã— {item['item_name']} â€” â‚¹{item['unit_price'] * item['quantity']:.0f}"
        for item in items
    )
    wa_message = (
        f"Dear {order['customer_name']},\n\n"
        f"Thank you for your order with Group of Tushar Restaurant. ðŸ™\n\n"
        f"ðŸ§¾ Order No.: {order_id}\n\n"
        f"ðŸ½ï¸ Your Order:\n{item_lines}\n\n"
        f"ðŸ’° Total Amount: â‚¹{order['total']:.2f}\n"
        f"ðŸ’³ Payment: Received Successfully âœ…\n"
        f"ðŸ“¦ Order: Confirmed âœ…\n\n"
        f"Your order is now being prepared. We will keep you updated about your order status.\n\n"
        f"Thank you for choosing Group of Tushar Restaurant. â¤ï¸\n\n"
        f"Group of Tushar Restaurant\n"
        f"Salute to everyone who fights for our country.\n"
        f"Jai Hind!\n"
        f"I Love My India â¤ï¸"
    )
    customer_wa_link = whatsapp_link(order["phone"], wa_message)
    status_messages = build_whatsapp_messages(order, items, get_site_settings(db))
    status_wa_links = {
        key: whatsapp_link(order["phone"], value)
        for key, value in status_messages.items()
    }
    return render_template("admin/order_detail.html", order=order, items=items, statuses=ORDER_STATUSES, PAYMENT_LABELS=PAYMENT_LABELS, customer_wa_link=customer_wa_link, status_wa_links=status_wa_links, site_settings=get_site_settings(db))


@app.route("/admin/orders/<int:order_id>/payment-received", methods=["POST"])
@admin_required
def admin_payment_received(order_id):
    db = get_db()
    order = db.execute("SELECT id, payment_status, status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        abort(404)
    if order["status"] == "cancelled":
        flash(f"Order #{order_id} is cancelled; payment cannot confirm the order.", "error")
        return redirect(url_for("admin_order_detail", order_id=order_id))
    db.execute(
        "UPDATE orders SET payment_status = 'received', status = 'confirmed', updated_at = datetime('now') WHERE id = ?",
        (order_id,),
    )
    db.commit()

    # Best-effort automatic WhatsApp send when official Cloud API is configured.
    settings = get_site_settings(db)
    full_order = db.execute(
        "SELECT o.*, u.name as customer_name FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id = ?",
        (order_id,),
    ).fetchone()
    items = db.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    if full_order and str(settings.get("whatsapp_auto_enabled", "0")) == "1":
        messages = build_whatsapp_messages(full_order, items, settings)
        send_whatsapp_cloud_text(full_order["phone"], messages["confirmed"], settings)

    flash(f"Payment received for Order #{order_id}. Order confirmed.", "success")
    return redirect(url_for("admin_order_detail", order_id=order_id, print="1"))


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
@admin_required
def admin_order_status(order_id):
    new_status = request.form.get("status")
    if new_status not in ORDER_STATUSES:
        abort(400)
    db = get_db()
    db.execute(
        "UPDATE orders SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (new_status, order_id),
    )
    db.commit()
    flash(f"Order #{order_id} marked as {STATUS_LABELS[new_status]}.", "success")
    return redirect(url_for("admin_order_detail", order_id=order_id))


if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
else:
    # also init when imported (e.g. by a WSGI server or test harness)
    with app.app_context():
        init_db()

