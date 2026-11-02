from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.device_analysis import stage_trace_report as tool


class StageTraceReportToolTests(unittest.TestCase):
    def test_build_report_uses_real_markers_and_ranks_self_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "trace.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE process(ipid INT, pid INT, name TEXT);
                CREATE TABLE thread(itid INT, tid INT, name TEXT, ipid INT);
                CREATE TABLE callstack(
                    id INT, ts INT, dur INT, callid INT, name TEXT, parent_id INT
                );
                INSERT INTO process VALUES(1, 99, 'photos');
                INSERT INTO thread VALUES(1, 100, 'main', 1);
                INSERT INTO callstack VALUES(1, 1000000, 1, 1, 'START', NULL);
                INSERT INTO callstack VALUES(2, 2000000, 8000000, 1, 'outer', NULL);
                INSERT INTO callstack VALUES(3, 3000000, 6000000, 1, 'child', 2);
                INSERT INTO callstack VALUES(4, 4000000, 3000000, 1, 'kernel', 3);
                INSERT INTO callstack VALUES(5, 11000000, 1, 1, 'END', NULL);
                """
            )
            connection.commit()
            connection.close()

            report = tool.build_report(
                database,
                [tool.StageSpec("edit", "编辑", "START", "END")],
                process_ids=[99],
                top_event_limit=3,
            )

        stage = report["stages"][0]
        self.assertEqual(stage["start_event"]["name"], "START")
        self.assertEqual(stage["end_event"]["name"], "END")
        self.assertEqual(stage["duration_ms"], 10.0)
        self.assertEqual(stage["event_count"], 3)
        self.assertEqual([event["name"] for event in stage["top_events"]], ["child", "kernel", "outer"])
        self.assertEqual(stage["top_events"][0]["metrics"]["self_time_estimate_total_duration_ms"], 3.0)
        self.assertFalse(stage["top_events"][0]["measurement_semantics"]["cpu_self_time_proven"])

    def test_parse_stage_spec_rejects_incomplete_value(self) -> None:
        with self.assertRaises(Exception):
            tool.parse_stage_spec("rotate|旋转|START")


if __name__ == "__main__":
    unittest.main()
