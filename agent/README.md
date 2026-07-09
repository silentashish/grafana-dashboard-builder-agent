# Grafana Dashboard Agent — Headless Assistant

A FastAPI service wrapping a **LangGraph** workflow whose model runtime is powered by
**PydanticAI**. This is the brain that the Grafana app plugin talks to: it runs the LLM,
discovers data via OpenSearch, and writes dashboards to Grafana — all through MCP tools.

> This is the backend half of the project. The UI lives in the Grafana app plugin at
> [`../agent-grafana-plugin`](../agent-grafana-plugin). See the [root README](../README.md)
> for the full architecture. A legacy Chainlit entrypoint (`app/app.py`) still exists, but
> the default runtime is the headless API consumed by the plugin.

## What this runs

- FastAPI assistant API on port `8000` (REST chat, settings, health, WebSocket streaming)
- A configurable LLM provider (Ollama, OpenAI, Anthropic, …)
- MCP tools for **OpenSearch** (data discovery) and **Grafana** (dashboard writes)
- PostgreSQL-backed thread history with Alembic migrations

## Project structure

```text
agent/
├── app/
│   ├── api.py            # FastAPI entrypoint consumed by the Grafana plugin
│   ├── app.py            # Legacy Chainlit entrypoint
│   ├── runtime.py        # Shared headless runtime adapter
│   ├── agent_graph.py    # LangGraph workflow + PydanticAI agent
│   ├── mcp_config.py     # MCP server config (OpenSearch, Grafana) — all env-driven
│   └── tools/            # Dashboard-building tools
├── alembic/              # DB migrations
├── scripts/start.sh      # alembic upgrade head → uvicorn
├── docker-compose.yml    # agent + PostgreSQL
├── Dockerfile
├── requirements.txt
└── .env.example
```

## Prerequisites

1. Docker Desktop (or Docker Engine + Compose plugin)
2. A reachable **LLM provider** (see below)
3. A **Grafana** instance and a **service-account token** (so the agent can write dashboards)
4. An **OpenSearch** endpoint (if you use the OpenSearch tools)

### Option: local Ollama

PydanticAI reaches Ollama through its OpenAI-compatible layer, so the base URL ends in `/v1`.

```bash
ollama pull kimik2.6      # or any model you set in MODEL_NAME
ollama serve
curl http://localhost:11434/api/tags   # quick check
```

### Option: hosted provider

Set `MODEL_PROVIDER=openai` (or `anthropic`, …), `MODEL_NAME`, and the provider's API key
(e.g. `ANTHROPIC_API_KEY`) in `.env`.

## Run with Docker Compose (recommended)

```bash
cp .env.example .env      # then edit — see "Environment variables" below
docker compose up --build
```

Compose starts **PostgreSQL** and the **agent**, runs `alembic upgrade head`
automatically, then serves the API. Check it:

```bash
curl http://localhost:8000/health
```

> Because the agent runs in a container, point `GRAFANA_URL`, `OPENSEARCH_URL`, and
> `OLLAMA_BASE_URL` / `MODEL_BASE_URL` at services the container can reach. For host
> services on Docker Desktop use `http://host.docker.internal:<port>` (the compose file
> maps `host.docker.internal` on Linux too).

## Run locally (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# needs a reachable PostgreSQL; set DATABASE_URL accordingly, then:
alembic upgrade head
uvicorn api:app --app-dir app --host 0.0.0.0 --port 8000
```

Legacy Chainlit UI, only when needed:

```bash
chainlit run app/app.py --host 0.0.0.0 --port 8001
```

## Connect the Grafana plugin

Configure the plugin (Configuration page or `provisioning/plugins/apps.yaml`) with:

- HTTP URL → the agent's REST base, e.g. `http://host.docker.internal:8000`
- WebSocket URL → `ws://localhost:8000/ws/assistant`
- API key → must equal `ASSISTANT_API_KEY` (leave both empty for local dev)

## Environment variables

Copy `.env.example` to `.env` and adjust. Everything is env-driven so the same code runs
locally, in Docker, or against cloud services.

### Model provider

| Variable | Default | Description |
|---|---|---|
| `MODEL_PROVIDER` | `ollama` | PydanticAI provider prefix: `ollama`, `openai`, `anthropic`, … |
| `MODEL_NAME` | `kimik2.6` | Model name passed to the provider |
| `MODEL_BASE_URL` | `http://localhost:11434/v1` | Optional provider base URL override |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama URL for local runs (keep the `/v1` suffix) |
| `ANTHROPIC_API_KEY` | *(unset)* | Provider API key when using a hosted model |

### API auth & CORS

| Variable | Default | Description |
|---|---|---|
| `ASSISTANT_API_KEY` | empty | Optional key required by the API; the plugin forwards it from `secureJsonData.apiKey` |
| `ASSISTANT_ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated CORS origins for REST calls |

### Database (PostgreSQL)

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://chainlit:chainlit@postgres:5432/chainlit` | Runtime DB URL (app context storage) |
| `POSTGRES_DB` | `chainlit` | Database name (compose) |
| `POSTGRES_USER` | `chainlit` | Database user (compose) |
| `POSTGRES_PASSWORD` | `chainlit` | Database password (compose) |

### Grafana MCP (dashboard writes)

| Variable | Default | Description |
|---|---|---|
| `GRAFANA_URL` | `http://grafana:3000` | Grafana the agent writes to |
| `GRAFANA_SERVICE_ACCOUNT_TOKEN` | empty | **Required** for writes. Grafana → Administration → Service accounts → Add token. Empty ⇒ every write 401s |
| `GRAFANA_MCP_BIN` | `mcp-grafana` | MCP binary/command override (e.g. `uvx`) |

### OpenSearch MCP (data discovery)

| Variable | Default | Description |
|---|---|---|
| `OPENSEARCH_URL` | `http://opensearch:9200` | OpenSearch endpoint |
| `OPENSEARCH_USERNAME` | `admin` | OpenSearch user |
| `OPENSEARCH_PASSWORD` | empty | OpenSearch password |
| `OPENSEARCH_DATASOURCE_UID` | `opensearch-default` | Grafana data-source UID the agent targets in generated panels |
| `OPENSEARCH_MCP_BIN` | `opensearch-mcp-server-py` | MCP binary/command override |
| `MCP_CONNECT_TIMEOUT` | `10` | MCP connect timeout (seconds) |
| `MCP_READ_TIMEOUT` | `300` | MCP read timeout (seconds) |

### Observability (Logfire) & legacy Chainlit

| Variable | Default | Description |
|---|---|---|
| `LOGFIRE_TOKEN` | empty | Ship traces to Logfire when set; empty keeps traces local |
| `LOGFIRE_SEND` | `if-token-present` | `false` disables shipping even with a token |
| `LOGFIRE_CONSOLE` | `false` | Print traces to console |
| `CHAINLIT_AUTH_SECRET` | `changeme` | Legacy Chainlit auth secret |
| `CHAINLIT_USERNAME` / `CHAINLIT_PASSWORD` | `admin` / `admin` | Legacy Chainlit login — change before exposing anywhere |

### Debugging (debugpy)

| Variable | Default | Description |
|---|---|---|
| `DEBUGPY_ENABLED` | `false` | Enable the remote debugger listener |
| `DEBUGPY_HOST` | `0.0.0.0` | Debugger bind host |
| `DEBUGPY_PORT` | `5678` | Debugger port |
| `DEBUGPY_WAIT_FOR_CLIENT` | `false` | Pause startup until the debugger attaches |

## Conversation history

- PostgreSQL stores app-specific conversation context.
- Alembic manages the schema.
- `conversation_contexts` is the source of truth for PydanticAI message history per thread.
- Grafana-generated thread IDs restore model context across turns.

The Grafana UI owns visible chat state; the API preserves the exact LLM context per thread.

## Debugging

The app ships `debugpy` with env-controlled startup.

```bash
# .env
DEBUGPY_ENABLED=true
DEBUGPY_PORT=5678
DEBUGPY_WAIT_FOR_CLIENT=true
```

```bash
docker compose up --build
```

Attach from VS Code using the Docker debug attach config in `.vscode/launch.json`, open
the repo root as the workspace (path mapping expects `${workspaceFolder}` = project root),
set breakpoints in `app/api.py`, `app/runtime.py`, or `app/agent_graph.py`, then trigger a
request. When `DEBUGPY_WAIT_FOR_CLIENT=true`, startup pauses until the debugger attaches.

Local (no Docker):

```bash
DEBUGPY_ENABLED=true DEBUGPY_WAIT_FOR_CLIENT=true uvicorn api:app --app-dir app --host 0.0.0.0 --port 8000
```

## Troubleshooting

**`ModuleNotFoundError: No module named 'agent_graph'`** — run uvicorn with `--app-dir app`
(the compose command and `scripts/start.sh` already do).

**Grafana writes return 401** — `GRAFANA_SERVICE_ACCOUNT_TOKEN` is empty or wrong. Mint one
under Grafana → Administration → Service accounts.

**Ollama connection refused** — verify the host is up (`curl http://localhost:11434/api/tags`)
and, from a container, use `http://host.docker.internal:11434/v1`. Bind Ollama with
`OLLAMA_HOST=0.0.0.0:11434 ollama serve` if needed.

**Rebuild cleanly:**

```bash
docker compose down
docker compose up --build
```

**Debugger port already in use** — set another `DEBUGPY_PORT` in `.env` and restart.
