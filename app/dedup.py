"""Поиск дубликатов: один и тот же рецепт часто публикуют разные каналы и сайты.

Три уровня проверки (от дешёвого к дорогому):
1. Текст: отпечаток первых 300 символов (models.text_fingerprint) — при сборе.
2. Картинка: перцептивный хэш (dHash) фото или превью видео — до запроса к модели.
   Ловит одно и то же видео/фото, даже пережатое, обрезанное по краям или с другим текстом.
3. Рецепт: похожее название блюда + похожий набор ингредиентов — после модели.
   Ловит один рецепт с разными фото и разными формулировками.
"""
from __future__ import annotations

import re

from PIL import Image

from app.models import MediaFile

PHASH_MAX_DISTANCE = 6        # из 64 бит; 0 — идентичные, до ~6 — та же картинка после пережатия
TITLE_SIMILARITY = 0.6        # доля общих слов в названии
INGREDIENT_SIMILARITY = 0.6   # доля общих ингредиентов

STOP_WORDS = {"с", "и", "в", "на", "из", "по", "для", "без", "к", "под", "от", "до", "а", "или", "со", "во",
              "рецепт", "домашний", "домашние", "домашняя", "вкусный", "вкусные", "вкусная", "быстрый",
              "простой", "самый", "настоящий", "идеальный", "нежный", "сочный", "минут", "мин"}


def media_hash(media: list[MediaFile]) -> str | None:
    """dHash первого фото или превью первого видео (16 hex-символов)."""
    for m in media:
        path = m.path if m.kind == "photo" else m.thumb
        if not path:
            continue
        try:
            with Image.open(path) as im:
                g = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
                px = list(g.getdata())
        except Exception:
            continue
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
        return f"{bits:016x}"
    return None


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _stems(text: str) -> set[str]:
    words = re.findall(r"[a-zа-яё]+", text.lower().replace("ё", "е"))
    return {w[:5] for w in words if len(w) > 2 and w not in STOP_WORDS}


def title_key(title: str) -> str:
    return " ".join(sorted(_stems(title)))


def ingredients_key(ingredients: list[str]) -> str:
    """Только названия ингредиентов (без количеств): «Сливочное масло — 130 г» -> «масло сливо»."""
    names = set()
    for i in ingredients:
        name = re.split(r"\s[—–-]\s|\d|:|\(", i, maxsplit=1)[0]
        stems = _stems(name)
        if stems:
            names.add(" ".join(sorted(stems)))
    return "|".join(sorted(names))


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def same_recipe(title_a: str, ingr_a: str, title_b: str, ingr_b: str) -> bool:
    """Ключи — из title_key / ingredients_key."""
    t = _jaccard(set(title_a.split()), set(title_b.split()))
    ia, ib = set(filter(None, ingr_a.split("|"))), set(filter(None, ingr_b.split("|")))
    i = _jaccard(ia, ib)
    if t >= TITLE_SIMILARITY and i >= INGREDIENT_SIMILARITY:
        return True
    # Совсем одинаковый состав (5+ ингредиентов) — дубль, даже если блюдо назвали иначе
    return i >= 0.85 and min(len(ia), len(ib)) >= 5
