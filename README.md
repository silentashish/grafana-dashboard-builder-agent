# Grafana Dashboard Agent

An AI assistant that builds Grafana dashboards from natural language, delivered as a
first-class experience **inside Grafana**.

The project has two independent, reusable parts:

| Folder | What it is | Stack |
|---|---|---|
| [`agent-grafana-plugin/`](./agent-grafana-plugin) | Grafana **app plugin** — the UI you install into Grafana. Adds an "Assistant" page and a configuration page. | React + TypeScript frontend, Go backend |
| [`agent/`](./agent) | The **headless agent** — the brain that runs the LLM and edits dashboards. | Python, FastAPI, LangGraph, PydanticAI |

Each folder has its own README with detailed setup:

- **Plugin setup →** [`agent-grafana-plugin/README.md`](./agent-grafana-plugin/README.md)
- **Agent setup →** [`agent/README.md`](./agent/README.md)

## How the two pieces fit together

```
┌─────────────────────────────────────────┐
│               Grafana                     │
│  ┌─────────────────────────────────────┐ │
│  │  App plugin (agent-grafana-plugin)  │ │
│  │                                     │ │
│  │  React UI ──REST──► Go backend ─────┼─┼──► FastAPI  (agent/)
│  │     │                               │ │      │  chat, settings, health
│  │     └──────WebSocket────────────────┼─┼──►   │  token streaming
│  └─────────────────────────────────────┘ │      │
└───────────────────────────────────────────┘     │
                                                   ▼
                              ┌────────────────────────────────────┐
                              │  LangGraph + PydanticAI agent       │
                              │                                     │
                              │  ├─ LLM provider (Ollama/OpenAI/…)  │
                              │  ├─ MCP: Grafana  (writes dashboards)│
                              │  ├─ MCP: OpenSearch (discovers data)│
                              │  └─ PostgreSQL  (thread history)     │
                              └────────────────────────────────────┘
```

- The **Go plugin backend** proxies REST calls (chat, settings, health) so the agent's
  API key never reaches the browser.
- The **browser** opens the WebSocket directly for low-latency token streaming.
- The **agent** uses MCP servers to read data (OpenSearch) and write dashboards
  (Grafana), driven by the LLM provider you configure.

## Quickstart

You need three things running: the **agent**, its **model provider**, and a **Grafana**
with the plugin installed.

### 1. Start the agent

```bash
cd agent
cp .env.example .env          # then edit: model provider, Grafana token, OpenSearch, etc.
docker compose up --build     # serves the assistant API on http://localhost:8000
```

Verify: `curl http://localhost:8000/health`

See [`agent/README.md`](./agent/README.md) for model-provider options (Ollama, OpenAI,
Anthropic, …) and every environment variable.

### 2. Start Grafana with the plugin

```bash
cd agent-grafana-plugin
npm install
npm run build      # build the frontend
mage -v            # build the Go backend binaries
npm run server     # start Grafana (Docker) with the plugin provisioned
```

Grafana comes up at `http://localhost:3000`.

### 3. Point the plugin at the agent

Open Grafana → **Administration → Plugins → Agent** → **Configuration**, and set:

| Field | Local default |
|---|---|
| HTTP URL | `http://host.docker.internal:8000` |
| WebSocket URL | `ws://localhost:8000/ws/assistant` |
| API key | *(optional — must match `ASSISTANT_API_KEY` in the agent)* |

These defaults are also provisioned in
[`agent-grafana-plugin/provisioning/plugins/apps.yaml`](./agent-grafana-plugin/provisioning/plugins/apps.yaml).

Then open the **Assistant** page from the Grafana nav and ask it to build a dashboard.

## Configuration surface (what makes this reusable)

Nothing about the deployment is baked into code — every connection point is an option:

**Agent** (`agent/.env`, see [full table](./agent/README.md#environment-variables)):

- `MODEL_PROVIDER` / `MODEL_NAME` / `MODEL_BASE_URL` — swap Ollama for OpenAI, Anthropic, etc.
- `GRAFANA_URL` / `GRAFANA_SERVICE_ACCOUNT_TOKEN` — which Grafana the agent writes to.
- `OPENSEARCH_URL` / `OPENSEARCH_USERNAME` / `OPENSEARCH_PASSWORD` / `OPENSEARCH_DATASOURCE_UID` — the data source it queries.
- `ASSISTANT_API_KEY` / `ASSISTANT_ALLOWED_ORIGINS` — auth and CORS for the API.

**Plugin** (set in the Configuration page or `provisioning/plugins/apps.yaml`):

- `assistantApiUrl` — where the Go backend reaches the agent.
- `assistantWsUrl` — where the browser streams from.
- `apiKey` (secret) — forwarded to the agent as `Authorization: Bearer <key>`.

### Renaming the plugin for your organization

The example ships with the plugin id `eclss-agentfrontend-app` and author `Eclss`. To
publish under your own org, the plugin id must be `<your-cloud-slug>-<name>-app`. Change
it in **all** of these files (Grafana requires an id restart afterward):

- `agent-grafana-plugin/src/plugin.json` — `id`, `name`, `info.author`
- `agent-grafana-plugin/pkg/main.go` — `app.Manage("<id>", …)`
- `agent-grafana-plugin/provisioning/plugins/apps.yaml` — `type`, `org_name`
- `agent-grafana-plugin/Dockerfile.grafana` — the copy path `/var/lib/grafana-plugins/<id>`
- `agent-grafana-plugin/.github/workflows/ci.yml` — any id references
- `agent-grafana-plugin/package.json` — `name`, `author`

> The files under `agent-grafana-plugin/.config/` are managed by Grafana's plugin tools —
> don't hand-edit them; re-run `npx @grafana/create-plugin@latest update` if they drift.

## License

Apache-2.0. See [`agent-grafana-plugin/LICENSE`](./agent-grafana-plugin/LICENSE).
