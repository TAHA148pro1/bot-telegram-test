import asyncio
import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import config
from ..db import db
from ..keyboards import DANGER, PRIMARY, SUCCESS, admin_back, admin_home, b, kb
from ..utils import esc, fmt_ts, human_size, q_label

log = logging.getLogger("admin")
router = Router()
START = time.time()
ADMIN = F.from_user.id.in_(config.ADMIN_IDS)
PAGE = 8


class St(StatesGroup):
    cookie = State()
    find = State()
    broadcast = State()


async def maint():
    return (await db.get_kv("maintenance", "0")) == "1"


async def home_text():
    up = int(time.time() - START)
    return (f"🛠 <b>پنل مدیریت ارباب</b>\n\n⏱ آپتایم: {up // 3600}h {up % 3600 // 60}m\n"
            f"👥 کاربران: <b>{await db.count_users()}</b>")


async def edit(c: CallbackQuery, text, markup):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


@router.message(Command("admin"), ADMIN, F.chat.type == "private")
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await home_text(), reply_markup=admin_home(await maint()))


@router.callback_query(F.data == "ad:home", ADMIN)
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    await edit(c, await home_text(), admin_home(await maint()))


@router.callback_query(F.data == "ad:stats", ADMIN)
async def cb_stats(c: CallbackQuery):
    s = await db.stats()
    def line(n, t):
        ok, bad, by = s[t]
        return f"{n}: <b>{ok}</b> موفق / {bad} ناموفق • {human_size(by)}"
    plat = "\n".join(f"  • {esc(p)}: {n}" for p, n in s["platforms"]) or "  —"
    total = s["all"][0] + s["all"][1]
    rate = f"{s['all'][0] / total * 100:.0f}%" if total else "—"
    txt = (f"📊 <b>آمار کلی</b>\n\n"
           f"👥 کاربران: <b>{s['users']}</b>  (امروز جدید: {s['users_today']} • فعال امروز: {s['active_today']})\n"
           f"🚫 مسدود: {s['banned']}   👨‍👩‍👧 گروه‌ها: {s['groups']}\n\n"
           f"{line('📥 امروز', 'today')}\n{line('📆 ۷ روز', 'week')}\n{line('🗂 کل', 'all')}\n"
           f"✅ نرخ موفقیت: {rate}\n\n🌐 <b>پلتفرم‌ها</b>\n{plat}")
    await c.answer()
    await edit(c, txt, admin_back())


@router.callback_query(F.data == "ad:usage", ADMIN)
async def cb_usage(c: CallbackQuery):
    d = await db.daily(7)
    rows = "\n".join(f"<code>{k}</code>  {v[0]:>4} دانلود  •  {human_size(v[1])}" for k, v in d.items())
    tot = sum(v[1] for v in d.values())
    await c.answer()
    await edit(c, f"📈 <b>مصرف ۷ روز اخیر</b>\n\n{rows}\n\n💾 مجموع: <b>{human_size(tot)}</b>", admin_back())


@router.callback_query(F.data == "ad:top", ADMIN)
async def cb_top(c: CallbackQuery):
    rows = await db.top_users(10)
    txt = "\n".join(f"{i}. {esc(r['first_name'] or '-')} "
                    f"({'@' + r['username'] if r['username'] else r['id']}) — {r['downloads']} • {human_size(r['bytes'])}"
                    for i, r in enumerate(rows, 1)) or "هنوز دانلودی ثبت نشده."
    await c.answer()
    await edit(c, f"🏆 <b>پرمصرف‌ترین کاربران</b>\n\n{txt}", admin_back())


FILTERS = [("a", "همه"), ("t", "امروز فعال"), ("n", "امروز جدید"), ("d", "دانلودکننده"), ("b", "مسدود")]


def _ufilter_kb(cur):
    row = [b(("• " if k == cur else "") + lbl, f"ad:u:{k}:0", style=PRIMARY) for k, lbl in FILTERS]
    return [row[:3], row[3:]]


def _dl_line(r, with_user=False):
    st = {"ok": "✅", "fail": "❌", "cancelled": "⛔"}.get(r["status"], "•")
    url = r["url"] or ""
    link = f'<a href="{esc(url, quote=True)}">{esc(url[:60])}</a>' if url.startswith("http") else esc(url[:60])
    who = ""
    if with_user:
        who = f" {esc(r['first_name'] or '-')}{' @' + r['username'] if r['username'] else ''} (<code>{r['user_id']}</code>)\n   "
    size = f" • {human_size(r['size'])}" if r["status"] == "ok" else ""
    return f"{st} <code>{fmt_ts(r['ts'], sec=False)}</code> • {esc(r['platform'] or '-')} • {q_label(r['quality'])}{size}\n   {who}{link}"


@router.callback_query(F.data.startswith("ad:users:"), ADMIN)
async def cb_users(c: CallbackQuery):
    page = int(c.data.split(":")[2])
    await _users_page(c, "a", page)


@router.callback_query(F.data.startswith("ad:u:"), ADMIN)
async def cb_users_f(c: CallbackQuery):
    _, _, f, page = c.data.split(":")
    await _users_page(c, f, int(page))


async def _users_page(c, f, page):
    total = await db.count_users(f)
    rows = await db.list_users(page * PAGE, PAGE, f)
    lbl = dict(FILTERS).get(f, "همه")
    txt = f"👥 <b>کاربران</b> — {lbl} ({total})\n\n" + ("\n".join(
        f"{'🚫' if r['banned'] else '•'} <code>{r['id']}</code> {esc(r['first_name'] or '-')} "
        f"{'@' + r['username'] if r['username'] else ''} — {r['downloads']} • {fmt_ts(r['last_seen'])}" for r in rows)
        or "هیچ کاربری نیست.")
    btns = [[b(f"👤 {r['first_name'] or r['id']}"[:30], f"ad:user:{r['id']}", style=PRIMARY)] for r in rows]
    nav = []
    if page > 0:
        nav.append(b("◀️ قبلی", f"ad:u:{f}:{page - 1}", style=PRIMARY))
    if (page + 1) * PAGE < total:
        nav.append(b("بعدی ▶️", f"ad:u:{f}:{page + 1}", style=PRIMARY))
    if nav:
        btns.append(nav)
    btns.extend(_ufilter_kb(f))
    btns.append([b("🔎 جستجو", "ad:find", style=PRIMARY), b("🔙 بازگشت", "ad:home", style=PRIMARY)])
    await c.answer()
    await edit(c, txt[:3900], kb(btns))


async def user_card(uid):
    r = await db.get_user(uid)
    if not r:
        return None, None
    summ, plats = await db.user_summary(r["id"])
    pl = "، ".join(f"{esc(p)}: {n}" for p, n in plats) or "—"
    txt = (f"👤 <b>{esc(r['first_name'] or '-')}</b> {'@' + r['username'] if r['username'] else ''}\n"
           f"🆔 <code>{r['id']}</code>\n📅 عضویت: {fmt_ts(r['joined_at'])}\n🕒 آخرین فعالیت: {fmt_ts(r['last_seen'])}\n"
           f"📥 دانلود موفق: <b>{r['downloads']}</b> • 💾 {human_size(r['bytes'])}\n"
           f"📆 امروز: {summ['today']} • ❌ ناموفق: {summ['bad']} • 💬 چت‌ها: {summ['chats']}\n"
           f"🌐 پلتفرم‌ها: {pl}\n"
           f"{'🕒 آخرین دانلود: ' + fmt_ts(summ['last']) if summ['last'] else ''}\n"
           f"وضعیت: {'🚫 مسدود' if r['banned'] else '✅ فعال'}")
    btn = (b("✅ رفع مسدودی", f"ad:unban:{r['id']}", style=SUCCESS) if r["banned"]
           else b("🚫 مسدود کردن", f"ad:ban:{r['id']}", style=DANGER))
    return txt, kb([[b("📜 تاریخچه لینک‌ها", f"ad:uh:{r['id']}:0", style=PRIMARY),
                      b("📤 خروجی CSV", f"ad:ucsv:{r['id']}", style=PRIMARY)],
                     [btn], [b("🔙 بازگشت", "ad:home", style=PRIMARY)]])


# ---------- download history ----------
@router.callback_query(F.data.startswith("ad:uh:"), ADMIN)
async def cb_user_hist(c: CallbackQuery):
    _, _, uid, page = c.data.split(":")
    uid, page = int(uid), int(page)
    u = await db.get_user(uid)
    total = await db.count_user_downloads(uid)
    rows = await db.user_downloads(uid, page * PAGE, PAGE)
    name = esc((u["first_name"] if u else None) or str(uid))
    body = "\n".join(_dl_line(r) for r in rows) or "هنوز لینکی ارسال نکرده."
    txt = f"📜 <b>تاریخچه {name}</b> ({total} درخواست)\n\n{body}"
    btns = []
    nav = []
    if page > 0:
        nav.append(b("◀️ قبلی", f"ad:uh:{uid}:{page - 1}", style=PRIMARY))
    if (page + 1) * PAGE < total:
        nav.append(b("بعدی ▶️", f"ad:uh:{uid}:{page + 1}", style=PRIMARY))
    if nav:
        btns.append(nav)
    btns.append([b("📤 خروجی CSV", f"ad:ucsv:{uid}", style=PRIMARY), b("👤 بازگشت به کاربر", f"ad:user:{uid}", style=PRIMARY)])
    await c.answer()
    await edit(c, txt[:3900], kb(btns))


@router.callback_query(F.data.startswith("ad:log:"), ADMIN)
async def cb_log(c: CallbackQuery):
    page = int(c.data.split(":")[2])
    total = await db.count_downloads()
    rows = await db.recent_downloads(page * PAGE, PAGE)
    body = "\n".join(_dl_line(r, with_user=True) for r in rows) or "هنوز دانلودی ثبت نشده."
    btns, nav = [], []
    if page > 0:
        nav.append(b("◀️ جدیدتر", f"ad:log:{page - 1}", style=PRIMARY))
    if (page + 1) * PAGE < total:
        nav.append(b("قدیمی‌تر ▶️", f"ad:log:{page + 1}", style=PRIMARY))
    if nav:
        btns.append(nav)
    btns.append([b("📤 خروجی CSV کل", "ad:csv_all", style=PRIMARY), b("🔙 بازگشت", "ad:home", style=PRIMARY)])
    await c.answer()
    await edit(c, f"🧾 <b>لاگ دانلودها</b> ({total})\n\n{body}"[:3900], kb(btns))


async def _send_csv(c: CallbackQuery, bot: Bot, rows, name):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "user_id", "first_name", "username", "chat_id", "chat_type", "platform", "quality",
                "status", "size_bytes", "seconds", "title", "url", "error"])
    for r in rows:
        w.writerow([fmt_ts(r["ts"], sec=True), r["user_id"], r["first_name"] or "", r["username"] or "",
                    r["chat_id"], r["chat_type"], r["platform"], r["quality"], r["status"], r["size"],
                    round(r["secs"] or 0, 1), r["title"] or "", r["url"], r["error"] or ""])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM برای باز شدن درست فارسی توی Excel
    await bot.send_document(c.from_user.id, BufferedInputFile(data, filename=name),
                            caption=f"📤 {len(rows)} رکورد")
    await c.answer("فایل فرستاده شد ✅")


@router.callback_query(F.data.startswith("ad:ucsv:"), ADMIN)
async def cb_user_csv(c: CallbackQuery, bot: Bot):
    uid = int(c.data.split(":")[2])
    rows = await db.export_downloads(uid)
    await _send_csv(c, bot, rows, f"downloads_{uid}.csv")


@router.callback_query(F.data == "ad:csv_all", ADMIN)
async def cb_csv_all(c: CallbackQuery, bot: Bot):
    rows = await db.export_downloads(None)
    await _send_csv(c, bot, rows, "downloads_all.csv")


@router.callback_query(F.data.startswith("ad:user:"), ADMIN)
async def cb_user(c: CallbackQuery):
    txt, mk = await user_card(int(c.data.split(":")[2]))
    await c.answer()
    await edit(c, txt or "کاربر پیدا نشد.", mk or admin_back())


@router.callback_query(F.data.regexp(r"^ad:(ban|unban):"), ADMIN)
async def cb_ban(c: CallbackQuery):
    _, act, uid = c.data.split(":")
    uid = int(uid)
    if uid in config.ADMIN_IDS:
        await c.answer("ادمین رو نمیشه مسدود کرد", show_alert=True)
        return
    await db.set_ban(uid, act == "ban")
    from ..middleware import invalidate
    invalidate(uid)
    txt, mk = await user_card(uid)
    await c.answer("انجام شد ✅")
    await edit(c, txt, mk)


@router.callback_query(F.data == "ad:maint", ADMIN)
async def cb_maint(c: CallbackQuery):
    new = "0" if await maint() else "1"
    await db.set_kv("maintenance", new)
    await c.answer("حالت تعمیر " + ("روشن شد" if new == "1" else "خاموش شد"))
    await edit(c, await home_text(), admin_home(new == "1"))


# ---------- find user ----------
@router.callback_query(F.data == "ad:find", ADMIN)
async def cb_find(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.find)
    await c.answer()
    await edit(c, "🔎 آیدی عددی یا یوزرنیم کاربر رو بفرست:", admin_back())


@router.message(St.find, ADMIN, F.chat.type == "private")
async def got_find(m: Message, state: FSMContext):
    q = (m.text or "").strip()
    txt, mk = await user_card(q)
    if txt:
        await state.clear()
        await m.answer(txt, reply_markup=mk)
        return
    hits = await db.search_users(q) if q else []
    if not hits:
        await m.answer("❌ کاربر پیدا نشد. دوباره بفرست یا /admin بزن.")
        return
    await state.clear()
    btns = [[b(f"👤 {h['first_name'] or h['id']}"[:30] + (f" @{h['username']}" if h['username'] else ""),
                f"ad:user:{h['id']}", style=PRIMARY)] for h in hits]
    await m.answer(f"🔎 {len(hits)} نتیجه:", reply_markup=kb(btns + [[b("🔙 بازگشت", "ad:home", style=PRIMARY)]]))


# ---------- broadcast ----------
@router.callback_query(F.data == "ad:bc", ADMIN)
async def cb_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.broadcast)
    await c.answer()
    await edit(c, "📢 پیامی که می‌خوای برای همه ارسال بشه رو بفرست (متن/عکس/ویدیو):", admin_back())


@router.message(St.broadcast, ADMIN, F.chat.type == "private")
async def got_bc(m: Message, state: FSMContext):
    await state.update_data(src_chat=m.chat.id, src_msg=m.message_id)
    n = len(await db.all_user_ids())
    await m.reply(f"ارسال برای <b>{n}</b> کاربر؟", reply_markup=kb([[
        b("✅ ارسال", "ad:bcgo", style=SUCCESS), b("❌ لغو", "ad:home", style=DANGER)]]))


@router.callback_query(F.data == "ad:bcgo", ADMIN)
async def cb_bcgo(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    if not d.get("src_msg"):
        await c.answer("منقضی شد", show_alert=True)
        return
    await c.answer("ارسال شروع شد")
    await c.message.edit_text("📤 ارسال همگانی شروع شد…")
    asyncio.create_task(run_broadcast(bot, c.message.chat.id, d["src_chat"], d["src_msg"]))


async def run_broadcast(bot, admin_chat, src_chat, src_msg):
    ids = await db.all_user_ids()
    ok = fail = 0
    for uid in ids:
        for attempt in (1, 2):
            try:
                await bot.copy_message(uid, src_chat, src_msg)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except (TelegramForbiddenError, TelegramBadRequest):
                fail += 1
                break
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(admin_chat, f"✅ ارسال همگانی تموم شد\n\nموفق: {ok}\nناموفق: {fail}",
                               reply_markup=admin_back())
    except Exception:
        pass


# ---------- cookies ----------
import json
import os
import re

from .. import downloader as dl


def parse_cookies(txt):
    rows = []
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            parts = re.split(r"\s+", line, maxsplit=6)
        if len(parts) >= 7:
            rows.append(parts)
    return rows


def normalize_cookies(txt):
    """Netscape یا JSON (Cookie-Editor / EditThisCookie) → متن Netscape. None = نامعتبر."""
    txt = txt.lstrip("\ufeff").replace("\r\n", "\n").strip()
    if txt[:1] in "[{":
        try:
            data = json.loads(txt)
            if isinstance(data, dict):
                data = data.get("cookies", [])
            lines = ["# Netscape HTTP Cookie File"]
            for c in data:
                dom = c.get("domain") or c.get("host") or ""
                if not dom or "name" not in c:
                    continue
                exp = c.get("expirationDate") or c.get("expires") or 0
                exp = int(exp) if isinstance(exp, (int, float)) and exp > 0 else 0
                lines.append("\t".join([
                    ("#HttpOnly_" if c.get("httpOnly") else "") + dom,
                    "TRUE" if dom.startswith(".") else "FALSE",
                    c.get("path") or "/",
                    "TRUE" if c.get("secure") else "FALSE",
                    str(exp), str(c["name"]), str(c.get("value", ""))]))
            return "\n".join(lines) + "\n" if len(lines) > 1 else None
        except Exception:
            return None
    rows = parse_cookies(txt)
    if not rows:
        return None
    out = ["# Netscape HTTP Cookie File"]
    for r in rows:
        out.append("\t".join(r[:6] + [r[6]]))
    return "\n".join(out) + "\n"


def cookie_status():
    p = config.COOKIES_FILE
    if not p or not os.path.exists(p):
        return "❌ هیچ کوکی‌ای تنظیم نشده."
    rows = parse_cookies(open(p, encoding="utf-8", errors="ignore").read())
    doms = sorted({r[0].replace("#HttpOnly_", "").lstrip(".") for r in rows})
    names = {r[5] for r in rows}
    login = bool(names & {"SID", "__Secure-3PSID", "LOGIN_INFO", "sessionid"})
    mt = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
    return (f"✅ کوکی فعاله ({len(rows)} مورد)\n🕒 آخرین بروزرسانی: {mt}\n"
            f"🔑 کوکی لاگین: {'دارد' if login else '⚠️ ندارد (احتمالاً لاگین نبودین)'}\n"
            f"🌐 دامنه‌ها: {esc(', '.join(doms[:8]))}")


def cookie_kb():
    return kb([[b("📤 آپلود کوکی جدید", "ad:cookie_up", style=SUCCESS),
                b("🗑 حذف کوکی", "ad:cookie_del", style=DANGER)],
               [b("🩺 تست کوکی/یوتیوب", "ad:diag", style=PRIMARY)],
               [b("🔙 بازگشت", "ad:home", style=PRIMARY)]])


@router.callback_query(F.data == "ad:cookie", ADMIN)
async def cb_cookie(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    await edit(c, "🍪 <b>مدیریت کوکی‌ها</b>\n\n" + cookie_status() +
               "\n\nفرمت‌های قابل قبول: <code>cookies.txt</code> (Netscape) یا خروجی <b>JSON</b> افزونه Cookie-Editor.\n"
               "هر فایلی که اسمش شامل <code>cookie</code> باشه رو هر وقت بفرستین، خودکار ثبت میشه.\n\n"
               "نکته: از پنجره Incognito بعد از لاگین به youtube.com اکسپورت کنین و پنجره رو ببندین.", cookie_kb())


@router.callback_query(F.data == "ad:cookie_up", ADMIN)
async def cb_cookie_up(c: CallbackQuery, state: FSMContext):
    await state.set_state(St.cookie)
    await c.answer()
    await edit(c, "📤 فایل کوکی (txt یا json) رو به صورت <b>فایل (Document)</b> همینجا بفرستین.\n"
                  "پیام بعد از دریافت پاک میشه.", admin_back())


async def import_cookie_doc(m: Message, state: FSMContext, bot: Bot):
    if (m.document.file_size or 0) > 3 * 1024 * 1024:
        await m.answer("❌ ارباب، فایل خیلی بزرگه (حداکثر ۳MB).")
        return
    buf = await bot.download(m.document)
    txt = normalize_cookies(buf.read().decode("utf-8", "ignore"))
    if not txt:
        await m.answer("❌ ارباب، فرمت فایل قابل خوندن نیست. باید cookies.txt (Netscape) یا JSON باشه.")
        return
    path = config.DATA_DIR / "cookies.txt"
    path.write_text(txt, encoding="utf-8")
    config.COOKIES_FILE = str(path)
    await state.clear()
    try:
        await m.delete()  # کوکی حساسه
    except Exception:
        pass
    note = await m.answer("✅ کوکی ذخیره شد؛ در حال تست روی یوتیوب…")
    try:
        res = await asyncio.wait_for(asyncio.to_thread(dl.diagnose), 150)
        ok = any(r[1] for r in res)
        verdict = "✅ تست یوتیوب موفق بود." if ok else "⚠️ تست یوتیوب ناموفق بود:\n" + esc(res[0][3] if res else "")
    except Exception as e:
        verdict = f"⚠️ تست انجام نشد: {esc(str(e)[:100])}"
    await note.edit_text(f"{cookie_status()}\n\n{verdict}", reply_markup=cookie_kb())


@router.message(St.cookie, ADMIN, F.chat.type == "private", F.document)
async def got_cookie(m: Message, state: FSMContext, bot: Bot):
    await import_cookie_doc(m, state, bot)


@router.message(ADMIN, F.chat.type == "private", F.document.file_name.regexp(r"(?i)cookie"))
async def got_cookie_auto(m: Message, state: FSMContext, bot: Bot):
    await import_cookie_doc(m, state, bot)


@router.message(St.cookie, ADMIN, F.chat.type == "private")
async def got_cookie_bad(m: Message):
    await m.answer("ارباب، کوکی رو به صورت فایل (Document) بفرستین یا /admin بزنین.")


@router.callback_query(F.data == "ad:cookie_del", ADMIN)
async def cb_cookie_del(c: CallbackQuery):
    try:
        (config.DATA_DIR / "cookies.txt").unlink()
    except FileNotFoundError:
        pass
    config.COOKIES_FILE = None
    await c.answer("حذف شد")
    await edit(c, "🍪 <b>مدیریت کوکی‌ها</b>\n\n" + cookie_status(), cookie_kb())


# ---------- diagnostics ----------
@router.callback_query(F.data == "ad:diag", ADMIN)
async def cb_diag(c: CallbackQuery):
    await c.answer("در حال تست…")
    await edit(c, "🩺 در حال تست (تا ۲ دقیقه)…", None)
    direct = await asyncio.to_thread(dl.warp_trace)
    lines = [f"🌐 آیپی مستقیم سرور: <code>{esc(direct.get('ip', direct.get('error', '?')))}</code> ({esc(direct.get('loc', '-'))})"]
    ws = config.warp_state()
    lines.append(f"🛡 WARP: {({'up': '✅ وصله', 'starting': '⏳ در حال اتصال', 'down': '❌ قطع', 'off': '⛔ خاموش'}).get(ws, ws)}")
    for p in config.PROXY_YT + ([config.WARP_PROXY] if ws == "up" else []):
        t = await asyncio.to_thread(dl.warp_trace, p)
        lines.append(f"🛡 {esc(p.split('@')[-1])}: " + (f"آیپی <code>{esc(t['ip'])}</code> • warp={esc(t.get('warp', '-'))}"
                                                       if "ip" in t else f"❌ {esc(t.get('error', '?'))}"))
    if ws != "up":
        try:
            tail = open("/tmp/warp.log", errors="ignore").read().strip().splitlines()[-3:]
            if tail:
                lines.append("<code>" + esc(" | ".join(tail))[:400] + "</code>")
        except Exception:
            pass
    lines.append("\n" + cookie_status())
    try:
        res = await asyncio.wait_for(asyncio.to_thread(dl.diagnose), 150)
        lines.append("\n📺 <b>تست یوتیوب</b>")
        for name, ok, used, err in res:
            lines.append(f"{'✅' if ok else '❌'} {esc(name.split('@')[-1])}" + (f" — {esc(err)}" if not ok else ""))
    except Exception as e:
        lines.append(f"\n📺 تست انجام نشد: {esc(str(e)[:100])}")
    if dl.LAST_ERRORS:
        lines.append("\n🧾 <b>آخرین خطاها</b>")
        for ts, url, err in list(dl.LAST_ERRORS)[-3:]:
            lines.append(f"• <code>{esc(err[:160])}</code>")
    await edit(c, "\n".join(lines)[:3900], kb([[b("🔄 تست دوباره", "ad:diag", style=PRIMARY)],
                                              [b("🔙 بازگشت", "ad:home", style=PRIMARY)]]))
