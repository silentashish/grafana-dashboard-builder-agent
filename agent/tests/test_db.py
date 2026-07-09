from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DEBUGPY_ENABLED", "false")
os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")

from db import save_conversation_history


class FakeConnection:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement) -> None:
        self.statements.append(statement)


class FakeBegin:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeEngine:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def begin(self) -> FakeBegin:
        return FakeBegin(self.connection)


class ConversationHistoryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_conversation_history_creates_parent_thread_before_upsert(self) -> None:
        engine = FakeEngine()

        with (
            patch("db.get_async_engine", return_value=engine),
            patch("db.ensure_thread_exists", new_callable=AsyncMock) as ensure_thread_exists,
        ):
            await save_conversation_history("thread-1", [{"role": "user", "content": "hello"}])

        ensure_thread_exists.assert_awaited_once_with("thread-1")
        self.assertEqual(len(engine.connection.statements), 1)


if __name__ == "__main__":
    unittest.main()
