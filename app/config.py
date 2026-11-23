"""Глобальные настройки (.env) и профили каналов (config/channels/*.yaml)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from typing import Annotated

from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    bot_token: str
    tg_api_id: int
    tg_api_hash: str
    # Прокси только для Telegram (бот, аккаунт-читатель, telegra.ph). Остальное — напрямую.
    # auto (по умолчанию) — если на этой машине слушает локальный прокси (SOCKS5 127.0.0.1:1080 или
    # HTTP 127.0.0.1:8118, как у Xray на сервере), Telegram идёт через него, иначе напрямую.
    # Можно задать явно: socks5://127.0.0.1:1080, http://127.0.0.1:8118; none — всегда напрямую.
    tg_proxy: str | None = "auto"
    # MAX (max.ru): токен бота из business.max.ru → Чат-боты → Настройки. Пусто — MAX выключен.
    max_token: str | None = None

    def model_post_init(self, _ctx) -> None:
        value = (self.tg_proxy or "").strip().lower()
        if value in ("", "none", "off", "direct"):
            self.tg_proxy = None
        elif value == "auto":
            self.tg_proxy = _detect_local_proxy()
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # Обработка текста: "openai" — любой OpenAI-совместимый API (OVH, Groq, OpenRouter, SambaNova…);
    # "anthropic" — Claude; "none" — правила без нейросети
    llm_provider: str = "openai"
    llm_api_key: str | None = None    # пусто — анонимный доступ (OVHcloud AI Endpoints)
    llm_base_url: str = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"
    llm_model: str = "Qwen3.8-27B"
    llm_vision: bool = True           # слать модели фото и превью видео (нужна vision-модель)
    llm_proxy: str | None = None      # прокси основного OpenAI-совместимого API
    llm_rpm: int = Field(default=0, ge=0)                  # лимит запросов в минуту (0 — без ограничения; без ключа — 2)

    # Резервная модель (включается при 429 / недоступности основной)
    llm_fallback_base_url: str | None = None
    llm_fallback_model: str | None = None
    llm_fallback_api_key: str | None = None
    llm_fallback_proxy: str | None = None
    llm_fallback_rpm: int = Field(default=0, ge=0)
    llm_fallback_vision: bool | None = None  # None — наследует LLM_VISION


    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    data_dir: Path = ROOT / "data"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        return v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autoposting.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def telethon_proxy(self) -> dict | None:
        """TG_PROXY в формате Telethon (python-socks)."""
        if not self.tg_proxy:
            return None
        from urllib.parse import urlparse

        u = urlparse(self.tg_proxy)
        proxy_type = {"socks5": "socks5", "socks5h": "socks5", "socks4": "socks4", "http": "http", "https": "http"}[u.scheme]
        out = {"proxy_type": proxy_type, "addr": u.hostname, "port": u.port, "rdns": True}
        if u.username:
            out.update(username=u.username, password=u.password or "")
        return out

    @property
    def log_path(self) -> Path:
        return self.data_dir / "app.log"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "reader"  # Telethon добавит .session


# Сначала — собственный прокси приложения (deploy/proxy.sh), затем общий Xray из srv-tg-proxy.sh
AUTO_PROXIES = (("127.0.0.1", 10808, "socks5"), ("127.0.0.1", 1080, "socks5"), ("127.0.0.1", 8118, "http"))


def _detect_local_proxy() -> str | None:
    """Ищет локальный прокси к Telegram (Xray из srv-tg-proxy.sh слушает 1080/8118 только на 127.0.0.1)."""
    import socket

    for host, port, scheme in AUTO_PROXIES:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return f"{scheme}://{host}:{port}"
        except OSError:
            continue
    return None


class TelegramSource(BaseModel):
    username: str | None = None   # публичный канал: @channel или t.me/channel
    invite: str | None = None     # приватный канал: ссылка-приглашение t.me/+XXXX (аккаунт вступит сам)
    name: str | None = None       # подпись для логов (необязательно)
    min_views: int = 0            # игнорировать посты с меньшим числом просмотров
    max_age_hours: int = 72       # не брать старые посты
    min_age_hours: float = 0      # «выдержка»: брать пост только через N часов, когда наберёт просмотры
    initial_limit: int = 20       # сколько последних постов взять при первом запуске

    @field_validator("username")
    @classmethod
    def _norm(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = v.strip().removeprefix("https://").removeprefix("http://").removeprefix("t.me/")
        return v.lstrip("@")

    @field_validator("invite")
    @classmethod
    def _invite_hash(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = v.strip().removeprefix("https://").removeprefix("http://").removeprefix("t.me/")
        return v.removeprefix("joinchat/").lstrip("+")

    def model_post_init(self, _ctx) -> None:
        if not (self.username or self.invite):
            raise ValueError("У источника telegram нужен username или invite")

    @property
    def key(self) -> str:
        return self.username or f"+{self.invite}"

    @property
    def label(self) -> str:
        return self.name or (f"@{self.username}" if self.username else f"приватный {self.invite[:6]}…")


class WebSource(BaseModel):
    rss: str | None = None            # RSS-лента сайта с рецептами
    pages: list[str] = Field(default_factory=list)  # страницы-каталоги, с которых собираем ссылки на рецепты
    link_pattern: str | None = None   # regex для ссылок на рецепты на страницах-каталогах
    urls: list[str] = Field(default_factory=list)  # отдельные страницы рецептов
    max_entries: int = 10


class Sources(BaseModel):
    telegram: list[TelegramSource] = Field(default_factory=list)
    web: list[WebSource] = Field(default_factory=list)


class ChannelProfile(BaseModel):
    key: str
    title: str
    content_type: str = "recipe"      # recipe — рецепты, deal — находки, news — новости
    target: str                       # @username канала или -100... id
    moderation_chat: int | None = None  # куда слать черновики; по умолчанию — первому из ADMIN_IDS
    timezone: str = "Europe/Moscow"
    schedule: list[str] = Field(default_factory=lambda: ["09:00", "13:00", "18:00", "21:00"])
    # scheduled — одобренные посты уходят в «Отложенные» канала (через аккаунт-читатель, он должен быть админом);
    # bot — бот сам публикует из очереди в слоты расписания
    publish_mode: str = "scheduled"
    # Для publish_mode=scheduled: N постов в день, время — случайное, но равномерно: окно day_start–day_end
    # делится на N равных интервалов, в каждом — один пост в случайную минуту.
    # posts_per_day и moderation можно менять из бота (/channels) — там значения хранятся в базе.
    posts_per_day: int | None = None  # по умолчанию — столько, сколько слотов в schedule
    day_start: str = "08:00"
    day_end: str = "23:00"
    moderation: bool = True           # True — черновики на проверку; False — сразу в отложенные
    buffer_days: int = 3              # на сколько дней вперёд держать посты (сбор и обработка — по потребности)
    max_scheduled: int = 100          # лимит Telegram на отложенные сообщения в чате
    min_lead_minutes: int = 15        # ближайший слот — не раньше чем через N минут
    collect_every_minutes: int = 30
    max_pending_drafts: int = 10      # не заваливать модератора: больше черновиков не создаём
    max_video_mb: int = 50            # лимит Bot API на загрузку файла
    dedup_days: int = 90              # за какой период искать дубли (по картинке и по рецепту)
    sources: Sources = Field(default_factory=Sources)
    style: str                        # правила оформления поста для LLM
    examples: list[str] = Field(default_factory=list)
    rubrics: list[str] = Field(default_factory=list)  # рубрики-хэштеги канала (модель выбирает одну)
    ctas: list[str] = Field(default_factory=list)    # призывы в конце заголовка (режим без LLM)
    emojis: list[str] = Field(default_factory=list)  # эмодзи в конце заголовка (режим без LLM)
    # Для находок: шаблоны партнёрских ссылок по площадкам (wb, ozon, ali, ym).
    # {url} — чистая ссылка на товар, {url_encoded} — она же, закодированная для параметра, {id} — id товара.
    affiliate: dict[str, str | None] = Field(default_factory=dict)
    ad_footer: str | None = None      # строка в конце поста (например, рекламная маркировка)


def load_profiles(directory: Path = ROOT / "config" / "channels") -> dict[str, ChannelProfile]:
    profiles: dict[str, ChannelProfile] = {}
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        p = ChannelProfile.model_validate(data)
        profiles[p.key] = p
    if not profiles:
        raise RuntimeError(f"Нет профилей каналов в {directory}")
    return profiles
