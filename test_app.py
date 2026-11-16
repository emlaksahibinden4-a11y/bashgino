#!/usr/bin/env python3
"""
تست‌های خودکار باشگینو — بدون نیاز به اینترنت، تلگرام، کاوه‌نگار یا کلید هوش مصنوعی.
اجرا:   python3 test_app.py
اگر همه‌ی تست‌ها سبز باشند، پیام OK چاپ می‌شود. install.sh قبل از هر به‌روزرسانی این تست‌ها را اجرا می‌کند.
"""
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT_TOKEN = "123456:TEST-TOKEN"
PHOTO = "data:image/jpeg;base64," + "A" * 200

# ---------------------------------------------------------------- هوش مصنوعی ساختگی
AI_CALLS = []
PROGRAM = {"name": "برنامه تست", "goal": "عضله‌سازی", "level": "متوسط", "desc": "گرم کردن",
           "days": [{"title": "روز ۱", "ex": [{"n": "اسکوات", "s": "4", "r": "10"}]}]}
DIET = {"name": "رژیم تست", "goal": "کاهش وزن", "kcal": 2000, "desc": "",
        "meals": [{"title": "صبحانه", "items": "تخم‌مرغ"}]}


class MockAI(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        AI_CALLS.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        s = body["system"]
        if "دستیار ورزشکار" in s:
            text = "جواب تست"
        elif "ارزیابی ظاهری" in s:
            text = json.dumps({"analysis": {"summary": "خوب", "bodyType": "مزومورف", "bodyFat": "۱۵ تا ۱۸",
                                            "strengths": ["شانه"], "focus": ["پا"], "tips": ["خواب"]},
                               "program": PROGRAM, "diet": DIET}, ensure_ascii=False)
        elif "برنامه‌ی غذایی" in s:
            text = "```json\n" + json.dumps(DIET, ensure_ascii=False) + "\n```"
        else:
            text = "این برنامه است: " + json.dumps(PROGRAM, ensure_ascii=False)
        out = json.dumps({"content": [{"type": "text", "text": text}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


ai_srv = ThreadingHTTPServer(("127.0.0.1", 0), MockAI)
threading.Thread(target=ai_srv.serve_forever, daemon=True).start()

# ---------------------------------------------------------------- بارگذاری برنامه با تنظیمات تست
TMP = tempfile.mkdtemp(prefix="bashgino-test-")
os.environ.update({
    "DATA_DIR": TMP, "BOT_TOKEN": BOT_TOKEN, "KAVENEGAR_API_KEY": "", "OTP_DEV": "1", "RUN_BOT": "0",
    "AI_API_KEY": "test-key", "AI_BASE_URL": f"http://127.0.0.1:{ai_srv.server_port}", "AI_DAILY_LIMIT": "50",
    "TRIAL_USERS": "3", "SECRET": "test-secret",
})
APP_FILE = Path(os.environ.get("APP_FILE", HERE / "app.py"))
spec = importlib.util.spec_from_file_location("bashgino_app", APP_FILE)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
app.log.setLevel("ERROR")
app.tg_send = lambda *a, **k: None          # در تست پیامی به تلگرام نمی‌رود
app.tg_api = lambda *a, **k: None
app.secrets.randbelow = lambda n: 12345      # کد ورود همیشه 12345
CODE = "12345"

srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"


def call(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


_n = [0]


def new_phone():
    _n[0] += 1
    return "0912" + str(1000000 + _n[0])


def login(phone=None):
    phone = phone or new_phone()
    st, r = call("POST", "/api/auth/otp", {"phone": phone})
    assert st == 200, r
    st, r = call("POST", "/api/auth/verify", {"phone": phone, "code": CODE})
    assert st == 200, r
    return r["token"], r, phone


def coach(role="coach"):
    tok, _, phone = login()
    st, r = call("POST", "/api/onboard", {"role": role, "name": "مربی تست", "wsName": "باشگاه تست"}, tok)
    assert st == 200, r
    return tok, r, phone


def put(tok, col, item):
    return call("PUT", f"/api/items/{col}/{item['id']}", item, tok)


def tg_init(user_id, **extra):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "Ali"}), **extra}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


# ================================================================ تست‌ها
class T1_Helpers(unittest.TestCase):
    def test_phone_normalize(self):
        for raw in ["09121234567", "+98 912 123 4567", "00989121234567", "9121234567", "۰۹۱۲۱۲۳۴۵۶۷", "989121234567"]:
            self.assertEqual(app.norm_phone(raw), "09121234567", raw)
        self.assertFalse(app.valid_ir("0912123"))

    def test_health_and_index(self):
        st, r = call("GET", "/api/health")
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"] and r["ai"])
        st, html = call("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"window.__BG", html)

    def test_static_no_path_traversal(self):
        st, _ = call("GET", "/static/../app.py")
        self.assertEqual(st, 404)
        st, _ = call("GET", "/static/..%2Fapp.py")
        self.assertEqual(st, 404)


class T2_Auth(unittest.TestCase):
    def test_bad_phone(self):
        st, _ = call("POST", "/api/auth/otp", {"phone": "123"})
        self.assertEqual(st, 400)

    def test_wrong_code_then_lockout(self):
        phone = new_phone()
        call("POST", "/api/auth/otp", {"phone": phone})
        for _ in range(5):
            st, _ = call("POST", "/api/auth/verify", {"phone": phone, "code": "00000"})
            self.assertEqual(st, 400)
        st, _ = call("POST", "/api/auth/verify", {"phone": phone, "code": CODE})
        self.assertEqual(st, 429, "بعد از ۵ تلاش اشتباه، حتی کد درست هم نباید قبول شود")

    def test_resend_rate_limit(self):
        phone = new_phone()
        self.assertEqual(call("POST", "/api/auth/otp", {"phone": phone})[0], 200)
        self.assertEqual(call("POST", "/api/auth/otp", {"phone": phone})[0], 429)

    def test_code_single_use(self):
        tok, r, phone = login()
        st, _ = call("POST", "/api/auth/verify", {"phone": phone, "code": CODE})
        self.assertEqual(st, 400)

    def test_requires_token_and_logout(self):
        self.assertEqual(call("GET", "/api/bootstrap")[0], 401)
        tok, _, _ = login()
        self.assertEqual(call("GET", "/api/bootstrap", token=tok)[0], 200)
        call("POST", "/api/auth/logout", {}, tok)
        self.assertEqual(call("GET", "/api/bootstrap", token=tok)[0], 401)

    def test_telegram_signature(self):
        self.assertIsNotNone(app.tg_validate(tg_init(5, signature="x")))
        self.assertIsNone(app.tg_validate(tg_init(5).replace("Ali", "Bob")))
        self.assertEqual(call("POST", "/api/auth/telegram", {"initData": "hash=bad"})[0], 401)

    def test_telegram_contact_login(self):
        st, r = call("POST", "/api/auth/telegram", {"initData": tg_init(777)})
        self.assertTrue(r.get("needPhone"))
        phone = new_phone()
        app.bot_handle({"chat": {"id": 777, "type": "private"}, "from": {"id": 777, "first_name": "Ali"},
                        "contact": {"user_id": 777, "phone_number": "+98" + phone[1:]}})
        st, r = call("POST", "/api/auth/telegram", {"initData": tg_init(777)})
        self.assertEqual(st, 200)
        self.assertEqual(r["user"]["phone"], phone)

    def test_contact_of_someone_else_ignored(self):
        app.bot_handle({"chat": {"id": 888, "type": "private"}, "from": {"id": 888},
                        "contact": {"user_id": 999, "phone_number": "+989120000000"}})
        self.assertIsNone(app.q1("SELECT * FROM tg_contacts WHERE tg_id=888"))


class T3_CoachData(unittest.TestCase):
    def test_onboard_creates_trial_and_workspace(self):
        tok, r, _ = coach("gym")
        self.assertEqual(r["user"]["role"], "gym")
        self.assertEqual(r["plan"]["id"], "trial")
        self.assertEqual(len(r["items"]["workspaces"]), 1)
        self.assertEqual(len(r["items"]["notifs"]), 1)

    def test_items_crud_and_ownership(self):
        tok, r, _ = coach()
        ws = r["items"]["workspaces"][0]["id"]
        prog = {"id": "prog0001", "ws": ws, "name": "برنامه", "days": [], "assigned": []}
        self.assertEqual(put(tok, "programs", prog)[0], 200)
        tok2, _, _ = coach()
        self.assertEqual(put(tok2, "programs", {**prog, "name": "هک"})[0], 403, "مربی دیگر نباید بتواند تغییر دهد")
        call("DELETE", "/api/items/programs/prog0001", None, tok2)
        st, b = call("GET", "/api/bootstrap", token=tok)
        self.assertEqual(b["items"]["programs"][0]["name"], "برنامه")
        self.assertEqual(call("DELETE", "/api/items/programs/prog0001", None, tok)[0], 200)
        self.assertEqual(call("GET", "/api/bootstrap", token=tok)[1]["items"]["programs"], [])

    def test_invalid_collection(self):
        tok, _, _ = coach()
        self.assertEqual(put(tok, "users", {"id": "abcd1234"})[0], 400)

    def test_capacity_limit(self):
        tok, r, _ = coach()
        ws = r["items"]["workspaces"][0]["id"]
        for i in range(3):
            self.assertEqual(put(tok, "athletes", {"id": f"ath{i}xyz", "ws": ws, "name": f"a{i}"})[0], 200)
        st, e = put(tok, "athletes", {"id": "ath9xyz", "ws": ws, "name": "زیادی"})
        self.assertEqual((st, e.get("code")), (402, "capacity"))
        self.assertEqual(put(tok, "athletes", {"id": "ath0xyz", "ws": ws, "name": "ویرایش"})[0], 200, "ویرایش مجاز است")

    def test_athlete_cannot_write_coach_data(self):
        tok, _, _ = login()
        self.assertEqual(put(tok, "programs", {"id": "zzzz9999"})[0], 403)


class T4_AthleteFlow(unittest.TestCase):
    def test_full_flow(self):
        ctok, r, _ = coach()
        ws = r["items"]["workspaces"][0]["id"]
        aphone = new_phone()
        put(ctok, "athletes", {"id": "athflow1", "ws": ws, "name": "علی", "phone": aphone, "status": "pending"})
        put(ctok, "programs", {"id": "progflow", "ws": ws, "name": "حجم", "assigned": ["athflow1"],
                               "days": [{"title": "روز ۱", "ex": []}]})
        put(ctok, "diets", {"id": "dietflow", "ws": ws, "name": "کات", "assigned": ["athflow1"], "meals": []})
        put(ctok, "programs", {"id": "progother", "ws": ws, "name": "مال کس دیگر", "assigned": []})
        atok, a, _ = login(aphone)
        self.assertEqual(a["user"]["role"], "athlete")
        self.assertEqual(a["user"]["name"], "علی")
        home = a["athleteOf"][0]
        self.assertEqual([p["id"] for p in home["programs"]], ["progflow"], "فقط برنامه‌های اختصاص‌یافته")
        self.assertEqual(len(home["diets"]), 1)
        self.assertEqual(home["athlete"]["status"], "active")
        st, s = call("POST", "/api/athlete/log", {"athleteId": "athflow1", "programId": "progflow", "day": "روز ۱"}, atok)
        self.assertEqual(st, 200)
        b = call("GET", "/api/bootstrap", token=ctok)[1]
        self.assertEqual(len(b["items"]["sessions"]), 1)
        self.assertGreaterEqual(len(b["items"]["notifs"]), 3)
        self.assertEqual(call("POST", "/api/athlete/photo", {"photo": PHOTO}, atok)[0], 200)
        b = call("GET", "/api/bootstrap", token=ctok)[1]
        self.assertEqual(b["items"]["athletes"][0]["photo"], PHOTO)

    def test_athlete_cannot_log_for_others(self):
        ctok, r, _ = coach()
        ws = r["items"]["workspaces"][0]["id"]
        put(ctok, "athletes", {"id": "athoth01", "ws": ws, "name": "x", "phone": new_phone()})
        otok, _, _ = login()
        self.assertEqual(call("POST", "/api/athlete/log", {"athleteId": "athoth01"}, otok)[0], 403)

    def test_bad_photo_rejected(self):
        tok, _, _ = login()
        self.assertEqual(call("POST", "/api/athlete/photo", {"photo": "javascript:alert(1)"}, tok)[0], 400)


class T5_AI(unittest.TestCase):
    def test_program_and_diet(self):
        tok, _, _ = coach()
        st, r = call("POST", "/api/ai/program", {"goal": "عضله‌سازی", "days": "3"}, tok)
        self.assertEqual(st, 200)
        self.assertEqual(r["program"]["days"][0]["ex"][0]["n"], "اسکوات")
        st, r = call("POST", "/api/ai/diet", {"goal": "کاهش وزن"}, tok)
        self.assertEqual((st, r["diet"]["kcal"]), (200, "2000"))
        h = AI_CALLS[-1]["headers"]
        self.assertEqual(h.get("x-api-key"), "test-key")
        self.assertEqual(h.get("anthropic-version"), "2023-06-01")

    def test_ai_coach_only(self):
        tok, _, _ = login()
        self.assertEqual(call("POST", "/api/ai/program", {}, tok)[0], 403)

    def test_analyze_by_athlete_saves_for_coach(self):
        ctok, r, _ = coach()
        ws = r["items"]["workspaces"][0]["id"]
        aphone = new_phone()
        put(ctok, "athletes", {"id": "athai001", "ws": ws, "name": "سارا", "phone": aphone})
        atok, _, _ = login(aphone)
        st, res = call("POST", "/api/ai/analyze", {"photo": PHOTO, "athleteId": "athai001", "height": "170"}, atok)
        self.assertEqual(st, 200)
        self.assertTrue(res["saved"])
        img = AI_CALLS[-1]["body"]["messages"][0]["content"][0]
        self.assertEqual(img["type"], "image")
        an = call("GET", "/api/bootstrap", token=ctok)[1]["items"]["analyses"]
        self.assertEqual(len(an), 1)
        self.assertNotIn("photo", an[0], "عکس نباید ذخیره شود")

    def test_chat(self):
        ctok, r, _ = coach()
        ws = r["items"]["workspaces"][0]["id"]
        aphone = new_phone()
        put(ctok, "athletes", {"id": "athchat1", "ws": ws, "name": "رضا", "phone": aphone})
        atok, _, _ = login(aphone)
        st, res = call("POST", "/api/ai/chat", {"athleteId": "athchat1", "messages": [{"role": "user", "content": "سلام"}]}, atok)
        self.assertEqual((st, res["reply"]), (200, "جواب تست"))

    def test_daily_limit(self):
        tok, _, _ = coach()
        old = app.AI_DAILY_LIMIT
        app.AI_DAILY_LIMIT = 2
        try:
            codes = [call("POST", "/api/ai/diet", {}, tok)[0] for _ in range(3)]
        finally:
            app.AI_DAILY_LIMIT = old
        self.assertEqual(codes, [200, 200, 429])


class T6_Extensibility(unittest.TestCase):
    def test_new_route_works(self):
        @app.route("POST", "test/echo/*")
        def r_echo(c):
            return {"arg": c.args[0], "body": c.body, "phone": c.user["phone"]}
        tok, _, phone = login()
        st, r = call("POST", "/api/test/echo/abc", {"x": 1}, tok)
        self.assertEqual((st, r["arg"], r["body"]["x"], r["phone"]), (200, "abc", 1, phone))
        self.assertEqual(call("POST", "/api/test/echo/abc", {})[0], 401)


if __name__ == "__main__":
    res = unittest.main(verbosity=2, exit=False).result
    sys.exit(0 if res.wasSuccessful() else 1)
