"""Отложенные сообщения в канале через user-аккаунт (Telethon).

Bot API не умеет отложенные сообщения, а пользовательский аккаунт умеет: пост
появляется в разделе «Отложенные» канала, и Telegram сам публикует его в нужное время.
Аккаунт-читатель должен быть администратором канала с правом публикации.
Лимит Telegram — 100 отложенных сообщений на чат (альбом из N фото = N сообщений).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from telethon import TelegramClient
from telethon.tl.functions.messages import GetScheduledHistoryRequest
from telethon.tl.types import DocumentAttributeVideo, Message

from app.models import MediaFile

log = logging.getLogger(__name__)


class ChannelScheduler:
    def __init__(self, client: TelegramClient):
        self.client = client
        self._entities: dict[str, object] = {}

    async def _entity(self, target: str):
        if target not in self._entities:
            self._entities[target] = await self.client.get_entity(target)
        return self._entities[target]

    async def can_post(self, target: str) -> bool:
        try:
            perms = await self.client.get_permissions(await self._entity(target), "me")
        except Exception as e:
            log.warning("Не удалось проверить права в %s: %s", target, e)
            return False
        return bool(perms.is_creator or (perms.is_admin and perms.post_messages))

    async def scheduled(self, target: str) -> list[Message]:
        res = await self.client(GetScheduledHistoryRequest(peer=await self._entity(target), hash=0))
        return [m for m in res.messages if isinstance(m, Message)]

    async def schedule(self, target: str, media: list[MediaFile], caption: str, when: datetime) -> list[int]:
        """Ставит пост в отложенные. Возвращает id отложенных сообщений."""
        entity = await self._entity(target)
        when = when.astimezone(timezone.utc)
        if not media:
            msg = await self.client.send_message(entity, caption, parse_mode="html",
                                                 link_preview=False, schedule=when)
            return [msg.id]
        media = media[:10]
        if len(media) == 1:
            m = media[0]
            kwargs: dict = {"caption": caption, "parse_mode": "html", "schedule": when}
            if m.kind == "video":
                kwargs.update(supports_streaming=True, thumb=m.thumb,
                              attributes=[DocumentAttributeVideo(duration=m.duration or 0, w=m.width or 0,
                                                                 h=m.height or 0, supports_streaming=True)])
            msg = await self.client.send_file(entity, _sendable(m), **kwargs)
            return [msg.id]
        files = [_sendable(m) for m in media]
        msgs = await self.client.send_file(entity, files, caption=[caption] + [""] * (len(files) - 1),
                                           parse_mode="html", schedule=when, supports_streaming=True)
        return [m.id for m in (msgs if isinstance(msgs, list) else [msgs])]


def _sendable(m: MediaFile) -> str:
    """Telethon определяет тип по расширению: фото без расширения ушло бы файлом. Пересохраняем в .jpg."""
    p = Path(m.path)
    if m.kind != "photo" or p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        return str(p)
    out = p.with_suffix(".jpg")
    if not out.exists():
        with Image.open(p) as im:
            im.convert("RGB").save(out, "JPEG", quality=92)
    return str(out)
