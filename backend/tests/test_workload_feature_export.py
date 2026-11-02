"""CSV export retains the measurement and scheduling semantics of saved profiles."""
from __future__ import annotations

import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.core import PlatformError
from backend.parallel_modeling.feature_export import (export_profile_csv,
                                                       source_anchor_profile_contract_eligible)
from backend.parallel_modeling.manager import FULL_PMU_BUILTIN_ID, ParallelModelingManager
from backend.parallel_modeling.workload_features import create_workload_features_router


def csv_rows(content):
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline="")))


class WorkloadFeatureExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.manager = ParallelModelingManager(Path(cls.temporary.name))
        dataset = cls.manager.import_dataset({"builtin_id": FULL_PMU_BUILTIN_ID})
        cls.features = cls.manager.workload_features
        cls.profile = cls.features.analyze({"dataset_id": dataset["id"]})
        cls.profile_path = cls.features.state_root / (cls.profile["id"] + ".json")
        cls.stages = csv_rows(export_profile_csv(cls.profile))
        cls.metrics = csv_rows(export_profile_csv(cls.profile, "pmu"))
        app = FastAPI()

        @app.exception_handler(PlatformError)
        async def platform_error_handler(_, exc):
            return JSONResponse(status_code=exc.status_code, content={
                "error": {"code": exc.code, "message": exc.message, "details": exc.details}})

        app.include_router(create_workload_features_router(cls.features))
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.temporary.cleanup()

    def synthetic_profile(self):
        profile = copy.deepcopy(self.profile)
        profile["stages"] = profile["stages"][:1]
        return profile, profile["stages"][0]

    def test_full_group_archive_has_one_stage_row_and_all_metric_rows_per_builditem(self):
        self.assertEqual(len(self.stages), 26)
        self.assertEqual(len(self.metrics), 26 * 26)
        self.assertEqual([row["阶段ID"] for row in self.stages],
                         [stage["id"] for stage in self.profile["stages"]])
        self.assertEqual({row["特征版本ID"] for row in self.metrics}, {self.profile["id"]})
        self.assertEqual({row["数据集ID"] for row in self.metrics}, {self.profile["dataset_id"]})
        for stage in self.profile["stages"]:
            rows = [row for row in self.metrics if row["阶段ID"] == stage["id"]]
            self.assertEqual(len({row["指标ID"] for row in rows}), 26)
            self.assertEqual({row["指标ID"] for row in rows}, set(stage["metric_evidence"]))

    def test_real_estimates_export_as_numbers_and_keep_quality_and_mapping(self):
        first = self.profile["stages"][0]
        self.assertIsNone(first["compute"]["instructions"])
        self.assertEqual(self.stages[0]["负载分类ID"], first["classification"]["id"])
        self.assertEqual(self.stages[0]["分型置信度"], first["classification"]["confidence"]["level"])
        self.assertEqual(float(self.stages[0]["分型置信分数"]), first["classification"]["confidence"]["score"])
        self.assertIn("已核验 LLC 事件语义", self.stages[0]["分型缺失证据"])
        running = first["classification"].get("cpu_candidate", first["classification"])
        self.assertEqual(self.stages[0]["Running负载分类ID"], running["id"])
        self.assertEqual(self.stages[0]["Running分型置信度"], running["confidence"]["level"])
        placement = first["scheduling"]["cpu_placement"]
        self.assertEqual(int(self.stages[0]["主要运行逻辑CPU"]), placement["primary_cpu"])
        self.assertEqual(self.stages[0]["主要核组ID"], placement["primary_core_group_id"] or "")
        self.assertEqual(self.stages[0]["主要核组名称"], placement["primary_core_group_name"] or "")
        self.assertEqual(self.stages[0]["主要核组代理类型"], placement["primary_proxy_kind"] or "")
        self.assertIn(f"CPU{placement['primary_cpu']}@{placement['primary_core_group_name'] or '核型未知'}@", self.stages[0]["CPU驻留_CPU@核组@kHz:ms"])
        first_metric = next(row for row in self.metrics if row["阶段ID"] == first["id"])
        self.assertEqual(int(first_metric["主要运行逻辑CPU"]), placement["primary_cpu"])
        self.assertEqual(first_metric["主要核组ID"], placement["primary_core_group_id"] or "")
        self.assertEqual(self.stages[0]["CPU驻留覆盖状态"], "complete")
        self.assertEqual(self.stages[0]["CPU驻留可作为源锚"], "false")
        self.assertIn("hardware_identity_unverified",
                      first["scheduling"]["cpu_residency_evidence"]["reason_codes"])
        self.assertIn("source_core_group_unresolved",
                      first["scheduling"]["cpu_residency_evidence"]["reason_codes"])
        self.assertAlmostEqual(float(self.stages[0]["CPU驻留覆盖(%)"]), 100)
        self.assertEqual(first_metric["CPU驻留覆盖状态"], "complete")
        for key, header in (("instructions", "instructions_指令数(count)"),
                            ("cycles", "cycles_周期数(count)"), ("ipc", "ipc_IPC(ratio)")):
            evidence = first["metric_evidence"][key]
            self.assertEqual(self.stages[0][f"{key}_状态"], "estimated")
            self.assertAlmostEqual(float(self.stages[0][header]), evidence["estimate"])
            row = next(row for row in self.metrics
                       if row["阶段ID"] == first["id"] and row["指标ID"] == key)
            self.assertAlmostEqual(float(row["数值"]), evidence["estimate"])
            self.assertEqual(row["阶段对齐计数值"], "")
            self.assertEqual(row["估算方法"], "wall_time_overlap")
            self.assertEqual(row["估算覆盖范围"], "covered_stage_window")
            self.assertEqual(float(row["有效时间覆盖(%)"]), 100)
            self.assertIn("uniform_wall_time_attribution_assumption", row["原因代码"])
        llc = next(row for row in self.metrics if row["指标ID"] == "llc_misses")
        self.assertEqual(llc["硬件映射已核验"], "false")
        self.assertIn("hardware_llc_mapping_unverified", llc["原因代码"])

    def test_same_group_ratio_is_preserved_not_recomputed_with_base_instructions(self):
        profile, stage = self.synthetic_profile()
        stage["metric_evidence"].update({
            "instructions": {"status": "estimated", "estimate": 1000, "group_id": "base"},
            "cache_misses": {"status": "estimated", "estimate": 3, "group_id": "cache"},
            # This group's local denominator is 200 instructions, not base's 1000.
            "cache_mpki": {"status": "estimated", "estimate": 15, "group_id": "cache"},
        })
        row = csv_rows(export_profile_csv(profile))[0]
        self.assertEqual(float(row["cache_mpki_Cache MPKI(per_kinst)"]), 15)
        detail = next(row for row in csv_rows(export_profile_csv(profile, "pmu"))
                      if row["指标ID"] == "cache_mpki")
        self.assertEqual(detail["采集组"], "cache")
        self.assertEqual(float(detail["数值"]), 15)

    def test_unavailable_observed_zero_does_not_become_an_exact_or_estimated_zero(self):
        unavailable = next(row for row in self.metrics if row["指标ID"] == "branch_instructions"
                           and row["状态"] == "unavailable")
        self.assertEqual(unavailable["数值"], "")
        self.assertEqual(unavailable["阶段对齐计数值"], "")
        self.assertEqual(unavailable["估算值"], "")
        self.assertEqual(unavailable["已观测贡献下界_非全阶段界"], "0")
        self.assertIn("no_counter_runtime_in_stage", unavailable["原因代码"])
        estimated_zero = next(row for row in self.metrics if row["指标ID"] == "branch_instructions"
                              and row["状态"] == "estimated")
        self.assertEqual(float(estimated_zero["数值"]), 0)
        self.assertEqual(estimated_zero["阶段对齐计数值"], "")
        profile, stage = self.synthetic_profile()
        stage["metric_evidence"]["instructions"] = {"status": "exact", "exact": 0, "bounds": [0, 0]}
        exact = next(row for row in csv_rows(export_profile_csv(profile, "pmu"))
                     if row["指标ID"] == "instructions")
        self.assertEqual(exact["状态"], "exact")
        self.assertEqual(exact["数值"], "0")
        self.assertEqual(exact["阶段对齐计数值"], "0")
        self.assertEqual(exact["估算值"], "")

    def test_legacy_bounds_do_not_invent_midpoints_or_preemption(self):
        fixture = Path(__file__).resolve().parents[2] / "tools/tests/fixtures/workload-feature-legacy-boundary.json"
        profile = json.loads(fixture.read_text())
        stage = csv_rows(export_profile_csv(profile))[0]
        self.assertEqual(stage["instructions_指令数(count)"], "")
        self.assertEqual(stage["instructions_状态"], "bounded")
        self.assertEqual(stage["抢占来源可区分"], "false")
        self.assertEqual(stage["Interrupted抢占等待_Runnable子集(ms)"], "")
        self.assertEqual(stage["其余Runnable(ms)"], "")
        self.assertEqual(stage["抢占事件次数"], "")
        self.assertEqual(float(stage["Running(ms)"]), .176)
        self.assertEqual(float(stage["Runnable总计(ms)"]), 0)
        metrics = {row["指标ID"]: row for row in csv_rows(export_profile_csv(profile, "pmu"))}
        instructions = metrics["instructions"]
        self.assertEqual(instructions["阶段保守下界"], "0")
        self.assertEqual(instructions["阶段保守上界"], "50391782")
        self.assertEqual(instructions["估算值"], "")
        self.assertEqual(metrics["ipc"]["数值"], "")

    def test_legacy_profile_may_show_residency_but_never_exports_a_source_cost_anchor(self):
        profile, stage = self.synthetic_profile()
        profile["schema_version"] = "workload_feature_profile/v1"
        profile["engine_version"] = "1.5.0"
        profile.pop("hardware_profile_sha256", None)
        evidence = stage["scheduling"]["cpu_residency_evidence"]
        placement = stage["scheduling"]["cpu_placement"]
        evidence.update(status="complete", source_anchor_eligible=True)
        placement.update(coverage_status="complete", source_anchor_eligible=True)
        self.assertFalse(source_anchor_profile_contract_eligible(profile))
        self.assertEqual(csv_rows(export_profile_csv(profile))[0]["CPU驻留可作为源锚"], "false")
        self.assertEqual(csv_rows(export_profile_csv(profile, "pmu"))[0]["CPU驻留可作为源锚"], "false")

    def test_scheduler_partition_stays_exclusive_with_nested_service_and_irq_annotations(self):
        profile, stage = self.synthetic_profile()
        stage.update(wall_ms=10, cpu_ms=4,
                     states_ms={"on_cpu": 4, "runnable": 3, "blocked": 2, "unknown": 1},
                     time_breakdown={"partition_ms": {"running": 4, "preempted_wait": 1,
                                     "runnable_other": 2, "blocked": 2, "unknown": 1},
                         "runnable_total_ms": 3, "preemption_evidence_available": True,
                         "preemption_event_count": 1, "irq_overlap_ms": .2,
                         "irq_evidence_complete": False})
        stage["scheduling"].update(binder_blocked_ms=1, service_cpu_ms=7, service_residence_ms=8)
        row = csv_rows(export_profile_csv(profile))[0]
        partition = ["Running(ms)", "Interrupted抢占等待_Runnable子集(ms)",
                     "其余Runnable(ms)", "Blocked(ms)", "状态未知(ms)"]
        self.assertEqual(sum(float(row[key]) for key in partition), float(row["墙钟耗时(ms)"]))
        self.assertEqual(float(row["Runnable总计(ms)"]), 3)
        self.assertEqual(float(row["Binder同步阻塞_Blocked子集(ms)"]), 1)
        self.assertEqual(float(row["服务端CPU耗时_非本线程分区(ms)"]), 7)
        self.assertEqual(float(row["服务停留耗时_非额外墙钟分区(ms)"]), 8)
        self.assertEqual(float(row["IRQ重叠_不另计耗时(ms)"]), .2)
        self.assertEqual(row["IRQ证据完整"], "false")

    def test_csv_roundtrips_chinese_quotes_commas_and_newlines(self):
        profile, stage = self.synthetic_profile()
        name = '相册,"编辑页面"\n第二行'
        stage["name"] = name
        stage["scheduling"]["services"] = ['服务,"一"\n下一行']
        for view in ("stages", "pmu"):
            content = export_profile_csv(profile, view)
            self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(csv_rows(content)[0]["阶段名称"], name)
        self.assertEqual(csv_rows(export_profile_csv(profile))[0]["服务名称"], '服务,"一"\n下一行')

    def test_untrusted_text_is_neutralized_without_quoting_numeric_negative_values(self):
        for text in ('=1+1', '+SUM(A1)', '-2+3', '@SUM(A1)', '  =1', '\tplain', '\rplain', '\nplain'):
            with self.subTest(text=text):
                profile, stage = self.synthetic_profile()
                stage["name"] = text
                stage["relative_start_ms"] = -1.5
                for view in ("stages", "pmu"):
                    row = csv_rows(export_profile_csv(profile, view))[0]
                    self.assertEqual(row["阶段名称"], "'" + text)
                    self.assertEqual(row["相对回调起点(ms)"], "-1.5")

    def test_export_api_downloads_bom_csv_and_validates_view_and_missing_profile(self):
        base = f'/v1/workload-features/profiles/{self.profile["id"]}/export.csv'
        for view, expected_rows in (("stages", 26), ("pmu", 676)):
            response = self.client.get(base, params={"view": view})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
            self.assertIn("text/csv", response.headers["content-type"])
            self.assertIn("charset=utf-8", response.headers["content-type"])
            self.assertEqual(response.headers["content-disposition"],
                             f'attachment; filename="{self.profile["id"]}-{view}.csv"')
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(len(csv_rows(response.content)), expected_rows)
        self.assertEqual(self.client.get(base).content, export_profile_csv(self.profile))
        self.assertEqual(self.client.get(base, params={"view": "invalid"}).status_code, 422)
        missing = self.client.get('/v1/workload-features/profiles/wf_' + 'f' * 24 + '/export.csv')
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "workload_features_not_found")

    def test_exports_preserve_profile_object_and_stored_bytes(self):
        before = copy.deepcopy(self.profile)
        saved = self.profile_path.read_bytes()
        for view in ("stages", "pmu"):
            export_profile_csv(self.profile, view)
            response = self.client.get(f'/v1/workload-features/profiles/{self.profile["id"]}/export.csv',
                                       params={"view": view})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.profile, before)
        self.assertEqual(self.profile_path.read_bytes(), saved)
        self.assertEqual(self.features.get_profile(self.profile["id"]), before)


if __name__ == "__main__":
    unittest.main()
