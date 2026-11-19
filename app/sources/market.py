"""Товары Яндекс Маркета по ссылкам, которые модератор прислал боту вручную.

Вы сами собираете ссылки «Поделиться» на «Товары с наградой» (реферальная программа Маркета)
и отправляете их боту. Бот открывает страницу товара как обычный посетитель — без входа в ваш
аккаунт — и берёт данные из разметки страницы: название, цену, старую цену, рейтинг, фото, отзывы.
Ваша реферальная ссылка остаётся в посте без изменений.
"""
from __future__ import annotations

import html as html_lib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from app.models import MediaFile

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


@dataclass
class MarketProduct:
    url: str                     # чистая ссылка на карточку
    name: str
    price: int | None
    old_price: int | None
    rating: float | None
    reviews_count: int | None
    category: str | None
    description: str | None
    reviews: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        """Текст «поста-источника» для модели."""
        lines = [f"Товар с Яндекс Маркета: {self.name}"]
        if self.price:
            lines.append(f"Цена: {self.price} ₽" + (f" (было {self.old_price} ₽)" if self.old_price and self.old_price > self.price else ""))
        if self.rating:
            lines.append(f"Рейтинг: {self.rating} из 5" + (f", {self.reviews_count} отзывов" if self.reviews_count else ""))
        if self.category:
            lines.append(f"Категория: {self.category}")
        if self.description:
            lines.append(f"Описание: {self.description}")
        if self.reviews:
            lines.append("Отзывы покупателей:\n" + "\n".join(f"— {r}" for r in self.reviews))
        return "\n".join(lines)


class MarketReader:
    def __init__(self):
        self.http = httpx.AsyncClient(headers={"User-Agent": UA, "Accept-Language": "ru"}, timeout=25,
                                      follow_redirects=True)

    async def stop(self) -> None:
        await self.http.aclose()

    async def product(self, url: str) -> MarketProduct | None:
        r = await self.http.get(url)
        r.raise_for_status()
        html = r.text
        soup = BeautifulSoup(html, "html.parser")
        prod, crumbs = None, []
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(tag.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if d.get("@type") == "Product":
                prod = d
            elif d.get("@type") == "BreadcrumbList":
                crumbs = [i.get("item", {}).get("name") for i in d.get("itemListElement", [])]
        if not prod:
            log.warning("Маркет: на странице %s нет данных товара (капча?)", r.url)
            return None

        offers = prod.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = _int(offers.get("price"))
        old = None
        if m := re.search(r'"oldPrice":\{"amount":\{"intPart":"(\d+)"', html):
            old = int(m.group(1))
        rating = prod.get("aggregateRating") or {}
        desc = prod.get("description") or ""
        desc = re.split(r"\s+–\s+купить|\s+— купить", desc)[0].strip()  # отрезаем SEO-хвост
        reviews = [re.sub(r"\s+", " ", (rv.get("reviewBody") or "")).strip()[:300]
                   for rv in (prod.get("review") or [])[:3]]

        images = []
        main = prod.get("image")
        for u in ([main] if isinstance(main, str) else (main or [])):
            images.append(u)
        for u in re.findall(r"https://avatars\.mds\.yandex\.net/get-mpic/\d+/[\w-]+/orig", html):
            if u not in images:
                images.append(u)

        return MarketProduct(
            url=offers.get("url") or prod.get("url") or str(r.url).split("?")[0],
            name=prod.get("name", "").strip(),
            price=price, old_price=old if old and price and old > price else None,
            rating=_float(rating.get("ratingValue")), reviews_count=_int(rating.get("ratingCount")),
            category=" / ".join(c for c in crumbs[-2:] if c) or None,
            description=desc if desc and desc != prod.get("name") else None,
            reviews=[r for r in reviews if r], images=images[:4],
        )

    async def wishlist_items(self, url: str) -> list[str] | None:
        """Ваш публичный список Маркета -> ссылки на его товары с вашими реферальными метками.

        Маркет сам выдаёт посетителю списка ссылки на товары с refid и utm_referral_context автора списка —
        их и берём. None — ссылка ведёт не на список."""
        r = await self.http.get(url)
        if "/wishlist/" not in str(r.url):
            return None
        out: dict[str, str] = {}
        for m in re.finditer(r'<a [^>]*href="([^"]*refid=[^"]*)"[^>]*data-auto="snippet-', r.text):
            href = html_lib.unescape(m.group(1))
            pid = re.search(r"/(?:card/[^/?]+|product--[^/?]+|product)/(\d{5,})", href)
            if pid and pid.group(1) not in out:
                out[pid.group(1)] = str(httpx.URL(str(r.url)).join(href))
        log.info("Список Маркета: товаров со ссылками — %d", len(out))
        return list(out.values())

    async def download_images(self, urls: list[str], dest: Path) -> list[MediaFile]:
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        for n, u in enumerate(urls):
            try:
                r = await self.http.get(u)
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("Маркет: картинка %s: %s", u, e)
                continue
            path = dest / f"img{n}.jpg"
            try:  # Маркет отдаёт и PNG, и WebP — приводим к JPEG до 1600px, чтобы Telegram принял как фото
                with Image.open(io.BytesIO(r.content)) as im:
                    im = im.convert("RGB")
                    im.thumbnail((1600, 1600))
                    im.save(path, "JPEG", quality=90)
            except Exception as e:
                log.warning("Маркет: не картинка %s: %s", u, e)
                continue
            out.append(MediaFile(kind="photo", path=str(path)))
        return out


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
