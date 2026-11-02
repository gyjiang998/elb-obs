from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.application_flow_trace import ApplicationFlowRuntimeTraceManager
from backend.core import ArtifactStore, PlatformError, SourceConnectorManager
from backend.custom_api_kb import CustomApiKnowledgeBaseManager
from backend.device_gateway import DeviceGateway
from backend.runtime_action_router import (
    LEGACY_BROWSER_DEVICE_ACTIONS,
    PlatformRuntimeActionRouter,
    _device_deployment_idempotency_key,
)


class _UnavailableLocalGateway:
    def local_discover(self) -> dict[str, object]:
        return {
            "schema_version": "device_discovery/v1",
            "hdc": {
                "available": False,
                "error_code": "DEVICE_HDC_UNAVAILABLE",
                "diagnostic": "未找到可执行的 HDC。",
            },
            "devices": [],
        }


class _BuildResultStore:
    def read(self, artifact_type: str, artifact_id: str) -> dict[str, object]:
        assert artifact_type == "build_source"
        assert artifact_id == "art_source"
        return {"artifact_id": artifact_id, "profile_id": "gpu_gles3"}


class _FailedBuildWorker:
    def build(self, **_: object) -> dict[str, object]:
        error = {
            "code": "BUILD_PROFILE_RUNTIME_UNAVAILABLE",
            "message": "固定运行时库未配置。",
            "details": {"missing_library_names": ["libEGL.so"]},
        }
        return {
            "artifact_id": "art_result",
            "status": "failed",
            "build_identity": "a" * 64,
            "summary_zh": "E0 构建失败 [BUILD_PROFILE_RUNTIME_UNAVAILABLE]：固定运行时库未配置。",
            "diagnostic": {"stage": "profile_runtime", "error": error, "compile": None, "link": None},
            "manifest": {"status": "failed", "error": error},
        }


class _ApplicationFlowConnectors:
    @staticmethod
    def list() -> list[dict[str, object]]:
        return []


class _ScopedApplicationFlows:
    def __init__(self) -> None:
        self.connectors = _ApplicationFlowConnectors()
        self.list_analysis_id = ""
        self.read_scope: tuple[str, str, str] | None = None

    def list_assets(self, **kwargs: object) -> dict[str, object]:
        self.list_analysis_id = str(kwargs.get("application_flow_analysis_id") or "")
        return {
            "schema_version": "application_flow_asset_catalog/v1",
            "discoveries": [],
            "scenarios": [],
        }

    def require_analysis_scope(
        self,
        artifact_type: str,
        artifact_id: str,
        application_flow_analysis_id: str,
    ) -> dict[str, object]:
        self.read_scope = (artifact_type, artifact_id, application_flow_analysis_id)
        return {
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "application_flow_analysis_id": application_flow_analysis_id,
        }


class RuntimeActionRouterTests(unittest.TestCase):
    @staticmethod
    def _experiment_finding_input(evidence_artifact_id: str) -> dict[str, object]:
        return {
            "summary_zh": "Vulkan compute 已完成设备侧功能对照，并发现一个需要继续复现的加载路径差异。",
            "findings": [
                {
                    "title": "Vulkan loader 直接链接与显式加载行为不同",
                    "category": "deployment_runtime_contract",
                    "observed_behavior": "当前实验中，直接链接后的首次 Vulkan 调用使进程静默结束；改为 dlopen/dlsym 后初始化与 dispatch 完成。",
                    "why_it_matters": "该差异会影响真机实验的运行时装载方式和失败诊断，但当前证据不足以形成跨设备规则。",
                    "trigger_conditions": ["当前设备", "gpu_vulkan profile", "相同 compute workload"],
                    "successful_revision": "使用 dlopen(\"libvulkan.so\", RTLD_NOW) 并通过 dlsym 取得函数指针。",
                    "applicability_scope": "仅限本次设备、profile、运行时和二进制组合。",
                    "limitations": ["尚未在第二台设备复现", "尚未完成直接链接与动态加载的同条件重复对照"],
                    "reproducibility": "first_observation",
                    "proposed_destinations": ["build_profile", "hardware_api_kb", "regression_fixture"],
                    "evidence_artifact_ids": [evidence_artifact_id],
                }
            ],
            "limitations": ["功能实验不证明热点已集成或存在性能收益"],
            "evidence_artifacts": [
                {"artifact_type": "device_remote_shell_run", "artifact_id": evidence_artifact_id}
            ],
        }

    @staticmethod
    def _learning_router(store: ArtifactStore) -> PlatformRuntimeActionRouter:
        return PlatformRuntimeActionRouter(
            artifact_store=store,
            build_worker=object(),  # type: ignore[arg-type]
            device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
            default_source_connector=lambda: {"connector_id": "test", "snapshot": {"match_level": "worktree"}},
            bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
        )

    @staticmethod
    def _write_shell_evidence(store: ArtifactStore) -> dict[str, object]:
        return store.write(
            "device_remote_shell_run",
            {"status": "succeeded", "execution": {"return_code": 0, "stdout": "GPU_MATCH=1"}},
            source_connector_id="test",
            source_binding={"match_level": "worktree"},
            producer={"name": "test", "version": "1.0.0"},
        )

    def test_local_discover_exposes_missing_hdc_as_a_failed_observation(self) -> None:
        """A missing executor must not look like an empty successful scan."""

        router = PlatformRuntimeActionRouter(
            artifact_store=object(),  # type: ignore[arg-type]
            build_worker=object(),  # type: ignore[arg-type]
            device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
            default_source_connector=lambda: {"connector_id": "test", "snapshot": {}},
            bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
        )

        result = router.execute("device.discover", {}, {"project_id": "local-default", "context": {}})

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "DEVICE_HDC_UNAVAILABLE")

    def test_pending_skill_bootstrap_does_not_block_platform_actions(self) -> None:
        router = PlatformRuntimeActionRouter(
            artifact_store=object(),  # type: ignore[arg-type]
            build_worker=object(),  # type: ignore[arg-type]
            device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
            default_source_connector=lambda: {"connector_id": "test", "snapshot": {}},
            bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
            application_flows=object(),  # type: ignore[arg-type]
        )
        run = {
            "project_id": "local-default",
            "context": {},
            "skill_bootstrap": {
                "status": "pending",
                "bootstrap_id": "bootstrap_pending",
            },
        }

        router.validate(
            "application_flow.inspect_repository",
            {"source_connector_id": "connector_test"},
            run,
        )

    def test_application_flow_asset_access_uses_the_current_root_analysis(self) -> None:
        application_flows = _ScopedApplicationFlows()
        router = PlatformRuntimeActionRouter(
            artifact_store=object(),  # type: ignore[arg-type]
            build_worker=object(),  # type: ignore[arg-type]
            device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
            default_source_connector=lambda: {"connector_id": "test", "snapshot": {}},
            bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
            application_flows=application_flows,  # type: ignore[arg-type]
        )
        run = {
            "run_id": "run_shared_page_agent",
            "project_id": "local-default",
            "context": {
                "focused_task": {
                    "kind": "application_flow_generation",
                    "application_flow_analysis_id": "application-flow:root-b",
                },
            },
        }

        router.execute("application_flow.list_assets", {}, run)
        artifact_id = "art_0123456789abcdef01234567"
        result = router.execute(
            "application_flow.read_asset",
            {"artifact_type": "application_flow_discovery", "artifact_id": artifact_id},
            run,
        )

        self.assertEqual(application_flows.list_analysis_id, "application-flow:root-b")
        self.assertEqual(
            application_flows.read_scope,
            ("application_flow_discovery", artifact_id, "application-flow:root-b"),
        )
        self.assertEqual(result["artifact"]["application_flow_analysis_id"], "application-flow:root-b")

    def test_runtime_trace_action_uses_the_page_bound_artifact_over_agent_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "state", project_id="local-default")
            selected = store.write(
                "trace_analysis_result",
                {"input_kind": "external_trace_analysis_report", "trace_name": "selected.json"},
                source_connector_id="connector_test",
                source_binding={"match_level": "unknown"},
                producer={"name": "test", "version": "1.0.0"},
            )
            router = PlatformRuntimeActionRouter(
                artifact_store=store,
                build_worker=object(),  # type: ignore[arg-type]
                device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
                default_source_connector=lambda: {"connector_id": "connector_test", "snapshot": {}},
                bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
                application_flows=object(),  # type: ignore[arg-type]
                application_flow_runtime_traces=object(),  # type: ignore[arg-type]
            )
            run = {
                "run_id": "run_trace_binding",
                "project_id": "local-default",
                "context": {
                    "focused_task": {
                        "kind": "application_flow_generation",
                        "trace_analysis_artifact_id": selected["artifact_id"],
                    },
                },
            }
            action_input = {
                "source_connector_id": "connector_test",
                "analysis_artifact_id": "art_000000000000000000000000",
                "scenario_artifact_id": "art_111111111111111111111111",
            }

            router.validate("application_flow.inspect_runtime_trace", action_input, run)

            self.assertEqual(action_input["analysis_artifact_id"], selected["artifact_id"])

    def test_runtime_trace_action_uses_durable_analysis_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "state", project_id="local-default")
            selected = store.write(
                "trace_analysis_result",
                {"input_kind": "external_trace_analysis_report", "trace_name": "server-bound.json"},
                source_connector_id="connector_test",
                source_binding={"match_level": "unknown"},
                producer={"name": "test", "version": "1.0.0"},
            )
            runtime_traces = ApplicationFlowRuntimeTraceManager(Path(directory) / "state", store, object())  # type: ignore[arg-type]
            runtime_traces.bind_trace("application-flow:bound", selected["artifact_id"])
            router = PlatformRuntimeActionRouter(
                artifact_store=store,
                build_worker=object(),  # type: ignore[arg-type]
                device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
                default_source_connector=lambda: {"connector_id": "connector_test", "snapshot": {}},
                bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
                application_flows=object(),  # type: ignore[arg-type]
                application_flow_runtime_traces=runtime_traces,
            )
            run = {
                "run_id": "run_trace_server_binding",
                "project_id": "local-default",
                "context": {
                    "focused_task": {
                        "kind": "application_flow_generation",
                        "application_flow_analysis_id": "application-flow:bound",
                        "trace_analysis_artifact_id": "art_000000000000000000000000",
                    },
                },
            }
            action_input = {
                "source_connector_id": "connector_test",
                "analysis_artifact_id": "",
                "scenario_artifact_id": "art_111111111111111111111111",
            }

            router.validate("application_flow.inspect_runtime_trace", action_input, run)

            self.assertEqual(action_input["analysis_artifact_id"], selected["artifact_id"])

    def test_runtime_trace_action_reports_a_stale_deployment_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "state", project_id="local-default")
            router = PlatformRuntimeActionRouter(
                artifact_store=store,
                build_worker=object(),  # type: ignore[arg-type]
                device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
                default_source_connector=lambda: {"connector_id": "connector_test", "snapshot": {}},
                bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
                application_flows=object(),  # type: ignore[arg-type]
                application_flow_runtime_traces=object(),  # type: ignore[arg-type]
            )
            run = {
                "run_id": "run_stale_trace_binding",
                "project_id": "local-default",
                "context": {
                    "focused_task": {
                        "kind": "application_flow_generation",
                        "trace_analysis_artifact_id": "art_000000000000000000000000",
                    },
                },
            }

            with self.assertRaises(PlatformError) as raised:
                router.validate(
                    "application_flow.inspect_runtime_trace",
                    {
                        "source_connector_id": "connector_test",
                        "analysis_artifact_id": "art_000000000000000000000000",
                        "scenario_artifact_id": "art_111111111111111111111111",
                    },
                    run,
                )

            self.assertEqual(raised.exception.code, "APPLICATION_FLOW_TRACE_ARTIFACT_UNAVAILABLE")
            self.assertIn("重新导入", raised.exception.message)

    def test_expired_device_lease_result_is_not_checkpoint_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = DeviceGateway(workspace_root=root, state_root=root / "state")
            pairing = gateway.create_pairing_code(project_id="local-default")
            registered = gateway.register_bridge(
                pairing_code=str(pairing["pairing_code"]),
                display_name="lease-checkpoint-test",
                capabilities={},
            )
            bridge_id = str(registered["bridge_id"])
            logical_device_id = "device_checkpoint_test"
            lease = gateway.create_lease(
                project_id="local-default",
                bridge_id=bridge_id,
                logical_device_id=logical_device_id,
            )
            router = PlatformRuntimeActionRouter(
                artifact_store=object(),  # type: ignore[arg-type]
                build_worker=object(),  # type: ignore[arg-type]
                device_gateway=gateway,
                default_source_connector=lambda: {"connector_id": "test", "snapshot": {}},
                bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
            )
            action_input = {"bridge_id": bridge_id, "logical_device_id": logical_device_id}
            prior = {"status": "succeeded", "result": {"status": "succeeded", "device_lease": lease}}
            run = {"project_id": "local-default"}

            self.assertTrue(router.can_reuse("device.create_lease", action_input, prior, run))

            registry = gateway._read_registry()
            registry["leases"][str(lease["lease_id"])]["expires_at_epoch"] = 0
            gateway._write_registry(registry)

            self.assertFalse(router.can_reuse("device.create_lease", action_input, prior, run))

    def test_explicit_deploy_retry_nonce_creates_a_new_bridge_identity(self) -> None:
        common = {
            "run_id": "run_test",
            "build_artifact_id": "art_build",
            "probe_artifact_id": "art_probe",
        }

        first = _device_deployment_idempotency_key(**common, retry_nonce="")
        repeated = _device_deployment_idempotency_key(**common, retry_nonce="")
        retry = _device_deployment_idempotency_key(**common, retry_nonce="retry-after-missing-file")

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, retry)

    def test_agent_can_list_then_read_one_hash_verified_hardware_template(self) -> None:
        template_root = Path(__file__).resolve().parents[2] / "agent-resources" / "hardware-api-templates"
        router = PlatformRuntimeActionRouter(
            artifact_store=object(),  # type: ignore[arg-type]
            build_worker=object(),  # type: ignore[arg-type]
            device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
            default_source_connector=lambda: {"connector_id": "test", "snapshot": {}},
            bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
            curated_template_root=template_root,
        )
        run = {"run_id": "run_test", "project_id": "local-default", "context": {}}

        list_input = {"unit": "gpu", "profile_id": "gpu_vulkan", "api_family": "vulkan_compute"}
        router.validate("platform.list_hardware_api_templates", list_input, run)
        listing = router.execute("platform.list_hardware_api_templates", list_input, run)
        template_id = listing["templates"][0]["template_id"]
        bundle = router.execute("platform.read_hardware_api_template", {"template_id": template_id}, run)

        self.assertEqual(listing["template_count"], 1)
        self.assertEqual(template_id, "gpu.vulkan.compute-buffer.v1")
        self.assertEqual(bundle["template"]["profile_id"], "gpu_vulkan")
        self.assertEqual({item["path"] for item in bundle["files"]}, set(bundle["template"]["files"]))

    def test_agent_can_build_a_new_isolated_custom_api_kb_from_a_registered_connector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "aae-module"
            include_root = source_root / "interfaces" / "include"
            include_root.mkdir(parents=True)
            (include_root / "aae_api.h").write_text(
                """/// Process one PCM frame with the accelerator.
int AAE_Process(const float *input, float *output, unsigned int frame_count);
""",
                encoding="utf-8",
            )
            store = ArtifactStore(root / "state", project_id="local-default")
            connectors = SourceConnectorManager(root / "state", store, workspace_root=root)
            connector = {
                "connector_id": "local_aae_test",
                "kind": "local",
                "mode": "local_readonly",
                "display_name": "AAE module",
                "source_root": str(source_root.resolve()),
                "snapshot": {"snapshot_id": "source_snapshot_aae_test", "clean": True, "repositories": []},
            }
            connectors.registry.write({"connectors": [connector]})
            manager = CustomApiKnowledgeBaseManager(root / "state", connectors)
            router = PlatformRuntimeActionRouter(
                artifact_store=store,
                build_worker=object(),  # type: ignore[arg-type]
                device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
                default_source_connector=lambda: connector,
                bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
                custom_api_kbs=manager,
            )
            run = {"run_id": "run_test", "project_id": "local-default", "context": {}}

            listing = router.execute("knowledge.list_custom_api_kbs", {}, run)
            self.assertEqual(listing["knowledge_bases"], [])
            self.assertEqual(listing["registered_source_connectors"][0]["source_connector_id"], "local_aae_test")

            action_input = {"unit_name": "AAE", "source_connector_id": "local_aae_test"}
            router.validate("knowledge.build_custom_api_kb", action_input, run)
            result = router.execute("knowledge.build_custom_api_kb", action_input, run)

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["knowledge_base"]["display_name"], "AAE API（自建）")
            self.assertEqual(result["knowledge_base"]["origin"], "user_built")
            self.assertEqual(result["records"][0]["symbol"], "AAE_Process")
            self.assertIn("开发者", result["summary_zh"])

    def test_template_listing_returns_recovery_metadata_for_unknown_filter_value(self) -> None:
        template_root = Path(__file__).resolve().parents[2] / "agent-resources" / "hardware-api-templates"
        router = PlatformRuntimeActionRouter(
            artifact_store=object(),  # type: ignore[arg-type]
            build_worker=object(),  # type: ignore[arg-type]
            device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
            default_source_connector=lambda: {"connector_id": "test", "snapshot": {}},
            bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
            curated_template_root=template_root,
        )
        run = {"run_id": "run_test", "project_id": "local-default", "context": {}}

        list_input = {"api_family": "gpu_vulkan", "query": "逐元素 RGBA buffer"}
        router.validate("platform.list_hardware_api_templates", list_input, run)
        listing = router.execute("platform.list_hardware_api_templates", list_input, run)

        self.assertEqual(listing["template_count"], 0)
        self.assertEqual(listing["suggested_templates"][0]["template_id"], "gpu.vulkan.compute-buffer.v1")
        self.assertEqual(listing["filter_diagnostics"][0]["recognized_as"], ["profile_id"])

    def test_durable_run_rejects_legacy_browser_device_actions(self) -> None:
        """Legacy UI staging must not put a backend Run into waiting_for_ui."""

        router = PlatformRuntimeActionRouter(
            artifact_store=object(),  # type: ignore[arg-type]
            build_worker=object(),  # type: ignore[arg-type]
            device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
            default_source_connector=lambda: {"connector_id": "test", "snapshot": {}},
            bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
        )

        for action_id in LEGACY_BROWSER_DEVICE_ACTIONS:
            with self.subTest(action_id=action_id), self.assertRaises(PlatformError) as raised:
                router.validate(action_id, {}, {"project_id": "local-default", "context": {}})
            self.assertEqual(raised.exception.code, "ACTION_LEGACY_BROWSER_ONLY")

    def test_build_create_preserves_manifest_error_for_the_agent(self) -> None:
        router = PlatformRuntimeActionRouter(
            artifact_store=_BuildResultStore(),  # type: ignore[arg-type]
            build_worker=_FailedBuildWorker(),  # type: ignore[arg-type]
            device_gateway=_UnavailableLocalGateway(),  # type: ignore[arg-type]
            default_source_connector=lambda: {"connector_id": "test", "snapshot": {}},
            bridge_artifact_url=lambda job_id, token: f"/{job_id}?token={token}",
        )

        result = router.execute(
            "build.create",
            {"profile_id": "gpu_gles3", "source_artifact_id": "art_source"},
            {"run_id": "run_test", "project_id": "local-default", "context": {}},
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "BUILD_PROFILE_RUNTIME_UNAVAILABLE")
        self.assertEqual(result["diagnostic"]["stage"], "profile_runtime")
        self.assertEqual(result["error"]["details"]["missing_library_names"], ["libEGL.so"])

    def test_experiment_finding_is_captured_without_auto_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            evidence = self._write_shell_evidence(store)
            router = self._learning_router(store)
            run = {
                "run_id": "run_test",
                "project_id": "local-default",
                "mode": "device_validation",
                "goal": "验证 Vulkan compute",
                "context": {"analysis_record_id": "analysis-1", "device_validation_facts": {"run_artifact_id": evidence["artifact_id"]}},
                "checkpoint": {"completed_action_keys": {}},
            }
            action_input = self._experiment_finding_input(str(evidence["artifact_id"]))

            router.validate("platform.record_experiment_findings", action_input, run)
            result = router.execute("platform.record_experiment_findings", action_input, run)
            report = store.read("experiment_learning_report", str(result["experiment_learning_report_artifact_id"]))

            self.assertEqual(result["capture_status"], "captured_unreviewed")
            self.assertEqual(result["finding_count"], 1)
            self.assertFalse(report["promotion_contract"]["automatic_skill_or_kb_update"])
            self.assertEqual(report["findings"][0]["reproducibility"], "first_observation")
            self.assertIn("仅限本次设备", report["findings"][0]["applicability_scope"])
            self.assertEqual(report["findings"][0]["evidence_artifact_ids"], [evidence["artifact_id"]])

    def test_experiment_finding_rejects_artifact_not_observed_by_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            evidence = self._write_shell_evidence(store)
            router = self._learning_router(store)
            run = {
                "run_id": "run_test",
                "project_id": "local-default",
                "mode": "device_validation",
                "goal": "验证 Vulkan compute",
                "context": {"analysis_record_id": "analysis-1"},
                "checkpoint": {"completed_action_keys": {}},
            }

            with self.assertRaises(PlatformError) as raised:
                router.validate(
                    "platform.record_experiment_findings",
                    self._experiment_finding_input(str(evidence["artifact_id"])),
                    run,
                )

            self.assertEqual(raised.exception.code, "EXPERIMENT_FINDINGS_EVIDENCE_NOT_OBSERVED")


if __name__ == "__main__":
    unittest.main()
