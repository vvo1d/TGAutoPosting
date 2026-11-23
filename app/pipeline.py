"""Конвейер: сбор -> переписывание -> модерация -> очередь -> публикация."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import math
import re
import random
import logging
import shutil
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon.errors import AuthKeyDuplicatedError, ScheduleTooMuchError, UnauthorizedError

from app import deals, dedup
from app.links import LinkResolver, canonical, wb_articles
from app.max_api import MaxClient, MaxError
from app.config import ChannelProfile, Settings
from app.db import DB
from app.models import MediaFile, RawPost
from app.publisher import send_post
from app.rules import AD_MARKERS
from app.scheduler_tg import ChannelScheduler
from app.settings_rt import CHANNEL_PARAMS, GLOBAL_PARAMS, clamp
from app.rewriter import CAPTION_LIMIT, RecipePost, Rewriter, format_caption, format_news, html_escape, visible_len
from app.sources.telegram import TelegramReader
from app.sources.market import MarketReader
from app.sources.web import WebReader

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, settings: Settings, profiles: dict[str, ChannelProfile], db: DB, bot: Bot,
                 tg: TelegramReader | None, web: WebReader, rewriter: Rewriter):
        self.settings, self.profiles, self.db, self.bot = settings, profiles, db, bot
        self.tg, self.web, self.rewriter = tg, web, rewriter
        self.links = LinkResolver()
        self.max = MaxClient(settings.max_token) if settings.max_token else None
        self.market = MarketReader()
        # Сбор и обработка идут независимо: черновики появляются, пока сбор ещё качает видео
        self._collect_locks = {k: asyncio.Lock() for k in profiles}
        self._process_locks = {k: asyncio.Lock() for k in profiles}
        self._sync_locks = {k: asyncio.Lock() for k in profiles}
        self.channel_scheduler = ChannelScheduler(tg.client) if tg else None
        self._limit_notified: dict[str, bool] = {}
        self._quota_notified: set[str] = set()
        self._manual_adding = 0
        self._max_lock = asyncio.Lock()
        self._budget, self._quota_left, self._video_mb = 0, 0, 0

    def moderation_chat(self, p: ChannelProfile) -> int:
        chat = p.moderation_chat or (self.settings.admin_ids[0] if self.settings.admin_ids else None)
        if not chat:
            raise RuntimeError("Не задан moderation_chat в профиле и ADMIN_IDS в .env")
        return chat

    def _media_dir(self, p: ChannelProfile, post: RawPost) -> Path:
        key = hashlib.sha1(f"{post.source}|{post.source_ref}".encode()).hexdigest()[:16]
        return self.settings.media_dir / p.key / key

    # ---------- 1. сбор ----------

    async def tick(self, key: str) -> None:
        """Ручной запуск (/collect): собрать новое и сразу подготовить черновики."""
        await self.collect_job(key)
        await self.process_job(key)

    # ---------- пауза ----------
    # collect — сбор из источников и подготовка черновиков; post — постановка в отложенные / публикация

    async def is_paused(self, key: str, what: str) -> bool:
        return await self.db.get_flag(f"pause:{key}:{what}") == "1"

    async def set_paused(self, key: str, what: str, paused: bool) -> None:
        await self.db.set_flag(f"pause:{key}:{what}", "1" if paused else None)
        log.info("[%s] %s: %s", key, what, "пауза" if paused else "возобновлено")

    # ---------- настройки канала (меняются из бота, хранятся в базе) ----------

    async def setting(self, scope: str, name: str) -> int:
        """Настройка из бота (база) или значение по умолчанию. scope — ключ канала или "global"."""
        params = GLOBAL_PARAMS if scope == "global" else CHANNEL_PARAMS
        par = params[name]
        v = await self.db.get_flag(f"cfg:{scope}:{name}")
        return int(v) if v else par.default(self.profiles.get(scope))

    async def set_setting(self, scope: str, name: str, value: int) -> int:
        params = GLOBAL_PARAMS if scope == "global" else CHANNEL_PARAMS
        value = clamp(params[name], value)
        await self.db.set_flag(f"cfg:{scope}:{name}", str(value))
        log.info("Настройка %s.%s = %s", scope, name, value)
        return value

    async def posts_per_day(self, key: str) -> int:
        return await self.setting(key, "ppd")

    async def set_posts_per_day(self, key: str, n: int) -> int:
        return await self.set_setting(key, "ppd", n)

    async def moderation(self, key: str) -> bool:
        v = await self.db.get_flag(f"cfg:{key}:moderation")
        return self.profiles[key].moderation if v is None else v == "1"

    async def set_moderation(self, key: str, on: bool) -> None:
        await self.db.set_flag(f"cfg:{key}:moderation", "1" if on else "0")

    async def collect_job(self, key: str) -> None:
        if await self.is_paused(key, "collect"):
            return
        if self._collect_locks[key].locked():
            log.info("[%s] сбор уже идёт", key)
            return
        async with self._collect_locks[key]:
            try:
                await self.collect(self.profiles[key])
            except Exception:
                log.exception("[%s] сбор упал", key)

    async def process_job(self, key: str) -> None:
        if await self.is_paused(key, "collect") or self._process_locks[key].locked():
            return
        async with self._process_locks[key]:
            try:
                await self.process(self.profiles[key])
            except Exception:
                log.exception("[%s] обработка упала", key)

    # ---------- план: сколько постов нужно и сколько для этого собрать ----------

    async def plan(self, p: ChannelProfile) -> dict:
        """Работаем от потребности: цель = постов в день × дней вперёд (не больше лимита отложенных).
        Не хватает = цель − (в отложке впереди + в очереди + на проверке). Чтобы получить столько годных,
        с учётом выхода годных (доля обработанных, дошедших до поста) нужно ~не хватает / выход сырых."""
        counts = await self.db.count(p.key)
        ppd = await self.posts_per_day(p.key)
        days = await self.setting(p.key, "buffer_days")
        # Лимит Telegram — 100 отложенных СООБЩЕНИЙ на чат, альбом из N фото = N сообщений.
        # Поэтому потолок в постах = лимит / среднее число сообщений в посте этого канала.
        per_post = await self._msgs_per_post(p.key)
        max_posts = max(1, int(p.max_scheduled / per_post))
        target = min(ppd * days, max_posts)
        future = await self._future_scheduled(p.key)
        have = future + counts.get("approved", 0) + counts.get("pending", 0) + counts.get("draft", 0)
        need = max(0, target - have)
        yield_rate, sample = await self._yield_rate(p.key)
        backlog = counts.get("new", 0)
        raw_needed = math.ceil(need / yield_rate) if need else 0
        return {"ppd": ppd, "days": days, "target": target, "future": future,
                "per_post": per_post, "max_posts": max_posts, "max_days": max(1, max_posts // ppd),
                "limit": p.max_scheduled, "capped": ppd * days > max_posts,
                "approved": counts.get("approved", 0), "pending": counts.get("pending", 0),
                "have": have, "need": need, "yield": yield_rate, "yield_sample": sample,
                "backlog": backlog, "raw_needed": raw_needed, "collect": max(0, raw_needed - backlog)}

    async def _msgs_per_post(self, key: str) -> float:
        """Среднее число сообщений Telegram на пост (фото/видео в альбоме, максимум 10) по последним 100 постам."""
        cur = await self.db.conn.execute(
            """SELECT media, tg_msg_ids FROM items WHERE channel=?
                 AND status IN ('new','draft','pending','approved','scheduled','published')
               ORDER BY id DESC LIMIT 100""", (key,))
        sizes = []
        for media, ids in await cur.fetchall():
            n = len(json.loads(ids)) if ids and ids != "[]" else min(len(json.loads(media or "[]")), 10)
            if n:
                sizes.append(n)
        return round(sum(sizes) / len(sizes), 2) if len(sizes) >= 5 else 1.0

    async def _yield_rate(self, key: str) -> tuple[float, int]:
        """Доля обработанных материалов, которые стали постами (по последним 300). Не обработанное
        по нашей причине (устарело, перенос, отключённый источник) не учитывается."""
        cur = await self.db.conn.execute(
            """SELECT status FROM items WHERE channel=? AND status NOT IN ('new','failed')
                 AND (note IS NULL OR (note NOT LIKE 'устарел%' AND note NOT LIKE 'не перенесён%'
                      AND note NOT LIKE 'источник отключён%'))
               ORDER BY id DESC LIMIT 300""", (key,))
        rows = [r[0] for r in await cur.fetchall()]
        if len(rows) < 10:
            return 0.5, len(rows)  # истории мало — считаем, что годен каждый второй
        good = sum(r in ("draft", "pending", "approved", "scheduled", "published") for r in rows)
        return max(0.1, min(1.0, good / len(rows))), len(rows)

    async def collect(self, p: ChannelProfile) -> int:
        await self.expire_stale(p)
        plan = await self.plan(p)
        if plan["need"] <= 0:
            log.info("[%s] запас полон (%d/%d постов на %d дн.) — сбор не нужен",
                     p.key, plan["have"], plan["target"], plan["days"])
            return 0
        if plan["collect"] <= 0:
            log.info("[%s] не хватает %d постов, необработанных уже %d (нужно ~%d) — сбор не нужен",
                     p.key, plan["need"], plan["backlog"], plan["raw_needed"])
            return 0
        max_new = await self.setting(p.key, "max_new")
        backlog = plan["backlog"]
        if backlog >= max_new:
            log.info("[%s] очередь необработанных полная (%d/%d) — сбор пропущен", p.key, backlog, max_new)
            return 0
        quota = await self.setting("global", "media_mb") * 1_000_000
        if self.media_usage() >= quota:
            log.warning("[%s] медиа на диске больше лимита (%.0f МБ) — сбор пропущен", p.key, quota / 1e6)
            await self._notify_once(p, "media_quota",
                                    f"⚠️ Медиа на диске достигли лимита {quota / 1e6:.0f} МБ — сбор остановлен, пока не освободится место. "
                                    "Увеличить лимит: /settings → Общие.")
            return 0
        self._quota_notified.discard("media_quota")
        # Бюджет сбора: сколько нужно по плану, но не больше per_run и свободного места в очереди
        self._budget = min(plan["collect"], await self.setting(p.key, "per_run"), max_new - backlog)
        log.info("[%s] не хватает %d постов (выход годных %.0f%%) — собираю до %d материалов",
                 p.key, plan["need"], plan["yield"] * 100, self._budget)
        self._video_mb = await self.setting(p.key, "video_mb")
        self._quota_left = quota - self.media_usage()
        added = 0
        # Обходим источники в случайном порядке, чтобы ни один не был всегда первым
        tg_sources = random.sample(p.sources.telegram, len(p.sources.telegram))
        web_sources = random.sample(p.sources.web, len(p.sources.web))
        if self.tg:
            for src in tg_sources:
                try:
                    added += await self._collect_tg(p, src)
                except Exception as e:
                    log.warning("[%s] источник %s: %s", p.key, src.label, e)
        for src in web_sources:
            try:
                added += await self._collect_web(p, src)
            except Exception as e:
                log.warning("[%s] сайт %s: %s", p.key, src.rss or src.pages or src.urls, e)
        if added:
            log.info("[%s] новых материалов: %d", p.key, added)
        return added

    async def _collect_tg(self, p: ChannelProfile, src) -> int:
        source = f"tg:{src.key}"
        last_id = await self.db.get_last_id(p.key, source)
        candidates, new_last = await self.tg.fetch(src, last_id, allow_text=p.content_type == "news")
        added = 0
        for n, c in enumerate(candidates):
            if self._budget <= 0 or self._quota_left <= 0:
                # Лимит сбора: остальное заберём в следующий раз — позицию двигаем только до обработанного
                new_last = max(m.id for m in candidates[n - 1].messages) if n else last_id
                break
            tph = next((l for l in c.links if "telegra.ph/" in l), None)
            if tph and (extra := await self.web.fetch_telegraph(tph)):
                c.post.text = f"{c.post.text}\n\n{extra}"
            if await self.db.is_known(p.key, c.post):
                continue
            c.post.media = await self.tg.download(c, self._media_dir(p, c.post), self._video_mb)
            if not c.post.media and not (p.content_type == "news" and c.post.text.strip()):
                continue
            if item_id := await self.db.add_item(p.key, c.post):
                await self.db.update(item_id, phash=dedup.media_hash(c.post.media))
                added += 1
                self._spend(c.post.media)
        if new_last is not None:
            await self.db.set_last_id(p.key, source, new_last)
        return added

    async def _collect_web(self, p: ChannelProfile, src) -> int:
        added = 0
        for url in await self.web.list_urls(src):
            if self._budget <= 0 or self._quota_left <= 0:
                break
            cur = await self.db.conn.execute("SELECT 1 FROM items WHERE channel=? AND source_ref=?", (p.key, url))
            if await cur.fetchone():
                continue
            res = await self.web.fetch_recipe(url)
            if not res:
                continue
            post, images = res
            if await self.db.is_known(p.key, post):
                continue
            post.media = await self.web.download_images(images, self._media_dir(p, post))
            if post.media and (item_id := await self.db.add_item(p.key, post)):
                await self.db.update(item_id, phash=dedup.media_hash(post.media))
                added += 1
                self._spend(post.media)
        return added

    def _spend(self, media: list) -> None:
        self._budget -= 1
        self._quota_left -= sum(Path(m.path).stat().st_size for m in media if Path(m.path).exists())

    # ---------- место на диске ----------

    def media_usage(self) -> int:
        root = self.settings.media_dir
        return sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) if root.exists() else 0

    async def expire_stale(self, p: ChannelProfile) -> int:
        """Необработанные дольше ttl_h — устарели: удаляем их медиа и помечаем отклонёнными."""
        ttl = await self.setting(p.key, "ttl_h")
        since = (datetime.now(timezone.utc) - timedelta(hours=ttl)).isoformat(timespec="seconds")
        cur = await self.db.conn.execute(
            "SELECT id FROM items WHERE channel=? AND status='new' AND created_at<?", (p.key, since))
        ids = [r[0] for r in await cur.fetchall()]
        for i in ids:
            it = await self.db.get(i)
            await self.db.update(i, status="rejected", note=f"устарел: не обработан за {ttl} ч")
            self._cleanup(it)
        if ids:
            log.info("[%s] устарело и удалено необработанных: %d", p.key, len(ids))
        return len(ids)

    async def cleanup_media(self) -> tuple[int, int]:
        """Удаляет папки медиа, которые не нужны ни одному живому посту. Возвращает (папок, байт)."""
        root = self.settings.media_dir
        if not root.exists():
            return 0, 0
        # Сбор качает файлы ДО записи поста в базу: пока он идёт, «ничьи» папки — это скачиваемое сейчас
        if any(lock.locked() for lock in self._collect_locks.values()) or self._manual_adding:
            log.info("Уборка медиа отложена: идёт сбор")
            return 0, 0
        cur = await self.db.conn.execute(
            "SELECT media FROM items WHERE status IN ('new','draft','pending','approved') OR max_status='queued'")
        keep = {str(Path(m["path"]).resolve().parent)
                for (raw,) in await cur.fetchall() for m in json.loads(raw or "[]")}
        fresh = time.time() - 2 * 3600  # и на всякий случай не трогаем папки моложе 2 часов
        dirs = removed = 0
        for ch_dir in root.iterdir():
            if not ch_dir.is_dir():
                continue
            for d in ch_dir.iterdir():
                if d.is_dir() and str(d.resolve()) not in keep and d.stat().st_mtime < fresh:
                    removed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                    shutil.rmtree(d, ignore_errors=True)
                    dirs += 1
        if dirs:
            log.info("Уборка медиа: удалено папок %d, освобождено %.0f МБ", dirs, removed / 1e6)
        return dirs, removed

    async def cleanup_job(self) -> None:
        try:
            for p in self.profiles.values():
                await self.expire_stale(p)
            await self.cleanup_media()
        except Exception:
            log.exception("уборка медиа упала")

    async def _notify_once(self, p: ChannelProfile, tag: str, text: str) -> None:
        if tag not in self._quota_notified:
            self._quota_notified.add(tag)
            await self._notify(p, text)

    # ---------- 2. переписывание и отправка на модерацию ----------

    async def process(self, p: ChannelProfile) -> None:
        moderated = await self.moderation(p.key)
        plan = await self.plan(p)
        # Сколько годных постов сделать в этот раз: сколько не хватает до запаса,
        # а с проверкой — ещё и не больше свободных мест на проверке
        want = plan["need"]
        if moderated:
            want = min(want, await self.setting(p.key, "max_pending") - plan["pending"])
        if want <= 0 or not plan["backlog"]:
            return
        new_items = await self.db.by_status(p.key, "new", limit=1000)
        for it in new_items:  # досчитываем хэши картинок (для материалов, собранных до появления проверки)
            if not it.get("phash") and (h := dedup.media_hash(it["media"])):
                it["phash"] = h
                await self.db.update(it["id"], phash=h)
        items = self._mix_sources(new_items)
        auto_added = made = 0
        attempts_left = max(5, want * 3)  # за один проход не тратим на модель больше, чем нужно с запасом
        for item in items:
            if made >= want or attempts_left <= 0:
                break
            attempts_left -= 1
            try:
                ok = await self._rewrite_item(p, item)
                if ok:
                    made += 1
                    draft = await self.db.get(item["id"])
                    if moderated or deals.CRITICAL in (draft["note"] or ""):  # битая ссылка и т.п. — всегда на проверку
                        await self.send_draft(p, draft)
                    else:
                        await self.db.update(item["id"], status="approved")
                        auto_added += 1
            except Exception as e:
                # Лимит (429), сбой сервера (5xx), нет оплаты (402), модель снята (410), неверный ключ (401):
                # материал не виноват — оставляем его в очереди и сообщаем модератору
                status = getattr(e, "status_code", None)
                if status in (401, 402, 404, 410, 429) or (status and status >= 500):
                    log.warning("[%s] LLM недоступна (%s), продолжу позже: %s", p.key, status, str(e)[:200])
                    if status in (401, 402, 404, 410):
                        await self._notify(p, f"⚠️ Модель недоступна ({status}): {str(e)[:300]}")
                    return
                log.exception("[%s] item %s: ошибка обработки", p.key, item["id"])
                await self.db.update(item["id"], status="failed", note=str(e)[:500])
        if auto_added:
            log.info("[%s] без проверки: %d постов сразу в очередь отложенных", p.key, auto_added)
            asyncio.create_task(self.sync_job(p.key))

    @staticmethod
    def _mix_sources(items: list[dict]) -> list[dict]:
        """Перемешиваем источники: случайный порядок, по одному посту от каждого по кругу.
        Внутри источника первым идёт более популярный пост."""
        groups: dict[str, list[dict]] = {}
        for it in items:
            groups.setdefault(it["source"], []).append(it)
        queues = [sorted(g, key=lambda it: -(it["views"] or 0)) for g in groups.values()]
        random.shuffle(queues)
        # Товары, которые модератор прислал сам, — первыми
        queues.sort(key=lambda q: not q[0]["source"].startswith("manual:"))
        mixed: list[dict] = []
        while queues:
            for q in list(queues):
                mixed.append(q.pop(0))
                if not q:
                    queues.remove(q)
        return mixed

    async def _reject(self, item: dict, note: str) -> bool:
        log.info("[%s] item %s отклонён: %s", item["channel"], item["id"], note)
        await self.db.update(item["id"], status="rejected", note=note)
        self._cleanup(item)
        return False

    async def _future_scheduled(self, key: str) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = await self.db.conn.execute(
            "SELECT COUNT(*) FROM items WHERE channel=? AND status='scheduled' AND scheduled_at>?", (key, now))
        return (await cur.fetchone())[0]

    def media_ok(self, item: dict) -> bool:
        if not item["media"]:
            return self.profiles[item["channel"]].content_type == "news"
        return all(Path(m.path).exists() for m in item["media"])

    async def _rewrite_item(self, p: ChannelProfile, item: dict, note: str | None = None) -> bool:
        if not self.media_ok(item):
            return await self._reject(item, "медиа не найдены на диске")
        # Явная реклама — не тратим запрос к модели (только для рецептов: в находках «скидка» и erid — норма)
        if p.content_type == "recipe" and AD_MARKERS.search(item["raw_text"]):
            return await self._reject(item, "похоже на рекламу")

        # Дубль по картинке/видео — проверяем до модели, это бесплатно
        pool = await self.db.dedup_pool(p.key, item["id"], p.dedup_days)
        phash = item.get("phash") or dedup.media_hash(item["media"])
        if phash:
            await self.db.update(item["id"], phash=phash)
            twin = next((o for o in pool if o["phash"] and dedup.hamming(phash, o["phash"]) <= dedup.PHASH_MAX_DISTANCE), None)
            if twin:
                return await self._reject(item, f"дубликат по картинке: #{twin['id']}")

        if p.content_type == "deal":
            return await self._rewrite_deal(p, item, pool, note)

        if p.content_type == "news":
            res = await self.rewriter.rewrite_news(p, item["raw_text"], note)
            if not res.is_news:
                return await self._reject(item, res.reject_reason or "не новость")
            title = dedup.title_key(res.title)
            twin = next((o for o in pool if o["title_key"] == title), None)
            if twin:
                return await self._reject(item, f"дубликат новости: #{twin['id']}")
            await self.db.update(item["id"], status="draft", title_key=title, caption=format_news(res))
            return True

        res = await self.rewriter.rewrite(p, item["raw_text"], item["media"], note=note)
        if not res.is_recipe:
            return await self._reject(item, res.reject_reason or "не рецепт")

        # Дубль по рецепту: похожее название + похожие ингредиенты
        tkey, ikey = dedup.title_key(res.title), dedup.ingredients_key(res.ingredients)
        await self.db.update(item["id"], title_key=tkey, ingr_key=ikey)
        twin = next((o for o in pool if o["title_key"] is not None
                     and dedup.same_recipe(tkey, ikey, o["title_key"], o["ingr_key"] or "")), None)
        if twin:
            return await self._reject(item, f"дубликат рецепта: #{twin['id']}")

        caption = format_caption(res)  # сам укладывает карточку в лимит подписи
        await self.db.update(item["id"], status="draft", caption=caption)
        return True

    async def _deal_links(self, item: dict) -> list:
        return await self.links.resolve_all(item["links"] + wb_articles(item["raw_text"]))

    async def _rewrite_deal(self, p: ChannelProfile, item: dict, pool: list[dict], note: str | None) -> bool:
        links = await self._deal_links(item)
        if not links:
            return await self._reject(item, "нет ссылок на товары WB / Ozon / AliExpress / Яндекс Маркета")
        # Дубль по товару: тот же id на той же площадке
        keys = {l.key for l in links if l.key}
        if keys:
            twin = next((o for o in pool if (o["title_key"] or "").startswith("deal:")
                         and keys & set(o["title_key"][5:].split("|"))), None)
            if twin:
                return await self._reject(item, f"дубликат товара: #{twin['id']}")
            await self.db.update(item["id"], title_key="deal:" + "|".join(sorted(keys)))

        res = await self.rewriter.rewrite_deal(p, item["raw_text"], item["media"], links, note)
        if not res.is_deal:
            return await self._reject(item, res.reject_reason or "не находка")
        caption, warnings = deals.format_deal(res, links, p, self._own_links(item, links))
        await self.db.update(item["id"], status="draft", caption=caption, note="\n".join(warnings) or None)
        return True

    @staticmethod
    def _own_links(item: dict, links: list) -> dict[str, str]:
        """Для товаров, присланных модератором, в посте остаётся его ссылка (реферальная)."""
        if not item["source"].startswith("manual:"):
            return {}
        return {l.key: l.source_url for l in links if l.key and l.source_url in item["links"]}

    async def add_manual(self, p: ChannelProfile, urls: list[str]) -> dict[str, list[str]]:
        """Товары по ссылкам от модератора (сейчас — Яндекс Маркет). Возвращает отчёт по группам."""
        self._manual_adding += 1
        try:
            return await self._add_manual(p, urls)
        finally:
            self._manual_adding -= 1

    async def _add_manual(self, p: ChannelProfile, urls: list[str]) -> dict[str, list[str]]:
        report: dict[str, list[str]] = {"added": [], "dup": [], "skip": [], "error": []}
        since_pool = await self.db.dedup_pool(p.key, -1, p.dedup_days)
        known = {k for o in since_pool if (o["title_key"] or "").startswith("deal:") for k in o["title_key"][5:].split("|")}
        # Ссылка на ваш список Маркета раскрывается в ссылки на все его товары (с вашими метками)
        expanded: list[str] = []
        report["lists"] = []
        for url in dict.fromkeys(urls):
            try:
                items = await self.market.wishlist_items(url)
            except Exception as e:
                log.warning("Маркет: список %s: %s", url, e)
                items = None
            if items is None:
                expanded.append(url)
            else:
                report["lists"].append(f"{len(items)}")
                expanded += items
        for url in dict.fromkeys(expanded):
            link = await self.links.resolve(url)
            if not link or link.marketplace != "ym" or not link.key:
                report["skip"].append(url)
                continue
            if link.key in known:
                report["dup"].append(url)
                continue
            try:
                prod = await self.market.product(url)
            except Exception as e:
                log.warning("Маркет %s: %s", url, e)
                prod = None
            if not prod:
                report["error"].append(url)
                continue
            post = RawPost(source="manual:ym", source_ref=link.key, text=prod.as_text(), url=prod.url, links=[url])
            post.media = await self.market.download_images(prod.images, self._media_dir(p, post))
            if not post.media:
                report["error"].append(url)
                continue
            if item_id := await self.db.add_item(p.key, post):
                await self.db.update(item_id, phash=dedup.media_hash(post.media), title_key=f"deal:{link.key}")
                known.add(link.key)
                report["added"].append(prod.name)
            else:
                report["dup"].append(url)
            await asyncio.sleep(1.5)  # бережно к Маркету
        return report

    async def send_draft(self, p: ChannelProfile, item: dict) -> None:
        chat = self.moderation_chat(p)
        media_msgs = await send_post(self.bot, chat, item["media"], caption=None)
        ctrl = await self.bot.send_message(
            chat, _control_text(p, item), reply_markup=moderation_kb(item["id"], p.content_type == "deal"),
        )
        await self.db.update(item["id"], status="pending",
                             draft_msg_ids=[m.message_id for m in media_msgs] + [ctrl.message_id])

    async def regenerate(self, item_id: int, note: str | None = None) -> dict:
        item = await self.db.get(item_id)
        p = self.profiles[item["channel"]]
        note = note or "Напиши другой вариант заголовка и подачи."
        if p.content_type == "deal":
            links = await self._deal_links(item)
            res = await self.rewriter.rewrite_deal(p, item["raw_text"], item["media"], links, note)
            if res.is_deal:
                caption, warnings = deals.format_deal(res, links, p, self._own_links(item, links))
                await self.db.update(item_id, caption=caption, note="\n".join(warnings) or None)
            return await self.db.get(item_id)
        if p.content_type == "news":
            res = await self.rewriter.rewrite_news(p, item["raw_text"], note)
            if res.is_news:
                await self.db.update(item_id, caption=format_news(res), title_key=dedup.title_key(res.title))
            return await self.db.get(item_id)
        res = await self.rewriter.rewrite(p, item["raw_text"], item["media"], note=note)
        caption = format_caption(res) if res.is_recipe else item["caption"]
        await self.db.update(item_id, caption=caption)
        return await self.db.get(item_id)

    async def replace_links(self, item_id: int, pasted: list[str]) -> tuple[dict, list[str]]:
        """Подставить в пост ссылки модератора (например, партнёрские из приложения маркетплейса).
        Ссылка встаёт на место ссылки на тот же товар; если товар в посте один — на его место."""
        item = await self.db.get(item_id)
        caption = item["caption"] or ""
        anchors = re.findall(r'<a href="([^"]+)">', caption)
        report: list[str] = []
        if not anchors:
            return item, ["в посте нет ссылок, которые можно заменить"]
        anchor_links = [canonical(html.unescape(a)) for a in anchors]
        replaced: set[int] = set()
        for url in pasted:
            new = await self.links.resolve(url)
            idx = None
            if new and new.key:
                idx = next((i for i, a in enumerate(anchor_links) if a and a.key == new.key and i not in replaced), None)
                if idx is None and len(anchors) == 1:
                    old_key = anchor_links[0].key if anchor_links[0] else None
                    if old_key and old_key != new.key:
                        report.append(f"❌ {url} ведёт на другой товар ({new.name} {new.product_id}), а в посте — {old_key}. Не заменил.")
                        continue
                    idx = 0
            else:
                # Не удалось раскрыть (например, Ozon) — ставим по порядку на свободное место
                idx = next((i for i in range(len(anchors)) if i not in replaced), None)
                if idx is not None:
                    report.append(f"⚠️ {url}: не смог проверить товар — поставил на место ссылки №{idx + 1}, проверьте.")
            if idx is None:
                report.append(f"❌ {url}: не нашёл, какую ссылку в посте заменить.")
                continue
            caption = caption.replace(f'<a href="{anchors[idx]}">', f'<a href="{html_escape(url)}">', 1)
            replaced.add(idx)
            if not any(r.startswith(f"⚠️ {url}") for r in report):
                report.append(f"✅ Ссылка №{idx + 1} заменена" + (f" ({new.name})" if new else ""))
        # Предупреждения о ссылках, которые теперь заменены, больше не нужны
        names = {(anchor_links[i].name if anchor_links[i] else "") for i in replaced}
        notes = [w for w in (item["note"] or "").splitlines() if not any(n and n in w for n in names)]
        await self.db.update(item_id, caption=caption, note="\n".join(notes) or None)
        return await self.db.get(item_id), report

    # ---------- 3. публикация ----------

    async def publish_next(self, key: str) -> None:
        if await self.is_paused(key, "post"):
            log.info("[%s] постинг на паузе — слот пропущен", key)
            return
        p = self.profiles[key]
        items = await self.db.by_status(key, "approved", limit=1, order="approved_at, id")
        if not items:
            log.info("[%s] очередь пуста — слот пропущен", key)
            return
        await self.publish(p, items[0])

    async def publish(self, p: ChannelProfile, item: dict) -> None:
        if not self.media_ok(item):
            await self.db.update(item["id"], status="failed", note="медиа не найдены на диске")
            await self._notify(p, f"⚠️ #{item['id']}: файлы поста не найдены на диске, опубликовать не могу.")
            return
        try:
            if visible_len(item["caption"] or "") <= CAPTION_LIMIT:
                await send_post(self.bot, p.target, item["media"], item["caption"])
            else:  # запасной вариант: медиа отдельно, текст отдельным сообщением
                await send_post(self.bot, p.target, item["media"], None)
                await self.bot.send_message(p.target, item["caption"])
        except Exception as e:
            log.exception("[%s] публикация item %s упала", p.key, item["id"])
            await self.db.update(item["id"], status="failed", note=f"publish: {e}"[:500])
            await self._notify(p, f"⚠️ Не удалось опубликовать #{item['id']}: {e}")
            return
        await self.db.update(item["id"], status="published")
        if await self.max_chat(p.key):
            await self.db.update(item["id"], max_status="queued",
                                 max_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            asyncio.create_task(self.max_job())
        else:
            self._cleanup(item)
        log.info("[%s] опубликован item %s", p.key, item["id"])

    # ---------- 3б. отложенные сообщения в канале ----------

    def uses_scheduled(self, p: ChannelProfile) -> bool:
        return p.publish_mode == "scheduled" and self.channel_scheduler is not None

    async def sync_job(self, key: str) -> None:
        if await self.is_paused(key, "post") or self._sync_locks[key].locked():
            return
        async with self._sync_locks[key]:
            try:
                await self.sync_schedule(self.profiles[key])
            except Exception:
                log.exception("[%s] синхронизация отложенных упала", key)

    async def sync_schedule(self, p: ChannelProfile) -> None:
        """Сверяем базу с «Отложенными» канала и добавляем туда одобренные посты, пока есть место."""
        if not self.uses_scheduled(p):
            return
        sch = self.channel_scheduler
        if not await sch.can_post(p.target):
            if self._limit_notified.get(f"perm:{p.key}"):
                return
            self._limit_notified[f"perm:{p.key}"] = True
            await self._notify(p, f"⚠️ Аккаунт-читатель не может публиковать в {p.target}. Сделайте его "
                                  "администратором канала с правом публикации — до тех пор одобренные посты ждут в очереди.")
            return
        self._limit_notified[f"perm:{p.key}"] = False
        existing = await sch.scheduled(p.target)
        present = {m.id for m in existing}
        occupied = [m.date for m in existing]
        now = datetime.now(timezone.utc)
        ppd = await self.posts_per_day(p.key)

        # 1) что уже вышло или было удалено вручную
        for it in await self.db.by_status(p.key, "scheduled", limit=1000):
            if any(i in present for i in it["tg_msg_ids"]):
                continue
            when = datetime.fromisoformat(it["scheduled_at"]) if it["scheduled_at"] else now
            if when <= now + timedelta(minutes=1):
                await self.db.update(it["id"], status="published")
            else:
                await self.db.update(it["id"], status="rejected", note="удалён из отложенных вручную")
                if it.get("max_status") == "queued":
                    await self.db.update(it["id"], max_status="cancelled")
                    self._cleanup(it)

        # 2) одобренные -> в отложенные, пока не упрёмся в лимит
        count = len(existing)
        for it in await self.db.by_status(p.key, "approved", limit=1000, order="approved_at, id"):
            if not self.media_ok(it):  # файлы пропали — пропускаем тихо, на это место встанет следующий
                log.warning("[%s] #%s: медиа не найдены на диске — пропускаю", p.key, it["id"])
                await self.db.update(it["id"], status="rejected", note="медиа не найдены на диске")
                continue
            need = min(len(it["media"]), 10) or 1
            if count + need > p.max_scheduled:
                self._limit_reached(p, count)
                break
            slot = next_slot(p, occupied, now, ppd)
            if not slot:
                log.warning("[%s] нет свободных интервалов на 60 дней вперёд", p.key)
                break
            try:
                ids = await sch.schedule(p.target, it["media"], it["caption"] or "", slot)
            except ScheduleTooMuchError:
                self._limit_reached(p, count)
                break
            except (AuthKeyDuplicatedError, UnauthorizedError) as e:
                log.error("[%s] сессия Telegram недоступна, посты остаются в очереди: %s", p.key, e)
                break
            except Exception as e:
                log.exception("[%s] не удалось поставить #%s в отложенные", p.key, it["id"])
                await self.db.update(it["id"], status="failed", note=f"schedule: {e}"[:500])
                await self._notify(p, f"⚠️ Не удалось поставить #{it['id']} в отложенные: {e}")
                continue
            occupied.append(slot)
            count += len(ids)
            self._limit_notified[p.key] = False
            await self.db.update(it["id"], status="scheduled", scheduled_at=slot.isoformat(timespec="seconds"),
                                 tg_msg_ids=ids)
            log.info("[%s] #%s в отложенных на %s", p.key, it["id"], slot.astimezone(ZoneInfo(p.timezone)))
            await self._mark_control(p, await self.db.get(it["id"]))
            if await self.max_chat(p.key):  # в MAX опубликуем сами в ту же минуту — файлы нужны до тех пор
                await self.db.update(it["id"], max_status="queued", max_at=slot.isoformat(timespec="seconds"))
            else:
                self._cleanup(it)

    def _limit_reached(self, p: ChannelProfile, count: int) -> None:
        if not self._limit_notified.get(p.key):
            log.info("[%s] лимит отложенных (%s/%s) — остальное добавлю, когда освободится место",
                     p.key, count, p.max_scheduled)
            self._limit_notified[p.key] = True

    async def _mark_control(self, p: ChannelProfile, item: dict) -> None:
        """Дописать в сообщение модерации, на когда поставлен пост."""
        if not item["draft_msg_ids"]:
            return
        local = datetime.fromisoformat(item["scheduled_at"]).astimezone(ZoneInfo(p.timezone))
        try:
            await self.bot.edit_message_text(
                _control_text(p, item) + f"\n\n🗓 В отложенных канала на {local:%d.%m %H:%M}",
                chat_id=self.moderation_chat(p), message_id=item["draft_msg_ids"][-1])
        except TelegramBadRequest:
            pass

    # ---------- MAX ----------

    async def max_chat(self, key: str) -> int | None:
        if not self.max:
            return None
        v = await self.db.get_flag(f"max:{key}:chat")
        return int(v) if v else None

    async def set_max_chat(self, key: str, chat_id: int | None) -> None:
        await self.db.set_flag(f"max:{key}:chat", str(chat_id) if chat_id else None)

    async def max_poll(self) -> dict[int, str]:
        """Забираем события MAX и запоминаем каналы, куда добавили бота (событие приходит один раз)."""
        marker = await self.db.get_flag("max:marker")
        found, new_marker = await self.max.poll_chats(int(marker) if marker else None)
        for cid, title in found.items():
            await self.db.set_flag(f"max:known:{cid}", title or "")
            log.info("MAX: бот добавлен в «%s» (chat_id %s)", title, cid)
        if new_marker:
            await self.db.set_flag("max:marker", str(new_marker))
        return found

    async def max_known_chats(self) -> dict[int, str]:
        cur = await self.db.conn.execute("SELECT key, value FROM flags WHERE key LIKE 'max:known:%'")
        return {int(k.split(":")[2]): v for k, v in await cur.fetchall()}

    async def max_backfill(self, key: str) -> dict[str, int]:
        """Посты, которые уже стоят в отложке Telegram, но не в очереди MAX (MAX включили позже):
        скачиваем их медиа обратно из отложенных сообщений и ставим в очередь MAX на то же время.
        Текст берём из самого отложенного сообщения — с вашими ручными правками."""
        from telethon.extensions import html as tg_html

        p = self.profiles[key]
        rep = {"queued": 0, "missing": 0, "failed": 0}
        if not await self.max_chat(key) or not self.channel_scheduler:
            return rep
        existing = {m.id: m for m in await self.channel_scheduler.scheduled(p.target)}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = await self.db.conn.execute(
            """SELECT id FROM items WHERE channel=? AND status='scheduled' AND scheduled_at>?
                 AND (max_status IS NULL OR max_status IN ('cancelled','failed')) ORDER BY scheduled_at""", (key, now))
        for (item_id,) in await cur.fetchall():
            it = await self.db.get(item_id)
            msgs = [existing[i] for i in it["tg_msg_ids"] if i in existing]
            if not msgs:
                rep["missing"] += 1
                continue
            dest = self.settings.media_dir / key / f"max_{item_id}"
            dest.mkdir(parents=True, exist_ok=True)
            media: list[MediaFile] = []
            try:
                for m in msgs:
                    kind = "video" if m.video else "photo" if m.photo else None
                    if not kind:
                        continue
                    path = await self.tg.client.download_media(m, file=str(dest / str(m.id)))
                    if path:
                        media.append(MediaFile(kind=kind, path=str(path)))
            except Exception as e:
                log.warning("[%s] MAX: не скачал медиа отложенного поста #%s: %s", key, item_id, e)
                shutil.rmtree(dest, ignore_errors=True)
                rep["failed"] += 1
                continue
            first = next((m for m in msgs if m.message), None)
            caption = tg_html.unparse(first.message, first.entities or []) if first else (it["caption"] or "")
            await self.db.update(item_id, media=media, caption=caption, max_status="queued",
                                 max_at=it["scheduled_at"], max_note=None)
            rep["queued"] += 1
        log.info("[%s] MAX: досинхронизация отложки — %s", key, rep)
        return rep

    async def max_test(self, key: str) -> str:
        """Пробная публикация в MAX: свежий готовый пост канала (статус в Telegram не меняется).
        Если готовых постов с медиа нет — пробный рецепт с povar.ru."""
        chat = await self.max_chat(key)
        if not chat:
            raise RuntimeError("для канала не включён MAX (/max канал chat_id)")
        cur = await self.db.conn.execute(
            """SELECT id FROM items WHERE channel=? AND caption IS NOT NULL
                 AND status IN ('pending','approved','draft','scheduled') ORDER BY id DESC LIMIT 30""", (key,))
        for (item_id,) in await cur.fetchall():
            it = await self.db.get(item_id)
            if self.media_ok(it):
                mid = await self.max.send_post(chat, it["media"], it["caption"])
                return f"пост #{item_id} ({it['status']}, медиа: {len(it['media'])}), mid {mid}"
        # Готовых постов с файлами нет — пробный рецепт
        res = await self.web.fetch_recipe("https://povar.ru/recipes/salat_mimoza_s_syrom_i_maslom-16189.html")
        if not res:
            raise RuntimeError("нет готовых постов с медиа и не удалось взять пробный рецепт")
        post, images = res
        media = await self.web.download_images(images[:1], self.settings.media_dir / key / "_maxtest")
        caption = format_caption(RecipePost(
            is_recipe=True, reject_reason=None, title="Салат «Мимоза» с сыром и маслом", emoji="🥗",
            intro="Пробный пост: проверяем публикацию в MAX.", time=None, servings=None, difficulty="легко",
            rubric="салаты", ingredients=["Рыбные консервы — 1 банка", "Яйца — 5 шт.", "Сыр — 100 г",
                                          "Сливочное масло — 80 г", "Лук — 1 шт.", "Майонез — по вкусу"],
            steps=["Отварите яйца, разделите на белки и желтки.", "Выкладывайте слоями: белки, сыр, рыба, лук, масло.",
                   "Посыпьте тёртыми желтками."]))
        try:
            mid = await self.max.send_post(chat, media, caption)
        finally:
            shutil.rmtree(self.settings.media_dir / key / "_maxtest", ignore_errors=True)
        return f"пробный рецепт (готовых постов с медиа не нашлось), mid {mid}"

    async def max_job(self) -> None:
        """Публикует в MAX посты, время которых подошло (в MAX нет отложенных сообщений в API)."""
        if not self.max or self._max_lock.locked():
            return
        try:
            await self.max_poll()
        except Exception as e:
            log.debug("MAX: события не получены: %s", e)
        async with self._max_lock:
            now = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(timespec="seconds")
            cur = await self.db.conn.execute(
                "SELECT id FROM items WHERE max_status='queued' AND max_at<=? ORDER BY max_at, id LIMIT 20", (now,))
            for (item_id,) in await cur.fetchall():
                it = await self.db.get(item_id)
                p = self.profiles.get(it["channel"])
                chat = await self.max_chat(it["channel"]) if p else None
                if not p or not chat:
                    await self.db.update(item_id, max_status="cancelled", max_note="MAX для канала выключен")
                    continue
                if not self.media_ok(it):
                    await self.db.update(item_id, max_status="failed", max_note="медиа не найдены на диске")
                    continue
                try:
                    mid = await self.max.send_post(chat, it["media"], it["caption"] or "")
                except Exception as e:
                    prev = re.match(r"попытка (\d+)", it.get("max_note") or "")
                    tries = (int(prev.group(1)) if prev else 0) + 1
                    log.warning("[%s] MAX: #%s не опубликован (попытка %d): %s", p.key, item_id, tries, e)
                    if tries >= 3:
                        await self.db.update(item_id, max_status="failed", max_note=f"{e}"[:400])
                        await self._notify(p, f"⚠️ MAX: пост #{item_id} не опубликован: {str(e)[:300]}")
                        self._cleanup(it)
                    else:
                        retry = (datetime.now(timezone.utc) + timedelta(minutes=5 * tries)).isoformat(timespec="seconds")
                        await self.db.update(item_id, max_at=retry, max_note=f"попытка {tries}: {e}"[:400])
                    continue
                await self.db.update(item_id, max_status="sent", max_mid=mid, max_note=None)
                log.info("[%s] MAX: опубликован #%s", p.key, item_id)
                self._cleanup(it)
                await asyncio.sleep(1)  # лимит MAX — 2 сообщения в секунду в канал

    # ---------- helpers ----------

    async def _notify(self, p: ChannelProfile, text: str) -> None:
        try:
            await self.bot.send_message(self.moderation_chat(p), html_escape(text))
        except Exception:
            log.exception("не удалось уведомить модератора")

    async def delete_draft_media(self, item: dict) -> None:
        """Убрать медиа черновика из чата модерации (управляющее сообщение остаётся)."""
        p = self.profiles[item["channel"]]
        for mid in item["draft_msg_ids"][:-1]:
            try:
                await self.bot.delete_message(self.moderation_chat(p), mid)
            except TelegramBadRequest:
                pass

    def _cleanup(self, item: dict) -> None:
        dirs = {Path(m.path).parent for m in item["media"]}
        for d in dirs:
            if self.settings.media_dir in d.parents:
                shutil.rmtree(d, ignore_errors=True)


def moderation_kb(item_id: int, deal: bool = False):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ В очередь", callback_data=f"mod:approve:{item_id}")
    kb.button(text="🚀 Сейчас", callback_data=f"mod:now:{item_id}")
    kb.button(text="🔄 Переписать", callback_data=f"mod:regen:{item_id}")
    kb.button(text="✏️ Изменить", callback_data=f"mod:edit:{item_id}")
    if deal:
        kb.button(text="🔗 Ссылка", callback_data=f"mod:link:{item_id}")
    kb.button(text="❌ Пропустить", callback_data=f"mod:skip:{item_id}")
    kb.adjust(2, 2, 2) if deal else kb.adjust(2, 2, 1)
    return kb.as_markup()


def _control_text(p: ChannelProfile, item: dict) -> str:
    """Предпросмотр для модератора (HTML): служебная шапка + карточка как она будет в канале."""
    caption = item["caption"] or ""
    n = visible_len(caption)
    warn = f"\n\n⚠️ {n} симв. — больше лимита подписи {CAPTION_LIMIT}" if n > CAPTION_LIMIT else ""
    if item.get("note") and item.get("status") in ("draft", "pending", "approved"):
        warn += "".join(f"\n{'' if w.startswith(deals.CRITICAL) else '⚠️ '}{html_escape(w)}" for w in item["note"].splitlines())
    views = f" · 👁 {item['views']}" if item.get("views") else ""
    src = html_escape(item["url"] or item["source"])
    return (f"<i>📝 Черновик #{item['id']} → {html_escape(p.title)}\nИсточник: {src}{views}</i>\n"
            f"────────────\n{caption}{warn}")


def _minute(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def day_intervals(p: ChannelProfile, day, ppd: int) -> list[tuple[datetime, datetime]]:
    """Окно публикаций дня (day_start–day_end), поделённое на ppd равных интервалов."""
    tz = ZoneInfo(p.timezone)

    def at(hhmm: str, d):
        h, m = (int(x) for x in hhmm.split(":"))
        if h >= 24:  # «24:00» — конец суток
            return datetime.combine(d + timedelta(days=1), dtime(0, 0), tzinfo=tz)
        return datetime.combine(d, dtime(h, m), tzinfo=tz)

    a0, b0 = at(p.day_start, day), at(p.day_end, day)
    if b0 <= a0:  # окно через полночь, например 20:00–02:00
        b0 += timedelta(days=1)
    step = (b0 - a0) / ppd
    return [(a0 + i * step, a0 + (i + 1) * step) for i in range(ppd)]


def next_slot(p: ChannelProfile, occupied: list[datetime], now: datetime, ppd: int,
              rng: random.Random | None = None, days: int = 60) -> datetime | None:
    """Ближайший свободный интервал (без других отложенных сообщений) и случайная минута в нём.
    Случайность — в средних 80% интервала, чтобы соседние посты не слипались на границах."""
    rng = rng or random
    tz = ZoneInfo(p.timezone)
    earliest = now + timedelta(minutes=p.min_lead_minutes)
    first_day = earliest.astimezone(tz).date() - timedelta(days=1)  # окно через полночь могло начаться вчера
    for d in range(days + 1):
        for a, b in day_intervals(p, first_day + timedelta(days=d), ppd):
            if b <= earliest + timedelta(minutes=2):
                continue
            if any(a <= t < b for t in occupied):
                continue
            lo, hi = a + (b - a) * 0.1, a + (b - a) * 0.9
            if hi <= earliest:            # текущий интервал почти прошёл — берём остаток до его конца
                lo, hi = earliest, b - timedelta(minutes=1)
            lo = max(lo, earliest)
            if hi <= lo:
                continue
            slot = lo + (hi - lo) * rng.random()
            slot = slot.replace(second=0, microsecond=0)
            return slot if slot >= earliest else slot + timedelta(minutes=1)
    return None
