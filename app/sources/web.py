"""Сайты с рецептами: RSS-лента -> страница рецепта -> разметка schema.org/Recipe (JSON-LD).

Большинство крупных кулинарных сайтов отдают рецепт в JSON-LD (так их понимает поиск),
поэтому один универсальный парсер покрывает многие сайты без отдельной вёрстки под каждый.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from app.config import WebSource
from app.models import MediaFile, RawPost

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RecipeCollector/1.0)"}


class WebReader:
    def __init__(self, tg_proxy: str | None = None):
        self.http = httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True)
        # telegra.ph — сервис Telegram: ходим к нему через тот же прокси, что и к Telegram
        self.tg_http = httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True,
                                         proxy=tg_proxy.replace("socks5h://", "socks5://") if tg_proxy else None)

    async def stop(self) -> None:
        await self.http.aclose()
        await self.tg_http.aclose()

    async def list_urls(self, src: WebSource) -> list[str]:
        urls = list(src.urls)
        if src.rss:
            resp = await self.http.get(src.rss)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            urls += [str(httpx.URL(src.rss).join(e.link)) for e in feed.entries[: src.max_entries] if getattr(e, "link", None)]
        for page in src.pages:
            resp = await self.http.get(page)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            pattern = re.compile(src.link_pattern) if src.link_pattern else None
            found: list[str] = []
            for a in soup.find_all("a", href=True):
                u = str(httpx.URL(page).join(a["href"])).split("#")[0]
                if (pattern.search(u) if pattern else True) and u not in found and u != page:
                    found.append(u)
            urls += found[: src.max_entries]
        return list(dict.fromkeys(urls))

    async def fetch_recipe(self, url: str) -> tuple[RawPost, list[str]] | None:
        """Возвращает пост без медиа и список url картинок."""
        resp = await self.http.get(url)
        resp.raise_for_status()
        recipe = _find_recipe(resp.text) or _find_recipe_microdata(resp.text)
        if not recipe:
            log.info("На %s нет разметки Recipe", url)
            return None

        name = _text(recipe.get("name"))
        ingredients = [_text(i) for i in _as_list(recipe.get("recipeIngredient"))]
        steps = _steps(recipe.get("recipeInstructions"))
        parts = [name, _text(recipe.get("description"))]
        if t := _text(recipe.get("totalTime")):
            parts.append(f"Время: {t}")
        if y := _text(recipe.get("recipeYield")):
            parts.append(f"Порций: {y}")
        parts.append("Ингредиенты:\n" + "\n".join(f"- {i}" for i in ingredients if i))
        if steps:
            parts.append("Приготовление:\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1)))
        text = "\n\n".join(p for p in parts if p)

        images = []
        for img in _as_list(recipe.get("image")):
            u = img.get("url") if isinstance(img, dict) else img
            if isinstance(u, str) and u:
                u = str(httpx.URL(url).join(u))
                if u not in images:
                    images.append(u)

        domain = urlparse(url).netloc.removeprefix("www.")
        return RawPost(source=f"web:{domain}", source_ref=url, text=text, url=url), images[:4]

    async def fetch_telegraph(self, url: str) -> str | None:
        """Текст статьи telegra.ph через открытый API (многие каналы прячут туда ингредиенты)."""
        m = re.search(r"telegra\.ph/([^/?#\s]+)", url)
        if not m:
            return None
        try:
            r = await self.tg_http.get(f"https://api.telegra.ph/getPage/{m.group(1)}", params={"return_content": "true"})
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("telegra.ph %s: %s", url, e)
            return None
        if not data.get("ok"):
            return None

        def node_text(n) -> str:
            if isinstance(n, str):
                return n
            inner = "".join(node_text(c) for c in n.get("children", []))
            return inner + ("\n" if n.get("tag") in ("p", "li", "h3", "h4", "br", "blockquote", "pre", "figure") else "")

        return "".join(node_text(n) for n in data["result"].get("content", [])).strip() or None

    async def download_images(self, urls: list[str], dest: Path) -> list[MediaFile]:
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        for n, u in enumerate(urls):
            try:
                r = await self.http.get(u)
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("Не скачалась картинка %s: %s", u, e)
                continue
            path = dest / f"img{n}"
            path.write_bytes(r.content)
            out.append(MediaFile(kind="photo", path=str(path)))
        return out


def _find_recipe(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(data):
            t = node.get("@type")
            if t == "Recipe" or (isinstance(t, list) and "Recipe" in t):
                return node
    return None


def _find_recipe_microdata(html: str) -> dict | None:
    """Разметка schema.org/Recipe через itemprop (Поварёнок, povar.ru, 1000.menu)."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find(attrs={"itemtype": re.compile(r"schema\.org/Recipe", re.I)})
    if not root:
        return None

    def props(name):
        return root.find_all(attrs={"itemprop": name})

    def val(el):
        if el.name == "meta":
            return el.get("content", "")
        if el.name in ("img", "source"):
            return el.get("src") or el.get("data-src") or el.get("content", "")
        if el.name in ("link", "a") and el.get("href"):
            return el.get("href")
        return el.get_text(" ", strip=True)

    name = next((val(e) for e in props("name") if val(e)), "")
    ingredients = [re.sub(r"\s+", " ", val(e)) for e in props("recipeIngredient") or props("ingredients")]
    steps_els = props("recipeInstructions")
    steps = []
    for e in steps_els:
        texts = [t.get_text(" ", strip=True) for t in e.find_all(attrs={"itemprop": "text"})]
        steps += texts or [val(e)]
    images = []
    for e in props("image"):
        v = val(e) if e.name != "div" else (e.find("img") or {}).get("src", "")
        if v and v not in images:
            images.append(v)
    if not name or not ingredients:
        return None
    return {
        "name": name,
        "description": next((val(e) for e in props("description")), ""),
        "recipeIngredient": [i for i in ingredients if i],
        "recipeInstructions": [s for s in steps if s],
        "totalTime": next((val(e) for e in props("totalTime")), None),
        "recipeYield": next((val(e) for e in props("recipeYield")), None),
        "image": images,
    }


def _walk(data):
    if isinstance(data, list):
        for x in data:
            yield from _walk(x)
    elif isinstance(data, dict):
        yield data
        if "@graph" in data:
            yield from _walk(data["@graph"])


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, dict):
        v = v.get("text") or v.get("name") or ""
    return BeautifulSoup(str(v), "html.parser").get_text(" ", strip=True)


def _steps(v) -> list[str]:
    out = []
    for s in _as_list(v):
        if isinstance(s, dict) and s.get("@type") == "HowToSection":
            out += _steps(s.get("itemListElement"))
        elif t := _text(s):
            out.append(t)
    return out
