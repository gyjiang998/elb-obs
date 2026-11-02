from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.core import PlatformError
from backend.dpvs_analysis import (
    DPVSAnalysisManager, compare_runs, create_dpvs_router, dispatch_dpvs_analysis,
    parse_flamegraph_svg, parse_folded, parse_numa_maps, parse_numastat,
    parse_perf_report, parse_sar, render_html,
)


def artifact(kind, content, name=None, **extra):
    return {"kind": kind, "content": content, "name": name or kind + ".txt", **extra}


def perf_report(share=20, count=200, total=1000):
    return f"""# Samples: {total} of event 'cycles'
# Overhead       Samples  Command  Shared Object  Symbol
  {share:.2f}%           {count}  cvs      cvs      [.] f
"""


SAR_TEXT = """Linux 6.1 (host) 10/31/2026 _x86_64_ (8 CPU)
12:00:00 AM CPU %user %nice %system %iowait %steal %idle
12:00:01 AM all 10 0 20 0 0 70
12:00:01 AM 0 20 0 30 0 0 50
12:00:02 AM all 20 0 20 0 0 60
Average: all 15 0 20 0 0 65
"""
NUMASTAT = """Per-node process memory usage (in MBs) for PID 123 (qemu)
                           Node 0          Node 1           Total
                  --------------- --------------- ---------------
Huge                       512.00          512.00         1024.00
Heap                         1.00            0.00            1.00
Total                      513.00          512.00         1025.00
"""
SVG = """<?xml version="1.0" standalone="no"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<svg xmlns="http://www.w3.org/2000/svg"><script>alert('no')</script>
<g id="frames"><g><title>all (1,000 samples, 100%)</title><rect x="0" y="100" width="1000" height="15"/></g>
<g><title>tcp_send_csum (200 samples, 20.00%)</title><rect x="0" y="80" width="200" height="15"/></g>
</g></svg>"""


class ParserTests(unittest.TestCase):
    def test_folded_recursion_and_self_inclusive(self):
        p = parse_folded("root;f;f 20\nroot;g 80\n", "cycles")
        rows = {r["name"]: r for r in p["functions"]}
        self.assertEqual(p["total_weight"], 100)
        self.assertEqual(rows["f"]["self_weight"], 20)
        self.assertEqual(rows["f"]["inclusive_weight"], 20)
        self.assertEqual(rows["root"]["self_weight"], 0)
        self.assertEqual(rows["root"]["inclusive_percent"], 100)

    def test_invalid_folded_is_not_successful_empty_import(self):
        with self.assertRaises(PlatformError):
            parse_folded("not a profile")
        p = parse_folded("root;f 1\nbroken record")
        self.assertEqual(p["coverage"], "partial_stacks")

    def test_report_preserves_raw_samples_and_reported_percent_separately(self):
        p = parse_perf_report(perf_report(20, 100, 1000))
        self.assertEqual(p["event"], "cycles")
        self.assertEqual(p["weight_unit"], "samples")
        self.assertEqual(p["functions"][0]["self_percent"], 20)
        self.assertEqual(p["functions"][0]["self_weight"], 100)
        self.assertIsNone(p["functions"][0]["inclusive_weight"])

    def test_report_children_and_approx_event_count(self):
        p = parse_perf_report("""# Samples: 1K of event 'cycles'
# Event count (approx.): 1000000
# Children Self Command Shared Object Symbol
  50.00% 20.00% cvs cvs [.] tcp_send_csum
       |---20.00%-- child
""")
        f = p["functions"][0]
        self.assertEqual(f["self_weight"], 200000)
        self.assertEqual(f["inclusive_weight"], 500000)
        self.assertEqual(p["weight_unit"], "estimated_event_count")
        self.assertEqual(len(p["functions"]), 1)
        with self.assertRaises(PlatformError):
            parse_perf_report(perf_report(), "cache-misses")

    def test_report_multiple_events_rejected(self):
        with self.assertRaises(PlatformError):
            parse_perf_report(perf_report() + perf_report().replace("'cycles'", "'cache-misses'"))

    def test_svg_standard_doctype_accepted_but_no_self_inferred(self):
        p = parse_flamegraph_svg(SVG, "cycles")
        self.assertEqual(p["total_weight"], 1000)
        self.assertEqual(p["functions"][0]["inclusive_percent"], 20)
        self.assertIsNone(p["functions"][0]["self_percent"])
        self.assertNotIn("script", json.dumps(p))

    def test_svg_entities_and_ambiguous_root_rejected(self):
        for content in ('<!DOCTYPE svg [<!ENTITY x "evil">]><svg/>', '<svg><g><title>f (10 samples, 100%)</title><rect/></g></svg>'):
            with self.assertRaises(PlatformError):
                parse_flamegraph_svg(content)

    def test_sar_averages_not_double_counted_and_cpu_rows_stay_separate(self):
        p = parse_sar(SAR_TEXT)
        self.assertEqual(len(p["records"]), 3)
        self.assertEqual(len(p["averages"]), 1)
        rows = {(r["entity"], r["metric"]): r for r in p["summary"]}
        self.assertEqual(rows["CPU=all", "%idle"]["mean"], 65)
        self.assertEqual(rows["CPU=all", "%idle"]["samples"], 2)
        self.assertEqual(rows["CPU=0", "%idle"]["mean"], 50)

    def test_sadf_semicolon_csv_weighted_intervals(self):
        p = parse_sar("""# hostname;interval;timestamp;CPU;%user;%idle
h;1;2026-10-31 12:00:01 UTC;all;10;90
h;3;2026-10-31 12:00:04 UTC;all;30;70
""")
        idle = next(r for r in p["summary"] if r["metric"] == "%idle")
        self.assertEqual(idle["mean"], 75)
        self.assertEqual(idle["aggregation"], "interval_weighted_mean")

    def test_sadf_comma_csv(self):
        p = parse_sar("hostname,interval,timestamp,CPU,%idle\nh,1,2026-10-31T01:00:00,all,90\n")
        self.assertEqual(p["summary"][0]["mean"], 90)

    def test_sar_restart_and_unweighted_variable_intervals_are_not_comparable(self):
        restarted = parse_sar("""12:00:00 CPU %idle
12:00:01 all 90
12:00:02 LINUX RESTART (8 CPU)
12:00:03 CPU %idle
12:00:04 all 10
""")
        self.assertFalse(restarted["comparison_allowed"])
        self.assertEqual(restarted["boot_segments"], 2)
        self.assertIn("sar_restart_boundary", restarted["comparison_blockers"])
        irregular = parse_sar("""12:00:00 CPU %idle
12:00:01 all 90
12:00:02 all 80
12:00:12 all 70
""")
        self.assertFalse(irregular["comparison_allowed"])
        self.assertEqual(irregular["timing"], "variable_interval_unweighted")
        self.assertIn("variable_interval_unweighted", irregular["comparison_blockers"])

    def test_numastat_is_residency_not_remote_fraction(self):
        p = parse_numastat(NUMASTAT)
        self.assertEqual(p["unit"], "MiB")
        huge = next(row for row in p["rows"] if row["name"] == "Huge")
        self.assertEqual(huge["nodes"], {"0": 512, "1": 512})
        self.assertNotIn("remote_fraction", p)

    def test_global_numastat_retains_allocation_counter_unit(self):
        p = parse_numastat("node0 node1\nnuma_hit 100 200\nnuma_miss 0 4\n")
        self.assertEqual(p["unit"], "allocation_counter")

    def test_numa_maps_mixed_page_sizes_and_unknown_sizes(self):
        p = parse_numa_maps("""7f000 interleave:0-1 file=/huge N0=2 N1=1 kernelpagesize_kB=2048
7f100 default anon=4 N1=4 kernelpagesize_kB=4
7f200 bind:0 anon=1 N0=1
""")
        self.assertEqual(p["node_bytes_known"]["0"], 4 * 1024 * 1024)
        self.assertEqual(p["node_bytes_known"]["1"], 2 * 1024 * 1024 + 16384)
        self.assertFalse(p["complete_page_sizes"])


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = DPVSAnalysisManager(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def run_profile(self, *, content="root;f 20\nroot;g 80", kind="perf_folded", **kwargs):
        payload = {"name": "run", "event": "cycles", "scope": "node", "sampling_hz": 100,
                   "sampling_config": "freq=100", "sampling_target": "cvs-worker-lcores=1-3",
                   "sar_window_signature": "steady-state-60s",
                   "duration_seconds": 10, "artifacts": [artifact(kind, content)]}
        payload.update(kwargs)
        return self.manager.import_run(payload)

    def test_share_halves_but_sample_rate_does_not(self):
        a = self.run_profile(kind="perf_report", content=perf_report(20, 200, 1000))
        b = self.run_profile(kind="perf_report", content=perf_report(10, 800, 8000), duration_seconds=40, node_count=16)
        report = self.manager.compare({"baseline_id": a["id"], "candidate_id": b["id"]})
        row = report["comparison"]["functions"][0]
        self.assertEqual(row["share_delta_pp"], -10)
        self.assertEqual(row["relative_share_change_percent"], -50)
        self.assertEqual(row["weight_delta"], 600)
        self.assertEqual(row["baseline"]["weight_per_second"], 20)
        self.assertEqual(row["candidate"]["weight_per_second"], 20)
        self.assertEqual(row["rate_delta"], 0)
        self.assertIsNone(row["per_packet_delta"])
        self.assertFalse(report["coverage"]["candidate"]["packet_denominator"])
        self.assertEqual(report["analysis_id"], report["id"])

    def test_missing_event_blocks_performance_differences(self):
        a = self.run_profile(event=None)
        b = self.run_profile(event=None)
        report = compare_runs(a, b)
        self.assertFalse(report["comparison"]["comparable"])
        self.assertIsNone(report["comparison"]["functions"][0]["share_delta_pp"])

    def test_cluster_scaling_efficiency_and_missing_zero_throughput(self):
        a = self.run_profile(node_count=1, workload={"cps": 100000, "pps": 200000})
        b = self.run_profile(node_count=16, workload={"cps": 700000, "pps": 1400000})
        result = compare_runs(a, b)["scaling_analysis"]
        self.assertEqual(result["node_ratio"], 16)
        self.assertEqual(result["input_scope"], "cluster_total_required")
        self.assertFalse(result["scope_verified"])
        for metric in ("pps", "cps"):
            self.assertEqual(result["metrics"][metric]["speedup"], 7)
            self.assertEqual(result["metrics"][metric]["efficiency"], 0.4375)
            self.assertEqual(result["metrics"][metric]["efficiency_percent"], 43.75)
        for workload in ({}, {"cps": 0}):
            missing = self.run_profile(node_count=16, workload=workload)
            for left, right in ((a, missing), (missing, a)):
                row = compare_runs(left, right)["scaling_analysis"]["metrics"]["cps"]
                self.assertIsNone(row["speedup"])
                self.assertIsNone(row["efficiency"])
                self.assertEqual(row["reason"], "missing_or_zero_throughput")

    def test_different_scope_or_event_is_not_comparable(self):
        a = self.run_profile()
        for changes in ({"event": "cache-misses"}, {"scope": "cluster", "node_count": 16},
                        {"sampling_target": "system-wide"}):
            with self.subTest(changes=changes):
                b = self.run_profile(**changes)
                self.assertFalse(compare_runs(a, b)["comparison"]["comparable"])

    def test_sampling_config_and_target_gate_absolute_sample_rate(self):
        a = self.run_profile()
        for changes in ({"sampling_config": "period=100000", "sampling_hz": 100}, {"sampling_target": None}):
            with self.subTest(changes=changes):
                b = self.run_profile(**changes)
                result = compare_runs(a, b)["comparison"]
                self.assertFalse(result["absolute_rate_comparable"])
                self.assertIsNone(result["functions"][0]["rate_delta"])
        normalized = self.run_profile(sampling_config=" FREQ = 100 ", sampling_target=" CVS-WORKER-LCORES = 1-3 ")
        self.assertTrue(compare_runs(a, normalized)["comparison"]["absolute_rate_comparable"])

    def test_missing_duration_keeps_counts_but_no_rate(self):
        a = self.run_profile(duration_seconds=None)
        b = self.run_profile()
        report = compare_runs(a, b)
        self.assertFalse(report["comparison"]["absolute_rate_comparable"])
        self.assertIsNone(report["comparison"]["functions"][0]["baseline"]["weight_per_second"])

    def test_missing_function_in_report_not_zero(self):
        a = self.run_profile(kind="perf_report", content=perf_report())
        b = self.run_profile(kind="perf_report", content=perf_report().replace("[.] f", "[.] g"))
        rows = compare_runs(a, b)["comparison"]["functions"]
        f = next(row for row in rows if row["name"] == "f")
        self.assertIsNone(f["candidate"]["percent"])
        self.assertIsNone(f["share_delta_pp"])

    def test_svg_requires_explicit_inclusive(self):
        a = self.run_profile(kind="flamegraph_svg", content=SVG)
        b = self.run_profile(kind="flamegraph_svg", content=SVG)
        self.assertFalse(compare_runs(a, b)["comparison"]["comparable"])
        self.assertTrue(compare_runs(a, b, metric="inclusive")["comparison"]["comparable"])

    def test_report_contract_sar_missing_side_and_numa_one_side(self):
        a = self.run_profile(artifacts=[artifact("perf_report", perf_report()),
                                       artifact("sar", "12:00:00 CPU %idle %system\nAverage: all 20 10\n", role="guest"),
                                       artifact("numastat", NUMASTAT, role="host")])
        b = self.run_profile(artifacts=[artifact("perf_report", perf_report(10, 800, 8000)),
                                       artifact("sar", "12:00:00 CPU %idle %sys\n12:00:01 all 30 20\n12:00:01 0 40 10\n", role="guest")],
                             duration_seconds=40, node_count=16)
        report = self.manager.compare({"baseline_id": a["id"], "candidate_id": b["id"]})
        required = {"analysis_id", "manifest", "coverage", "perf_comparison", "sar_comparison", "host_numa_analysis", "findings", "recommendations", "limitations", "provenance"}
        self.assertTrue(required <= report.keys())
        idle = next(row for row in report["sar_comparison"]["metrics"] if row["metric"] == "%idle" and row["entity"] == "CPU=all")
        self.assertEqual(idle["delta"], 10)
        self.assertEqual(idle["delta_unit"], "percentage_points")
        self.assertEqual(idle["baseline_sources"][0]["coverage"], "average_only")
        system = next(row for row in report["sar_comparison"]["metrics"] if row["metric"] == "%system")
        self.assertIsNone(system["candidate"])
        self.assertIsNone(system["delta"])
        self.assertFalse(report["host_numa_analysis"]["paired_evidence"])
        self.assertIsNone(report["host_numa_analysis"]["remote_access_fraction"])
        for source in report["provenance"]:
            self.assertEqual(len(source["sha256"]), 64)
            self.assertTrue({"role", "scenario", "parser", "confidence"} <= source.keys())

    def test_same_sar_entity_multiple_files_not_silently_averaged(self):
        a = self.run_profile(artifacts=[artifact("sar", SAR_TEXT, "a.txt"), artifact("sar", SAR_TEXT, "b.txt")])
        b = self.run_profile(artifacts=[artifact("sar", SAR_TEXT)])
        rows = compare_runs(a, b)["sar_comparison"]["metrics"]
        self.assertTrue(all(row["baseline"] is None and row["reason"] == "multiple_sources_not_merged" for row in rows))

    def test_sar_window_signature_gates_mean_delta(self):
        sar = "12:00:00 CPU %idle\nAverage: all 50\n"
        a = self.run_profile(artifacts=[artifact("sar", sar)])
        for signature in (None, "different-window"):
            with self.subTest(signature=signature):
                b = self.run_profile(artifacts=[artifact("sar", sar)], sar_window_signature=signature)
                row = compare_runs(a, b)["sar_comparison"]["metrics"][0]
                self.assertEqual(row["reason"], "windows_not_aligned")
                self.assertEqual(row["baseline"], 50)
                self.assertEqual(row["candidate"], 50)
                self.assertIsNone(row["delta"])

    def test_restart_sar_is_retained_but_not_compared(self):
        text = """12:00:00 CPU %idle
12:00:01 all 90
12:00:02 LINUX RESTART (8 CPU)
12:00:03 CPU %idle
12:00:04 all 10
"""
        a = self.run_profile(artifacts=[artifact("sar", text)])
        b = self.run_profile(artifacts=[artifact("sar", "12:00:00 CPU %idle\n12:00:01 all 50\n")])
        row = compare_runs(a, b)["sar_comparison"]["metrics"][0]
        self.assertEqual(row["reason"], "sar_source_not_comparable")
        self.assertIsNone(row["baseline"])
        self.assertIsNone(row["candidate"])
        self.assertIn("sar_restart_boundary", row["comparison_blockers"])

    def test_raw_input_preserved_and_user_name_not_used_as_path(self):
        a = self.run_profile(artifacts=[artifact("perf_folded", "root;f 20", "../../escape.txt")])
        self.assertEqual((self.manager.runs_root / a["id"] / "source-00.txt").read_text(), "root;f 20")
        self.assertFalse((self.root.parent / "escape.txt").exists())
        with self.assertRaises(PlatformError):
            self.manager.get_run("../../anything")

    def test_multiple_profiles_require_selection(self):
        a = self.run_profile(artifacts=[artifact("perf_folded", "root;f 10", "a"), artifact("perf_folded", "root;f 30", "b")])
        b = self.run_profile()
        with self.assertRaises(PlatformError):
            compare_runs(a, b)
        result = compare_runs(a, b, baseline_profile="a")
        self.assertEqual(result["comparison"]["baseline_profile"]["total_weight"], 10)

    def test_html_escapes_symbols_and_svg_never_embedded(self):
        a = self.run_profile(content="root;<img src=x onerror=alert(1)> 20", name="<script>bad</script>")
        report = compare_runs(a, a)
        output = render_html(report)
        self.assertNotIn("<script>", output)
        self.assertNotIn("<img ", output)
        self.assertIn("&lt;img", output)
        self.assertIn("Content-Security-Policy", output)

    def test_nonfinite_measurements_rejected(self):
        for value in (float("nan"), float("inf"), -1, True):
            with self.subTest(value=value), self.assertRaises(PlatformError):
                self.run_profile(duration_seconds=value)

    def test_desktop_and_http_use_same_contract(self):
        request = {"method": "POST", "path": "/v1/dpvs-analysis/runs", "body": {
            "name": "desktop", "event": "cycles", "artifacts": [artifact("perf_folded", "a;b 10")]}}
        run = dispatch_dpvs_analysis(request, self.root)
        self.assertIsNone(dispatch_dpvs_analysis({"path": "/v1/unrelated"}, self.root))
        report = dispatch_dpvs_analysis({"method": "POST", "path": "/v1/dpvs-analysis/compare", "body": {
            "baseline_id": run["id"], "candidate_id": run["id"]}}, self.root)
        app = FastAPI()

        @app.exception_handler(PlatformError)
        async def error_handler(_, exc):
            return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message}})

        app.include_router(create_dpvs_router(self.root))
        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/dpvs-analysis/runs/" + run["id"]).json(), run)
            for fmt in ("json", "markdown", "html"):
                path = f"/v1/dpvs-analysis/reports/{report['id']}/export?format={fmt}"
                exported = dispatch_dpvs_analysis({"path": path}, self.root)
                self.assertEqual(exported["__desktop_response_kind__"], "text")
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, exported["content"])
            self.assertEqual(client.post("/v1/dpvs-analysis/runs", json={"artifacts": []}).status_code, 400)
            self.assertEqual(client.get("/v1/dpvs-analysis/runs/run_0000000000000000").status_code, 404)
        with patch("backend.dpvs_analysis.DESKTOP_MAX_REQUEST_BYTES", 20), self.assertRaises(PlatformError):
            dispatch_dpvs_analysis(request, self.root)


if __name__ == "__main__":
    unittest.main()
