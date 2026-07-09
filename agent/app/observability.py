"""Logfire configuration and auto-instrumentation for the Chainlit agent."""

from __future__ import annotations

import os

import logfire

_CONFIGURED = False
_INSTRUMENTED = False


def _is_enabled(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_send_mode() -> bool | str:
    """Decide whether to ship spans to Logfire.

    Why: a stale LOGFIRE_TOKEN in .env will otherwise spam 401s every batch.
    LOGFIRE_SEND=false lets ops disable shipping without deleting the token.
    """
    override = os.getenv("LOGFIRE_SEND")
    if override is None:
        return "if-token-present"
    normalized = override.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return "if-token-present"


def configure_observability(service_name: str = "chainlit-ollama-agent") -> None:
    """Initialize Logfire once per process. Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    logfire.configure(
        service_name=service_name,
        service_version=os.getenv("GIT_SHA", "dev"),
        environment=os.getenv("ENV", "local"),
        send_to_logfire=_resolve_send_mode(),
        scrubbing=logfire.ScrubbingOptions(
            extra_patterns=[r"api[_-]?key", r"authorization", r"bearer"],
        ),
        console=logfire.ConsoleOptions(min_log_level="info")
        if _is_enabled(os.getenv("LOGFIRE_CONSOLE", "false"))
        else False,
    )
    _CONFIGURED = True


def install_instrumentors(app=None) -> None:
    """Attach auto-instrumentors. Call after configure_observability()."""
    global _INSTRUMENTED
    if _INSTRUMENTED:
        return

    logfire.instrument_pydantic_ai()
    logfire.instrument_pydantic()
    logfire.instrument_httpx(capture_all=False)
    if app is not None:
        try:
            logfire.instrument_fastapi(app, capture_headers=False)
        except RuntimeError:
            pass

    _INSTRUMENTED = True
