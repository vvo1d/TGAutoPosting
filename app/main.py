"""Точка входа: бот модерации + чтение источников + расписание публикаций в одном процессе.

Запуск: python -m app.main
"""
from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from app import watchdog
from app.bot.handlers import router
from app.config import Settings, load_profiles
from app.db import DB
from app.pipeline import Pipeline
from app.rewriter import Rewriter
from app.sources.telegram import TelegramReader
from app.sources.web import WebReader


async def main() -> None:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Лог в консоль и в файл (команда /logs в боте читает файл)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    file_handler = RotatingFileHandler(settings.log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[logging.StreamHandler(), file_handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("app")

    profiles = load_profiles()
    db = DB(settings.db_path)
    await db.connect()

    session = AiohttpSession(proxy=settings.tg_proxy) if settings.tg_proxy else None
    if settings.tg_proxy:
        log.info("Telegram через прокси %s", settings.tg_proxy)
    bot = Bot(settings.bot_token, session=session,
              default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
    tg = None
    if any(p.sources.telegram for p in profiles.values()):
        tg = TelegramReader(settings)
        await tg.start()
    web = WebReader(settings.tg_proxy)
    pipeline = Pipeline(settings, profiles, db, bot, tg, web, Rewriter(settings))

    scheduler = AsyncIOScheduler()
    start = datetime.now() + timedelta(seconds=10)
    for key, p in profiles.items():
        scheduler.add_job(pipeline.collect_job, "interval", args=[key], id=f"collect:{key}",
                          minutes=await pipeline.setting(key, "collect_min"), next_run_time=start,
                          max_instances=1, coalesce=True)
        # Обработка «новых» материалов каждую минуту — не ждём окончания сбора
        scheduler.add_job(pipeline.process_job, "interval", args=[key], id=f"process:{key}",
                          minutes=1, next_run_time=start + timedelta(seconds=20), max_instances=1, coalesce=True)
        if p.publish_mode == "scheduled" and tg:
            # Одобренные посты -> «Отложенные» канала: при старте и каждые 5 минут
            # (удалённый вручную пост освобождает интервал — туда сразу встаёт следующий)
            scheduler.add_job(pipeline.sync_job, "interval", args=[key], id=f"sync:{key}",
                              minutes=5, next_run_time=start + timedelta(seconds=5), max_instances=1, coalesce=True)
        else:
            slots = [CronTrigger(hour=int(t.split(":")[0]), minute=int(t.split(":")[1]), timezone=p.timezone)
                     for t in p.schedule]
            scheduler.add_job(pipeline.publish_next, OrTrigger(slots), args=[key], id=f"publish:{key}",
                              max_instances=1, coalesce=True, misfire_grace_time=600)
        log.info("Канал %s → %s, слоты: %s", p.title, p.target, ", ".join(p.schedule))
    if pipeline.max:
        # MAX: публикуем посты, время которых подошло (отложенных в API MAX нет) — раз в минуту
        scheduler.add_job(pipeline.max_job, "interval", id="max", minutes=1,
                          next_run_time=start + timedelta(seconds=15), max_instances=1, coalesce=True)
        try:
            me = await pipeline.max.me()
            log.info("MAX: бот %s подключён", me.get("username") or me.get("name"))
        except Exception as e:
            log.warning("MAX: не удалось подключиться: %s", e)
    # Уборка: устаревшие необработанные посты и ненужные медиа — при старте и раз в час
    scheduler.add_job(pipeline.cleanup_job, "interval", id="cleanup", hours=1,
                      next_run_time=start + timedelta(seconds=2), max_instances=1, coalesce=True)
    scheduler.start()

    dp = Dispatcher()
    dp["pipeline"] = pipeline
    dp["scheduler"] = scheduler
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="status", description="Статистика и очередь"),
        BotCommand(command="collect", description="Собрать посты сейчас"),
        BotCommand(command="publish", description="Опубликовать следующий из очереди"),
        BotCommand(command="settings", description="Настройки каналов, лимиты, диск"),
        BotCommand(command="max", description="Публикация в MAX"),
        BotCommand(command="pause", description="Пауза сбора/постинга (кнопки — в /status)"),
        BotCommand(command="resume", description="Снять паузу"),
        BotCommand(command="server", description="Версия, диск, прокси"),
        BotCommand(command="logs", description="Последние строки лога"),
        BotCommand(command="update", description="Обновить из GitHub и перезапуститься"),
        BotCommand(command="restart", description="Перезапустить приложение"),
    ])

    watchdog_task = asyncio.create_task(watchdog.run())  # systemd перезапустит, если цикл событий зависнет
    try:
        await dp.start_polling(bot)
    finally:
        watchdog_task.cancel()
        scheduler.shutdown(wait=False)
        if tg:
            await tg.stop()
        await web.stop()
        await pipeline.links.stop()
        await pipeline.market.stop()
        if pipeline.max:
            await pipeline.max.stop()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
