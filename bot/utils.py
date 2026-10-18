import html
import re
from functools import lru_cache
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)

HOSTS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "instagram.com": "Instagram", "instagr.am": "Instagram",
    "tiktok.com": "TikTok",
    "twitter.com": "X", "x.com": "X",
    "facebook.com": "Facebook", "fb.watch": "Facebook", "fb.com": "Facebook",
    "reddit.com": "Reddit", "redd.it": "Reddit",
    "pinterest.com": "Pinterest", "pin.it": "Pinterest",
    "soundcloud.com": "SoundCloud", "vimeo.com": "Vimeo",
    "twitch.tv": "Twitch", "snapchat.com": "Snapchat",
    "threads.net": "Threads", "threads.com": "Threads",
    "dailymotion.com": "Dailymotion", "bilibili.com": "Bilibili",
    "likee.video": "Likee", "ok.ru": "OK", "vk.com": "VK",
    "tumblr.com": "Tumblr", "streamable.com": "Streamable",
    "pornhub.com": "Pornhub", "pornhub.org": "Pornhub", "pornhubpremium.com": "Pornhub",
    "xvideos.com": "XVideos", "xnxx.com": "XNXX", "xhamster.com": "xHamster",
    "redtube.com": "RedTube", "youporn.com": "YouPorn", "spankbang.com": "SpankBang",
    "eporner.com": "Eporner", "tube8.com": "Tube8", "thumbzilla.com": "Thumbzilla",
    "aparat.com": "Aparat", "namasha.com": "Namasha", "rumble.com": "Rumble",
    "kick.com": "Kick", "bandcamp.com": "Bandcamp", "mixcloud.com": "Mixcloud",
    "music.youtube.com": "YouTube Music", "9gag.com": "9GAG", "imgur.com": "Imgur",
    "coub.com": "Coub", "telegram.org": None,
}
HOSTS = {k: v for k, v in HOSTS.items() if v}

esc = html.escape


def extract_urls(text):
    out = []
    for m in URL_RE.findall(text or ""):
        m = m.rstrip(".,;:!?)]}»›")
        if m not in out:
            out.append(m)
    return out


def host_of(url):
    return (urlparse(url).hostname or "").lower()


def known_platform(url):
    h = host_of(url)
    for dom, name in HOSTS.items():
        if h == dom or h.endswith("." + dom):
            return name
    return None


_IES = None


@lru_cache(maxsize=2048)
def ytdlp_supported(url):
    """True اگه yt-dlp یه extractor اختصاصی (غیر Generic) برای این لینک داشته باشه."""
    global _IES
    try:
        if _IES is None:
            from yt_dlp.extractor import gen_extractor_classes
            _IES = [ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic"]
        return any(ie.suitable(url) for ie in _IES)
    except Exception:
        return False


def group_accepts(url, accept_all=False):
    """توی گروه: سایت‌های شناخته‌شده + هر سایتی که yt-dlp پشتیبانی می‌کنه (Pornhub و ۱۸۰۰ سایت دیگه)."""
    if accept_all:
        return True
    return bool(known_platform(url) or ytdlp_supported(url))


def norm_url(u):
    u = (u or "").strip().rstrip(".,;:!?)]}»›")
    if u and not re.match(r"(?i)^[a-z][a-z0-9+.-]*://", u):
        u = "https://" + u
    return u


def platform_of(url):
    return known_platform(url) or (host_of(url).removeprefix("www.") or "Other")


def human_size(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def human_dur(s):
    s = int(s or 0)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def bar(pct, n=10):
    k = max(0, min(n, int(pct / 100 * n)))
    return "▰" * k + "▱" * (n - k)


# ---------- تاریخ/ساعت (به وقت BOT_TZ، شمسی یا میلادی) ----------
def _g2j(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def fmt_ts(ts, date=True, sec=False):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from . import config
    if not ts:
        return "—"
    d = datetime.fromtimestamp(int(ts), ZoneInfo(config.TZ_NAME))
    t = d.strftime("%H:%M:%S" if sec else "%H:%M")
    if not date:
        return t
    if config.JALALI:
        y, m, dd = _g2j(d.year, d.month, d.day)
        return f"{y}/{m:02d}/{dd:02d} {t}"
    return d.strftime("%Y-%m-%d ") + t


def q_label(q):
    return {"a": "MP3", "b": "best"}.get(q, f"{q}p" if q else "-")
