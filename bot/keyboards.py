from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import config

PRIMARY, SUCCESS, DANGER = "primary", "success", "danger"  # آبی / سبز / قرمز


def b(text, data=None, url=None, style=None):
    kw = {}
    if style and config.COLORED_BUTTONS:
        kw["style"] = style  # Bot API 9.4+
    return InlineKeyboardButton(text=text, callback_data=data, url=url, **kw)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


DEFAULT_OPTS = [("1080", "🎬 1080p"), ("720", "🎬 720p"), ("480", "🎬 480p"), ("360", "🎬 360p")]


def quality_kb(token, opts=None, audio_only=False):
    rows = []
    if audio_only:
        rows.append([b("🎵 دانلود صدا", f"dl:{token}:a", style=SUCCESS)])
    else:
        row = []
        for q, label in (opts or DEFAULT_OPTS):
            row.append(b(label, f"dl:{token}:{q}", style=PRIMARY))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([b("🎵 MP3 صدا", f"dl:{token}:a", style=SUCCESS)])
    rows.append([b("❌ لغو", f"cx:{token}", style=DANGER)])
    return kb(rows)


def start_kb(username):
    return kb([[b("➕ افزودن به گروه", url=f"https://t.me/{username}?startgroup=true", style=SUCCESS)],
               [b("📖 راهنما", "help", style=PRIMARY)]])


def admin_home(maint):
    return kb([
        [b("📊 آمار کلی", "ad:stats", style=PRIMARY), b("👥 کاربران", "ad:users:0", style=PRIMARY)],
        [b("🧾 لاگ دانلودها", "ad:log:0", style=PRIMARY), b("📤 خروجی CSV", "ad:csv_all", style=PRIMARY)],
        [b("📈 مصرف ۷ روز", "ad:usage", style=PRIMARY), b("🏆 برترین‌ها", "ad:top", style=PRIMARY)],
        [b("🔎 جستجوی کاربر", "ad:find", style=PRIMARY), b("📢 پیام همگانی", "ad:bc", style=SUCCESS)],
        [b("🔴 حالت تعمیر: روشن" if maint else "🟢 حالت تعمیر: خاموش", "ad:maint",
           style=DANGER if maint else SUCCESS)],
        [b("🍪 کوکی‌ها", "ad:cookie", style=PRIMARY), b("🩺 تشخیص", "ad:diag", style=PRIMARY)],
        [b("🔄 بروزرسانی", "ad:home", style=PRIMARY)],
    ])


def admin_back():
    return kb([[b("🔙 بازگشت", "ad:home", style=PRIMARY)]])
