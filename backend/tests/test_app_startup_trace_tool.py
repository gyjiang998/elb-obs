from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.device_analysis import app_startup_trace as tool


class AppStartupTraceToolTests(unittest.TestCase):
    def test_candidate_catalog_has_enough_unique_safe_bundles(self) -> None:
        self.assertGreaterEqual(len(tool.APP_CANDIDATES), 20)
        self.assertEqual(len(tool.APP_CANDIDATES), len(set(tool.APP_CANDIDATES)))
        for bundle in tool.APP_CANDIDATES:
            self.assertEqual(tool.validate_bundle(bundle), bundle)

    def test_default_arkui_profile_avoids_scheduler_volume(self) -> None:
        self.assertIn("ace", tool.TRACE_PROFILES["arkui-startup"])
        self.assertIn("ark", tool.TRACE_PROFILES["arkui-startup"])
        self.assertNotIn("sched", tool.TRACE_PROFILES["arkui-startup"])
        self.assertIn("sched", tool.TRACE_PROFILES["broad"])

    def test_parse_hdc_targets_ignores_transport_warning(self) -> None:
        output = (
            "026MLM25CK129272\n"
            "\x1b[1;33m[W]\x1b[0m FreeChannelContinue handle->data is nullptr\n"
        )

        self.assertEqual(tool.parse_hdc_targets(output), ["026MLM25CK129272"])

    def test_parse_installed_bundles_only_accepts_bundle_identifiers(self) -> None:
        output = "ID: 100:\n\tcom.example.alpha\nrandom message\n\tcom.example.beta\n"

        self.assertEqual(
            tool.parse_installed_bundles(output),
            {"com.example.alpha", "com.example.beta"},
        )

    def test_launch_shell_marks_window_without_clearing_app_data(self) -> None:
        command = tool.build_launch_shell(
            "com.example.alpha",
            attempt_index=3,
            dwell_seconds=1.5,
            marker_path="/sys/kernel/tracing/trace_marker",
        )

        self.assertIn("aa force-stop com.example.alpha", command)
        self.assertIn("aa start -b com.example.alpha -A action.system.home", command)
        self.assertIn("H:HETERO_APP_LAUNCH::03::com.example.alpha|I1", command)
        self.assertIn('echo "E|$$|I1"', command)
        self.assertNotIn("bm clean", command)
        self.assertNotIn("uninstall", command)

    def test_launch_parser_requires_success_and_extracts_pids(self) -> None:
        success, pids, reason = tool.parse_launch_result(
            "start ability successfully.\nHETERO_PID=313 211\n",
            0,
        )

        self.assertTrue(success)
        self.assertEqual(pids, [211, 313])
        self.assertEqual(reason, "launcher_resolved")

        failed, _, _ = tool.parse_launch_result("failed to start ability\nHETERO_PID=", 0)
        self.assertFalse(failed)

    def test_parse_hiperf_flat_rows(self) -> None:
        rows = tool.parse_hiperf_report(
            " 5.41%  5843967081 swapper 0 swapper swapper@0x0\n"
            " 0.42%  1234 AppThread 4321 /system/lib64/libace.z.so Ace::Render\n"
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["pid"], 4321)
        self.assertEqual(rows[1]["function"], "Ace::Render")
        self.assertEqual(rows[1]["heat_percent"], 0.42)

    def test_launch_marker_regex_preserves_attempt_and_bundle(self) -> None:
        matched = tool.LAUNCH_MARKER_RE.search(
            "H:HETERO_APP_LAUNCH::17::com.huawei.hmos.calculator|I1"
        )

        self.assertIsNotNone(matched)
        assert matched is not None
        self.assertEqual(matched.group(1), "17")
        self.assertEqual(matched.group(2), "com.huawei.hmos.calculator")

    def test_trace_streamer_discovery_prefers_host_binary(self) -> None:
        with TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            binary_root = root / ".local-artifacts/tools/trace_streamer_x/trace_streamer_binary"
            binary_root.mkdir(parents=True)
            for name in ("trace_streamer_linux", "trace_streamer_mac"):
                path = binary_root / name
                path.write_bytes(b"binary")
                path.chmod(0o755)
            with patch.object(tool, "WEB_ROOT", root), patch.object(tool.platform, "system", return_value="Darwin"):
                resolved = tool.resolve_trace_streamer(None)

        self.assertEqual(resolved.name, "trace_streamer_mac")


if __name__ == "__main__":
    unittest.main()
