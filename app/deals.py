"""Тип контента «находки»: товары с маркетплейсов (WB, Ozon, AliExpress, Яндекс Маркет).

Модель получает текст поста-источника, фото и пронумерованные ссылки на товары
(уже раскрытые и очищенные: [L1], [L2]…), а возвращает структуру: подходит ли пост,
название, интро и список товаров с ценами и номером ссылки. Подпись собирает код —
с вашими партнёрскими ссылками и рекламной маркировкой из профиля канала.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.config import ChannelProfile
from app.links import ProductLink, affiliate_url

CAPTION_LIMIT = 1024


class DealItem(BaseModel):
    name: str = Field(description="Короткое понятное название товара (3–8 слов), без артикулов и эмодзи")
    price: str | None = Field(description="Цена сейчас, например «1 299 ₽»; null, если неизвестна")
    old_price: str | None = Field(description="Старая цена до скидки, например «2 499 ₽»; null, если неизвестна")
    link: str | None = Field(description="Номер ссылки на этот товар из списка, например «L1»; null, если ссылки нет")


class DealPost(BaseModel):
    is_deal: bool = Field(description="true, если это конкретный товар(ы) с маркетплейса, который можно купить по ссылке")
    reject_reason: str | None = Field(description="Почему не подходит (реклама сервиса, курс, розыгрыш, нет товара/ссылки); null, если подходит")
    title: str = Field(description="Заголовок: что это и почему выгодно, 3–8 слов, без эмодзи; пусто, если не подходит")
    emoji: str = Field(description="Одно эмодзи по теме товара")
    intro: str = Field(description="1–2 коротких живых предложения: чем полезен товар/почему стоит взять; без цены")
    items: list[DealItem] = Field(description="Товары из поста (обычно 1, в подборке до 5)")
    promo_code: str | None = Field(description="Промокод, если он есть в посте; иначе null")
    rubric: str | None = Field(description="Рубрика из списка рубрик канала (одно слово без #)")

    @property
    def accepted(self) -> bool:
        return self.is_deal


SYSTEM_TEMPLATE = """Ты — редактор Telegram-канала «{title}» о выгодных находках на маркетплейсах.
Тебе присылают пост из другого канала. Сделай из него пост для нашего канала.

Правила оформления:
{style}

Общие правила:
- Пиши на русском, своими словами, не копируй исходный текст.
- Убирай упоминания других каналов, авторов, их промокоды на подписку, призывы подписаться, хэштеги.
- Подходит только пост о конкретном товаре (или подборке товаров) с WB, Ozon, AliExpress или Яндекс Маркета, на который есть ссылка из списка.
  Реклама сервисов, банков, курсов, VPN, розыгрыши, новости, подборки без ссылок — is_deal=false.
- Цены бери только из поста. Не выдумывай цены, скидки и характеристики.
- Не обещай лечебный эффект и не пересказывай медицинские истории из отзывов. Если товар — БАД или витамины,
  закончи intro фразой «БАД, не является лекарственным средством».
- В items.link укажи номер ссылки из списка ([L1], [L2]…), которая ведёт на этот товар. Если товар один — обычно это L1.
- rubric выбери из списка: {rubrics}.

Примеры тона нашего канала:
{examples}
"""


def build_prompt(text: str, links: list[ProductLink], note: str | None) -> str:
    lines = [f"[L{n}] {l.name}: {l.url}" for n, l in enumerate(links, 1)] or ["(ссылок на товары нет)"]
    prompt = (f"Исходный пост:\n<source>\n{text.strip() or '(текста нет, только фото)'}\n</source>\n\n"
              "Ссылки на товары из поста:\n" + "\n".join(lines))
    if note:
        prompt += f"\n\nДополнительное указание редактора: {note}"
    return prompt


def system_prompt(profile: ChannelProfile) -> str:
    return SYSTEM_TEMPLATE.format(
        title=profile.title, style=profile.style.strip(),
        rubrics=", ".join(profile.rubrics) or "(нет)",
        examples="\n\n---\n\n".join(e.strip() for e in profile.examples) or "(нет)",
    )


def _e(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _discount(price: str | None, old: str | None) -> str:
    def num(s):
        d = re.sub(r"[^\d]", "", s or "")
        return int(d) if d else 0
    p, o = num(price), num(old)
    return f" (−{round((1 - p / o) * 100)}%)" if p and o > p else ""


def format_deal(post: DealPost, links: list[ProductLink], profile: ChannelProfile,
                own_links: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Возвращает (HTML-подпись, предупреждения для модератора).
    own_links: {ключ товара: ссылка} — ваши готовые ссылки (например, реферальные с Маркета), ставятся как есть."""
    own_links = own_links or {}
    warnings: list[str] = []
    parts = [f"<b>{_e(post.title.strip())}</b> {_e(post.emoji.strip())}".strip()]
    if post.intro.strip():
        parts[0] += "\n" + _e(post.intro.strip())

    blocks = []
    for item in post.items[:5]:
        link = _pick_link(item.link, links)
        line = f"🔥 {_e(item.name.strip())}"
        if item.price:
            line += f" — <b>{_e(item.price)}</b>"
            if item.old_price:
                line += f" <s>{_e(item.old_price)}</s>{_discount(item.price, item.old_price)}"
        if link:
            url = own_links.get(link.key or "") or affiliate_url(link, profile.affiliate)
            if not url:
                url = link.url
                if not link.product_id:
                    warnings.append(f"ссылку {link.name} не удалось раскрыть — замените вручную: {link.url}")
                else:
                    warnings.append(f"партнёрская ссылка для {link.name} не настроена — стоит чистая ссылка")
            line += f'\n🛒 <a href="{_e(url)}">Смотреть на {link.name}</a>'
        else:
            warnings.append(f"у товара «{item.name}» нет ссылки")
        blocks.append(line)
    if blocks:
        parts.append("\n\n".join(blocks))
    if post.promo_code:
        parts.append(f"🎟 Промокод: <code>{_e(post.promo_code)}</code>")
    tail = []
    if post.rubric:
        tag = re.sub(r"[^\w]", "", post.rubric.lower().replace(" ", "_"))
        if tag:
            tail.append(f"#{tag}")
    if profile.ad_footer:
        tail.append(_e(profile.ad_footer))
    if tail:
        parts.append("\n".join(tail))
    return "\n\n".join(parts), list(dict.fromkeys(warnings))


def _pick_link(ref: str | None, links: list[ProductLink]) -> ProductLink | None:
    if ref and (m := re.search(r"\d+", ref)):
        n = int(m.group(0))
        if 1 <= n <= len(links):
            return links[n - 1]
    return links[0] if len(links) == 1 else None
