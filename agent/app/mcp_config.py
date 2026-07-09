"""Connection config for the upstream MCP servers we proxy.

Each entry describes how MultiServerMCPClient should spawn and talk to
one backend (OpenSearch, Grafana, etc.).  Everything is configured via
environment variables so the same code works locally and inside Docker.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from dotenv import load_dotenv
from pydantic_ai.mcp import MCPServerStdio, ProcessToolCallback

load_dotenv()

_log = logging.getLogger(__name__)
_warned_missing_grafana_token = False


def _resolve_grafana_token() -> str:
    """Pull the Grafana service-account token, warning once if it's missing.

    Why: an empty token causes mcp-grafana to fail every dashboard write with a
    bare 401, which the model can't recover from. The warning makes the misconfig
    visible in logs so operators don't chase phantom schema bugs instead.
    """
    global _warned_missing_grafana_token
    token = os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not token and not _warned_missing_grafana_token:
        _log.warning(
            "GRAFANA_SERVICE_ACCOUNT_TOKEN is empty; Grafana MCP writes will return 401. "
            "Mint a token in Grafana UI -> Administration -> Service accounts."
        )
        _warned_missing_grafana_token = True
    return token


def _build_env(**overrides: str) -> dict[str, str]:
    """Return os.environ merged with the given overrides.

    We need to pass the *full* environment to child processes launched via
    StdioServerParameters -- otherwise they'd lose PATH, HOME, and friends.
    """
    env = os.environ.copy()
    env.update({k: v for k, v in overrides.items() if v})
    return env


def mcp_server_configs() -> dict:
    """Return connection dicts consumed by ``MultiServerMCPClient``.

    Add new upstream MCP servers here.  Each key becomes the tool-name
    prefix so tools don't collide across backends.
    """
    return {
        "opensearch": {
            "transport": "stdio",
            "command": os.getenv(
                "OPENSEARCH_MCP_BIN",
                "opensearch-mcp-server-py",
            ),
            "args": ["--transport", "stdio"],
            "env": _build_env(
                OPENSEARCH_URL=os.getenv("OPENSEARCH_URL", "http://opensearch:9200"),
                OPENSEARCH_USERNAME=os.getenv("OPENSEARCH_USERNAME", "admin"),
                OPENSEARCH_PASSWORD=os.getenv("OPENSEARCH_PASSWORD", ""),
            ),
        },
        "grafana": {
            "transport": "stdio",
            "command": os.getenv("GRAFANA_MCP_BIN", "mcp-grafana"),
            "args": ["mcp-grafana"] if os.getenv("GRAFANA_MCP_BIN") == "uvx" else [],
            "env": _build_env(
                GRAFANA_URL=os.getenv("GRAFANA_URL", "http://grafana:3000"),
                GRAFANA_SERVICE_ACCOUNT_TOKEN=_resolve_grafana_token(),
            ),
        },
    }


def mcp_toolsets(
    *,
    process_tool_call: ProcessToolCallback | None = None,
    process_tool_call_factory: Callable[[str], ProcessToolCallback] | None = None,
) -> list[MCPServerStdio]:
    """Build PydanticAI MCP toolsets from the shared server config."""
    timeout = float(os.getenv("MCP_CONNECT_TIMEOUT", "10"))
    read_timeout = float(os.getenv("MCP_READ_TIMEOUT", "300"))
    servers: list[MCPServerStdio] = []

    for server_name, config in mcp_server_configs().items():
        transport = config.get("transport", "stdio")
        if transport != "stdio":
            raise ValueError(f"Unsupported MCP transport for {server_name}: {transport}")

        callback = (
            process_tool_call_factory(server_name)
            if process_tool_call_factory is not None
            else process_tool_call
        )
        servers.append(
            MCPServerStdio(
                command=config["command"],
                args=config.get("args", []),
                env=config.get("env"),
                cwd=config.get("cwd"),
                tool_prefix=server_name,
                timeout=timeout,
                read_timeout=read_timeout,
                process_tool_call=callback,
            )
        )

    return servers
