"""Synthetic timestamps test evidence semantics, not device performance."""
import csv
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from backend.parallel_modeling.evidence import Pmu
from backend.parallel_modeling.manager import ParallelModelingManager, HARDWARE_ID
from backend.parallel_modeling.workload_features import WorkloadFeaturesManager, _metric_evidence
from backend.tests.test_parallel_modeling import trace_fixture


SYNC = 1_000_500_000


def line(timestamp, event, body, tid=10):
    return f"app-{tid} ( {tid:4d}) [000] .... {timestamp:.9f}: {event}: {body}\n"


def with_events(trace, extra):
    return "".join(sorted(trace.splitlines(keepends=True) + extra,
                          key=lambda text: float(re.search(r"(\d+\.\d+):", text)[1])))


def pmu_fixture(*, multiplex=True, coarse=False, no_base=False, missing_cycle=False, all_groups=False):
    names = ["cycles", "instructions", "generic_cache_references", "generic_cache_misses"]
    groups = [{"id": "base", "event_names": [] if no_base else ["cycles", "instructions"],
               "requested_event_names": ["cycles", "instructions"], "status": "unavailable" if no_base else "active",
               "unavailable_events": [{"name": "cycles", "errno": 22, "stage": "open"}] if no_base else []},
              {"id": "cache", "event_names": names[1:] if missing_cycle else names, "status": "partial" if missing_cycle else "active"}]
    if all_groups:
        for identity, events in (("branch", ["branch_instructions", "branch_misses"]), ("stall", ["stalled_frontend", "stalled_backend"]),
                                 ("l1d", ["l1d_read_access", "l1d_read_misses"]), ("llc", ["llc_read_access", "llc_read_misses"]),
                                 ("dtlb", ["dtlb_read_access", "dtlb_read_misses"])):
            groups.append({"id": identity, "event_names": ["cycles", "instructions", *events], "status": "active"})
    metadata = {"schema_version": "thread_pmu_capture/v2", "format": "grouped_long_csv", "group": "auto",
                "groups": groups, "target_tid": 10, "scope": "target_thread_user_only_no_inherit",
                "sync_begin_ns": SYNC, "sync_end_ns": SYNC + 1000}
    columns = ["sample_index", "group_id", "read_begin_ns", "read_end_ns", "event_count", "enabled_ns", "running_ns",
               "event0", "event1", "event2", "event3", "read_error", "reader_cpu"]
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=columns); writer.writeheader()
    timestamps = [1_000_000_000, 1_030_926_656] if coarse else [1_001_000_000 + n * 500_000 for n in range(10)]
    for tick, ns in enumerate(timestamps):
        for group in groups:
            if not group["event_names"]: continue
            is_base = group["id"] == "base"
            increments = {"cycles": 500 if is_base else 100, "instructions": 1000 if is_base else 200,
                          "generic_cache_references": 40, "generic_cache_misses": 20}
            if coarse:
                increments.update(instructions=50_391_782, cycles=25_195_891)
            counters = [tick * increments.get(name, 20) for name in group["event_names"]]
            writer.writerow({"sample_index": tick, "group_id": group["id"], "read_begin_ns": ns,
                "read_end_ns": ns + 1000, "event_count": len(counters), "enabled_ns": tick * 100_000,
                "running_ns": tick * (25_000 if multiplex else 100_000),
                **{f"event{i}": counters[i] if i < len(counters) else "" for i in range(4)}, "read_error": 0, "reader_cpu": 0})
    return metadata, output.getvalue()


def import_fixture(manager, *, coarse=False, exact=False, old_scheduler=False):
    trace = with_events(trace_fixture(), [line(SYNC / 1e9, "tracing_mark_write", f"PMU_CLOCK_SYNC|{SYNC}"),
        line(1.0021, "irq_handler_entry", "irq=30 name=timer"),
        line(1.0022, "irq_handler_exit", "irq=30 ret=handled"),
        line(1.0023, "sched_switch", "prev_pid=10 prev_state=R+ ==> next_pid=20"),
        line(1.0025, "sched_switch", "prev_pid=20 prev_state=S ==> next_pid=10"),
        line(1.0030, "sched_switch", "prev_pid=10 prev_state=R ==> next_pid=20"),
        line(1.0032, "sched_switch", "prev_pid=20 prev_state=S ==> next_pid=10")])
    if coarse: trace = trace.replace("1.004000:", "1.002176:")
    meta, data = pmu_fixture(coarse=coarse, multiplex=not coarse, all_groups=True)
    dataset = manager.import_dataset({"name": "边界包络" if coarse else "多组复用", "trace_text": trace,
        "pmu_text": data, "pmu_metadata": meta, "hardware_profile_id": HARDWARE_ID})
    if old_scheduler:
        path = manager.state_root / "inputs" / dataset["id"] / "parsed.json"
        parsed = json.loads(path.read_text()); parsed["frame"]["evidence"].pop("scheduler_evidence_version", None)
        path.write_text(json.dumps(parsed))
    features = WorkloadFeaturesManager(manager)
    profile = features.analyze({"dataset_id": dataset["id"]})
    if exact:
        stage = profile["stages"][0]
        profile = features.analyze({"dataset_id": dataset["id"], "measurements": {
            "schema_version": "workload_stage_measurements/v1", "dataset_id": dataset["id"],
            "hardware_profile_id": HARDWARE_ID, "clock_domain": "trace_monotonic", "clock_alignment_evidence": "synthetic aligned stage",
            "provenance": {"source": "synthetic unit fixture", "run_id": "fixture", "collector": "test"},
            "stages": [{"stage_id": stage["id"], "start_ns": stage["start_ns"], "end_ns": stage["end_ns"], "tid": 10,
                "measurement_scope": "thread_user", "counters": {name: {"value": value, "unit": "count", "event": name}
                    for name, value in {"instructions": 2000, "cycles": 1000, "llc_misses": 2}.items()}}]}})
    return dataset, profile


class PmuEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.directory = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def load(self, **options):
        meta, data = pmu_fixture(**options)
        (self.directory / "pmu.json").write_text(json.dumps(meta)); (self.directory / "pmu.csv").write_text(data)
        return Pmu(self.directory, [{"event": "tracing_mark_write", "body": f"PMU_CLOCK_SYNC|{SYNC}", "ns": SYNC}])

    def test_group_mux_weighted_estimates_and_ratios_use_same_group(self):
        pmu = self.load(); stage = pmu.stage(1_002_000_000, 1_004_000_000)
        metrics = _metric_evidence(stage, None)
        self.assertFalse(stage["bounds_valid"])
        self.assertIsNone(metrics["instructions"]["bounds"])
        self.assertEqual(metrics["instructions"]["status"], "estimated")
        self.assertEqual(metrics["instructions"]["estimate_scope"], "covered_stage_window")
        self.assertEqual(metrics["instructions"]["estimate_method"], "wall_time_overlap")
        self.assertAlmostEqual(metrics["instructions"]["estimate"], 16000)
        self.assertEqual(metrics["instructions"]["interior_estimate"], 12000)
        self.assertEqual(metrics["cache_mpki"]["estimate"], 100)
        self.assertEqual(metrics["cache_mpki"]["group_id"], "cache")
        self.assertEqual(metrics["cache_mpki"]["runtime_coverage"], .25)
        self.assertEqual(metrics["instructions"]["time_coverage"], 1)
        self.assertLess(metrics["instructions"]["interior_time_coverage"], 1)
        self.assertIsNotNone(metrics["instructions"]["observed_counter_bounds"])
        self.assertNotEqual(metrics["instructions"]["estimate"], metrics["instructions"]["observed_counter_bounds"][0])
        self.assertFalse(stage["feature_usable_under_25pct_instruction_boundary_rule"])

    def test_short_stage_apportions_sample_delta_and_preserves_original_bounds(self):
        stage = self.load(coarse=True, multiplex=False).stage(1_002_000_000, 1_002_176_000)
        metric = _metric_evidence(stage, None)["instructions"]
        self.assertEqual(metric["bounds"], [0, 50_391_782])
        self.assertIsNone(metric["exact"])
        self.assertAlmostEqual(metric["estimate"], 50_391_782 * 176_000 / 30_926_656)
        self.assertEqual(metric["status"], "estimated")
        self.assertEqual(metric["time_coverage"], 1)
        self.assertEqual(metric["interior_time_coverage"], 0)
        self.assertEqual(metric["sample_window_convention"], "adjacent_read_bracket_midpoints")
        self.assertEqual(metric["max_boundary_span_us"], 30927.656)
        self.assertIn("short_stage_no_interior_samples", metric["reason_codes"])

    def test_adjacent_stages_conserve_scaled_sample_deltas_without_overlapping_brackets(self):
        pmu = self.load()
        # The split is inside a read bracket, where outer-bracket weighting would
        # double-count time. Midpoint bins form one non-overlapping partition.
        start, split, end = 1_001_000_500, 1_002_000_200, 1_005_500_500
        whole = pmu.stage(start, end)["time_weighted_estimates"]["instructions"]
        left = pmu.stage(start, split)["time_weighted_estimates"]["instructions"]
        right = pmu.stage(split, end)["time_weighted_estimates"]["instructions"]
        self.assertAlmostEqual(whole, 36000)
        self.assertAlmostEqual(left + right, whole)

    def test_partial_coverage_does_not_extrapolate_and_invalid_bins_leave_gaps(self):
        pmu = self.load()
        start, end = 1_000_000_500, 1_002_000_500
        stage = pmu.stage(start, end)
        self.assertAlmostEqual(stage["weighted_time_coverage"], .5)
        self.assertAlmostEqual(stage["time_weighted_estimates"]["instructions"], 8000)
        self.assertIn("weighted_estimate_partial_stage_coverage", stage["reason_codes"])
        pmu.groups["base"]["bins"][1]["valid"] = False
        stage = pmu.stage(start, end)
        self.assertAlmostEqual(stage["weighted_time_coverage"], .25)
        self.assertAlmostEqual(stage["time_weighted_estimates"]["instructions"], 4000)
        self.assertEqual(_metric_evidence(stage, None)["ipc"]["estimate"], 2)

    def test_each_bin_is_scaled_before_overlap_weighting(self):
        pmu = self.load()
        entries = pmu.groups["cache"]["bins"]
        entries[0]["running_ns"] = 100000
        entries[1]["running_ns"] = 50000
        entries[1]["values"]["generic_cache_misses"] = 60
        # One full first bin plus half of the second, with different multiplex
        # ratios and miss rates; averaging coverage first is not equivalent.
        stage = pmu.stage(1_001_000_500, 1_001_750_500)
        metric = _metric_evidence(stage, None)["cache_mpki"]
        self.assertAlmostEqual(metric["estimate"], 1000 * (20 + 60 * 2 * .5) / (200 + 200 * 2 * .5))
        self.assertAlmostEqual(metric["runtime_coverage"], 125000 / 150000)
        self.assertEqual(metric["group_id"], "cache")

    def test_partial_group_and_unavailable_base_preserve_other_metrics(self):
        stage = self.load(no_base=True, missing_cycle=True).stage(1_002_000_000, 1_004_000_000)
        metric = _metric_evidence(stage, None)
        self.assertEqual(stage["group_id"], "cache")
        self.assertEqual(metric["instructions"]["status"], "estimated")
        self.assertEqual(metric["cache_mpki"]["estimate"], 100)
        self.assertEqual(metric["cycles"]["status"], "unavailable")
        self.assertIsNone(metric["ipc"]["estimate"])

    def test_all_specialty_groups_expose_metrics_without_claiming_llc_mapping(self):
        stage = self.load(all_groups=True).stage(1_002_000_000, 1_004_000_000)
        metrics = _metric_evidence(stage, None)
        for key in ("llc_mpki", "l1d_mpki", "dtlb_mpki", "branch_mpki"):
            self.assertEqual(metrics[key]["estimate"], 100)
            self.assertEqual(metrics[key]["status"], "estimated")
        self.assertFalse(metrics["llc_mpki"]["hardware_mapping_verified"])
        self.assertIn("hardware_llc_mapping_unverified", metrics["llc_mpki"]["reason_codes"])
        self.assertIsNotNone(metrics["stalled_frontend"]["estimate"])
        self.assertEqual(metrics["stalled_frontend"]["group_id"], "stall")

    def test_zero_runtime_base_cannot_override_an_active_cache_group_with_exact_zero(self):
        pmu = self.load(multiplex=False)
        path = self.directory / "pmu.csv"
        rows = list(csv.DictReader(io.StringIO(path.read_text())))
        for row in rows:
            if row["group_id"] == "base":
                for name in ("enabled_ns", "running_ns", "event0", "event1"): row[name] = 0
        output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
        path.write_text(output.getvalue())
        pmu = Pmu(self.directory, [{"event": "tracing_mark_write", "body": f"PMU_CLOCK_SYNC|{SYNC}", "ns": SYNC}])
        stage = pmu.stage(1_002_000_000, 1_004_000_000)
        metrics = _metric_evidence(stage, None)
        self.assertFalse(stage["groups"]["base"]["bounds_valid"])
        self.assertIn("no_counter_runtime_in_stage", stage["groups"]["base"]["reason_codes"])
        self.assertEqual(stage["group_id"], "cache")
        self.assertEqual(metrics["instructions"]["group_id"], "cache")
        self.assertEqual(metrics["instructions"]["bounds"], [600, 1000])
        self.assertIsNone(metrics["instructions"]["exact"])

    def test_zero_counts_with_positive_runtime_remain_observed_zero(self):
        pmu = self.load(multiplex=False)
        for entry in pmu.groups["base"]["bins"]:
            entry["values"] = {"cycles": 0, "instructions": 0}
        stage = pmu.stage(1_002_000_000, 1_004_000_000)
        self.assertTrue(stage["bounds_valid"])
        self.assertEqual(_metric_evidence(stage, None)["instructions"]["exact"], 0)

    def test_no_clock_marker_never_yields_estimated_or_bounded_stage(self):
        pmu = self.load(); pmu.clock_verified = False
        stage = pmu.stage(1_002_000_000, 1_004_000_000)
        self.assertIsNone(stage["interior_estimates"])
        self.assertIsNone(stage["time_weighted_estimates"])
        self.assertIsNone(stage["observed_counter_lower"])
        self.assertIn("clock_alignment_unverified", stage["reason_codes"])

    def test_invalid_interval_cannot_become_scaled_bound(self):
        pmu = self.load(); pmu.groups["base"]["bins"][3]["valid"] = False
        stage = pmu.stage(1_002_000_000, 1_004_000_000)
        self.assertIsNone(stage["lower"])
        self.assertIn("invalid_counter_intervals", stage["reason_codes"])
        self.assertIsNotNone(stage["interior_estimates"])
        self.assertLess(stage["estimate_time_fraction"], .6)

    def test_legacy_four_column_csv_keeps_no_mux_interval_contract(self):
        meta, data = pmu_fixture(multiplex=False)
        group = meta["groups"][1]
        meta = {key: value for key, value in meta.items() if key not in ("format", "groups")}
        meta.update(group="cache", event_names=group["event_names"])
        rows = [row for row in csv.DictReader(io.StringIO(data)) if row["group_id"] == "cache"]
        columns = ["read_begin_ns", "read_end_ns", "event_count", "enabled_ns", "running_ns", "cycles", "instructions", "event2", "event3", "read_error"]
        output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=columns); writer.writeheader()
        for row in rows:
            writer.writerow({**{key: row[key] for key in columns if key not in ("cycles", "instructions")},
                             "cycles": row["event0"], "instructions": row["event1"]})
        (self.directory / "pmu.json").write_text(json.dumps(meta)); (self.directory / "pmu.csv").write_text(output.getvalue())
        stage = Pmu(self.directory, [{"event": "tracing_mark_write", "body": f"PMU_CLOCK_SYNC|{SYNC}", "ns": SYNC}]).stage(1_002_000_000, 1_004_000_000)
        self.assertTrue(stage["bounds_valid"])
        self.assertEqual(stage["lower"]["instructions"], 600)
        self.assertEqual(stage["upper"]["instructions"], 1000)
        self.assertEqual(stage["interior_ipc"], 2)

    def test_streaming_frame_window_preserves_boundary_evidence_with_less_retention(self):
        full = self.load(all_groups=True)
        events = [{"event": "tracing_mark_write", "body": f"PMU_CLOCK_SYNC|{SYNC}", "ns": SYNC}]
        selected = Pmu(self.directory, events, window=(1_002_000_500, 1_003_500_500))
        # Both endpoints cut a read bracket; retaining just one arbitrary nearest
        # row would lose a boundary counter bin and produce a narrower false bound.
        bounds = (1_002_000_500, 1_003_500_500)
        self.assertEqual(full.stage(*bounds), selected.stage(*bounds))
        self.assertLess(selected.quality["retained_sample_count"], full.quality["retained_sample_count"])
        self.assertEqual(selected.quality["sample_count"], full.quality["sample_count"])
        self.assertEqual(selected.quality["statistics_scope"], "selected_callback_with_boundary_snapshots")

    def test_clock_marker_must_match_entire_timestamp_token(self):
        self.load()
        pmu = Pmu(self.directory, [{"event": "tracing_mark_write", "body": f"PMU_CLOCK_SYNC|{SYNC}7", "ns": SYNC}])
        self.assertFalse(pmu.clock_verified)

    def test_importer_profile_end_to_end_and_exact_override(self):
        manager = ParallelModelingManager(self.directory / "state")
        _, profile = import_fixture(manager)
        stage = profile["stages"][0]
        self.assertEqual(stage["metric_evidence"]["cache_mpki"]["estimate"], 100)
        self.assertIsNone(stage["compute"]["instructions"])
        self.assertFalse(stage["quality"]["pmu_usable"])
        self.assertEqual(stage["workload_class"], "mixed")
        self.assertEqual(stage["classification"]["confidence"]["level"], "low")
        self.assertEqual(stage["classification"]["evidence_quality"], "partial")
        self.assertIn("充分的阶段边界与 PMU 运行覆盖", stage["classification"]["missing_evidence"])
        _, exact = import_fixture(manager, exact=True)
        self.assertEqual(exact["stages"][0]["metric_evidence"]["instructions"]["exact"], 2000)
        self.assertIsNone(exact["stages"][0]["metric_evidence"]["instructions"]["estimate"])

    def historical_input(self, *, remove_raw=False):
        manager = ParallelModelingManager(self.directory / "state")
        dataset, current = import_fixture(manager, coarse=True)
        directory = manager.state_root / "inputs" / dataset["id"]
        path = directory / "parsed.json"
        parsed = json.loads(path.read_text())
        for component in parsed["component_features"]:
            pmu = component["pmu"]
            pmu["evidence_version"] = "grouped_stage_pmu/v2"
            for group in [pmu, *pmu["groups"].values()]:
                for key in list(group):
                    if key.startswith("weighted_") or key in ("time_weighted_estimates", "sample_window_convention"):
                        del group[key]
        path.write_text(json.dumps(parsed))
        if remove_raw: (directory / "pmu.csv").unlink()
        return manager, dataset, current

    def test_old_input_rebuilds_from_raw_and_leaves_existing_profile_immutable(self):
        manager, dataset, current = self.historical_input()
        features = WorkloadFeaturesManager(manager)
        old_hash = features.get_profile(current["id"])["profile_sha256"]
        rebuilt = features.analyze({"dataset_id": dataset["id"]})
        self.assertNotEqual(rebuilt["id"], current["id"])
        self.assertEqual(features.get_profile(current["id"])["profile_sha256"], old_hash)
        self.assertEqual(rebuilt["provenance"]["pmu_estimate_rebuild"]["status"], "rebuilt_from_raw_samples")
        metric = rebuilt["stages"][0]["metric_evidence"]["instructions"]
        self.assertAlmostEqual(metric["estimate"], 50_391_782 * 176_000 / 30_926_656)
        self.assertEqual(metric["bounds"], [0, 50_391_782])
        features.resolve_for_dataset(rebuilt["id"], dataset)
        csv_path = manager.state_root / "inputs" / dataset["id"] / "pmu.csv"
        csv_path.write_text(csv_path.read_text() + "\n")
        with self.assertRaisesRegex(Exception, "原始 PMU / Trace 数据已改变"):
            features.resolve_for_dataset(rebuilt["id"], dataset)

    def test_old_range_without_raw_samples_does_not_get_a_midpoint_estimate(self):
        manager, dataset, _ = self.historical_input(remove_raw=True)
        profile = WorkloadFeaturesManager(manager).analyze({"dataset_id": dataset["id"]})
        metric = profile["stages"][0]["metric_evidence"]["instructions"]
        self.assertEqual(profile["provenance"]["pmu_estimate_rebuild"]["status"], "raw_samples_unavailable")
        self.assertEqual(metric["bounds"], [0, 50_391_782])
        self.assertIsNone(metric["estimate"])
        self.assertEqual(metric["status"], "bounded")


class SchedulingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.manager = ParallelModelingManager(Path(self.temp.name))
        self.features = WorkloadFeaturesManager(self.manager)

    def tearDown(self): self.temp.cleanup()

    def analyze(self, trace):
        dataset = self.manager.import_dataset({"trace_text": trace, "hardware_profile_id": HARDWARE_ID})
        return self.features.analyze({"dataset_id": dataset["id"]})["stages"][0]

    def test_preemption_and_plain_r_are_disjoint_and_irq_stays_inside_running(self):
        trace = with_events(trace_fixture(), [
            line(1.0021, "irq_handler_entry", "irq=30 name=timer"),
            line(1.0022, "irq_handler_exit", "irq=30 ret=handled"),
            line(1.0023, "sched_switch", "prev_pid=10 prev_state=R+ ==> next_pid=20"),
            line(1.0027, "sched_switch", "prev_pid=20 prev_state=S ==> next_pid=10"),
            line(1.0030, "sched_switch", "prev_pid=10 prev_state=R ==> next_pid=20"),
            line(1.0033, "sched_switch", "prev_pid=20 prev_state=S ==> next_pid=10"),
            line(1.0034, "softirq_entry", "vec=1 [action=TIMER]"),
            line(1.0035, "softirq_exit", "vec=1 [action=TIMER]"),
        ])
        stage = self.analyze(trace); time = stage["time_breakdown"]; part = time["partition_ms"]
        self.assertAlmostEqual(part["preempted_wait"], .4)
        self.assertAlmostEqual(part["runnable_other"], .3)
        self.assertAlmostEqual(time["runnable_total_ms"], .7)
        self.assertAlmostEqual(time["irq_overlap_ms"], .1)
        self.assertAlmostEqual(time["softirq_overlap_ms"], .1)
        self.assertAlmostEqual(part["running"], 1.3)
        self.assertAlmostEqual(sum(part.values()), stage["wall_ms"])
        self.assertEqual(time["preemption_event_count"], 1)

    def test_sleep_binder_and_wakeup_not_mislabeled_preemption(self):
        stage = self.analyze(trace_fixture(binder=True)); part = stage["time_breakdown"]["partition_ms"]
        self.assertEqual(part["preempted_wait"], 0)
        self.assertAlmostEqual(part["runnable_other"], .1)
        self.assertAlmostEqual(part["blocked"], .4)
        self.assertLessEqual(stage["scheduling"]["binder_blocked_ms"], part["blocked"] + 1e-8)
        self.assertIsNone(stage["scheduling"]["io_wait_ms"])
        self.assertIsNone(stage["time_breakdown"]["irq_overlap_ms"])

    def test_unknown_and_historical_cause_unavailable_remain_explicit(self):
        trace = trace_fixture().splitlines(keepends=True)
        stage = self.analyze("".join(trace[1:]))
        self.assertAlmostEqual(stage["time_breakdown"]["partition_ms"]["unknown"], stage["wall_ms"])
        dataset = self.manager.import_dataset({"builtin_id": "gallery-edit-pmu-20261014"})
        stage = self.features.analyze({"dataset_id": dataset["id"]})["stages"][0]
        self.assertFalse(stage["time_breakdown"]["preemption_evidence_available"])
        self.assertIsNone(stage["time_breakdown"]["preemption_event_count"])


if __name__ == "__main__": unittest.main()
