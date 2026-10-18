import base64
import os
import re
import time
from pathlib import Path


def _ints(s):
    return {int(x) for x in re.findall(r"-?\d+", s or "")}


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _data_dir() -> Path:
    for cand in (os.getenv("DATA_DIR") or "/app/data", "./data"):
        try:
            p = Path(cand)
            p.mkdir(parents=True, exist_ok=True)
            t = p / ".w"
            t.write_text("1")
            t.unlink()
            return p
        except Exception:
            continue
    return Path("/tmp")


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = _ints(os.getenv("ADMIN_IDS", ""))
TZ_NAME = os.getenv("BOT_TZ", "Asia/Tehran")
DATA_DIR = _data_dir()

MAX_CONCURRENT = max(1, _int("MAX_CONCURRENT", 3))
USER_MAX_ACTIVE = max(1, _int("USER_MAX_ACTIVE", 2))
DAILY_LIMIT = _int("DAILY_LIMIT", 0)
MAX_DURATION_MIN = _int("MAX_DURATION_MIN", 120)
DL_TIMEOUT = _int("DL_TIMEOUT", 3600)  # فایل‌های ۲ گیگی زمان بیشتری لازم دارن
SEND_TIMEOUT = _int("SEND_TIMEOUT", 3600)
# توی گروه به هر لینکی جواب بده (حتی سایت‌هایی که yt-dlp extractor اختصاصی نداره)
GROUP_ALL_LINKS = os.getenv("GROUP_ALL_LINKS", "0") == "1"
COLORED_BUTTONS = os.getenv("COLORED_BUTTONS", "1") == "1"

LOCAL_API_URL = os.getenv("LOCAL_API_URL", "").strip()
# start.sh اگه TG_API_ID/TG_API_HASH ست باشه، Local Bot API Server رو داخل همین کانتینر بالا میاره
# و LOCAL_API_URL=http://127.0.0.1:8081 رو export می‌کنه → سقف ارسال 2GB
IS_LOCAL_API = bool(LOCAL_API_URL)
UPLOAD_LIMIT = (1999 if LOCAL_API_URL else 49) * 1024 * 1024
PROXY = os.getenv("PROXY", "").strip() or None
# پروکسی(های) مخصوص یوتیوب، با کاما جدا کنین؛ مثال: http://user:pass@1.2.3.4:8080
PROXY_YT = [x for x in re.split(r"[,\s]+", os.getenv("PROXY_YT", "")) if x]
# WARP حالا در پس‌زمینه بالا میاد و بات منتظرش نمی‌مونه؛ هر وقت وصل شد خودکار استفاده میشه.
ENABLE_WARP = os.getenv("ENABLE_WARP", "1") == "1"
WARP_PROXY = os.getenv("WARP_PROXY", "").strip() or "socks5://127.0.0.1:40000"
WARP_STATE = Path(os.getenv("WARP_STATE", "/tmp/warp.state"))
_warp_cache = [0.0, False]


def warp_state() -> str:
    """starting | up | down | off"""
    if not ENABLE_WARP:
        return "off"
    try:
        return WARP_STATE.read_text().strip() or "starting"
    except Exception:
        return "starting"


def warp_up() -> bool:
    now = time.time()
    if now - _warp_cache[0] > 5:
        _warp_cache[0], _warp_cache[1] = now, warp_state() == "up"
    return _warp_cache[1]


def yt_proxies():
    """پروکسی‌های فعال برای یوتیوب/سایت‌های بلاک‌شده (WARP فقط وقتی واقعاً وصله)."""
    out = list(PROXY_YT)
    if warp_up() and WARP_PROXY not in out:
        out.append(WARP_PROXY)
    return out


JALALI = os.getenv("JALALI_DATES", "1") == "1"

POT_URL = os.getenv("POT_PROVIDER_URL", "").strip()  # سرویس bgutil برای PO Token یوتیوب (اختیاری)

COOKIES_FILE = None
_b64 = os.getenv("COOKIES_B64", "").strip()
if (DATA_DIR / "cookies.txt").exists():
    COOKIES_FILE = str(DATA_DIR / "cookies.txt")  # کوکی آپلود شده از پنل ادمین اولویت دارد
elif _b64 or os.getenv("COOKIES_TXT"):
    try:
        _p = DATA_DIR / "cookies.txt"
        _p.write_bytes(base64.b64decode(_b64) if _b64 else os.getenv("COOKIES_TXT", "").replace("\\n", "\n").encode())
        COOKIES_FILE = str(_p)
    except Exception:
        COOKIES_FILE = None
