# Headless PydanticAI LangGraph Assistant

FastAPI service with a LangGraph workflow whose model runtime is powered by PydanticAI. The legacy Chainlit entrypoint remains in `app/app.py`, but the default runtime is now the headless API consumed by the Grafana app plugin.

## What This Runs

- FastAPI assistant API on port `8000`
- Model provider served from your host or cloud endpoint
- Tool-enabled assistant for OpenSearch and Grafana dashboard operations
- WebSocket bridge for Grafana UI streaming packets
- PostgreSQL-backed thread storage with Alembic migrations

## Project Structure

```text
chainlit-ollama-agent/
├── app/
│   ├── app.py                 # Chainlit entrypoint
│   ├── api.py                 # FastAPI entrypoint for Grafana
│   ├── runtime.py             # Shared headless runtime adapter
│   └── agent_graph.py         # LangGraph workflow + PydanticAI agent runtime
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .vscode/launch.json        # VS Code launch/attach configs
├── .chainlit/config.toml
├── .env.example
└── README.md
```

## Prerequisites

1. Docker Desktop (or Docker Engine + Compose plugin)
2. A reachable model provider

### Local Ollama via PydanticAI

PydanticAI supports Ollama through its OpenAI-compatible provider layer. The local endpoint should use `/v1`.

3. Model pulled locally:

```bash
ollama pull kimik2.6
```

4. Ollama server running:

```bash
ollama serve
```

Quick check:

```bash
curl http://localhost:11434/api/tags
```

## Run With Docker Compose

1. Create env file:

```bash
cp .env.example .env
```

Default login after copying `.env.example`:

- Username: `admin`
- Password: `admin`

Change these values in `.env` before exposing the app anywhere outside local development.

The default Compose stack also starts PostgreSQL and runs `alembic upgrade head` before the assistant API starts.

2. Build and run:

```bash
docker compose up --build
```

3. Open the API health endpoint:

`http://localhost:8000/health`

## Run Locally (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn api:app --app-dir app --host 0.0.0.0 --port 8000
```

Legacy Chainlit UI, when explicitly needed:

```bash
chainlit run app/app.py --host 0.0.0.0 --port 8001
```

## Debugging

The app includes `debugpy` with env-controlled startup.

### Docker Compose Debugging

1. Enable debugger in `.env`:

```bash
DEBUGPY_ENABLED=true
DEBUGPY_PORT=5678
DEBUGPY_WAIT_FOR_CLIENT=true
```

2. Start compose:

```bash
docker compose up --build
```

3. Attach debugger from VS Code:
- Use the Docker debug attach configuration from `.vscode/launch.json`
- Open the repository root as the VS Code workspace (path mapping expects `${workspaceFolder}` = project root)
- Set breakpoints in `app/api.py`, `app/runtime.py`, or `app/agent_graph.py`
- Open the Grafana Assistant page or call `POST http://localhost:8000/api/assistant/chat`

If `DEBUGPY_WAIT_FOR_CLIENT=true`, app startup pauses until the debugger attaches.

### Local Debugging (No Docker)

- Run manually:

```bash
DEBUGPY_ENABLED=true DEBUGPY_WAIT_FOR_CLIENT=true uvicorn api:app --app-dir app --host 0.0.0.0 --port 8000
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CHAINLIT_AUTH_SECRET` | `changeme` | Legacy Chainlit auth secret |
| `CHAINLIT_USERNAME` | `admin` | Username for local password auth |
| `CHAINLIT_PASSWORD` | `admin` | Password for local password auth |
| `DATABASE_URL` | `postgresql+asyncpg://chainlit:chainlit@postgres:5432/chainlit` | Runtime database URL used by Chainlit and app context storage |
| `POSTGRES_DB` | `chainlit` | PostgreSQL database name |
| `POSTGRES_USER` | `chainlit` | PostgreSQL username |
| `POSTGRES_PASSWORD` | `chainlit` | PostgreSQL password |
| `MODEL_PROVIDER` | `ollama` | PydanticAI provider prefix such as `ollama`, `openai`, or `anthropic` |
| `MODEL_NAME` | `kimik2.6` | Model name passed to the configured provider |
| `MODEL_BASE_URL` | `http://localhost:11434/v1` | Optional provider base URL override used by the app |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama URL for local runs |
| `ASSISTANT_API_KEY` | empty | Optional API key required by FastAPI. The Grafana plugin forwards this from `secureJsonData.apiKey` for REST calls. |
| `ASSISTANT_ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins for REST calls. |
| `DEBUGPY_ENABLED` | `false` | Enable remote debugger listener |
| `DEBUGPY_HOST` | `0.0.0.0` | Debugger bind host inside container/process |
| `DEBUGPY_PORT` | `5678` | Debugger port |
| `DEBUGPY_WAIT_FOR_CLIENT` | `false` | Pause startup until debugger attaches |

When `MODEL_PROVIDER=ollama`, the app normalizes a missing `/v1` suffix automatically, but keeping `/v1` in env vars avoids ambiguity.

## Conversation History

This app now uses:

- PostgreSQL for app-specific conversation context
- Alembic migrations for schema management
- `conversation_contexts` as the source of truth for PydanticAI message history per thread
- Grafana-generated thread IDs to restore model context across turns

The Grafana UI owns visible chat state, while the API preserves the exact LLM context for each thread.

## Troubleshooting

### `ModuleNotFoundError: No module named 'agent_graph'`

This happens when only `./app` is mounted into the container and root modules are hidden.  
The current compose file mounts the full repo (`.:/app`) and runs `uvicorn` with `--app-dir app`, which fixes this.

### Ollama connection refused

1. Verify Ollama is running on host:

```bash
curl http://localhost:11434/api/tags
```

2. If needed, bind Ollama explicitly:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

### Rebuild cleanly

```bash
docker compose down
docker compose up --build
```

### Debugger port already in use

Set another port in `.env`, then restart:

```bash
DEBUGPY_PORT=5680
docker compose up --build
```
