"""Обработка поста без LLM: отсев рекламы, чистка, разбор рецепта и сборка по шаблону канала.

Работает на типичной структуре кулинарных постов:
  заголовок -> «Ингредиенты:» / «Нам понадобится:» -> список -> «Приготовление:» -> шаги.
Текст не переписывается своими словами: заголовок берётся из источника, а призыв
и эмодзи подставляются из профиля канала.
"""
from __future__ import annotations

import random
import re

from app.config import ChannelProfile
from app.models import MediaFile

# --- отсев ---
AD_MARKERS = re.compile(
    r"\b(erid|реклама|рекламн\w*|промокод\w*|скидк[аиу]|розыгрыш\w*|giveaway|партн[её]рск\w*|"
    r"заказывай\w*|курс[аы]? по|марафон\w*|вебинар\w*)\b",
    re.I,
)
# --- чистка ---
URL = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+)", re.I)
MENTION = re.compile(r"@\w{3,}")
HASHTAG = re.compile(r"#[\w_]+")
JUNK_LINE = re.compile(
    r"(подпис|подпиш|наш канал|в канале|переходи|ставь|лайк|репост|сохран|источник|автор|"
    r"больше рецептов|присоединяй|жми|кликай|ссылк)",
    re.I,
)
# --- структура ---
ING_HEADER = re.compile(
    r"^\W*(ингредиенты|нам понадобится|вам понадобится|понадобится|потребуется|нужно|состав|продукты)\b[^\n]*:?\s*$",
    re.I,
)
STEPS_HEADER = re.compile(
    r"^\W*(приготовление|способ приготовления|как готовить|как приготовить|готовим|рецепт|процесс)\b[^\n]*:?\s*$",
    re.I,
)
INLINE_ING = re.compile(r"^\W*(ингредиенты|нам понадобится|вам понадобится|понадобится|состав)\s*:\s*(.+)$", re.I)
QTY = re.compile(
    r"(\d|½|¼|¾|\b(г|гр|кг|мл|л|шт|ст\.?\s?л|ч\.?\s?л|стакан\w*|щепотк\w*|пучок|пучка|зубч\w*|"
    r"банк\w*|упаковк\w*|по вкусу|для подачи|для жарки|горсть)\b)",
    re.I,
)
BULLET = re.compile(r"^[\W\d_]*?(?=[A-Za-zА-Яа-яЁё0-9½¼¾])")
LETTER = re.compile(r"[A-Za-zА-Яа-яЁё]")
KBZHU = re.compile(r"\b(кбжу|калорийност\w*|ккал|б/ж/у|бжу)\b", re.I)
SUBHEADER = re.compile(r"^\W*(для|на)\s+[^:]{2,30}:\s*$", re.I)  # «Для крема:», «Для соуса:»
STEP_NUM = re.compile(r"^\s*(\d+[.)]|шаг\s*\d+[.:)]?)\s*", re.I)
BENEFITS = [
    re.compile(r"за\s+\d+\s*(минут\w*|мин\.?)", re.I),
    re.compile(r"без\s+(выпечки|духовки|муки|сахара|яиц|возни|заморочек)", re.I),
    re.compile(r"\b(пп|диетическ\w*|низкокалорийн\w*|полезн\w*)\b", re.I),
    re.compile(r"\b(на сковороде|в духовке|в аэрогриле|в мультиварке|в микроволновке)\b", re.I),
]

DEFAULT_CTAS = [
    "Сохрани, ставь лайк и готовь с удовольствием!",
    "Сохраняй, чтобы не потерять, и готовь с удовольствием!",
    "Сохрани рецепт и порадуй близких!",
    "Сохраняй и готовь уже сегодня!",
    "Сохрани, пригодится! Приятного аппетита!",
]
DEFAULT_EMOJIS = ["👌🏻", "😋", "🔥", "❤️", "👍🏻", "🤤"]



class RuleResult:
    def __init__(self, ok: bool, reason: str | None = None, title: str = "", emoji: str = "", intro: str = "",
                 ingredients: list[str] | None = None, steps: list[str] | None = None):
        self.is_recipe, self.reject_reason = ok, reason
        self.title, self.emoji, self.intro = title, emoji, intro
        self.ingredients, self.steps = ingredients or [], steps or []


def process(profile: ChannelProfile, text: str, media: list[MediaFile], seed: int | None = None) -> RuleResult:
    """Без LLM. Длину подписи подгоняет format_caption (общий для всех режимов)."""
    rnd = random.Random(seed)
    if AD_MARKERS.search(text):
        return RuleResult(False, "похоже на рекламу")

    lines = _clean_lines(text)
    title, ingredients, steps = _parse(lines)
    if len(ingredients) < 2:
        return RuleResult(False, "не найден список ингредиентов")
    if not title:
        return RuleResult(False, "не найдено название блюда")

    ctas = profile.ctas or DEFAULT_CTAS
    emojis = profile.emojis or DEFAULT_EMOJIS
    return RuleResult(True, title=_title_with_benefit(title, text, ingredients), emoji=rnd.choice(emojis),
                      intro=rnd.choice(ctas), ingredients=ingredients, steps=steps[:8])


def _clean_lines(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = URL.sub("", raw)
        line = MENTION.sub("", line)
        line = HASHTAG.sub("", line).strip()
        if not line:
            out.append("")
            continue
        if JUNK_LINE.search(line) and not QTY.search(line) and not ING_HEADER.match(line):
            continue
        out.append(re.sub(r"\s{2,}", " ", line))
    return out


def _strip_bullet(line: str) -> str:
    line = STEP_NUM.sub("", line) if STEP_NUM.match(line) else line
    m = BULLET.match(line)
    line = line[m.end():] if m else line
    return line.strip(" -–—•·▪️▫️✅✔️☑️🔸🔹").strip()


def _parse(lines: list[str]) -> tuple[str, list[str], list[str]]:
    title = ""
    ingredients: list[str] = []
    steps: list[str] = []
    mode = "head"
    for line in lines:
        if not line:
            if mode == "ing" and ingredients:
                mode = "after_ing"
            continue
        if not LETTER.search(line) or KBZHU.search(line):  # разделители, эмодзи, КБЖУ
            continue
        if SUBHEADER.match(line) and mode != "steps":
            mode = "ing"
            continue
        if line.count(",") >= 2 and QTY.search(line) and mode in ("ing", "after_ing") and not STEP_NUM.match(line):
            ingredients += [_strip_bullet(x) for x in line.split(",") if x.strip()]  # «1 яйцо, 200 мл молока, …»
            mode = "ing"
            continue
        if m := INLINE_ING.match(line):  # «Ингредиенты: мука, яйца, молоко»
            ingredients += [_strip_bullet(x) for x in re.split(r"[,;]", m.group(2)) if x.strip()]
            mode = "after_ing"
            continue
        if ING_HEADER.match(line):
            mode = "ing"
            continue
        if STEPS_HEADER.match(line):
            mode = "steps"
            continue

        if mode == "head":
            if not title and not QTY.search(line[:3]):
                title = _clean_title(line)
            elif QTY.search(line) and len(line) < 70:  # список без заголовка «Ингредиенты»
                ingredients.append(_strip_bullet(line))
                mode = "ing"
        elif mode == "ing":
            if len(line) < 90:
                ingredients.append(_strip_bullet(line))
            else:
                mode, steps = "steps", [line]
        elif mode in ("after_ing", "steps"):
            if mode == "after_ing" and QTY.search(line) and len(line) < 70 and not STEP_NUM.match(line):
                ingredients.append(_strip_bullet(line))  # ингредиенты после пустой строки (подгруппы)
                continue
            mode = "steps"
            s = _strip_bullet(line)
            if s:
                steps.append(s)
    ingredients = [i for i in ingredients if i and len(i) > 1]
    return title, ingredients, steps


def _clean_title(line: str) -> str:
    t = _strip_bullet(line)
    t = re.sub(r"[\s:!.,]+$", "", t)
    t = re.sub(r"[^\w\s«»\"'()\-–—,.!?%/]", "", t).strip()  # убираем эмодзи из заголовка
    if len(t) > 90:
        t = t[:90].rsplit(" ", 1)[0]
    return t[:1].upper() + t[1:] if t else ""


def _title_with_benefit(title: str, text: str, ingredients: list[str]) -> str:
    extras = []
    for rx in BENEFITS:
        m = rx.search(text)
        if m and m.group(0).lower() not in title.lower():
            extras.append(m.group(0).lower())
            break
    if 2 <= len(ingredients) <= 5 and "ингредиент" not in title.lower():
        extras.append(f"из {len(ingredients)} ингредиентов")
    return f"{title} {' '.join(extras)}".strip()
