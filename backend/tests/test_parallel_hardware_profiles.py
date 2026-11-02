"""Hardware topology management remains explicit, durable, and honest."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core import PlatformError
from backend.parallel_modeling.manager import (
    HARDWARE_ID,
    MAX_CONFIGURATION_CANDIDATES,
    MODEL_ID,
    ParallelModelingManager,
)
from backend.parallel_workbench import create_parallel_router
from backend.tests.test_parallel_modeling import trace_fixture
from backend.tools.desktop_platform_rpc import dispatch


def profile_payload(name: str = "测试机 · HDC 提取") -> dict:
    return {
        "schema_version": "parallel_hardware_profile/v1",
        "name": name,
        "vendor": "Huawei",
        "model": "fixture-device",
        "chipset": "fixture-soc",
        "os_build": "fixture-build",
        "source": "local_hdc",
        "cpu_groups": [
            {"id": "small", "kind": "small", "physical_core_count": 2,
             "logical_cpu_ids": [0, 1], "opps_khz": [500000, 1000000],
             "min_khz": 500000, "max_khz": 1000000},
            {"id": "middle", "kind": "middle", "physical_core_count": 2,
             "logical_cpu_ids": [2, 3], "opps_khz": [800000, 1200000, 1800000],
             "min_khz": 800000, "max_khz": 1800000},
            {"id": "big", "kind": "big", "physical_core_count": 1,
             "logical_cpu_ids": [4, 5], "opps_khz": [1000000, 1800000, 2400000],
             "min_khz": 1000000, "max_khz": 2400000},
        ],
        "evidence": {"device_serial_sha256": "a" * 64, "cpu_present": "0-5", "cpu_online": "0-5"},
    }


def arbitrary_profile_payload(domain_count: int) -> dict:
    payload = profile_payload(f"{domain_count} 频率域测试机")
    payload["cpu_groups"] = []
    cpu = 0
    for index in range(domain_count):
        core_count = index + 1
        cpus = list(range(cpu, cpu + core_count))
        cpu += core_count
        maximum = 1_200_000 + index * 400_000
        payload["cpu_groups"].append({
            "id": f"cluster_{index}",
            "kind": f"cluster_{index}",
            "physical_core_count": core_count,
            "logical_cpu_ids": cpus,
            "opps_khz": [400_000, maximum],
            "min_khz": 400_000,
            "max_khz": maximum,
        })
    return payload


class HardwareProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = ParallelModelingManager(self.root)

    def test_profile_is_content_addressed_persistent_and_catalog_has_proxy_model(self):
        created = self.manager.create_hardware_profile(profile_payload())
        self.assertRegex(created["id"], r"^hp_[0-9a-f]{24}$")
        self.assertRegex(created["profile_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(created["id"].startswith("hp_"))
        self.assertEqual(created["source"], "local_hdc")
        self.assertEqual(created["core_groups"], created["cpu_groups"])
        self.assertFalse(created["builtin"])
        self.assertFalse(created["archived"])
        self.assertEqual(created["model_status"], "topology_proxy_uncalibrated")
        model_id = created["compatible_model_package_id"]
        self.assertRegex(model_id, r"^tm_[0-9a-f]{24}$")

        restarted = ParallelModelingManager(self.root)
        self.assertEqual(restarted.create_hardware_profile(profile_payload()), created)
        with tempfile.TemporaryDirectory() as other_root:
            independent = ParallelModelingManager(Path(other_root)).create_hardware_profile(profile_payload())
        self.assertEqual(independent["id"], created["id"])
        self.assertEqual(independent["profile_sha256"], created["profile_sha256"])
        self.assertEqual(independent["model_package_id"], created["model_package_id"])
        rows = restarted.list_hardware_profiles()
        self.assertEqual([row["id"] for row in rows], [HARDWARE_ID, created["id"]])
        package = next(row for row in restarted.catalog()["model_packages"] if row["id"] == model_id)
        self.assertEqual(package["hardware_profile_id"], created["id"])
        self.assertEqual(package["validation"]["status"], "not_device_calibrated")
        self.assertIn("继承", package["description"])
        self.assertIn("未经", package["description"])

    def test_calibration_catalog_lists_every_active_profile_across_archive_restore_and_restart(self):
        first = self.manager.create_hardware_profile(profile_payload("校准设备 A"))
        second = self.manager.create_hardware_profile(profile_payload("校准设备 B"))

        expected_ids = {HARDWARE_ID, first["id"], second["id"]}
        self.assertEqual(
            {row["id"] for row in self.manager.calibration.catalog()["hardware_profiles"]},
            expected_ids,
        )

        self.manager.archive_hardware_profile(first["id"])
        restarted = ParallelModelingManager(self.root)
        self.assertEqual(
            {row["id"] for row in restarted.calibration.catalog()["hardware_profiles"]},
            {HARDWARE_ID, second["id"]},
        )

        restarted.restore_hardware_profile(first["id"])
        restored = ParallelModelingManager(self.root)
        self.assertEqual(
            {row["id"] for row in restored.calibration.catalog()["hardware_profiles"]},
            expected_ids,
        )

    def test_validation_rejects_raw_serial_overlap_and_inconsistent_opp_boundary(self):
        cases = []
        raw_serial = profile_payload("raw serial")
        raw_serial["evidence"]["device_serial"] = "secret"
        cases.append(raw_serial)
        overlap = profile_payload("overlap")
        overlap["cpu_groups"][1]["logical_cpu_ids"] = [1, 2]
        cases.append(overlap)
        boundary = profile_payload("boundary")
        boundary["cpu_groups"][0]["max_khz"] = 999999
        cases.append(boundary)
        for payload in cases:
            with self.subTest(name=payload["name"]), self.assertRaises(PlatformError) as caught:
                self.manager.create_hardware_profile(payload)
            self.assertEqual(caught.exception.code, "parallel_hardware_profile_invalid")
        self.assertEqual(len(self.manager.list_hardware_profiles()), 1)

    def test_arbitrary_nonzero_frequency_domains_are_preserved_with_explicit_proxy_mapping(self):
        for count in (1, 2, 4):
            with self.subTest(domain_count=count):
                created = self.manager.create_hardware_profile(arbitrary_profile_payload(count))
                self.assertEqual(len(created["core_groups"]), count)
                self.assertEqual([group["id"] for group in created["core_groups"]],
                                 [f"cluster_{index}" for index in range(count)])
                self.assertTrue(all(group["proxy_kind"] in {"small", "middle", "big"}
                                    for group in created["core_groups"]))
                if count == 4:
                    self.assertEqual([group["proxy_kind"] for group in created["core_groups"]],
                                     ["small", "middle", "middle", "big"])

        four = next(row for row in self.manager.list_hardware_profiles()
                    if row["name"] == "4 频率域测试机")
        dataset = self.manager.import_dataset({"trace_text": trace_fixture(), "hardware_profile_id": four["id"]})
        analysis = self.manager.analyze({"dataset_id": dataset["id"]})
        result = self.manager.search({"analysis_id": analysis["id"], "space": {
            "available_core_group_ids": ["cluster_3", "cluster_2"],
            "main_core_group_id": "cluster_3", "worker_counts": [1, 2], "frequency_policy": "max_opps"}})
        self.assertEqual(set(result["best"]["frequency_vector"]),
                         {"cluster_0", "cluster_1", "cluster_2", "cluster_3"})
        self.assertTrue(all(row["configuration"]["main"]["kind"] == "cluster_3"
                            for row in result["candidates"]))

    def test_physical_core_topology_preserves_smt_siblings_and_rejects_ambiguous_maps(self):
        payload = profile_payload("SMT 拓扑测试机")
        payload["cpu_groups"][2].update(
            physical_core_representative_cpu_ids=[4],
            physical_core_topology=[{
                "id": "core_0",
                "logical_cpu_ids": [4, 5],
                "representative_logical_cpu_id": 4,
                "topology_source": "thread_siblings_list",
            }],
        )
        created = self.manager.create_hardware_profile(payload)
        big = created["core_groups"][2]
        self.assertEqual(big["physical_core_count"], 1)
        self.assertEqual(big["logical_cpu_count"], 2)
        self.assertEqual(len(big["logical_cpu_ids"]), 2)
        self.assertEqual(big["physical_core_topology"][0]["logical_cpu_ids"], [4, 5])
        self.assertEqual(big["physical_core_topology"][0]["representative_logical_cpu_id"], 4)

        invalid = profile_payload("重叠 SMT 拓扑")
        invalid["cpu_groups"][0]["physical_core_topology"] = [
            {"id": "core_0", "logical_cpu_ids": [0, 1], "representative_logical_cpu_id": 0},
            {"id": "core_1", "logical_cpu_ids": [1], "representative_logical_cpu_id": 1},
        ]
        with self.assertRaises(PlatformError) as caught:
            self.manager.create_hardware_profile(invalid)
        self.assertEqual(caught.exception.code, "parallel_hardware_profile_invalid")

    def test_kirin_9030_pro_style_nine_physical_fourteen_logical_topology_round_trips(self):
        payload = profile_payload("Kirin 9030 Pro 拓扑样例")
        payload["cpu_groups"] = [
            {
                "id": "small", "kind": "small", "physical_core_count": 4,
                "logical_cpu_ids": [0, 1, 2, 3], "opps_khz": [400_000, 1_600_000],
                "min_khz": 400_000, "max_khz": 1_600_000,
                "physical_core_representative_cpu_ids": [0, 1, 2, 3],
                "physical_core_topology": [
                    {"id": f"core_{index}", "logical_cpu_ids": [index],
                     "representative_logical_cpu_id": index}
                    for index in range(4)
                ],
            },
            {
                "id": "middle", "kind": "middle", "physical_core_count": 4,
                "logical_cpu_ids": list(range(4, 12)), "opps_khz": [600_000, 2_400_000],
                "min_khz": 600_000, "max_khz": 2_400_000,
                "physical_core_representative_cpu_ids": [4, 6, 8, 10],
                "physical_core_topology": [
                    {"id": f"core_{index}", "logical_cpu_ids": [cpu, cpu + 1],
                     "representative_logical_cpu_id": cpu}
                    for index, cpu in enumerate(range(4, 12, 2))
                ],
            },
            {
                "id": "big", "kind": "big", "physical_core_count": 1,
                "logical_cpu_ids": [12, 13], "opps_khz": [900_000, 3_200_000],
                "min_khz": 900_000, "max_khz": 3_200_000,
                "physical_core_representative_cpu_ids": [12],
                "physical_core_topology": [{
                    "id": "core_0", "logical_cpu_ids": [12, 13],
                    "representative_logical_cpu_id": 12,
                }],
            },
        ]
        payload["evidence"].update(cpu_present=list(range(14)), cpu_online=list(range(14)))

        created = self.manager.create_hardware_profile(payload)
        restarted = ParallelModelingManager(self.root).get_hardware_profile(created["id"])
        self.assertEqual(sum(group["physical_core_count"] for group in restarted["core_groups"]), 9)
        self.assertEqual(sum(group["logical_cpu_count"] for group in restarted["core_groups"]), 14)
        self.assertEqual(
            [(group["physical_core_count"], group["logical_cpu_count"]) for group in restarted["core_groups"]],
            [(4, 4), (4, 8), (1, 2)],
        )
        self.assertEqual(restarted["core_groups"][1]["physical_core_representative_cpu_ids"], [4, 6, 8, 10])

    def test_dynamic_topology_configuration_count_is_exact_before_expansion(self):
        profile = self.manager.create_hardware_profile(arbitrary_profile_payload(4))
        model = self.manager._resolve_model(profile["model_package_id"])
        configurations, _ = self.manager._configurations(
            {"space": {"worker_counts": [1, 2], "frequency_policy": "all_opps"}},
            model,
            profile,
        )
        self.assertEqual(len(configurations), 72)
        self.assertTrue(all(configuration["main"]["kind"] == "cluster_3"
                            for configuration in configurations))

    def test_large_dynamic_topology_is_rejected_before_configuration_expansion(self):
        payload = arbitrary_profile_payload(16)
        for group in payload["cpu_groups"]:
            maximum = group["max_khz"]
            group["opps_khz"] = [
                400_000 + (maximum - 400_000) * index // 63
                for index in range(64)
            ]
        profile = self.manager.create_hardware_profile(payload)
        model = self.manager._resolve_model(profile["model_package_id"])

        with self.assertRaises(PlatformError) as caught:
            self.manager._configurations(
                {"space": {"frequency_policy": "all_opps"}},
                model,
                profile,
            )

        self.assertEqual(caught.exception.code, "parallel_modeling_search_space_too_large")
        self.assertEqual(caught.exception.details["candidate_count"], 664_320)
        self.assertEqual(caught.exception.details["candidate_limit"], MAX_CONFIGURATION_CANDIDATES)
        self.assertEqual(caught.exception.details["active_core_group_count"], 16)
        self.assertEqual(caught.exception.details["main_opp_count"], 64)
        self.assertEqual(caught.exception.details["frequency_counts"]["cluster_0"], 64)

    def test_archive_is_idempotent_but_builtin_and_referenced_profiles_are_protected(self):
        disposable = self.manager.create_hardware_profile(profile_payload("可归档画像"))
        archived = self.manager.archive_hardware_profile(disposable["id"])
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(self.manager.archive_hardware_profile(disposable["id"]), archived)
        self.assertNotIn(disposable["id"], [row["id"] for row in self.manager.catalog()["hardware_profiles"]])
        restored = self.manager.restore_hardware_profile(disposable["id"])
        self.assertFalse(restored["archived"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["profile_sha256"], disposable["profile_sha256"])
        self.assertIn(disposable["id"], [row["id"] for row in self.manager.catalog()["hardware_profiles"]])
        self.assertEqual(self.manager.create_hardware_profile(profile_payload("可归档画像"))["id"], disposable["id"])
        with self.assertRaises(PlatformError) as caught:
            self.manager.archive_hardware_profile(HARDWARE_ID)
        self.assertEqual(caught.exception.code, "parallel_hardware_profile_read_only")

        referenced = self.manager.create_hardware_profile(profile_payload("被引用画像"))
        self.manager.import_dataset({"trace_text": trace_fixture(), "hardware_profile_id": referenced["id"]})
        with self.assertRaises(PlatformError) as caught:
            self.manager.archive_hardware_profile(referenced["id"])
        self.assertEqual(caught.exception.code, "parallel_hardware_profile_in_use")
        self.assertTrue(caught.exception.details["dataset_ids"])

    def test_binding_derives_dataset_and_custom_topology_drives_search(self):
        profile = self.manager.create_hardware_profile(profile_payload())
        source = self.manager.import_dataset({"trace_text": trace_fixture(), "name": "未绑定负载"})
        source_input = self.manager.state_root / "inputs" / source["id"]
        before = {path.name: path.read_bytes() for path in source_input.iterdir() if path.is_file()}
        derived = self.manager.bind_dataset_hardware(source["id"], {"hardware_profile_id": profile["id"]})

        self.assertNotEqual(derived["id"], source["id"])
        self.assertIsNone(self.manager.get_dataset(source["id"])["hardware_profile_id"])
        self.assertEqual(derived["hardware_profile_id"], profile["id"])
        self.assertEqual(derived["provenance"]["derived_from_dataset_id"], source["id"])
        self.assertEqual(derived["quality"]["prediction_applicability"], "user_bound_topology_proxy_prediction")
        self.assertFalse(derived["quality"]["hardware_identity_verified"])
        derived_input = self.manager.state_root / "inputs" / derived["id"]
        self.assertEqual(before, {path.name: path.read_bytes() for path in derived_input.iterdir() if path.is_file()})
        self.assertEqual(self.manager.bind_dataset_hardware(source["id"], {"hardware_profile_id": profile["id"]}), derived)

        analysis = self.manager.analyze({"dataset_id": derived["id"]})
        self.assertEqual(analysis["cost_model"]["id"], profile["model_package_id"])
        result = self.manager.search({"analysis_id": analysis["id"], "space": {
            "available_core_group_ids": ["big", "middle"], "worker_counts": [1, 2, 3, 4],
            "frequency_policy": "all_opps"}})
        self.assertEqual(result["model_package"]["status"], "topology_proxy_uncalibrated")
        self.assertEqual(result["model_package"]["hardware_profile_id"], profile["id"])
        self.assertEqual(result["search_provenance"]["hardware_profile_sha256"], profile["profile_sha256"])
        self.assertEqual({row["frequency_vector"]["big"] for row in result["candidates"]}, {1000000, 1800000, 2400000})
        self.assertLessEqual(max(len(row["configuration"]["workers"]) for row in result["candidates"]), 2)
        self.assertTrue(any("继承" in text and "尚未" in text for text in result["assumptions"]))

    def test_explicit_mismatched_model_is_rejected_without_mutation(self):
        profile = self.manager.create_hardware_profile(profile_payload())
        dataset = self.manager.import_dataset({"trace_text": trace_fixture(), "hardware_profile_id": profile["id"]})
        with self.assertRaises(PlatformError) as caught:
            self.manager.analyze({"dataset_id": dataset["id"], "model_package_id": MODEL_ID})
        self.assertEqual(caught.exception.code, "parallel_modeling_cost_model_mismatch")
        self.assertEqual(self.manager.list_analyses(), [])

    def test_http_crud_and_immutable_binding_contract(self):
        service = type("Service", (), {"modeling": self.manager})()
        app = FastAPI()
        app.include_router(create_parallel_router(service))
        # These successful routes do not need the asynchronous service methods.
        with TestClient(app) as client:
            response = client.get("/v1/parallel-modeling/hardware-profiles")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["profiles"][0]["id"], HARDWARE_ID)
            response = client.post("/v1/parallel-modeling/hardware-profiles", json=profile_payload())
            self.assertEqual(response.status_code, 200)
            profile = response.json()
            self.assertEqual(client.get(f"/v1/parallel-modeling/hardware-profiles/{profile['id']}").status_code, 200)
            self.assertEqual(client.post(f"/v1/parallel-modeling/hardware-profiles/{profile['id']}/archive").status_code, 200)
            restored = client.post(f"/v1/parallel-modeling/hardware-profiles/{profile['id']}/restore")
            self.assertEqual(restored.status_code, 200)
            self.assertEqual(restored.json()["status"], "active")
            source = self.manager.import_dataset({"trace_text": trace_fixture()})
            response = client.post(f"/v1/parallel-modeling/datasets/{source['id']}/hardware-binding",
                                   json={"hardware_profile_id": profile["id"]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["hardware_profile_id"], profile["id"])

    def test_desktop_rpc_allowlists_profile_crud_and_binding(self):
        with patch.dict(os.environ, {"HETERO_STATE_ROOT": str(self.root)}, clear=False):
            profile = dispatch({"method": "POST", "path": "/v1/parallel-modeling/hardware-profiles",
                                "body": profile_payload()})["value"]
            listed = dispatch({"method": "GET", "path": "/v1/parallel-modeling/hardware-profiles"})["value"]
            self.assertIn(profile["id"], [row["id"] for row in listed["profiles"]])
            loaded = dispatch({"method": "GET", "path": f"/v1/parallel-modeling/hardware-profiles/{profile['id']}"})["value"]
            self.assertEqual(loaded["profile_sha256"], profile["profile_sha256"])
            archived = dispatch({"method": "POST", "path": f"/v1/parallel-modeling/hardware-profiles/{profile['id']}/archive", "body": {}})["value"]
            self.assertTrue(archived["archived"])
            restored = dispatch({"method": "POST", "path": f"/v1/parallel-modeling/hardware-profiles/{profile['id']}/restore", "body": {}})["value"]
            self.assertFalse(restored["archived"])
            source = dispatch({"method": "POST", "path": "/v1/parallel-modeling/datasets",
                               "body": {"trace_text": trace_fixture()}})["value"]
            bound = dispatch({"method": "POST",
                              "path": f"/v1/parallel-modeling/datasets/{source['id']}/hardware-binding",
                              "body": {"hardware_profile_id": profile["id"]}})["value"]
            self.assertEqual(bound["hardware_profile_id"], profile["id"])


if __name__ == "__main__":
    unittest.main()
