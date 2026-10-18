import asyncio
import logging
import secrets
import shutil
import tempfile
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, LinkPreviewOptions, Message, ReplyParameters

from .. import config, downloader as dl
from ..db import db
from ..keyboards import quality_kb, start_kb
from ..utils import bar, esc, extract_urls, group_accepts, human_dur, human_size, norm_url, platform_of

log = logging.getLogger("user")
router = Router()
NOPREVIEW = LinkPreviewOptions(is_disabled=True)

CACHE: dict = {}
ACTIVE: dict = {}
TASKS: set = set()
SEM = asyncio.Semaphore(config.MAX_CONCURRENT)
_me = None
_last_cookie_alert = 0.0


async def notify_cookie(bot, url):
    global _last_cookie_alert
    if time.time() - _last_cookie_alert < 3600:
        return
    _last_cookie_alert = time.time()
    for aid in config.ADMIN_IDS:
        try:
            await bot.send_message(aid, "🍪 ارباب، دانلود از این لینک به خاطر کوکی ناموفق شد:\n"
                                        f"{esc(url[:200])}\n\nاز /admin ← 🍪 کوکی‌ها، کوکی جدید آپلود کنین.")
        except Exception:
            pass

START_TXT = ("👑 سلام ارباب! به خدمتگزارتون خوش اومدین.\n\nلینک ویدیو یا آهنگ رو بفرستین "
             "(یوتیوب، اینستاگرام، تیک‌تاک، X، فیسبوک، ردیت، ساندکلاود و ...) تا کیفیت‌ها رو تقدیمتون کنم.\n\n"
             "👥 توی گروه هم در خدمتم؛ فقط منو اضافه کنین و لینک بفرستین.")
HELP_TXT = ("📖 <b>راهنمای ارباب</b>\n\n1️⃣ لینک رو بفرستین\n2️⃣ کیفیت دلخواهتون رو بزنین\n3️⃣ فایل تقدیمتون میشه\n\n"
            "• برای فقط صدا: دکمه MP3\n• توی گروه: لینک بفرستین یا روی لینک ریپلای کنین و /dl بزنین\n"
            f"• حداکثر حجم ارسال: {human_size(config.UPLOAD_LIMIT)}")


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return t


async def me(bot):
    global _me
    if _me is None:
        _me = await bot.me()
    return _me


async def cleanup_loop():
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for k in [k for k, v in CACHE.items() if now - v["ts"] > 3600 and v["state"] != "busy"]:
            CACHE.pop(k, None)


async def _edit(bot, e, text, kb=None):
    try:
        if e.get("photo"):
            await bot.edit_message_caption(chat_id=e["chat_id"], message_id=e["menu_id"],
                                           caption=text, reply_markup=kb)
        else:
            await bot.edit_message_text(text, chat_id=e["chat_id"], message_id=e["menu_id"],
                                        reply_markup=kb, link_preview_options=NOPREVIEW)
    except Exception as ex:  # message not modified / deleted / flood
        log.debug("edit failed: %s", ex)


def menu_text(info, platform):
    if not info:
        return f"🔗 لینک <b>{platform}</b> رسید ارباب\n\nکیفیت مورد نظرتون رو انتخاب کنین 👇"
    t = f"🎬 <b>{esc((info.get('title') or '')[:120])}</b>\n"
    meta = [f"📺 {platform}"]
    if info.get("duration"):
        meta.insert(0, f"⏱ {human_dur(info['duration'])}")
    return t + " • ".join(meta) + "\n\nکیفیت مورد نظرتون رو انتخاب کنین ارباب 👇"


def build_kb(token, info):
    if not info:
        return quality_kb(token)
    has_v, opts = dl.quality_options(info)
    if not has_v:
        return quality_kb(token, audio_only=True)
    labeled = [(q, f"🎬 {q}p" + (f" • {human_size(s)}" if s else "") if q != "b" else "📥 دانلود ویدیو")
               for q, s in opts]
    return quality_kb(token, labeled)


@router.message(CommandStart())
async def cmd_start(m: Message, bot: Bot):
    if m.chat.type == "private":
        await m.answer(START_TXT, reply_markup=start_kb((await me(bot)).username))
    else:
        await m.reply("✅ در خدمتتونم ارباب! لینک بفرستین تا منوی دانلود بیاد.")


@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(HELP_TXT)


@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.answer()
    if c.message:
        await c.message.answer(HELP_TXT)


def msg_urls(msg):
    """لینک‌ها از متن + entityهای تلگرام (url بدون http مثل pornhub.com/... و text_link)."""
    if not msg:
        return []
    text = msg.text or msg.caption or ""
    urls = [norm_url(u) for u in extract_urls(text)]
    for ent in (msg.entities or msg.caption_entities or []):
        try:
            if ent.type == "text_link" and ent.url:
                u = ent.url
            elif ent.type == "url":
                u = ent.extract_from(text)
            else:
                continue
        except Exception:
            continue
        u = norm_url(u)
        if u.lower().startswith(("http://", "https://")) and u not in urls:
            urls.append(u)
    return urls


@router.message(F.text | F.caption)
async def on_link(m: Message, bot: Bot):
    text = m.text or m.caption or ""
    log.info("msg chat=%s(%s) user=%s text=%r", m.chat.id, m.chat.type, m.from_user.id if m.from_user else None, text[:80])
    if not m.from_user:
        return
    urls = msg_urls(m)
    is_cmd = text.split("@")[0].split()[0].lower() == "/dl" if text.startswith("/") else False
    if is_cmd and not urls and m.reply_to_message:
        urls = msg_urls(m.reply_to_message)
    if text.startswith("/") and not is_cmd:
        return
    private = m.chat.type == "private"
    if not private and not is_cmd:
        # قبلاً فقط لیست ثابت HOSTS قبول می‌شد؛ حالا هر سایتی که yt-dlp می‌شناسه (Pornhub و ...)
        checks = await asyncio.to_thread(lambda: [u for u in urls if group_accepts(u, config.GROUP_ALL_LINKS)])
        if urls and not checks:
            log.info("group link ignored (unsupported by yt-dlp): %s", urls[0][:80])
        urls = checks
    if not urls:
        if private and not text.startswith("/"):
            await m.answer("🔗 ارباب، لینک معتبر بفرستین (یوتیوب، اینستاگرام، تیک‌تاک و ...)")
        return
    url = urls[0]
    uid = m.from_user.id
    if config.DAILY_LIMIT and uid not in config.ADMIN_IDS:
        if await db.user_today_count(uid) >= config.DAILY_LIMIT:
            await m.reply(f"⛔ ارباب، سقف دانلود روزانه ({config.DAILY_LIMIT}) تموم شده. فردا در خدمتتونم.")
            return
    token = secrets.token_urlsafe(5)
    platform = platform_of(url)
    kw = {"message_thread_id": m.message_thread_id} if m.is_topic_message else {}
    menu = await bot.send_message(
        m.chat.id, menu_text(None, platform), reply_markup=quality_kb(token),
        link_preview_options=NOPREVIEW, **kw,
        reply_parameters=ReplyParameters(message_id=m.message_id, allow_sending_without_reply=True))
    CACHE[token] = {"url": url, "user_id": uid, "chat_id": m.chat.id, "chat_type": m.chat.type,
                    "msg_id": m.message_id, "menu_id": menu.message_id, "state": "menu", "info": None,
                    "platform": platform, "ts": time.time(), "prog": None,
                    "thread": m.message_thread_id if m.is_topic_message else None}
    spawn(fill_info(bot, token))


async def fill_info(bot, token):
    e = CACHE.get(token)
    if not e:
        return
    try:
        info = await asyncio.wait_for(asyncio.to_thread(dl.fetch_info, e["url"]), 45)
    except Exception as ex:
        log.info("info failed %s: %s", e["url"], ex)
        return
    e = CACHE.get(token)
    if not info or not e or e["state"] != "menu":
        return
    e["info"] = info
    if info.get("is_live"):
        await _edit(bot, e, "📡 ببخشید ارباب، لایو پشتیبانی نمیشه.")
        CACHE.pop(token, None)
        return
    if config.MAX_DURATION_MIN and (info.get("duration") or 0) > config.MAX_DURATION_MIN * 60:
        await _edit(bot, e, f"⏱ ارباب، این ویدیو از {config.MAX_DURATION_MIN} دقیقه طولانی‌تره.")
        CACHE.pop(token, None)
        return
    text, markup = menu_text(info, e["platform"]), build_kb(token, info)
    tdir = None
    try:
        tdir = tempfile.mkdtemp(prefix="dl_th_")
        jpg = await asyncio.wait_for(asyncio.to_thread(dl.fetch_thumb, info, tdir), 25)
        if jpg and e["state"] == "menu":
            kw = {"message_thread_id": e["thread"]} if e.get("thread") else {}
            msg = await bot.send_photo(
                e["chat_id"], FSInputFile(jpg), caption=text, reply_markup=markup, **kw,
                reply_parameters=ReplyParameters(message_id=e["msg_id"], allow_sending_without_reply=True))
            if e["state"] != "menu" or token not in CACHE:  # کاربر در این فاصله انتخاب کرد
                await bot.delete_message(e["chat_id"], msg.message_id)
            else:
                old = e["menu_id"]
                e["menu_id"], e["photo"] = msg.message_id, True
                try:
                    await bot.delete_message(e["chat_id"], old)
                except Exception:
                    pass
                return
    except Exception as ex:
        log.info("thumbnail menu failed: %s", ex)
    finally:
        if tdir:
            shutil.rmtree(tdir, ignore_errors=True)
    if e["state"] == "menu" and not e.get("photo"):
        await _edit(bot, e, text, markup)


@router.callback_query(F.data.startswith("cx:"))
async def cb_cancel(c: CallbackQuery):
    token = c.data.split(":")[1]
    e = CACHE.get(token)
    if e and c.from_user.id != e["user_id"] and c.from_user.id not in config.ADMIN_IDS:
        await c.answer("ارباب، این منو برای شما نیست 🙂", show_alert=True)
        return
    if e:
        if e.get("prog") is not None:
            e["prog"]["cancel"] = True
        CACHE.pop(token, None)
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.answer("به چشم ارباب، لغو شد")


@router.callback_query(F.data.startswith("dl:"))
async def cb_download(c: CallbackQuery, bot: Bot):
    _, token, q = c.data.split(":")
    e = CACHE.get(token)
    if not e:
        await c.answer("ارباب، این منو منقضی شده؛ لینک رو دوباره بفرستین.", show_alert=True)
        return
    if c.from_user.id != e["user_id"] and c.from_user.id not in config.ADMIN_IDS:
        await c.answer("ارباب، این منو برای شما نیست 🙂", show_alert=True)
        return
    if e["state"] != "menu":
        await c.answer("در حال انجامه ارباب…")
        return
    if ACTIVE.get(e["user_id"], 0) >= config.USER_MAX_ACTIVE:
        await c.answer("ارباب، چند دانلود همزمان دارین؛ کمی صبر کنین ⏳", show_alert=True)
        return
    e["state"] = "busy"
    await c.answer("به روی چشم ارباب ✅")
    spawn(process(bot, token, q))


async def run_download(e, q, workdir):
    prog = {"pct": 0.0, "speed": "", "stage": "dl", "cancel": False}
    e["prog"] = prog
    task = asyncio.create_task(asyncio.to_thread(dl.download, e["url"], q, workdir, prog, e.get("info")))
    return prog, task


async def process(bot, token, q):
    e = CACHE.get(token)
    if not e:
        return
    uid = e["user_id"]
    ACTIVE[uid] = ACTIVE.get(uid, 0) + 1
    t0 = time.time()
    workdir = None
    res = None
    status, err, size, ok = "fail", "", 0, False
    try:
        if SEM.locked():
            await _edit(bot, e, "⏳ ارباب، در صف دانلود…")
        async with SEM:
            await _edit(bot, e, "⏳ در حال آماده‌سازی برای ارباب…")
            key = f"{e['url']}|{q}"
            cached = await db.get_file(key)
            if cached and await send_cached(bot, e, cached):
                status, ok = "ok", True
                return
            chain = dl.fallback_chain(q)
            for i, qq in enumerate(chain):
                workdir = tempfile.mkdtemp(prefix="dl_")
                prog, task = await run_download(e, qq, workdir)
                started = time.time()
                last = ""
                while not task.done():
                    await asyncio.sleep(3)
                    if time.time() - started > config.DL_TIMEOUT:
                        prog["cancel"] = True
                        raise asyncio.TimeoutError()
                    txt = ("🔀 در حال پردازش برای ارباب…" if prog["stage"] == "merge"
                           else f"📥 در حال دانلود برای ارباب…\n{bar(prog['pct'])} {prog['pct']:.0f}%"
                                + (f"  •  {prog['speed']}" if prog["speed"] else ""))
                    if txt != last:
                        last = txt
                        await _edit(bot, e, txt)
                try:
                    res = await task
                except dl.TooBig as big:
                    res = None
                    big_size = big.size
                else:
                    if res.size <= config.UPLOAD_LIMIT:
                        break
                    big_size = res.size
                    res = None
                shutil.rmtree(workdir, ignore_errors=True)
                workdir = None
                if i == len(chain) - 1 or q == "a":
                    raise dl.DlError(f"ارباب، حجم فایل ({human_size(big_size)}) بیشتر از حد مجاز تلگرام "
                                     f"({human_size(config.UPLOAD_LIMIT)}) هست. کیفیت پایین‌تری امتحان بفرمایین.")
                await _edit(bot, e, "📦 حجم زیاده ارباب؛ تلاش با کیفیت پایین‌تر…")
            size = res.size
            await _edit(bot, e, "📤 در حال تقدیم به ارباب…")
            await send_result(bot, e, res, key)
            status, ok = "ok", True
    except dl.DlError as ex:
        err = str(ex)
        if ex.cookie:
            spawn(notify_cookie(bot, e["url"]))
    except dl.TooBig:
        err = f"ارباب، حجم فایل بیشتر از حد مجاز تلگرام ({human_size(config.UPLOAD_LIMIT)}) هست. کیفیت پایین‌تری امتحان بفرمایین."
    except asyncio.TimeoutError:
        err = "ببخشید ارباب، زمان دانلود تموم شد. دوباره امتحان کنین."
    except dl.DownloadCancelled:
        err, status = "", "cancelled"
    except Exception as ex:
        log.exception("process failed")
        err = "ببخشید ارباب، خطای غیرمنتظره پیش اومد؛ دوباره امتحان کنین."
        status = "fail"
        e["err_raw"] = repr(ex)
    finally:
        ACTIVE[uid] = max(0, ACTIVE.get(uid, 1) - 1)
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        e["prog"] = None
        try:
            await db.add_download(uid, e["chat_id"], e["chat_type"], e["url"], e["platform"], q,
                                  status, size, time.time() - t0, err or e.get("err_raw", ""),
                                  title=(res.title if res else (e.get("info") or {}).get("title", "")))
        except Exception:
            log.exception("db add failed")
        if ok:
            CACHE.pop(token, None)
            try:
                await bot.delete_message(e["chat_id"], e["menu_id"])
            except Exception:
                pass
        elif status == "cancelled":
            CACHE.pop(token, None)
        elif token in CACHE:
            e["state"] = "menu"
            await _edit(bot, e, f"❌ {err}", build_kb(token, e.get("info")))


def _caption(res_title, platform, username):
    t = f"<b>{esc(res_title[:180])}</b>\n\n" if res_title else ""
    return f"{t}📥 {platform} • @{username}"


async def _send(bot, e, kind, media, caption, res=None):
    kw = dict(chat_id=e["chat_id"], caption=caption, request_timeout=config.SEND_TIMEOUT,
              reply_parameters=ReplyParameters(message_id=e["msg_id"], allow_sending_without_reply=True))
    if e.get("thread"):
        kw["message_thread_id"] = e["thread"]
    if kind == "photo":
        return await bot.send_photo(photo=media, **kw)
    if kind == "document":
        return await bot.send_document(document=media, **kw)
    if kind == "audio":
        extra = {"title": res.title[:60], "duration": res.duration} if res else {}
        return await bot.send_audio(audio=media, **extra, **kw)
    extra = {}
    if res:
        extra = {"duration": res.duration or None, "width": res.width or None, "height": res.height or None}
    return await bot.send_video(video=media, supports_streaming=True, **extra, **kw)


def _file_id(msg, kind):
    if kind == "photo":
        return msg.photo[-1].file_id
    if kind == "audio" and msg.audio:
        return msg.audio.file_id
    if msg.video:
        return msg.video.file_id
    if msg.document:
        return msg.document.file_id
    return None


async def send_cached(bot, e, cached):
    file_id, kind = cached
    try:
        u = (await me(bot)).username
        await _send(bot, e, kind, file_id, _caption("", e["platform"], u))
        return True
    except Exception as ex:
        log.info("cached send failed: %s", ex)
        return False


async def send_result(bot, e, res, key):
    u = (await me(bot)).username
    cap = _caption(res.title, e["platform"], u)
    try:
        await bot.send_chat_action(e["chat_id"], "upload_video" if res.kind == "video" else "upload_document")
    except Exception:
        pass
    # با Local Bot API Server فایل مستقیم از دیسک خونده میشه (file://) → تا 2GB، بدون آپلود دوباره
    file = res.path.resolve().as_uri() if config.IS_LOCAL_API else FSInputFile(res.path)
    try:
        msg = await _send(bot, e, res.kind, file, cap, res)
        fid = _file_id(msg, res.kind)
    except Exception as ex:
        log.warning("typed send failed (%s), falling back to document", ex)
        msg = await bot.send_document(
            chat_id=e["chat_id"], document=FSInputFile(res.path), caption=cap, request_timeout=config.SEND_TIMEOUT,
            message_thread_id=e.get("thread"),
            reply_parameters=ReplyParameters(message_id=e["msg_id"], allow_sending_without_reply=True))
        fid = msg.document.file_id if msg.document else None
        res.kind = "video"
    if fid:
        try:
            await db.put_file(key, fid, res.kind if msg.document is None else "document")
        except Exception:
            pass
