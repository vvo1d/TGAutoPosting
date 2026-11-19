"""Переписывание исходного материала в карточку рецепта с помощью LLM.

Модель получает текст источника + картинки (фото / превью видео) и возвращает
структурированный ответ: рецепт ли это, «цепляющая» первая строка, ингредиенты, шаги.
Финальную подпись собирает formatter — так формат поста всегда одинаковый.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time

import httpx
from PIL import Image
from pydantic import BaseModel, Field

from app.config import ChannelProfile, Settings
from app.models import MediaFile

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024  # лимит подписи к медиа в Telegram


class RecipePost(BaseModel):
    # Все поля обязательные: иначе модели (особенно открытые) любят их пропускать
    is_recipe: bool = Field(description="true, если в материале есть конкретный рецепт, пригодный для поста")
    reject_reason: str | None = Field(description="Почему не подходит (реклама, нет рецепта, мало данных); null, если рецепт")
    title: str = Field(description="Название блюда, коротко и аппетитно, без эмодзи; пусто, если не рецепт")
    emoji: str = Field(description="Одно эмодзи, подходящее к блюду")
    intro: str = Field(description="1–2 коротких предложения: чем хорошо блюдо + призыв сохранить/приготовить, в конце 1 эмодзи (не то же, что в emoji)")
    time: str | None = Field(description="Общее время приготовления, например «40 мин» или «1 ч + ночь в холодильнике»; null, если неизвестно")
    servings: str | None = Field(description="Количество порций, например «4 порции»; null, если неизвестно")
    difficulty: str | None = Field(description="Сложность: «легко», «средне» или «сложно»")
    rubric: str | None = Field(description="Рубрика из списка рубрик канала (одно слово без #)")
    ingredients: list[str] = Field(description="Ингредиенты в формате «Название — количество», без маркеров; пусто, если не рецепт")
    steps: list[str] = Field(description="Шаги приготовления, каждый 1–2 коротких предложения, без номеров; пусто, если шагов нет в источнике")

    @property
    def accepted(self) -> bool:
        return self.is_recipe


SYSTEM_TEMPLATE = """Ты — редактор Telegram-канала «{title}».
Тебе присылают материал из другого канала или с сайта. Твоя задача — сделать из него карточку рецепта для нашего канала.

Правила оформления:
{style}

Общие правила:
- Пиши всегда на русском языке, даже если источник на другом языке (переводи и названия ингредиентов).
- Пиши своими словами, не копируй исходный текст дословно.
- Удаляй рекламу, ссылки, упоминания других каналов, авторов, хэштеги, призывы подписаться на чужие ресурсы.
- Не выдумывай ингредиенты и количества, которых нет в материале или не видно на изображениях. Если рецепта нет или данных не хватает — верни is_recipe=false и причину.
- Реклама, розыгрыши, подборки без рецептов, новости — это is_recipe=false.
- Шаги (steps): перескажи процесс из источника по шагам, 3–8 шагов, кратко, в повелительном наклонении («Смешайте…»), с температурой и временем, если они есть. Если в источнике процесса нет — оставь steps пустым, не выдумывай.
- time и servings заполняй, только если они есть в источнике или однозначно следуют из рецепта (например, сумма времени выпекания и охлаждения). difficulty оцени сам.
- rubric выбери из списка: {rubrics}.
- Весь пост должен уложиться примерно в {limit} символов: если рецепт большой — сокращай формулировки шагов.
- Тип медиа поста: {media_kind}.

Примеры тона и подачи нашего канала:
{examples}
"""


class Rewriter:
    """Общая логика промпта; сам вызов модели — в backend (Claude или OpenAI-совместимый API)."""

    def __init__(self, settings: Settings):
        self.backend = None
        if settings.llm_provider == "none":
            log.info("Обработка текста: правила, без LLM")
            return
        if settings.llm_provider == "anthropic":
            self.backend = AnthropicBackend(settings)
        else:
            self.backend = OpenAICompatBackend(settings)
        log.info("LLM: %s / %s", settings.llm_provider, self.backend.model)

    @property
    def uses_llm(self) -> bool:
        return self.backend is not None

    async def rewrite(
        self, profile: ChannelProfile, text: str, media: list[MediaFile], note: str | None = None
    ) -> RecipePost:
        if self.backend is None:
            from app import rules

            r = rules.process(profile, text, media, seed=None if note else 0)
            return RecipePost(is_recipe=r.is_recipe, reject_reason=r.reject_reason, title=r.title,
                              emoji=r.emoji, intro=r.intro, time=None, servings=None, difficulty=None,
                              rubric=None, ingredients=r.ingredients, steps=r.steps)
        media_kind = "видео" if any(m.kind == "video" for m in media) else "фото"
        system = SYSTEM_TEMPLATE.format(
            title=profile.title,
            style=profile.style.strip(),
            media_kind=media_kind,
            limit=CAPTION_LIMIT - 80,
            rubrics=", ".join(profile.rubrics) or "(нет)",
            examples="\n\n---\n\n".join(e.strip() for e in profile.examples) or "(нет)",
        )
        prompt = f"Исходный материал:\n<source>\n{text.strip() or '(текста нет, только медиа)'}\n</source>"
        if note:
            prompt += f"\n\nДополнительное указание редактора: {note}"
        images = _images_for_llm(media) if self.backend.vision else []
        post = await self.backend.complete(system, prompt, images, RecipePost)
        if post is None:
            return _refused_recipe()
        if post.is_recipe and (not post.title.strip() or len(post.ingredients) < 2):
            log.info("Модель вернула неполный пост — повторяю запрос")
            post = await self.backend.complete(
                system, prompt + "\n\nВажно: если это рецепт, обязательно заполни title, intro и ingredients.",
                images, RecipePost)
            if post is None:
                return _refused_recipe()
            if post.is_recipe and (not post.title.strip() or len(post.ingredients) < 2):
                raise RuntimeError("Модель дважды вернула пустой заголовок или список ингредиентов")
        return post

    async def rewrite_deal(self, profile: ChannelProfile, text: str, media: list[MediaFile], links: list,
                           note: str | None = None):
        from app import deals

        if self.backend is None:
            raise RuntimeError("Для канала находок нужна LLM (LLM_PROVIDER не может быть none)")
        system, prompt = deals.system_prompt(profile), deals.build_prompt(text, links, note)
        images = _images_for_llm(media) if self.backend.vision else []
        post = await self.backend.complete(system, prompt, images, deals.DealPost)
        if post is None:
            return deals.DealPost(is_deal=False, reject_reason="модель отказалась", title="", emoji="", intro="",
                                  items=[], promo_code=None, rubric=None)
        if post.is_deal and (not post.title.strip() or not post.items):
            log.info("Модель вернула неполный пост — повторяю запрос")
            post = await self.backend.complete(
                system, prompt + "\n\nВажно: если пост подходит, обязательно заполни title и items.", images, deals.DealPost)
            if post is None or (post.is_deal and (not post.title.strip() or not post.items)):
                raise RuntimeError("Модель дважды вернула пустой заголовок или список товаров")
        return post


def _refused_recipe() -> "RecipePost":
    return RecipePost(is_recipe=False, reject_reason="модель отказалась", title="", emoji="", intro="", time=None,
                      servings=None, difficulty=None, rubric=None, ingredients=[], steps=[])


class AnthropicBackend:
    vision = True

    def __init__(self, settings: Settings):
        import anthropic

        self.client = anthropic.AsyncAnthropic()
        self.model = settings.claude_model
        self.effort = settings.claude_effort

    async def complete(self, system: str, prompt: str, images: list[str], schema: type[BaseModel]):
        """Возвращает объект schema или None, если модель отказалась отвечать."""
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}}
            for img in images
        ]
        content.append({"type": "text", "text": prompt})
        resp = await self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # при отказе модели запрос автоматически повторяется на запасной
            output_config={"effort": self.effort},
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
        )
        if resp.stop_reason == "refusal":
            log.warning("Модель отказалась: %s", resp.stop_details.explanation if resp.stop_details else "")
            return None
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise RuntimeError(f"Не удалось разобрать ответ Claude (stop_reason={resp.stop_reason}, request_id={resp._request_id})")
        return resp.parsed_output


class OpenAICompatBackend:
    """SambaNova Cloud и любой другой OpenAI-совместимый API (base_url + ключ + модель)."""

    def __init__(self, settings: Settings):
        from openai import AsyncOpenAI

        http_client = None
        if not settings.llm_api_key:
            # Анонимный доступ (например, OVHcloud AI Endpoints): шлём запрос без заголовка Authorization
            log.warning("LLM_API_KEY не задан — работаю анонимно с %s", settings.llm_base_url)

            async def _strip_auth(request: httpx.Request) -> None:
                request.headers.pop("Authorization", None)

            http_client = httpx.AsyncClient(event_hooks={"request": [_strip_auth]}, timeout=180)
        self.client = AsyncOpenAI(api_key=settings.llm_api_key or "anonymous", base_url=settings.llm_base_url,
                                  max_retries=2, http_client=http_client)
        self.model = settings.llm_model
        self.vision = settings.llm_vision
        rpm = settings.llm_rpm or (2 if not settings.llm_api_key else 0)  # анонимный OVH: 2 запроса/мин
        self._interval = 60 / rpm + 1 if rpm else 0
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        # Что принимает сервер, выясняем по ходу и запоминаем (400 -> пробуем вариант попроще)
        self._formats = ["json_schema", "json_object"]
        self._max_images = 4

    async def complete(self, system: str, prompt: str, images: list[str], model_cls: type[BaseModel]):
        from openai import BadRequestError

        schema = model_cls.model_json_schema()
        sys_json = system + "\n\nОтветь строго одним JSON-объектом по схеме:\n" + json.dumps(schema, ensure_ascii=False)

        def build(imgs: list[str]) -> list[dict]:
            user: list[dict] | str = prompt
            if imgs:
                user = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{i}"}} for i in imgs]
                user.append({"type": "text", "text": prompt})
            return [{"role": "system", "content": sys_json}, {"role": "user", "content": user}]

        def fmt(kind: str) -> dict:
            if kind == "json_schema":
                return {"type": "json_schema", "json_schema": {"name": "post", "schema": schema}}
            return {"type": "json_object"}

        resp = None
        while resp is None:
            async with self._lock:  # соблюдаем лимит запросов в минуту
                wait = self._last_call + self._interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = time.monotonic()
            imgs = images[: self._max_images]
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model, messages=build(imgs), max_tokens=8000, temperature=0.7,
                    response_format=fmt(self._formats[0]),
                )
            except BadRequestError as e:
                msg = (e.message or "")[:200]
                if imgs and len(imgs) > 1:
                    log.warning("Сервер не принял %d картинок (%s) — шлю по одной", len(imgs), msg)
                    self._max_images = 1
                elif len(self._formats) > 1:
                    log.warning("%s не принят (%s) — пробую %s", self._formats[0], msg, self._formats[1])
                    self._formats.pop(0)
                elif imgs:
                    log.warning("Сервер не принял картинку (%s) — работаю только с текстом", msg)
                    self._max_images = 0
                else:
                    raise

        choice = resp.choices[0]
        raw = choice.message.content or ""
        if choice.finish_reason == "length":
            raise RuntimeError("Ответ модели обрезан по длине")
        try:
            return model_cls.model_validate_json(_extract_json(raw))
        except ValueError as e:
            raise RuntimeError(f"Модель вернула невалидный JSON: {e}; начало ответа: {raw[:200]!r}")


def _extract_json(raw: str) -> str:
    """Убираем рассуждения <think>…</think> и обёртку ```json, если модель их добавила."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    m = re.search(r"\{.*\}", raw, flags=re.S)
    return m.group(0) if m else raw


def format_caption(post: RecipePost) -> str:
    """Карточка рецепта в HTML. Укладываем в лимит подписи: сокращаем шаги, потом убираем лишнее."""
    steps = [st.strip() for st in post.steps if st.strip()]
    intro, ingredients = post.intro.strip(), list(post.ingredients)
    while True:
        html = _card_html(post, intro, ingredients, steps)
        if visible_len(html) <= CAPTION_LIMIT:
            return html
        if any(len(st) > 90 for st in steps):  # сначала — шаги до первого предложения
            steps = [re.split(r"(?<=[.!?])\s", st, maxsplit=1)[0] for st in steps]
            steps = [st if len(st) <= 140 else st[:137].rsplit(" ", 1)[0] + "…" for st in steps]
        elif len(steps) > 3:
            steps = steps[:-1]
        elif steps:
            steps = []
        elif intro:
            intro = ""
        elif len(ingredients) > 3:
            ingredients = ingredients[:-1]
        else:
            return html


def _card_html(post: RecipePost, intro: str, ingredients: list[str], steps: list[str]) -> str:
    e = html_escape
    parts = [f"<b>{e(post.title.strip())}</b> {e(post.emoji.strip())}".strip()]
    if intro:
        parts[0] += "\n" + e(intro)
    meta = []
    if post.time:
        meta.append(f"⏱ {e(post.time)}")
    if post.servings:
        meta.append(f"🍽 {e(post.servings)}")
    if post.difficulty:
        meta.append(f"👌 {e(post.difficulty)}")
    if meta:
        parts.append(" · ".join(meta))
    if ingredients:
        items = "\n".join(f"• {e(i.strip().lstrip('-•–— ').strip())}" for i in ingredients)
        parts.append(f"<b>Ингредиенты:</b>\n{items}")
    if steps:
        body = "\n".join(f"{n}. {e(st)}" for n, st in enumerate(steps, 1))
        parts.append(f"<blockquote expandable><b>Как готовить</b> 👇\n{body}</blockquote>")
    if post.rubric:
        tag = re.sub(r"[^\w]", "", post.rubric.lower().replace(" ", "_"))
        if tag:
            parts.append(f"#{tag}")
    return "\n\n".join(parts)


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def visible_len(html: str) -> int:
    """Длина текста так, как её считает Telegram: без тегов и с раскрытыми &-сущностями."""
    import html as _html

    return len(_html.unescape(re.sub(r"<[^>]+>", "", html)))


def _images_for_llm(media: list[MediaFile], limit: int = 4) -> list[str]:
    """Фото и превью видео -> base64 JPEG, уменьшенные до 1568px по длинной стороне."""
    paths = [m.path if m.kind == "photo" else m.thumb for m in media]
    out = []
    for p in [p for p in paths if p][:limit]:
        try:
            with Image.open(p) as im:
                im = im.convert("RGB")
                im.thumbnail((1568, 1568))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=85)
                out.append(base64.standard_b64encode(buf.getvalue()).decode())
        except Exception as e:  # битый файл не должен ронять обработку
            log.warning("Не удалось подготовить изображение %s: %s", p, e)
    return out
