from __future__ import annotations

import os
import unittest

os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")

from agent_graph import PydanticGraphAgent


class AgentGraphDashboardGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(PydanticGraphAgent)

    def test_dashboard_guard_blocks_source_code_without_update(self) -> None:
        response = self.agent._guard_dashboard_response(
            user_message="Build a Grafana dashboard for trades-*",
            response="```python\nprint('dashboard json')\n```",
            tool_calls=[],
        )

        self.assertIn("did not create or update", response)
        self.assertIn("blocked", response)

    def test_dashboard_guard_reports_failed_builder(self) -> None:
        response = self.agent._guard_dashboard_response(
            user_message="Create a dashboard",
            response="Here is code",
            tool_calls=[
                {
                    "tool": "build_dashboard_tool",
                    "parameters": {"code_chars": 100},
                    "result": "Build failed. Fix the code and call build_dashboard_tool again.\n"
                    "Error: bad import",
                }
            ],
        )

        self.assertIn("dashboard build failed", response)
        self.assertIn("bad import", response)

    def test_dashboard_guard_allows_successful_update_summary(self) -> None:
        response = self.agent._guard_dashboard_response(
            user_message="Create a dashboard",
            response="Dashboard saved: trades-overview",
            tool_calls=[
                {
                    "tool": "grafana_update_dashboard",
                    "parameters": {},
                    "result": '{"status": "success"}',
                }
            ],
        )

        self.assertEqual(response, "Dashboard saved: trades-overview")

    def test_dashboard_guard_reports_update_failure(self) -> None:
        response = self.agent._guard_dashboard_response(
            user_message="Create a dashboard",
            response="Dashboard source below",
            tool_calls=[
                {
                    "tool": "build_opensearch_dashboard_tool",
                    "parameters": {},
                    "result": "Dashboard build succeeded: uid=abc, title=ABC, panels=10",
                },
                {
                    "tool": "grafana_update_dashboard",
                    "parameters": {},
                    "result": "Error: 401 unauthorized",
                },
            ],
        )

        self.assertIn("Grafana rejected", response)
        self.assertIn("401 unauthorized", response)

    def test_dashboard_guard_allows_composite_save_success(self) -> None:
        response = self.agent._guard_dashboard_response(
            user_message="Create a dashboard",
            response="Dashboard saved: trades-overview",
            tool_calls=[
                {
                    "tool": "build_and_save_opensearch_dashboard_tool",
                    "parameters": {},
                    "result": "Dashboard save succeeded: uid=abc, title=ABC, panels=10, variables=2",
                }
            ],
        )

        self.assertEqual(response, "Dashboard saved: trades-overview")

    def test_dashboard_guard_allows_focused_chart_save_success(self) -> None:
        response = self.agent._guard_dashboard_response(
            user_message="Create a pie chart dashboard",
            response="Dashboard saved: system-state-pie",
            tool_calls=[
                {
                    "tool": "build_and_save_opensearch_chart_dashboard_tool",
                    "parameters": {},
                    "result": "Dashboard save succeeded: uid=abc, title=ABC, panels=1, variables=0",
                }
            ],
        )

        self.assertEqual(response, "Dashboard saved: system-state-pie")

    def test_dashboard_guard_allows_planned_save_success(self) -> None:
        response = self.agent._guard_dashboard_response(
            user_message="Create the dashboard from the approved plan",
            response="Dashboard saved: planned-overview",
            tool_calls=[
                {
                    "tool": "build_and_save_opensearch_planned_dashboard_tool",
                    "parameters": {},
                    "result": "Dashboard save succeeded: uid=abc, title=ABC, panels=3, variables=1",
                }
            ],
        )

        self.assertEqual(response, "Dashboard saved: planned-overview")


if __name__ == "__main__":
    unittest.main()
