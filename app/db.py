"""SQLite-хранилище: найденные материалы, их статусы и состояние источников.

Жизненный цикл item.status:
  new -> draft (переписан моделью, ждёт отправки модератору)
      -> pending (у модератора) -> approved (в очереди)
      -> scheduled (в «Отложенных» канала) -> published
      -> rejected (не рецепт / отклонён модератором) | failed (ошибка)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from app.models import MediaFile, RawPost

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT NOT NULL,
    source        TEXT NOT NULL,
    source_ref    TEXT NOT NULL,
    url           TEXT,
    text_hash     TEXT,
    views         INTEGER,
    raw_text      TEXT NOT NULL,
    media         TEXT NOT NULL DEFAULT '[]',
    caption       TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    note          TEXT,
    draft_msg_ids TEXT,
    created_at    TEXT NOT NULL,
    approved_at   TEXT,
    published_at  TEXT,
    UNIQUE (channel, source, source_ref)
);
CREATE INDEX IF NOT EXISTS ix_items_status ON items (channel, status);
CREATE INDEX IF NOT EXISTS ix_items_hash ON items (channel, text_hash);

CREATE TABLE IF NOT EXISTS flags (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_state (
    channel TEXT NOT NULL,
    source  TEXT NOT NULL,
    last_id INTEGER NOT NULL,
    PRIMARY KEY (channel, source)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        # Миграции: колонки для поиска дублей
        cur = await self.conn.execute("PRAGMA table_info(items)")
        cols = {r[1] for r in await cur.fetchall()}
        for col in ("phash", "title_key", "ingr_key", "scheduled_at", "tg_msg_ids", "links",
                    "max_status", "max_at", "max_mid", "max_note"):
            if col not in cols:
                await self.conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---------- items ----------

    async def is_known(self, channel: str, post: RawPost) -> bool:
        """Уже видели этот пост или такой же текст из другого источника."""
        cur = await self.conn.execute(
            "SELECT 1 FROM items WHERE channel=? AND ((source=? AND source_ref=?) OR (text_hash IS NOT NULL AND text_hash=?)) LIMIT 1",
            (channel, post.source, post.source_ref, post.text_hash if post.text.strip() else None),
        )
        return await cur.fetchone() is not None

    async def add_item(self, channel: str, post: RawPost) -> int | None:
        cur = await self.conn.execute(
            """INSERT OR IGNORE INTO items (channel, source, source_ref, url, text_hash, views, raw_text, media, links, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                channel, post.source, post.source_ref, post.url,
                post.text_hash if post.text.strip() else None, post.views, post.text,
                json.dumps([m.to_dict() for m in post.media], ensure_ascii=False),
                json.dumps(post.links, ensure_ascii=False), _now(),
            ),
        )
        await self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    async def get(self, item_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,))
        row = await cur.fetchone()
        return _row(row) if row else None

    async def by_status(self, channel: str, status: str, limit: int = 50, order: str = "id") -> list[dict]:
        cur = await self.conn.execute(
            f"SELECT * FROM items WHERE channel=? AND status=? ORDER BY {order} LIMIT ?",
            (channel, status, limit),
        )
        return [_row(r) for r in await cur.fetchall()]

    async def count(self, channel: str) -> dict[str, int]:
        cur = await self.conn.execute(
            "SELECT status, COUNT(*) c FROM items WHERE channel=? GROUP BY status", (channel,)
        )
        return {r["status"]: r["c"] for r in await cur.fetchall()}

    async def update(self, item_id: int, **fields: Any) -> None:
        if "media" in fields and not isinstance(fields["media"], str):
            fields["media"] = json.dumps([m.to_dict() for m in fields["media"]], ensure_ascii=False)
        if "tg_msg_ids" in fields and not isinstance(fields["tg_msg_ids"], str):
            fields["tg_msg_ids"] = json.dumps(fields["tg_msg_ids"])
        if "draft_msg_ids" in fields and not isinstance(fields["draft_msg_ids"], str):
            fields["draft_msg_ids"] = json.dumps(fields["draft_msg_ids"])
        if fields.get("status") == "approved":
            fields.setdefault("approved_at", _now())
        if fields.get("status") == "published":
            fields.setdefault("published_at", _now())
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE items SET {cols} WHERE id=?", (*fields.values(), item_id))
        await self.conn.commit()

    async def dedup_pool(self, channel: str, item_id: int, days: int) -> list[dict]:
        """С чем сравнивать материал на дубли: всё, что уже прошло обработку (кроме самих дублей и ошибок),
        плюс ещё не обработанные с меньшим id — так из пары дублей выживает ровно один."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        cur = await self.conn.execute(
            """SELECT id, status, phash, title_key, ingr_key FROM items
               WHERE channel=? AND id<>? AND created_at>=? AND (
                     status IN ('draft','pending','approved','scheduled','published')
                  OR (status='rejected' AND (note IS NULL OR note NOT LIKE 'дубл%'))
                  OR (status='new' AND id<?))""",
            (channel, item_id, since, item_id),
        )
        return [dict(r) for r in await cur.fetchall()]

    # ---------- флаги (пауза и т.п.) ----------

    async def get_flag(self, key: str) -> str | None:
        cur = await self.conn.execute("SELECT value FROM flags WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set_flag(self, key: str, value: str | None) -> None:
        if value is None:
            await self.conn.execute("DELETE FROM flags WHERE key=?", (key,))
        else:
            await self.conn.execute("INSERT INTO flags (key, value) VALUES (?, ?) "
                                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await self.conn.commit()

    # ---------- source state ----------

    async def get_last_id(self, channel: str, source: str) -> int | None:
        cur = await self.conn.execute(
            "SELECT last_id FROM source_state WHERE channel=? AND source=?", (channel, source)
        )
        row = await cur.fetchone()
        return row["last_id"] if row else None

    async def set_last_id(self, channel: str, source: str, last_id: int) -> None:
        await self.conn.execute(
            "INSERT INTO source_state (channel, source, last_id) VALUES (?, ?, ?) "
            "ON CONFLICT(channel, source) DO UPDATE SET last_id=excluded.last_id",
            (channel, source, last_id),
        )
        await self.conn.commit()


def _row(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["media"] = [MediaFile.from_dict(m) for m in json.loads(d["media"] or "[]")]
    d["draft_msg_ids"] = json.loads(d["draft_msg_ids"]) if d["draft_msg_ids"] else []
    d["tg_msg_ids"] = json.loads(d["tg_msg_ids"]) if d.get("tg_msg_ids") else []
    d["links"] = json.loads(d["links"]) if d.get("links") else []
    return d
