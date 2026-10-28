"""Full-text search over documents (F09). FTS5 specifics stay in this module."""

import json
import re
from typing import Any, Protocol

from pydantic import BaseModel
from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent.db.schema import documents


class SearchHit(BaseModel):
    id: str
    source: str
    title: str
    snippet: str
    score: float
    meta: dict[str, Any] = {}


class SearchIndex(Protocol):
    async def upsert(
        self, doc_id: str, source: str, title: str, body: str, meta_json: str = "{}"
    ) -> None: ...
    async def remove(self, doc_id: str) -> None: ...
    async def search(
        self, query: str, limit: int = 10, source: str | None = None, any_word: bool = False
    ) -> list[SearchHit]: ...


def _fts_query(query: str, any_word: bool = False) -> str:
    """Turn free text into a safe FTS5 query: each word quoted; all words required, or any with any_word."""
    words = list(dict.fromkeys(re.findall(r"\w+", query)))
    return (" OR " if any_word else " ").join(f'"{w}"' for w in words)


class Fts5SearchIndex:
    """documents plus the documents_fts virtual table, linked by an unindexed doc_id column."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert(self, doc_id: str, source: str, title: str, body: str, meta_json: str = "{}") -> None:
        async with self._engine.begin() as conn:
            await self._remove(conn, doc_id)
            await conn.execute(
                insert(documents).values(
                    id=doc_id, source=source, title=title, body=body, meta_json=meta_json
                )
            )
            await conn.execute(
                text("INSERT INTO documents_fts(doc_id, title, body) VALUES (:id, :title, :body)"),
                {"id": doc_id, "title": title, "body": body},
            )

    async def remove(self, doc_id: str) -> None:
        async with self._engine.begin() as conn:
            await self._remove(conn, doc_id)

    @staticmethod
    async def _remove(conn: AsyncConnection, doc_id: str) -> None:
        await conn.execute(
            text("DELETE FROM documents_fts WHERE doc_id = :id"),
            {"id": doc_id},
        )
        await conn.execute(delete(documents).where(documents.c.id == doc_id))

    async def search(
        self, query: str, limit: int = 10, source: str | None = None, any_word: bool = False
    ) -> list[SearchHit]:
        match = _fts_query(query, any_word)
        if not match:
            return []
        sql = (
            "SELECT d.id, d.source, d.title, d.meta_json, "
            "snippet(documents_fts, 2, '[', ']', '…', 12) AS snippet, "
            "bm25(documents_fts, 0.0, 5.0, 1.0) AS score "
            "FROM documents_fts JOIN documents d ON d.id = documents_fts.doc_id "
            "WHERE documents_fts MATCH :match"
        )
        params: dict[str, object] = {"match": match, "limit": limit}
        if source is not None:
            sql += " AND d.source = :source"
            params["source"] = source
        sql += " ORDER BY score LIMIT :limit"
        async with self._engine.connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        # bm25 is lower-is-better; expose higher-is-better.
        return [
            SearchHit(
                id=r["id"],
                source=r["source"],
                title=r["title"],
                snippet=r["snippet"],
                score=-r["score"],
                meta=json.loads(r["meta_json"] or "{}"),
            )
            for r in rows
        ]

    async def get(self, doc_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(documents).where(documents.c.id == doc_id))).mappings().first()
        return dict(row) if row else None

    async def by_source(self, source: str) -> list[dict[str, Any]]:
        """id and meta of every document of one kind (no bodies)."""
        query = select(documents.c.id, documents.c.meta_json).where(documents.c.source == source)
        async with self._engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(query)).mappings().all()]
