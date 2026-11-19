"""Чтение каналов-источников через user-аккаунт (Telethon).

Бот не может читать чужие каналы, поэтому нужен отдельный пользовательский аккаунт,
подписанный (или нет — для публичных каналов не обязательно) на источники.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import InviteRequestSentError, UserAlreadyParticipantError
from telethon.tl.custom import Message
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready, ChatInvitePeek, MessageEntityTextUrl, MessageEntityUrl

from app.config import Settings, TelegramSource
from app.models import MediaFile, RawPost

log = logging.getLogger(__name__)


@dataclass
class TgCandidate:
    post: RawPost          # без медиа — медиа качаем только для новых постов
    messages: list[Message]
    links: list[str]       # ссылки из поста (в т.ч. скрытые под текстом)


class TelegramReader:
    def __init__(self, settings: Settings):
        self.client = TelegramClient(str(settings.session_path), settings.tg_api_id, settings.tg_api_hash,
                                     proxy=settings.telethon_proxy)
        self._invite_cache: dict[str, object] = {}

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telethon не авторизован. Запустите: python -m scripts.login")

    async def stop(self) -> None:
        await self.client.disconnect()

    async def _resolve(self, src: TelegramSource):
        if src.username:
            return await self.client.get_entity(src.username)
        if src.invite in self._invite_cache:
            return self._invite_cache[src.invite]
        info = await self.client(CheckChatInviteRequest(src.invite))
        if isinstance(info, (ChatInviteAlready, ChatInvitePeek)):
            chat = info.chat
        else:
            try:
                updates = await self.client(ImportChatInviteRequest(src.invite))
                chat = updates.chats[0]
                log.info("Вступили в %s", getattr(chat, "title", src.label))
            except UserAlreadyParticipantError:
                chat = (await self.client(CheckChatInviteRequest(src.invite))).chat
            except InviteRequestSentError:
                raise RuntimeError(f"{src.label}: отправлена заявка на вступление, ждём одобрения админа канала")
        self._invite_cache[src.invite] = chat
        return chat

    async def fetch(self, src: TelegramSource, last_id: int | None) -> tuple[list[TgCandidate], int | None]:
        entity = await self._resolve(src)
        if last_id is None:
            msgs = await self.client.get_messages(entity, limit=src.initial_limit)
        else:
            msgs = await self.client.get_messages(entity, limit=100, min_id=last_id)
        if not msgs:
            return [], last_id
        # Слишком свежие посты не берём и не сдвигаем позицию за них — вернёмся к ним позже
        too_young = datetime.now(timezone.utc) - timedelta(hours=src.min_age_hours)
        young = [m.id for m in msgs if m.date > too_young]
        msgs = [m for m in msgs if m.date <= too_young]
        if young:
            new_last = min(young) - 1
            if last_id is not None:
                new_last = max(new_last, last_id)
        else:
            new_last = max(m.id for m in msgs)
        if not msgs:
            return [], new_last if last_id is not None else None

        # Альбомы приходят как несколько сообщений с общим grouped_id
        groups: dict[int, list[Message]] = {}
        for m in msgs:
            groups.setdefault(m.grouped_id or -m.id, []).append(m)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=src.max_age_hours)
        out: list[TgCandidate] = []
        for items in groups.values():
            items.sort(key=lambda m: m.id)
            first = items[0]
            if first.date < cutoff:
                continue
            if not any(_media_kind(m) for m in items):
                continue  # формат канала требует фото или видео
            views = max((m.views or 0) for m in items)
            if views < src.min_views:
                continue
            text = next((m.message for m in items if m.message), "") or ""
            url = (f"https://t.me/{src.username}/{first.id}" if src.username
                   else f"https://t.me/c/{entity.id}/{first.id}")
            post = RawPost(source=f"tg:{src.key}", source_ref=str(first.id), text=text, views=views, url=url)
            links = []
            for m in items:
                for ent, txt in m.get_entities_text():
                    if isinstance(ent, MessageEntityTextUrl):
                        links.append(ent.url)
                    elif isinstance(ent, MessageEntityUrl):
                        links.append(txt)
                for row in (m.buttons or []):  # кнопки «Купить» под постом
                    for b in row:
                        if getattr(b, "url", None):
                            links.append(b.url)
            post.links = list(dict.fromkeys(links))
            out.append(TgCandidate(post=post, messages=items, links=links))
        out.sort(key=lambda c: int(c.post.source_ref))
        return out, new_last

    async def download(self, cand: TgCandidate, dest: Path, max_video_mb: int) -> list[MediaFile]:
        dest.mkdir(parents=True, exist_ok=True)
        files: list[MediaFile] = []
        for m in cand.messages[:10]:
            kind = _media_kind(m)
            if not kind:
                continue
            if kind == "video" and (m.file.size or 0) > max_video_mb * 1024 * 1024:
                log.info("Пропуск видео %s: %.1f МБ > лимита", cand.post.url, m.file.size / 1e6)
                continue
            path = await self.client.download_media(m, file=str(dest / f"{m.id}"))
            if not path:
                continue
            mf = MediaFile(kind=kind, path=str(path))
            if kind == "video":
                mf.width, mf.height = m.file.width, m.file.height
                mf.duration = int(m.file.duration or 0) or None
                thumb = await self.client.download_media(m, file=str(dest / f"{m.id}_thumb"), thumb=-1)
                mf.thumb = str(thumb) if thumb else None
            files.append(mf)
        return files


def _media_kind(m: Message) -> str | None:
    if m.photo:
        return "photo"
    if m.video and not m.gif and not m.video_note:
        return "video"
    return None
