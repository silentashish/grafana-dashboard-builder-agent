"""Database configuration and conversation history persistence helpers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    MetaData,
    Table,
    Column,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    Integer,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def get_database_url() -> str:
    """Return the app runtime database URL."""
    return os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://chainlit:chainlit@postgres:5432/chainlit",
    )


def get_alembic_database_url() -> str:
    """Return a sync URL suitable for Alembic."""
    configured = os.getenv("ALEMBIC_DATABASE_URL")
    if configured:
        return configured

    database_url = get_database_url()
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


metadata = MetaData()

users_table = Table(
    "users",
    metadata,
    Column("id", Text, primary_key=True),
    Column("identifier", Text, nullable=False, unique=True),
    Column("metadata", JSONB, nullable=False),
    Column("createdAt", Text),
)

threads_table = Table(
    "threads",
    metadata,
    Column("id", Text, primary_key=True),
    Column("createdAt", Text),
    Column("name", Text),
    Column("userId", Text),
    Column("userIdentifier", Text),
    Column("tags", ARRAY(Text)),
    Column("metadata", JSONB),
)

steps_table = Table(
    "steps",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("threadId", Text, nullable=False),
    Column("parentId", Text),
    Column("streaming", Boolean, nullable=False),
    Column("waitForAnswer", Boolean),
    Column("isError", Boolean),
    Column("metadata", JSONB),
    Column("tags", ARRAY(Text)),
    Column("input", Text),
    Column("output", Text),
    Column("createdAt", Text),
    Column("command", Text),
    Column("start", Text),
    Column("end", Text),
    Column("generation", JSONB),
    Column("showInput", Text),
    Column("language", Text),
    Column("indent", Integer),
    Column("defaultOpen", Boolean),
    Column("autoCollapse", Boolean),
    Column("disableFeedback", Boolean),
    Column("modes", JSONB),
)

elements_table = Table(
    "elements",
    metadata,
    Column("id", Text, primary_key=True),
    Column("threadId", Text),
    Column("type", Text),
    Column("url", Text),
    Column("chainlitKey", Text),
    Column("name", Text, nullable=False),
    Column("display", Text),
    Column("objectKey", Text),
    Column("size", Text),
    Column("page", Integer),
    Column("language", Text),
    Column("forId", Text),
    Column("mime", Text),
    Column("props", JSONB),
)

feedbacks_table = Table(
    "feedbacks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("forId", Text, nullable=False),
    Column("threadId", Text, nullable=False),
    Column("value", Integer, nullable=False),
    Column("comment", Text),
)

conversation_contexts_table = Table(
    "conversation_contexts",
    metadata,
    Column("thread_id", Text, ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True),
    Column("conversation_history", JSONB, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


_async_engine: AsyncEngine | None = None


def get_async_engine() -> AsyncEngine:
    """Create a shared async engine for app-specific queries."""
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(get_database_url(), future=True)
    return _async_engine


def normalize_conversation_history(history: Any) -> list[Any]:
    """Validate and normalize the stored conversation history payload."""
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except json.JSONDecodeError:
            return []

    if not isinstance(history, list):
        return []

    return history


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def ensure_thread_exists(thread_id: str | None, *, source: str = "assistant-api") -> None:
    """Create the parent Chainlit thread row needed by conversation_contexts."""
    if not thread_id:
        return

    statement = pg_insert(threads_table).values(
        id=thread_id,
        createdAt=_utc_now_iso(),
        name="Grafana Assistant",
        userIdentifier=os.getenv("ASSISTANT_THREAD_USER_IDENTIFIER", "grafana-assistant"),
        tags=["grafana", "assistant"],
        metadata={"source": source},
    )
    statement = statement.on_conflict_do_nothing(index_elements=[threads_table.c.id])

    engine = get_async_engine()
    async with engine.begin() as connection:
        await connection.execute(statement)


async def load_conversation_history(thread_id: str | None) -> list[Any]:
    """Load the stored conversation history for a thread."""
    if not thread_id:
        return []

    engine = get_async_engine()
    async with engine.begin() as connection:
        result = await connection.execute(
            select(conversation_contexts_table.c.conversation_history).where(
                conversation_contexts_table.c.thread_id == thread_id
            )
        )
        row = result.first()

    if not row:
        return []

    return normalize_conversation_history(row[0])


async def save_conversation_history(
    thread_id: str | None, conversation_history: list[Any]
) -> None:
    """Persist the conversation history for a thread."""
    if not thread_id:
        return

    normalized = normalize_conversation_history(conversation_history)
    engine = get_async_engine()
    statement = pg_insert(conversation_contexts_table).values(
        thread_id=thread_id,
        conversation_history=normalized,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[conversation_contexts_table.c.thread_id],
        set_={
            "conversation_history": normalized,
            "updated_at": func.now(),
        },
    )

    await ensure_thread_exists(thread_id)
    async with engine.begin() as connection:
        await connection.execute(statement)
