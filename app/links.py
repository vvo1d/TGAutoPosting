"""Ссылки на товары: раскрываем сокращалки и чужие партнёрские редиректы,
приводим к чистой ссылке на товар и подставляем свою партнёрскую.

Поддержаны: Wildberries, Ozon, AliExpress, Яндекс Маркет.
Ozon закрыт защитой от ботов: короткие ozon.ru/p/... и ozon.ru/t/... раскрыть нельзя —
такие ссылки помечаются, и модератор подставляет ссылку вручную.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

MARKETPLACES = {
    "wb": "Wildberries",
    "ozon": "Ozon",
    "ali": "AliExpress",
    "ym": "Яндекс Маркет",
}

# Ссылки, которые точно не товар: соцсети, сам Telegram, мессенджеры
SKIP_HOSTS = re.compile(r"(^|\.)(t\.me|telegram\.me|max\.ru|vk\.com|vk\.ru|youtube\.com|youtu\.be|instagram\.com|"
                        r"dzen\.ru|tiktok\.com|apps\.apple\.com|play\.google\.com)$", re.I)

_CANON = [
    # (маркетплейс, regex по итоговому url, шаблон чистой ссылки)
    ("wb", re.compile(r"wildberries\.(?:ru|by|kz|am)/catalog/(\d{5,})"), "https://www.wildberries.ru/catalog/{id}/detail.aspx"),
    ("wb", re.compile(r"wb\.ru/catalog/(\d{5,})"), "https://www.wildberries.ru/catalog/{id}/detail.aspx"),
    ("ozon", re.compile(r"ozon\.(?:ru|by|kz)/product/(?:[a-z0-9\-]*-)?(\d{5,})"), "https://www.ozon.ru/product/{id}/"),
    ("ali", re.compile(r"aliexpress\.(?:ru|com|us)/item/(\d{8,})"), "https://aliexpress.ru/item/{id}.html"),
    ("ym", re.compile(r"market\.yandex\.ru/((?:card/[^/?#]+|product--[^/?#]+|product)/(\d{5,}))"), "https://market.yandex.ru/{path}"),
]
_UNRESOLVABLE = [("ozon", re.compile(r"ozon\.(?:ru|by|kz)/(?:p|t)/[\w-]+"))]


@dataclass
class ProductLink:
    marketplace: str            # wb | ozon | ali | ym
    product_id: str | None      # None — ссылку не удалось раскрыть
    url: str                    # чистая ссылка на товар (или исходная, если не раскрылась)
    source_url: str

    @property
    def key(self) -> str | None:
        return f"{self.marketplace}:{self.product_id}" if self.product_id else None

    @property
    def name(self) -> str:
        return MARKETPLACES[self.marketplace]


def canonical(url: str) -> ProductLink | None:
    for mp, rx, tpl in _CANON:
        if m := rx.search(url):
            pid = m.groups()[-1]  # id — последняя группа; у Маркета первая группа — путь карточки
            return ProductLink(mp, pid, tpl.format(id=pid, path=m.group(1)), url)
    for mp, rx in _UNRESOLVABLE:
        if rx.search(url):
            return ProductLink(mp, None, url.split("?")[0], url)
    return None


class LinkResolver:
    def __init__(self):
        self.http = httpx.AsyncClient(headers={"User-Agent": UA, "Accept-Language": "ru"}, timeout=15,
                                      follow_redirects=False)

    async def stop(self) -> None:
        await self.http.aclose()

    async def resolve_all(self, urls: list[str]) -> list[ProductLink]:
        out: list[ProductLink] = []
        seen: set[str] = set()
        results = await asyncio.gather(*(self.resolve(u) for u in dict.fromkeys(urls)), return_exceptions=True)
        for r in results:
            if isinstance(r, ProductLink):
                k = r.key or r.url
                if k not in seen:
                    seen.add(k)
                    out.append(r)
        return out

    async def resolve(self, url: str) -> ProductLink | None:
        """Идём по редиректам (в т.ч. meta refresh / js), пока не узнаем товар."""
        host = httpx.URL(url).host or ""
        if SKIP_HOSTS.search(host):
            return None
        current = url
        for _ in range(10):
            if (pl := canonical(current)) and pl.product_id:
                return ProductLink(pl.marketplace, pl.product_id, pl.url, url)
            try:
                r = await self.http.get(current)
            except httpx.HTTPError as e:
                log.debug("resolve %s: %s", current, e)
                break
            loc = r.headers.get("location")
            if r.is_redirect and loc:
                current = str(httpx.URL(current).join(loc))
                continue
            m = re.search(r"""(?:http-equiv=["']refresh["'][^>]*url=|window\.location(?:\.href)?\s*=\s*["'])([^"'>\s]+)""",
                          r.text[:5000], re.I)
            if m:
                current = str(httpx.URL(current).join(m.group(1)))
                continue
            break
        pl = canonical(current) or canonical(url)
        return ProductLink(pl.marketplace, pl.product_id, pl.url, url) if pl else None


def affiliate_url(link: ProductLink, templates: dict[str, str | None]) -> str | None:
    """Своя партнёрская ссылка по шаблону из профиля канала. None — шаблон для площадки не задан.

    В шаблоне: {url} — чистая ссылка, {url_encoded} — она же для подстановки в параметр, {id} — id товара.
    """
    tpl = templates.get(link.marketplace)
    if not tpl or not link.product_id:
        return None
    return tpl.format(url=link.url, url_encoded=quote(link.url, safe=""), id=link.product_id)


ARTICLE = re.compile(r"(?:артикул\w*|арт\.?)\s*(?:на\s*)?(?:wb|вб)?\s*[:№#]?\s*(\d{6,12})", re.I)


def wb_articles(text: str) -> list[str]:
    """Каналы WB часто пишут артикул текстом вместо ссылки."""
    return [f"https://www.wildberries.ru/catalog/{a}/detail.aspx" for a in ARTICLE.findall(text)]
