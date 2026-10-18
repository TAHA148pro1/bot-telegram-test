import collections
import copy
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled, match_filter_func

from . import config

log = logging.getLogger("dl")

# سایت‌هایی مثل Pornhub بدون TLS-impersonation (curl_cffi) 403 می‌دن
try:
    import curl_cffi  # noqa: F401
    from yt_dlp.networking.impersonate import ImpersonateTarget
    _IMP = ImpersonateTarget.from_str("chrome")
except Exception:  # noqa
    _IMP = None
LAST_ERRORS = collections.deque(maxlen=5)
TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def _note(url, e):
    LAST_ERRORS.append((time.time(), url[:80], str(e).replace("\n", " ")[:260]))

IMG = {".jpg", ".jpeg", ".png", ".webp"}
AUD = {".mp3", ".m4a", ".opus", ".ogg", ".aac"}
SKIP = {".part", ".ytdl", ".json", ".vtt", ".srt", ".temp"}
TIERS = [1080, 720, 480, 360, 240]


class DlError(Exception):
    """Error with a user-friendly Persian message."""

    def __init__(self, msg, cookie=False):
        super().__init__(msg)
        self.cookie = cookie


# ---------- مسیرهای دانلود (کلاینت یوتیوب × پروکسی × کوکی) ----------
# بدون کوکی: کلاینت‌هایی که لاگین/PO Token کمتری لازم دارن
YT_CLIENTS = [None, ["tv_simply"], ["android_vr"], ["web_embedded"], ["mweb"]]
# با کوکی: فقط کلاینت‌هایی که کوکی رو قبول می‌کنن (android_vr/tv_simply کوکی رو نادیده می‌گیرن)
YT_COOKIE_CLIENTS = [None, ["web_safari"], ["mweb"]]  # همون ترکیبی که توی نسخه‌ی سالم کار می‌کرد
# اگه کوکی خراب/منقضی باشه، آخرین شانس بدون کوکی
YT_NOCOOKIE_FALLBACK = [["tv_simply"], ["android_vr"]]

_YT_RE = re.compile(r"(?i)(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$")
_GOOD: dict = {}  # family -> آخرین مسیر موفق (اول همون امتحان میشه → سریع‌تر)
_CK_LOCK = threading.Lock()


def is_yt(url):
    try:
        return bool(_YT_RE.search((urlparse(url).hostname or "").lower()))
    except Exception:
        return "youtu" in (url or "").lower()


def _fam(url):
    return "yt" if is_yt(url) else (urlparse(url).hostname or "").lower().removeprefix("www.")


def _attempts(url):
    """لیست (client, proxy, use_cookie) برای امتحان پشت‌سرهم."""
    proxies = config.yt_proxies()
    if not is_yt(url):
        # سایت‌های غیر یوتیوب: اول مستقیم، اگه 403/محدودیت منطقه‌ای بود با پروکسی/WARP
        routes = [(None, None, True)] + [(None, p, True) for p in proxies]
    else:
        routes = []
        has_ck = bool(config.COOKIES_FILE)
        for p in proxies + [None]:  # آخرین تلاش: مستقیم
            if has_ck:
                routes += [(tuple(c) if c else None, p, True) for c in YT_COOKIE_CLIENTS]
            else:
                routes += [(tuple(c) if c else None, p, False) for c in YT_CLIENTS]
        if has_ck:  # فقط یک بار، آخر صف: اگه کوکی باطل شده بود
            routes += [(tuple(c), None, False) for c in YT_NOCOOKIE_FALLBACK]
    good = _GOOD.get(_fam(url))
    if good in routes:
        routes.remove(good)
        routes.insert(0, good)
    return routes


def is_block(e):
    m = str(e).lower()
    return ("sign in to confirm" in m or "not a bot" in m or "confirm you" in m
            or "login required" in m or "use --cookies" in m or ("cookies" in m and "sign in" in m))


def is_forbidden(e):
    m = str(e).lower()
    return any(k in m for k in ("403", "forbidden", "451", "legal reasons", "your country",
                                "your region", "geo", "cloudflare", "unable to download webpage"))


def is_redirect(e):
    """Pornhub & co: سرور به صفحه‌ی تأیید سن/بلاک منطقه‌ای/لاگین ریدایرکت می‌شه."""
    m = str(e).lower()
    return any(k in m for k in ("redirection detected", "require login", "requires login",
                                "age verification", "verify your age", "not available in your",
                                "http error 302", "http error 429"))


# خطاهایی که با عوض کردن کلاینت/پروکسی درست نمیشن → بی‌خود تلاش نکن
_YT_FINAL = ("private video", "video is private", "has been removed", "account associated",
             "copyright", "members-only", "join this channel", "premieres in", "live event",
             "is live", "unsupported url", "does not pass filter", "is not a valid url",
             "incomplete youtube id", "playlist does not exist", "larger than max-filesize")


def _retryable(url, e):
    m = str(e).lower()
    if is_yt(url):
        if any(k in m for k in _YT_FINAL):
            return False
        # فقط خطاهایی که با کلاینت/مسیر دیگه درست میشن؛ تلاش بی‌خود = سوختن سشن کوکی
        return is_block(e) or any(k in m for k in (
            "requested format", "only images", "po token", "403", "forbidden",
            "429", "too many requests", "timed out", "timeout", "connection", "proxy", "socks"))
    return is_block(e) or is_forbidden(e) or is_redirect(e)


class _CookieJar:
    """کپی موقت کوکی برای هر تلاش؛ yt-dlp موقع بستن کوکی رو بازنویسی می‌کنه و
    چند دانلود همزمان روی یک فایل = خراب شدن/باطل شدن نشست یوتیوب. فقط بعد از موفقیت برمی‌گرده."""

    def __init__(self, use):
        self.src = config.COOKIES_FILE if use else None
        self.path = None

    def __enter__(self):
        if self.src and os.path.exists(self.src):
            fd, self.path = tempfile.mkstemp(prefix="ck_", suffix=".txt")
            os.close(fd)
            shutil.copyfile(self.src, self.path)
        return self.path

    def commit(self):
        if not (self.path and self.src):
            return
        try:
            data = Path(self.path).read_text(encoding="utf-8", errors="ignore")
            if data.count("\t") < 12 or self.src != config.COOKIES_FILE:
                return  # خالی/خراب یا ادمین در این فاصله کوکی رو عوض کرده
            if "\tSID\t" in Path(self.src).read_text(encoding="utf-8", errors="ignore") and "\tSID\t" not in data:
                return  # کوکی لاگین پاک شده؛ نسخه‌ی سالم قبلی رو خراب نکن
            with _CK_LOCK:
                tmp = self.src + ".new"
                Path(tmp).write_text(data, encoding="utf-8")
                os.replace(tmp, self.src)
        except Exception as ex:  # noqa
            log.debug("cookie commit: %s", ex)

    def __exit__(self, exc_type, *a):
        # همیشه برگردون (حتی اگه خطا داد): یوتیوب روی هر درخواست کوکی‌های PSIDTS/SIDCC رو می‌چرخونه؛
        # اگه نسخه‌ی جدید دور ریخته بشه، درخواست بعدی با کوکی کهنه میره و گوگل کل سشن رو باطل می‌کنه
        # (باگ اصلی v4). YoutubeDL قبل از این‌جا بسته شده و کوکی‌ها رو نوشته.
        self.commit()
        if self.path:
            try:
                os.unlink(self.path)
            except Exception:
                pass


@dataclass
class Result:
    path: Path
    title: str
    duration: int
    width: int
    height: int
    kind: str  # video | audio | photo
    size: int


def _base(client=None, proxy=None, url="", cookiefile=None):
    o = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "playlist_items": "1",
        "socket_timeout": 25, "retries": 5, "fragment_retries": 5, "extractor_retries": 2,
        "file_access_retries": 3, "concurrent_fragment_downloads": 4, "geo_bypass": True,
        "restrictfilenames": True, "js_runtimes": {"node": {}},
        "source_address": "0.0.0.0",  # فقط IPv4؛ IPv6 سرور امتیاز بدتری می‌گیرد
    }
    ea = {}
    if client:
        ea["youtube"] = {"player_client": list(client)}
    if config.POT_URL:
        ea["youtubepot-bgutilhttp"] = {"base_url": [config.POT_URL]}
    if ea:
        o["extractor_args"] = ea
    if cookiefile:
        o["cookiefile"] = cookiefile
    if _IMP and not is_yt(url):
        o["impersonate"] = _IMP
    if proxy or config.PROXY:
        o["proxy"] = proxy or config.PROXY
    return o


def _first(info):
    if info and info.get("entries"):
        for e in info["entries"]:
            if e:
                return e
    return info


def _ok(url, route):
    _GOOD[_fam(url)] = route


def _bad(url, route):
    if _GOOD.get(_fam(url)) == route:
        _GOOD.pop(_fam(url), None)


def fetch_info(url, budget=40):
    last, t0 = None, time.time()
    for route in _attempts(url):
        if time.time() - t0 > budget:
            break
        client, proxy, ck = route
        jar = _CookieJar(ck)
        try:
            with jar as cf, YoutubeDL({**_base(client, proxy, url, cf), "skip_download": True}) as y:
                info = _first(y.extract_info(url, download=False))
            if info and is_yt(url) and not any(f.get("vcodec") not in (None, "none") or f.get("acodec") not in (None, "none")
                                              for f in info.get("formats") or [{"vcodec": "x"}]):
                raise RuntimeError("Only images are available")  # فقط storyboard → مسیر بعدی
            _ok(url, route)
            if info is not None:
                info["_route"], info["_ts"] = route, time.time()
            return info
        except Exception as e:  # noqa
            last = e
            _note(url, e)
            _bad(url, route)
            if not _retryable(url, e):
                break
    raise last or RuntimeError("timeout")


def selector(q):
    if q == "a":
        return "bestaudio[ext=m4a]/bestaudio/best"
    if q == "b":
        return "b[ext=mp4]/bv*+ba/b"
    h = int(q)
    return (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/"
            f"b[height<={h}][ext=mp4]/b[height<={h}]/b")


def _fsize(f):
    return f.get("filesize") or f.get("filesize_approx") or 0


def quality_options(info):
    """-> (has_video, [(q, size_estimate)])"""
    fmts = info.get("formats") or []
    vids = [f for f in fmts if f.get("vcodec") != "none" and f.get("height")]
    if not vids:
        has_v = bool(fmts) and any(f.get("vcodec") != "none" and f.get("ext") not in ("m4a", "mp3", "opus", "ogg", "aac")
                                       for f in fmts)
        return has_v, ([("b", 0)] if has_v else [])
    mh = max(f["height"] for f in vids)
    heights = [t for t in TIERS if t <= mh]
    if mh < 1080 and mh not in heights:
        heights.insert(0, mh)
    if not heights:
        heights = [mh]
    audio = max([_fsize(f) for f in fmts if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")] or [0])
    dur = info.get("duration") or 0
    out, seen = [], set()
    for h in heights:
        c = [f for f in vids if f["height"] <= h]
        if not c:
            continue
        best = max(c, key=lambda f: (f["height"], f.get("tbr") or 0))
        if best["height"] in seen:
            continue
        seen.add(best["height"])
        size = _fsize(best)
        if not size and dur and best.get("tbr"):
            size = int(best["tbr"] * 125 * dur)
        if size and best.get("acodec") in (None, "none"):
            size += audio
        out.append((str(best["height"] if best["height"] < h else h), size))
    return True, out


def fallback_chain(q):
    if q in ("a", "b"):
        return [q]
    h = int(q)
    return [q] + [str(x) for x in TIERS if x < h]


class TooBig(Exception):
    def __init__(self, size):
        super().__init__(f"too big: {size}")
        self.size = size


def friendly_error(e):
    m = str(e).lower()
    if is_redirect(e):
        return ("ارباب، سایت مقصد سرور رو به صفحه‌ی تأیید سن/لاگین ریدایرکت کرد (معمولاً بلاک منطقه‌ای IP سرور). "
                "ادمین باید یه PROXY_YT غیرآمریکایی ست کنه یا کوکی لاگین‌شده‌ی همون سایت رو آپلود کنه. "
                "اگه با پروکسی هم نشد، احتمالاً ویدیو حذف شده.")
    if is_block(e):
        return ("ارباب، یوتیوب/پلتفرم تشخیص داد درخواست از سرور میاد و کوکی می‌خواد. "
                "ادمین باید از پنل /admin ← 🍪 کوکی‌ها یه کوکی جدید آپلود کنه.")
    if "requested format" in m or "only images" in m or "po token" in m:
        return ("ارباب، یوتیوب فرمت قابل دانلود به سرور نداد (معمولاً نیاز به PO Token/کوکی تازه). "
                "از /admin ← 🩺 تشخیص وضعیت رو چک کنین یا کیفیت دیگه‌ای امتحان کنین.")
    if "403" in m or "forbidden" in m:
        return ("ارباب، سایت مقصد دسترسی سرور رو رد کرد (403). ادمین باید مطمئن بشه "
                "curl-cffi نصب هست و در صورت نیاز PROXY یا WARP فعاله.")
    if "private" in m:
        return "ارباب، این محتوا خصوصیه و قابل دانلود نیست."
    if "login" in m or "cookies" in m or "rate-limit" in m or "empty media response" in m:
        return "ارباب، این پلتفرم برای این لینک نیاز به ورود (کوکی) داره یا محدودیت زده."
    if "unsupported url" in m:
        return "ببخشید ارباب، این لینک پشتیبانی نمیشه."
    if "no video" in m or "there is no video" in m:
        return "ارباب، توی این لینک ویدیویی پیدا نشد."
    if "not available" in m or "unavailable" in m or "removed" in m or "deleted" in m:
        return "ارباب، این محتوا در دسترس نیست یا حذف شده."
    if "copyright" in m or "blocked" in m or "geo" in m:
        return "ارباب، این محتوا به دلیل محدودیت منطقه‌ای/کپی‌رایت در دسترس نیست."
    if "live" in m:
        return "ببخشید ارباب، لایو پشتیبانی نمیشه."
    if "timed out" in m or "timeout" in m or "connection" in m:
        return "ارباب، مشکل شبکه پیش اومد. دوباره امتحان کنین."
    return "ببخشید ارباب، دانلود ناموفق بود. لینک رو چک کنین یا کیفیت دیگه‌ای امتحان بفرمایین."


def _clean(outdir):
    for p in Path(outdir).iterdir():
        if p.is_file() and not p.name.startswith("thumb"):
            try:
                p.unlink()
            except Exception:
                pass


def _req_size(info):
    if not info:
        return 0
    rf = info.get("requested_formats") or []
    if rf:
        return sum(_fsize(f) for f in rf)
    return _fsize(info)


def download(url, q, outdir, prog, info=None, budget=240):
    def hook(d):
        if prog.get("cancel"):
            raise DownloadCancelled("cancelled")
        if d.get("status") == "downloading":
            prog["stage"] = "dl"
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            prog["pct"] = min(100.0, done / tot * 100) if tot else 0
            prog["speed"] = re.sub(r"\x1b\[[0-9;]*m", "", d.get("_speed_str") or "").strip()
        elif d.get("status") == "finished":
            prog["stage"] = "merge"

    def pp_hook(d):
        if d.get("status") == "started":
            prog["stage"] = "merge"

    def build(client, proxy, cf):
        o = _base(client, proxy, url, cf)
        o.update({
            "format": selector(q),
            "outtmpl": str(Path(outdir) / "%(id).60B.%(ext)s"),
            "merge_output_format": "mp4",
            "progress_hooks": [hook],
            "postprocessor_hooks": [pp_hook],
            # کمی بالاتر از سقف؛ حجم دقیق بعد از merge توی process چک میشه
            "max_filesize": int(config.UPLOAD_LIMIT * 1.08),
        })
        if config.MAX_DURATION_MIN > 0:
            o["match_filter"] = match_filter_func(f"!is_live & duration <=? {config.MAX_DURATION_MIN * 60}")
        if q == "a":
            o["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                    "preferredquality": "192"}]
        return o

    got, last = None, None
    # ۱) اگه همین الان اطلاعات با یه مسیر موفق گرفته شده، بدون استخراج دوباره دانلود کن (سریع‌تر)
    if info and info.get("_route") and time.time() - info.get("_ts", 0) < 1800:
        route = info["_route"]
        jar = _CookieJar(route[2])
        try:
            data = copy.deepcopy({k: v for k, v in info.items() if k not in ("_route", "_ts")})
            with jar as cf, YoutubeDL(build(route[0], route[1], cf)) as y:
                got = _first(y.process_ie_result(data, download=True)) or data
        except DownloadCancelled:
            raise
        except Exception as e:  # noqa
            log.info("fast path failed, full retry: %s", e)
            got = None
            _clean(outdir)
            if "larger than max-filesize" in str(e).lower():
                raise TooBig(_req_size(info) or config.UPLOAD_LIMIT * 2)

    # ۲) همه‌ی مسیرها به ترتیب
    if got is None:
        t0 = time.time()
        for route in _attempts(url):
            if time.time() - t0 > budget and last is not None:
                break
            client, proxy, ck = route
            jar = _CookieJar(ck)
            try:
                with jar as cf, YoutubeDL(build(client, proxy, cf)) as y:
                    got = _first(y.extract_info(url, download=True))
                _ok(url, route)
                last = None
                break
            except DownloadCancelled:
                raise
            except Exception as e:  # noqa
                last = e
                _clean(outdir)
                if "larger than max-filesize" in str(e).lower():
                    raise TooBig(_req_size(info) or config.UPLOAD_LIMIT * 2)
                _note(url, e)
                _bad(url, route)
                log.warning("download failed (client=%s proxy=%s cookie=%s) %s: %s", client, bool(proxy), ck, url, e)
                if not _retryable(url, e):
                    break
        if last is not None:
            raise DlError(friendly_error(last), cookie=is_block(last))

    files = [p for p in Path(outdir).iterdir()
             if p.is_file() and p.suffix.lower() not in SKIP and not p.name.startswith("thumb")]
    if not files:
        sz = _req_size(got)
        if sz > config.UPLOAD_LIMIT:
            raise TooBig(sz)  # yt-dlp به خاطر max_filesize رد کرد → کیفیت پایین‌تر
        raise DlError("ارباب، فایلی دریافت نشد (شاید ویدیو از حد مجاز طولانی‌تره یا لایوه).")
    p = max(files, key=lambda x: x.stat().st_size)
    ext = p.suffix.lower()
    kind = "photo" if ext in IMG else "audio" if ext in AUD or q == "a" else "video"
    got = got or {}
    return Result(p, (got.get("title") or "")[:200], int(got.get("duration") or 0),
                  int(got.get("width") or 0), int(got.get("height") or 0), kind, p.stat().st_size)


def fetch_thumb(info, outdir):
    """دانلود تامنیل و تبدیل به JPEG (تلگرام WebP/فرمت‌های عجیب را همیشه قبول نمی‌کند)."""
    url = info.get("thumbnail")
    if not url:
        th = [t for t in (info.get("thumbnails") or []) if t.get("url")]
        url = th[-1]["url"] if th else None
    if not url:
        return None
    raw, jpg = Path(outdir) / "thumb_raw", Path(outdir) / "thumb.jpg"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        raw.write_bytes(r.read(8 * 1024 * 1024))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-frames:v", "1",
                    "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(jpg)],
                   check=True, timeout=25)
    return jpg if jpg.exists() and 0 < jpg.stat().st_size < 5 * 1024 * 1024 else None


def warp_trace(proxy=None):
    cmd = ["curl", "-s", "-m", "12"]
    if proxy:
        cmd += ["-x", proxy.replace("socks5://", "socks5h://", 1)]
    cmd.append("https://www.cloudflare.com/cdn-cgi/trace")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except Exception as e:  # noqa
        return {"error": str(e)[:100]}
    d = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    return d or {"error": "no response"}


def diagnose(url=TEST_URL):
    """برای هر مسیر (پروکسی‌ها + مستقیم) تست استخراج اطلاعات یوتیوب. -> [(name, ok, client, err)]"""
    res = []
    for p in config.yt_proxies() + [None]:
        ok, used, err = False, None, ""
        for route in [r for r in _attempts(url) if r[1] == p]:
            client, _, ck = route
            jar = _CookieJar(ck)
            try:
                with jar as cf, YoutubeDL({**_base(client, p, url, cf), "skip_download": True}) as y:
                    info = y.extract_info(url, download=False)
                if not any(f.get("vcodec") not in (None, "none") for f in (info or {}).get("formats") or []):
                    raise RuntimeError("Only images are available (PO token?)")
                ok, used = True, ("+cookie " if ck else "") + (",".join(client) if client else "default")
                break
            except Exception as e:  # noqa
                err = str(e).replace("\n", " ")[:160]
        res.append((p or "direct", ok, used, err))
    return res
