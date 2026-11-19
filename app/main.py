"""Точка входа: бот модерации + чтение источников + расписание публикаций в одном процессе.

Запуск: python -m app.main
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from app.bot.handlers import router
from app.config import Settings, load_profiles
from app.db import DB
from app.pipeline import Pipeline
from app.rewriter import Rewriter
from app.sources.telegram import TelegramReader
from app.sources.web import WebReader


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("app")

    settings = Settings()
    profiles = load_profiles()
    db = DB(settings.db_path)
    await db.connect()

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
    tg = None
    if any(p.sources.telegram for p in profiles.values()):
        tg = TelegramReader(settings)
        await tg.start()
    web = WebReader()
    pipeline = Pipeline(settings, profiles, db, bot, tg, web, Rewriter(settings))

    scheduler = AsyncIOScheduler()
    start = datetime.now() + timedelta(seconds=10)
    for key, p in profiles.items():
        scheduler.add_job(pipeline.collect_job, "interval", args=[key], id=f"collect:{key}",
                          minutes=p.collect_every_minutes, next_run_time=start, max_instances=1, coalesce=True)
        # Обработка «новых» материалов каждую минуту — не ждём окончания сбора
        scheduler.add_job(pipeline.process_job, "interval", args=[key], id=f"process:{key}",
                          minutes=1, next_run_time=start + timedelta(seconds=20), max_instances=1, coalesce=True)
        if p.publish_mode == "scheduled" and tg:
            # Одобренные посты -> «Отложенные» канала: при старте и каждые 10 минут (освобождается место в лимите)
            scheduler.add_job(pipeline.sync_job, "interval", args=[key], id=f"sync:{key}",
                              minutes=10, next_run_time=start + timedelta(seconds=5), max_instances=1, coalesce=True)
        else:
            slots = [CronTrigger(hour=int(t.split(":")[0]), minute=int(t.split(":")[1]), timezone=p.timezone)
                     for t in p.schedule]
            scheduler.add_job(pipeline.publish_next, OrTrigger(slots), args=[key], id=f"publish:{key}",
                              max_instances=1, coalesce=True, misfire_grace_time=600)
        log.info("Канал %s → %s, слоты: %s", p.title, p.target, ", ".join(p.schedule))
    scheduler.start()

    dp = Dispatcher()
    dp["pipeline"] = pipeline
    dp["scheduler"] = scheduler
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="status", description="Статистика и очередь"),
        BotCommand(command="collect", description="Собрать посты сейчас"),
        BotCommand(command="publish", description="Опубликовать следующий из очереди"),
    ])

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if tg:
            await tg.stop()
        await web.stop()
        await pipeline.links.stop()
        await pipeline.market.stop()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
