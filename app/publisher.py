"""Отправка медиа + подписи в Telegram через Bot API (aiogram)."""
from __future__ import annotations

from aiogram import Bot
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, Message

from app.models import MediaFile


async def send_post(bot: Bot, chat_id: int | str, media: list[MediaFile], caption: str | None) -> list[Message]:
    """Одно медиа — send_photo/send_video, несколько — альбом (подпись на первом элементе)."""
    media = media[:10]
    if not media:
        return [await bot.send_message(chat_id, caption or "(пусто)")]

    if len(media) == 1:
        m = media[0]
        if m.kind == "video":
            return [await bot.send_video(chat_id, FSInputFile(m.path), caption=caption, **_video_kwargs(m))]
        return [await bot.send_photo(chat_id, FSInputFile(m.path), caption=caption)]

    group = []
    for n, m in enumerate(media):
        cap = caption if n == 0 else None
        if m.kind == "video":
            group.append(InputMediaVideo(media=FSInputFile(m.path), caption=cap, **_video_kwargs(m)))
        else:
            group.append(InputMediaPhoto(media=FSInputFile(m.path), caption=cap))
    return await bot.send_media_group(chat_id, media=group)


def _video_kwargs(m: MediaFile) -> dict:
    kw: dict = {"supports_streaming": True}
    if m.width and m.height:
        kw.update(width=m.width, height=m.height)
    if m.duration:
        kw["duration"] = m.duration
    if m.thumb:
        kw["thumbnail"] = FSInputFile(m.thumb)
    return kw
