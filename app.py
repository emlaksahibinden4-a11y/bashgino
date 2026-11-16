#!/usr/bin/env python3
"""
باشگینو — سرور اصلی (API + سایت + ربات تلگرام)
فقط با کتابخانه‌های استاندارد پایتون ۳.۱۰+ کار می‌کند؛ نصب پکیج لازم نیست.

تنظیمات از فایل .env کنار همین فایل خوانده می‌شود (install.sh آن را می‌سازد).
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _load_env():
    p = BASE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
E = os.environ.get
HOST = E("HOST", "127.0.0.1")
PORT = int(E("PORT", "8080"))
DOMAIN = E("DOMAIN", "localhost")
APP_URL = E("APP_URL", f"https://{DOMAIN}/")
BOT_TOKEN = E("BOT_TOKEN", "").strip()
KAVENEGAR_API_KEY = E("KAVENEGAR_API_KEY", "").strip()
KAVENEGAR_TEMPLATE = E("KAVENEGAR_TEMPLATE", "bashgino-otp").strip()
OTP_DEV = E("OTP_DEV", "0") == "1"  # کد را در لاگ سرور هم چاپ می‌کند (فقط برای تست)
RUN_BOT = E("RUN_BOT", "1") == "1"
APP_NAME = E("APP_NAME", "باشگینو")
SUPPORT_USERNAME = E("SUPPORT_USERNAME", "").lstrip("@")
BOT_USERNAME = ""
TRIAL_DAYS = int(E("TRIAL_DAYS", "30"))
TRIAL_USERS = int(E("TRIAL_USERS", "3"))
DATA_DIR = Path(E("DATA_DIR", str(BASE / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DATA_DIR / "bashgino.db")

_secret_file = DATA_DIR / "secret.key"
if E("SECRET"):
    SECRET = E("SECRET").encode()
else:
    if not _secret_file.exists():
        _secret_file.write_text(secrets.token_hex(32))
        os.chmod(_secret_file, 0o600)
    SECRET = _secret_file.read_text().strip().encode()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bashgino")

DAY = 86400
COLS = {"athletes", "programs", "diets", "sessions", "coaches", "workspaces", "notifs", "analyses"}
LOCK = threading.RLock()

# ------------------------------------------------------------------ database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, phone TEXT UNIQUE, name TEXT DEFAULT '', role TEXT,
  tg_id INTEGER, plan TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS otps(
  phone TEXT PRIMARY KEY, hash TEXT, expires INTEGER, attempts INTEGER DEFAULT 0,
  last_sent INTEGER DEFAULT 0, hour_start INTEGER DEFAULT 0, hour_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, user_id INTEGER, expires INTEGER);
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY, col TEXT, owner INTEGER, phone TEXT, data TEXT, updated INTEGER);
CREATE INDEX IF NOT EXISTS ix_items_owner ON items(owner, col);
CREATE INDEX IF NOT EXISTS ix_items_phone ON items(phone);
CREATE TABLE IF NOT EXISTS tg_contacts(tg_id INTEGER PRIMARY KEY, phone TEXT, t INTEGER);
"""

_db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_db.row_factory = sqlite3.Row
_db.execute("PRAGMA journal_mode=WAL")
_db.executescript(SCHEMA)


def q(sql, args=()):
    with LOCK:
        return _db.execute(sql, args).fetchall()


def q1(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def x(sql, args=()):
    with LOCK:
        return _db.execute(sql, args)


# ------------------------------------------------------------------ helpers
class ApiError(Exception):
    def __init__(self, status, msg, **extra):
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra


_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def norm_phone(p):
    s = re.sub(r"\D", "", str(p or "").translate(_FA_DIGITS))
    if s.startswith("0098"):
        s = s[4:]
    elif s.startswith("98") and len(s) == 12:
        s = s[2:]
    if len(s) == 10 and s.startswith("9"):
        s = "0" + s
    return s


def valid_ir(s):
    return re.fullmatch(r"09\d{9}", s or "") is not None


def now():
    return int(time.time())


def new_id():
    return secrets.token_hex(6)


def code_hash(phone, code):
    return hmac.new(SECRET, f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()


def tok_hash(t):
    return hashlib.sha256(t.encode()).hexdigest()


def user_row(uid):
    return q1("SELECT * FROM users WHERE id=?", (uid,))


def user_json(u):
    return {"id": u["id"], "phone": u["phone"], "name": u["name"] or "", "role": u["role"],
            "telegram": bool(u["tg_id"])}


def plan_of(u):
    try:
        return json.loads(u["plan"]) if u["plan"] else None
    except ValueError:
        return None


def add_notif(owner, text):
    nid = new_id()
    data = {"id": nid, "t": now() * 1000, "read": False, "text": text}
    x("INSERT INTO items(id,col,owner,phone,data,updated) VALUES(?,?,?,?,?,?)",
      (nid, "notifs", owner, None, json.dumps(data, ensure_ascii=False), now()))
    u = user_row(owner)
    if u and u["tg_id"]:
        tg_send(u["tg_id"], "🔔 " + text)


# ------------------------------------------------------------------ telegram
def tg_api(method, **params):
    if not BOT_TOKEN:
        return None
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            return json.load(r).get("result")
    except urllib.error.HTTPError as e:
        log.warning("telegram %s: %s %s", method, e.code, e.read()[:300])
    except Exception as e:  # noqa
        log.warning("telegram %s: %s", method, e)
    return None


def tg_send(chat_id, text, markup=None):
    params = {"chat_id": chat_id, "text": text}
    if markup:
        params["reply_markup"] = markup
    threading.Thread(target=tg_api, args=("sendMessage",), kwargs=params, daemon=True).start()


def open_btn(text="🏋️ باز کردن پنل باشگینو"):
    return {"inline_keyboard": [[{"text": text, "web_app": {"url": APP_URL}}]]}


def tg_validate(init_data):
    """بررسی امضای initData مینی‌اپ تلگرام. خروجی: کاربر تلگرام یا None"""
    if not BOT_TOKEN or not init_data:
        return None
    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    h = pairs.pop("hash", None)
    if not h:
        return None
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(key, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, h):
        return None
    if now() - int(pairs.get("auth_date", "0") or 0) > 7 * DAY:
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None


# ------------------------------------------------------------------ sms
def kavenegar_send(phone, code):
    url = (f"https://api.kavenegar.com/v1/{KAVENEGAR_API_KEY}/verify/lookup.json?"
           + urllib.parse.urlencode({"receptor": phone, "token": code, "template": KAVENEGAR_TEMPLATE}))
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            res = json.load(r)
        ok = res.get("return", {}).get("status") == 200
        if not ok:
            log.warning("kavenegar: %s", res.get("return"))
        return ok
    except urllib.error.HTTPError as e:
        log.warning("kavenegar http %s: %s", e.code, e.read()[:300].decode("utf-8", "ignore"))
    except Exception as e:  # noqa
        log.warning("kavenegar: %s", e)
    return False


# ------------------------------------------------------------------ auth
def otp_request(phone):
    phone = norm_phone(phone)
    if not valid_ir(phone):
        raise ApiError(400, "شماره موبایل معتبر نیست. مثال: 09121234567")
    t = now()
    row = q1("SELECT * FROM otps WHERE phone=?", (phone,))
    hour_start, hour_count = (row["hour_start"], row["hour_count"]) if row else (t, 0)
    if row and t - row["last_sent"] < 60:
        raise ApiError(429, "برای ارسال دوباره کد کمی صبر کنید.", wait=60 - (t - row["last_sent"]))
    if t - hour_start > 3600:
        hour_start, hour_count = t, 0
    if hour_count >= 5:
        raise ApiError(429, "تعداد درخواست‌ها زیاد است. یک ساعت دیگر دوباره تلاش کنید.")
    code = f"{secrets.randbelow(100000):05d}"
    channels = []
    if KAVENEGAR_API_KEY and kavenegar_send(phone, code):
        channels.append("sms")
    tg_id = None
    u = q1("SELECT tg_id FROM users WHERE phone=?", (phone,))
    if u and u["tg_id"]:
        tg_id = u["tg_id"]
    else:
        c = q1("SELECT tg_id FROM tg_contacts WHERE phone=? ORDER BY t DESC", (phone,))
        tg_id = c["tg_id"] if c else None
    if tg_id and BOT_TOKEN:
        tg_send(tg_id, f"🔐 کد ورود شما به {APP_NAME}: {code}\nاین کد را به کسی ندهید.")
        channels.append("telegram")
    if OTP_DEV:
        log.info("OTP for %s: %s", phone, code)
        channels.append("dev")
    if not channels:
        raise ApiError(503, "ارسال کد در حال حاضر ممکن نیست. لطفاً کمی بعد دوباره تلاش کنید.")
    x("INSERT OR REPLACE INTO otps(phone,hash,expires,attempts,last_sent,hour_start,hour_count) "
      "VALUES(?,?,?,?,?,?,?)", (phone, code_hash(phone, code), t + 180, 0, t, hour_start, hour_count + 1))
    return {"ok": True, "phone": phone, "via": channels, "expires": 180}


def get_or_create_user(phone):
    u = q1("SELECT * FROM users WHERE phone=?", (phone,))
    if u:
        return u
    x("INSERT INTO users(phone,created) VALUES(?,?)", (phone, now()))
    return q1("SELECT * FROM users WHERE phone=?", (phone,))


def link_tg(u, tg_user):
    if tg_user and tg_user.get("id") and u["tg_id"] != tg_user["id"]:
        x("UPDATE users SET tg_id=NULL WHERE tg_id=?", (tg_user["id"],))
        x("UPDATE users SET tg_id=? WHERE id=?", (tg_user["id"], u["id"]))
        if not u["name"]:
            nm = " ".join(filter(None, [tg_user.get("first_name"), tg_user.get("last_name")]))
            x("UPDATE users SET name=? WHERE id=?", (nm, u["id"]))
    return user_row(u["id"])


def after_login(u):
    """اتصال ورزشکار به رکوردهایی که مربی‌ها با این شماره ساخته‌اند."""
    recs = q("SELECT * FROM items WHERE col='athletes' AND phone=?", (u["phone"],))
    for r in recs:
        d = json.loads(r["data"])
        if d.get("uid") != u["id"]:
            first = d.get("status") == "pending" or not d.get("uid")
            d["uid"] = u["id"]
            d["status"] = "active"
            x("UPDATE items SET data=?, updated=? WHERE id=?", (json.dumps(d, ensure_ascii=False), now(), r["id"]))
            if first:
                add_notif(r["owner"], f"ورزشکار «{d.get('name', u['phone'])}» وارد {APP_NAME} شد و برنامه‌هایش را می‌بیند.")
    if not u["role"] and recs:
        x("UPDATE users SET role='athlete' WHERE id=?", (u["id"],))
    if not u["name"] and recs:
        x("UPDATE users SET name=? WHERE id=?", (json.loads(recs[0]["data"]).get("name", ""), u["id"]))
    return user_row(u["id"])


def issue_token(u):
    t = secrets.token_urlsafe(32)
    x("INSERT INTO tokens(hash,user_id,expires) VALUES(?,?,?)", (tok_hash(t), u["id"], now() + 90 * DAY))
    return t


def otp_verify(phone, code, init_data=None):
    phone = norm_phone(phone)
    code = re.sub(r"\D", "", str(code or "").translate(_FA_DIGITS))
    row = q1("SELECT * FROM otps WHERE phone=?", (phone,))
    if not row or row["expires"] < now():
        raise ApiError(400, "کد منقضی شده است. دوباره درخواست کد بدهید.")
    if row["attempts"] >= 5:
        raise ApiError(429, "تعداد تلاش‌ها زیاد بود. دوباره درخواست کد بدهید.")
    if not hmac.compare_digest(row["hash"], code_hash(phone, code)):
        x("UPDATE otps SET attempts=attempts+1 WHERE phone=?", (phone,))
        raise ApiError(400, "کد وارد شده درست نیست.")
    x("DELETE FROM otps WHERE phone=?", (phone,))
    u = get_or_create_user(phone)
    u = link_tg(u, tg_validate(init_data))
    u = after_login(u)
    return {"token": issue_token(u), **bootstrap(u)}


def tg_login(init_data):
    tgu = tg_validate(init_data)
    if not tgu:
        raise ApiError(401, "اعتبارسنجی تلگرام ناموفق بود.")
    u = q1("SELECT * FROM users WHERE tg_id=?", (tgu["id"],))
    if not u:
        c = q1("SELECT phone FROM tg_contacts WHERE tg_id=?", (tgu["id"],))
        if not c:
            return {"needPhone": True}
        u = link_tg(get_or_create_user(c["phone"]), tgu)
    u = after_login(u)
    return {"token": issue_token(u), **bootstrap(u)}


def auth_user(handler):
    h = handler.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        raise ApiError(401, "لطفاً وارد شوید.")
    r = q1("SELECT user_id, expires FROM tokens WHERE hash=?", (tok_hash(h[7:].strip()),))
    if not r or r["expires"] < now():
        raise ApiError(401, "نشست شما منقضی شده است. دوباره وارد شوید.")
    return user_row(r["user_id"])


# ------------------------------------------------------------------ data
def items_of(owner):
    out = {c: [] for c in COLS}
    for r in q("SELECT col, data FROM items WHERE owner=? ORDER BY updated", (owner,)):
        out[r["col"]].append(json.loads(r["data"]))
    return out


def athlete_home(u):
    recs = q("SELECT * FROM items WHERE col='athletes' AND phone=?", (u["phone"],))
    out = []
    for r in recs:
        a = json.loads(r["data"])
        coach = user_row(r["owner"])
        mine = items_of(r["owner"])
        ws = next((w for w in mine["workspaces"] if w.get("id") == a.get("ws")), None)
        out.append({
            "athlete": a,
            "coach": {"name": coach["name"] if coach else "", "ws": ws.get("name") if ws else ""},
            "programs": [p for p in mine["programs"] if a["id"] in (p.get("assigned") or [])],
            "diets": [p for p in mine["diets"] if a["id"] in (p.get("assigned") or [])],
            "sessions": [s for s in mine["sessions"] if s.get("a") == a["id"]],
            "analyses": [s for s in mine["analyses"] if s.get("a") == a["id"]],
        })
    return out


def bootstrap(u):
    res = {"user": user_json(u), "plan": plan_of(u)}
    if u["role"] in ("coach", "gym"):
        res["items"] = items_of(u["id"])
    res["athleteOf"] = athlete_home(u)
    return res


def require_coach(u):
    if u["role"] not in ("coach", "gym"):
        raise ApiError(403, "این بخش مخصوص مربی‌هاست.")


def put_item(u, col, iid, data):
    require_coach(u)
    if col not in COLS or not re.fullmatch(r"[A-Za-z0-9_-]{4,40}", iid or "") or not isinstance(data, dict):
        raise ApiError(400, "درخواست نامعتبر")
    data["id"] = iid
    ex = q1("SELECT owner FROM items WHERE id=?", (iid,))
    if ex and ex["owner"] != u["id"]:
        raise ApiError(403, "دسترسی ندارید.")
    phone = None
    if col == "athletes":
        phone = norm_phone(data.get("phone")) or None
        data["phone"] = phone or ""
        if not ex:
            plan = plan_of(u) or {}
            cnt = q1("SELECT COUNT(*) n FROM items WHERE owner=? AND col='athletes'", (u["id"],))["n"]
            if plan.get("end", 0) < now() * 1000:
                raise ApiError(402, "اشتراک شما به پایان رسیده است.", code="expired")
            if cnt >= int(plan.get("users", 0)):
                raise ApiError(402, "ظرفیت پلن شما تکمیل است.", code="capacity")
            if phone:
                au = q1("SELECT * FROM users WHERE phone=?", (phone,))
                if au:  # ورزشکار قبلاً حساب دارد
                    data["uid"] = au["id"]
                    data["status"] = "active"
                    if not au["role"]:
                        x("UPDATE users SET role='athlete' WHERE id=?", (au["id"],))
                    if au["tg_id"]:
                        tg_send(au["tg_id"], f"🏋️ مربی «{u['name']}» شما را در {APP_NAME} اضافه کرد.", open_btn())
    raw = json.dumps(data, ensure_ascii=False)
    if len(raw) > 200_000:
        raise ApiError(413, "حجم اطلاعات زیاد است.")
    x("INSERT INTO items(id,col,owner,phone,data,updated) VALUES(?,?,?,?,?,?) "
      "ON CONFLICT(id) DO UPDATE SET data=excluded.data, phone=excluded.phone, updated=excluded.updated",
      (iid, col, u["id"], phone, raw, now()))
    return {"ok": True, "item": data}


def del_item(u, col, iid):
    require_coach(u)
    x("DELETE FROM items WHERE id=? AND owner=? AND col=?", (iid, u["id"], col))
    return {"ok": True}


def onboard(u, body):
    role = body.get("role")
    if role not in ("coach", "gym"):
        raise ApiError(400, "نقش نامعتبر")
    name = (body.get("name") or "").strip()[:80]
    ws_name = (body.get("wsName") or name).strip()[:80]
    if not name:
        raise ApiError(400, "نام را وارد کنید.")
    plan = plan_of(u)
    if not plan:
        t = now() * 1000
        plan = {"id": "trial", "users": TRIAL_USERS, "start": t, "end": t + TRIAL_DAYS * DAY * 1000, "cycle": "trial"}
    x("UPDATE users SET role=?, name=?, plan=? WHERE id=?", (role, name, json.dumps(plan), u["id"]))
    u = user_row(u["id"])
    if not q1("SELECT 1 FROM items WHERE owner=? AND col='workspaces'", (u["id"],)):
        wid = new_id()
        x("INSERT INTO items(id,col,owner,phone,data,updated) VALUES(?,?,?,?,?,?)",
          (wid, "workspaces", u["id"], None, json.dumps({"id": wid, "name": ws_name, "type": role}, ensure_ascii=False), now()))
        add_notif(u["id"], f"به {APP_NAME} خوش آمدید! دوره آزمایشی {TRIAL_DAYS} روزه شما فعال شد.")
    return bootstrap(u)


def update_me(u, body):
    if "name" in body:
        x("UPDATE users SET name=? WHERE id=?", ((body.get("name") or "").strip()[:80], u["id"]))
    if body.get("role") in ("coach", "gym") and u["role"] in ("coach", "gym"):
        x("UPDATE users SET role=? WHERE id=?", (body["role"], u["id"]))
    return {"user": user_json(user_row(u["id"]))}


def athlete_log(u, body):
    aid = body.get("athleteId")
    r = q1("SELECT * FROM items WHERE id=? AND col='athletes'", (aid,))
    if not r or r["phone"] != u["phone"]:
        raise ApiError(403, "دسترسی ندارید.")
    a = json.loads(r["data"])
    sid = new_id()
    s = {"id": sid, "a": aid, "ws": a.get("ws"), "t": now() * 1000, "by": "athlete",
         "p": body.get("programId"), "day": (body.get("day") or "")[:60]}
    x("INSERT INTO items(id,col,owner,phone,data,updated) VALUES(?,?,?,?,?,?)",
      (sid, "sessions", r["owner"], None, json.dumps(s, ensure_ascii=False), now()))
    add_notif(r["owner"], f"«{a.get('name', '')}» تمرین {s['day'] or 'امروز'} را انجام داد ✅")
    return {"ok": True, "session": s}


# ------------------------------------------------------------------ routes (قابل توسعه)
# برای افزودن قابلیت جدید فقط یک تابع با @route بنویسید:
#   @route("POST", "my/feature")            -> /api/my/feature   (نیاز به ورود)
#   @route("GET", "items/*/*", auth=False)  -> * ها در ctx.args قرار می‌گیرند
ROUTES = []


class Ctx:
    def __init__(self, handler, body, args, user):
        self.handler, self.body, self.args, self.user = handler, body, args, user


def route(method, path, auth=True):
    def deco(fn):
        ROUTES.append((method, path.strip("/").split("/"), auth, fn))
        return fn
    return deco


def match_route(method, parts):
    for m, pat, auth, fn in ROUTES:
        if m == method and len(pat) == len(parts) and all(p == "*" or p == v for p, v in zip(pat, parts)):
            return fn, auth, [v for p, v in zip(pat, parts) if p == "*"]
    return None


@route("GET", "health", auth=False)
def r_health(c):
    return {"ok": True, "sms": bool(KAVENEGAR_API_KEY), "bot": bool(BOT_TOKEN), "ai": bool(AI_API_KEY)}


@route("POST", "auth/otp", auth=False)
def r_otp(c):
    return otp_request(c.body.get("phone"))


@route("POST", "auth/verify", auth=False)
def r_verify(c):
    return otp_verify(c.body.get("phone"), c.body.get("code"), c.body.get("initData"))


@route("POST", "auth/telegram", auth=False)
def r_tg(c):
    return tg_login(c.body.get("initData"))


@route("POST", "auth/logout")
def r_logout(c):
    h = c.handler.headers.get("Authorization", "")[7:].strip()
    x("DELETE FROM tokens WHERE hash=?", (tok_hash(h),))
    return {"ok": True}


@route("GET", "bootstrap")
def r_boot(c):
    return bootstrap(c.user)


@route("POST", "onboard")
def r_onboard(c):
    return onboard(c.user, c.body)


@route("PATCH", "me")
def r_me(c):
    return update_me(c.user, c.body)


@route("PUT", "items/*/*")
def r_put(c):
    return put_item(c.user, c.args[0], c.args[1], c.body)


@route("DELETE", "items/*/*")
def r_del(c):
    return del_item(c.user, c.args[0], c.args[1])


@route("POST", "athlete/log")
def r_alog(c):
    return athlete_log(c.user, c.body)


def check_photo(p):
    if not p:
        return ""
    if not re.match(r"^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$", p) or len(p) > 400_000:
        raise ApiError(400, "عکس نامعتبر است یا حجمش زیاد است.")
    return p


@route("POST", "athlete/photo")
def r_aphoto(c):
    photo = check_photo(c.body.get("photo"))
    for r in q("SELECT * FROM items WHERE col='athletes' AND phone=?", (c.user["phone"],)):
        d = json.loads(r["data"])
        d["photo"] = photo
        x("UPDATE items SET data=?, updated=? WHERE id=?", (json.dumps(d, ensure_ascii=False), now(), r["id"]))
    return {"ok": True}


# ------------------------------------------------------------------ AI (Claude)
AI_API_KEY = E("AI_API_KEY", "").strip()
AI_MODEL = E("AI_MODEL", "claude-sonnet-5-5").strip()
AI_BASE_URL = E("AI_BASE_URL", "https://api.anthropic.com").rstrip("/")
AI_DAILY_LIMIT = int(E("AI_DAILY_LIMIT", "30"))
x("CREATE TABLE IF NOT EXISTS ai_usage(user_id INTEGER, day TEXT, n INTEGER, PRIMARY KEY(user_id, day))")

AI_RULES = ("تو مربی ورزشی و متخصص تغذیه‌ی حرفه‌ای در اپ «" + APP_NAME + "» هستی. همیشه فارسی روان بنویس. "
            "توصیه‌ها باید ایمن، واقع‌بینانه و مناسب سطح فرد باشند. تو پزشک نیستی؛ برای بیماری، آسیب، بارداری "
            "یا داروی خاص، توصیه کن با پزشک مشورت شود. هرگز رژیم‌های خیلی کم‌کالری (کمتر از ۱۲۰۰ برای زنان و "
            "۱۵۰۰ برای مردان) یا روش‌های افراطی پیشنهاد نکن.")

PROGRAM_SCHEMA = ('{"name": "نام برنامه", "goal": "هدف", "level": "مبتدی|متوسط|پیشرفته", "desc": "توضیح کوتاه شامل گرم کردن و نکات", '
                  '"days": [{"title": "روز ۱ — سینه و پشت بازو", "ex": [{"n": "نام حرکت", "s": "4", "r": "10-12"}]}]}')
DIET_SCHEMA = ('{"name": "نام رژیم", "goal": "هدف", "kcal": 2200, "desc": "توضیح کوتاه و نکات", '
               '"meals": [{"title": "صبحانه", "items": "هر قلم در یک خط با مقدار"}]}')


def ai_quota(u):
    day = time.strftime("%Y-%m-%d")
    r = q1("SELECT n FROM ai_usage WHERE user_id=? AND day=?", (u["id"], day))
    if r and r["n"] >= AI_DAILY_LIMIT:
        raise ApiError(429, "سقف استفاده‌ی امروز از هوش مصنوعی پر شده است. فردا دوباره امتحان کنید.")
    x("INSERT INTO ai_usage(user_id,day,n) VALUES(?,?,1) ON CONFLICT(user_id,day) DO UPDATE SET n=n+1", (u["id"], day))


def ai_call(u, system, messages, max_tokens=4000):
    if not AI_API_KEY:
        raise ApiError(503, "هوش مصنوعی هنوز فعال نشده است.")
    ai_quota(u)
    req = urllib.request.Request(
        AI_BASE_URL + "/v1/messages",
        data=json.dumps({"model": AI_MODEL, "max_tokens": max_tokens, "system": system,
                         "messages": messages}).encode(),
        headers={"Content-Type": "application/json", "x-api-key": AI_API_KEY, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        log.warning("ai http %s: %s", e.code, e.read()[:500].decode("utf-8", "ignore"))
        raise ApiError(502, "سرویس هوش مصنوعی پاسخ نداد. کمی بعد دوباره تلاش کنید.")
    except Exception as e:  # noqa
        log.warning("ai: %s", e)
        raise ApiError(502, "اتصال به هوش مصنوعی برقرار نشد.")
    return "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text").strip()


def ai_json(u, system, content, max_tokens=4000):
    text = ai_call(u, system + "\nفقط و فقط یک شیء JSON معتبر برگردان، بدون هیچ متن یا ``` اضافه.",
                   [{"role": "user", "content": content}], max_tokens)
    a, b = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[a:b + 1])
    except ValueError:
        log.warning("ai bad json: %s", text[:300])
        raise ApiError(502, "پاسخ هوش مصنوعی قابل خواندن نبود. دوباره تلاش کنید.")


def clean_program(p):
    p = p if isinstance(p, dict) else {}
    days = []
    for d in (p.get("days") or [])[:7]:
        ex = [{"n": str(e.get("n", ""))[:80], "s": str(e.get("s", ""))[:10], "r": str(e.get("r", ""))[:20]}
              for e in (d.get("ex") or [])[:12] if isinstance(e, dict) and e.get("n")]
        days.append({"title": str(d.get("title", ""))[:80], "ex": ex})
    return {"name": str(p.get("name", "برنامه هوشمند"))[:80], "goal": str(p.get("goal", ""))[:40],
            "level": p.get("level") if p.get("level") in ("مبتدی", "متوسط", "پیشرفته") else "متوسط",
            "desc": str(p.get("desc", ""))[:1500], "days": days}


def clean_diet(p):
    p = p if isinstance(p, dict) else {}
    meals = [{"title": str(m.get("title", ""))[:40], "items": str(m.get("items", ""))[:800]}
             for m in (p.get("meals") or [])[:8] if isinstance(m, dict)]
    kcal = re.sub(r"\D", "", str(p.get("kcal", "")))[:5]
    return {"name": str(p.get("name", "رژیم هوشمند"))[:80], "goal": str(p.get("goal", ""))[:40], "kcal": kcal,
            "desc": str(p.get("desc", ""))[:1500], "meals": meals}


def person_info(b):
    keys = [("sex", "جنسیت"), ("age", "سن"), ("height", "قد (سانتی‌متر)"), ("weight", "وزن (کیلوگرم)"),
            ("goal", "هدف"), ("level", "سطح"), ("days", "تعداد روز تمرین در هفته"), ("minutes", "مدت هر جلسه (دقیقه)"),
            ("place", "محل تمرین"), ("kcal", "کالری هدف"), ("notes", "توضیحات مربی/ورزشکار")]
    return "\n".join(f"- {t}: {str(b[k])[:300]}" for k, t in keys if b.get(k))


@route("POST", "ai/program")
def r_ai_program(c):
    require_coach(c.user)
    p = ai_json(c.user, AI_RULES + "\nیک برنامه‌ی تمرینی هفتگی کامل طراحی کن با این ساختار:\n" + PROGRAM_SCHEMA,
                "مشخصات ورزشکار:\n" + person_info(c.body))
    return {"program": clean_program(p)}


@route("POST", "ai/diet")
def r_ai_diet(c):
    require_coach(c.user)
    p = ai_json(c.user, AI_RULES + "\nیک برنامه‌ی غذایی روزانه با غذاهای رایج ایرانی طراحی کن با این ساختار:\n" + DIET_SCHEMA,
                "مشخصات ورزشکار:\n" + person_info(c.body))
    return {"diet": clean_diet(p)}


@route("POST", "ai/analyze")
def r_ai_analyze(c):
    b = c.body
    photo = check_photo(b.get("photo"))
    if not photo:
        raise ApiError(400, "عکس را انتخاب کنید.")
    rec = None
    if c.user["role"] not in ("coach", "gym"):
        rec = q1("SELECT * FROM items WHERE id=? AND col='athletes'", (b.get("athleteId"),))
        if not rec or rec["phone"] != c.user["phone"]:
            raise ApiError(403, "دسترسی ندارید.")
    mt, data = re.match(r"^data:(image/\w+);base64,(.+)$", photo).groups()
    system = (AI_RULES + "\nاز روی عکس بدن، ارزیابی ظاهری و غیرپزشکی انجام بده: تیپ بدنی، تخمین تقریبی درصد چربی "
              "(به‌صورت بازه)، نقاط قوت، و بخش‌هایی که باید روی آن‌ها کار شود؛ محترمانه و انگیزه‌بخش. "
              "سپس بر همین اساس یک برنامه‌ی تمرینی و یک رژیم غذایی پیشنهاد بده. "
              "اگر عکس شامل یک انسان نیست، یا فرد به نظر زیر ۱۸ سال است، یا عکس نامناسب/برهنه است، فقط "
              '{"error": "دلیل به فارسی"} برگردان. ساختار خروجی:\n'
              '{"analysis": {"summary": "خلاصه ۲-۳ جمله", "bodyType": "اکتومورف/مزومورف/اندومورف یا ترکیبی", '
              '"bodyFat": "مثلاً ۱۸ تا ۲۲ درصد", "strengths": ["..."], "focus": ["..."], "tips": ["..."]}, '
              '"program": ' + PROGRAM_SCHEMA + ', "diet": ' + DIET_SCHEMA + "}")
    res = ai_json(c.user, system, [
        {"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}},
        {"type": "text", "text": "مشخصات:\n" + (person_info(b) or "-")}], max_tokens=6000)
    if res.get("error"):
        raise ApiError(400, str(res["error"])[:300])
    an = res.get("analysis") or {}
    lst = lambda v: [str(i)[:200] for i in (v or [])[:6]] if isinstance(v, list) else []
    out = {"analysis": {"summary": str(an.get("summary", ""))[:800], "bodyType": str(an.get("bodyType", ""))[:60],
                        "bodyFat": str(an.get("bodyFat", ""))[:40], "strengths": lst(an.get("strengths")),
                        "focus": lst(an.get("focus")), "tips": lst(an.get("tips"))},
           "program": clean_program(res.get("program")), "diet": clean_diet(res.get("diet"))}
    if rec:  # ورزشکار خودش آنالیز گرفته: برای مربی ذخیره شود (بدون عکس)
        a = json.loads(rec["data"])
        aid = new_id()
        item = {"id": aid, "a": a["id"], "ws": a.get("ws"), "t": now() * 1000, "by": "athlete", **out}
        x("INSERT INTO items(id,col,owner,phone,data,updated) VALUES(?,?,?,?,?,?)",
          (aid, "analyses", rec["owner"], None, json.dumps(item, ensure_ascii=False), now()))
        add_notif(rec["owner"], f"«{a.get('name', '')}» آنالیز بدن با هوش مصنوعی انجام داد؛ نتیجه در صفحه‌ی او است.")
        out["saved"] = True
    return out


@route("POST", "ai/chat")
def r_ai_chat(c):
    rec = q1("SELECT * FROM items WHERE id=? AND col='athletes'", (c.body.get("athleteId"),))
    if not rec or rec["phone"] != c.user["phone"]:
        raise ApiError(403, "دسترسی ندارید.")
    a = json.loads(rec["data"])
    mine = items_of(rec["owner"])
    progs = [clean_program(p) for p in mine["programs"] if a["id"] in (p.get("assigned") or [])]
    diets = [clean_diet(p) for p in mine["diets"] if a["id"] in (p.get("assigned") or [])]
    ctx = json.dumps({"athlete": {k: a.get(k) for k in ("name", "goal", "h", "w", "note")},
                      "programs": progs, "diets": diets}, ensure_ascii=False)[:12000]
    system = (AI_RULES + "\nتو دستیار ورزشکار هستی و به سؤال‌هایش درباره‌ی حرکات، اجرای درست، تغذیه، "
              "ریکاوری و انگیزه کوتاه و کاربردی جواب می‌دهی. برنامه‌ی مربی را تغییر نده؛ اگر نیاز به تغییر بود "
              "بگو با مربی هماهنگ کند. اطلاعات ورزشکار و برنامه‌هایش:\n" + ctx)
    msgs = []
    for m in (c.body.get("messages") or [])[-12:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": str(m["content"])[:2000]})
    if not msgs or msgs[-1]["role"] != "user":
        raise ApiError(400, "پیامی ارسال نشده است.")
    return {"reply": ai_call(c.user, system, msgs, 1200)}


# ------------------------------------------------------------------ http
INDEX = BASE / "index.html"
STATIC = BASE / "static"
STATIC_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".svg": "image/svg+xml"}


def static_images():
    if not STATIC.is_dir():
        return {}
    return {f.stem: "/static/" + f.name for f in STATIC.iterdir() if f.suffix.lower() in STATIC_TYPES}


class Handler(BaseHTTPRequestHandler):
    server_version = "Bashgino"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status, body, ctype="application/json; charset=utf-8", cache="no-store"):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ApiError(413, "درخواست خیلی بزرگ است.")
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiError(400, "JSON نامعتبر")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api("GET", path)
        if path.startswith("/static/"):
            f = (STATIC / path[8:]).resolve()
            if f.parent == STATIC.resolve() and f.suffix.lower() in STATIC_TYPES and f.is_file():
                return self._send(200, f.read_bytes(), STATIC_TYPES[f.suffix.lower()], "public, max-age=86400")
            return self._send(404, {"error": "not found"})
        if path in ("/", "/index.html") or "." not in path.rsplit("/", 1)[-1]:
            cfg = json.dumps({"bot": BOT_USERNAME, "support": SUPPORT_USERNAME, "ai": bool(AI_API_KEY),
                              "images": static_images()})
            html = INDEX.read_text(encoding="utf-8").replace(
                '<div id="app"></div>', f'<div id="app"></div>\n<script>window.__BG={cfg};</script>', 1)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        self._api("POST", urllib.parse.urlparse(self.path).path)

    def do_PUT(self):
        self._api("PUT", urllib.parse.urlparse(self.path).path)

    def do_PATCH(self):
        self._api("PATCH", urllib.parse.urlparse(self.path).path)

    def do_DELETE(self):
        self._api("DELETE", urllib.parse.urlparse(self.path).path)

    def _api(self, method, path):
        try:
            body = self._body() if method in ("POST", "PUT", "PATCH") else {}
            m = match_route(method, path.strip("/").split("/")[1:])
            if not m:
                raise ApiError(404, "not found")
            fn, need_auth, args = m
            user = auth_user(self) if need_auth else None
            self._send(200, fn(Ctx(self, body, args, user)))
        except ApiError as e:
            self._send(e.status, {"error": e.msg, **e.extra})
        except Exception:  # noqa
            log.exception("server error")
            self._send(500, {"error": "خطای سرور. دوباره تلاش کنید."})


# ------------------------------------------------------------------ bot
def bot_loop():
    me = tg_api("getMe")
    if not me:
        log.warning("bot: توکن نامعتبر است یا تلگرام در دسترس نیست؛ ربات اجرا نشد.")
        return
    global BOT_USERNAME
    BOT_USERNAME = me["username"]
    log.info("bot @%s started", BOT_USERNAME)
    tg_api("setChatMenuButton", menu_button={"type": "web_app", "text": "پنل باشگینو", "web_app": {"url": APP_URL}})
    tg_api("setMyCommands", commands=[
        {"command": "start", "description": "شروع و باز کردن پنل"},
        {"command": "login", "description": "ورود با شماره موبایل"},
        {"command": "help", "description": "راهنما"}])
    offset = 0
    while True:
        ups = tg_api("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
        if ups is None:
            time.sleep(3)
            continue
        for up in ups:
            offset = up["update_id"] + 1
            try:
                bot_handle(up.get("message") or {})
            except Exception:  # noqa
                log.exception("bot handler")


def bot_handle(m):
    if not m or m.get("chat", {}).get("type") != "private":
        return
    chat, frm = m["chat"]["id"], m.get("from", {})
    if m.get("contact"):
        c = m["contact"]
        if c.get("user_id") != frm.get("id"):
            return tg_send(chat, "لطفاً شماره‌ی خودتان را با دکمه‌ی «ارسال شماره» بفرستید.")
        phone = norm_phone(c.get("phone_number"))
        x("INSERT OR REPLACE INTO tg_contacts(tg_id,phone,t) VALUES(?,?,?)", (frm["id"], phone, now()))
        u = q1("SELECT * FROM users WHERE phone=?", (phone,))
        if u:
            link_tg(u, frm)
        tg_api("sendMessage", chat_id=chat, text="✅ شماره شما تأیید شد.", reply_markup={"remove_keyboard": True})
        return tg_send(chat, "حالا پنل را باز کنید؛ خودکار وارد می‌شوید 👇", open_btn())
    text = (m.get("text") or "").strip()
    cmd = text.split(" ")[0].split("@")[0]
    if cmd == "/login":
        return tg_send(chat, "برای ورود، شماره موبایل خود را با دکمه زیر بفرستید 👇", {
            "keyboard": [[{"text": "📱 ارسال شماره موبایل", "request_contact": True}]],
            "resize_keyboard": True, "one_time_keyboard": True})
    if cmd == "/help":
        return tg_send(chat, f"📌 راهنمای {APP_NAME}\n/start — باز کردن پنل\n/login — ورود با شماره موبایل\n\n"
                             "مربی‌ها: ورزشکار را با شماره موبایلش اضافه کنید.\n"
                             "ورزشکارها: با همان شماره وارد شوید تا برنامه‌تان را ببینید.", open_btn())
    tg_send(chat, f"سلام {frm.get('first_name', '')} 👋\nبه {APP_NAME} خوش آمدید — سامانه مدیریت ورزشکاران، "
                  "برنامه تمرینی و رژیم غذایی برای مربیان و باشگاه‌ها.\n\nبرای شروع روی دکمه زیر بزنید 👇", open_btn())


# ------------------------------------------------------------------ main
def main():
    if RUN_BOT and BOT_TOKEN:
        threading.Thread(target=bot_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("%s server on http://%s:%s  (sms=%s, bot=%s, ai=%s, dev=%s)", APP_NAME, HOST, PORT,
             bool(KAVENEGAR_API_KEY), bool(BOT_TOKEN), bool(AI_API_KEY), OTP_DEV)
    srv.serve_forever()


if __name__ == "__main__":
    main()
