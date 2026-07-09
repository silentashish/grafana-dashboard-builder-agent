# Grafana Dashboard Agent

An AI assistant that builds Grafana dashboards from natural language, right inside
Grafana. This app plugin adds an **Assistant** page where you describe the dashboard you
want and the agent creates it for you — discovering data and writing panels through MCP
tools.

## Overview

The plugin is the UI. It connects to a separate headless agent (FastAPI + LangGraph +
PydanticAI) that runs the LLM and performs the dashboard operations. The plugin's Go
backend proxies REST calls to the agent so its API key never reaches the browser, while
the browser streams responses over a WebSocket for a live, token-by-token experience.

## Requirements

- Grafana `>= 12.3.0`
- A running Dashboard Agent service reachable from Grafana (see the project repository)
- The agent's LLM provider (Ollama, OpenAI, Anthropic, …) and, for dashboard writes, a
  Grafana service-account token configured on the agent

## Getting started

1. Install and enable the plugin in Grafana.
2. Open the plugin's **Configuration** page (Admin) and set:
   - **HTTP URL** — the agent's REST base, e.g. `http://host.docker.internal:8000`
   - **WebSocket URL** — e.g. `ws://localhost:8000/ws/assistant`
   - **API key** — optional; must match the agent's `ASSISTANT_API_KEY`
3. Open the **Assistant** page from the nav and ask it to build a dashboard.

## Documentation

Full setup, architecture, and configuration reference are in the project repository
README, including how to run the agent and how to reuse the plugin under your own
organization.
