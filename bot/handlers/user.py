import asyncio
import html
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, InputMediaVideo, LinkPreviewOptions, Message)

from .. import config, db, services
from ..gate import missing_channels
from ..i18n import t

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")

esc = html.escape
URL_RE = re.compile(r"https?://[^\s<>\"']+")
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".opus", ".wav"}
TRACK = {"igsh", "igshid", "si", "fbclid", "feature"}

CACHE: dict[str, Path] = {}     # tugmalar uchun vaqtincha fayllar
QUERIES: dict[str, str] = {}    # Shazam natijasi -> qidiruv matni
BUSY: set[int] = set()


# ---------------- yordamchilar ----------------
def extract_url(m: Message) -> str | None:
    mt = URL_RE.search(m.text or "")
    if mt:
        return mt.group(0).rstrip(").,;")
    for e in m.entities or []:
        if e.type == "text_link" and e.url:
            return e.url
    return None


def is_link(m: Message) -> bool:
    return extract_url(m) is not None


def is_plain(m: Message) -> bool:
    return bool(m.text) and not m.text.startswith("/")


def norm_url(url: str) -> str:
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if not (k.startswith("utm_") or k in TRACK)]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), urlencode(q), ""))


@asynccontextmanager
async def job(uid: int):
    if uid in BUSY:
        yield False
        return
    BUSY.add(uid)
    try:
        yield True
    finally:
        BUSY.discard(uid)


def remember(path: Path) -> str:
    key = uuid.uuid4().hex[:10]
    CACHE[key] = path
    return key


def actions_kb(key: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_circle"), callback_data=f"circle:{key}"),
         InlineKeyboardButton(text=t(lang, "btn_music"), callback_data=f"music:{key}")],
        [InlineKeyboardButton(text=t(lang, "btn_mp3"), callback_data=f"mp3:{key}"),
         InlineKeyboardButton(text=t(lang, "btn_gif"), callback_data=f"gif:{key}"),
         InlineKeyboardButton(text=t(lang, "btn_cover"), callback_data=f"cover:{key}")],
    ])


def fid(msg: Message) -> list[str] | None:
    if msg.video:
        return ["video", msg.video.file_id]
    if msg.photo:
        return ["photo", msg.photo[-1].file_id]
    if msg.audio:
        return ["audio", msg.audio.file_id]
    if msg.document:
        return ["document", msg.document.file_id]
    return None


def caption(title: str, uname: str) -> str:
    base = esc((title or "").strip()[:180])
    tail = f"📥 @{uname}"
    return f"{base}\n\n{tail}" if base else tail


async def within_limit(msg: Message, uid: int, lang: str) -> bool:
    limit = int(db.setting("daily_limit", "0") or 0)
    if limit <= 0 or uid in config.ADMIN_IDS:
        return True
    if await db.usage_get(uid) >= limit:
        await msg.answer(t(lang, "limit", n=limit))
        return False
    return True


async def source_file(call: CallbackQuery, key: str, bot: Bot) -> Path | None:
    p = CACHE.get(key)
    if p and p.exists():
        return p
    m = call.message
    media = (m.video or m.video_note) if isinstance(m, Message) else None
    if media and (media.file_size or 0) <= config.MAX_DOWNLOAD_FROM_TG:
        dst = services.tmp(".mp4", "src")
        await bot.download(media, destination=dst)
        CACHE[key] = dst
        return dst
    return None


# ---------------- yuborish ----------------
async def fit(path: Path) -> Path | None:
    if path.stat().st_size <= config.MAX_UPLOAD:
        return path
    if path.suffix.lower() in VIDEO_EXT:
        small = await services.shrink(path)
        path.unlink(missing_ok=True)
        return small
    path.unlink(missing_ok=True)
    return None


async def send_one(m: Message, path: Path, cap: str | None, lang: str) -> list[str] | None:
    ext = path.suffix.lower()
    f = FSInputFile(path, chunk_size=512 * 1024)
    if ext in VIDEO_EXT:
        sent = await m.answer_video(f, caption=cap, supports_streaming=True,
                                    reply_markup=actions_kb(remember(path), lang))
        return fid(sent)
    try:
        if ext in IMAGE_EXT:
            sent = await m.answer_photo(f, caption=cap)
        elif ext in AUDIO_EXT:
            sent = await m.answer_audio(f, caption=cap)
        else:
            sent = await m.answer_document(f, caption=cap)
    except TelegramBadRequest:
        sent = await m.answer_document(FSInputFile(path), caption=cap)
    path.unlink(missing_ok=True)
    return fid(sent)


async def deliver(m: Message, files: list[Path], cap: str, lang: str) -> list[list[str]]:
    ready = []
    for p in files:
        q = await fit(p)
        if q:
            ready.append(q)
        else:
            await m.answer(t(lang, "too_big"))
    items: list[list[str]] = []
    visual = [p for p in ready if p.suffix.lower() in VIDEO_EXT | IMAGE_EXT]
    rest = ready if len(visual) < 2 else [p for p in ready if p not in visual]
    first = True
    if len(visual) >= 2:
        for i in range(0, len(visual), 10):
            chunk = visual[i:i + 10]
            if len(chunk) == 1:
                r = await send_one(m, chunk[0], cap if first else None, lang)
                first = False
                items += [r] if r else []
                continue
            group = []
            for p in chunk:
                c = cap if first else None
                first = False
                if p.suffix.lower() in VIDEO_EXT:
                    group.append(InputMediaVideo(media=FSInputFile(p), caption=c, supports_streaming=True))
                else:
                    group.append(InputMediaPhoto(media=FSInputFile(p), caption=c))
            try:
                msgs = await m.answer_media_group(group)
                items += [x for x in (fid(s) for s in msgs) if x]
                for p in chunk:
                    p.unlink(missing_ok=True)
            except TelegramBadRequest:
                for p in chunk:
                    r = await send_one(m, p, None, lang)
                    items += [r] if r else []
    for p in rest:
        r = await send_one(m, p, cap if first else None, lang)
        first = False
        items += [r] if r else []
    return items


async def send_cached(m: Message, key: str, cap: str, lang: str) -> bool:
    row = await db.cache_get(key)
    if not row:
        return False
    items = json.loads(row["payload"])
    try:
        if len(items) >= 2 and all(k in ("video", "photo") for k, _ in items) and len(items) <= 10:
            group = [InputMediaVideo(media=f, caption=cap if i == 0 else None) if k == "video"
                     else InputMediaPhoto(media=f, caption=cap if i == 0 else None)
                     for i, (k, f) in enumerate(items)]
            await m.answer_media_group(group)
        else:
            for i, (k, f) in enumerate(items):
                c = cap if i == 0 else None
                if k == "video":
                    await m.answer_video(f, caption=c, reply_markup=actions_kb(uuid.uuid4().hex[:10], lang))
                elif k == "photo":
                    await m.answer_photo(f, caption=c)
                elif k == "audio":
                    await m.answer_audio(f, caption=c)
                else:
                    await m.answer_document(f, caption=c)
        return True
    except TelegramBadRequest:
        await db.cache_del(key)
        return False


# ---------------- boshlash / til / yordam ----------------
def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
    ]])


async def welcome_kb(bot: Bot, lang: str) -> InlineKeyboardMarkup:
    uname = (await bot.me()).username
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_group"), url=f"https://t.me/{uname}?startgroup=true")],
        [InlineKeyboardButton(text=t(lang, "btn_lang"), callback_data="set:lang")],
    ])


@router.message(CommandStart())
async def start(m: Message, u, lang: str, bot: Bot):
    if not u["lang_set"]:
        await m.answer(t(lang, "lang_pick"), reply_markup=lang_kb())
        return
    await m.answer(t(lang, "hello", name=esc(m.from_user.first_name or "")) + t(lang, "help"),
                   reply_markup=await welcome_kb(bot, lang))


@router.message(Command("lang"))
async def cmd_lang(m: Message, lang: str):
    await m.answer(t(lang, "lang_pick"), reply_markup=lang_kb())


@router.message(Command("help"))
async def cmd_help(m: Message, lang: str, bot: Bot):
    await m.answer(t(lang, "help"), reply_markup=await welcome_kb(bot, lang))


@router.callback_query(F.data.startswith("lang:"))
async def pick_lang(call: CallbackQuery, bot: Bot):
    new = call.data.split(":", 1)[1]
    if new not in ("uz", "ru", "en"):
        return await call.answer()
    await db.set_lang(call.from_user.id, new)
    await call.answer(t(new, "lang_ok"))
    if isinstance(call.message, Message):
        try:
            await call.message.edit_text(t(new, "hello", name=esc(call.from_user.first_name or "")) + t(new, "help"),
                                         reply_markup=await welcome_kb(bot, new))
        except TelegramBadRequest:
            pass


@router.callback_query(F.data == "chk")
async def check_sub(call: CallbackQuery, bot: Bot, lang: str):
    miss = await missing_channels(bot, call.from_user.id, force=True)
    if miss:
        return await call.answer(t(lang, "sub_fail"), show_alert=True)
    await call.answer()
    if isinstance(call.message, Message):
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass
    await bot.send_message(call.from_user.id, t(lang, "sub_ok"))


# ---------------- sozlamalar / tarix ----------------
def settings_kb(u, lang: str) -> InlineKeyboardMarkup:
    mark = lambda v: "✅" if v else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "set_circle", v=mark(u["auto_circle"])), callback_data="set:auto_circle")],
        [InlineKeyboardButton(text=t(lang, "set_music", v=mark(u["auto_music"])), callback_data="set:auto_music")],
        [InlineKeyboardButton(text=t(lang, "btn_lang"), callback_data="set:lang")],
    ])


@router.message(Command("settings"))
async def cmd_settings(m: Message, u, lang: str):
    await m.answer(t(lang, "settings"), reply_markup=settings_kb(u, lang))


@router.callback_query(F.data.startswith("set:"))
async def on_setting(call: CallbackQuery, lang: str):
    what = call.data.split(":", 1)[1]
    if not isinstance(call.message, Message):
        return await call.answer()
    if what == "lang":
        await call.answer()
        await call.message.answer(t(lang, "lang_pick"), reply_markup=lang_kb())
        return
    if what in ("auto_circle", "auto_music"):
        await db.toggle_flag(call.from_user.id, what)
    u = await db.get_user(call.from_user.id)
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=settings_kb(u, lang))
    except TelegramBadRequest:
        pass


@router.message(Command("history"))
async def cmd_history(m: Message, lang: str):
    rows = await db.history_list(m.from_user.id)
    if not rows:
        return await m.answer(t(lang, "history_empty"))
    lines = [f'{i}. <a href="{esc(r["url"])}">{esc((r["title"] or r["url"])[:50])}</a>'
             for i, r in enumerate(rows, 1)]
    await m.answer(t(lang, "history_title") + "\n\n" + "\n".join(lines),
                   link_preview_options=LinkPreviewOptions(is_disabled=True))


# ---------------- link orqali yuklash ----------------
YT_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^\s]*&)?v=|embed/|live/)|youtu\.be/)([\w-]{11})")


async def send_by_url(m: Message, r: dict, cap: str, lang: str) -> list[str] | None:
    """Telegramning o'zi havoladan oladi: server yuklamaydi ham, yubormaydi ham."""
    try:
        if r["kind"] == "photo":
            sent = await m.answer_photo(r["url"], caption=cap)
        else:
            sent = await m.answer_video(r["url"], caption=cap, supports_streaming=True,
                                        reply_markup=actions_kb(uuid.uuid4().hex[:10], lang))
        return fid(sent)
    except Exception as e:
        log.info("Havola orqali yuborilmadi: %s", str(e)[:150])
        return None


async def record(uid: int, key: str, url: str, title: str, items: list) -> None:
    if items:
        await db.cache_put(key, items, title)
    await db.incr("download")
    await db.bump_downloads(uid)
    await db.usage_add(uid)
    await db.history_add(uid, url, title)


IG_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([\w-]+)", re.I)
TT_RE = re.compile(r"tiktok\.com/.*?/video/(\d+)", re.I)
TW_RE = re.compile(r"(?:twitter|x)\.com/.*?/status/(\d+)", re.I)


def canon_key(url: str, height: int = 720) -> str:
    """Bir xil kontent turli havolalarda ham bitta keshga tushsin (tracking parametrlarsiz)."""
    suffix = f"#h{height}" if height != 720 else ""
    for pre, rx in (("ig", IG_RE), ("tt", TT_RE), ("x", TW_RE), ("yt", YT_RE)):
        mt = rx.search(url)
        if mt:
            return f"{pre}:{mt.group(1)}{suffix}"
    return norm_url(url) + suffix


async def handle_link(m: Message, bot: Bot, lang: str, url: str, height: int, uid: int):
    if not await within_limit(m, uid, lang):
        return
    async with job(uid) as ok:
        if not ok:
            return await m.answer(t(lang, "busy"))
        key = canon_key(url, height)
        uname = (await bot.me()).username
        row = await db.cache_get(key)
        if row and await send_cached(m, key, caption(row["title"], uname), lang):
            return await record(uid, key, url, row["title"], [])

        asyncio.create_task(bot.send_chat_action(m.chat.id, "upload_video"))

        def skip_msg(e):
            return t(lang, "dl_live") if e.reason == "live" else t(lang, "dl_long", m=config.MAX_VIDEO_SEC // 60)

        # 1) tezkor yo'l (YouTube'dan tashqari)
        if height == 720 and not services.is_youtube(url):
            try:
                r = await services.resolve(url)
            except services.Skipped as e:
                return await m.answer(skip_msg(e))
            except Exception as e:
                log.info("resolve o'tmadi (%s): %s", url, str(e)[:160])
                r = None
            if r:
                items = await send_by_url(m, r, caption(r["title"], uname), lang)
                if items:
                    return await record(uid, key, url, r["title"], [items])

        # 2) oddiy yuklash
        status = await m.answer(t(lang, "dl_start"))
        try:
            res = await services.download(url, height)
        except services.Skipped as e:
            return await status.edit_text(skip_msg(e))
        except Exception as e:
            log.warning("Yuklashda xato (%s): %s", url, str(e)[:300])
            try:
                alt = await services.scrape_media(url)
            except Exception:
                alt = None
            if alt:
                items = await send_by_url(m, alt, caption(alt["title"], uname), lang)
                if items:
                    await status.delete()
                    return await record(uid, key, url, alt["title"], [items])
            return await status.edit_text(t(lang, "dl_fail"))
        if not res.files:
            return await status.edit_text(t(lang, "dl_empty"))
        try:
            items = await deliver(m, res.files, caption(res.title, uname), lang)
        except Exception:
            log.exception("Yuborishda xato")
            return await status.edit_text(t(lang, "dl_fail"))
        await status.delete()
        if items:
            await record(uid, key, url, res.title, items)


@router.message(is_link)
async def on_link(m: Message, bot: Bot, lang: str):
    url = extract_url(m)
    mt = YT_RE.search(url)
    if mt:
        vid = mt.group(1)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="360p", callback_data=f"yq:360:{vid}"),
            InlineKeyboardButton(text="480p", callback_data=f"yq:480:{vid}"),
            InlineKeyboardButton(text="720p", callback_data=f"yq:720:{vid}"),
            InlineKeyboardButton(text=t(lang, "btn_mp3"), callback_data=f"yq:0:{vid}"),
        ]])
        return await m.answer(t(lang, "yt_pick"), reply_markup=kb)
    await handle_link(m, bot, lang, url, 720, m.from_user.id)


@router.callback_query(F.data.startswith("yq:"))
async def yt_quality(call: CallbackQuery, bot: Bot, lang: str):
    try:
        _, h, vid = call.data.split(":")
        h = int(h)
    except ValueError:
        return await call.answer()
    if not isinstance(call.message, Message):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    await call.answer()
    msg = call.message
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    uid = call.from_user.id
    url = f"https://www.youtube.com/watch?v={vid}"
    if h == 0:
        async with job(uid) as ok:
            if not ok:
                return await msg.answer(t(lang, "busy"))
            await send_song(msg, lang, uid, url, f"yt:{vid}", None)
    else:
        await handle_link(msg, bot, lang, url, h, uid)


# ---------------- video / audio yuborilganda ----------------
async def run_circle(msg: Message, src: Path, lang: str, uid: int):
    status = await msg.answer(t(lang, "circle_prep"))
    out = await services.make_circle(src)
    if not out:
        return await status.edit_text(t(lang, "circle_fail"))
    try:
        await msg.answer_video_note(FSInputFile(out), length=640)
        await db.incr("circle")
    finally:
        out.unlink(missing_ok=True)
    await status.delete()


async def run_mp3(msg: Message, src: Path, lang: str):
    out = await services.to_mp3(src)
    if not out:
        return await msg.answer(t(lang, "mp3_fail"))
    try:
        if out.stat().st_size > config.MAX_UPLOAD:
            await msg.answer(t(lang, "too_big"))
        else:
            await msg.answer_audio(FSInputFile(out))
    finally:
        out.unlink(missing_ok=True)


async def show_shazam(msg: Message, info: dict, lang: str):
    await db.incr("shazam")
    qkey = uuid.uuid4().hex[:10]
    QUERIES[qkey] = f"{info['artist']} - {info['title']}"
    rows = [[InlineKeyboardButton(text=t(lang, "btn_full"), callback_data=f"sg:{qkey}")]]
    if info.get("url"):
        rows.append([InlineKeyboardButton(text="🔗 Shazam", url=info["url"])])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = t(lang, "music_found", title=esc(info["title"]), artist=esc(info["artist"]))
    if info.get("cover"):
        try:
            return await msg.answer_photo(info["cover"], caption=text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await msg.answer(text, reply_markup=kb)


async def identify(msg: Message, src: Path, lang: str):
    status = await msg.answer(t(lang, "music_search"))
    try:
        info = await services.recognize(src)
    except Exception:
        log.exception("Shazam xato")
        return await status.edit_text(t(lang, "music_err"))
    if not info:
        return await status.edit_text(t(lang, "music_none"))
    await status.delete()
    await show_shazam(msg, info, lang)


async def voice_flow(msg: Message, src: Path, lang: str):
    """Ovozli xabar: Shazam (qo'shiq eshittirilsa) va nutqni matnga o'girish (nom aytilsa) birga ishlaydi."""
    status = await msg.answer(t(lang, "music_search"))
    shz, txt = await asyncio.gather(services.recognize(src), services.transcribe(src, lang), return_exceptions=True)
    info = shz if isinstance(shz, dict) else None
    text = txt.strip() if isinstance(txt, str) else ""
    if isinstance(shz, Exception):
        log.info("Shazam: %s", str(shz)[:120])
    if isinstance(txt, Exception):
        log.info("STT: %s", str(txt)[:120])
    if info:
        await status.delete()
        return await show_shazam(msg, info, lang)
    if len(text) >= 2:
        await status.edit_text(t(lang, "voice_heard", q=esc(text[:100])))
        return await show_search(msg, text, lang)
    await status.edit_text(t(lang, "voice_none"))


@router.message(F.video | F.video_note)
async def on_video(m: Message, bot: Bot, u, lang: str):
    media = m.video or m.video_note
    uid = m.from_user.id
    if (media.file_size or 0) > config.MAX_DOWNLOAD_FROM_TG:
        return await m.answer(t(lang, "tg20"))
    do_circle = bool(u["auto_circle"]) and not m.video_note
    do_music = bool(u["auto_music"])
    async with job(uid) as ok:
        if not ok:
            return await m.answer(t(lang, "busy"))
        src = services.tmp(".mp4", "src")
        await bot.download(media, destination=src)
        if not (do_circle or do_music):
            return await m.answer(t(lang, "ask_action"), reply_markup=actions_kb(remember(src), lang),
                                  reply_to_message_id=m.message_id)
        try:
            if do_circle:
                await run_circle(m, src, lang, uid)
            if do_music:
                await identify(m, src, lang)
        finally:
            src.unlink(missing_ok=True)


@router.message(F.audio | F.voice)
async def on_audio(m: Message, bot: Bot, lang: str):
    media = m.audio or m.voice
    if (media.file_size or 0) > config.MAX_DOWNLOAD_FROM_TG:
        return await m.answer(t(lang, "tg20"))
    async with job(m.from_user.id) as ok:
        if not ok:
            return await m.answer(t(lang, "busy"))
        src = services.tmp(".ogg" if m.voice else ".bin", "aud")
        await bot.download(media, destination=src)
        try:
            if m.voice and (m.voice.duration or 0) <= 60:
                await voice_flow(m, src, lang)
            else:
                await identify(m, src, lang)
        finally:
            src.unlink(missing_ok=True)


def is_action(c: CallbackQuery) -> bool:
    return (c.data or "").split(":", 1)[0] in ("circle", "music", "mp3", "gif", "cover")


@router.callback_query(is_action)
async def on_action(call: CallbackQuery, bot: Bot, lang: str):
    action, key = call.data.split(":", 1)
    if not isinstance(call.message, Message):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    uid = call.from_user.id
    async with job(uid) as ok:
        if not ok:
            return await call.answer(t(lang, "busy"), show_alert=True)
        src = await source_file(call, key, bot)
        if not src:
            return await call.answer(t(lang, "file_old"), show_alert=True)
        await call.answer()
        if action == "circle":
            await run_circle(call.message, src, lang, uid)
        elif action == "music":
            await identify(call.message, src, lang)
        elif action == "mp3":
            await run_mp3(call.message, src, lang)
        else:
            out = await (services.make_gif(src) if action == "gif" else services.make_cover(src))
            if not out:
                return await call.message.answer(t(lang, "tool_fail"))
            try:
                if action == "gif":
                    await call.message.answer_animation(FSInputFile(out))
                else:
                    await call.message.answer_photo(FSInputFile(out))
            finally:
                out.unlink(missing_ok=True)


# ---------------- qo'shiq qidirish va yuklash ----------------
async def send_song(msg: Message, lang: str, uid: int, target: str, cache_key: str, title: str | None = None):
    if not await within_limit(msg, uid, lang):
        return
    row = await db.cache_get(cache_key)
    if row:
        try:
            await msg.answer_audio(json.loads(row["payload"])[0][1])
            await db.incr("download")
            return
        except TelegramBadRequest:
            await db.cache_del(cache_key)
    status = await msg.answer(t(lang, "song_wait"))
    try:
        song = await services.download_song(target, title)
    except Exception as e:
        log.warning("Qo'shiq xato (%s): %s", target, str(e)[:300])
        song = None
    if not song:
        return await status.edit_text(t(lang, "song_fail"))
    try:
        if song.path.stat().st_size > config.MAX_UPLOAD:
            return await status.edit_text(t(lang, "too_big"))
        sent = await msg.answer_audio(FSInputFile(song.path, chunk_size=512 * 1024),
                                      title=song.title[:60], performer=song.artist[:60])
        await db.cache_put(cache_key, [["audio", sent.audio.file_id]], song.title)
        await db.incr("download")
        await db.bump_downloads(uid)
        await db.usage_add(uid)
        await status.delete()
    finally:
        song.path.unlink(missing_ok=True)


PAGE = 10
SEARCH: dict[str, dict] = {}


def fmt_dur(s: int) -> str:
    return f"{s // 60}:{s % 60:02d}" if s else ""


def search_text(q: str, res: list, page: int) -> str:
    chunk = res[page * PAGE:(page + 1) * PAGE]
    lines = [f"{page * PAGE + i}. {esc(r['title'][:70])} <b>{fmt_dur(r['duration'])}</b>".rstrip()
             for i, r in enumerate(chunk, 1)]
    return f"🔍 <code>{esc(q)}</code>\n\n" + "\n".join(lines)


def search_kb(sid: str, res: list, page: int) -> InlineKeyboardMarkup:
    n = len(res[page * PAGE:(page + 1) * PAGE])
    rows, row = [], []
    for i in range(n):
        gi = page * PAGE + i
        row.append(InlineKeyboardButton(text=str(gi + 1), callback_data=f"sp:{sid}:{gi}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️", callback_data=f"sn:{sid}:{page - 1}"),
                 InlineKeyboardButton(text="❌", callback_data=f"sx:{sid}"),
                 InlineKeyboardButton(text="➡️", callback_data=f"sn:{sid}:{page + 1}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("sp:"))
async def pick_song(call: CallbackQuery, lang: str):
    try:
        _, sid, idx = call.data.split(":")
        item = SEARCH[sid]["res"][int(idx)]
    except (ValueError, KeyError, IndexError):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    if not isinstance(call.message, Message):
        return await call.answer()
    async with job(call.from_user.id) as ok:
        if not ok:
            return await call.answer(t(lang, "busy"), show_alert=True)
        await call.answer()
        await send_song(call.message, lang, call.from_user.id, item["target"], "u:" + item["target"], item["title"])


@router.callback_query(F.data.startswith("sn:"))
async def search_page(call: CallbackQuery, lang: str):
    try:
        _, sid, page = call.data.split(":")
        page = int(page)
        s_ = SEARCH[sid]
    except (ValueError, KeyError):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    pages = (len(s_["res"]) + PAGE - 1) // PAGE
    if page < 0 or page >= pages or not isinstance(call.message, Message):
        return await call.answer()
    await call.answer()
    try:
        await call.message.edit_text(search_text(s_["q"], s_["res"], page), reply_markup=search_kb(sid, s_["res"], page))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("sx:"))
async def search_close(call: CallbackQuery):
    SEARCH.pop(call.data.split(":", 1)[1], None)
    await call.answer()
    if isinstance(call.message, Message):
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("sg:"))
async def full_song(call: CallbackQuery, lang: str):
    q = QUERIES.get(call.data.split(":", 1)[1])
    if not q or not isinstance(call.message, Message):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    async with job(call.from_user.id) as ok:
        if not ok:
            return await call.answer(t(lang, "busy"), show_alert=True)
        await call.answer()
        await send_song(call.message, lang, call.from_user.id, f"ytsearch1:{q}", f"q:{q.lower()[:80]}", q)


async def show_search(msg: Message, q: str, lang: str):
    q = q.strip()[:100]
    if len(q) < 2:
        return await msg.answer(t(lang, "short_q"))
    status = await msg.answer(t(lang, "search_wait", q=esc(q)))
    results = await services.search(q)
    if not results:
        return await status.edit_text(t(lang, "search_none"))
    await db.incr("search")
    sid = uuid.uuid4().hex[:8]
    SEARCH[sid] = {"q": q, "res": results}
    await status.edit_text(search_text(q, results, 0), reply_markup=search_kb(sid, results, 0))


@router.message(is_plain)
async def on_text(m: Message, lang: str):
    await show_search(m, m.text, lang)
