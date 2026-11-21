"""Клиент Bot API мессенджера MAX (dev.max.ru): публикация постов в канал.

- Адрес API: https://platform-api2.max.ru, токен в заголовке Authorization (без «Bearer»).
- TLS-сертификат API выдан УЦ Минцифры — добавляем его корни (app/certs) к стандартным.
- Медиа: POST /uploads?type=image|video -> url (+ token для видео) -> multipart поле «data» ->
  вложение {"type": "image"|"video", "payload": {"token": ...}}. До 12 фото/видео в сообщении.
  Видео после загрузки обрабатывается: пока не готово, API отвечает attachment.not.ready — повторяем.
- Отложенных сообщений в API нет: публикуем сами в нужную минуту (см. pipeline.max_job).
"""
from __future__ import annotations

import asyncio
import logging
import re
import ssl
from pathlib import Path

import certifi
import httpx

from app.models import MediaFile

log = logging.getLogger(__name__)

API = "https://platform-api2.max.ru"
CERTS = Path(__file__).resolve().parent / "certs"
MAX_ATTACHMENTS = 12


class MaxError(RuntimeError):
    pass


def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    for f in sorted(CERTS.glob("*.cer")):
        ctx.load_verify_locations(str(f))
    return ctx


def tg_html_to_max(html: str) -> str:
    """Разметка Telegram -> MAX: сворачиваемых цитат в MAX нет, остальное совпадает."""
    html = re.sub(r"<blockquote\s+expandable\s*>", "<blockquote>", html)
    html = re.sub(r"<tg-spoiler>|</tg-spoiler>", "", html)
    return html


class MaxClient:
    def __init__(self, token: str):
        # MAX — российский сервис: всегда напрямую, мимо VPN/прокси (trust_env=False игнорирует HTTPS_PROXY и т.п.)
        self.http = httpx.AsyncClient(base_url=API, headers={"Authorization": token}, timeout=120,
                                      verify=ssl_context(), trust_env=False)
        self.up = httpx.AsyncClient(timeout=600, verify=ssl_context(), trust_env=False)  # загрузка на CDN MAX

    async def stop(self) -> None:
        await self.http.aclose()
        await self.up.aclose()

    async def _call(self, method: str, path: str, **kw) -> dict:
        r = await self.http.request(method, path, **kw)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:300]}
        if r.status_code >= 400:
            raise MaxError(f"{r.status_code} {data}")
        return data

    async def me(self) -> dict:
        return await self._call("GET", "/me")

    async def chat(self, chat_id: int) -> dict:
        return await self._call("GET", f"/chats/{chat_id}")

    async def chat_by_link(self, link: str) -> dict:
        """Канал по публичной ссылке: https://max.ru/channel_Name или просто channel_Name."""
        slug = re.sub(r"^(https?://)?(web\.)?max\.ru/", "", link.strip()).strip("/@ ")
        return await self._call("GET", f"/chats/{slug}")

    async def bot_chats(self) -> dict[int, str]:
        """Каналы и чаты, куда добавили бота (из событий bot_added; списка чатов в API MAX нет)."""
        data = await self._call("GET", "/updates", params={"timeout": 0, "limit": 1000,
                                                           "types": "bot_added,bot_started,message_created"})
        found: dict[int, str] = {}
        for u in data.get("updates", []):
            cid = u.get("chat_id") or (u.get("message") or {}).get("recipient", {}).get("chat_id")
            if cid:
                found[int(cid)] = ""
        for cid in list(found):
            try:
                found[cid] = (await self.chat(cid)).get("title") or ""
            except MaxError:
                pass
        return found

    async def upload(self, m: MediaFile) -> dict:
        kind = "video" if m.kind == "video" else "image"
        info = await self._call("POST", "/uploads", params={"type": kind})
        path = Path(m.path)
        with path.open("rb") as f:
            r = await self.up.post(info["url"], files={"data": (path.name, f)})
        r.raise_for_status()
        if kind == "video":
            token = info.get("token")
        else:
            token = _find_token(r.json())
        if not token:
            raise MaxError(f"не получил токен загрузки {kind}: {r.text[:200]}")
        return {"type": kind, "payload": {"token": token}}

    async def send_post(self, chat_id: int, media: list[MediaFile], html: str) -> str:
        """Публикует пост. Возвращает id сообщения (mid)."""
        attachments = [await self.upload(m) for m in media[:MAX_ATTACHMENTS]]
        body = {"text": tg_html_to_max(html), "format": "html", "attachments": attachments}
        delay = 3
        for _ in range(12):  # видео обрабатывается после загрузки — ждём до ~5 минут
            try:
                data = await self._call("POST", "/messages", params={"chat_id": chat_id}, json=body)
                return (data.get("message") or {}).get("body", {}).get("mid", "")
            except MaxError as e:
                if "attachment.not.ready" not in str(e):
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        raise MaxError("медиа так и не обработались на стороне MAX")


def _find_token(obj) -> str | None:
    """Ответ загрузки фото: {"photos": {"<id>": {"token": "..."}}} — ищем token на любой глубине."""
    if isinstance(obj, dict):
        if isinstance(obj.get("token"), str):
            return obj["token"]
        for v in obj.values():
            if t := _find_token(v):
                return t
    return None
