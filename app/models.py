from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field


@dataclass
class MediaFile:
    kind: str                 # "photo" | "video"
    path: str
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    thumb: str | None = None  # превью видео (для Claude и для Telegram)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MediaFile":
        return cls(**d)


@dataclass
class RawPost:
    """Сырой материал из источника, до обработки Claude."""
    source: str               # "tg:channel" | "web:domain"
    source_ref: str           # id сообщения / url — уникален в рамках source
    text: str
    media: list[MediaFile] = field(default_factory=list)
    views: int | None = None
    url: str | None = None
    links: list[str] = field(default_factory=list)  # ссылки из поста (в т.ч. скрытые под текстом и в кнопках)

    @property
    def text_hash(self) -> str:
        return text_fingerprint(self.text)


def text_fingerprint(text: str) -> str:
    """Отпечаток текста для поиска дублей (один рецепт часто гуляет по разным каналам)."""
    norm = re.sub(r"[^\w]+", " ", text.lower())
    norm = re.sub(r"\s+", " ", norm).strip()[:300]
    return hashlib.sha1(norm.encode()).hexdigest()
