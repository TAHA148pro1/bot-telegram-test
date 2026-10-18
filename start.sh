#!/bin/sh
WLOG=/tmp/warp.log
WSTATE=/tmp/warp.state
: > "$WLOG"
echo starting > "$WSTATE"
wlog() { echo "[warp] $*" | tee -a "$WLOG"; }

if [ "${AUTO_UPDATE_YTDLP:-1}" = "1" ]; then
  timeout 120 pip install --no-cache-dir -U "yt-dlp[default,curl-cffi]" >/dev/null 2>&1 || echo "yt-dlp update skipped"
fi

warp_ok() {
  curl -s -m 10 --socks5-hostname 127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace 2>>"$WLOG" | grep -Eq 'warp=(on|plus)'
}

start_warp() {
  command -v wgcf >/dev/null 2>&1 && command -v wireproxy >/dev/null 2>&1 || { wlog "wgcf/wireproxy not installed"; return 1; }
  D="${DATA_DIR:-/app/data}/warp"; mkdir -p "$D"; cd "$D" || return 1

  # 1) کانفیگ WireGuard آماده از متغیر (اگر ثبت‌نام خودکار کلادفلر روی IP سرور بلاک بود)
  if [ -n "$WG_CONF_B64" ]; then
    echo "$WG_CONF_B64" | base64 -d > wgcf-profile.conf 2>>"$WLOG" && wlog "using WG_CONF_B64"
  fi
  # 2) ثبت‌نام خودکار
  if [ ! -f wgcf-profile.conf ]; then
    if [ ! -f wgcf-account.toml ]; then
      n=0
      while [ $n -lt 5 ]; do
        n=$((n+1))
        wlog "wgcf register attempt $n"
        wgcf register --accept-tos >>"$WLOG" 2>&1 && break
        sleep 6
      done
    fi
    [ -f wgcf-account.toml ] || { wlog "register failed: $(tail -n 3 "$WLOG" | tr '\n' ' ')"; return 1; }
    wgcf generate >>"$WLOG" 2>&1 || { wlog "generate failed"; return 1; }
  fi
  [ -f wgcf-profile.conf ] || { wlog "no wgcf-profile.conf"; return 1; }

  PRIV=$(grep -m1 -E '^PrivateKey' wgcf-profile.conf | sed 's/^PrivateKey *= *//')
  ADDR=$(grep -m1 -E '^Address' wgcf-profile.conf | sed -E 's/^Address *= *//; s/,.*//')
  PUB=$(grep -m1 -E '^PublicKey' wgcf-profile.conf | sed 's/^PublicKey *= *//')
  [ -n "$PRIV" ] && [ -n "$ADDR" ] && [ -n "$PUB" ] || { wlog "profile parse failed"; return 1; }

  # چند endpoint/پورت امتحان می‌شود (بعضی شبکه‌ها پورت 2408 را می‌بندند)
  for EP in 162.159.192.1:2408 162.159.192.1:500 162.159.192.1:4500 162.159.192.1:1701 engage.cloudflareclient.com:2408; do
    CONF="$D/wireproxy.conf"
    cat > "$CONF" <<EOT
[Interface]
PrivateKey = $PRIV
Address = $ADDR
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = $PUB
AllowedIPs = 0.0.0.0/0
Endpoint = $EP
PersistentKeepalive = 25

[Socks5]
BindAddress = 127.0.0.1:40000
EOT
    wlog "trying endpoint $EP"
    wireproxy -c "$CONF" >>"$WLOG" 2>&1 &
    WP=$!
    t=0
    while [ $t -lt 8 ]; do
      sleep 2; t=$((t+1))
      kill -0 "$WP" 2>/dev/null || break
      if warp_ok; then
        wlog "WARP is up via $EP"
        echo up > "$WSTATE"
        # watchdog: wireproxy کرش کنه دوباره بالا میاد و وضعیت (up/down) به بات گزارش میشه
        ( while true; do
            sleep 15
            if ! kill -0 "$WP" 2>/dev/null; then wireproxy -c "$CONF" >>"$WLOG" 2>&1 & WP=$!; sleep 3; fi
            if warp_ok; then echo up > "$WSTATE"; else echo down > "$WSTATE"; fi
          done ) &
        return 0
      fi
    done
    kill "$WP" 2>/dev/null; wait "$WP" 2>/dev/null
  done
  wlog "all endpoints failed. last log: $(tail -n 5 "$WLOG" | tr '\n' ' ')"
  echo down > "$WSTATE"
  return 1
}

# WARP توی پس‌زمینه بالا میاد؛ بات منتظرش نمی‌مونه (قبلاً تا ۱۰۰ ثانیه استارت رو قفل می‌کرد)
if [ "${ENABLE_WARP:-1}" = "1" ]; then
  ( start_warp || echo "[warp] continuing WITHOUT warp" ) &
else
  echo off > "$WSTATE"
fi
cd /app

# ---------- Local Bot API Server (ارسال فایل تا 2GB) ----------
# فقط کافیه TG_API_ID و TG_API_HASH (از my.telegram.org) رو ست کنین؛ سرور داخل همین کانتینر بالا میاد.
API_ID="${TG_API_ID:-$TELEGRAM_API_ID}"
API_HASH="${TG_API_HASH:-$TELEGRAM_API_HASH}"
if [ -z "$LOCAL_API_URL" ] && [ -n "$API_ID" ] && [ -n "$API_HASH" ]; then
  if command -v telegram-bot-api >/dev/null 2>&1; then
    TD="${DATA_DIR:-/app/data}/tgapi"; mkdir -p "$TD" /tmp/tgapi-tmp
    ( while true; do
        telegram-bot-api --api-id="$API_ID" --api-hash="$API_HASH" --local \
          --http-ip-address=127.0.0.1 --http-port=8081 \
          --dir="$TD" --temp-dir=/tmp/tgapi-tmp --verbosity=1
        echo "[tgapi] exited, restarting in 3s"; sleep 3
      done ) &
    i=0
    while [ $i -lt 30 ]; do
      i=$((i+1))
      curl -s -m 2 -o /dev/null http://127.0.0.1:8081/ && break
      sleep 1
    done
    export LOCAL_API_URL="http://127.0.0.1:8081"
    echo "[tgapi] local Bot API server on $LOCAL_API_URL (upload limit 2GB)"
  else
    echo "[tgapi] telegram-bot-api binary not found; staying on cloud API (49MB)"
  fi
elif [ -z "$LOCAL_API_URL" ]; then
  echo "[tgapi] TG_API_ID/TG_API_HASH not set -> cloud API, upload limit 49MB"
fi

exec python -m bot.main
