"""FastAPI bridge for the Grafana Assistant plugin."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from runtime import AssistantRuntime, configure_runtime


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    thread_id: str | None = None


class ThreadState(BaseModel):
    thread_id: str
    model: str
    provider: str
    history_length: int


def _allowed_origins() -> list[str]:
    configured = os.getenv("ASSISTANT_ALLOWED_ORIGINS", "*")
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    return origins or ["*"]


def _expected_api_key() -> str:
    return os.getenv("ASSISTANT_API_KEY", "").strip()


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_assistant_api_key: str | None = Header(default=None),
) -> None:
    expected = _expected_api_key()
    if not expected:
        return

    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    elif x_assistant_api_key:
        supplied = x_assistant_api_key.strip()

    if supplied != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="assistant API key is invalid or missing",
        )


def _websocket_authorized(websocket: WebSocket) -> bool:
    expected = _expected_api_key()
    if not expected:
        return True

    authorization = websocket.headers.get("authorization", "")
    if authorization.lower().startswith("bearer ") and authorization[7:].strip() == expected:
        return True

    token = websocket.query_params.get("token", "")
    return token == expected


def _thread_id(value: str | None = None) -> str:
    return value or str(uuid.uuid4())


def _chunk_text(text: str, chunk_size: int = 96) -> list[str]:
    if not text:
        return []
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


runtime = AssistantRuntime()
app = FastAPI(title="ECLSS Assistant API", version="1.0.0")
allowed_origins = _allowed_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=allowed_origins != ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Assistant-Api-Key", "X-Grafana-User"],
)
configure_runtime(app=app)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/assistant/threads/{thread_id}", response_model=ThreadState)
async def get_thread(
    thread_id: str,
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    return await runtime.get_thread_state(thread_id)


@app.post("/api/assistant/chat")
async def chat(
    request: ChatRequest,
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    return await runtime.send_message(_thread_id(request.thread_id), request.message.strip())


@app.websocket("/ws/assistant")
async def assistant_socket(websocket: WebSocket) -> None:
    if not _websocket_authorized(websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    thread_id = _thread_id(websocket.query_params.get("thread_id"))
    await websocket.accept()
    await websocket.send_json({"type": "ready", "thread_id": thread_id})

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "content": "Invalid JSON message."})
                continue

            if payload.get("type") != "message":
                await websocket.send_json(
                    {"type": "error", "content": f"Unsupported message type: {payload.get('type')}"}
                )
                continue

            content = str(payload.get("content", "")).strip()
            if not content:
                await websocket.send_json({"type": "error", "content": "Message content is required."})
                continue

            requested_thread_id = str(payload.get("thread_id") or thread_id)
            await websocket.send_json({"type": "ack", "thread_id": requested_thread_id})
            await websocket.send_json(
                {
                    "type": "thinking",
                    "content": "Reading thread context and preparing the model request.",
                }
            )

            try:
                await websocket.send_json(
                    {
                        "type": "thinking",
                        "content": "Running the assistant model and any required tools.",
                    }
                )
                response = await runtime.send_message(requested_thread_id, content)
            except Exception as exc:
                await websocket.send_json({"type": "error", "content": str(exc)})
                continue

            for thinking_step in response.get("thinking", []):
                await websocket.send_json({"type": "thinking", "content": thinking_step})

            for tool_call in response.get("tool_calls", []):
                await websocket.send_json({"type": "tool_call", **tool_call})

            for chunk in _chunk_text(response["content"]):
                await websocket.send_json({"type": "token", "content": chunk})

            await websocket.send_json(
                {
                    "type": "done",
                    "thread_id": requested_thread_id,
                    "conversation_history_length": response["conversation_history_length"],
                    "model": response["model"],
                    "provider": response["provider"],
                }
            )
    except WebSocketDisconnect:
        return
