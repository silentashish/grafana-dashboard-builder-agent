from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DEBUGPY_ENABLED", "false")
os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")

from fastapi.testclient import TestClient

from api import app


class AssistantApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_chat_requires_api_key_when_configured(self) -> None:
        with patch.dict(os.environ, {"ASSISTANT_API_KEY": "secret"}):
            response = self.client.post(
                "/api/assistant/chat",
                json={"thread_id": "thread-1", "message": "hello"},
            )

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
