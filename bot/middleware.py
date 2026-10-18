import time

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery

from . import config
from .db import db

_cache: dict = {}  # uid -> (banned, ts)
_notified: dict = {}


def invalidate(uid):
    _cache.pop(uid, None)


class Gate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user and not user.is_bot:
            now = time.time()
            hit = _cache.get(user.id)
            if not hit or now - hit[1] > 60:
                try:
                    banned = await db.touch_user(user)
                    if chat and chat.type in ("group", "supergroup"):
                        await db.touch_chat(chat)
                except Exception:
                    banned = hit[0] if hit else False
                _cache[user.id] = (banned, now)
            banned = _cache[user.id][0]
            if user.id not in config.ADMIN_IDS:
                if banned:
                    if isinstance(event, CallbackQuery):
                        await event.answer("⛔ ارباب، دسترسی شما مسدود شده.", show_alert=True)
                    return
                try:
                    maint = (await db.get_kv("maintenance", "0")) == "1"
                except Exception:
                    maint = False
                if maint:
                    if chat and chat.type == "private":
                        if now - _notified.get(user.id, 0) > 300:
                            _notified[user.id] = now
                            try:
                                if isinstance(event, CallbackQuery):
                                    await event.answer("🔧 ببخشید ارباب، بات در حال بروزرسانیه؛ کمی بعد در خدمتتونم.", show_alert=True)
                                else:
                                    await event.answer("🔧 ببخشید ارباب، بات در حال بروزرسانیه؛ کمی بعد در خدمتتونم.")
                            except Exception:
                                pass
                    return
        return await handler(event, data)
