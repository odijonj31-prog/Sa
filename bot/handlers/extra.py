import json
import logging

from aiogram import Bot, F, Router
from aiogram.types import (InlineQuery, InlineQueryResultCachedAudio, InlineQueryResultCachedPhoto,
                           InlineQueryResultCachedVideo, InlineQueryResultsButton, Message)

from .. import db
from .user import URL_RE, canon_key, extract_url, handle_link, is_link

log = logging.getLogger(__name__)
router = Router()          # inline rejim
group = Router()           # guruhlar: faqat linklar
group.message.filter(F.chat.type.in_({"group", "supergroup"}))


@group.message(is_link)
async def on_group_link(m: Message, bot: Bot, lang: str = "uz"):
    if m.from_user is None:
        return
    await handle_link(m, bot, lang, extract_url(m), 720, m.from_user.id)


@router.inline_query()
async def inline(q: InlineQuery, bot: Bot):
    text = (q.query or "").strip()
    results = []
    mt = URL_RE.search(text)
    if mt:
        row = await db.cache_get(canon_key(mt.group(0)))
        if row:
            for i, (kind, fid) in enumerate(json.loads(row["payload"])[:5]):
                rid = f"c{i}"
                if kind == "video":
                    results.append(InlineQueryResultCachedVideo(id=rid, video_file_id=fid, title=(row["title"] or "Video")[:60]))
                elif kind == "photo":
                    results.append(InlineQueryResultCachedPhoto(id=rid, photo_file_id=fid))
                elif kind == "audio":
                    results.append(InlineQueryResultCachedAudio(id=rid, audio_file_id=fid))
    await q.answer(results, cache_time=5, is_personal=False,
                   button=InlineQueryResultsButton(text="📥 Botda yuklash / Open bot", start_parameter="inline"))
