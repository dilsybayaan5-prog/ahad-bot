"""AHAD WhatsApp Sales Bot - official WhatsApp Cloud API + Claude (Roman Urdu)."""
import os, re, json, time, hmac, hashlib, sqlite3, threading
from datetime import datetime
import requests
from flask import Flask, request, abort
from apscheduler.schedulers.background import BackgroundScheduler

E = os.environ.get
TOKEN, PHONE_ID = E("WHATSAPP_TOKEN", ""), E("PHONE_NUMBER_ID", "")
VERIFY, APP_SECRET = E("VERIFY_TOKEN", "ahad-verify"), E("META_APP_SECRET", "")
OWNER = re.sub(r"\D", "", E("OWNER_NUMBER", ""))
AKEY, MODEL = E("ANTHROPIC_API_KEY", ""), E("CLAUDE_MODEL", "claude-haiku-5-5")
GKEY, GMODEL = E("GEMINI_API_KEY", ""), E("GEMINI_MODEL", "gemini-2.5-flash")
SHOP, PAY = E("SHOP_NAME", "AHAD Digital Tools"), E("PAYMENT_DETAILS", "Payment ki details owner se le lein.")
TEMPLATE, TEMPLATE_LANG, DB = E("REMINDER_TEMPLATE", ""), E("TEMPLATE_LANG", "en"), E("DB_PATH", "bot.db")
GRAPH = "https://graph.facebook.com/v21.0"
HDR = {"Authorization": f"Bearer {TOKEN}"}
app = Flask(__name__)


# ---------- database ----------
def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(phone TEXT PRIMARY KEY, name TEXT, state TEXT DEFAULT 'idle', pending INTEGER, last_in REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY, phone TEXT, role TEXT, text TEXT);
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, phone TEXT, product TEXT, price TEXT, days INTEGER, status TEXT DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS subs(id INTEGER PRIMARY KEY, phone TEXT, product TEXT, expires REAL, r3 INTEGER DEFAULT 0, r1 INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY);
        """)


def get_user(phone, name):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(phone,name) VALUES(?,?)", (phone, name))
        c.execute("UPDATE users SET last_in=? WHERE phone=?", (time.time(), phone))
        return c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()


def set_state(phone, state, pending=None):
    with db() as c:
        c.execute("UPDATE users SET state=?, pending=? WHERE phone=?", (state, pending, phone))


def log(phone, role, text):
    with db() as c:
        c.execute("INSERT INTO msgs(phone,role,text) VALUES(?,?,?)", (phone, role, text))


# ---------- catalog (ALL prices/facts come from catalog.json only) ----------
def catalog():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.json"), encoding="utf-8") as f:
        return json.load(f)


def product_lines():
    out = []
    for i, p in enumerate(catalog()["products"], 1):
        line = f"{i}. {p['name']} - {p['price']} ({p['duration']})"
        if p.get("details"):
            line += f"\n   {p['details']}"
        out.append(line)
    return "\n".join(out)


def fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%d-%m-%Y")


# ---------- WhatsApp sending ----------
def post(payload):
    r = requests.post(f"{GRAPH}/{PHONE_ID}/messages", headers=HDR,
                      json={"messaging_product": "whatsapp", **payload}, timeout=30)
    if r.status_code >= 400:
        print("SEND ERROR", r.status_code, r.text[:300])
    return r


def send(to, text):
    r = post({"to": to, "type": "text", "text": {"body": text[:4000]}})
    log(to, "bot", text)
    return r.status_code < 400


def send_buttons(to, body, buttons):
    """Reply buttons (max 3). Button labels are English."""
    r = post({"to": to, "type": "interactive", "interactive": {"type": "button", "body": {"text": body[:1000]},
              "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in buttons[:3]]}}})
    log(to, "bot", body)
    return r.status_code < 400


def send_list(to, body, button, rows):
    """Pick-one list (max 10 rows). Labels are English / product names."""
    r = post({"to": to, "type": "interactive", "interactive": {"type": "list", "body": {"text": body[:1000]},
              "action": {"button": button[:20], "sections": [{"title": "Tools", "rows": [
                  {"id": i, "title": t[:24], "description": d[:72]} for i, t, d in rows[:10]]}]}}})
    log(to, "bot", body)
    return r.status_code < 400


def in_window(to):
    with db() as c:
        r = c.execute("SELECT last_in FROM users WHERE phone=?", (to,)).fetchone()
    return bool(r) and time.time() - r["last_in"] < 23 * 3600


def send_smart(to, text):
    """Free text inside WhatsApp's 24h window, approved template outside it. Returns True if sent."""
    if in_window(to) or not TEMPLATE:
        return send(to, text)
    r = post({"to": to, "type": "template", "template": {"name": TEMPLATE, "language": {"code": TEMPLATE_LANG},
              "components": [{"type": "body", "parameters": [{"type": "text", "text": text.replace("\n", " | ")[:900]}]}]}})
    return r.status_code < 400


def notify_owner(text):
    if OWNER:
        send_smart(OWNER, text)


def forward_image(media_id, caption):
    info = requests.get(f"{GRAPH}/{media_id}", headers=HDR, timeout=30).json()
    blob = requests.get(info["url"], headers=HDR, timeout=60)
    mime = info.get("mime_type", "image/jpeg")
    up = requests.post(f"{GRAPH}/{PHONE_ID}/media", headers=HDR, data={"messaging_product": "whatsapp", "type": mime},
                       files={"file": ("payment.jpg", blob.content, mime)}, timeout=60).json()
    post({"to": OWNER, "type": "image", "image": {"id": up["id"], "caption": caption[:1000]}})


# ---------- AI (Gemini free tier or Claude) - Roman Urdu, only from catalog ----------
def build_ctx(phone, state):
    with db() as c:
        rows = c.execute("SELECT role,text FROM msgs WHERE phone=? ORDER BY id DESC LIMIT 10", (phone,)).fetchall()[::-1]
    msgs = []
    for r in rows:
        role = "user" if r["role"] == "user" else "assistant"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + r["text"]
        else:
            msgs.append({"role": role, "content": r["text"]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    cat = catalog()
    system = (
        f"Tum '{SHOP}' ke WhatsApp sales assistant ho. Yeh digital tools ki subscriptions bechte hain.\n"
        "Rules:\n- Hamesha sirf Roman Urdu mein jawab do (Urdu alfaaz English huroof mein). Chhote, saaf, dostana jawab (3-5 lines).\n"
        "- Sirf neeche diye gaye CATALOG, POLICIES aur FAQ se baat karo. Qeemat, muddat, features, discount ya availability apni taraf se kabhi mat banao.\n"
        "- Agar jawab in mein nahi hai to saaf kaho: 'Ye main owner se confirm kar ke batata hoon.' Andaza mat lagao.\n"
        "- Order ke liye customer ko kaho ke 'Place Order' dabayein ya 'order' likhein. Payment ya account details khud mat do.\n- Koi jhoota wada mat karo.\n"
        f"CATALOG:\n{product_lines()}\nPOLICIES:\n{cat.get('policies', '')}\n"
        f"FAQ:\n" + "\n".join(f"- {f['keywords']}: {f['answer']}" for f in cat.get("faq", [])) + "\n"
    )
    if state == "awaiting_payment":
        system += "Customer ne order kiya hai aur payment ke screenshot ka intezar hai; unhein screenshot bhejne ko kaho.\n"
    return system, msgs


def ai_reply(phone, state):
    """Returns the AI answer, or None if the AI is unavailable (then FAQ mode takes over)."""
    system, msgs = build_ctx(phone, state)
    try:
        if GKEY:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{GMODEL}:generateContent"
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in msgs],
                    "generationConfig": {"maxOutputTokens": 600, "temperature": 0.3, "thinkingConfig": {"thinkingBudget": 0}}}
            hdr = {"x-goog-api-key": GKEY, "content-type": "application/json"}
            r = requests.post(url, headers=hdr, json=body, timeout=60)
            if r.status_code >= 400:  # some models do not allow thinkingBudget=0: retry plainly
                body["generationConfig"].pop("thinkingConfig")
                r = requests.post(url, headers=hdr, json=body, timeout=60)
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                          headers={"x-api-key": AKEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": MODEL, "max_tokens": 400, "system": system, "messages": msgs})
        return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print("AI ERROR", repr(e))
        return None


def faq_reply(text):
    """Free mode: answer only from catalog.json (products + faq)."""
    low, cat = text.lower(), catalog()
    for p in cat["products"]:
        if p["name"].lower() in low:
            return f"{p['name']}: {p['price']} ({p['duration']}). {p.get('details', '')}\nOrder ke liye 'Place Order' dabayein."
    best, score = None, 0
    for f in cat.get("faq", []):
        n = sum(1 for k in f["keywords"] if k.lower() in low)
        if n > score:
            best, score = f, n
    return best["answer"] if best else None


# ---------- conversation logic ----------
def menu(phone):
    set_state(phone, "idle")
    send_buttons(phone, f"Assalam o Alaikum! {SHOP} mein khush amdeed. Neeche se option chunein, ya seedha apna sawal likh dein.",
                 [("menu_prices", "Tools & Prices"), ("menu_order", "Place Order"), ("menu_subs", "My Subscription")])


def show_prices(phone):
    send(phone, "Hamare tools:\n\n" + product_lines())
    send_buttons(phone, "Order karna chahte hain?", [("menu_order", "Place Order"), ("menu_main", "Main Menu")])


def start_order(phone):
    set_state(phone, "choosing")
    rows = [(f"prod:{i}", p["name"], f"{p['price']} - {p['duration']}") for i, p in enumerate(catalog()["products"], 1)]
    send_list(phone, "Konsa tool lena hai? Neeche list kholein aur chunein.", "Choose Tool", rows)


def cancel(phone):
    with db() as c:
        c.execute("UPDATE orders SET status='cancelled' WHERE phone=? AND status='pending'", (phone,))
    set_state(phone, "idle")
    send_buttons(phone, "Theek hai, cancel kar diya.", [("menu_main", "Main Menu")])


def create_order(phone, pick):
    with db() as c:
        cur = c.execute("INSERT INTO orders(phone,product,price,days) VALUES(?,?,?,?)",
                        (phone, pick["name"], pick["price"], int(pick.get("days", 30))))
        oid = cur.lastrowid
    set_state(phone, "awaiting_payment", oid)
    send(phone, f"Order #{oid}\n{pick['name']} - {pick['price']} ({pick['duration']})\n\nPayment yahan karein:\n{PAY}")
    send_buttons(phone, "Payment karne ke baad uska screenshot yahin bhej dein.", [("cancel", "Cancel Order")])
    notify_owner(f"Naya order #{oid}: {pick['name']} {pick['price']}\nCustomer: {phone}\nPayment ka intezar hai.")


def choose_text(phone, low):
    items = catalog()["products"]
    if re.fullmatch(r"\d+", low) and 1 <= int(low) <= len(items):
        return create_order(phone, items[int(low) - 1])
    for p in items:
        if p["name"].lower() in low or low in p["name"].lower():
            return create_order(phone, p)
    send(phone, "Samajh nahi aya. Barae meharbani list se tool chunein, ya tool ka naam likhein.")


def show_subs(phone):
    with db() as c:
        rows = c.execute("SELECT product,expires FROM subs WHERE phone=? ORDER BY expires", (phone,)).fetchall()
    if not rows:
        return send_buttons(phone, "Aap ki koi active subscription record mein nahi hai.", [("menu_order", "Place Order"), ("menu_main", "Main Menu")])
    send_buttons(phone, "Aap ki subscriptions:\n" + "\n".join(f"- {r['product']}: {fmt(r['expires'])} tak" for r in rows),
                 [("menu_order", "Renew / Order"), ("menu_main", "Main Menu")])


def on_image(phone, msg, u):
    if u["state"] != "awaiting_payment" or not u["pending"]:
        return send_buttons(phone, "Ye screenshot kis order ka hai? Pehle tool chunein, phir screenshot bhejein.", [("menu_order", "Place Order")])
    oid = u["pending"]
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        c.execute("UPDATE orders SET status='paid_review' WHERE id=?", (oid,))
    set_state(phone, "idle")
    send(phone, "Screenshot mil gaya. Owner verify kar ke jald confirm karenge. Shukriya!")
    try:
        forward_image(msg["image"]["id"], f"Payment screenshot - Order #{oid}\n{o['product']} {o['price']}\nCustomer: {phone}")
        send_buttons(OWNER, f"Order #{oid}: {o['product']} {o['price']}\nCustomer: {phone}\nPayment verify kar ke chunein:",
                     [(f"confirm:{oid}", "Confirm"), (f"reject:{oid}", "Reject")])
    except Exception as e:
        print("FORWARD ERROR", e)
        notify_owner(f"Order #{oid} ka screenshot forward nahi hua. Customer {phone} se khud check karein.")


def owner_cmd(text):
    t = text.strip()
    m = re.match(r"(?i)^(confirm|reject)\s+#?(\d+)\s*(.*)$", t)
    if m:
        act, oid, note = m.group(1).lower(), int(m.group(2)), m.group(3)
        with db() as c:
            o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return send(OWNER, f"Order #{oid} nahi mila.")
        if o["status"] in ("confirmed", "rejected"):
            return send(OWNER, f"Order #{oid} pehle hi {o['status']} hai.")
        if act == "confirm":
            exp = time.time() + o["days"] * 86400
            with db() as c:
                c.execute("UPDATE orders SET status='confirmed' WHERE id=?", (oid,))
                c.execute("INSERT INTO subs(phone,product,expires) VALUES(?,?,?)", (o["phone"], o["product"], exp))
            send_smart(o["phone"], f"Aap ki payment confirm ho gayi hai. {o['product']} ki delivery owner abhi kar rahe hain. "
                                   f"Subscription {fmt(exp)} tak chalegi. Shukriya!")
            send(OWNER, f"Order #{oid} confirm.\nRecord: {o['phone']} | {o['product']} | expiry {fmt(exp)}\n"
                        f"Ab customer ko account details khud bhej dein.")
        else:
            with db() as c:
                c.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
            send_smart(o["phone"], "Maazrat, aap ki payment verify nahi ho saki." + (f" Wajah: {note}." if note else "") +
                       " Dobara koshish ke liye 'Place Order' dabayein.")
            send(OWNER, f"Order #{oid} reject kar diya.")
        return
    m = re.match(r"(?is)^send\s+(\d+)\s+(.+)$", t)
    if m:
        send_smart(m.group(1), m.group(2))
        return send(OWNER, "Bhej diya.")
    if t.lower() == "subs":
        with db() as c:
            rows = c.execute("SELECT phone,product,expires FROM subs ORDER BY expires LIMIT 30").fetchall()
        return send(OWNER, "\n".join(f"{r['phone']} - {r['product']} - {fmt(r['expires'])}" for r in rows) or "Koi subscription nahi.")
    send(OWNER, "Commands:\nCONFIRM 12\nREJECT 12 wajah\nSEND 923001234567 message\nSUBS")


def handle(phone, name, msg):
    u = get_user(phone, name)
    typ = msg.get("type")
    rid = None
    if typ == "interactive":
        it = msg.get("interactive", {})
        rid = (it.get("button_reply") or it.get("list_reply") or {}).get("id")
    text = msg.get("text", {}).get("body", "").strip() if typ == "text" else ""
    if OWNER and phone == OWNER:
        m = re.match(r"(confirm|reject):(\d+)$", rid or "")
        if m:
            text = f"{m.group(1)} {m.group(2)}"
        return owner_cmd(text) if text else None
    if typ == "image":
        return on_image(phone, msg, u)
    if rid:
        if rid == "menu_main":
            return menu(phone)
        if rid == "menu_prices":
            return show_prices(phone)
        if rid == "menu_order":
            return start_order(phone)
        if rid == "menu_subs":
            return show_subs(phone)
        if rid == "cancel":
            return cancel(phone)
        if rid.startswith("prod:"):
            items = catalog()["products"]
            k = int(rid[5:])
            return create_order(phone, items[k - 1]) if 1 <= k <= len(items) else start_order(phone)
        return
    if typ != "text" or not text:
        return send(phone, "Maazrat, main sirf text aur payment ka screenshot samajh sakta hoon.")
    log(phone, "user", text)
    low, state = text.lower(), u["state"]
    if re.search(r"\bcancel\b", low) and state != "idle":
        return cancel(phone)
    if low in ("menu", "hi", "hello", "salam", "assalam o alaikum", "assalamualaikum", "start"):
        return menu(phone)
    if state == "choosing":
        return choose_text(phone, low)
    if low in ("1", "price", "prices", "list", "catalog", "tools"):
        return show_prices(phone)
    if low == "2" or re.search(r"\b(order|buy|kharid\w*|lena|lene)\b", low):
        return start_order(phone)
    if low == "3" or "meri subscription" in low:
        return show_subs(phone)
    if re.search(r"\b(owner|human|insaan|malik)\b", low):
        notify_owner(f"Customer {phone} aap se baat karna chahta hai.")
        return send(phone, "Theek hai, owner ko bata diya hai. Wo jald rabta karenge.")
    if AKEY or GKEY:
        ans = ai_reply(phone, state)
        if ans:
            return send(phone, ans)
    ans = faq_reply(text)
    if ans:
        return send(phone, ans)
    notify_owner(f"Customer {phone} ka sawal (bot jawab nahi de saka): {text[:300]}")
    send(phone, "Ye main owner se confirm kar ke batata hoon. Wo jald jawab denge.")


def safe_handle(phone, name, msg):
    try:
        handle(phone, name, msg)
    except Exception as e:
        print("HANDLE ERROR", repr(e))


# ---------- subscription reminders ----------
def reminders():
    now, digest = time.time(), []
    with db() as c:
        rows = c.execute("SELECT * FROM subs").fetchall()
    for s in rows:
        left = (s["expires"] - now) / 86400
        msg, col, val = None, None, None
        if left <= 0 and s["r1"] < 2:
            msg, col, val = f"Aap ki {s['product']} subscription khatam ho chuki hai. Renew ke liye 'Place Order' dabayein.", "r1", 2
        elif 0 < left <= 1 and s["r1"] < 1:
            msg, col, val = f"Yaad dehani: aap ki {s['product']} subscription kal tak khatam ho rahi hai. Renew ke liye 'Place Order' dabayein.", "r1", 1
        elif 1 < left <= 3 and s["r3"] < 1:
            msg, col, val = f"Yaad dehani: aap ki {s['product']} subscription {fmt(s['expires'])} ko khatam ho rahi hai. Renew ke liye 'Place Order' dabayein.", "r3", 1
        if not msg:
            continue
        try:
            if in_window(s["phone"]) or TEMPLATE:
                ok = send_smart(s["phone"], msg)
            else:  # free mode: WhatsApp does not allow free text now -> tell the owner instead
                digest.append(f"{s['phone']} - {s['product']} - {fmt(s['expires'])}")
                ok = True
            if ok:
                with db() as c:
                    c.execute(f"UPDATE subs SET {col}=? WHERE id=?", (val, s["id"]))
        except Exception as e:
            print("REMINDER ERROR", e)
    if digest:
        notify_owner("Renewal reminder (in customers ko khud message karein, ya SEND number message likhein):\n" + "\n".join(digest))


# ---------- webhook ----------
@app.get("/")
def home():
    return "AHAD bot is running", 200


@app.get("/webhook")
def verify():
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY:
        return request.args.get("hub.challenge", ""), 200
    abort(403)


@app.post("/webhook")
def hook():
    if APP_SECRET:
        good = "sha256=" + hmac.new(APP_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), good):
            abort(403)
    data = request.get_json(silent=True) or {}
    for e in data.get("entry", []):
        for ch in e.get("changes", []):
            v = ch.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in v.get("contacts", [])}
            for m in v.get("messages", []):
                with db() as c:
                    if c.execute("SELECT 1 FROM seen WHERE id=?", (m["id"],)).fetchone():
                        continue
                    c.execute("INSERT INTO seen(id) VALUES(?)", (m["id"],))
                threading.Thread(target=safe_handle, args=(m["from"], names.get(m["from"], ""), m), daemon=True).start()
    return "ok", 200


init()
sched = BackgroundScheduler()
sched.add_job(reminders, "interval", minutes=60)
sched.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(E("PORT", "8000")))
