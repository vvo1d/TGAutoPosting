"""Бот модерации: кнопки под черновиками и служебные команды."""
from __future__ import annotations

import asyncio
import logging
import re

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app.pipeline import Pipeline, _control_text, moderation_kb
from app.rewriter import html_escape

log = logging.getLogger(__name__)
router = Router()

# user_id -> item_id, ожидаем от модератора новый текст
pending_edits: dict[int, int] = {}
# user_id -> item_id, ждём от модератора ссылку(и) на товар
pending_links: dict[int, int] = {}

STATUS_RU = {
    "new": "новые", "draft": "черновики", "pending": "на модерации", "approved": "в очереди",
    "scheduled": "в отложенных", "published": "опубликовано", "rejected": "отклонено", "failed": "ошибки",
}


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, pipeline: Pipeline) -> bool:
        user = event.from_user
        return bool(user and user.id in pipeline.settings.admin_ids)


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


@router.message(Command("start", "help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(
        "Я веду ваши каналы.\n\n"
        "/status — статистика и очередь\n"
        "/collect — собрать и подготовить посты сейчас\n"
        "/publish &lt;канал&gt; — опубликовать следующий из очереди\n\n"
        "Черновики приходят сюда с кнопками. В режиме «✏️ Изменить» пришлите новый текст поста "
        "или (если подключена LLM) начните сообщение с «!», чтобы дать указание модели (например: «! короче и без шагов»)."
    )


@router.message(Command("status"))
async def cmd_status(msg: Message, pipeline: Pipeline, scheduler) -> None:
    lines = []
    for key, p in pipeline.profiles.items():
        counts = await pipeline.db.count(key)
        stats = ", ".join(f"{STATUS_RU.get(s, s)}: {c}" for s, c in sorted(counts.items())) or "пусто"
        if pipeline.uses_scheduled(p):
            sch = await pipeline.db.by_status(key, "scheduled", limit=1000, order="scheduled_at")
            nxt = sch[0]["scheduled_at"] if sch else None
            if nxt:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                nxt = datetime.fromisoformat(nxt).astimezone(ZoneInfo(p.timezone)).strftime("%d.%m %H:%M")
            last = sch[-1]["scheduled_at"] if sch else None
            info = f"В отложенных: {len(sch)}" + (f", ближайший {nxt}" if nxt else "")
            if last:
                info += f", расписано до {datetime.fromisoformat(last).astimezone(ZoneInfo(p.timezone)):%d.%m}"
        else:
            job = scheduler.get_job(f"publish:{key}")
            info = "Следующая публикация: " + (job.next_run_time.strftime("%d.%m %H:%M") if job and job.next_run_time else "—")
        lines.append(f"<b>{html_escape(p.title)}</b> ({key})\n{stats}\n{info}")
    await msg.answer("\n\n".join(lines), parse_mode="HTML")


@router.message(Command("collect"))
async def cmd_collect(msg: Message, pipeline: Pipeline) -> None:
    # Сбор может идти долго (качаются видео) — запускаем в фоне, черновики придут по мере готовности
    for key in pipeline.profiles:
        asyncio.create_task(pipeline.collect_job(key))
    await msg.answer("Запустил сбор. Черновики будут приходить по мере обработки — /status покажет прогресс.")


@router.message(Command("publish"))
async def cmd_publish(msg: Message, command: CommandObject, pipeline: Pipeline) -> None:
    key = (command.args or "").strip() or next(iter(pipeline.profiles))
    if key not in pipeline.profiles:
        await msg.answer(f"Нет канала «{key}». Есть: {', '.join(pipeline.profiles)}")
        return
    await pipeline.publish_next(key)
    await msg.answer("Ок.")


@router.callback_query(F.data.startswith("mod:"))
async def on_moderation(cb: CallbackQuery, pipeline: Pipeline) -> None:
    _, action, raw_id = cb.data.split(":")
    item = await pipeline.db.get(int(raw_id))
    if not item or item["status"] not in ("pending", "approved"):
        await cb.answer("Этот черновик уже обработан", show_alert=True)
        return
    p = pipeline.profiles[item["channel"]]

    if action == "approve":
        await pipeline.db.update(item["id"], status="approved")
        queued = await pipeline.db.count(p.key)
        where = "ставлю в отложенные канала…" if pipeline.uses_scheduled(p) else f"всего в очереди: {queued.get('approved', 0)}"
        await cb.message.edit_text(_control_text(p, item) + f"\n\n✅ Одобрено — {where}")
        await cb.answer("Одобрено")
        if pipeline.uses_scheduled(p):
            asyncio.create_task(pipeline.sync_job(p.key))

    elif action == "now":
        await cb.answer("Публикую…")
        await pipeline.publish(p, item)
        item = await pipeline.db.get(item["id"])
        mark = "🚀 Опубликовано" if item["status"] == "published" else f"⚠️ Ошибка: {html_escape(item['note'] or '')}"
        await cb.message.edit_text(_control_text(p, item) + f"\n\n{mark}")

    elif action == "regen":
        await cb.answer("Переписываю…")
        item = await pipeline.regenerate(item["id"])
        await cb.message.edit_text(_control_text(p, item), reply_markup=moderation_kb(item["id"], p.content_type == "deal"))

    elif action == "edit":
        pending_edits[cb.from_user.id] = item["id"]
        pending_links.pop(cb.from_user.id, None)
        await cb.answer()
        await cb.message.answer(
            f"Пришлите новый текст для #{item['id']} целиком.\n"
            + ("Или начните с «!», чтобы модель переписала по вашему указанию." if pipeline.rewriter.uses_llm else "")
        )

    elif action == "link":
        pending_links[cb.from_user.id] = item["id"]
        pending_edits.pop(cb.from_user.id, None)
        await cb.answer()
        await cb.message.answer(
            f"Пришлите ссылку для #{item['id']} — например, из «Поделиться» в приложении маркетплейса.\n"
            "Если товаров в посте несколько — по одной ссылке в строке. Бот проверит, что ссылка ведёт на тот же товар.")

    elif action == "skip":
        await pipeline.db.update(item["id"], status="rejected", note="отклонён модератором")
        await pipeline.delete_draft_media(item)
        pipeline._cleanup(item)
        await cb.message.edit_text(f"❌ #{item['id']} пропущен")
        await cb.answer()


@router.message(F.text, F.from_user.id.func(lambda uid: uid in pending_links))
async def on_link_text(msg: Message, pipeline: Pipeline) -> None:
    urls = re.findall(r"https?://\S+", msg.text or "")
    if not urls:
        await msg.answer("Не вижу ссылки. Пришлите ссылку целиком, начиная с https://")
        return
    item_id = pending_links.pop(msg.from_user.id)
    item = await pipeline.db.get(item_id)
    if not item or item["status"] not in ("pending", "approved"):
        await msg.answer("Этот черновик уже обработан.")
        return
    p = pipeline.profiles[item["channel"]]
    item, report = await pipeline.replace_links(item_id, urls)
    await msg.answer("\n".join(html_escape(r) for r in report))
    ctrl = await msg.answer(_control_text(p, item), reply_markup=moderation_kb(item_id, p.content_type == "deal"))
    await pipeline.db.update(item_id, status="pending", draft_msg_ids=item["draft_msg_ids"] + [ctrl.message_id])


@router.message(F.text, F.from_user.id.func(lambda uid: uid in pending_edits))
async def on_edit_text(msg: Message, pipeline: Pipeline) -> None:
    item_id = pending_edits.pop(msg.from_user.id)
    item = await pipeline.db.get(item_id)
    if not item or item["status"] not in ("pending", "approved"):
        await msg.answer("Этот черновик уже обработан.")
        return
    p = pipeline.profiles[item["channel"]]
    if msg.text.startswith("!") and not pipeline.rewriter.uses_llm:
        pending_edits[msg.from_user.id] = item_id
        await msg.answer("Указания «!» работают только с LLM. Пришлите готовый текст поста целиком.")
        return
    if msg.text.startswith("!"):
        await msg.answer("Переписываю…")
        item = await pipeline.regenerate(item_id, note=msg.text[1:].strip())
    else:
        # html_text сохраняет форматирование, которое модератор сделал в Telegram (жирный, цитаты…)
        await pipeline.db.update(item_id, caption=msg.html_text)
        item = await pipeline.db.get(item_id)
    # Новое управляющее сообщение — старое останется выше в истории
    ctrl = await msg.answer(_control_text(p, item), reply_markup=moderation_kb(item_id, p.content_type == "deal"))
    await pipeline.db.update(item_id, status="pending", draft_msg_ids=item["draft_msg_ids"] + [ctrl.message_id])


# ---------- товары, которые модератор прислал сам ----------

MARKET_LINK = re.compile(r"https?://(?:[\w-]+\.)*(?:market\.yandex\.ru|ya\.cc)/\S+")


@router.message(F.text.func(lambda t: bool(MARKET_LINK.search(t or ""))))
async def on_market_links(msg: Message, pipeline: Pipeline) -> None:
    """Ссылки на Яндекс Маркет (например, «Поделиться» из «Товаров с наградой») -> новые товары для канала находок."""
    profile = next((p for p in pipeline.profiles.values() if p.content_type == "deal"), None)
    if not profile:
        await msg.answer("Нет канала с типом deal, некуда добавлять товары.")
        return
    urls = MARKET_LINK.findall(msg.text)
    await msg.answer(f"Принял {len(urls)} ссылок для {html_escape(profile.title)}, открываю товары…")

    async def run() -> None:
        rep = await pipeline.add_manual(profile, urls)
        lines = []
        if rep.get("lists"):
            lines.append("📋 Списков: " + str(len(rep["lists"])) + " (товаров в них: " + ", ".join(rep["lists"]) + ")")
        lines.append(f"✅ Добавлено товаров: {len(rep['added'])}")
        lines += [f"   • {html_escape(n[:70])}" for n in rep["added"][:15]]
        if rep["dup"]:
            lines.append(f"🔁 Уже были (дубли): {len(rep['dup'])}")
        if rep["skip"]:
            lines.append(f"⏭ Не ссылки на товар Маркета: {len(rep['skip'])}")
        if rep["error"]:
            lines.append("⚠️ Не удалось открыть: " + ", ".join(html_escape(u) for u in rep["error"][:5]))
        if rep["added"]:
            lines.append("\nЧерновики придут по мере обработки — ваши товары идут первыми, ссылки остаются ваши.")
        await msg.answer("\n".join(lines))
        if rep["added"]:
            await pipeline.process_job(profile.key)

    asyncio.create_task(run())
