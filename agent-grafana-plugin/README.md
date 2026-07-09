# Grafana Dashboard Agent — App Plugin

A Grafana **app plugin** that hosts an AI dashboard-building assistant directly inside
Grafana. It adds an **Assistant** page (a React/TypeScript chat UI built with
`@grafana/ui`) and a **Configuration** page, and talks to the headless Python agent in
[`../agent`](../agent) over REST and WebSocket.

> This is the frontend half of the project. The assistant's brain — the LLM, the
> dashboard tools, the data-source access — lives in [`../agent`](../agent). See the
> [root README](../README.md) for the full architecture.

## What you get

- An **Assistant** nav page that sits beside your dashboards.
- A **Configuration** page (Admin-only) to point the plugin at your agent.
- A **Go backend** that proxies REST calls to the agent so the API key never reaches the
  browser; the browser opens the WebSocket directly for token streaming.

## Requirements

- Node.js `>= 22` (`.nvmrc` pins the version)
- Go `1.25+` and [Mage](https://magefile.org/) (for the backend binary)
- Docker (for `npm run server`, which spins up Grafana)
- A running agent — start it first from [`../agent`](../agent)

## Install & run (development)

```bash
# 1. Frontend
npm install
npm run build          # production build  (or: npm run dev  for watch mode)

# 2. Backend (Go) — builds binaries for linux/windows/darwin into dist/
mage -v
mage -l                # list all available targets

# 3. Grafana with the plugin provisioned (Docker)
npm run server         # http://localhost:3000
```

`npm run server` builds `Dockerfile.grafana`, which compiles the frontend + backend and
installs the app into `/var/lib/grafana-plugins/<plugin-id>` (outside the persisted data
volume) so the backend binary is always present. Mounting only `dist/` is **not** enough
for a backend app plugin unless `mage -v` has produced the platform binary there.

Pin a Grafana version if needed:

```bash
GRAFANA_VERSION=12.4.0 npm run server
```

## Configure the plugin

Open Grafana → **Administration → Plugins → Agent → Configuration** and set:

| Field | Stored as | Example | Purpose |
|---|---|---|---|
| HTTP URL | `jsonData.assistantApiUrl` | `http://host.docker.internal:8000` | Base URL the Go backend uses for REST calls |
| WebSocket URL | `jsonData.assistantWsUrl` | `ws://localhost:8000/ws/assistant` | Streaming URL the browser connects to |
| API key | `secureJsonData.apiKey` | *(optional)* | Forwarded by the backend as `Authorization: Bearer <key>`; must match `ASSISTANT_API_KEY` in the agent |

The same values are provisioned for local development in
[`provisioning/plugins/apps.yaml`](./provisioning/plugins/apps.yaml), so a freshly
started `npm run server` is already wired to a local agent on port `8000`.

## Reuse this plugin under your own organization

The example ships with plugin id `eclss-agentfrontend-app` and author `Eclss`. Grafana
requires the id to be `<your-cloud-slug>-<name>-app`. To rename, change it in **every**
file below and then **restart Grafana** (a changed plugin id requires a restart):

- `src/plugin.json` — `id`, `name`, `info.author`
- `pkg/main.go` — `app.Manage("<id>", …)`
- `provisioning/plugins/apps.yaml` — `type`, `org_name`
- `Dockerfile.grafana` — the install path `/var/lib/grafana-plugins/<id>`
- `.github/workflows/ci.yml` — any id references
- `package.json` — `name`, `author`

> **Do not hand-edit `.config/`.** Those files are generated and managed by
> `@grafana/create-plugin`. To update tooling, run
> `npx @grafana/create-plugin@latest update` and follow the
> [extend-configurations guide](https://grafana.com/developers/plugin-tools/how-to-guides/extend-configurations.md).

## Testing & linting

```bash
npm run test          # Jest, watch mode (needs a git repo)
npm run test:ci       # Jest, single run
npm run typecheck     # tsc --noEmit
npm run lint          # eslint  (npm run lint:fix to autofix + prettier)

# End-to-end (Playwright) — needs a running Grafana
npm run server
npm run e2e
```

## Signing & distributing

Plugins distributed via the Grafana catalog (publicly or privately) must be signed with
`@grafana/sign-plugin`. Signing is **not** required for local development — the Docker
dev environment runs the plugin unsigned (`GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS`).

Before signing, read Grafana's
[publishing & signing criteria](https://grafana.com/legal/plugins/#plugin-publishing-and-signing-criteria)
and [signature levels](https://grafana.com/legal/plugins/#what-are-the-different-classifications-of-plugins).

**One-time setup:**

1. Create a [Grafana Cloud account](https://grafana.com/signup).
2. Make sure the first part of the plugin id matches your Grafana Cloud account slug.
3. Create a Grafana Cloud API key with the `PluginPublisher` role.

**Signing via the GitHub Actions release workflow** ([`.github/workflows/release.yml`](./.github/workflows/release.yml)):

1. In the repo, add a secret named `GRAFANA_API_KEY` with your Cloud API key
   (Settings → Secrets → Actions).
2. Push a version tag to trigger the workflow:
   ```bash
   npm version <major|minor|patch>
   git push origin main --follow-tags
   ```

## Learn more

- [`plugin.json` reference](https://grafana.com/developers/plugin-tools/reference/plugin-json)
- [Grafana plugin SDK for Go](https://grafana.com/developers/plugin-tools/key-concepts/backend-plugins/grafana-plugin-sdk-for-go)
- [Basic app plugin example](https://github.com/grafana/grafana-plugin-examples/tree/master/examples/app-basic#readme)
- [Sign a plugin](https://grafana.com/developers/plugin-tools/publish-a-plugin/sign-a-plugin)
