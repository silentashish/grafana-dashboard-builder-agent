"""Helpers for wrapping LangGraph nodes in Logfire parent spans."""

from __future__ import annotations

import asyncio
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar

import logfire

State = dict[str, Any]
SyncNodeFn = Callable[[State], State]
AsyncNodeFn = Callable[[State], Awaitable[State]]
F = TypeVar("F", bound=Callable[..., Any])


def _record_input(span: logfire.LogfireSpan, state: State) -> None:
    user_message = state.get("user_message")
    if isinstance(user_message, str):
        span.set_attribute("input.user_message", user_message)
    history = state.get("history_payload") or state.get("message_history") or []
    if isinstance(history, list):
        span.set_attribute("input.history_len", len(history))


def _record_output(span: logfire.LogfireSpan, result: Any) -> None:
    if not isinstance(result, dict):
        return
    response = result.get("response")
    if isinstance(response, str):
        span.set_attribute("output.response_len", len(response))
    tool_calls = result.get("tool_calls")
    if isinstance(tool_calls, list):
        span.set_attribute("output.tool_call_count", len(tool_calls))


def traced_node(name: str) -> Callable[[F], F]:
    """Wrap a LangGraph node so its execution becomes a parent span.

    Auto-instrumented Pydantic AI / httpx spans nest beneath this one,
    yielding a clean LangGraph -> PydanticAI -> tool/model hierarchy.
    """

    def decorator(fn: F) -> F:
        if asyncio.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(state: State, *args: Any, **kwargs: Any) -> State:
                with logfire.span(
                    "langgraph.node {node_name}",
                    node_name=name,
                    _tags=["langgraph", "node"],
                ) as span:
                    _record_input(span, state)
                    result = await fn(state, *args, **kwargs)
                    _record_output(span, result)
                    return result

            return async_wrapper  # type: ignore[return-value]

        @wraps(fn)
        def sync_wrapper(state: State, *args: Any, **kwargs: Any) -> State:
            with logfire.span(
                "langgraph.node {node_name}",
                node_name=name,
                _tags=["langgraph", "node"],
            ) as span:
                _record_input(span, state)
                result = fn(state, *args, **kwargs)
                _record_output(span, result)
                return result

        return sync_wrapper  # type: ignore[return-value]

    return decorator
