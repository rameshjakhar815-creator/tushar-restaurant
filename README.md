# Group of Tushar Restaurant — Restaurant
The first-run database seeds an admin account internally; credentials are not displayed on the login page or in this README. Use Admin → Change Password after signing in.
```

**Change that password (or the account) before this is ever reachable by
real people** — it's a seed/demo credential, documented in plain text
right here.

## What I actually tested (not just wrote — ran)

Unlike some earlier parts of our session (Rust/C++, where I didn't have
a compiler), **this one I could fully run and test**, since Flask, SQLite,
and Python are all available here. I started the real server and drove it
with real HTTP requests end-to-end:

- **Customer flow**: register → login → browse menu → add to cart →
  checkout (delivery, with address/phone/notes) → order created in the
  database → cart correctly emptied → order appears in "My Orders" →
  tracking page renders.
- **Admin flow**: admin login → dashboard stats load → add a menu item →
  it appears on the live public menu → edit it → soft-delete it → it
  disappears from the public menu → walk an order through every status
  (confirmed → preparing → out for delivery → delivered) → customer sees
  the updated status and a fully-completed tracker.
- **Security boundaries**: anonymous users bounced from checkout/orders/
  admin routes to login; a logged-in *customer* is blocked from all
  `/admin/*` routes; a customer **cannot** view another customer's order
  (403) — verified this isn't just a UI hide, the route itself refuses it.
- **Static assets**: CSS and JS actually serve correctly with the expected
  content.

All of that passed with zero server errors in the log (checked the whole
run for tracebacks — none). I can't promise there are no bugs at all, but
this isn't a guess — it's a build that ran and did the things it's
supposed to do, on real requests.

## Design

Built to look like a specific restaurant, not a generic template:
- **Palette**: espresso-dark background, turmeric/saffron gold accent,
  chili red for actions and non-veg markers, curry-green for veg markers
  — pulled from actual spice-market materials rather than a generic
  cream+terracotta "restaurant site" look.
- **Type**: Fraunces (a warm, characterful serif) for headings, Work Sans
  for body text, JetBrains Mono for prices and order numbers (a
  receipt-like feel).
- **Signature element**: the order-tracking page uses a "tandoor
  timeline" — stages of the order shown as a skewer of dots with cooking-
  themed icons (📝 → ✔ → 🔥 → 🛵 → 🍽️) instead of a generic progress bar.
- Fully responsive (mobile nav collapses, cart/checkout grids stack,
  admin sidebar becomes a horizontal scroller).

No stock photos are used — dish "images" are large emoji + a veg/non-veg
indicator dot, matching common Indian-restaurant menu conventions. Swap
in real photos later by adding an `image_url` column and an `<img>` tag
in the templates wherever you see `{{ item.emoji }}`.

## Project structure

```
tusar_restaurant/
  app.py              — all routes, auth, DB access (single file, ~450 lines)
  schema.sql           — SQLite schema (users, categories, menu_items, orders, order_items)
  requirements.txt
  static/
    css/style.css       — the whole design system
    js/main.js          — small progressive-enhancement touches only
  templates/
    base.html, index.html, menu.html, cart.html, checkout.html,
    order_detail.html, my_orders.html, login.html, register.html
    admin/
      base_admin.html, dashboard.html, menu.html, menu_form.html,
      orders.html, order_detail.html
```

## How the pieces work

- **Auth**: Flask session + `werkzeug.security` password hashing (proper
  salted hashes, not plaintext). One `users` table with a `role` column
  (`customer` / `admin`) — same login form for both, redirected based on
  role.
- **Cart**: kept in the session as `{item_id: qty}` — no DB writes until
  checkout, so browsing/adding never touches the database.
- **Orders**: `orders` + `order_items` tables. Order line items **snapshot**
  the item name and price at order time, so editing a menu item's price
  later doesn't rewrite history on past orders — this is deliberate and
  matches how real point-of-sale/ordering systems work.
- **Admin menu delete** is a soft-delete (`is_available = 0`), not a real
  row delete — so it never breaks a foreign-key reference from an order
  that already includes that item.
- **Tax**: one `TAX_RATE` constant at the top of `app.py` — change it
  there.

## Known gaps / next steps (be aware before going live)

- No payment gateway integration — checkout creates the order but doesn't
  charge a card. Wire in Razorpay/Stripe/etc. if you need real payments.
- No email/SMS notifications on order status change.
- No image upload for menu items (emoji only, by design — see Design
  section above for how to add real photos).
- `app.secret_key` has a dev default — **set `TUSAR_SECRET_KEY`** as a
  real environment variable before deploying anywhere public.
- Flask's built-in dev server (`app.run(debug=True)`) is for local testing
  only — use a real WSGI server (gunicorn, waitress, etc.) behind a
  reverse proxy for production.
- No rate-limiting on login/register — add if this goes public.


## Group of Tushar Restaurant customizations

- Brand: **Group of Tushar Restaurant**
- Location text: **Near Mahindra SEZ, Jaipur • Asiniya Umang**
- Delivery policy: **within 4 km**
- **Cash on Delivery is disabled**. Checkout accepts UPI / PhonePe only.
- Admin → **Front Page** lets you upload, activate and schedule the main front image (for example, a 15 August front).
- Admin → **Settings** lets you change restaurant name/location, delivery radius, UPI ID and WhatsApp number.
- The supplied Group of Tushar front artwork is included in `static/uploads/tushar_front.png`.
- Other supplied reference/logo images are also included in `static/uploads/`.
- The UPI button becomes a direct UPI/PhonePe intent link after the real UPI ID is entered in Admin → Settings.
- WhatsApp support uses a click-to-chat link. Fully automatic WhatsApp push notifications require a WhatsApp Business/Cloud API account and credentials; this package does not pretend to send automatic messages without those credentials.

### Run

```bat
cd /d C:
estaurant	usar	usar
py app.py
```

Open `http://127.0.0.1:5000/`.

Admin login uses the admin account already present in your database. Do not delete your existing database.

## Payment confirmation flow

- Cash on Delivery remains disabled; checkout uses UPI / PhonePe.
- New orders start as **Payment pending**.
- Customer sees **⏳ Waiting for payment confirmation** until the restaurant confirms payment.
- Admin → Orders → Manage has **✅ Payment Received / Done**.
- Clicking it marks payment as received and automatically changes the order to **Confirmed**.
- After confirmation, Admin can use **💬 Send WhatsApp confirmation** to open the customer's WhatsApp message with the approved order details and closing message.
- Existing databases are migrated automatically with a `payment_status` column; no manual database reset is required.


## Enhanced features
- Admin new-order detection every 2 seconds with popup, strong alarm and voice announcement.
- Admin dashboard visible data refresh every 20 seconds.
- Customer order page polls payment/order status automatically and shows Payment Successful without manual refresh.
- UPI QR upload/change in Admin Settings.
- Restaurant OPEN/CLOSED control in Admin Settings.
- Restaurant phone is clickable on customer pages.
- Optional official WhatsApp Cloud API settings for automatic sending; wa.me links remain available without API credentials.


### WhatsApp automatic sending
The project includes optional official WhatsApp Cloud API fields in Admin → Settings.
For truly automatic sending, turn it ON and enter the official Phone Number ID and access token.
Without API credentials, the customer/status WhatsApp links still work without saving the contact.

### Payment status live update
The customer's order page checks the server every 3 seconds. When the admin presses
Payment Received / Done, the customer's page changes to **Payment Successful** automatically.


## Latest live order + table booking update
- Customer order tracking polls payment/status every 2 seconds and updates without manual refresh.
- Added public Table Booking for 2, 3, 4, 5 and larger party sizes.
- Added Admin → Table Bookings management.
