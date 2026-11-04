"""M10 statistics dataset, ChartSpec, cache, and isolation tests."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from database.charts import ChartNotFoundError, ChartSpecError, build_chart
from database.db import add_task, complete_task, query_statistics, set_user_timezone
from database.migrations import apply_migrations
from harness.tool_protocol import ToolCallContext
from harness.tool_registry import ToolDispatcher
from harness.todo_tools import build_todo_tool_registry


class ConnectionProxy:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def close(self) -> None:
        pass


class M10ChartTests(unittest.TestCase):
    @contextmanager
    def services(self):
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        apply_migrations(connection)
        try:
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                yield connection
        finally:
            connection.close()

    def test_m10_statistics_persists_dataset_and_builds_cached_chart(self) -> None:
        with self.services() as connection:
            set_user_timezone("alice", "Asia/Shanghai")
            task = add_task("alice", "Chart task")
            complete_task(
                "alice",
                task["id"],
                occurred_at="2025-01-01T01:00:00Z",
                idempotency_key="chart-1",
            )
            statistics = query_statistics(
                "alice",
                period="custom",
                range_start="2025-01-01",
                range_end="2025-01-03",
                status="completed",
            )
            self.assertTrue(statistics["dataset_ref"].startswith("statistics:"))
            self.assertEqual(len(connection.execute("SELECT * FROM chart_queries").fetchall()), 1)

            chart = build_chart(
                "alice",
                chart_type="line",
                dataset_ref=statistics["dataset_ref"],
                dimensions=["period"],
                metric="count",
                query_digest=statistics["query_digest"],
            )
            self.assertEqual(chart["spec"]["chart_type"], "line")
            self.assertEqual(chart["spec"]["timezone"], "Asia/Shanghai")
            self.assertEqual(chart["spec"]["data"][0], {"period": "2025-01-01", "count": 1})

            cached = build_chart(
                "alice",
                chart_type="line",
                dataset_ref=statistics["dataset_ref"],
                dimensions=["period"],
                metric="count",
                query_digest=statistics["query_digest"],
            )
            self.assertTrue(cached["cached"])
            self.assertEqual(cached["artifact_id"], chart["artifact_id"])
            self.assertEqual(len(connection.execute("SELECT * FROM chart_artifacts").fetchall()), 1)

    def test_m10_chart_is_user_scoped_and_rejects_untrusted_spec_fields(self) -> None:
        with self.services():
            statistics = query_statistics(
                "alice",
                period="custom",
                range_start="2025-01-01",
                range_end="2025-01-02",
            )
            with self.assertRaises(ChartNotFoundError):
                build_chart(
                    "bob",
                    chart_type="line",
                    dataset_ref=statistics["dataset_ref"],
                    dimensions=["period"],
                    metric="count",
                )
            with self.assertRaises(ChartSpecError):
                build_chart(
                    "alice",
                    chart_type="line",
                    dataset_ref=statistics["dataset_ref"],
                    dimensions=["raw_sql"],
                    metric="count",
                )
            with self.assertRaises(ChartSpecError):
                build_chart(
                    "alice",
                    chart_type="pie",
                    dataset_ref=statistics["dataset_ref"],
                    dimensions=["period", "task_id"],
                    metric="count",
                )

    def test_m10_chart_tool_uses_statistics_dataset_and_digest(self) -> None:
        with self.services():
            dispatcher = ToolDispatcher(build_todo_tool_registry())
            context = ToolCallContext(user_id="alice", request_id="m10-tool")
            query = dispatcher.dispatch(
                "statistics_query",
                {
                    "period": "custom",
                    "range_start": "2025-01-01",
                    "range_end": "2025-01-02",
                },
                context,
            )
            self.assertTrue(query.ok)
            chart = dispatcher.dispatch(
                "chart_build",
                {
                    "chart_type": "table",
                    "dataset_ref": query.data["data"]["dataset_ref"],
                    "query_digest": query.data["data"]["query_digest"],
                    "dimensions": ["period"],
                    "metric": "count",
                },
                context,
            )
            self.assertTrue(chart.ok)
            self.assertEqual(chart.data["data"]["spec"]["chart_type"], "table")


if __name__ == "__main__":
    unittest.main()
