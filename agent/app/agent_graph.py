"""PydanticAI + LangGraph conversation runtime for the Chainlit app."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict, cast

import logfire
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.mcp import CallToolFunc, ToolResult
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.usage import UsageLimits

from mcp_config import mcp_toolsets
from tools.dashboard_builder import (
    build_and_save_opensearch_chart_dashboard_tool,
    build_and_save_opensearch_dashboard_tool,
    build_and_save_opensearch_planned_dashboard_tool,
    build_dashboard_tool,
    build_opensearch_chart_dashboard_tool,
    build_opensearch_dashboard_tool,
    build_opensearch_planned_dashboard_tool,
    summarize_opensearch_mapping_tool,
)
from tracing import traced_node

_DASHBOARD_INTENT_RE = re.compile(
    r"\b(dashboard|grafana|panel|panels|visuali[sz]ation|build.*chart|create.*chart)\b",
    re.IGNORECASE,
)
_SOURCE_CODE_BLOCK_RE = re.compile(r"```(?:python|json|py)\b", re.IGNORECASE)

_DASHBOARD_INSTRUCTIONS_STATIC = """You are an observability copilot inside a Grafana Assistant plugin. You have three capability layers:

1. **OpenSearch MCP tools** (prefix `opensearch_`) — `ListIndexTool`, `IndexMappingTool`, `SearchIndexTool`, `CountTool`, `MsearchTool`, `ExplainTool`, `ClusterHealthTool`, `GetShardsTool`, `LogPatternAnalysisTool`, `DataDistributionTool`, `GenericOpenSearchApiTool`. Tool names are PascalCase and prefixed: e.g. `opensearch_IndexMappingTool`.
2. **Grafana MCP tools** (prefix `grafana_`, NOT `mcp_grafana_`) — key ones: `list_datasources`, `get_datasource`, `search_dashboards`, `get_dashboard_by_uid`, `update_dashboard` (also creates), `get_dashboard_panel_queries`. Snake-case and prefixed: e.g. `grafana_update_dashboard`.
3. **Local dashboard builders**:
   - **`summarize_opensearch_mapping_tool`** — use after a large `opensearch_IndexMappingTool` response to collapse mappings into canonical `time_field`, `numeric_fields`, `categorical_fields`, and `table_fields`.
   - **`build_and_save_opensearch_chart_dashboard_tool`** — preferred for simple single-chart/panel create requests (for example: "make a pie chart by status", "build one bar chart", "create a stat panel"). It creates exactly one data panel, saves it to Grafana, verifies the saved dashboard, and retries deterministic repairs by default.
   - **`build_opensearch_chart_dashboard_tool`** — same focused one-panel builder, but returns dashboard JSON without saving.
   - **`build_and_save_opensearch_planned_dashboard_tool`** — preferred for open-ended dashboard create requests after discovery and user confirmation. The model supplies one structured dashboard plan; the tool compiles exactly that plan, saves it to Grafana, verifies the saved dashboard, and retries deterministic repairs by default. It does not add default tables or template panels.
   - **`build_opensearch_planned_dashboard_tool`** — same explicit-plan compiler, but returns dashboard JSON without saving.
   - **`build_and_save_opensearch_dashboard_tool`** — legacy rich template builder for requests that explicitly ask for a comprehensive/dense/multi-row KPI/trend/distribution/raw-document dashboard.
   - **`build_opensearch_dashboard_tool`** — same legacy rich template builder without saving.
   - **`build_dashboard_tool`** — escape hatch for custom layouts. Runs Python that uses `grafana_foundation_sdk` to build a dashboard and prints its JSON. Returns the JSON string on success, or `"Build failed. ..."` with a traceback on failure.

## Dashboard planning workflow

- For vague/open-ended requests such as "Based on OpenSearch data with trades-* add a dashboard", do not immediately build a template dashboard. First discover the datasource, index mapping, and 1-5 sample documents. If the index pattern or datasource cannot be inferred, ask the user for the missing information.
- After discovery, produce a concise dashboard plan in the response and ask for approval before saving. The plan should list variables and each proposed panel with chart type, metric, grouping field, and reason. Include a table panel only if the data and user goal make a table useful; raw/debug tables are not mandatory.
- When the user confirms the plan, call `build_and_save_opensearch_planned_dashboard_tool` with `confirmed_by_user=true`. Pass a single structured `dashboard_plan` containing exactly the approved panels. Do not add panels that were not in the plan.
- If the user already provides an explicit panel list or says to build a specific approved plan, you may call the planned save tool in the same turn with `confirmed_by_user=true`.
- Use the legacy rich builder only when the user explicitly requests that fixed style, e.g. comprehensive multi-row dashboard with KPI/stat row, telemetry trends, distribution panels, and raw documents.

## Dashboard quality bar

- For dashboard requests, first inspect the relevant OpenSearch mapping and one small sample document unless the user already supplied field names.
- If the mapping response is large, call `summarize_opensearch_mapping_tool` before selecting fields or building the dashboard.
- Identify:
  - time field: prefer `@timestamp`, then `timestamp`, then `received_at`, then the mapped date field that best matches the request.
  - numeric fields: `float`, `double`, `half_float`, `scaled_float`, `integer`, `long`, `short`, `byte`.
  - categorical fields: exact aggregation fields. Use `keyword` / `constant_keyword` fields directly; for `text` fields with a `keyword` subfield, pass `field.keyword`.
  - table fields: IDs, timestamps, names, status/state, source/site/equipment, and the most important metrics.
- Match the user's requested scope. If the user asks for a single chart/panel, create exactly one chart/panel with `build_and_save_opensearch_chart_dashboard_tool` (or `build_opensearch_chart_dashboard_tool` when they ask for JSON only). Do not add KPI rows, trend rows, raw tables, or extra charts unless the user asked for them.
- For open-ended dashboard requests, prefer a planned dashboard over the legacy rich template. The LLM should decide the panel mix from mapping/sample data, present that plan, and then compile exactly the approved plan.
- Prefer `build_and_save_opensearch_dashboard_tool` only when the user asks for the legacy comprehensive template shape. Use `build_opensearch_dashboard_tool` only when the user asks for that comprehensive dashboard JSON without saving.
- Leave save-tool verification enabled unless the user explicitly asks to skip it. If verification fails, report the failed panel/query errors rather than claiming the dashboard works.
- A useful planned dashboard should contain only panels that answer the user's goal. Do not add a raw table, top-values table, or generic debugging table unless the plan explicitly justifies it.
- A useful legacy comprehensive dashboard should normally have at least: one time-series volume panel, one stat, one panel per important numeric metric (up to a reasonable limit), top categorical breakdowns, and a raw-documents table for debugging.
- Avoid dashboards that only contain a single count or one generic chart unless the user explicitly asks for that; if they do explicitly ask, honor it with the focused chart builder.
- User-provided SDK class names are not authoritative. If the user mentions `MetricAggregation`, `BucketAggregation`, `Templating`, `Variable`, or any OpenSearch-specific Foundation SDK module, ignore those names and use the local tool allowlist.

## `build_dashboard_tool` — one-call contract (HARD)

- **One call per dashboard.** The snippet you submit MUST be the complete dashboard build — every import, every panel, every target, ending in a single `print(JSONEncoder(...).encode(dashboard.build()))`. Do not split the build across multiple calls.
- **Never use this tool as a REPL.** Snippets like `print('ok')`, `print('loaded')`, or `from X import Y; print(...)` to probe whether a name exists are forbidden. The tool is not a sandbox for exploring the SDK — it is a one-shot dashboard compiler.
- **Do not invent imports or class names.** Use only the imports and method names listed in the working skeleton below. If a feature is not covered there, omit it rather than guessing (`BucketAgg`, `Agg`, `MetricBuilder`, `builders.elasticsearch.aggs`, etc. do **not** exist).
- If `build_dashboard_tool` fails, you get exactly **2 retries**. Each retry must again be a complete one-shot build. After 2 failures, return a concise failure report with the error. Do not return runnable dashboard source code as the solution.

## Efficiency rules

- Never call the same MCP tool with the same args twice in one turn. Reuse mappings/samples from your context.
- A typical focused chart flow needs at most: 1× `grafana_list_datasources` (skip if UID known) → 1× `opensearch_IndexMappingTool` → 1× optional `summarize_opensearch_mapping_tool` → **1×** `build_and_save_opensearch_chart_dashboard_tool`.
- A typical open-ended planned dashboard flow takes two turns: first 1× `grafana_list_datasources` → 1× `opensearch_IndexMappingTool` → 1× optional `summarize_opensearch_mapping_tool` → 1× `opensearch_SearchIndexTool` with `size: 1-5` → respond with the proposed plan and ask for approval; after approval, **1×** `build_and_save_opensearch_planned_dashboard_tool`.
- A typical legacy comprehensive dashboard flow needs at most: 1× `grafana_list_datasources` (skip if UID known) → 1× `opensearch_ListIndexTool` (skip if user named the index) → 1× `opensearch_IndexMappingTool` → 1× optional `summarize_opensearch_mapping_tool` for large mappings → 1× optional `opensearch_SearchIndexTool` with `size: 1` → **1×** `build_and_save_opensearch_dashboard_tool`.

## Discovering the OpenSearch datasource

Call `grafana_list_datasources` once and pick an entry with `type == "grafana-opensearch-datasource"`. If the user names an index pattern (e.g. `trades-*`) and there is more than one OpenSearch datasource, call `grafana_get_datasource` for the candidate datasource UID(s) and prefer the one whose `jsonData.database` matches that pattern. Only fall back to the default OpenSearch datasource when no candidate exposes a matching database/index pattern.

## Dashboard build / update flow

- **Never emit Grafana dashboard JSON directly in your reply or in `grafana_update_dashboard` args.** Always produce it via a local builder/save tool or `build_dashboard_tool`.
- **Create:** build JSON → call `grafana_update_dashboard` with `{"dashboard": <json>, "overwrite": true, "message": "..."}`. (Grafana's create and update share an endpoint.) The final response should summarize the created dashboard title/UID/status, not include Python or JSON source.
- **Update:** `grafana_get_dashboard_by_uid` for the existing `uid` and `version` → regenerate via a local builder keeping the same `uid` → call `grafana_update_dashboard`. Grafana enforces optimistic concurrency on `version`.

## Foundation SDK rules (HARD)

- For OpenSearch, queries use the **Elasticsearch** builder (OpenSearch is wire-compatible). Everything you need lives directly in `grafana_foundation_sdk.builders.elasticsearch`. There is **no** `builders.opensearch`, no `builders.dataproxy`, no `builders.elasticsearch.aggs` submodule.
- Panel method is `with_target(builder)` (singular). It calls `.build()` on its argument, so the argument MUST be a builder instance — **never a raw dict**.
- `metrics(...)` and `bucket_aggs(...)` on `Dataquery` also call `.build()` on every list element. Pass **lists of builder objects** (e.g. `Count()`, `DateHistogram()`), never lists of dicts. Lists of dicts produce `AttributeError: 'dict' object has no attribute 'build'`.
- `datasource()` on a panel takes a dict: `{"type": "grafana-opensearch-datasource", "uid": "<uid>"}`. (Dicts are correct here — only `with_target` / `metrics` / `bucket_aggs` require builders.)
- Every OpenSearch `Dataquery` must set `.time_field("<date-field>")`, including stat and raw-document panels, so dashboard time ranges work consistently.
- Time arguments are strings: `.time("now-6h", "now")`. Refresh: `.refresh("30s")`.
- Panel classes live in their own modules: `Panel as Timeseries` from `builders.timeseries`, `Panel as Stat` from `builders.stat`, `Panel as Table` from `builders.table`, `Panel as Bargauge` from `builders.bargauge`, `Panel as PieChart` from `builders.piechart`, and `Row` / `QueryVariable` from `builders.dashboard`. Always alias `Panel` so panel types don't collide.
- Set explicit panel `.id(...)` and `.grid_pos(GridPos(...))` values. Grafana's grid is 24 columns wide; do not let panels overlap or extend beyond x + w = 24.
- Print the final dashboard with `JSONEncoder(sort_keys=True, indent=2).encode(dashboard.build())` — exactly one JSON document on stdout, nothing else.

### Available elasticsearch builders (use only these names)

- Bucket aggregations: `DateHistogram`, `Histogram`, `Terms`, `Filters`, `Filter`, `GeoHashGrid`, `Nested`. Settings builders: `DateHistogramSettings`, `HistogramSettings`, `TermsSettings`, `FiltersSettings`, `GeoHashGridSettings`.
- Metric aggregations: `Count`, `Average`, `Sum`, `Min`, `Max`, `UniqueCount`, `Percentiles`, `ExtendedStats`, `Rate`, `Logs`, `RawData`, `RawDocument`, `TopMetrics`, `MovingAverage`, `MovingFunction`, `Derivative`, `CumulativeSum`, `BucketScript`, `SerialDiff`. Raw document settings: `ElasticsearchRawDataSettings`, `ElasticsearchRawDocumentSettings`.
- Query container: `Dataquery`.

If the chart you need cannot be built from the names above, omit the aggregation rather than inventing a builder.

## Working skeleton (copy and adapt — do not invent imports or method names)

```python
from grafana_foundation_sdk.builders.dashboard import Dashboard
from grafana_foundation_sdk.builders.timeseries import Panel as Timeseries
from grafana_foundation_sdk.builders.stat import Panel as Stat
from grafana_foundation_sdk.builders.elasticsearch import (
    Dataquery,
    Count,
    DateHistogram,
    DateHistogramSettings,
)
from grafana_foundation_sdk.models.dashboard import GridPos
from grafana_foundation_sdk.cog.encoder import JSONEncoder

DS_UID = "<datasource-uid-from-grafana_list_datasources>"
DS = {"type": "grafana-opensearch-datasource", "uid": DS_UID}

# Each Dataquery is one panel target. `query` is a Lucene/OpenSearch query string;
# `metrics` and `bucket_aggs` take BUILDER instances, not dicts.
events_per_minute = (
    Dataquery()
    .ref_id("A")
    .query("*")
    .alias("events")
    .time_field("@timestamp")
    .metrics([Count().id("1")])
    .bucket_aggs([
        DateHistogram()
        .id("2")
        .field("@timestamp")
        .settings(DateHistogramSettings().interval("auto"))
    ])
)

total_events = (
    Dataquery()
    .ref_id("B")
    .query("*")
    .time_field("@timestamp")
    .metrics([Count().id("1")])
    .bucket_aggs([])
)

dashboard = (
    Dashboard("Trades overview")
    .uid("trades-overview")
    .refresh("30s")
    .time("now-6h", "now")
    .with_panel(
        Timeseries()
        .id(1)
        .title("Events per minute")
        .grid_pos(GridPos(h=8, w=16, x=0, y=0))
        .datasource(DS)
        .with_target(events_per_minute)
    )
    .with_panel(
        Stat()
        .id(2)
        .title("Total events (window)")
        .grid_pos(GridPos(h=4, w=8, x=16, y=0))
        .datasource(DS)
        .with_target(total_events)
    )
)

print(JSONEncoder(sort_keys=True, indent=2).encode(dashboard.build()))
```

## Failure handling for `build_dashboard_tool`

When the tool returns "Build failed":
1. Read the traceback. Common causes: typo in a class name; passing a dict where a builder is required (`with_target`, `metrics`, `bucket_aggs` all need builders); importing a name that doesn't exist (use only names from the "Available elasticsearch builders" list above).
2. Fix the *one* thing the traceback points to and resubmit the **complete** dashboard snippet. Do not submit a probe like `print('ok')` to test an import — if you're unsure a name exists, it doesn't; pick one from the list.
3. Maximum 2 retries. After the 2nd retry, stop and report the error and last code to the user.

For all other questions (ad-hoc queries, explaining mappings, summarising data) answer normally with concise, useful prose.
"""


def _dashboard_instructions(opensearch_uid: str) -> str:
    if opensearch_uid:
        hint = (
            f"\n## Default datasource hint\n\n"
            f"If `grafana_list_datasources` is unavailable, fall back to UID `{opensearch_uid}`.\n"
        )
        return _DASHBOARD_INSTRUCTIONS_STATIC + hint
    return _DASHBOARD_INSTRUCTIONS_STATIC


@dataclass
class ToolExecution:
    tool: str
    parameters: dict[str, Any]
    result: str


@dataclass
class AgentDeps:
    workspace_root: Path
    tool_calls: list[ToolExecution] = field(default_factory=list)


class AgentGraphState(TypedDict, total=False):
    user_message: str
    history_payload: list[Any]
    legacy_context: str
    message_history: list[ModelMessage]
    response: str
    tool_calls: list[dict[str, Any]]
    updated_history: list[Any]


class PydanticGraphAgent:
    """Conversation agent implemented as a LangGraph workflow around a PydanticAI agent."""

    def __init__(
        self,
        model_name: str,
        provider_name: str = "ollama",
        provider_base_url: str | None = None,
        workspace_root: str | None = None,
    ) -> None:
        self.model = model_name
        self.provider_name = provider_name
        self.provider_base_url = provider_base_url
        self.workspace_root = Path(workspace_root or os.getcwd()).resolve()
        self._agent = self._build_agent()
        self._graph = self._build_graph()

    def _build_model(self) -> Any:
        if self.provider_name == "ollama":
            base_url = (
                self.provider_base_url or os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"
            ).rstrip("/")
            if not base_url.endswith("/v1"):
                base_url = f"{base_url}/v1"
            self.provider_base_url = base_url
            return OpenAIChatModel(
                model_name=self.model,
                provider=OllamaProvider(base_url=base_url),
            )

        return f"{self.provider_name}:{self.model}"

    def _build_agent(self) -> Agent[AgentDeps, str]:
        opensearch_uid = os.getenv("OPENSEARCH_DATASOURCE_UID", "opensearch-default")
        agent = Agent[AgentDeps, str](
            self._build_model(),
            deps_type=AgentDeps,
            toolsets=mcp_toolsets(process_tool_call_factory=self._mcp_tool_recorder_for),
            instructions=_dashboard_instructions(opensearch_uid),
        )
        agent.tool(build_and_save_opensearch_chart_dashboard_tool)
        agent.tool(build_and_save_opensearch_planned_dashboard_tool)
        agent.tool(build_and_save_opensearch_dashboard_tool)
        agent.tool(build_opensearch_chart_dashboard_tool)
        agent.tool(build_opensearch_planned_dashboard_tool)
        agent.tool(build_opensearch_dashboard_tool)
        agent.tool(build_dashboard_tool)
        agent.tool(summarize_opensearch_mapping_tool)
        return agent

    def _build_graph(self):
        builder = StateGraph(AgentGraphState)
        builder.add_node("prepare_context", traced_node("prepare_context")(self._prepare_context))
        builder.add_node("run_agent", traced_node("run_agent")(self._run_agent))
        builder.add_node(
            "finalize_response", traced_node("finalize_response")(self._finalize_response)
        )
        builder.add_edge(START, "prepare_context")
        builder.add_edge("prepare_context", "run_agent")
        builder.add_edge("run_agent", "finalize_response")
        builder.add_edge("finalize_response", END)
        return builder.compile()

    def _record_tool(
        self,
        ctx: RunContext[AgentDeps],
        tool_name: str,
        parameters: dict[str, Any],
        result: str,
    ) -> None:
        ctx.deps.tool_calls.append(
            ToolExecution(tool=tool_name, parameters=parameters, result=result)
        )

    def _mcp_tool_recorder_for(self, server_name: str):
        async def process_tool_call(
            ctx: RunContext[AgentDeps],
            call_tool: CallToolFunc,
            name: str,
            tool_args: dict[str, Any],
        ) -> ToolResult:
            display_name = name if name.startswith(f"{server_name}_") else f"{server_name}_{name}"
            try:
                result = await call_tool(name, tool_args, None)
            except Exception as exc:
                self._record_tool(ctx, display_name, tool_args, f"Error: {exc}")
                raise

            self._record_tool(ctx, display_name, tool_args, self._tool_result_to_text(result))
            return result

        return process_tool_call

    @staticmethod
    def _tool_result_to_text(result: ToolResult) -> str:
        content = getattr(result, "content", None)
        if not content:
            return str(result)

        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(str(text))
            else:
                parts.append(json.dumps(item, default=str))
        return "\n".join(parts)

    def _prepare_context(self, state: AgentGraphState) -> AgentGraphState:
        history_payload = state.get("history_payload") or []
        if not history_payload:
            return {"message_history": [], "legacy_context": ""}

        try:
            message_history = cast(
                list[ModelMessage], ModelMessagesTypeAdapter.validate_python(history_payload)
            )
            return {"message_history": message_history, "legacy_context": ""}
        except Exception:
            legacy_context = self._build_legacy_context(history_payload)
            return {"message_history": [], "legacy_context": legacy_context}

    async def _run_agent(self, state: AgentGraphState) -> AgentGraphState:
        deps = AgentDeps(workspace_root=self.workspace_root)
        prompt = state["user_message"]
        legacy_context = state.get("legacy_context", "").strip()
        if legacy_context:
            prompt = (
                "The following transcript was produced by the previous agent runtime. "
                "Use it as prior conversation context.\n\n"
                f"{legacy_context}\n\n"
                f"User message: {state['user_message']}"
            )

        # Cap LLM iterations so a model that gets stuck retrying a broken tool
        # call (e.g. `build_dashboard_tool` with the wrong SDK API) cannot loop
        # forever. 25 covers the typical 6-call dashboard flow with headroom
        # for two `build_dashboard_tool` retries.
        request_limit = int(os.getenv("AGENT_REQUEST_LIMIT", "25"))

        try:
            async with self._agent:
                result = await self._agent.run(
                    prompt,
                    deps=deps,
                    message_history=state.get("message_history") or None,
                    usage_limits=UsageLimits(request_limit=request_limit),
                )
        except ValidationError as exc:
            logfire.error(
                "pydantic_ai output failed validation",
                node="run_agent",
                error_count=len(exc.errors()),
                errors=exc.errors(),
            )
            raise

        return {
            "response": str(result.output),
            "tool_calls": [
                {
                    "tool": tool_call.tool,
                    "parameters": tool_call.parameters,
                    "result": tool_call.result,
                }
                for tool_call in deps.tool_calls
            ],
            "updated_history": json.loads(result.all_messages_json().decode("utf-8")),
        }

    def _finalize_response(self, state: AgentGraphState) -> AgentGraphState:
        response = self._guard_dashboard_response(
            user_message=state.get("user_message", ""),
            response=state.get("response", ""),
            tool_calls=state.get("tool_calls", []),
        )
        return {
            "response": response,
            "tool_calls": state.get("tool_calls", []),
            "updated_history": state.get("updated_history", []),
        }

    def _guard_dashboard_response(
        self,
        *,
        user_message: str,
        response: str,
        tool_calls: list[dict[str, Any]],
    ) -> str:
        """Prevent dashboard-create turns from ending as source-code workarounds."""
        if not _DASHBOARD_INTENT_RE.search(user_message):
            return response

        update_succeeded = any(
            (
                call.get("tool") == "grafana_update_dashboard"
                and not str(call.get("result", "")).lower().startswith("error")
            )
            or (
                call.get("tool") == "build_and_save_opensearch_dashboard_tool"
                and str(call.get("result", "")).startswith("Dashboard save succeeded")
            )
            or (
                call.get("tool") == "build_and_save_opensearch_chart_dashboard_tool"
                and str(call.get("result", "")).startswith("Dashboard save succeeded")
            )
            or (
                call.get("tool") == "build_and_save_opensearch_planned_dashboard_tool"
                and str(call.get("result", "")).startswith("Dashboard save succeeded")
            )
            for call in tool_calls
        )
        if update_succeeded:
            if not _SOURCE_CODE_BLOCK_RE.search(response):
                return response
            return (
                "Dashboard update completed successfully. I saved the dashboard in Grafana; "
                "the final response was trimmed because dashboard-create turns should report "
                "the saved dashboard status rather than source code."
            )

        dashboard_build_calls = [
            call
            for call in tool_calls
            if call.get("tool")
            in {
                "build_dashboard_tool",
                "build_opensearch_chart_dashboard_tool",
                "build_and_save_opensearch_chart_dashboard_tool",
                "build_opensearch_planned_dashboard_tool",
                "build_and_save_opensearch_planned_dashboard_tool",
                "build_opensearch_dashboard_tool",
                "build_and_save_opensearch_dashboard_tool",
            }
        ]
        latest_failure = next(
            (
                str(call.get("result", ""))
                for call in reversed(dashboard_build_calls)
                if str(call.get("result", "")).startswith("Build failed")
            ),
            "",
        )
        build_succeeded = any(
            str(call.get("result", "")).startswith("Dashboard build succeeded")
            for call in dashboard_build_calls
        )
        latest_update_failure = next(
            (
                str(call.get("result", ""))
                for call in reversed(tool_calls)
                if (
                    call.get("tool") == "grafana_update_dashboard"
                    and str(call.get("result", "")).lower().startswith("error")
                )
                or (
                    call.get("tool") == "build_and_save_opensearch_dashboard_tool"
                    and str(call.get("result", "")).startswith(
                        ("Dashboard save failed", "Dashboard save or verification failed")
                    )
                )
                or (
                    call.get("tool") == "build_and_save_opensearch_chart_dashboard_tool"
                    and str(call.get("result", "")).startswith(
                        ("Dashboard save failed", "Dashboard save or verification failed")
                    )
                )
                or (
                    call.get("tool") == "build_and_save_opensearch_planned_dashboard_tool"
                    and str(call.get("result", "")).startswith(
                        ("Dashboard save failed", "Dashboard save or verification failed")
                    )
                )
            ),
            "",
        )
        attempted_source_code_answer = bool(_SOURCE_CODE_BLOCK_RE.search(response))

        if latest_failure:
            logfire.warning(
                "dashboard response guarded after build failure",
                failure=latest_failure[:1000],
            )
            return (
                "I could not create the Grafana dashboard because the dashboard build failed, "
                "and no dashboard was saved. The next retry should fix the builder arguments "
                "or SDK usage and then call `grafana_update_dashboard`.\n\n"
                f"{latest_failure[:1800]}"
            )

        if latest_update_failure:
            logfire.warning(
                "dashboard response guarded after update failure",
                failure=latest_update_failure[:1000],
            )
            return (
                "I built the dashboard flow far enough to attempt saving it, but Grafana "
                "rejected the update. No dashboard was saved successfully.\n\n"
                f"{latest_update_failure[:1800]}"
            )

        if build_succeeded:
            logfire.warning("dashboard response guarded after unsaved dashboard build")
            return (
                "I built a dashboard JSON successfully, but it was not saved to Grafana because "
                "`grafana_update_dashboard` was not called. No dashboard was created or updated. "
                "Retry the turn and complete the required build → update flow."
            )

        if attempted_source_code_answer:
            logfire.warning("dashboard response guarded after source-code final answer")
            return (
                "I did not create or update a Grafana dashboard. The response attempted to return "
                "dashboard source code instead of completing the build → `grafana_update_dashboard` "
                "flow, so it was blocked."
            )

        return response

    def _build_legacy_context(self, history_payload: list[Any]) -> str:
        transcript_lines: list[str] = []
        for item in history_payload[-12:]:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant", "system"} and isinstance(content, str):
                transcript_lines.append(f"{role.title()}: {content}")
        return "\n".join(transcript_lines)

    async def send_message(
        self, message: str, conversation_history: list[Any] | None = None
    ) -> dict[str, Any]:
        with logfire.span(
            "agent.send_message",
            model=self.model,
            provider=self.provider_name,
            history_len=len(conversation_history or []),
            _tags=["agent", "turn"],
        ):
            result = await self._graph.ainvoke(
                {
                    "user_message": message,
                    "history_payload": conversation_history or [],
                }
            )
            return {
                "content": result["response"],
                "tool_calls": result.get("tool_calls", []),
                "conversation_history": result.get("updated_history", []),
            }
