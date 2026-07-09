"""Headless assistant runtime shared by Chainlit and the API server."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from agent_graph import PydanticGraphAgent
from db import (
    load_conversation_history,
    normalize_conversation_history,
    save_conversation_history,
)
from observability import configure_observability, install_instrumentors


_RUNTIME_CONFIGURED = False


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def setup_debugger() -> None:
    """Enable remote debugging when requested via env vars."""
    if not _is_enabled(os.getenv("DEBUGPY_ENABLED", "false")):
        return

    try:
        import debugpy
    except ImportError:
        print("DEBUGPY_ENABLED=true but debugpy is not installed.")
        return

    host = os.getenv("DEBUGPY_HOST", "0.0.0.0")
    port = int(os.getenv("DEBUGPY_PORT", "5678"))

    try:
        debugpy.listen((host, port))
        print(f"debugpy listening on {host}:{port}")
    except RuntimeError as exc:
        normalized = str(exc).lower()
        if "already listening" not in normalized and "address already in use" not in normalized:
            raise
        print(f"debugpy listener skipped on {host}:{port}: {exc}")

    if _is_enabled(os.getenv("DEBUGPY_WAIT_FOR_CLIENT", "false")):
        print("Waiting for debugger client to attach...")
        debugpy.wait_for_client()
        print("Debugger attached.")


def configure_runtime(service_name: str = "eclss-assistant-api", app: Any | None = None) -> None:
    """Configure process-wide diagnostics once."""
    global _RUNTIME_CONFIGURED
    if _RUNTIME_CONFIGURED:
        return

    setup_debugger()
    configure_observability(service_name=service_name)
    install_instrumentors(app)
    _RUNTIME_CONFIGURED = True


def create_agent() -> PydanticGraphAgent:
    """Create and configure the graph-backed agent for a conversation."""
    model_name = os.getenv("MODEL_NAME", "kimik2.6")
    model_provider = os.getenv("MODEL_PROVIDER", "ollama")
    provider_base_url = os.getenv("MODEL_BASE_URL") or os.getenv("OLLAMA_BASE_URL")

    return PydanticGraphAgent(
        model_name=model_name,
        provider_name=model_provider,
        provider_base_url=provider_base_url,
        workspace_root=os.getcwd(),
    )


def assistant_greeting(user_identifier: str, agent: PydanticGraphAgent) -> str:
    """Return the assistant greeting used by UI adapters."""
    return (
        f"Hello {user_identifier}! I'm an AI assistant powered by **{agent.model}** "
        f"through **{agent.provider_name}** using **PydanticAI + LangGraph**.\n\n"
        "This app stores thread history in PostgreSQL and restores the exact model context when you reopen a chat.\n\n"
        "I can help you with:\n"
        "- **OpenSearch discovery**: inspect indices, mappings, and documents\n"
        "- **Grafana operations**: search, create, and update dashboards\n"
        "- **Dashboard building**: use OpenSearch data to design Grafana panels\n\n"
        "What would you like to do?"
    )


def build_thinking_summary(
    *,
    history_length: int,
    tool_calls: list[dict[str, Any]],
    model: str,
    provider: str,
) -> list[str]:
    """Return a safe, high-level progress summary for UI display."""
    steps = [
        (
            f"Loaded {history_length} prior message(s) from this thread."
            if history_length
            else "Started from an empty thread context."
        ),
        f"Ran the request through {provider} / {model}.",
    ]

    normalized_tool_calls = [call for call in tool_calls if isinstance(call, dict)]

    if normalized_tool_calls:
        tool_names = ", ".join(str(call.get("tool", "tool")) for call in normalized_tool_calls[:4])
        suffix = "" if len(normalized_tool_calls) <= 4 else f", and {len(normalized_tool_calls) - 4} more"
        steps.append(f"Used {len(normalized_tool_calls)} tool call(s): {tool_names}{suffix}.")
    else:
        steps.append("No external tool calls were needed.")

    steps.append("Prepared the final response for the Grafana chat UI.")
    return steps


class AssistantRuntime:
    """Thread-aware adapter around the existing PydanticGraphAgent contract."""

    def __init__(self) -> None:
        self._agents: dict[str, PydanticGraphAgent] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._agents_lock = asyncio.Lock()

    async def get_agent(self, thread_id: str) -> PydanticGraphAgent:
        async with self._agents_lock:
            agent = self._agents.get(thread_id)
            if agent is None:
                agent = create_agent()
                self._agents[thread_id] = agent
            return agent

    async def get_lock(self, thread_id: str) -> asyncio.Lock:
        async with self._agents_lock:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[thread_id] = lock
            return lock

    async def get_thread_state(self, thread_id: str) -> dict[str, Any]:
        agent = await self.get_agent(thread_id)
        history = await load_conversation_history(thread_id)
        return {
            "thread_id": thread_id,
            "model": agent.model,
            "provider": agent.provider_name,
            "history_length": len(history),
        }

    async def send_message(self, thread_id: str, message: str) -> dict[str, Any]:
        """Run one assistant turn and persist updated model history."""
        lock = await self.get_lock(thread_id)
        async with lock:
            agent = await self.get_agent(thread_id)
            conversation_history = normalize_conversation_history(
                await load_conversation_history(thread_id)
            )
            response = await agent.send_message(
                message=message,
                conversation_history=conversation_history,
            )
            updated_history = normalize_conversation_history(
                response.get("conversation_history")
            )
            raw_tool_calls = response.get("tool_calls", [])
            tool_calls = raw_tool_calls if isinstance(raw_tool_calls, list) else []
            await save_conversation_history(thread_id, updated_history)
            return {
                "thread_id": thread_id,
                "model": agent.model,
                "provider": agent.provider_name,
                "content": response["content"],
                "tool_calls": tool_calls,
                "thinking": build_thinking_summary(
                    history_length=len(conversation_history),
                    tool_calls=tool_calls,
                    model=agent.model,
                    provider=agent.provider_name,
                ),
                "conversation_history_length": len(updated_history),
            }
