"""Subprocess-backed Foundation-SDK dashboard builder, exposed as a Pydantic AI tool."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import logfire
from pydantic_ai import RunContext

DEFAULT_TIMEOUT_S = 20
DEFAULT_GRAFANA_TIMEOUT_S = 20

# Lines emitted by debugpy / pydevd when our parent process has it loaded.
# They leak into the child's stderr and confuse the model into "fixing" things
# that aren't broken. Filter them so the model only sees real tracebacks.
_PYDEVD_NOISE = re.compile(
    r"^(\s*\d+\.\d+s - |Note: Debugging will proceed|.*frozen modules|.*PYDEVD_DISABLE_FILE_VALIDATION).*$",
    re.MULTILINE,
)


class DashboardCodeError(RuntimeError):
    """Raised when the LLM-supplied code fails to produce valid dashboard JSON."""

    def __init__(self, message: str, *, stderr: str = "", returncode: int | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


@dataclass
class DashboardBuildResult:
    json_text: str
    parsed: dict[str, Any]
    stdout: str
    stderr: str


@dataclass
class DashboardVerificationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]
    checked_panels: int = 0
    queried_panels: int = 0
    panels_with_data: int = 0


_DATA_PANEL_TYPES = {
    "bargauge",
    "barchart",
    "gauge",
    "piechart",
    "stat",
    "table",
    "timeseries",
}

_FORBIDDEN_DASHBOARD_CODE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"grafana_foundation_sdk\.models\.templating"),
        "templating classes do not live in grafana_foundation_sdk.models.templating",
    ),
    (re.compile(r"\bTemplating\b"), "Templating is not a supported Foundation SDK builder"),
    (re.compile(r"\bVariable\b"), "Variable is not a supported Foundation SDK builder"),
    (re.compile(r"\bMetricAggregation\b"), "MetricAggregation is not a supported builder"),
    (re.compile(r"\bBucketAggregation\b"), "BucketAggregation is not a supported builder"),
    (
        re.compile(r"grafana_foundation_sdk\.builders\.opensearch"),
        "OpenSearch dashboards must use grafana_foundation_sdk.builders.elasticsearch",
    ),
    (
        re.compile(r"grafana_foundation_sdk\.builders\.elasticsearch\.aggs"),
        "Elasticsearch builders are exposed directly from builders.elasticsearch",
    ),
    (
        re.compile(r"grafana_foundation_sdk\.builders\.dataproxy"),
        "dataproxy is not part of the supported dashboard builder surface",
    ),
)

_NUMERIC_MAPPING_TYPES = {
    "byte",
    "double",
    "float",
    "half_float",
    "integer",
    "long",
    "scaled_float",
    "short",
}


@dataclass(frozen=True)
class VariableSpec:
    name: str
    label: str
    field: str


def _dashboard_validation_errors(dashboard: dict[str, Any]) -> list[str]:
    """Return structural problems that usually make Grafana panels empty or broken."""
    errors: list[str] = []

    if not isinstance(dashboard.get("title"), str) or not dashboard["title"].strip():
        errors.append("Dashboard must have a non-empty title.")

    if not isinstance(dashboard.get("uid"), str) or not dashboard["uid"].strip():
        errors.append("Dashboard must have a stable non-empty uid.")

    panels = dashboard.get("panels")
    if not isinstance(panels, list) or not panels:
        errors.append("Dashboard must contain at least one panel.")
        return errors

    try:
        min_panels = max(1, int(os.getenv("DASHBOARD_MIN_PANEL_COUNT", "1")))
    except ValueError:
        min_panels = 1
    if len(panels) < min_panels:
        errors.append(
            f"Dashboard has {len(panels)} panel(s), but DASHBOARD_MIN_PANEL_COUNT={min_panels}."
        )

    seen_ids: set[int] = set()
    for index, panel in enumerate(panels, start=1):
        if not isinstance(panel, dict):
            errors.append(f"Panel #{index} must be a JSON object.")
            continue

        panel_label = panel.get("title") or f"#{index}"
        panel_type = panel.get("type")
        if not isinstance(panel_type, str) or not panel_type:
            errors.append(f"Panel {panel_label!r} is missing type.")

        panel_id = panel.get("id")
        if not isinstance(panel_id, int):
            errors.append(f"Panel {panel_label!r} is missing an integer id.")
        elif panel_id in seen_ids:
            errors.append(f"Panel {panel_label!r} reuses duplicate id {panel_id}.")
        else:
            seen_ids.add(panel_id)

        if not isinstance(panel.get("title"), str) or not panel["title"].strip():
            errors.append(f"Panel #{index} is missing a non-empty title.")

        grid_pos = panel.get("gridPos")
        if not isinstance(grid_pos, dict):
            errors.append(f"Panel {panel_label!r} is missing gridPos.")
        else:
            for key in ("h", "w", "x", "y"):
                if not isinstance(grid_pos.get(key), int):
                    errors.append(f"Panel {panel_label!r} gridPos.{key} must be an integer.")
            width = grid_pos.get("w")
            x_pos = grid_pos.get("x")
            height = grid_pos.get("h")
            if isinstance(width, int) and (width <= 0 or width > 24):
                errors.append(f"Panel {panel_label!r} gridPos.w must be between 1 and 24.")
            if isinstance(height, int) and height <= 0:
                errors.append(f"Panel {panel_label!r} gridPos.h must be positive.")
            if isinstance(width, int) and isinstance(x_pos, int) and x_pos + width > 24:
                errors.append(f"Panel {panel_label!r} extends beyond Grafana's 24-column grid.")

        if panel_type not in _DATA_PANEL_TYPES:
            continue

        datasource = panel.get("datasource")
        targets = panel.get("targets")
        if not isinstance(datasource, dict) or not datasource.get("uid"):
            errors.append(f"Panel {panel_label!r} must set datasource.uid.")
        if not isinstance(targets, list) or not targets:
            errors.append(f"Panel {panel_label!r} must contain at least one target.")
            continue

        is_opensearch_panel = (
            isinstance(datasource, dict)
            and datasource.get("type") == "grafana-opensearch-datasource"
        )
        for target_index, target in enumerate(targets, start=1):
            if not isinstance(target, dict):
                errors.append(f"Panel {panel_label!r} target #{target_index} must be an object.")
                continue
            target_label = target.get("refId") or f"#{target_index}"

            if not target.get("refId"):
                errors.append(f"Panel {panel_label!r} target {target_label!r} is missing refId.")
            if "query" not in target:
                errors.append(f"Panel {panel_label!r} target {target_label!r} is missing query.")

            looks_like_opensearch = is_opensearch_panel or any(
                key in target for key in ("bucketAggs", "metrics", "timeField")
            )
            if not looks_like_opensearch:
                continue

            if not isinstance(target.get("timeField"), str) or not target["timeField"].strip():
                errors.append(
                    f"Panel {panel_label!r} target {target_label!r} must set timeField."
                )

            metrics = target.get("metrics")
            if not isinstance(metrics, list) or not metrics:
                errors.append(
                    f"Panel {panel_label!r} target {target_label!r} must define metrics."
                )
            else:
                for metric in metrics:
                    if (
                        not isinstance(metric, dict)
                        or not metric.get("id")
                        or not metric.get("type")
                    ):
                        errors.append(
                            f"Panel {panel_label!r} target {target_label!r} has an invalid metric."
                        )

            bucket_aggs = target.get("bucketAggs")
            if bucket_aggs is not None and not isinstance(bucket_aggs, list):
                errors.append(
                    f"Panel {panel_label!r} target {target_label!r} bucketAggs must be a list."
                )

    return errors


def _validate_dashboard_model(dashboard: dict[str, Any]) -> None:
    errors = _dashboard_validation_errors(dashboard)
    if errors:
        raise DashboardCodeError(
            "Dashboard JSON failed quality checks:\n- " + "\n- ".join(errors)
        )


def _rich_opensearch_dashboard_errors(
    dashboard: dict[str, Any],
    *,
    require_variables: bool,
    min_stat_panels: int,
    min_trend_panels: int,
    require_distribution_panel: bool,
    require_bar_panel: bool,
    require_raw_table: bool,
) -> list[str]:
    """Return quality-bar errors for deterministic OpenSearch dashboards."""
    errors: list[str] = []
    panels = dashboard.get("panels")
    if not isinstance(panels, list):
        return ["Dashboard panels must be a list."]

    data_panels = [panel for panel in panels if panel.get("type") in _DATA_PANEL_TYPES]
    stat_panels = [panel for panel in data_panels if panel.get("type") == "stat"]
    trend_panels = [panel for panel in data_panels if panel.get("type") == "timeseries"]
    pie_panels = [panel for panel in data_panels if panel.get("type") == "piechart"]
    bar_panels = [
        panel for panel in data_panels if panel.get("type") in {"bargauge", "barchart"}
    ]
    row_panels = [panel for panel in panels if panel.get("type") == "row"]

    if len(row_panels) < 3:
        errors.append("Dashboard must organize panels with at least three row panels.")
    if len(stat_panels) < min_stat_panels:
        errors.append(
            f"Dashboard must contain at least {min_stat_panels} stat KPI panels."
        )
    if len(trend_panels) < min_trend_panels:
        errors.append(
            f"Dashboard must contain at least {min_trend_panels} telemetry trend panels."
        )
    if require_distribution_panel and not pie_panels:
        errors.append("Dashboard must contain a piechart distribution panel.")
    if require_bar_panel and not bar_panels:
        errors.append("Dashboard must contain a bargauge or barchart grouped metric panel.")

    if require_raw_table:
        has_raw_table = False
        for panel in data_panels:
            if panel.get("type") != "table":
                continue
            for target in panel.get("targets") or []:
                for metric in target.get("metrics") or []:
                    if metric.get("type") in {"raw_data", "raw_document"}:
                        has_raw_table = True
        if not has_raw_table:
            errors.append("Dashboard must contain a raw-document table panel.")

    templating = dashboard.get("templating")
    variables = []
    if isinstance(templating, dict):
        variables = templating.get("list") or []
    if require_variables:
        if not isinstance(variables, list) or len(variables) < 2:
            errors.append("Dashboard must contain at least two template variables.")
        else:
            queries = " ".join(
                str(target.get("query", ""))
                for panel in data_panels
                for target in (panel.get("targets") or [])
                if isinstance(target, dict)
            )
            for variable in variables[:2]:
                name = variable.get("name") if isinstance(variable, dict) else None
                if not name:
                    errors.append("Dashboard variable is missing a name.")
                    continue
                if "${" + name + ":" not in queries and "$" + name not in queries:
                    errors.append(
                        f"Dashboard variable {name!r} is not wired into panel queries."
                    )

    return errors


def _validate_rich_opensearch_dashboard(
    dashboard: dict[str, Any], *, require_variables: bool
) -> None:
    errors = _rich_opensearch_dashboard_errors(
        dashboard,
        require_variables=require_variables,
        min_stat_panels=4,
        min_trend_panels=2,
        require_distribution_panel=True,
        require_bar_panel=True,
        require_raw_table=True,
    )
    if errors:
        raise DashboardCodeError(
            "OpenSearch dashboard failed content quality checks:\n- " + "\n- ".join(errors)
        )


def _slugify_uid(value: str) -> str:
    uid = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")
    uid = re.sub(r"-{2,}", "-", uid)
    return (uid or "opensearch-dashboard")[:80]


def _display_name(field: str) -> str:
    field = field.removesuffix(".keyword")
    field = field.replace("_", " ").replace(".", " ")
    return field.title()


def _variable_name_for_field(field: str, existing: set[str]) -> str:
    base = field.removesuffix(".keyword").replace(".", "_")
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", base).strip("_").lower() or "filter"
    original = name
    suffix = 2
    while name in existing:
        name = f"{original}_{suffix}"
        suffix += 1
    existing.add(name)
    return name


def _variable_specs(fields: list[str], *, limit: int = 2) -> list[VariableSpec]:
    existing: set[str] = set()
    specs: list[VariableSpec] = []
    for field in _rank_categorical_fields(fields)[:limit]:
        specs.append(
            VariableSpec(
                name=_variable_name_for_field(field, existing),
                label=_display_name(field),
                field=field,
            )
        )
    return specs


def _query_with_variable_filters(
    query: str,
    variables: list[VariableSpec],
    *,
    exclude_fields: set[str] | None = None,
) -> str:
    query = query.strip() or "*"
    if not variables:
        return query

    excluded = exclude_fields or set()
    filters = [
        f"{spec.field}:${{{spec.name}:regex}}"
        for spec in variables
        if spec.field not in excluded
    ]
    if not filters:
        return query
    if query == "*":
        return " AND ".join(filters)
    return f"({query}) AND " + " AND ".join(filters)


def _unit_for_field(field: str) -> str | None:
    normalized = field.lower()
    if "temperature" in normalized and normalized.endswith("_c"):
        return "celsius"
    if "humidity" in normalized:
        return "humidity"
    if normalized.endswith("_pct") or "percent" in normalized:
        return "percent"
    if normalized.endswith("_kw"):
        return "kwatt"
    if normalized.endswith("_v") or "voltage" in normalized:
        return "volt"
    if normalized.endswith("_a") or "current" in normalized:
        return "amp"
    if normalized.endswith("_ppm"):
        return "ppm"
    if "pressure" in normalized and normalized.endswith("_kpa"):
        return "pressurekpa"
    if "flow" in normalized and normalized.endswith("_lpm"):
        return "lpm"
    if "vibration" in normalized:
        return "velocitymms"
    return None


def _field_priority(field: str, priorities: tuple[str, ...]) -> tuple[int, int, str]:
    normalized = field.removesuffix(".keyword").lower()
    for index, token in enumerate(priorities):
        if token in normalized:
            return (index, len(normalized), normalized)
    return (len(priorities), 0, "")


def _rank_numeric_fields(fields: list[str]) -> list[str]:
    priorities = (
        "temperature",
        "power",
        "vibration",
        "pressure",
        "flow",
        "voltage",
        "current",
        "humidity",
        "oxygen",
        "co2",
    )
    indexed = list(enumerate(fields))
    return [
        field
        for _, field in sorted(
            indexed,
            key=lambda item: (_field_priority(item[1], priorities), item[0]),
        )
    ]


def _rank_categorical_fields(fields: list[str]) -> list[str]:
    priorities = (
        "equipment_group",
        "system_state",
        "state",
        "status",
        "site",
        "location",
        "equipment",
        "group",
    )
    indexed = list(enumerate(fields))
    return [
        field
        for _, field in sorted(
            indexed,
            key=lambda item: (_field_priority(item[1], priorities), item[0]),
        )
    ]


def _record_local_tool_call(
    ctx: RunContext[Any] | None,
    tool_name: str,
    parameters: dict[str, Any],
    result: str,
) -> None:
    deps = getattr(ctx, "deps", None)
    calls = getattr(deps, "tool_calls", None)
    if isinstance(calls, list):
        calls.append(
            SimpleNamespace(tool=tool_name, parameters=parameters, result=result)
        )


def _grafana_api_url(path: str) -> str:
    grafana_url = os.getenv("GRAFANA_URL", "http://grafana:3000").rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return f"{grafana_url}{path}"


def _grafana_dashboard_api_url() -> str:
    return _grafana_api_url("/api/dashboards/db")


def _grafana_request(
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout_s: int = DEFAULT_GRAFANA_TIMEOUT_S,
) -> dict[str, Any]:
    token = os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not token:
        raise DashboardCodeError(
            "GRAFANA_SERVICE_ACCOUNT_TOKEN is required to call Grafana APIs."
        )

    request = urllib.request.Request(
        _grafana_api_url(path),
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise DashboardCodeError(
            f"Grafana API {method} {path} failed with HTTP {exc.code}: {response_body[:1000]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DashboardCodeError(f"Grafana API {method} {path} failed: {exc}") from exc

    if not response_body.strip():
        return {}
    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise DashboardCodeError(
            f"Grafana API {method} {path} returned non-JSON response: {response_body[:1000]}"
        ) from exc
    if not isinstance(parsed, dict):
        raise DashboardCodeError(f"Grafana API {method} {path} returned an invalid response.")
    return parsed


def _post_grafana_dashboard(
    dashboard: dict[str, Any],
    *,
    folder_uid: str | None = None,
    message: str = "",
    overwrite: bool = True,
    timeout_s: int = DEFAULT_GRAFANA_TIMEOUT_S,
) -> dict[str, Any]:
    token = os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not token:
        raise DashboardCodeError(
            "GRAFANA_SERVICE_ACCOUNT_TOKEN is required to save dashboards directly."
        )

    payload: dict[str, Any] = {
        "dashboard": dashboard,
        "overwrite": overwrite,
        "message": message or f"Save {dashboard.get('title', 'dashboard')}",
    }
    if folder_uid:
        payload["folderUid"] = folder_uid

    request = urllib.request.Request(
        _grafana_dashboard_api_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise DashboardCodeError(
            f"Grafana dashboard save failed with HTTP {exc.code}: {body[:1000]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DashboardCodeError(f"Grafana dashboard save failed: {exc}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DashboardCodeError(
            f"Grafana dashboard save returned non-JSON response: {body[:1000]}"
        ) from exc
    if not isinstance(parsed, dict):
        raise DashboardCodeError("Grafana dashboard save returned an invalid response.")
    return parsed


def _get_grafana_dashboard(uid: str) -> dict[str, Any]:
    encoded_uid = urllib.parse.quote(uid, safe="")
    response = _grafana_request(f"/api/dashboards/uid/{encoded_uid}")
    dashboard = response.get("dashboard")
    if not isinstance(dashboard, dict):
        raise DashboardCodeError(f"Grafana did not return dashboard JSON for uid={uid!r}.")
    return dashboard


def _datasource_query_payload(
    target: dict[str, Any],
    *,
    panel: dict[str, Any],
    time_from: str,
    time_to: str,
) -> dict[str, Any]:
    query = json.loads(json.dumps(target))
    datasource = query.get("datasource") or panel.get("datasource")
    if isinstance(datasource, dict):
        query["datasource"] = datasource
    query.setdefault("intervalMs", 60000)
    query.setdefault("maxDataPoints", 500)
    return {
        "queries": [query],
        "range": {
            "from": time_from,
            "to": time_to,
            "raw": {"from": time_from, "to": time_to},
        },
        "from": time_from,
        "to": time_to,
    }


def _query_result_status(result: dict[str, Any], ref_id: str) -> tuple[bool, bool, str]:
    results = result.get("results")
    if not isinstance(results, dict):
        return False, False, "Grafana query response did not contain a results object."
    ref_result = results.get(ref_id)
    if not isinstance(ref_result, dict):
        return False, False, f"Grafana query response did not contain result for refId {ref_id!r}."

    error = ref_result.get("error") or ref_result.get("errorSource")
    if error:
        return False, False, str(error)
    status = ref_result.get("status")
    if isinstance(status, int) and status >= 400:
        return False, False, f"Grafana query returned status {status}."

    frames = ref_result.get("frames")
    if not isinstance(frames, list) or not frames:
        return True, False, ""
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        data = frame.get("data")
        if not isinstance(data, dict):
            continue
        values = data.get("values")
        if isinstance(values, list) and any(bool(value) for value in values):
            return True, True, ""
    return True, False, ""


def _target_static_errors(panel: dict[str, Any], target: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    title = panel.get("title") or panel.get("id") or "untitled"
    ref_id = target.get("refId") or "?"

    datasource = target.get("datasource")
    if not isinstance(datasource, dict) or not datasource.get("uid"):
        errors.append(f"Panel {title!r} target {ref_id!r} is missing target datasource.")

    if not target.get("timeField"):
        errors.append(f"Panel {title!r} target {ref_id!r} is missing timeField.")

    query = target.get("query")
    if isinstance(query, str):
        if ":lucene}" in query:
            errors.append(f"Panel {title!r} target {ref_id!r} uses unsupported lucene variable formatting.")
        if re.search(r":\(\$\{[^}:]+:regex\}\)", query):
            errors.append(f"Panel {title!r} target {ref_id!r} wraps regex variable filters in parentheses.")

    for metric in target.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        if metric.get("type") == "raw_document":
            errors.append(f"Panel {title!r} target {ref_id!r} uses raw_document instead of raw_data.")

    return errors


def _verify_dashboard_saved_and_working(
    uid: str,
    expected_dashboard: dict[str, Any],
    *,
    execute_queries: bool = True,
) -> DashboardVerificationResult:
    errors: list[str] = []
    warnings: list[str] = []
    checked_panels = 0
    queried_panels = 0
    panels_with_data = 0

    try:
        saved_dashboard = _get_grafana_dashboard(uid)
    except DashboardCodeError as exc:
        return DashboardVerificationResult(
            valid=False,
            errors=[f"Saved dashboard could not be fetched: {exc}"],
            warnings=[],
        )

    if saved_dashboard.get("uid") != expected_dashboard.get("uid"):
        errors.append(
            f"Fetched dashboard uid {saved_dashboard.get('uid')!r} did not match expected {expected_dashboard.get('uid')!r}."
        )

    expected_panel_count = len(expected_dashboard.get("panels") or [])
    saved_panels = saved_dashboard.get("panels")
    if not isinstance(saved_panels, list):
        errors.append("Saved dashboard panels are missing or invalid.")
        saved_panels = []
    elif len(saved_panels) != expected_panel_count:
        errors.append(
            f"Saved dashboard has {len(saved_panels)} panel(s), expected {expected_panel_count}."
        )

    errors.extend(_dashboard_validation_errors(saved_dashboard))

    time_range = saved_dashboard.get("time") if isinstance(saved_dashboard.get("time"), dict) else {}
    time_from = str(time_range.get("from") or (expected_dashboard.get("time") or {}).get("from") or "now-6h")
    time_to = str(time_range.get("to") or (expected_dashboard.get("time") or {}).get("to") or "now")

    for panel in saved_panels:
        if not isinstance(panel, dict) or panel.get("type") not in _DATA_PANEL_TYPES:
            continue
        checked_panels += 1
        targets = panel.get("targets")
        if not isinstance(targets, list):
            continue
        panel_had_data = False
        for target in targets:
            if not isinstance(target, dict):
                continue
            errors.extend(_target_static_errors(panel, target))
            if not execute_queries:
                continue
            datasource = target.get("datasource") or panel.get("datasource")
            if not (
                isinstance(datasource, dict)
                and datasource.get("type") == "grafana-opensearch-datasource"
            ):
                continue
            try:
                result = _grafana_request(
                    "/api/ds/query",
                    method="POST",
                    body=_datasource_query_payload(
                        target,
                        panel=panel,
                        time_from=time_from,
                        time_to=time_to,
                    ),
                )
            except DashboardCodeError as exc:
                message = str(exc)
                if "HTTP 400" in message:
                    errors.append(f"Panel {panel.get('title')!r} query failed: {message}")
                else:
                    warnings.append(f"Panel {panel.get('title')!r} query verification skipped: {message}")
                continue
            queried_panels += 1
            ok, has_data, message = _query_result_status(result, str(target.get("refId") or "A"))
            if not ok:
                errors.append(f"Panel {panel.get('title')!r} query failed: {message}")
            elif has_data:
                panel_had_data = True
        if panel_had_data:
            panels_with_data += 1

    if execute_queries and queried_panels and panels_with_data == 0:
        message = "Grafana queries ran, but no panel returned data for the selected time range."
        if os.getenv("DASHBOARD_VERIFY_REQUIRE_DATA", "").lower() in {"1", "true", "yes"}:
            errors.append(message)
        else:
            warnings.append(message)

    return DashboardVerificationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        checked_panels=checked_panels,
        queried_panels=queried_panels,
        panels_with_data=panels_with_data,
    )


def _stringify_agg_settings(settings: Any) -> None:
    if not isinstance(settings, dict):
        return
    for key in ("size", "min_doc_count", "trimEdges"):
        if key in settings and not isinstance(settings[key], str):
            settings[key] = str(settings[key])


def _repair_dashboard_for_verification(
    dashboard: dict[str, Any],
    verification: DashboardVerificationResult,
) -> tuple[dict[str, Any], list[str]]:
    repaired = json.loads(json.dumps(dashboard))
    actions: list[str] = []

    for panel in repaired.get("panels") or []:
        if not isinstance(panel, dict) or panel.get("type") not in _DATA_PANEL_TYPES:
            continue
        panel_datasource = panel.get("datasource")
        for target in panel.get("targets") or []:
            if not isinstance(target, dict):
                continue

            if isinstance(panel_datasource, dict) and not isinstance(target.get("datasource"), dict):
                target["datasource"] = panel_datasource
                actions.append("added target datasource")

            if not target.get("timeField"):
                target["timeField"] = "@timestamp"
                actions.append("added missing timeField")

            query = target.get("query")
            if isinstance(query, str):
                fixed_query = query
                fixed_query = re.sub(r"\$\{([^}:]+):lucene\}", r"${\1:regex}", fixed_query)
                fixed_query = re.sub(r":\(\$\{([^}:]+):regex\}\)", r":${\1:regex}", fixed_query)
                if fixed_query != query:
                    target["query"] = fixed_query
                    actions.append("normalized variable interpolation")

            for metric in target.get("metrics") or []:
                if not isinstance(metric, dict):
                    continue
                if metric.get("type") == "raw_document":
                    metric["type"] = "raw_data"
                    actions.append("changed raw_document metric to raw_data")
                _stringify_agg_settings(metric.get("settings"))

            for bucket in target.get("bucketAggs") or []:
                if not isinstance(bucket, dict):
                    continue
                settings = bucket.setdefault("settings", {})
                if bucket.get("type") == "date_histogram" and isinstance(settings, dict):
                    settings.setdefault("min_doc_count", "0")
                    settings.setdefault("trimEdges", "0")
                _stringify_agg_settings(settings)

    return repaired, sorted(set(actions))


def _save_dashboard_with_verification(
    dashboard: dict[str, Any],
    *,
    folder_uid: str | None = None,
    message: str = "",
    overwrite: bool = True,
    verify: bool = True,
    max_repair_attempts: int = 2,
) -> tuple[dict[str, Any], dict[str, Any], DashboardVerificationResult]:
    max_repair_attempts = max(0, max_repair_attempts)
    current_dashboard = dashboard
    repair_history: list[str] = []
    last_save_result: dict[str, Any] | None = None
    last_verification = DashboardVerificationResult(
        valid=not verify,
        errors=[],
        warnings=[],
    )

    for attempt in range(max_repair_attempts + 1):
        last_save_result = _post_grafana_dashboard(
            current_dashboard,
            folder_uid=folder_uid,
            message=message or f"Save {current_dashboard.get('title', 'dashboard')}",
            overwrite=overwrite,
        )
        if not verify:
            return last_save_result, current_dashboard, last_verification

        saved_uid = str(last_save_result.get("uid") or current_dashboard.get("uid") or "")
        if not saved_uid:
            raise DashboardCodeError("Grafana save response did not include a dashboard uid.")
        last_verification = _verify_dashboard_saved_and_working(saved_uid, current_dashboard)
        if last_verification.valid:
            if repair_history:
                last_verification.warnings.append(
                    "Applied dashboard repairs before verification passed: "
                    + ", ".join(repair_history)
                )
            return last_save_result, current_dashboard, last_verification

        if attempt >= max_repair_attempts:
            break
        repaired_dashboard, actions = _repair_dashboard_for_verification(
            current_dashboard,
            last_verification,
        )
        if not actions or repaired_dashboard == current_dashboard:
            break
        repair_history.extend(actions)
        current_dashboard = repaired_dashboard

    error_text = "; ".join(last_verification.errors) or "Unknown verification failure."
    raise DashboardCodeError(
        "Dashboard was saved but failed verification after "
        f"{max_repair_attempts + 1} attempt(s): {error_text}"
    )


def _reject_forbidden_dashboard_code(code: str) -> None:
    for pattern, reason in _FORBIDDEN_DASHBOARD_CODE_PATTERNS:
        if pattern.search(code):
            raise DashboardCodeError(f"Dashboard code uses forbidden SDK API: {reason}")


def _coerce_mapping_payload(mapping_text: str) -> dict[str, Any]:
    mapping_text = mapping_text.strip()
    if not mapping_text:
        raise ValueError("mapping text is empty")
    if not mapping_text.startswith("{"):
        first_brace = mapping_text.find("{")
        last_brace = mapping_text.rfind("}")
        if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
            raise ValueError("mapping text does not contain a JSON object")
        mapping_text = mapping_text[first_brace : last_brace + 1]
    parsed = json.loads(mapping_text)
    if not isinstance(parsed, dict):
        raise ValueError("mapping JSON must be an object")
    return parsed


def _iter_mapping_properties(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if "properties" in payload and isinstance(payload["properties"], dict):
        return [payload["properties"]]
    if "mappings" in payload and isinstance(payload["mappings"], dict):
        mappings = payload["mappings"]
        if isinstance(mappings.get("properties"), dict):
            return [mappings["properties"]]
    property_sets: list[dict[str, Any]] = []
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        mappings = value.get("mappings")
        if isinstance(mappings, dict) and isinstance(mappings.get("properties"), dict):
            property_sets.append(mappings["properties"])
    return property_sets


def _collect_mapping_fields(
    properties: dict[str, Any],
    *,
    prefix: str = "",
    date_fields: list[str],
    numeric_fields: list[str],
    categorical_fields: list[str],
) -> None:
    for name, definition in properties.items():
        if not isinstance(definition, dict):
            continue
        field_name = f"{prefix}.{name}" if prefix else name
        field_type = definition.get("type")
        if field_type == "date":
            date_fields.append(field_name)
        elif field_type in _NUMERIC_MAPPING_TYPES:
            numeric_fields.append(field_name)
        elif field_type in {"keyword", "constant_keyword"}:
            categorical_fields.append(field_name)
        elif field_type == "text":
            keyword_fields = definition.get("fields")
            if isinstance(keyword_fields, dict):
                keyword = keyword_fields.get("keyword")
                if isinstance(keyword, dict) and keyword.get("type") == "keyword":
                    categorical_fields.append(f"{field_name}.keyword")

        nested_properties = definition.get("properties")
        if isinstance(nested_properties, dict):
            _collect_mapping_fields(
                nested_properties,
                prefix=field_name,
                date_fields=date_fields,
                numeric_fields=numeric_fields,
                categorical_fields=categorical_fields,
            )


def _unique_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _summarize_mapping(mapping_text: str, preferred_index_pattern: str = "") -> dict[str, Any]:
    payload = _coerce_mapping_payload(mapping_text)
    property_sets = _iter_mapping_properties(payload)
    if not property_sets:
        raise ValueError("mapping JSON does not contain mappings.properties")

    date_fields: list[str] = []
    numeric_fields: list[str] = []
    categorical_fields: list[str] = []
    for properties in property_sets:
        _collect_mapping_fields(
            properties,
            date_fields=date_fields,
            numeric_fields=numeric_fields,
            categorical_fields=categorical_fields,
        )

    date_fields = _unique_ordered(date_fields)
    numeric_fields = _unique_ordered(numeric_fields)
    categorical_fields = _unique_ordered(categorical_fields)
    preferred_time_fields = ["@timestamp", "timestamp", "received_at", "Date"]
    time_field = next(
        (field for field in preferred_time_fields if field in date_fields),
        date_fields[0] if date_fields else "@timestamp",
    )

    table_fields = _unique_ordered(
        [
            time_field,
            *[field.removesuffix(".keyword") for field in categorical_fields[:4]],
            *numeric_fields[:6],
        ]
    )

    return {
        "index_pattern": preferred_index_pattern or None,
        "time_field": time_field,
        "date_fields": date_fields,
        "numeric_fields": numeric_fields,
        "categorical_fields": categorical_fields,
        "table_fields": table_fields,
        "field_counts": {
            "date": len(date_fields),
            "numeric": len(numeric_fields),
            "categorical": len(categorical_fields),
        },
    }


def _clean_fields(fields: list[str] | None, *, exclude: set[str] | None = None) -> list[str]:
    exclude = exclude or set()
    cleaned: list[str] = []
    seen: set[str] = set()
    for field in fields or []:
        if not isinstance(field, str):
            continue
        field = field.strip()
        if not field or field in exclude or field in seen:
            continue
        seen.add(field)
        cleaned.append(field)
    return cleaned


def _ref_id(index: int) -> str:
    """Return Grafana-style ref ids: A, B, ... Z, AA, AB, ..."""
    if index < 0:
        raise ValueError("index must be non-negative")
    letters = ""
    while True:
        index, remainder = divmod(index, 26)
        letters = chr(ord("A") + remainder) + letters
        if index == 0:
            return letters
        index -= 1


def _terms_settings(size: int = 10, *, order_by: str = "_count"):
    from grafana_foundation_sdk.builders.elasticsearch import TermsSettings

    return TermsSettings().size(str(size)).order("desc").order_by(order_by)


def _date_histogram(time_field: str, id_val: str = "2"):
    from grafana_foundation_sdk.builders.elasticsearch import (
        DateHistogram,
        DateHistogramSettings,
    )

    return (
        DateHistogram()
        .id(id_val)
        .field(time_field)
        .settings(
            DateHistogramSettings()
            .interval("auto")
            .min_doc_count("0")
            .trim_edges("0")
        )
    )


def _count_query(ref: str, query: str, time_field: str):
    from grafana_foundation_sdk.builders.elasticsearch import Count, Dataquery

    return (
        Dataquery()
        .ref_id(ref)
        .query(query)
        .time_field(time_field)
        .metrics([Count().id("1")])
        .bucket_aggs([])
    )


def _count_over_time_query(ref: str, query: str, time_field: str):
    from grafana_foundation_sdk.builders.elasticsearch import Count, Dataquery

    return (
        Dataquery()
        .ref_id(ref)
        .query(query)
        .alias("documents")
        .time_field(time_field)
        .metrics([Count().id("1")])
        .bucket_aggs([_date_histogram(time_field)])
    )


def run_dashboard_code(code: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> DashboardBuildResult:
    """Execute Foundation-SDK code in a subprocess and return the JSON it printed.

    The contract with the model: the snippet must print exactly one JSON document
    (a Grafana dashboard) to stdout. Anything else is treated as a build failure.
    """
    _reject_forbidden_dashboard_code(code)

    # Strip debugpy/pydevd vars so the child doesn't auto-attach the debugger;
    # the inherited PYDEVD_LOAD_VALUES_ASYNC + DEBUGPY_* vars otherwise inject
    # a bytecode bootstrap into `python -c` that has previously produced a
    # SyntaxError before the model's snippet even runs.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYDEVD_DISABLE_FILE_VALIDATION": "1",
    }

    with tempfile.TemporaryDirectory(prefix="dashboard-build-") as cwd:
        try:
            completed = subprocess.run(
                [sys.executable, "-Xfrozen_modules=off", "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=cwd,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DashboardCodeError(
                f"Dashboard code timed out after {timeout_s}s",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            ) from exc

    stderr = _PYDEVD_NOISE.sub("", completed.stderr).strip()

    if completed.returncode != 0:
        raise DashboardCodeError(
            f"Dashboard code exited with status {completed.returncode}",
            stderr=stderr,
            returncode=completed.returncode,
        )

    stdout = completed.stdout.strip()
    if not stdout:
        raise DashboardCodeError(
            "Dashboard code produced no stdout. The snippet must print the JSON.",
            stderr=stderr,
        )

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DashboardCodeError(
            f"Dashboard stdout was not valid JSON: {exc.msg}",
            stderr=stderr,
        ) from exc

    if not isinstance(parsed, dict):
        raise DashboardCodeError(
            "Dashboard JSON must be a JSON object (the dashboard model), not a list or scalar.",
            stderr=stderr,
        )

    _validate_dashboard_model(parsed)

    return DashboardBuildResult(
        json_text=stdout,
        parsed=parsed,
        stdout=completed.stdout,
        stderr=stderr,
    )


def build_dashboard_tool(ctx: RunContext[Any], code: str) -> str:
    """Compile a complete Grafana dashboard from a Foundation-SDK Python snippet, in ONE call.

    Submit a single self-contained Python program: every import, every panel, every target,
    ending in exactly one `print(JSONEncoder(sort_keys=True, indent=2).encode(dashboard.build()))`.
    Returns the dashboard JSON string on success — pass it directly to `grafana_update_dashboard`.
    On failure returns "Build failed. ..." with a stderr tail; fix the one thing the traceback
    points to and resubmit the complete snippet (max 2 retries).

    HARD rules — violating these wastes tool calls:

    - This is NOT a REPL. Never submit probes like `print('ok')`, `print('loaded')`, or
      `from X import Y; print(...)` to test whether a name exists. If you are unsure a name
      exists, it does not — pick one from the allowlist below.
    - `with_target(...)`, `metrics([...])`, and `bucket_aggs([...])` all call `.build()` on
      their argument(s). Pass BUILDER INSTANCES, never raw dicts. Dicts produce
      `AttributeError: 'dict' object has no attribute 'build'`.
    - `datasource({...})` is the only place a dict is correct:
      `{"type": "grafana-opensearch-datasource", "uid": "<uid>"}`.
    - Every OpenSearch `Dataquery` must set `.time_field("<date-field>")`, including stat
      and raw-document panels.
    - Every data panel must set `.id(...)` and `.grid_pos(GridPos(...))`.
    - Allowed elasticsearch builder names (from `grafana_foundation_sdk.builders.elasticsearch`):
        Query: `Dataquery`.
        Bucket aggs: `DateHistogram`, `Histogram`, `Terms`, `Filters`, `Filter`, `GeoHashGrid`,
            `Nested`. Settings: `DateHistogramSettings`, `HistogramSettings`, `TermsSettings`,
            `FiltersSettings`, `GeoHashGridSettings`.
        Metric aggs: `Count`, `Average`, `Sum`, `Min`, `Max`, `UniqueCount`, `Percentiles`,
            `ExtendedStats`, `Rate`, `Logs`, `RawData`, `RawDocument`, `TopMetrics`,
            `MovingAverage`, `MovingFunction`, `Derivative`, `CumulativeSum`, `BucketScript`,
            `SerialDiff`.
      There is no `builders.opensearch`, no `builders.elasticsearch.aggs` submodule, no
      `Agg`/`BucketAgg`/`MetricBuilder`. Do not invent names.
    - Panel classes are in their own modules: `Panel as Timeseries` from `builders.timeseries`,
      `Panel as Stat` from `builders.stat`, `Panel as Table` from `builders.table`,
      `Panel as Bargauge` from `builders.bargauge`, `Panel as PieChart` from
      `builders.piechart`, and `Row` / `QueryVariable` from `builders.dashboard`.
      Always alias `Panel`.
    - Forbidden hallucinated names are rejected before execution: `Templating`, `Variable`,
      `MetricAggregation`, `BucketAggregation`, `builders.opensearch`, and
      `builders.elasticsearch.aggs`.

    Minimal correct shape:

        from grafana_foundation_sdk.builders.dashboard import Dashboard
        from grafana_foundation_sdk.builders.timeseries import Panel as Timeseries
        from grafana_foundation_sdk.builders.elasticsearch import (
            Dataquery, Count, DateHistogram, DateHistogramSettings,
        )
        from grafana_foundation_sdk.models.dashboard import GridPos
        from grafana_foundation_sdk.cog.encoder import JSONEncoder

        DS = {"type": "grafana-opensearch-datasource", "uid": "<uid>"}
        target = (
            Dataquery().ref_id("A").query("*").time_field("@timestamp")
            .metrics([Count().id("1")])
            .bucket_aggs([
                DateHistogram().id("2").field("@timestamp")
                .settings(DateHistogramSettings().interval("auto"))
            ])
        )
        dashboard = (
            Dashboard("My dash").uid("my-dash").refresh("30s").time("now-6h", "now")
            .with_panel(
                Timeseries()
                .id(1)
                .title("Events")
                .grid_pos(GridPos(h=8, w=24, x=0, y=0))
                .datasource(DS)
                .with_target(target)
            )
        )
        print(JSONEncoder(sort_keys=True, indent=2).encode(dashboard.build()))
    """
    with logfire.span(
        "tool.build_dashboard_tool",
        code_chars=len(code),
        _tags=["tool", "dashboard"],
    ) as span:
        try:
            result = run_dashboard_code(code)
        except DashboardCodeError as exc:
            span.set_attribute("error", True)
            span.set_attribute("error.stderr", exc.stderr[-2000:])
            failure = (
                "Build failed. Fix the code and call build_dashboard_tool again.\n"
                f"Error: {exc}\n"
                f"Stderr (tail):\n{exc.stderr[-1500:]}"
            )
            _record_local_tool_call(
                ctx,
                "build_dashboard_tool",
                {"code_chars": len(code)},
                failure[:2000],
            )
            return failure

        span.set_attribute("dashboard.uid", result.parsed.get("uid"))
        span.set_attribute("dashboard.title", result.parsed.get("title"))
        span.set_attribute("dashboard.panel_count", len(result.parsed.get("panels", [])))
        _record_local_tool_call(
            ctx,
            "build_dashboard_tool",
            {"code_chars": len(code)},
            (
                "Dashboard build succeeded: "
                f"uid={result.parsed.get('uid')}, "
                f"title={result.parsed.get('title')}, "
                f"panels={len(result.parsed.get('panels', []))}"
            ),
        )
        return result.json_text


def summarize_opensearch_mapping_tool(
    ctx: RunContext[Any],
    mapping: str,
    preferred_index_pattern: str = "",
) -> str:
    """Summarize a large OpenSearch mapping into dashboard-ready field groups.

    Use this after `opensearch_IndexMappingTool` when the mapping response is large.
    It returns canonical `time_field`, `numeric_fields`, `categorical_fields`, and
    `table_fields` values suitable for `build_opensearch_dashboard_tool`.
    """
    with logfire.span(
        "tool.summarize_opensearch_mapping_tool",
        preferred_index_pattern=preferred_index_pattern,
        mapping_chars=len(mapping),
        _tags=["tool", "dashboard", "opensearch"],
    ) as span:
        try:
            summary = _summarize_mapping(mapping, preferred_index_pattern)
        except (json.JSONDecodeError, ValueError) as exc:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(exc))
            failure = f"Mapping summary failed: {exc}"
            _record_local_tool_call(
                ctx,
                "summarize_opensearch_mapping_tool",
                {"mapping_chars": len(mapping), "preferred_index_pattern": preferred_index_pattern},
                failure,
            )
            return failure

        span.set_attribute("mapping.time_field", summary["time_field"])
        span.set_attribute("mapping.numeric_count", summary["field_counts"]["numeric"])
        span.set_attribute("mapping.categorical_count", summary["field_counts"]["categorical"])
        result = json.dumps(summary, sort_keys=True, indent=2)
        _record_local_tool_call(
            ctx,
            "summarize_opensearch_mapping_tool",
            {"mapping_chars": len(mapping), "preferred_index_pattern": preferred_index_pattern},
            (
                "Mapping summary succeeded: "
                f"time_field={summary['time_field']}, "
                f"numeric={summary['field_counts']['numeric']}, "
                f"categorical={summary['field_counts']['categorical']}"
            ),
        )
        return result


def build_opensearch_dashboard_tool(
    ctx: RunContext[Any],
    title: str,
    datasource_uid: str,
    time_field: str = "@timestamp",
    uid: str | None = None,
    query: str = "*",
    time_from: str = "now-6h",
    time_to: str = "now",
    refresh: str = "30s",
    numeric_fields: list[str] | None = None,
    categorical_fields: list[str] | None = None,
    table_fields: list[str] | None = None,
    max_numeric_panels: int = 6,
    max_category_panels: int = 4,
    raw_document_limit: int = 50,
    index_pattern: str | None = None,
    variable_fields: list[str] | None = None,
    group_by_field: str | None = None,
    include_variables: bool = True,
) -> str:
    """Build a detailed Grafana dashboard for an OpenSearch index or index pattern.

    Prefer this tool for normal dashboard creation. It takes field names discovered
    from OpenSearch mappings/samples and produces a validated dashboard JSON string
    ready for `grafana_update_dashboard`.

    Field guidance:
    - `numeric_fields`: numeric metric fields such as price, volume, temperature_c.
    - `categorical_fields`: exact fields usable in terms aggregations. For text mappings
      with a keyword subfield, pass `field.keyword`; for keyword mappings, pass `field`.
    - `table_fields`: fields that should be visible/useful in the raw-document table.
      Grafana/OpenSearch may still return all fields, but these names inform panel titles
      and trace metadata.
    - `query`: Lucene query scoped to the user request. Use `*` only when the datasource
      itself is already scoped to the intended index/pattern.
    - `variable_fields`: optional categorical fields to expose as dashboard variables.
      Defaults to the first two `categorical_fields`.
    - `group_by_field`: optional categorical field used for split time-series and grouped
      distribution panels. Defaults to the first categorical field.
    """
    with logfire.span(
        "tool.build_opensearch_dashboard_tool",
        dashboard_title=title,
        datasource_uid=datasource_uid,
        _tags=["tool", "dashboard"],
    ) as span:
        try:
            json_text = _build_opensearch_dashboard_json(
                title=title,
                datasource_uid=datasource_uid,
                time_field=time_field,
                uid=uid,
                query=query,
                time_from=time_from,
                time_to=time_to,
                refresh=refresh,
                numeric_fields=numeric_fields,
                categorical_fields=categorical_fields,
                table_fields=table_fields,
                max_numeric_panels=max_numeric_panels,
                max_category_panels=max_category_panels,
                raw_document_limit=raw_document_limit,
                index_pattern=index_pattern,
                variable_fields=variable_fields,
                group_by_field=group_by_field,
                include_variables=include_variables,
            )
        except DashboardCodeError as exc:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(exc))
            failure = (
                "Build failed. Fix the dashboard builder arguments and call "
                f"build_opensearch_dashboard_tool again.\nError: {exc}"
            )
            _record_local_tool_call(
                ctx,
                "build_opensearch_dashboard_tool",
                {
                    "title": title,
                    "datasource_uid": datasource_uid,
                    "time_field": time_field,
                    "uid": uid,
                },
                failure,
            )
            return failure

        parsed = json.loads(json_text)
        span.set_attribute("dashboard.uid", parsed.get("uid"))
        span.set_attribute("dashboard.title", parsed.get("title"))
        span.set_attribute("dashboard.panel_count", len(parsed.get("panels", [])))
        variables = (parsed.get("templating") or {}).get("list") or []
        span.set_attribute("dashboard.variable_count", len(variables))
        span.set_attribute("dashboard.time_from", (parsed.get("time") or {}).get("from"))
        span.set_attribute("dashboard.time_to", (parsed.get("time") or {}).get("to"))
        _record_local_tool_call(
            ctx,
            "build_opensearch_dashboard_tool",
            {
                "title": title,
                "datasource_uid": datasource_uid,
                "time_field": time_field,
                "uid": uid,
            },
            (
                "Dashboard build succeeded: "
                f"uid={parsed.get('uid')}, "
                f"title={parsed.get('title')}, "
                f"panels={len(parsed.get('panels', []))}, "
                f"variables={len(variables)}"
            ),
        )
        return json_text


def build_and_save_opensearch_dashboard_tool(
    ctx: RunContext[Any],
    title: str,
    datasource_uid: str,
    time_field: str = "@timestamp",
    uid: str | None = None,
    query: str = "*",
    time_from: str = "now-6h",
    time_to: str = "now",
    refresh: str = "30s",
    numeric_fields: list[str] | None = None,
    categorical_fields: list[str] | None = None,
    table_fields: list[str] | None = None,
    max_numeric_panels: int = 6,
    max_category_panels: int = 4,
    raw_document_limit: int = 50,
    index_pattern: str | None = None,
    variable_fields: list[str] | None = None,
    group_by_field: str | None = None,
    include_variables: bool = True,
    folder_uid: str | None = None,
    message: str = "",
    overwrite: bool = True,
    verify: bool = True,
    max_repair_attempts: int = 2,
) -> str:
    """Build a rich OpenSearch dashboard and save it to Grafana in one tool call.

    Use this for create/update requests. It removes the fragile model-dependent
    handoff between a successful local dashboard build and the Grafana save call.
    By default it fetches and verifies the saved dashboard, then applies known
    deterministic repairs and retries up to `max_repair_attempts` times.
    """
    with logfire.span(
        "tool.build_and_save_opensearch_dashboard_tool",
        dashboard_title=title,
        datasource_uid=datasource_uid,
        _tags=["tool", "dashboard", "grafana"],
    ) as span:
        try:
            json_text = _build_opensearch_dashboard_json(
                title=title,
                datasource_uid=datasource_uid,
                time_field=time_field,
                uid=uid,
                query=query,
                time_from=time_from,
                time_to=time_to,
                refresh=refresh,
                numeric_fields=numeric_fields,
                categorical_fields=categorical_fields,
                table_fields=table_fields,
                max_numeric_panels=max_numeric_panels,
                max_category_panels=max_category_panels,
                raw_document_limit=raw_document_limit,
                index_pattern=index_pattern,
                variable_fields=variable_fields,
                group_by_field=group_by_field,
                include_variables=include_variables,
            )
            dashboard = json.loads(json_text)
            save_result, dashboard, verification = _save_dashboard_with_verification(
                dashboard,
                folder_uid=folder_uid,
                message=message or f"Save {dashboard.get('title', title)}",
                overwrite=overwrite,
                verify=verify,
                max_repair_attempts=max_repair_attempts,
            )
        except DashboardCodeError as exc:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(exc))
            failure = f"Dashboard save or verification failed.\nError: {exc}"
            _record_local_tool_call(
                ctx,
                "build_and_save_opensearch_dashboard_tool",
                {"title": title, "datasource_uid": datasource_uid, "uid": uid},
                failure,
            )
            return failure

        variables = (dashboard.get("templating") or {}).get("list") or []
        span.set_attribute("dashboard.uid", dashboard.get("uid"))
        span.set_attribute("dashboard.title", dashboard.get("title"))
        span.set_attribute("dashboard.panel_count", len(dashboard.get("panels", [])))
        span.set_attribute("dashboard.variable_count", len(variables))
        span.set_attribute("grafana.status", save_result.get("status"))
        span.set_attribute("dashboard.verified", verification.valid)
        span.set_attribute("dashboard.queried_panels", verification.queried_panels)
        summary = {
            "status": save_result.get("status", "success"),
            "uid": save_result.get("uid") or dashboard.get("uid"),
            "title": dashboard.get("title"),
            "url": save_result.get("url"),
            "version": save_result.get("version"),
            "panel_count": len(dashboard.get("panels", [])),
            "variable_count": len(variables),
            "verified": verification.valid if verify else None,
            "verification_warnings": verification.warnings,
            "queried_panels": verification.queried_panels,
            "panels_with_data": verification.panels_with_data,
        }
        result = json.dumps(summary, sort_keys=True, indent=2)
        _record_local_tool_call(
            ctx,
            "build_and_save_opensearch_dashboard_tool",
            {"title": title, "datasource_uid": datasource_uid, "uid": uid},
            (
                "Dashboard save succeeded: "
                f"uid={summary['uid']}, title={summary['title']}, "
                f"panels={summary['panel_count']}, variables={summary['variable_count']}, "
                f"verified={summary['verified']}"
            ),
        )
        return result


def build_opensearch_chart_dashboard_tool(
    ctx: RunContext[Any],
    title: str,
    datasource_uid: str,
    chart_type: str = "piechart",
    time_field: str = "@timestamp",
    uid: str | None = None,
    query: str = "*",
    time_from: str = "now-6h",
    time_to: str = "now",
    refresh: str = "",
    metric_type: str = "count",
    metric_field: str | None = None,
    group_by_field: str | None = None,
    variable_fields: list[str] | None = None,
    include_variables: bool = False,
    index_pattern: str | None = None,
    raw_document_limit: int = 50,
) -> str:
    """Build a one-panel OpenSearch dashboard for simple chart/panel requests.

    Use this when the user asks for a single chart or a small focused panel, such as
    "build a pie chart by system_state" or "make a stat panel for average power".
    It intentionally creates exactly one data panel and no dense KPI/trend layout.

    Supported `chart_type` values: `piechart`, `bargauge`, `barchart`, `timeseries`,
    `stat`, and `table`.
    Supported `metric_type` values: `count`, `avg`, `sum`, `min`, `max`,
    `unique_count`, and `raw_data`.
    """
    with logfire.span(
        "tool.build_opensearch_chart_dashboard_tool",
        dashboard_title=title,
        datasource_uid=datasource_uid,
        chart_type=chart_type,
        _tags=["tool", "dashboard"],
    ) as span:
        try:
            json_text = _build_opensearch_single_chart_dashboard_json(
                title=title,
                datasource_uid=datasource_uid,
                chart_type=chart_type,
                time_field=time_field,
                uid=uid,
                query=query,
                time_from=time_from,
                time_to=time_to,
                refresh=refresh,
                metric_type=metric_type,
                metric_field=metric_field,
                group_by_field=group_by_field,
                variable_fields=variable_fields,
                include_variables=include_variables,
                index_pattern=index_pattern,
                raw_document_limit=raw_document_limit,
            )
        except DashboardCodeError as exc:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(exc))
            failure = (
                "Build failed. Fix the focused chart builder arguments and call "
                f"build_opensearch_chart_dashboard_tool again.\nError: {exc}"
            )
            _record_local_tool_call(
                ctx,
                "build_opensearch_chart_dashboard_tool",
                {
                    "title": title,
                    "datasource_uid": datasource_uid,
                    "chart_type": chart_type,
                    "uid": uid,
                },
                failure,
            )
            return failure

        parsed = json.loads(json_text)
        span.set_attribute("dashboard.uid", parsed.get("uid"))
        span.set_attribute("dashboard.title", parsed.get("title"))
        span.set_attribute("dashboard.panel_count", len(parsed.get("panels", [])))
        _record_local_tool_call(
            ctx,
            "build_opensearch_chart_dashboard_tool",
            {
                "title": title,
                "datasource_uid": datasource_uid,
                "chart_type": chart_type,
                "uid": uid,
            },
            (
                "Dashboard build succeeded: "
                f"uid={parsed.get('uid')}, "
                f"title={parsed.get('title')}, panels={len(parsed.get('panels', []))}"
            ),
        )
        return json_text


def build_and_save_opensearch_chart_dashboard_tool(
    ctx: RunContext[Any],
    title: str,
    datasource_uid: str,
    chart_type: str = "piechart",
    time_field: str = "@timestamp",
    uid: str | None = None,
    query: str = "*",
    time_from: str = "now-6h",
    time_to: str = "now",
    refresh: str = "",
    metric_type: str = "count",
    metric_field: str | None = None,
    group_by_field: str | None = None,
    variable_fields: list[str] | None = None,
    include_variables: bool = False,
    index_pattern: str | None = None,
    raw_document_limit: int = 50,
    folder_uid: str | None = None,
    message: str = "",
    overwrite: bool = True,
    verify: bool = True,
    max_repair_attempts: int = 2,
) -> str:
    """Build and save a one-panel OpenSearch dashboard for simple chart requests.

    By default it fetches and verifies the saved dashboard, applies known
    deterministic repairs, and retries up to `max_repair_attempts` times.
    """
    with logfire.span(
        "tool.build_and_save_opensearch_chart_dashboard_tool",
        dashboard_title=title,
        datasource_uid=datasource_uid,
        chart_type=chart_type,
        _tags=["tool", "dashboard", "grafana"],
    ) as span:
        try:
            json_text = _build_opensearch_single_chart_dashboard_json(
                title=title,
                datasource_uid=datasource_uid,
                chart_type=chart_type,
                time_field=time_field,
                uid=uid,
                query=query,
                time_from=time_from,
                time_to=time_to,
                refresh=refresh,
                metric_type=metric_type,
                metric_field=metric_field,
                group_by_field=group_by_field,
                variable_fields=variable_fields,
                include_variables=include_variables,
                index_pattern=index_pattern,
                raw_document_limit=raw_document_limit,
            )
            dashboard = json.loads(json_text)
            save_result, dashboard, verification = _save_dashboard_with_verification(
                dashboard,
                folder_uid=folder_uid,
                message=message or f"Save {dashboard.get('title', title)}",
                overwrite=overwrite,
                verify=verify,
                max_repair_attempts=max_repair_attempts,
            )
        except DashboardCodeError as exc:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(exc))
            failure = f"Dashboard save or verification failed.\nError: {exc}"
            _record_local_tool_call(
                ctx,
                "build_and_save_opensearch_chart_dashboard_tool",
                {
                    "title": title,
                    "datasource_uid": datasource_uid,
                    "chart_type": chart_type,
                    "uid": uid,
                },
                failure,
            )
            return failure

        variables = (dashboard.get("templating") or {}).get("list") or []
        span.set_attribute("dashboard.uid", dashboard.get("uid"))
        span.set_attribute("dashboard.title", dashboard.get("title"))
        span.set_attribute("dashboard.panel_count", len(dashboard.get("panels", [])))
        span.set_attribute("dashboard.variable_count", len(variables))
        span.set_attribute("grafana.status", save_result.get("status"))
        span.set_attribute("dashboard.verified", verification.valid)
        span.set_attribute("dashboard.queried_panels", verification.queried_panels)
        summary = {
            "status": save_result.get("status", "success"),
            "uid": save_result.get("uid") or dashboard.get("uid"),
            "title": dashboard.get("title"),
            "url": save_result.get("url"),
            "version": save_result.get("version"),
            "panel_count": len(dashboard.get("panels", [])),
            "variable_count": len(variables),
            "chart_type": chart_type,
            "verified": verification.valid if verify else None,
            "verification_warnings": verification.warnings,
            "queried_panels": verification.queried_panels,
            "panels_with_data": verification.panels_with_data,
        }
        result = json.dumps(summary, sort_keys=True, indent=2)
        _record_local_tool_call(
            ctx,
            "build_and_save_opensearch_chart_dashboard_tool",
            {
                "title": title,
                "datasource_uid": datasource_uid,
                "chart_type": chart_type,
                "uid": uid,
            },
            (
                "Dashboard save succeeded: "
                f"uid={summary['uid']}, title={summary['title']}, "
                f"panels={summary['panel_count']}, variables={summary['variable_count']}, "
                f"verified={summary['verified']}"
            ),
        )
        return result


def build_opensearch_planned_dashboard_tool(
    ctx: RunContext[Any],
    dashboard_plan: dict[str, Any],
    datasource_uid: str,
    time_field: str = "@timestamp",
    title: str | None = None,
    uid: str | None = None,
    query: str = "*",
    time_from: str | None = None,
    time_to: str | None = None,
    refresh: str | None = None,
    index_pattern: str | None = None,
) -> str:
    """Build dashboard JSON from an explicit LLM-authored plan.

    Use this for open-ended dashboard requests after discovery and user approval.
    The compiler creates exactly the panels listed in `dashboard_plan["panels"]`;
    it does not add default KPI rows, distribution panels, raw tables, or template
    panels. Add a table only when the plan explicitly contains a table panel.

    Expected plan shape:
    {
      "title": "Equipment overview",
      "description": "optional",
      "variables": [{"name": "state", "label": "State", "field": "state.keyword"}],
      "panels": [
        {
          "title": "Temperature by state",
          "type": "timeseries",
          "metric_type": "avg",
          "metric_field": "temperature_c",
          "group_by_field": "state.keyword",
          "query": "*",
          "row": "Telemetry",
          "gridPos": {"h": 8, "w": 12, "x": 0, "y": 1}
        }
      ]
    }

    Supported panel types: stat, gauge, timeseries, piechart, bargauge, barchart, table.
    Supported metric types: count, avg, sum, min, max, unique_count, raw_data.
    """
    with logfire.span(
        "tool.build_opensearch_planned_dashboard_tool",
        datasource_uid=datasource_uid,
        _tags=["tool", "dashboard"],
    ) as span:
        try:
            json_text = _build_opensearch_planned_dashboard_json(
                dashboard_plan=dashboard_plan,
                datasource_uid=datasource_uid,
                time_field=time_field,
                title=title,
                uid=uid,
                query=query,
                time_from=time_from,
                time_to=time_to,
                refresh=refresh,
                index_pattern=index_pattern,
            )
        except DashboardCodeError as exc:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(exc))
            failure = (
                "Build failed. Fix the dashboard plan and call "
                f"build_opensearch_planned_dashboard_tool again.\nError: {exc}"
            )
            _record_local_tool_call(
                ctx,
                "build_opensearch_planned_dashboard_tool",
                {"datasource_uid": datasource_uid, "uid": uid},
                failure,
            )
            return failure

        parsed = json.loads(json_text)
        variables = (parsed.get("templating") or {}).get("list") or []
        span.set_attribute("dashboard.uid", parsed.get("uid"))
        span.set_attribute("dashboard.title", parsed.get("title"))
        span.set_attribute("dashboard.panel_count", len(parsed.get("panels", [])))
        span.set_attribute("dashboard.variable_count", len(variables))
        _record_local_tool_call(
            ctx,
            "build_opensearch_planned_dashboard_tool",
            {"datasource_uid": datasource_uid, "uid": parsed.get("uid")},
            (
                "Dashboard build succeeded: "
                f"uid={parsed.get('uid')}, title={parsed.get('title')}, "
                f"panels={len(parsed.get('panels', []))}, variables={len(variables)}"
            ),
        )
        return json_text


def build_and_save_opensearch_planned_dashboard_tool(
    ctx: RunContext[Any],
    dashboard_plan: dict[str, Any],
    datasource_uid: str,
    time_field: str = "@timestamp",
    title: str | None = None,
    uid: str | None = None,
    query: str = "*",
    time_from: str | None = None,
    time_to: str | None = None,
    refresh: str | None = None,
    index_pattern: str | None = None,
    folder_uid: str | None = None,
    message: str = "",
    overwrite: bool = True,
    verify: bool = True,
    max_repair_attempts: int = 2,
    confirmed_by_user: bool = False,
) -> str:
    """Build and save a dashboard from an explicit, user-approved plan.

    For vague requests like "add a dashboard for trades-*", first discover the
    mapping/sample documents, present the planned panels to the user, and call
    this only after the user confirms. This tool compiles exactly the plan and
    verifies the saved Grafana dashboard with the same repair loop as the other
    save tools.
    """
    if not confirmed_by_user:
        result = (
            "Dashboard plan was not saved because confirmed_by_user=false. "
            "Show the discovered dashboard plan to the user and ask whether to build it; "
            "call this tool again only after explicit approval."
        )
        _record_local_tool_call(
            ctx,
            "build_and_save_opensearch_planned_dashboard_tool",
            {"datasource_uid": datasource_uid, "uid": uid, "confirmed_by_user": False},
            result,
        )
        return result

    with logfire.span(
        "tool.build_and_save_opensearch_planned_dashboard_tool",
        datasource_uid=datasource_uid,
        _tags=["tool", "dashboard", "grafana"],
    ) as span:
        try:
            json_text = _build_opensearch_planned_dashboard_json(
                dashboard_plan=dashboard_plan,
                datasource_uid=datasource_uid,
                time_field=time_field,
                title=title,
                uid=uid,
                query=query,
                time_from=time_from,
                time_to=time_to,
                refresh=refresh,
                index_pattern=index_pattern,
            )
            dashboard = json.loads(json_text)
            save_result, dashboard, verification = _save_dashboard_with_verification(
                dashboard,
                folder_uid=folder_uid,
                message=message or f"Save {dashboard.get('title', title or 'dashboard')}",
                overwrite=overwrite,
                verify=verify,
                max_repair_attempts=max_repair_attempts,
            )
        except DashboardCodeError as exc:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(exc))
            failure = f"Dashboard save or verification failed.\nError: {exc}"
            _record_local_tool_call(
                ctx,
                "build_and_save_opensearch_planned_dashboard_tool",
                {"datasource_uid": datasource_uid, "uid": uid},
                failure,
            )
            return failure

        variables = (dashboard.get("templating") or {}).get("list") or []
        data_panel_count = len([p for p in dashboard.get("panels", []) if p.get("type") != "row"])
        span.set_attribute("dashboard.uid", dashboard.get("uid"))
        span.set_attribute("dashboard.title", dashboard.get("title"))
        span.set_attribute("dashboard.panel_count", len(dashboard.get("panels", [])))
        span.set_attribute("dashboard.data_panel_count", data_panel_count)
        span.set_attribute("dashboard.variable_count", len(variables))
        span.set_attribute("grafana.status", save_result.get("status"))
        span.set_attribute("dashboard.verified", verification.valid)
        span.set_attribute("dashboard.queried_panels", verification.queried_panels)
        summary = {
            "status": save_result.get("status", "success"),
            "uid": save_result.get("uid") or dashboard.get("uid"),
            "title": dashboard.get("title"),
            "url": save_result.get("url"),
            "version": save_result.get("version"),
            "panel_count": len(dashboard.get("panels", [])),
            "data_panel_count": data_panel_count,
            "variable_count": len(variables),
            "verified": verification.valid if verify else None,
            "verification_warnings": verification.warnings,
            "queried_panels": verification.queried_panels,
            "panels_with_data": verification.panels_with_data,
        }
        result = json.dumps(summary, sort_keys=True, indent=2)
        _record_local_tool_call(
            ctx,
            "build_and_save_opensearch_planned_dashboard_tool",
            {"datasource_uid": datasource_uid, "uid": summary["uid"]},
            (
                "Dashboard save succeeded: "
                f"uid={summary['uid']}, title={summary['title']}, "
                f"panels={summary['panel_count']}, variables={summary['variable_count']}, "
                f"verified={summary['verified']}"
            ),
        )
        return result


def _metric_builder(metric_type: str, metric_field: str | None, *, raw_size: int = 50) -> Any:
    from grafana_foundation_sdk.builders.elasticsearch import (
        Average,
        Count,
        ElasticsearchRawDataSettings,
        Max,
        Min,
        RawData,
        Sum,
        UniqueCount,
    )

    normalized = metric_type.strip().lower().replace("-", "_")
    if normalized in {"count", "doc_count", "documents"}:
        return Count().id("1")
    if normalized in {"raw", "raw_data", "documents_table"}:
        return (
            RawData()
            .id("1")
            .settings(ElasticsearchRawDataSettings().size(str(max(1, raw_size))))
        )

    if not metric_field:
        raise DashboardCodeError(f"metric_field is required for metric_type={metric_type!r}.")

    if normalized in {"avg", "average", "mean"}:
        return Average().id("1").field(metric_field)
    if normalized == "sum":
        return Sum().id("1").field(metric_field)
    if normalized == "min":
        return Min().id("1").field(metric_field)
    if normalized == "max":
        return Max().id("1").field(metric_field)
    if normalized in {"unique", "unique_count", "cardinality"}:
        return UniqueCount().id("1").field(metric_field)

    raise DashboardCodeError(
        "Unsupported metric_type. Use count, avg, sum, min, max, unique_count, or raw_data."
    )


def _normalize_panel_type(panel_type: Any) -> str:
    normalized = str(panel_type or "").strip().lower().replace("-", "").replace("_", "")
    aliases = {
        "pie": "piechart",
        "donut": "piechart",
        "bar": "barchart",
        "bars": "barchart",
        "bargraph": "barchart",
        "bargauge": "bargauge",
        "gauge": "gauge",
        "timeseries": "timeseries",
        "timeserie": "timeseries",
        "timechart": "timeseries",
        "line": "timeseries",
        "linechart": "timeseries",
        "stat": "stat",
        "table": "table",
    }
    panel_type = aliases.get(normalized, normalized)
    supported = {"stat", "gauge", "timeseries", "piechart", "bargauge", "barchart", "table"}
    if panel_type not in supported:
        raise DashboardCodeError(
            "Unsupported planned panel type. Use stat, gauge, timeseries, piechart, "
            "bargauge, barchart, or table."
        )
    return panel_type


def _planned_variables(plan: dict[str, Any]) -> list[VariableSpec]:
    variables = plan.get("variables") or []
    if not isinstance(variables, list):
        raise DashboardCodeError("dashboard_plan.variables must be a list when provided.")

    existing: set[str] = set()
    specs: list[VariableSpec] = []
    for index, variable in enumerate(variables, start=1):
        if not isinstance(variable, dict):
            raise DashboardCodeError(f"dashboard_plan.variables[{index}] must be an object.")
        field = str(variable.get("field") or "").strip()
        if not field:
            raise DashboardCodeError(f"dashboard_plan.variables[{index}] is missing field.")
        name = str(variable.get("name") or "").strip()
        if not name:
            name = _variable_name_for_field(field, existing)
        else:
            name = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
            if not name:
                name = _variable_name_for_field(field, existing)
            elif name in existing:
                name = _variable_name_for_field(name, existing)
            else:
                existing.add(name)
        specs.append(
            VariableSpec(
                name=name,
                label=str(variable.get("label") or _display_name(field)),
                field=field,
            )
        )
    return specs


def _planned_grid_pos(
    panel: dict[str, Any],
    *,
    panel_type: str,
    cursor: dict[str, int],
) -> dict[str, int]:
    raw_grid = panel.get("gridPos") or panel.get("grid_pos")
    if isinstance(raw_grid, dict):
        grid = {
            "h": int(raw_grid.get("h", 8)),
            "w": int(raw_grid.get("w", 12)),
            "x": int(raw_grid.get("x", 0)),
            "y": int(raw_grid.get("y", cursor["y"])),
        }
        if grid["w"] <= 0 or grid["w"] > 24 or grid["x"] < 0 or grid["x"] + grid["w"] > 24:
            raise DashboardCodeError(f"Invalid gridPos for planned panel {panel.get('title')!r}.")
        if grid["h"] <= 0:
            raise DashboardCodeError(f"Invalid gridPos height for planned panel {panel.get('title')!r}.")
        cursor["x"] = grid["x"] + grid["w"]
        cursor["y"] = max(cursor["y"], grid["y"] + grid["h"])
        if cursor["x"] >= 24:
            cursor["x"] = 0
        return grid

    default_h = 4 if panel_type in {"stat", "gauge"} else 8
    default_w = 6 if panel_type in {"stat", "gauge"} else 12
    requested_w = panel.get("w") or panel.get("width")
    requested_h = panel.get("h") or panel.get("height")
    try:
        width = int(requested_w) if requested_w is not None else default_w
        height = int(requested_h) if requested_h is not None else default_h
    except (TypeError, ValueError) as exc:
        raise DashboardCodeError(f"Invalid planned panel size for {panel.get('title')!r}.") from exc
    width = min(max(1, width), 24)
    height = max(1, height)

    if cursor["x"] + width > 24:
        cursor["x"] = 0
    grid = {"h": height, "w": width, "x": cursor["x"], "y": cursor["y"]}
    cursor["x"] += width
    if cursor["x"] >= 24:
        cursor["x"] = 0
        cursor["y"] += height
    else:
        cursor["y"] = max(cursor["y"], grid["y"] + height)
    return grid


def _build_opensearch_planned_dashboard_json(
    *,
    dashboard_plan: dict[str, Any],
    datasource_uid: str,
    time_field: str = "@timestamp",
    title: str | None = None,
    uid: str | None = None,
    query: str = "*",
    time_from: str | None = None,
    time_to: str | None = None,
    refresh: str | None = None,
    index_pattern: str | None = None,
) -> str:
    from grafana_foundation_sdk.builders.barchart import Panel as BarChart
    from grafana_foundation_sdk.builders.bargauge import Panel as Bargauge
    from grafana_foundation_sdk.builders.dashboard import Dashboard, QueryVariable, Row
    from grafana_foundation_sdk.builders.elasticsearch import Dataquery, Terms
    from grafana_foundation_sdk.builders.gauge import Panel as Gauge
    from grafana_foundation_sdk.builders.piechart import Panel as PieChart
    from grafana_foundation_sdk.builders.stat import Panel as Stat
    from grafana_foundation_sdk.builders.table import Panel as Table
    from grafana_foundation_sdk.builders.timeseries import Panel as Timeseries
    from grafana_foundation_sdk.cog.encoder import JSONEncoder
    from grafana_foundation_sdk.models.dashboard import (
        GridPos,
        VariableOption,
        VariableRefresh,
        VariableSort,
    )

    if not isinstance(dashboard_plan, dict):
        raise DashboardCodeError("dashboard_plan must be a JSON object.")

    datasource_uid = datasource_uid.strip()
    if not datasource_uid:
        raise DashboardCodeError("datasource_uid is required.")

    plan_title = str(title or dashboard_plan.get("title") or "").strip()
    if not plan_title:
        raise DashboardCodeError("dashboard_plan.title is required.")
    dashboard_uid = _slugify_uid(uid or str(dashboard_plan.get("uid") or plan_title))
    time_field = str(dashboard_plan.get("time_field") or time_field or "@timestamp").strip()
    base_query = str(dashboard_plan.get("query") or query or "*").strip() or "*"
    time_from_value = str(time_from or dashboard_plan.get("time_from") or "now-6h")
    time_to_value = str(time_to or dashboard_plan.get("time_to") or "now")
    refresh_value = str(refresh if refresh is not None else dashboard_plan.get("refresh", ""))

    panels = dashboard_plan.get("panels")
    if not isinstance(panels, list) or not panels:
        raise DashboardCodeError("dashboard_plan.panels must contain at least one panel.")

    datasource = {"type": "grafana-opensearch-datasource", "uid": datasource_uid}
    variable_specs = _planned_variables(dashboard_plan)
    description = str(dashboard_plan.get("description") or "").strip()
    if not description:
        description = (
            "Generated by the Grafana Assistant PydanticAI/LangGraph dashboard agent from an explicit dashboard plan."
            + (f" Index pattern: {index_pattern}." if index_pattern else "")
        )

    dashboard = (
        Dashboard(plan_title)
        .uid(dashboard_uid)
        .description(description)
        .refresh(refresh_value)
        .time(time_from_value, time_to_value)
        .tags(["opensearch", "ai-planned"])
        .timezone("browser")
    )

    for spec in variable_specs:
        dashboard = dashboard.with_variable(
            QueryVariable(spec.name)
            .label(spec.label)
            .datasource(datasource)
            .query({"find": "terms", "field": spec.field})
            .refresh(VariableRefresh.ON_DASHBOARD_LOAD)
            .sort(VariableSort.ALPHABETICAL_ASC)
            .include_all(True)
            .multi(True)
            .all_value("*")
            .current(VariableOption(text=["$__all"], value=["$__all"]))
        )

    panel_builders = {
        "stat": Stat,
        "gauge": Gauge,
        "timeseries": Timeseries,
        "piechart": PieChart,
        "bargauge": Bargauge,
        "barchart": BarChart,
        "table": Table,
    }

    panel_id = 1
    ref_index = 0
    cursor = {"x": 0, "y": 0}
    current_row: str | None = None

    for index, panel_plan in enumerate(panels, start=1):
        if not isinstance(panel_plan, dict):
            raise DashboardCodeError(f"dashboard_plan.panels[{index}] must be an object.")
        panel_title = str(panel_plan.get("title") or "").strip()
        if not panel_title:
            raise DashboardCodeError(f"dashboard_plan.panels[{index}] is missing title.")
        panel_type = _normalize_panel_type(panel_plan.get("type") or panel_plan.get("chart_type"))
        row_title = str(panel_plan.get("row") or panel_plan.get("section") or "").strip()
        if row_title and row_title != current_row:
            dashboard = dashboard.with_row(
                Row(row_title)
                .id(panel_id)
                .grid_pos(GridPos(h=1, w=24, x=0, y=cursor["y"]))
                .collapsed(False)
            )
            panel_id += 1
            cursor = {"x": 0, "y": cursor["y"] + 1}
            current_row = row_title

        metric_type = str(
            panel_plan.get("metric_type")
            or ("raw_data" if panel_type == "table" and not panel_plan.get("group_by_field") else "count")
        )
        metric_field = panel_plan.get("metric_field")
        metric_field = str(metric_field).strip() if metric_field is not None else None
        raw_size = int(panel_plan.get("raw_document_limit") or panel_plan.get("raw_size") or 50)
        metric = _metric_builder(metric_type, metric_field, raw_size=raw_size)

        group_by = str(panel_plan.get("group_by_field") or panel_plan.get("group_by") or "").strip()
        panel_query = str(panel_plan.get("query") or base_query).strip() or "*"
        panel_query = _query_with_variable_filters(
            panel_query,
            variable_specs,
        )

        bucket_aggs: list[Any] = []
        if panel_type == "timeseries":
            if group_by:
                bucket_aggs.append(
                    Terms()
                    .id("3")
                    .field(group_by)
                    .settings(_terms_settings(size=int(panel_plan.get("terms_size") or 10), order_by="_term"))
                )
            bucket_aggs.append(_date_histogram(time_field, id_val="2"))
        elif panel_type in {"piechart", "bargauge", "barchart"}:
            if not group_by:
                raise DashboardCodeError(
                    f"Planned {panel_type} panel {panel_title!r} requires group_by_field."
                )
            normalized_metric = metric_type.strip().lower().replace("-", "_")
            order_by = "1" if normalized_metric not in {"count", "doc_count", "documents"} else "_count"
            bucket_aggs.append(
                Terms()
                .id("2")
                .field(group_by)
                .settings(_terms_settings(size=int(panel_plan.get("terms_size") or 10), order_by=order_by))
            )
        elif panel_type == "table" and group_by:
            bucket_aggs.append(
                Terms()
                .id("2")
                .field(group_by)
                .settings(_terms_settings(size=int(panel_plan.get("terms_size") or 10)))
            )

        target = (
            Dataquery()
            .ref_id(_ref_id(ref_index))
            .query(panel_query)
            .time_field(time_field)
            .datasource(datasource)
            .metrics([metric])
            .bucket_aggs(bucket_aggs)
        )
        ref_index += 1

        grid = _planned_grid_pos(panel_plan, panel_type=panel_type, cursor=cursor)
        panel = (
            panel_builders[panel_type]()
            .id(panel_id)
            .title(panel_title)
            .grid_pos(GridPos(h=grid["h"], w=grid["w"], x=grid["x"], y=grid["y"]))
            .datasource(datasource)
            .with_target(target)
        )
        panel_id += 1
        panel_description = str(panel_plan.get("description") or "").strip()
        if panel_description:
            panel = panel.description(panel_description)
        unit = str(panel_plan.get("unit") or _unit_for_field(metric_field or "") or "").strip()
        if unit and panel_type != "table":
            panel = panel.unit(unit)

        dashboard = dashboard.with_panel(panel)

    json_text = JSONEncoder(sort_keys=True, indent=2).encode(dashboard.build())
    parsed = json.loads(json_text)
    _validate_dashboard_model(parsed)
    data_panels = [panel for panel in parsed.get("panels", []) if panel.get("type") != "row"]
    if len(data_panels) != len(panels):
        raise DashboardCodeError(
            "Planned dashboard compiler created a different number of data panels than requested."
        )
    return json_text


def _build_opensearch_single_chart_dashboard_json(
    title: str,
    datasource_uid: str,
    chart_type: str = "piechart",
    time_field: str = "@timestamp",
    uid: str | None = None,
    query: str = "*",
    time_from: str = "now-6h",
    time_to: str = "now",
    refresh: str = "",
    metric_type: str = "count",
    metric_field: str | None = None,
    group_by_field: str | None = None,
    variable_fields: list[str] | None = None,
    include_variables: bool = False,
    index_pattern: str | None = None,
    raw_document_limit: int = 50,
) -> str:
    from grafana_foundation_sdk.builders.barchart import Panel as BarChart
    from grafana_foundation_sdk.builders.bargauge import Panel as Bargauge
    from grafana_foundation_sdk.builders.dashboard import Dashboard, QueryVariable
    from grafana_foundation_sdk.builders.elasticsearch import Dataquery, Terms
    from grafana_foundation_sdk.builders.piechart import Panel as PieChart
    from grafana_foundation_sdk.builders.stat import Panel as Stat
    from grafana_foundation_sdk.builders.table import Panel as Table
    from grafana_foundation_sdk.builders.timeseries import Panel as Timeseries
    from grafana_foundation_sdk.cog.encoder import JSONEncoder
    from grafana_foundation_sdk.models.dashboard import (
        GridPos,
        VariableOption,
        VariableRefresh,
        VariableSort,
    )

    title = title.strip() or "OpenSearch chart"
    uid = _slugify_uid(uid or title)
    datasource_uid = datasource_uid.strip()
    if not datasource_uid:
        raise DashboardCodeError("datasource_uid is required.")

    chart_type = chart_type.strip().lower().replace("-", "")
    aliases = {
        "pie": "piechart",
        "donut": "piechart",
        "bar": "barchart",
        "bars": "barchart",
        "bargraph": "barchart",
        "barGauge": "bargauge",
        "bar_gauge": "bargauge",
        "time_series": "timeseries",
        "timechart": "timeseries",
        "line": "timeseries",
    }
    chart_type = aliases.get(chart_type, chart_type)
    supported = {"piechart", "bargauge", "barchart", "timeseries", "stat", "table"}
    if chart_type not in supported:
        raise DashboardCodeError(
            "Unsupported chart_type. Use piechart, bargauge, barchart, timeseries, stat, or table."
        )

    time_field = time_field.strip() or "@timestamp"
    query = query.strip() or "*"
    group_by = (group_by_field or "").strip()
    grouped_chart = chart_type in {"piechart", "bargauge", "barchart"}
    if grouped_chart and not group_by:
        raise DashboardCodeError(f"group_by_field is required for chart_type={chart_type!r}.")

    datasource = {"type": "grafana-opensearch-datasource", "uid": datasource_uid}
    variable_specs = (
        _variable_specs(_clean_fields(variable_fields, exclude={time_field}), limit=2)
        if include_variables
        else []
    )
    excluded_filter_fields = {group_by} if group_by else set()
    filtered_query = _query_with_variable_filters(
        query,
        variable_specs,
        exclude_fields=excluded_filter_fields,
    )

    metric = _metric_builder(metric_type, metric_field, raw_size=raw_document_limit)
    bucket_aggs: list[Any] = []
    if chart_type == "timeseries":
        if group_by:
            bucket_aggs.append(
                Terms()
                .id("3")
                .field(group_by)
                .settings(_terms_settings(size=10, order_by="_term"))
            )
        bucket_aggs.append(_date_histogram(time_field, id_val="2"))
    elif grouped_chart:
        order_by = "1" if metric_type.strip().lower() not in {"count", "doc_count"} else "_count"
        bucket_aggs.append(
            Terms().id("2").field(group_by).settings(_terms_settings(size=10, order_by=order_by))
        )

    target = (
        Dataquery()
        .ref_id("A")
        .query(filtered_query)
        .time_field(time_field)
        .datasource(datasource)
        .metrics([metric])
        .bucket_aggs(bucket_aggs)
    )

    dashboard = (
        Dashboard(title)
        .uid(uid)
        .description(
            "Generated by the Grafana Assistant PydanticAI/LangGraph dashboard agent from a focused OpenSearch chart request."
            + (f" Index pattern: {index_pattern}." if index_pattern else "")
        )
        .refresh(refresh)
        .time(time_from, time_to)
        .tags(["opensearch", "ai-generated"])
        .timezone("browser")
    )

    for spec in variable_specs:
        dashboard = dashboard.with_variable(
            QueryVariable(spec.name)
            .label(spec.label)
            .datasource(datasource)
            .query({"find": "terms", "field": spec.field})
            .refresh(VariableRefresh.ON_DASHBOARD_LOAD)
            .sort(VariableSort.ALPHABETICAL_ASC)
            .include_all(True)
            .multi(True)
            .all_value("*")
            .current(VariableOption(text=["$__all"], value=["$__all"]))
        )

    panel_builders = {
        "piechart": PieChart,
        "bargauge": Bargauge,
        "barchart": BarChart,
        "timeseries": Timeseries,
        "stat": Stat,
        "table": Table,
    }
    panel = (
        panel_builders[chart_type]()
        .id(1)
        .title(title)
        .grid_pos(GridPos(h=10, w=24, x=0, y=0))
        .datasource(datasource)
        .with_target(target)
    )
    unit = _unit_for_field(metric_field or "")
    if unit and chart_type != "table":
        panel = panel.unit(unit)

    dashboard = dashboard.with_panel(panel)
    json_text = JSONEncoder(sort_keys=True, indent=2).encode(dashboard.build())
    parsed = json.loads(json_text)
    _validate_dashboard_model(parsed)
    if len([p for p in parsed.get("panels", []) if p.get("type") != "row"]) != 1:
        raise DashboardCodeError("Focused chart dashboard must contain exactly one data panel.")
    return json_text


def _build_opensearch_dashboard_json(
    title: str,
    datasource_uid: str,
    time_field: str = "@timestamp",
    uid: str | None = None,
    query: str = "*",
    time_from: str = "now-6h",
    time_to: str = "now",
    refresh: str = "30s",
    numeric_fields: list[str] | None = None,
    categorical_fields: list[str] | None = None,
    table_fields: list[str] | None = None,
    max_numeric_panels: int = 6,
    max_category_panels: int = 4,
    raw_document_limit: int = 50,
    index_pattern: str | None = None,
    variable_fields: list[str] | None = None,
    group_by_field: str | None = None,
    include_variables: bool = True,
) -> str:
    """Build and validate the JSON for build_opensearch_dashboard_tool."""
    from grafana_foundation_sdk.builders.bargauge import Panel as Bargauge
    from grafana_foundation_sdk.builders.dashboard import Dashboard, QueryVariable, Row
    from grafana_foundation_sdk.builders.elasticsearch import (
        Average,
        Count,
        Dataquery,
        ElasticsearchRawDataSettings,
        Max,
        Min,
        RawData,
        Sum,
        Terms,
        UniqueCount,
    )
    from grafana_foundation_sdk.builders.piechart import Panel as PieChart
    from grafana_foundation_sdk.builders.stat import Panel as Stat
    from grafana_foundation_sdk.builders.table import Panel as Table
    from grafana_foundation_sdk.builders.timeseries import Panel as Timeseries
    from grafana_foundation_sdk.cog.encoder import JSONEncoder
    from grafana_foundation_sdk.models.dashboard import (
        GridPos,
        VariableOption,
        VariableRefresh,
        VariableSort,
    )

    title = title.strip() or "OpenSearch dashboard"
    uid = _slugify_uid(uid or title)
    datasource_uid = datasource_uid.strip()
    if not datasource_uid:
        raise DashboardCodeError("datasource_uid is required.")

    time_field = time_field.strip() or "@timestamp"
    query = query.strip() or "*"
    numeric_fields = _rank_numeric_fields(
        _clean_fields(numeric_fields, exclude={time_field})
    )[: max(0, max_numeric_panels)]
    categorical_fields = _rank_categorical_fields(
        _clean_fields(categorical_fields, exclude={time_field})
    )[: max(0, max_category_panels)]
    table_fields = _clean_fields(table_fields, exclude=set())
    variable_source_fields = _clean_fields(variable_fields or categorical_fields, exclude={time_field})
    variable_specs = _variable_specs(variable_source_fields, limit=2) if include_variables else []
    filtered_query = _query_with_variable_filters(query, variable_specs)

    if not numeric_fields:
        raise DashboardCodeError("At least one numeric field is required for KPI and trend panels.")
    if not categorical_fields:
        raise DashboardCodeError(
            "At least one categorical field is required for variables and distribution panels."
        )

    group_by = (group_by_field or categorical_fields[0]).strip()
    if group_by not in categorical_fields:
        categorical_fields = [group_by, *categorical_fields]

    datasource = {"type": "grafana-opensearch-datasource", "uid": datasource_uid}
    panel_id = 1
    ref_index = 0

    def next_panel_id() -> int:
        nonlocal panel_id
        value = panel_id
        panel_id += 1
        return value

    def next_ref() -> str:
        nonlocal ref_index
        value = _ref_id(ref_index)
        ref_index += 1
        return value

    def add_row(dashboard: Any, title: str, y: int) -> Any:
        return dashboard.with_row(
            Row(title)
            .id(next_panel_id())
            .grid_pos(GridPos(h=1, w=24, x=0, y=y))
            .collapsed(False)
        )

    def metric_target(ref: str, metric: Any, *, buckets: list[Any] | None = None) -> Any:
        return (
            Dataquery()
            .ref_id(ref)
            .query(filtered_query)
            .time_field(time_field)
            .datasource(datasource)
            .metrics([metric])
            .bucket_aggs(buckets or [])
        )

    def grouped_buckets() -> list[Any]:
        return [
            Terms().id("3").field(group_by).settings(_terms_settings(size=10, order_by="_term")),
            _date_histogram(time_field, id_val="2"),
        ]

    dashboard = (
        Dashboard(title)
        .uid(uid)
        .description(
            "Generated by the Grafana Assistant PydanticAI/LangGraph dashboard agent from OpenSearch metadata."
            + (f" Index pattern: {index_pattern}." if index_pattern else "")
        )
        .refresh(refresh)
        .time(time_from, time_to)
        .tags(["opensearch", "ai-generated"])
        .timezone("browser")
    )

    for spec in variable_specs:
        dashboard = dashboard.with_variable(
            QueryVariable(spec.name)
            .label(spec.label)
            .datasource(datasource)
            .query({"find": "terms", "field": spec.field})
            .refresh(VariableRefresh.ON_DASHBOARD_LOAD)
            .sort(VariableSort.ALPHABETICAL_ASC)
            .include_all(True)
            .multi(True)
            .all_value("*")
            .current(VariableOption(text=["$__all"], value=["$__all"]))
        )

    dashboard = add_row(dashboard, "Key Performance Indicators", 0)

    kpi_specs: list[tuple[str, Any, str | None, str]] = []
    for field in numeric_fields[:4]:
        kpi_specs.append(
            (
                f"Avg {_display_name(field)}",
                Average().id("1").field(field),
                _unit_for_field(field),
                f"Average `{field}` over the dashboard time range.",
            )
        )

    if len(kpi_specs) < 4:
        kpi_specs.append(
            (
                "Matching Documents",
                Count().id("1"),
                "short",
                "Total matching documents in the dashboard time range.",
            )
        )
    for field in categorical_fields:
        if len(kpi_specs) >= 4:
            break
        kpi_specs.append(
            (
                f"Unique {_display_name(field)}",
                UniqueCount().id("1").field(field),
                "short",
                f"Unique count of `{field}` in the dashboard time range.",
            )
        )
    if len(kpi_specs) < 4:
        first_metric = numeric_fields[0]
        kpi_specs.extend(
            [
                (
                    f"Max {_display_name(first_metric)}",
                    Max().id("1").field(first_metric),
                    _unit_for_field(first_metric),
                    f"Maximum `{first_metric}` in the dashboard time range.",
                ),
                (
                    f"Min {_display_name(first_metric)}",
                    Min().id("1").field(first_metric),
                    _unit_for_field(first_metric),
                    f"Minimum `{first_metric}` in the dashboard time range.",
                ),
            ]
        )
    kpi_specs = kpi_specs[:4]

    for index, (panel_title, metric, unit, description) in enumerate(kpi_specs):
        panel = (
            Stat()
            .id(next_panel_id())
            .title(panel_title)
            .description(description)
            .grid_pos(GridPos(h=4, w=6, x=index * 6, y=1))
            .datasource(datasource)
            .with_target(metric_target(next_ref(), metric, buckets=[_date_histogram(time_field)]))
        )
        if unit:
            panel = panel.unit(unit)
        dashboard = dashboard.with_panel(panel)

    dashboard = add_row(dashboard, "Telemetry Trends", 5)

    trend_fields = numeric_fields[:3]
    while len(trend_fields) < 2:
        trend_fields.append(numeric_fields[0])
    trend_width = 8 if len(trend_fields[:3]) == 3 else 12
    for index, field in enumerate(trend_fields[:3]):
        target = (
            Dataquery()
            .ref_id(next_ref())
            .query(_query_with_variable_filters(query, variable_specs, exclude_fields={group_by}))
            .alias(f"avg {field} by {group_by}")
            .time_field(time_field)
            .datasource(datasource)
            .metrics([Average().id("1").field(field)])
            .bucket_aggs(grouped_buckets())
        )
        panel = (
            Timeseries()
            .id(next_panel_id())
            .title(f"{_display_name(field)} by {_display_name(group_by)}")
            .description(f"Average `{field}` over time split by `{group_by}`.")
            .grid_pos(GridPos(h=8, w=trend_width, x=trend_width * index, y=6))
            .datasource(datasource)
            .with_target(target)
        )
        unit = _unit_for_field(field)
        if unit:
            panel = panel.unit(unit)
        dashboard = dashboard.with_panel(panel)

    dashboard = add_row(dashboard, "Distribution & Deep Dive", 14)

    pie_target = (
        Dataquery()
        .ref_id(next_ref())
        .query(_query_with_variable_filters(query, variable_specs, exclude_fields={group_by}))
        .time_field(time_field)
        .datasource(datasource)
        .metrics([Count().id("1")])
        .bucket_aggs([Terms().id("2").field(group_by).settings(_terms_settings(size=10))])
    )
    dashboard = dashboard.with_panel(
        PieChart()
        .id(next_panel_id())
        .title(f"Documents by {_display_name(group_by)}")
        .description(f"Document count distribution across `{group_by}`.")
        .grid_pos(GridPos(h=8, w=8, x=0, y=15))
        .datasource(datasource)
        .unit("short")
        .with_target(pie_target)
    )

    primary_metric = next(
        (field for field in numeric_fields if "power" in field.lower()),
        numeric_fields[0],
    )
    primary_metric_builder = (
        Sum().id("1").field(primary_metric)
        if "power" in primary_metric.lower()
        else Average().id("1").field(primary_metric)
    )
    primary_metric_label = "Total" if "power" in primary_metric.lower() else "Avg"
    bar_target = (
        Dataquery()
        .ref_id(next_ref())
        .query(_query_with_variable_filters(query, variable_specs, exclude_fields={group_by}))
        .time_field(time_field)
        .datasource(datasource)
        .metrics([primary_metric_builder])
        .bucket_aggs([Terms().id("2").field(group_by).settings(_terms_settings(size=10, order_by="1"))])
    )
    bar_panel = (
        Bargauge()
        .id(next_panel_id())
        .title(f"{primary_metric_label} {_display_name(primary_metric)} by {_display_name(group_by)}")
        .description(f"{primary_metric_label} `{primary_metric}` grouped by `{group_by}`.")
        .grid_pos(GridPos(h=8, w=8, x=8, y=15))
        .datasource(datasource)
        .with_target(bar_target)
    )
    unit = _unit_for_field(primary_metric)
    if unit:
        bar_panel = bar_panel.unit(unit)
    dashboard = dashboard.with_panel(bar_panel)

    top_field = categorical_fields[1] if len(categorical_fields) > 1 else group_by
    top_target = (
        Dataquery()
        .ref_id(next_ref())
        .query(_query_with_variable_filters(query, variable_specs, exclude_fields={top_field}))
        .time_field(time_field)
        .datasource(datasource)
        .metrics([Count().id("1")])
        .bucket_aggs([Terms().id("2").field(top_field).settings(_terms_settings(size=10))])
    )
    dashboard = dashboard.with_panel(
        Table()
        .id(next_panel_id())
        .title(f"Top {_display_name(top_field)}")
        .description(f"Top `{top_field}` values by matching document count.")
        .grid_pos(GridPos(h=8, w=8, x=16, y=15))
        .datasource(datasource)
        .with_target(top_target)
    )

    dashboard = add_row(dashboard, "Raw Documents", 23)

    raw_target = (
        Dataquery()
        .ref_id(next_ref())
        .query(filtered_query)
        .time_field(time_field)
        .datasource(datasource)
        .metrics(
            [
                RawData()
                .id("1")
                .settings(ElasticsearchRawDataSettings().size(str(max(1, raw_document_limit))))
            ]
        )
        .bucket_aggs([])
    )
    raw_title = "Recent documents"
    if table_fields:
        raw_title = "Recent documents: " + ", ".join(table_fields[:4])
    dashboard = dashboard.with_panel(
        Table()
        .id(next_panel_id())
        .title(raw_title)
        .description("Raw matching documents for spot checks and debugging empty aggregations.")
        .grid_pos(GridPos(h=10, w=24, x=0, y=24))
        .datasource(datasource)
        .with_target(raw_target)
    )

    json_text = JSONEncoder(sort_keys=True, indent=2).encode(dashboard.build())
    parsed = json.loads(json_text)
    _validate_dashboard_model(parsed)
    _validate_rich_opensearch_dashboard(parsed, require_variables=include_variables)

    return json_text
