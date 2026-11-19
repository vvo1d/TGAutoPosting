"""Настройки, которые меняются из бота (/settings). Хранятся в базе (таблица flags),
значения по умолчанию — из профиля канала / констант ниже. Переживают перезапуски и /update."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.config import ChannelProfile


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    lo: int
    hi: int
    step: int
    default: Callable[[ChannelProfile | None], int]
    hint: str = ""


# Настройки канала
CHANNEL_PARAMS: dict[str, Param] = {p.name: p for p in [
    Param("ppd", "Постов в день", 1, 48, 1,
          lambda p: p.posts_per_day or len(p.schedule) or 5),
    Param("buffer_days", "Готовить вперёд, дней", 1, 30, 1, lambda p: p.buffer_days,
          "на сколько дней вперёд держать посты в отложке; когда запас полон — сбор и обработка не работают"),
    Param("max_pending", "Черновиков на проверке", 1, 50, 1, lambda p: p.max_pending_drafts),
    Param("collect_min", "Сбор раз в, мин", 10, 720, 10, lambda p: p.collect_every_minutes),
    Param("per_run", "Новых за один сбор", 1, 100, 5, lambda p: 15),
    Param("max_new", "Очередь необработанных", 5, 300, 5, lambda p: 30,
          "когда полная — сбор пропускается и ничего не качает"),
    Param("video_mb", "Видео до, МБ (0 — без видео)", 0, 50, 10, lambda p: min(p.max_video_mb, 50)),
    Param("ttl_h", "Хранить необработанное, ч", 6, 168, 6, lambda p: 48,
          "потом пост и его медиа удаляются"),
]}

# Общие настройки (на все каналы)
GLOBAL_PARAMS: dict[str, Param] = {p.name: p for p in [
    Param("media_mb", "Лимит медиа на диске, МБ", 200, 50000, 200, lambda p: 2000,
          "при превышении сбор останавливается"),
]}


def clamp(param: Param, value: int) -> int:
    return max(param.lo, min(param.hi, value))
