#!/usr/bin/env bash
# نصب / به‌روزرسانی باشگینو روی سرور اوبونتو
# اجرا:  bash install.sh
# اجرای دوباره = به‌روزرسانی (تنظیمات و اطلاعات حفظ می‌شود)
set -euo pipefail

REPO="${REPO:-emlaksahibinden4-a11y/bashgino}"
BRANCH="${BRANCH:-main}"
DIR=/opt/bashgino
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH"

if [ "$(id -u)" != "0" ]; then echo "لطفاً با کاربر root اجرا کنید."; exit 1; fi

echo "==> نصب پیش‌نیازها (python3, caddy)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null
apt-get install -y python3 curl ca-certificates >/dev/null
if ! command -v caddy >/dev/null 2>&1; then
  if ! apt-get install -y caddy >/dev/null 2>&1; then
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y >/dev/null && apt-get install -y caddy >/dev/null
  fi
fi

echo "==> دریافت آخرین نسخه برنامه از گیت‌هاب ($REPO)..."
mkdir -p "$DIR/data"
STAGE=$(mktemp -d)
curl -fsSL "$RAW/app.py" -o "$STAGE/app.py"
curl -fsSL "$RAW/index.html" -o "$STAGE/index.html"
if curl -fsSL "$RAW/test_app.py" -o "$STAGE/test_app.py" 2>/dev/null; then
  echo "==> اجرای تست‌های خودکار روی نسخه‌ی جدید..."
  if ! (cd "$STAGE" && python3 test_app.py >"$STAGE/test.log" 2>&1); then
    echo "❌ تست‌ها رد شدند؛ نسخه‌ی جدید نصب نشد و نسخه‌ی قبلی همچنان کار می‌کند."
    tail -n 30 "$STAGE/test.log"
    rm -rf "$STAGE"; exit 1
  fi
  echo "   ✅ همه‌ی تست‌ها قبول شدند."
  cp "$STAGE/test_app.py" "$DIR/test_app.py"
else
  python3 -m py_compile "$STAGE/app.py"
fi
mv "$STAGE/app.py" "$DIR/app.py"
mv "$STAGE/index.html" "$DIR/index.html"
rm -rf "$STAGE"
# عکس‌های اختیاری (اگر در گیت‌هاب گذاشته باشید): hero.jpg, workout.jpg, food.jpg, login.jpg
mkdir -p "$DIR/static"
for f in hero workout food login; do
  curl -fsSL "$RAW/$f.jpg" -o "$DIR/static/$f.jpg.new" 2>/dev/null && mv "$DIR/static/$f.jpg.new" "$DIR/static/$f.jpg" || rm -f "$DIR/static/$f.jpg.new"
done

if [ ! -f "$DIR/.env" ]; then
  echo
  echo "==> تنظیمات اولیه (فقط بار اول پرسیده می‌شود)"
  read -r -p "دامنه پنل [app.bashgino.com]: " DOMAIN </dev/tty; DOMAIN=${DOMAIN:-app.bashgino.com}
  read -r -p "توکن ربات تلگرام (از BotFather): " BOT_TOKEN </dev/tty
  read -r -p "کلید API کاوه‌نگار (اگر هنوز ندارید Enter بزنید): " KAVE </dev/tty
  read -r -p "نام الگوی کاوه‌نگار [bashgino-otp]: " KTPL </dev/tty; KTPL=${KTPL:-bashgino-otp}
  read -r -p "یوزرنیم پشتیبانی تلگرام بدون @ (اختیاری): " SUPPORT </dev/tty
  read -r -p "کلید API هوش مصنوعی Claude (اگر هنوز ندارید Enter بزنید): " AIKEY </dev/tty
  DEV=0; [ -z "$KAVE" ] && DEV=1
  cat > "$DIR/.env" <<EOF
DOMAIN=$DOMAIN
BOT_TOKEN=$BOT_TOKEN
KAVENEGAR_API_KEY=$KAVE
KAVENEGAR_TEMPLATE=$KTPL
SUPPORT_USERNAME=$SUPPORT
# هوش مصنوعی (Claude): کلید از console.anthropic.com
AI_API_KEY=$AIKEY
AI_MODEL=claude-sonnet-5-5
# حداکثر استفاده‌ی هر کاربر از هوش مصنوعی در روز
AI_DAILY_LIMIT=30
# وقتی 1 باشد کد ورود در لاگ سرور هم نوشته می‌شود (فقط برای تست، بعد از فعال شدن پیامک 0 کنید)
OTP_DEV=$DEV
HOST=127.0.0.1
PORT=8080
EOF
  chmod 600 "$DIR/.env"
fi
DOMAIN=$(grep '^DOMAIN=' "$DIR/.env" | cut -d= -f2)

echo "==> ساخت سرویس..."
cat > /etc/systemd/system/bashgino.service <<EOF
[Unit]
Description=Bashgino (API + bot)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:8080
}
EOF

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

systemctl daemon-reload
systemctl enable bashgino >/dev/null 2>&1
systemctl restart bashgino
systemctl enable caddy >/dev/null 2>&1
systemctl restart caddy
sleep 3

if curl -fsS http://127.0.0.1:8080/api/health >/dev/null; then
  echo
  echo "✅ باشگینو روشن شد:  https://$DOMAIN"
  echo "   (اگر دامنه را تازه تنظیم کرده‌اید، تا چند دقیقه طول می‌کشد تا https فعال شود)"
  grep -q '^OTP_DEV=1' "$DIR/.env" && echo "   حالت تست فعال است؛ برای دیدن کد ورود:  journalctl -u bashgino -f | grep OTP"
else
  echo "❌ سرویس بالا نیامد. خطا:"; journalctl -u bashgino -n 30 --no-pager
fi
