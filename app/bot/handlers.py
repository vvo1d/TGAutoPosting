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
        "/settings — все настройки каналов: проверка, постов в день, лимиты сбора и места на диске\n"        "/max — публикация в каналы MAX вместе с Telegram\n"
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
        f"<b>Медиа на диске:</b> {media:.0f} МБ (лимит {await pipeline.setting('global', 'media_mb')} МБ, /settings)\n<pre>{html_escape(disk.strip())}</pre>")


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


# ---------- настройки из бота: /settings (и /channels) ----------

def _mb(b: int) -> str:
    return f"{b / 1e6:.0f} МБ" if b < 1e9 else f"{b / 1e9:.1f} ГБ"


async def _settings_menu(pipeline: Pipeline) -> tuple[str, object]:
    quota = await pipeline.setting("global", "media_mb") * 1_000_000
    used = pipeline.media_usage()
    lines = ["<b>Настройки</b>", f"Медиа на диске: {_mb(used)} из {_mb(quota)}"]
    kb = InlineKeyboardBuilder()
    for key, p in pipeline.profiles.items():
        mod = await pipeline.moderation(key)
        ppd = await pipeline.posts_per_day(key)
        lines.append(f"\n<b>{html_escape(p.title)}</b>: {'🛡 с проверкой' if mod else '⚡️ без проверки'}, {ppd} постов в день")
        kb.button(text=f"⚙️ {p.title}", callback_data=f"st:ch:{key}")
    kb.button(text="🌐 Общие (диск, уборка)", callback_data="st:gl")
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


async def _channel_page(pipeline: Pipeline, key: str) -> tuple[str, object]:
    from app.settings_rt import CHANNEL_PARAMS

    p = pipeline.profiles[key]
    mod = await pipeline.moderation(key)
    ppd = await pipeline.posts_per_day(key)
    counts = await pipeline.db.count(key)
    lines = [f"<b>⚙️ {html_escape(p.title)}</b>",
             f"Проверка: {'🛡 включена — черновики приходят вам' if mod else '⚡️ выключена — сразу в отложенные'}",
             f"Публикации: окно {p.day_start}–{p.day_end}, ~раз в {_fmt_interval(p, ppd)}, время случайное",
             _plan_text(await pipeline.plan(p)), ""]
    kb = InlineKeyboardBuilder()
    kb.button(text="🛡 Проверка: вкл" if mod else "⚡️ Проверка: выкл", callback_data=f"st:mod:{key}")
    sizes = [1]
    for name, par in CHANNEL_PARAMS.items():
        v = await pipeline.setting(key, name)
        if par.hint:
            lines.append(f"• {par.label}: <b>{v}</b> — {par.hint}")
        else:
            lines.append(f"• {par.label}: <b>{v}</b>")
        kb.button(text="−", callback_data=f"st:set:{key}:{name}:-{par.step}")
        kb.button(text=f"{par.label}: {v}", callback_data=f"st:info:{key}:{name}")
        kb.button(text="+", callback_data=f"st:set:{key}:{name}:{par.step}")
        sizes.append(3)
    kb.button(text="◀️ Назад", callback_data="st:menu")
    sizes.append(1)
    kb.adjust(*sizes)
    return "\n".join(lines), kb.as_markup()


def _plan_text(pl: dict) -> str:
    """План работы канала: сколько постов нужно на горизонт и что из этого следует для сбора."""
    lines = [f"📋 <b>План:</b> {pl['ppd']} в день × {pl['days']} дн. = "
             + (f"{pl['ppd'] * pl['days']}, но лимит — <b>{pl['target']}</b> постов вперёд" if pl["capped"]
                else f"<b>{pl['target']}</b> постов вперёд"),
             f"Лимит Telegram: {pl['limit']} сообщений в отложке, у этого канала пост ≈ {pl['per_post']:g} сообщ. "
             f"→ максимум {pl['max_posts']} постов, при {pl['ppd']} в день — на <b>{pl['max_days']} дн.</b>",
             f"Есть {pl['have']}: в отложке {pl['future']}, в очереди {pl['approved']}, на проверке {pl['pending']}"]
    if pl["need"] <= 0:
        lines.append("✅ Запас полон — сбор и обработка отдыхают")
    else:
        y = f"выход годных {pl['yield'] * 100:.0f}%" + ("" if pl["yield_sample"] >= 10 else " (пока оценка)")
        lines.append(f"Не хватает <b>{pl['need']}</b> → нужно ~{pl['raw_needed']} материалов ({y}), "
                     f"необработанных {pl['backlog']}")
        lines.append("⬇️ Сбор: ещё ~" + str(pl["collect"]) if pl["collect"] else "⏳ Сбор не нужен — хватит необработанных")
    return "\n".join(lines)


async def _global_page(pipeline: Pipeline, note: str = "") -> tuple[str, object]:
    from app.settings_rt import GLOBAL_PARAMS

    used = pipeline.media_usage()
    lines = ["<b>🌐 Общие настройки</b>", f"Медиа на диске сейчас: {_mb(used)}", ""]
    kb = InlineKeyboardBuilder()
    sizes = []
    for name, par in GLOBAL_PARAMS.items():
        v = await pipeline.setting("global", name)
        lines.append(f"• {par.label}: <b>{v}</b>" + (f" — {par.hint}" if par.hint else ""))
        kb.button(text="−", callback_data=f"st:set:global:{name}:-{par.step}")
        kb.button(text=f"{par.label}: {v}", callback_data=f"st:info:global:{name}")
        kb.button(text="+", callback_data=f"st:set:global:{name}:{par.step}")
        sizes.append(3)
    lines.append("\nУборка идёт сама раз в час: устаревшие необработанные посты и файлы, которые больше не нужны.")
    if note:
        lines.append(f"\n{note}")
    kb.button(text="🧹 Убрать сейчас", callback_data="st:clean")
    kb.button(text="◀️ Назад", callback_data="st:menu")
    sizes += [1, 1]
    kb.adjust(*sizes)
    return "\n".join(lines), kb.as_markup()


def _fmt_interval(p, ppd: int) -> str:
    from datetime import date
    from app.pipeline import day_intervals

    a, b = day_intervals(p, date.today(), ppd)[0]
    mins = int((b - a).total_seconds() // 60)
    return f"{mins // 60} ч {mins % 60:02d} мин" if mins >= 60 else f"{mins} мин"


@router.message(Command("settings", "channels"))
async def cmd_settings(msg: Message, pipeline: Pipeline) -> None:
    text, kb = await _settings_menu(pipeline)
    await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("st:"))
async def on_settings(cb: CallbackQuery, pipeline: Pipeline, scheduler) -> None:
    from app.settings_rt import CHANNEL_PARAMS, GLOBAL_PARAMS

    parts = cb.data.split(":")
    action = parts[1]
    page = None
    if action == "menu":
        page = await _settings_menu(pipeline)
    elif action == "ch" and parts[2] in pipeline.profiles:
        page = await _channel_page(pipeline, parts[2])
    elif action == "gl":
        page = await _global_page(pipeline)
    elif action == "mod" and parts[2] in pipeline.profiles:
        key = parts[2]
        on = not await pipeline.moderation(key)
        await pipeline.set_moderation(key, on)
        await cb.answer(f"Проверка {'включена' if on else 'выключена'}")
        if not on:
            asyncio.create_task(pipeline.process_job(key))
        page = await _channel_page(pipeline, key)
    elif action == "info":
        par = (GLOBAL_PARAMS if parts[2] == "global" else CHANNEL_PARAMS)[parts[3]]
        await cb.answer(f"{par.label}: от {par.lo} до {par.hi}. {par.hint}".strip(), show_alert=True)
        return
    elif action == "set":
        scope, name, delta = parts[2], parts[3], int(parts[4])
        if scope != "global" and scope not in pipeline.profiles:
            await cb.answer("Неизвестный канал", show_alert=True)
            return
        v = await pipeline.set_setting(scope, name, await pipeline.setting(scope, name) + delta)
        await cb.answer(f"= {v}")
        if name == "collect_min" and scheduler.get_job(f"collect:{scope}"):
            scheduler.reschedule_job(f"collect:{scope}", trigger="interval", minutes=v)
        if name in ("ppd", "buffer_days", "max_pending"):  # цель изменилась — пересчитать план сразу
            asyncio.create_task(pipeline.collect_job(scope))
            asyncio.create_task(pipeline.process_job(scope))
            asyncio.create_task(pipeline.sync_job(scope))
        page = await (_global_page(pipeline) if scope == "global" else _channel_page(pipeline, scope))
    elif action == "clean":
        await cb.answer("Убираю…")
        expired = 0
        for p in pipeline.profiles.values():
            expired += await pipeline.expire_stale(p)
        dirs, freed = await pipeline.cleanup_media()
        page = await _global_page(pipeline, f"🧹 Удалено устаревших постов: {expired}, папок медиа: {dirs}, освобождено {_mb(freed)}")
    if page:
        if action not in ("mod", "set", "clean"):
            await cb.answer()
        try:
            await cb.message.edit_text(page[0], reply_markup=page[1])
        except Exception:
            pass  # «message is not modified» — значение упёрлось в границу


@router.message(Command("ppd"))
async def cmd_ppd(msg: Message, command: CommandObject, pipeline: Pipeline) -> None:
    args = (command.args or "").split()
    if len(args) != 2 or args[0] not in pipeline.profiles or not args[1].isdigit():
        await msg.answer(f"Формат: /ppd канал число, например /ppd shop_sparks 12. Каналы: {', '.join(pipeline.profiles)}")
        return
    n = await pipeline.set_posts_per_day(args[0], int(args[1]))
    asyncio.create_task(pipeline.sync_job(args[0]))
    text, kb = await _channel_page(pipeline, args[0])
    await msg.answer(f"Готово: {n} постов в день.\n\n{text}", reply_markup=kb)


# ---------- MAX ----------

@router.message(Command("max"))
async def cmd_max(msg: Message, command: CommandObject, pipeline: Pipeline) -> None:
    """/max — статус и найденные каналы; /max канал chat_id — включить; /max канал off — выключить."""
    if not pipeline.max:
        await msg.answer("MAX не подключён: впишите MAX_TOKEN в .env на сервере и перезапустите (/restart).\n"
                         "Токен: business.max.ru → Чат-боты → ⋮ → Настройки.")
        return
    args = (command.args or "").split()
    if len(args) == 2 and args[0] in pipeline.profiles:
        key = args[0]
        if args[1].lower() in ("off", "выкл", "0"):
            await pipeline.set_max_chat(key, None)
            await msg.answer(f"MAX для {html_escape(pipeline.profiles[key].title)} выключен.")
            return
        try:
            known = {t.lower(): cid for cid, t in (await pipeline.max_known_chats()).items() if t}
            if args[1].lower() in known:  # можно указать название канала из списка /max
                args[1] = str(known[args[1].lower()])
            if re.fullmatch(r"-?\d+", args[1]):
                chat_id = int(args[1])
                info = await pipeline.max.chat(chat_id)
            else:  # ссылка на канал: https://max.ru/channel_Name
                info = await pipeline.max.chat_by_link(args[1])
                chat_id = int(info["chat_id"])
        except Exception as e:
            await msg.answer(f"❌ MAX не даёт доступ к чату {args[1]}: {html_escape(str(e)[:300])}\n"
                             "Проверьте, что бот добавлен в канал администратором.")
            return
        await pipeline.set_max_chat(key, chat_id)
        await msg.answer(f"✅ {html_escape(pipeline.profiles[key].title)} → MAX «{html_escape(info.get('title') or str(chat_id))}».\n"
                         "Новые посты будут выходить в MAX в ту же минуту, что и в Telegram.\n"
                         "Ставлю в очередь MAX посты, которые уже стоят в отложке Telegram…")
        asyncio.create_task(_backfill_and_report(msg, pipeline, key))
        return

    lines = []
    try:
        me = await pipeline.max.me()
        lines.append(f"<b>MAX</b>: бот {html_escape(me.get('name') or '')} @{html_escape(me.get('username') or '')}")
    except Exception as e:
        await msg.answer(f"❌ MAX: не удалось подключиться: {html_escape(str(e)[:300])}")
        return
    for key, p in pipeline.profiles.items():
        chat = await pipeline.max_chat(key)
        cur = await pipeline.db.conn.execute(
            "SELECT max_status, COUNT(*) FROM items WHERE channel=? AND max_status IS NOT NULL GROUP BY max_status", (key,))
        st = {r[0]: r[1] for r in await cur.fetchall()}
        lines.append(f"\n<b>{html_escape(p.title)}</b> ({key}): " + (f"→ MAX {chat}" if chat else "MAX выключен")
                     + (f"\nв очереди {st.get('queued', 0)}, опубликовано {st.get('sent', 0)}, ошибок {st.get('failed', 0)}" if st else ""))
    try:
        await pipeline.max_poll()
    except Exception as e:
        lines.append(f"\n(не удалось получить события MAX: {html_escape(str(e)[:150])})")
    chats = await pipeline.max_known_chats()
    if chats:
        lines.append("\n<b>Каналы и чаты, куда добавлен бот:</b>")
        lines += [f"• {html_escape(t or 'без названия')}: <code>{cid}</code>" for cid, t in chats.items()]
    lines.append("\nВключить: <code>/max shop_sparks chat_id</code> или <code>/max shop_sparks https://max.ru/ссылка_канала</code>"
                 " · выключить: <code>/max shop_sparks off</code>\n"
                 "Если канала нет в списке — уберите бота из администраторов канала MAX и назначьте заново: "
                 "MAX сообщает о добавлении только в этот момент. Через минуту повторите /max.")
    await msg.answer("\n".join(lines))


@router.message(Command("maxtest"))
async def cmd_maxtest(msg: Message, command: CommandObject, pipeline: Pipeline) -> None:
    """Пробная публикация в MAX: /maxtest [канал] (по умолчанию — первый канал с включённым MAX)."""
    if not pipeline.max:
        await msg.answer("MAX не подключён (нет MAX_TOKEN в .env).")
        return
    key = (command.args or "").strip()
    if not key:
        key = next((k for k in pipeline.profiles if await pipeline.max_chat(k)), "")
    if key not in pipeline.profiles:
        await msg.answer(f"Укажите канал: /maxtest taste_recipe. Каналы: {', '.join(pipeline.profiles)}")
        return
    await msg.answer("Публикую пробный пост в MAX… (видео может обрабатываться до пары минут)")
    try:
        info = await pipeline.max_test(key)
    except Exception as e:
        await msg.answer(f"❌ MAX: {html_escape(str(e)[:500])}")
        return
    await msg.answer(f"✅ Опубликовано в MAX: {html_escape(info)}. Проверьте канал.")


async def _backfill_and_report(msg: Message, pipeline: Pipeline, key: str) -> None:
    try:
        rep = await pipeline.max_backfill(key)
    except Exception as e:
        await msg.answer(f"❌ Досинхронизация с MAX не удалась: {html_escape(str(e)[:400])}")
        return
    text = f"🔁 {html_escape(pipeline.profiles[key].title)}: в очередь MAX поставлено {rep['queued']} постов из отложки Telegram"
    if rep["missing"]:
        text += f"; не найдено в отложке {rep['missing']} (уже вышли или удалены)"
    if rep["failed"]:
        text += f"; не удалось скачать {rep['failed']} (повторите /maxsync)"
    await msg.answer(text + ". Они выйдут в MAX в то же время, что и в Telegram.")


@router.message(Command("maxsync"))
async def cmd_maxsync(msg: Message, command: CommandObject, pipeline: Pipeline) -> None:
    """Поставить в очередь MAX посты, уже стоящие в отложке Telegram: /maxsync [канал]."""
    keys = [(command.args or "").strip()] if (command.args or "").strip() else [
        k for k in pipeline.profiles if await pipeline.max_chat(k)]
    keys = [k for k in keys if k in pipeline.profiles and await pipeline.max_chat(k)]
    if not keys:
        await msg.answer("Нет каналов с включённым MAX. Включите: /max канал chat_id")
        return
    await msg.answer("Скачиваю посты из отложки Telegram и ставлю в очередь MAX…")
    for k in keys:
        await _backfill_and_report(msg, pipeline, k)
