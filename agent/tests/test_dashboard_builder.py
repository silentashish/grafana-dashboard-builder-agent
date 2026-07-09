from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")

from tools.dashboard_builder import (
    DashboardCodeError,
    DashboardVerificationResult,
    _save_dashboard_with_verification,
    build_and_save_opensearch_chart_dashboard_tool,
    build_and_save_opensearch_dashboard_tool,
    build_and_save_opensearch_planned_dashboard_tool,
    build_opensearch_chart_dashboard_tool,
    build_opensearch_dashboard_tool,
    build_opensearch_planned_dashboard_tool,
    run_dashboard_code,
    summarize_opensearch_mapping_tool,
)


class DashboardBuilderTests(unittest.TestCase):
    def test_build_opensearch_chart_dashboard_tool_creates_single_pie_chart(self) -> None:
        dashboard_json = build_opensearch_chart_dashboard_tool(
            None,
            title="System state distribution",
            uid="system-state-pie",
            datasource_uid="opensearch-default",
            chart_type="piechart",
            time_field="@timestamp",
            query="*",
            group_by_field="system_state.keyword",
            metric_type="count",
            time_from="2010-01-01T00:00:00Z",
            time_to="2010-02-05T00:00:00Z",
            index_pattern="trades-*",
        )

        dashboard = json.loads(dashboard_json)
        panels = dashboard["panels"]

        self.assertEqual(dashboard["uid"], "system-state-pie")
        self.assertEqual(len(panels), 1)
        self.assertEqual(panels[0]["type"], "piechart")
        self.assertEqual(panels[0]["gridPos"], {"h": 10, "w": 24, "x": 0, "y": 0})
        self.assertFalse((dashboard.get("templating") or {}).get("list"))
        target = panels[0]["targets"][0]
        self.assertEqual(target["datasource"]["uid"], "opensearch-default")
        self.assertEqual(target["metrics"], [{"id": "1", "type": "count"}])
        self.assertEqual(target["query"], "*")
        self.assertEqual(target["bucketAggs"][0]["type"], "terms")
        self.assertEqual(target["bucketAggs"][0]["field"], "system_state.keyword")

    def test_build_opensearch_planned_dashboard_tool_uses_exact_plan(self) -> None:
        plan = {
            "title": "Trades planned overview",
            "variables": [
                {
                    "name": "equipment_group",
                    "label": "Equipment Group",
                    "field": "equipment_group.keyword",
                }
            ],
            "panels": [
                {
                    "title": "Temperature by equipment group",
                    "type": "timeseries",
                    "metric_type": "avg",
                    "metric_field": "temperature_c",
                    "group_by_field": "equipment_group.keyword",
                    "row": "Telemetry",
                    "gridPos": {"h": 8, "w": 12, "x": 0, "y": 1},
                },
                {
                    "title": "System state distribution",
                    "type": "piechart",
                    "metric_type": "count",
                    "group_by_field": "system_state.keyword",
                    "row": "Distribution",
                    "gridPos": {"h": 8, "w": 12, "x": 12, "y": 1},
                },
            ],
        }

        dashboard_json = build_opensearch_planned_dashboard_tool(
            None,
            dashboard_plan=plan,
            datasource_uid="opensearch-default",
            time_field="@timestamp",
            query="*",
            time_from="2010-01-01T00:00:00Z",
            time_to="2010-02-05T00:00:00Z",
            index_pattern="trades-*",
        )

        dashboard = json.loads(dashboard_json)
        data_panels = [panel for panel in dashboard["panels"] if panel["type"] != "row"]

        self.assertEqual(dashboard["title"], "Trades planned overview")
        self.assertEqual(len(data_panels), 2)
        self.assertEqual([panel["type"] for panel in data_panels], ["timeseries", "piechart"])
        self.assertNotIn("table", {panel["type"] for panel in dashboard["panels"]})
        self.assertEqual(
            [variable["name"] for variable in dashboard["templating"]["list"]],
            ["equipment_group"],
        )

        trend_target = data_panels[0]["targets"][0]
        self.assertEqual(trend_target["bucketAggs"][0]["type"], "terms")
        self.assertEqual(trend_target["bucketAggs"][1]["type"], "date_histogram")
        self.assertIn("${equipment_group:regex}", trend_target["query"])
        self.assertEqual(trend_target["datasource"]["uid"], "opensearch-default")

    def test_build_opensearch_dashboard_tool_creates_detailed_dashboard(self) -> None:
        dashboard_json = build_opensearch_dashboard_tool(
            None,
            title="Trades overview",
            uid="trades-overview",
            datasource_uid="opensearch-default",
            time_field="@timestamp",
            query='test_bed:"trades"',
            numeric_fields=["price", "volume"],
            categorical_fields=["symbol.keyword", "test_bed.keyword"],
            table_fields=["@timestamp", "symbol", "price", "volume"],
        )

        dashboard = json.loads(dashboard_json)
        panels = dashboard["panels"]

        self.assertEqual(dashboard["uid"], "trades-overview")
        self.assertGreaterEqual(len(panels), 10)
        self.assertEqual({panel["id"] for panel in panels}, set(range(1, len(panels) + 1)))
        self.assertIn("Key Performance Indicators", {panel["title"] for panel in panels})
        self.assertIn("Telemetry Trends", {panel["title"] for panel in panels})
        self.assertIn("Distribution & Deep Dive", {panel["title"] for panel in panels})
        self.assertIn("Avg Price", {panel["title"] for panel in panels})
        self.assertIn("Top Test Bed", {panel["title"] for panel in panels})
        self.assertIn("piechart", {panel["type"] for panel in panels})
        self.assertIn("bargauge", {panel["type"] for panel in panels})

        variables = dashboard["templating"]["list"]
        self.assertEqual([variable["name"] for variable in variables], ["symbol", "test_bed"])

        all_queries: list[str] = []
        for panel in panels:
            grid_pos = panel["gridPos"]
            self.assertLessEqual(grid_pos["x"] + grid_pos["w"], 24)
            self.assertGreater(grid_pos["h"], 0)
            if panel["type"] == "row":
                continue
            self.assertEqual(panel["datasource"]["uid"], "opensearch-default")
            for target in panel.get("targets", []):
                self.assertEqual(target["timeField"], "@timestamp")
                self.assertEqual(target["datasource"]["uid"], "opensearch-default")
                self.assertIn("metrics", target)
                all_queries.append(target["query"])

        joined_queries = "\n".join(all_queries)
        self.assertIn("${symbol:regex}", joined_queries)
        self.assertIn("${test_bed:regex}", joined_queries)
        self.assertNotIn(":lucene", joined_queries)

    def test_trades_trace_dashboard_shape(self) -> None:
        dashboard_json = build_opensearch_dashboard_tool(
            None,
            title="Trades Sample Dashboard abc123",
            uid="trades-sample-dashboard-abc123",
            datasource_uid="opensearch-default",
            time_field="@timestamp",
            query="*",
            time_from="2009-01-01",
            time_to="2010-12-31",
            numeric_fields=[
                "co2_ppm",
                "temperature_c",
                "pressure_kpa",
                "flow_rate_lpm",
                "power_kw",
                "voltage_v",
                "current_a",
                "vibration_mm_s",
            ],
            categorical_fields=[
                "equipment_group.keyword",
                "site.keyword",
                "system_state.keyword",
            ],
            table_fields=[
                "@timestamp",
                "site",
                "equipment_group",
                "system_state",
                "temperature_c",
                "pressure_kpa",
            ],
            index_pattern="trades-*",
        )

        dashboard = json.loads(dashboard_json)
        panels = dashboard["panels"]
        data_panels = [panel for panel in panels if panel["type"] != "row"]

        self.assertEqual(dashboard["time"], {"from": "2009-01-01", "to": "2010-12-31"})
        self.assertEqual(
            [variable["name"] for variable in dashboard["templating"]["list"]],
            ["equipment_group", "system_state"],
        )
        self.assertEqual(
            [panel["title"] for panel in data_panels if panel["type"] == "stat"][:4],
            [
                "Avg Temperature C",
                "Avg Power Kw",
                "Avg Vibration Mm S",
                "Avg Pressure Kpa",
            ],
        )
        self.assertGreaterEqual(
            len([panel for panel in data_panels if panel["type"] == "stat"]),
            4,
        )
        self.assertGreaterEqual(
            len([panel for panel in data_panels if panel["type"] == "timeseries"]),
            2,
        )
        self.assertIn("piechart", {panel["type"] for panel in data_panels})
        self.assertIn("bargauge", {panel["type"] for panel in data_panels})
        self.assertTrue(
            any(
                metric["type"] == "raw_document"
                or metric["type"] == "raw_data"
                for panel in data_panels
                for target in panel.get("targets", [])
                for metric in target.get("metrics", [])
            )
        )
        for panel in data_panels:
            for target in panel["targets"]:
                self.assertEqual(target["timeField"], "@timestamp")
                self.assertEqual(target["datasource"]["uid"], "opensearch-default")

        trend = next(panel for panel in data_panels if panel["type"] == "timeseries")
        self.assertEqual(trend["targets"][0]["bucketAggs"][0]["type"], "terms")
        self.assertEqual(trend["targets"][0]["bucketAggs"][1]["type"], "date_histogram")
        self.assertIn("${system_state:regex}", trend["targets"][0]["query"])
        self.assertNotIn("${equipment_group:regex}", trend["targets"][0]["query"])

        for variable in dashboard["templating"]["list"]:
            self.assertEqual(variable["current"], {"text": ["$__all"], "value": ["$__all"]})
            self.assertEqual(variable["refresh"], 1)

    def test_run_dashboard_code_rejects_missing_time_field(self) -> None:
        code = """
from grafana_foundation_sdk.builders.dashboard import Dashboard
from grafana_foundation_sdk.builders.stat import Panel as Stat
from grafana_foundation_sdk.builders.elasticsearch import Dataquery, Count
from grafana_foundation_sdk.models.dashboard import GridPos
from grafana_foundation_sdk.cog.encoder import JSONEncoder

DS = {"type": "grafana-opensearch-datasource", "uid": "opensearch-default"}
target = Dataquery().ref_id("A").query("*").metrics([Count().id("1")]).bucket_aggs([])
dashboard = (
    Dashboard("Broken dash")
    .uid("broken-dash")
    .with_panel(
        Stat()
        .id(1)
        .title("Count")
        .grid_pos(GridPos(h=4, w=8, x=0, y=0))
        .datasource(DS)
        .with_target(target)
    )
)
print(JSONEncoder(sort_keys=True, indent=2).encode(dashboard.build()))
"""

        with self.assertRaisesRegex(DashboardCodeError, "timeField"):
            run_dashboard_code(code)

    def test_run_dashboard_code_rejects_hallucinated_templating_import(self) -> None:
        code = """
from grafana_foundation_sdk.models.templating import Templating
print("{}")
"""

        with self.assertRaisesRegex(DashboardCodeError, "forbidden SDK API"):
            run_dashboard_code(code)

    def test_run_dashboard_code_rejects_raw_metric_dicts(self) -> None:
        code = """
from grafana_foundation_sdk.builders.dashboard import Dashboard
from grafana_foundation_sdk.builders.stat import Panel as Stat
from grafana_foundation_sdk.builders.elasticsearch import Dataquery
from grafana_foundation_sdk.models.dashboard import GridPos
from grafana_foundation_sdk.cog.encoder import JSONEncoder

DS = {"type": "grafana-opensearch-datasource", "uid": "opensearch-default"}
target = (
    Dataquery()
    .ref_id("A")
    .query("*")
    .time_field("@timestamp")
    .metrics([{"id": "1", "type": "count"}])
    .bucket_aggs([])
)
dashboard = (
    Dashboard("Broken raw dict")
    .uid("broken-raw-dict")
    .with_panel(
        Stat()
        .id(1)
        .title("Count")
        .grid_pos(GridPos(h=4, w=8, x=0, y=0))
        .datasource(DS)
        .with_target(target)
    )
)
print(JSONEncoder(sort_keys=True, indent=2).encode(dashboard.build()))
"""

        with self.assertRaises(DashboardCodeError) as exc:
            run_dashboard_code(code)
        self.assertIn("dict", exc.exception.stderr)

    def test_summarize_opensearch_mapping_tool_extracts_dashboard_fields(self) -> None:
        mapping = {
            "trades-2010.01.01": {
                "mappings": {
                    "properties": {
                        "@timestamp": {"type": "date"},
                        "temperature_c": {"type": "float"},
                        "pressure_kpa": {"type": "float"},
                        "equipment_group": {
                            "type": "text",
                            "fields": {"keyword": {"type": "keyword"}},
                        },
                        "site": {
                            "type": "text",
                            "fields": {"keyword": {"type": "keyword"}},
                        },
                    }
                }
            }
        }

        summary = json.loads(
            summarize_opensearch_mapping_tool(
                None,
                "Mapping for trades-*:\n" + json.dumps(mapping),
                preferred_index_pattern="trades-*",
            )
        )

        self.assertEqual(summary["time_field"], "@timestamp")
        self.assertEqual(summary["index_pattern"], "trades-*")
        self.assertIn("temperature_c", summary["numeric_fields"])
        self.assertIn("equipment_group.keyword", summary["categorical_fields"])

    def test_build_and_save_opensearch_dashboard_tool_posts_to_grafana(self) -> None:
        with patch(
            "tools.dashboard_builder._post_grafana_dashboard",
            return_value={
                "status": "success",
                "uid": "trades-overview",
                "url": "/d/trades-overview/trades-overview",
                "version": 7,
            },
        ) as post, patch(
            "tools.dashboard_builder._verify_dashboard_saved_and_working",
            return_value=DashboardVerificationResult(
                valid=True,
                errors=[],
                warnings=[],
                checked_panels=15,
                queried_panels=15,
                panels_with_data=10,
            ),
        ) as verify:
            result = json.loads(
                build_and_save_opensearch_dashboard_tool(
                    None,
                    title="Trades overview",
                    uid="trades-overview",
                    datasource_uid="opensearch-default",
                    time_field="@timestamp",
                    numeric_fields=["temperature_c", "pressure_kpa", "flow_rate_lpm", "power_kw"],
                    categorical_fields=["equipment_group.keyword", "site.keyword"],
                    table_fields=["@timestamp", "site", "equipment_group", "temperature_c"],
                    time_from="2009-01-01",
                    time_to="2010-12-31",
                    index_pattern="trades-*",
                )
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["uid"], "trades-overview")
        self.assertEqual(result["panel_count"], 15)
        self.assertEqual(result["variable_count"], 2)
        self.assertIs(result["verified"], True)
        post.assert_called_once()
        verify.assert_called_once()
        dashboard_arg = post.call_args.args[0]
        self.assertEqual(dashboard_arg["uid"], "trades-overview")

    def test_build_and_save_opensearch_chart_dashboard_tool_posts_single_panel(self) -> None:
        with patch(
            "tools.dashboard_builder._post_grafana_dashboard",
            return_value={
                "status": "success",
                "uid": "system-state-pie",
                "url": "/d/system-state-pie/system-state-pie",
                "version": 2,
            },
        ) as post, patch(
            "tools.dashboard_builder._verify_dashboard_saved_and_working",
            return_value=DashboardVerificationResult(
                valid=True,
                errors=[],
                warnings=[],
                checked_panels=1,
                queried_panels=1,
                panels_with_data=1,
            ),
        ) as verify:
            result = json.loads(
                build_and_save_opensearch_chart_dashboard_tool(
                    None,
                    title="System state distribution",
                    uid="system-state-pie",
                    datasource_uid="opensearch-default",
                    chart_type="piechart",
                    time_field="@timestamp",
                    group_by_field="system_state.keyword",
                    metric_type="count",
                )
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["uid"], "system-state-pie")
        self.assertEqual(result["panel_count"], 1)
        self.assertEqual(result["chart_type"], "piechart")
        self.assertIs(result["verified"], True)
        verify.assert_called_once()
        dashboard_arg = post.call_args.args[0]
        self.assertEqual(len(dashboard_arg["panels"]), 1)
        self.assertEqual(dashboard_arg["panels"][0]["type"], "piechart")

    def test_planned_save_requires_user_confirmation(self) -> None:
        plan = {
            "title": "Needs confirmation",
            "panels": [
                {
                    "title": "Documents",
                    "type": "stat",
                    "metric_type": "count",
                }
            ],
        }

        with patch("tools.dashboard_builder._post_grafana_dashboard") as post:
            result = build_and_save_opensearch_planned_dashboard_tool(
                None,
                dashboard_plan=plan,
                datasource_uid="opensearch-default",
                confirmed_by_user=False,
            )

        self.assertIn("confirmed_by_user=false", result)
        post.assert_not_called()

    def test_build_and_save_opensearch_planned_dashboard_tool_posts_plan(self) -> None:
        plan = {
            "title": "Planned save",
            "panels": [
                {
                    "title": "Documents",
                    "type": "stat",
                    "metric_type": "count",
                },
                {
                    "title": "By state",
                    "type": "piechart",
                    "metric_type": "count",
                    "group_by_field": "system_state.keyword",
                },
            ],
        }

        with patch(
            "tools.dashboard_builder._post_grafana_dashboard",
            return_value={
                "status": "success",
                "uid": "planned-save",
                "url": "/d/planned-save/planned-save",
                "version": 3,
            },
        ) as post, patch(
            "tools.dashboard_builder._verify_dashboard_saved_and_working",
            return_value=DashboardVerificationResult(
                valid=True,
                errors=[],
                warnings=[],
                checked_panels=2,
                queried_panels=2,
                panels_with_data=2,
            ),
        ) as verify:
            result = json.loads(
                build_and_save_opensearch_planned_dashboard_tool(
                    None,
                    dashboard_plan=plan,
                    datasource_uid="opensearch-default",
                    uid="planned-save",
                    confirmed_by_user=True,
                )
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["uid"], "planned-save")
        self.assertEqual(result["data_panel_count"], 2)
        self.assertIs(result["verified"], True)
        verify.assert_called_once()
        dashboard_arg = post.call_args.args[0]
        data_panels = [panel for panel in dashboard_arg["panels"] if panel["type"] != "row"]
        self.assertEqual([panel["title"] for panel in data_panels], ["Documents", "By state"])
        self.assertNotIn("table", {panel["type"] for panel in dashboard_arg["panels"]})

    def test_save_dashboard_with_verification_repairs_and_retries(self) -> None:
        dashboard = {
            "uid": "repair-me",
            "title": "Repair me",
            "panels": [
                {
                    "id": 1,
                    "title": "Broken table",
                    "type": "table",
                    "datasource": {
                        "type": "grafana-opensearch-datasource",
                        "uid": "opensearch-default",
                    },
                    "gridPos": {"h": 8, "w": 24, "x": 0, "y": 0},
                    "targets": [
                        {
                            "refId": "A",
                            "query": "state.keyword:(${state:lucene})",
                            "timeField": "@timestamp",
                            "metrics": [
                                {"id": "1", "type": "raw_document", "settings": {"size": 20}}
                            ],
                            "bucketAggs": [],
                        }
                    ],
                }
            ],
            "time": {"from": "now-6h", "to": "now"},
        }

        invalid = DashboardVerificationResult(
            valid=False,
            errors=[
                "Panel 'Broken table' target 'A' is missing target datasource.",
                "Panel 'Broken table' target 'A' uses unsupported lucene variable formatting.",
                "Panel 'Broken table' target 'A' uses raw_document instead of raw_data.",
            ],
            warnings=[],
            checked_panels=1,
        )
        valid = DashboardVerificationResult(
            valid=True,
            errors=[],
            warnings=[],
            checked_panels=1,
            queried_panels=1,
            panels_with_data=1,
        )

        with patch(
            "tools.dashboard_builder._post_grafana_dashboard",
            return_value={"status": "success", "uid": "repair-me", "version": 1},
        ) as post, patch(
            "tools.dashboard_builder._verify_dashboard_saved_and_working",
            side_effect=[invalid, valid],
        ):
            _, repaired, verification = _save_dashboard_with_verification(
                dashboard,
                verify=True,
                max_repair_attempts=2,
            )

        self.assertTrue(verification.valid)
        self.assertEqual(post.call_count, 2)
        second_dashboard = post.call_args_list[1].args[0]
        target = second_dashboard["panels"][0]["targets"][0]
        self.assertEqual(target["datasource"]["uid"], "opensearch-default")
        self.assertEqual(target["query"], "state.keyword:${state:regex}")
        self.assertEqual(target["metrics"][0]["type"], "raw_data")
        self.assertEqual(target["metrics"][0]["settings"]["size"], "20")
        self.assertEqual(repaired, second_dashboard)


if __name__ == "__main__":
    unittest.main()
