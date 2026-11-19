"""Бот модерации: кнопки под черновиками и служебные команды."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

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
        "/publish &lt;канал&gt; — опубликовать следующий из очереди\n"
        "/channels — проверка вкл/выкл и число постов в день для каждого канала\n"
        "/pause, /resume [канал] [сбор|постинг] — пауза; без аргументов — всё. Кнопки паузы — под /status\n"
        "/server — версия, диск, прокси · /logs [N] — последние строки лога\n"
        "/update — обновить из GitHub и перезапуститься · /restart — перезапуск\n\n"
        "Черновики приходят сюда с кнопками. В режиме «✏️ Изменить» пришлите новый текст поста "
        "или (если подключена LLM) начните сообщение с «!», чтобы дать указание модели (например: «! короче и без шагов»)."
    )


WHAT_RU = {"collect": "Сбор", "post": "Постинг"}


async def _status_text(pipeline: Pipeline, scheduler) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    lines = []
    for key, p in pipeline.profiles.items():
        counts = await pipeline.db.count(key)
        stats = ", ".join(f"{STATUS_RU.get(s, s)}: {c}" for s, c in sorted(counts.items())) or "пусто"
        if pipeline.uses_scheduled(p):
            sch = await pipeline.db.by_status(key, "scheduled", limit=1000, order="scheduled_at")
            tz = ZoneInfo(p.timezone)
            info = f"В отложенных: {len(sch)}"
            if sch:
                info += (f", ближайший {datetime.fromisoformat(sch[0]['scheduled_at']).astimezone(tz):%d.%m %H:%M}"
                         f", расписано до {datetime.fromisoformat(sch[-1]['scheduled_at']).astimezone(tz):%d.%m}")
        else:
            job = scheduler.get_job(f"publish:{key}")
            info = "Следующая публикация: " + (job.next_run_time.strftime("%d.%m %H:%M") if job and job.next_run_time else "—")
        parts = []
        for w in ("collect", "post"):
            parts.append(f"{WHAT_RU[w]}: {'⏸ пауза' if await pipeline.is_paused(key, w) else '▶️ работает'}")
        state = " · ".join(parts)
        lines.append(f"<b>{html_escape(p.title)}</b> ({key})\n{state}\n{stats}\n{info}")
    return "\n\n".join(lines)


async def _status_kb(pipeline: Pipeline):
    kb = InlineKeyboardBuilder()
    for key, p in pipeline.profiles.items():
        for w in ("collect", "post"):
            paused = await pipeline.is_paused(key, w)
            kb.button(text=f"{'▶️' if paused else '⏸'} {WHAT_RU[w]} · {p.title}",
                      callback_data=f"ctl:{key}:{w}:{'on' if paused else 'off'}")
    kb.adjust(2)
    return kb.as_markup()


@router.message(Command("status"))
async def cmd_status(msg: Message, pipeline: Pipeline, scheduler) -> None:
    await msg.answer(await _status_text(pipeline, scheduler), reply_markup=await _status_kb(pipeline))


@router.callback_query(F.data.startswith("ctl:"))
async def on_control(cb: CallbackQuery, pipeline: Pipeline, scheduler) -> None:
    _, key, what, mode = cb.data.split(":")
    if key not in pipeline.profiles or what not in WHAT_RU:
        await cb.answer("Неизвестный канал", show_alert=True)
        return
    await pipeline.set_paused(key, what, mode == "off")
    p = pipeline.profiles[key]
    await cb.answer(f"{WHAT_RU[what]} · {p.title}: {'пауза' if mode == 'off' else 'возобновлён'}")
    if mode == "on" and what == "post" and pipeline.uses_scheduled(p):
        asyncio.create_task(pipeline.sync_job(key))  # сразу разложить накопившуюся очередь
    await cb.message.edit_text(await _status_text(pipeline, scheduler), reply_markup=await _status_kb(pipeline))


def _parse_pause_args(args: str | None, pipeline: Pipeline) -> tuple[list[str], list[str]] | str:
    """/pause [канал] [сбор|постинг] -> (каналы, что). Без аргументов — все каналы, всё."""
    keys, whats = [], []
    for a in (args or "").lower().split():
        if a in pipeline.profiles:
            keys.append(a)
        elif a.startswith("сбор") or a == "collect":
            whats.append("collect")
        elif a.startswith("пост") or a == "post":
            whats.append("post")
        else:
            return f"Не понял «{html_escape(a)}». Каналы: {', '.join(pipeline.profiles)}; что: сбор, постинг."
    return keys or list(pipeline.profiles), whats or ["collect", "post"]


@router.message(Command("pause", "resume"))
async def cmd_pause(msg: Message, command: CommandObject, pipeline: Pipeline, scheduler) -> None:
    parsed = _parse_pause_args(command.args, pipeline)
    if isinstance(parsed, str):
        await msg.answer(parsed)
        return
    keys, whats = parsed
    paused = command.command == "pause"
    for k in keys:
        for w in whats:
            await pipeline.set_paused(k, w, paused)
        if not paused and "post" in whats and pipeline.uses_scheduled(pipeline.profiles[k]):
            asyncio.create_task(pipeline.sync_job(k))
    await msg.answer(await _status_text(pipeline, scheduler), reply_markup=await _status_kb(pipeline))


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


@router.message(F.text, ~F.text.startswith("/"), F.from_user.id.func(lambda uid: uid in pending_links))
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


@router.message(F.text, ~F.text.startswith("/"), F.from_user.id.func(lambda uid: uid in pending_edits))
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
            if await pipeline.is_paused(profile.key, "collect"):
                await msg.answer("⏸ Сбор на паузе — черновики по этим товарам придут после /resume.")
            await pipeline.process_job(profile.key)

    asyncio.create_task(run())


# ---------- управление приложением на сервере ----------

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
UNDER_SYSTEMD = bool(os.environ.get("INVOCATION_ID"))  # systemd выставляет переменную своим службам


async def _run(*cmd: str, timeout: int = 300) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=ROOT_DIR, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "таймаут"
    return proc.returncode, out.decode(errors="replace")


def _restart_soon() -> None:
    """Выходим — systemd (Restart=always) поднимет процесс заново через несколько секунд."""
    asyncio.get_running_loop().call_later(1.5, lambda: os._exit(0))


@router.message(Command("logs"))
async def cmd_logs(msg: Message, command: CommandObject, pipeline: Pipeline) -> None:
    n = int(command.args) if (command.args or "").strip().isdigit() else 30
    path = pipeline.settings.log_path
    if not path.exists():
        await msg.answer("Лог-файла пока нет.")
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-min(n, 200):]
    text = "\n".join(lines)[-3800:]
    await msg.answer(f"<pre>{html_escape(text)}</pre>" if text else "Лог пуст.")


@router.message(Command("server"))
async def cmd_server(msg: Message, pipeline: Pipeline) -> None:
    _, commit = await _run("git", "log", "-1", "--format=%h %ci %s")
    _, disk = await _run("df", "-h", str(ROOT_DIR))
    media = sum(f.stat().st_size for f in pipeline.settings.media_dir.rglob("*") if f.is_file()) / 1e6 \
        if pipeline.settings.media_dir.exists() else 0
    await msg.answer(
        f"<b>Версия:</b> {html_escape(commit.strip())}\n"
        f"<b>systemd:</b> {'да' if UNDER_SYSTEMD else 'нет (запущено вручную)'}\n"
        f"<b>Прокси Telegram:</b> {html_escape(pipeline.settings.tg_proxy or 'нет')}\n"
        f"<b>Медиа на диске:</b> {media:.0f} МБ\n<pre>{html_escape(disk.strip())}</pre>")


@router.message(Command("restart"))
async def cmd_restart(msg: Message) -> None:
    if not UNDER_SYSTEMD:
        await msg.answer("Приложение запущено не как служба systemd — перезапустите его вручную.")
        return
    await msg.answer("Перезапускаюсь… Через ~10 секунд проверьте /server.")
    _restart_soon()


@router.message(Command("update"))
async def cmd_update(msg: Message) -> None:
    """git pull + зависимости + перезапуск."""
    if not UNDER_SYSTEMD:
        await msg.answer("Обновление через бота работает только на сервере (служба systemd).")
        return
    await msg.answer("Обновляю код из GitHub…")
    code, out = await _run("git", "pull", "--ff-only")
    if code != 0:
        await msg.answer(f"❌ git pull не удался:\n<pre>{html_escape(out[-3000:])}</pre>")
        return
    if "Already up to date" in out or "Уже обновлено" in out:
        await msg.answer("Новых изменений нет.")
        return
    code, pip_out = await _run(sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt", timeout=900)
    if code != 0:
        await msg.answer(f"❌ Установка зависимостей не удалась, остаюсь на старой версии процесса:\n"
                         f"<pre>{html_escape(pip_out[-3000:])}</pre>")
        return
    _, commit = await _run("git", "log", "-1", "--format=%h %s")
    await msg.answer(f"✅ Обновлено до {html_escape(commit.strip())}\n<pre>{html_escape(out[-1500:])}</pre>\nПерезапускаюсь…")
    _restart_soon()


# ---------- настройки каналов: проверка и постов в день ----------

async def _channels_text(pipeline: Pipeline) -> str:
    lines = ["<b>Каналы</b>"]
    for key, p in pipeline.profiles.items():
        mod = await pipeline.moderation(key)
        ppd = await pipeline.posts_per_day(key)
        lines.append(
            f"\n<b>{html_escape(p.title)}</b> ({key})\n"
            f"Проверка: {'🛡 включена — черновики приходят вам' if mod else '⚡️ выключена — сразу в отложенные'}\n"
            f"Постов в день: <b>{ppd}</b> · окно {p.day_start}–{p.day_end}, "
            f"примерно раз в {_fmt_interval(p, ppd)}, время случайное")
    lines.append("\nУдалённый вручную пост из отложенных заменяется следующим из очереди (проверка раз в 5 минут).")
    return "\n".join(lines)


def _fmt_interval(p, ppd: int) -> str:
    from app.pipeline import day_intervals
    from datetime import date
    a, b = day_intervals(p, date.today(), ppd)[0]
    mins = int((b - a).total_seconds() // 60)
    return f"{mins // 60} ч {mins % 60:02d} мин" if mins >= 60 else f"{mins} мин"


async def _channels_kb(pipeline: Pipeline):
    kb = InlineKeyboardBuilder()
    sizes = []
    for key, p in pipeline.profiles.items():
        mod = await pipeline.moderation(key)
        kb.button(text=f"{'🛡 Проверка: вкл' if mod else '⚡️ Проверка: выкл'} · {p.title}", callback_data=f"chan:{key}:mod")
        ppd = await pipeline.posts_per_day(key)
        kb.button(text="−", callback_data=f"chan:{key}:ppd:-1")
        kb.button(text=f"{ppd} в день", callback_data=f"chan:{key}:noop")
        kb.button(text="+", callback_data=f"chan:{key}:ppd:1")
        sizes += [1, 3]
    kb.adjust(*sizes)
    return kb.as_markup()


@router.message(Command("channels"))
async def cmd_channels(msg: Message, pipeline: Pipeline) -> None:
    await msg.answer(await _channels_text(pipeline), reply_markup=await _channels_kb(pipeline))


@router.callback_query(F.data.startswith("chan:"))
async def on_channel_setting(cb: CallbackQuery, pipeline: Pipeline) -> None:
    parts = cb.data.split(":")
    key, action = parts[1], parts[2]
    if key not in pipeline.profiles:
        await cb.answer("Неизвестный канал", show_alert=True)
        return
    p = pipeline.profiles[key]
    if action == "noop":
        await cb.answer("Кнопками − и + меняется число постов в день. Точное значение: /ppd канал число")
        return
    if action == "mod":
        on = not await pipeline.moderation(key)
        await pipeline.set_moderation(key, on)
        await cb.answer(f"{p.title}: проверка {'включена' if on else 'выключена'}")
        if not on:
            asyncio.create_task(pipeline.process_job(key))
    elif action == "ppd":
        n = await pipeline.set_posts_per_day(key, await pipeline.posts_per_day(key) + int(parts[3]))
        await cb.answer(f"{p.title}: {n} постов в день")
        asyncio.create_task(pipeline.sync_job(key))
    await cb.message.edit_text(await _channels_text(pipeline), reply_markup=await _channels_kb(pipeline))


@router.message(Command("ppd"))
async def cmd_ppd(msg: Message, command: CommandObject, pipeline: Pipeline) -> None:
    args = (command.args or "").split()
    if len(args) != 2 or args[0] not in pipeline.profiles or not args[1].isdigit():
        await msg.answer(f"Формат: /ppd канал число, например /ppd shop_sparks 12. Каналы: {', '.join(pipeline.profiles)}")
        return
    n = await pipeline.set_posts_per_day(args[0], int(args[1]))
    asyncio.create_task(pipeline.sync_job(args[0]))
    await msg.answer(f"Готово: {n} постов в день.\n\n" + await _channels_text(pipeline),
                     reply_markup=await _channels_kb(pipeline))
