"""Chainlit application backed by a PydanticAI + LangGraph agent runtime."""

from __future__ import annotations

import json
import os
from typing import Any, List

import chainlit as cl
from chainlit.types import ThreadDict

from db import (
    get_database_url,
    load_conversation_history,
    normalize_conversation_history,
    save_conversation_history,
)
from runtime import assistant_greeting, configure_runtime, create_agent

try:
    from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
except ImportError:
    SQLAlchemyDataLayer = None


DATABASE_URL = get_database_url()
configure_runtime(service_name="chainlit-ollama-agent")


@cl.data_layer
def get_data_layer():
    """Persist threads so Chainlit can restore and switch conversations."""
    if SQLAlchemyDataLayer is None:
        raise RuntimeError(
            "SQLAlchemyDataLayer is unavailable. Install the SQLAlchemy extras from requirements.txt."
        )

    return SQLAlchemyDataLayer(conninfo=DATABASE_URL)


@cl.password_auth_callback
def auth_callback(username: str, password: str):
    """Simple local auth required for Chainlit thread history."""
    expected_username = os.getenv("CHAINLIT_USERNAME", "admin")
    expected_password = os.getenv("CHAINLIT_PASSWORD", "admin")

    if username == expected_username and password == expected_password:
        return cl.User(
            identifier=username,
            metadata={"provider": "credentials", "role": "admin"},
        )
    return None


def _metadata_conversation_history(thread: ThreadDict) -> List[Any]:
    """Read the persisted conversation history from thread metadata when available."""
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}

    return normalize_conversation_history(metadata.get("conversation_history"))


def initialize_session(conversation_history: List[Any] | None = None):
    """Create a fresh agent instance and load the restored history if any."""
    agent = create_agent()
    cl.user_session.set("agent", agent)
    cl.user_session.set("conversation_history", conversation_history or [])
    return agent


def get_current_thread_id() -> str | None:
    """Get the current Chainlit thread ID."""
    session = getattr(cl.context, "session", None)
    return getattr(session, "thread_id", None)


async def hydrate_conversation_history(thread: ThreadDict | None = None) -> List[Any]:
    """Load the conversation history from the database, with thread metadata as fallback."""
    thread_id = thread.get("id") if thread else get_current_thread_id()
    history = await load_conversation_history(thread_id)
    if history:
        return history

    if thread:
        history = _metadata_conversation_history(thread)
        if history:
            await save_conversation_history(thread_id, history)
            return history

    return []


@cl.on_chat_start
async def start():
    """Initialize the agent when chat starts."""
    agent = initialize_session()
    app_user = cl.user_session.get("user")

    await cl.Message(
        content=assistant_greeting(app_user.identifier, agent)
    ).send()


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    """Restore the agent and model conversation state for resumed threads."""
    history = await hydrate_conversation_history(thread)
    initialize_session(history)


@cl.on_message
async def on_message(message: cl.Message):
    """Handle incoming messages."""
    agent = cl.user_session.get("agent")
    if agent is None:
        agent = initialize_session()

    conversation_history = normalize_conversation_history(cl.user_session.get("conversation_history"))
    if not conversation_history:
        conversation_history = await hydrate_conversation_history()
        cl.user_session.set("conversation_history", conversation_history)

    try:
        async with cl.Step(name="Agent trace", type="run", show_input="json") as trace_step:
            trace_step.input = json.dumps(
                {
                    "thread_id": get_current_thread_id(),
                    "user_message": message.content,
                    "history_length": len(conversation_history),
                    "model": agent.model,
                    "provider": agent.provider_name,
                },
                indent=2,
            )

            response = await agent.send_message(
                message=message.content,
                conversation_history=conversation_history,
            )

            tool_calls = response.get("tool_calls", [])
            for tool_call in tool_calls:
                async with cl.Step(name=tool_call["tool"], type="tool", show_input="json") as tool_step:
                    tool_step.input = json.dumps(tool_call["parameters"], indent=2)
                    tool_step.output = tool_call["result"]

            if tool_calls:
                async with cl.Step(name="Final answer", type="llm") as final_step:
                    final_step.input = json.dumps(
                        [
                            {
                                "tool": call["tool"],
                                "parameters": call["parameters"],
                                "result": call["result"],
                            }
                            for call in tool_calls
                        ],
                        indent=2,
                    )
                    final_step.output = response["content"]

            trace_step.output = (
                f"Completed with {len(tool_calls)} tool call(s)." if tool_calls else "Completed without tools."
            )

        conversation_history = normalize_conversation_history(response.get("conversation_history"))
        cl.user_session.set("conversation_history", conversation_history)
        await save_conversation_history(get_current_thread_id(), conversation_history)

        await cl.Message(content=response["content"]).send()

    except Exception as e:
        await cl.Message(
            content=(
                f"Error: {str(e)}\n\n"
                f"Check the {agent.provider_name} model configuration for {agent.model}."
            ),
            author="System",
        ).send()
