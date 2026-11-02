from __future__ import annotations

import os
import asyncio
import json
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from starlette.requests import Request

from backend import app as platform_app
from backend.catalog import build_catalog
from backend.core import ArtifactStore, PlatformError, SourceConnectorManager, validate_git_url
from backend.htrace_analysis import (
    analyze_htrace,
    analyze_htrace_stage_windows,
    list_htrace_cluster_events,
    list_htrace_runtime_event_names,
    list_htrace_self_time_events,
    validate_htrace_header,
)
from backend.hitrace_instrumentation import describe_hitrace_event, load_hitrace_tag_catalog, locate_hitrace_instrumentation
from backend.trace_analysis import cluster_sys_trace, normalize_external_report
from backend.trace_modules import summarize_trace_modules


class ArtifactStoreTests(unittest.TestCase):
    def test_platform_version_exposes_build_and_mcp_contract(self) -> None:
        payload = platform_app.platform_version()

        self.assertEqual(payload["schema_version"], "platform_version/v1")
        self.assertEqual(payload["build"]["product"], "heterogeneous-enable-web")
        self.assertTrue(payload["build"]["build_id"])
        self.assertGreater(payload["contracts"]["mcp_tool_count"], 0)
        self.assertEqual(len(payload["contracts"]["mcp_tool_catalog_sha256"]), 64)
        self.assertIn(
            "reasoning_prepare_hardware_semantic_route",
            payload["contracts"]["required_reasoning_tools"],
        )

    def test_liveness_does_not_touch_runtime_registries(self) -> None:
        with (
            patch.object(platform_app, "ensure_catalog", side_effect=AssertionError("catalog access")),
            patch.object(platform_app.connectors, "list", side_effect=AssertionError("connector access")),
            patch.object(platform_app.store, "summary", side_effect=AssertionError("artifact scan")),
        ):
            payload = platform_app.liveness()

        self.assertEqual(payload["schema_version"], "service_liveness/v1")
        self.assertEqual(payload["status"], "ok")

    def test_health_does_not_require_a_default_source_connector(self) -> None:
        with patch.object(
            platform_app,
            "_default_connector",
            side_effect=PlatformError("SOURCE_SNAPSHOT_FAILED", "missing Git metadata"),
        ):
            payload = platform_app.health()

        self.assertEqual(payload["status"], "ok")
        self.assertIn("source_connector_count", payload)

    def test_overview_keeps_repository_registration_available_without_git_metadata(self) -> None:
        failure = PlatformError(
            "SOURCE_SNAPSHOT_FAILED",
            "Source root does not contain a readable Git snapshot.",
            details={"reason": "no Git repositories found below source root"},
        )
        with patch.object(platform_app, "_default_connector", side_effect=failure):
            payload = platform_app.overview()

        self.assertIsNone(payload["default_connector"])
        self.assertEqual(payload["default_connector_status"]["status"], "unavailable")
        self.assertEqual(payload["default_connector_status"]["error"]["code"], "SOURCE_SNAPSHOT_FAILED")

    def test_openharmony_sdk_provision_api_uses_only_registered_connector_id(self) -> None:
        expected = {"status": "ready", "selection": {"source_connector_id": "local_openharmony"}}
        with patch.object(
            platform_app.openharmony_sdk_assets,
            "provision_from_connector",
            return_value=expected,
        ) as provision:
            payload = platform_app.provision_openharmony_sdk_asset(
                platform_app.OpenHarmonySdkProvisionRequest(source_connector_id="local_openharmony")
            )

        self.assertEqual(payload, expected)
        provision.assert_called_once_with("local_openharmony")

    def test_model_relay_allows_a_configurable_large_agent_output(self) -> None:
        max_output_tokens = 32 * 1024
        budget = platform_app.PromptTokenBudgetRequest(
            contextWindowTokens=128 * 1024,
            inputTokens=2_317,
            outputReserveTokens=16_384,
            occupiedTokens=18_701,
            remainingTokens=112_371,
            utilization=18_701 / (128 * 1024),
            nearLimit=False,
            exceedsWindow=False,
            compactionApplied=False,
            compactedItems=0,
        )
        relay = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=max_output_tokens,
        )

        self.assertEqual(budget.outputReserveTokens, 16_384)
        self.assertEqual(relay.max_tokens, max_output_tokens)

    def test_model_relay_stream_limit_scales_with_output_token_budget(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=16 * 1024,
        )

        self.assertEqual(
            platform_app._model_relay_response_limit(request, stream=True),
            min(
                platform_app.MODEL_RELAY_MAX_RESPONSE_BYTES,
                16 * 1024 * platform_app.MODEL_RELAY_STREAM_BYTES_PER_OUTPUT_TOKEN,
            ),
        )
        self.assertGreater(
            platform_app._model_relay_response_limit(request, stream=True),
            2 * 1024 * 1024,
        )

    def test_model_relay_stream_accepts_transport_overhead_above_two_mib(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=16 * 1024,
        )

        class FakeResponse:
            headers = {"Content-Type": "text/event-stream"}

            def __iter__(self):
                return iter([
                    b":" + (b" transport-padding" * 128 * 1024) + b"\n\n",
                    b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ])

            def close(self) -> None:
                return None

        with patch.object(platform_app, "urlopen", return_value=FakeResponse()):
            response = platform_app._relay_chat_completion_stream(request)

            async def consume() -> bytes:
                chunks: list[bytes] = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
                return b"".join(chunks)

            body = asyncio.run(consume())

        self.assertGreater(len(body), 2 * 1024 * 1024)
        self.assertNotIn(b"MODEL_RELAY_RESPONSE_TOO_LARGE", body)
        self.assertIn(b"answer", body)

    def test_model_relay_converts_an_upstream_timeout_to_a_gateway_error(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=8,
        )
        with patch.object(platform_app, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(PlatformError) as raised:
                platform_app._relay_chat_completion(request)

        self.assertEqual(raised.exception.code, "MODEL_RELAY_UPSTREAM_TIMEOUT")
        self.assertEqual(raised.exception.status_code, 504)

    def test_model_relay_streams_openai_sse_without_persisting_the_key(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=8,
        )
        captured: dict[str, object] = {}

        class FakeResponse:
            headers = {"Content-Type": "text/event-stream"}

            def __iter__(self):
                return iter([
                    b'data: {"choices":[{"delta":{"reasoning_content":"reason"}}]}\\n\\n',
                    b'data: {"choices":[{"delta":{"content":"answer"}}]}\\n\\n',
                    b'data: [DONE]\\n\\n',
                ])

            def close(self) -> None:
                return None

        def fake_urlopen(upstream, *, timeout: int):
            captured["payload"] = json.loads(upstream.data.decode("utf-8"))
            captured["timeout"] = timeout
            captured["accept_language"] = upstream.get_header("Accept-language")
            return FakeResponse()

        with patch.object(platform_app, "urlopen", side_effect=fake_urlopen):
            response = platform_app._relay_chat_completion_stream(request)

            async def consume() -> bytes:
                chunks: list[bytes] = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
                return b"".join(chunks)

            body = asyncio.run(consume()).decode("utf-8")

        self.assertTrue(captured["payload"]["stream"])
        self.assertEqual(captured["payload"]["stream_options"], {"include_usage": True})
        self.assertEqual(captured["payload"]["model"], "kimi-for-coding")
        self.assertEqual(captured["timeout"], platform_app.MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS)
        self.assertEqual(captured["accept_language"], "zh-CN,zh;q=0.9")
        self.assertIn('"reasoning_content":"reason"', body)
        self.assertIn('"content":"answer"', body)

    def test_model_relay_stream_retries_without_optional_usage_when_upstream_rejects_it(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=8,
        )
        payloads: list[dict[str, object]] = []

        class FakeResponse:
            headers = {"Content-Type": "text/event-stream"}

            def __iter__(self):
                return iter([
                    b'data: {"choices":[{"delta":{"content":"answer"}}]}\\n\\n',
                    b'data: [DONE]\\n\\n',
                ])

            def close(self) -> None:
                return None

        unsupported = platform_app.HTTPError(
            "https://api.kimi.com/coding/v1/chat/completions",
            400,
            "Bad Request",
            {},
            BytesIO(b'{"error":{"message":"Unsupported parameter(s): `stream_options`"}}'),
        )
        self.addCleanup(unsupported.close)

        def fake_urlopen(upstream, *, timeout: int):
            del timeout
            payloads.append(json.loads(upstream.data.decode("utf-8")))
            if len(payloads) == 1:
                raise unsupported
            return FakeResponse()

        with patch.object(platform_app, "urlopen", side_effect=fake_urlopen):
            response = platform_app._relay_chat_completion_stream(request)

            async def consume() -> bytes:
                chunks: list[bytes] = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
                return b"".join(chunks)

            body = asyncio.run(consume()).decode("utf-8")

        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["stream_options"], {"include_usage": True})
        self.assertNotIn("stream_options", payloads[1])
        self.assertIn('"content":"answer"', body)

    def test_artifact_write_is_content_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="test-project")
            arguments = {
                "source_connector_id": "connector_test",
                "source_binding": {"declared_revision": "rev", "resolved_snapshot_id": "snap", "match_level": "exact", "resolver_version": "1.0"},
                "producer": {"name": "test", "version": "1.0"},
            }
            first = store.write("trace_analysis_result", {"value": 1}, **arguments)
            second = store.write("trace_analysis_result", {"value": 1}, **arguments)
            self.assertEqual(first["artifact_id"], second["artifact_id"])
            self.assertEqual(first["content_sha256"], second["content_sha256"])
            self.assertEqual(store.summary()["artifact_count"], 1)

    def test_artifact_id_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            with self.assertRaises(PlatformError) as raised:
                store.read("trace_analysis_result", "../outside")
            self.assertEqual(raised.exception.code, "ARTIFACT_NOT_FOUND")

    def test_dirty_local_snapshot_tracks_content_without_requiring_a_commit(self) -> None:
        """Changing a file while it remains `M` must invalidate the snapshot."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "source"
            repo.mkdir()
            for command in (
                ["git", "init", str(repo)],
                ["git", "-C", str(repo), "config", "user.email", "platform-test@example.invalid"],
                ["git", "-C", str(repo), "config", "user.name", "Platform Test"],
            ):
                subprocess.run(command, check=True, capture_output=True, text=True)
            source_file = repo / "worker.cpp"
            source_file.write_text("int work() { return 1; }\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "worker.cpp"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
            source_file.write_text("int work() { return 2; }\n", encoding="utf-8")

            state_root = root / "platform-state"
            store = ArtifactStore(state_root, project_id="local-default")
            manager = SourceConnectorManager(state_root, store, workspace_root=Path(__file__).resolve().parents[3])
            connector = manager.register_local(repo, display_name="dirty fixture")
            snapshot_id = str(connector["snapshot"]["snapshot_id"])
            fingerprint = str(connector["worktree_fingerprint"]["sha256"])

            self.assertFalse(connector["snapshot"]["clean"])
            self.assertEqual(connector["snapshot"]["schema_version"], "1.1")
            self.assertTrue(
                manager.verify_current_snapshot(
                    str(connector["connector_id"]),
                    snapshot_id,
                    expected_worktree_fingerprint_sha256=fingerprint,
                )["matches_expected"]
            )

            # Git still reports this file as modified, but the local source
            # identity must change before AST analysis is allowed to proceed.
            source_file.write_text("int work() { return 3; }\n", encoding="utf-8")
            stale = manager.verify_current_snapshot(
                str(connector["connector_id"]),
                snapshot_id,
                expected_worktree_fingerprint_sha256=fingerprint,
            )
            self.assertFalse(stale["matches_expected"])
            self.assertNotEqual(stale["current_snapshot_id"], snapshot_id)
            self.assertNotEqual(stale["current_worktree_fingerprint_sha256"], fingerprint)

            refreshed = manager.refresh_snapshot(str(connector["connector_id"]))
            self.assertFalse(refreshed["snapshot"]["clean"])
            self.assertEqual(refreshed["snapshot"]["snapshot_id"], stale["current_snapshot_id"])

    def test_event_catalog_is_persisted_as_a_deterministic_evidence_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="test-project")
            arguments = {
                "source_connector_id": "connector_test",
                "source_binding": {"declared_revision": "rev", "resolved_snapshot_id": "snap", "match_level": "exact", "resolver_version": "1.0"},
                "producer": {"name": "test", "version": "1.0"},
            }
            analysis = store.write(
                "trace_analysis_result",
                {"clusters": [{"cluster_id": "cluster-1", "time_range_ns": {"start_ns": 10, "end_ns": 20}}]},
                **arguments,
            )
            cluster = analysis["clusters"][0]
            catalog = {
                "events": [{"event_id": "event-1", "rank": 1, "event_name": "RenderFrame", "event_kind": "callstack_span", "metrics": {}, "trace_event_refs": [], "source_resolution": "raw_htrace_symbol_only"}],
                "quality": {"status": "partial", "profile_id": "test-v1", "limit": 24},
            }
            with patch.object(platform_app, "store", store):
                first = platform_app._write_trace_event_catalog(analysis, cluster, catalog)
                second = platform_app._write_trace_event_catalog(analysis, cluster, catalog)

            self.assertEqual(first["artifact_id"], second["artifact_id"])
            self.assertEqual(store.read("trace_event_catalog", first["artifact_id"])["events"][0]["event_name"], "RenderFrame")

    def test_trace_wide_hotspot_discovery_uses_a_non_cluster_catalog_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            arguments = {
                "source_connector_id": "connector_test",
                "source_binding": {"declared_revision": "rev", "resolved_snapshot_id": "snap", "match_level": "exact", "resolver_version": "1.0"},
                "producer": {"name": "test", "version": "1.0"},
            }
            analysis = store.write(
                "trace_analysis_result",
                {"input_kind": "raw_htrace", "raw_htrace": {"conversion": {"database": {"sha256": "d" * 64}}}},
                **arguments,
            )
            catalog = {
                "events": [{"event_id": "event-self-1", "rank": 1, "event_name": "RenderFrame", "event_kind": "callstack_span", "metrics": {}, "trace_event_refs": [], "source_resolution": "raw_htrace_symbol_only"}],
                "quality": {"status": "partial", "profile_id": "htrace-event-self-time-v2", "limit": 100},
            }
            with patch.object(platform_app, "store", store), patch.object(platform_app, "_enrich_hitrace_event_catalog", side_effect=lambda _, value: value), patch.object(platform_app, "list_htrace_self_time_events", return_value=catalog) as extractor:
                response = platform_app.list_trace_hotspot_discovery(
                    "local-default",
                    platform_app.TraceHotspotDiscoveryRequest(
                        analysis_artifact_id=analysis["artifact_id"],
                        discovery_id="event_self_time",
                        module_key="arkui_ui",
                    ),
                )

            payload = store.read("trace_event_catalog", response["artifact"]["artifact_id"])
            self.assertEqual(response["discovery"]["kind"], "trace_hotspot_discovery")
            self.assertEqual(response["discovery"]["module_key"], "arkui_ui")
            self.assertEqual(payload["catalog_scope"]["discovery_id"], "event_self_time")
            self.assertEqual(payload["catalog_scope"]["module_key"], "arkui_ui")
            self.assertEqual(extractor.call_args.kwargs["module_key"], "arkui_ui")
            self.assertNotIn("cluster_id", payload)

    def test_system_cluster_selection_requires_a_versioned_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            arguments = {
                "source_connector_id": "connector_test",
                "source_binding": {"declared_revision": "rev", "resolved_snapshot_id": "snap", "match_level": "exact", "resolver_version": "1.0"},
                "producer": {"name": "test", "version": "1.0"},
            }
            analysis = store.write(
                "trace_analysis_result",
                {"clusters": [{"cluster_id": "cluster-1", "time_range_ns": {"start_ns": 10, "end_ns": 20}}]},
                **arguments,
            )
            missing_policy = platform_app.ClusterSelectionRequest(
                analysis_artifact_id=analysis["artifact_id"],
                cluster_id="cluster-1",
                rationale="Deterministic automatic triage selected this window.",
                selection_actor="system",
            )
            with patch.object(platform_app, "store", store):
                with self.assertRaises(PlatformError) as raised:
                    platform_app.select_cluster("local-default", missing_policy)
                self.assertEqual(raised.exception.code, "AUTO_TRIAGE_POLICY_REQUIRED")

                selected = platform_app.select_cluster(
                    "local-default",
                    platform_app.ClusterSelectionRequest(
                        analysis_artifact_id=analysis["artifact_id"],
                        cluster_id="cluster-1",
                        rationale="Deterministic automatic triage selected this window.",
                        selection_actor="system",
                        automation_policy_id="trace-auto-triage",
                        automation_policy_version="1.0.0",
                        automation_input_hash="a" * 64,
                    ),
                )["artifact"]

            selected_cluster = selected["selected_cluster"]
            self.assertEqual(selected_cluster["selection_actor"], "system")
            self.assertEqual(selected_cluster["automation"]["policy_id"], "trace-auto-triage")


class SelectedEventOpportunityTests(unittest.TestCase):
    class _Connector:
        def verify_current_snapshot(self, _: str, __: str, *, expected_worktree_fingerprint_sha256: str | None = None) -> dict[str, bool]:
            return {"matches_expected": True}

    class _BindingConnector:
        worktree_fingerprint = {
            "algorithm": "git_dirty_worktree_content_v1",
            "sha256": "f" * 64,
            "dirty_repository_count": 0,
            "changed_path_count": 0,
        }

        def get(self, _: str) -> dict[str, object]:
            return {
                "kind": "local",
                "snapshot": {"snapshot_id": "snapshot_test", "clean": True},
                "worktree_fingerprint": self.worktree_fingerprint,
            }

        def resolve_binding(self, _: str, declared_revision: str | None) -> dict[str, object]:
            return {
                "declared_revision": declared_revision,
                "resolved_snapshot_id": "snapshot_test",
                "match_level": "exact" if declared_revision == "snapshot_test" else "mismatch",
                "resolver_version": "1.0",
            }

        def verify_current_snapshot(self, _: str, __: str, *, expected_worktree_fingerprint_sha256: str | None = None) -> dict[str, bool]:
            self.last_expected_worktree_fingerprint = expected_worktree_fingerprint_sha256
            return {"matches_expected": True}

    class _DirtyBindingConnector(_BindingConnector):
        worktree_fingerprint = {
            "algorithm": "git_dirty_worktree_content_v1",
            "sha256": "d" * 64,
            "dirty_repository_count": 1,
            "changed_path_count": 3,
        }

        def get(self, _: str) -> dict[str, object]:
            connector = super().get(_)
            connector["snapshot"] = {"snapshot_id": "snapshot_dirty", "clean": False}
            return connector

    def _write_inputs(self, store: ArtifactStore, *, resolution_state: str) -> tuple[dict[str, object], dict[str, object]]:
        arguments = {
            "source_connector_id": "connector_test",
            "source_binding": {"declared_revision": "rev", "resolved_snapshot_id": "snapshot_test", "match_level": "exact", "resolver_version": "1.0"},
            "producer": {"name": "test", "version": "1.0"},
        }
        analysis = store.write("trace_analysis_result", {"input_kind": "external_trace_analysis_report"}, **arguments)
        source_resolution = store.write(
            "trace_event_source_resolution",
            {
                "analysis_artifact_id": analysis["artifact_id"],
                "event": {
                    "event_id": "event_1",
                    "event_name": "RenderFrame",
                    "event_kind": "callstack_span",
                    "metrics": {"max_span_duration_ms": 12.0},
                    "trace_event_refs": [{"event_id": "callstack:1"}],
                },
                "resolution": {"state": resolution_state, "trace_binding_match_level": "exact"},
                "code_block": {"source": {"path": "render/frame.cpp", "line": 24}},
                "catalog_scope": {"kind": "trace_hotspot_discovery", "scope": "trace_wide"},
            },
            **arguments,
            input_artifacts=[f"artifact:{analysis['artifact_id']}"],
        )
        return analysis, source_resolution

    def test_selected_event_keeps_browsing_only_source_resolution_out_of_ast_and_kb(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            analysis, source_resolution = self._write_inputs(store, resolution_state="instrumentation_point_pattern")
            with patch.object(platform_app, "store", store), patch.object(platform_app, "connectors", self._Connector()):
                response = platform_app.identify_selected_event_opportunity(
                    "local-default",
                    platform_app.EventOpportunityAnalysisRequest(
                        analysis_artifact_id=str(analysis["artifact_id"]),
                        source_resolution_artifact_id=str(source_resolution["artifact_id"]),
                    ),
                )

        self.assertEqual(response["run"]["run_kind"], "manual_trace_event")
        self.assertEqual(response["report"]["state"], "partial")
        self.assertEqual(response["report"]["opportunities"][0]["state"], "source_location_required")

    def test_selected_event_runs_ast_features_then_kb_matching_for_declared_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            analysis, source_resolution = self._write_inputs(store, resolution_state="declared_source")
            feature = {"function": "Renderer::Draw", "signature": "void Renderer::Draw()", "loops": [{"kind": "for"}]}
            match = {
                "knowledge_base_pattern_id": "parallel_image_kernel",
                "confidence": "development",
                "knowledge_readiness": "development_only",
                "target_unit": "GPU",
                "alternative_units": ["CPU"],
                "source_layers": {"algorithm": "image"},
                "matched_features": ["for_loop"],
                "missing_features": [],
                "capability_constraints": {},
                "cost_model": {},
                "fallback_pairs": [],
                "recommendation": "Evaluate a GPU implementation.",
            }
            feature_payload = {
                "source": {"path": "render/frame.cpp", "line": 24, "sha256": "a" * 64},
                "feature_status": "available",
                "features": [feature],
                "ast_diagnostics": [],
                "structural_precheck": {},
                "kb_matches": [{"matches": [match]}],
            }
            binding = {"declared_revision": "rev", "resolved_snapshot_id": "snapshot_test", "match_level": "exact", "resolver_version": "1.0"}
            with (
                patch.object(platform_app, "store", store),
                patch.object(platform_app, "connectors", self._Connector()),
                patch.object(platform_app, "_build_source_inspection_payload", return_value=(feature_payload, binding)) as extractor,
                patch.object(platform_app, "_included_kb_matches", return_value=[(feature, match)]),
            ):
                response = platform_app.identify_selected_event_opportunity(
                    "local-default",
                    platform_app.EventOpportunityAnalysisRequest(
                        analysis_artifact_id=str(analysis["artifact_id"]),
                        source_resolution_artifact_id=str(source_resolution["artifact_id"]),
                    ),
                )

        self.assertEqual(extractor.call_args.args[1:], ("render/frame.cpp", 24))
        self.assertEqual(response["report"]["state"], "development_candidate")
        opportunity = response["report"]["opportunities"][0]
        self.assertEqual(opportunity["state"], "development_candidate")
        self.assertEqual(opportunity["knowledge_base_pattern_id"], "parallel_image_kernel")
        self.assertEqual(response["report"]["summary"]["ast_available_count"], 1)
        self.assertEqual(response["report"]["summary"]["kb_match_count"], 1)

    def test_tag_verified_exact_instrumentation_point_can_enter_ast_and_kb(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            analysis, source_resolution = self._write_inputs(store, resolution_state="instrumentation_point_exact")
            feature_payload = {
                "source": {"path": "render/frame.cpp", "line": 24, "sha256": "a" * 64},
                "feature_status": "available",
                "features": [],
                "ast_diagnostics": [],
                "structural_precheck": {},
                "kb_matches": [],
            }
            binding = {"declared_revision": "rev", "resolved_snapshot_id": "snapshot_test", "match_level": "exact", "resolver_version": "1.0"}
            with (
                patch.object(platform_app, "store", store),
                patch.object(platform_app, "connectors", self._Connector()),
                patch.object(platform_app, "_build_source_inspection_payload", return_value=(feature_payload, binding)) as extractor,
            ):
                response = platform_app.identify_selected_event_opportunity(
                    "local-default",
                    platform_app.EventOpportunityAnalysisRequest(
                        analysis_artifact_id=str(analysis["artifact_id"]),
                        source_resolution_artifact_id=str(source_resolution["artifact_id"]),
                    ),
                )

        self.assertEqual(extractor.call_args.args[1:], ("render/frame.cpp", 24))
        self.assertEqual(response["report"]["opportunities"][0]["state"], "no_kb_match")

    def test_source_snapshot_attestation_derives_an_exact_analysis_without_mutating_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            source_analysis = store.write(
                "trace_analysis_result",
                {"input_kind": "raw_htrace", "trace_name": "captured.htrace", "clusters": []},
                source_connector_id="connector_old",
                source_binding={"declared_revision": None, "resolved_snapshot_id": "snapshot_old", "match_level": "unknown", "resolver_version": "1.0"},
                producer={"name": "test", "version": "1.0"},
            )
            with patch.object(platform_app, "store", store), patch.object(platform_app, "connectors", self._BindingConnector()):
                response = platform_app.attest_trace_source_snapshot(
                    "local-default",
                    platform_app.TraceSourceSnapshotAttestationRequest(
                        analysis_artifact_id=str(source_analysis["artifact_id"]),
                        source_connector_id="connector_test",
                        source_snapshot_id="snapshot_test",
                        attestation_actor="user",
                        attestation_note="Device build matches the selected workspace snapshot.",
                    ),
                )

        rebound = response["artifact"]
        attestation = response["binding_artifact"]
        self.assertEqual(source_analysis["source_binding"]["match_level"], "unknown")
        self.assertEqual(rebound["source_binding"]["match_level"], "exact")
        self.assertEqual(rebound["source_connector_id"], "connector_test")
        self.assertEqual(rebound["source_binding_attestation"]["binding_artifact_id"], attestation["artifact_id"])
        self.assertIn(f"artifact:{source_analysis['artifact_id']}", rebound["input_artifacts"])

    def test_source_snapshot_attestation_accepts_dirty_worktree_with_content_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            source_analysis = store.write(
                "trace_analysis_result",
                {"input_kind": "raw_htrace", "trace_name": "captured.htrace", "clusters": []},
                source_connector_id="connector_old",
                source_binding={"declared_revision": None, "resolved_snapshot_id": "snapshot_old", "match_level": "unknown", "resolver_version": "1.0"},
                producer={"name": "test", "version": "1.0"},
            )
            connector = self._DirtyBindingConnector()
            with patch.object(platform_app, "store", store), patch.object(platform_app, "connectors", connector):
                response = platform_app.attest_trace_source_snapshot(
                    "local-default",
                    platform_app.TraceSourceSnapshotAttestationRequest(
                        analysis_artifact_id=str(source_analysis["artifact_id"]),
                        source_connector_id="connector_test",
                        source_snapshot_id="snapshot_dirty",
                        attestation_actor="user",
                        attestation_note="Device build was captured from the current developer worktree.",
                    ),
                )

        binding = response["artifact"]["source_binding"]
        self.assertEqual(binding["match_level"], "exact")
        self.assertEqual(binding["snapshot_state"], "dirty")
        self.assertEqual(binding["worktree_fingerprint_sha256"], "d" * 64)
        self.assertEqual(connector.last_expected_worktree_fingerprint, "d" * 64)

    def test_agent_snapshot_attestation_requires_recorded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            source_analysis = store.write(
                "trace_analysis_result",
                {"input_kind": "raw_htrace"},
                source_connector_id="connector_old",
                source_binding={"declared_revision": None, "resolved_snapshot_id": "snapshot_old", "match_level": "unknown", "resolver_version": "1.0"},
                producer={"name": "test", "version": "1.0"},
            )
            with patch.object(platform_app, "store", store), patch.object(platform_app, "connectors", self._BindingConnector()):
                with self.assertRaises(PlatformError) as raised:
                    platform_app.attest_trace_source_snapshot(
                        "local-default",
                        platform_app.TraceSourceSnapshotAttestationRequest(
                            analysis_artifact_id=str(source_analysis["artifact_id"]),
                            source_connector_id="connector_test",
                            source_snapshot_id="snapshot_test",
                            attestation_actor="agent",
                        ),
                    )

        self.assertEqual(raised.exception.code, "TRACE_SOURCE_ATTESTATION_EVIDENCE_REQUIRED")


class ContractTests(unittest.TestCase):
    def test_optimization_knowledge_base_contains_only_the_strict_user_space_corpus(self) -> None:
        payload = platform_app._optimization_kb()
        strict_cases = payload["strict_heterogeneous_cases"]

        self.assertEqual(payload["artifact_type"], "optimization_knowledge_base")
        self.assertEqual(payload["summary"]["record_count"], 57)
        self.assertEqual(payload["summary"]["strict_case_count"], 40)
        self.assertEqual(payload["summary"]["strict_case_source_groups"], {
            "v21_user_space": 17,
            "v37_audio_dsp": 1,
            "v38_mindspore": 22,
        })
        self.assertEqual(len(strict_cases), 40)
        self.assertFalse(any(str(case.get("module", "")).startswith("kernel_") for case in strict_cases))
        self.assertTrue(all(case.get("cpu_fallback_ast_profile") for case in strict_cases))

    def test_strict_cases_expose_evidence_bound_cpu_feature_summaries(self) -> None:
        payload = platform_app._optimization_kb()
        strict_cases = payload["strict_heterogeneous_cases"]
        summaries = [case.get("cpu_code_feature_summary") for case in strict_cases]

        self.assertEqual(len(summaries), 40)
        self.assertTrue(all(isinstance(summary, dict) for summary in summaries))
        self.assertTrue(all(summary["schema_version"] == "cpu_code_feature_summary/v1" for summary in summaries))
        self.assertTrue(all(summary["availability"] in {"ast_available", "ast_unavailable"} for summary in summaries))

        astc = next(case for case in strict_cases if case["case_id"] == "astc_opencl_encode_to_cpu_software_fallback")
        astc_features = astc["cpu_code_feature_summary"]["structured_features"]
        self.assertEqual(astc["cpu_code_feature_summary"]["availability"], "ast_available")
        self.assertIn("uint8_t", astc_features["data_type_evidence"])
        self.assertIn("astcenc_compress_image", astc_features["computation"]["notable_callees"])
        self.assertEqual(astc_features["memory_access"]["continuity"], "not_proven_by_current_ast")
        self.assertIn("compute-core", astc["cpu_code_feature_summary"]["summary"])

        audio = next(case for case in strict_cases if case["case_id"] == "audio_effect_dsp_hdi_to_ap_runtime_chain_fallback")
        self.assertEqual(audio["cpu_code_feature_summary"]["availability"], "ast_unavailable")
        self.assertEqual(audio["cpu_code_feature_summary"]["structured_features"]["memory_access"]["classification"], "not_available")
        self.assertIn("不能从该案例推断", audio["cpu_code_feature_summary"]["summary"])

    def test_strict_case_detail_keeps_cpu_source_bound_to_the_profile_snapshot(self) -> None:
        astc = platform_app.optimization_kb_strict_case_detail("astc_opencl_encode_to_cpu_software_fallback")
        audio = platform_app.optimization_kb_strict_case_detail("audio_effect_dsp_hdi_to_ap_runtime_chain_fallback")

        self.assertEqual(astc["case"]["case_id"], "astc_opencl_encode_to_cpu_software_fallback")
        self.assertIn(astc["cpu_source"]["state"], {"available", "source_changed", "too_large", "not_available"})
        if astc["cpu_source"]["state"] == "available":
            self.assertTrue(astc["cpu_source"]["excerpt"])
            self.assertIn("AstcSoftwareEncodeCore", "\n".join(line["text"] for line in astc["cpu_source"]["excerpt"]))
        self.assertEqual(audio["cpu_source"]["state"], "not_available")
        self.assertIn("不能展示或猜测", audio["cpu_source"]["reason"])

    def test_reasoning_corpus_keeps_cpu_features_for_strict_case_matching(self) -> None:
        optimization = platform_app._optimization_kb()
        hardware = platform_app._hardware_kb()
        records = platform_app._optimization_reasoning_records()
        astc_record = next(record for record in records if record["record_id"] == "astc_opencl_encode_to_cpu_software_fallback")

        self.assertIn("cpu_code_features", astc_record)
        self.assertIn("astcenc_compress_image", astc_record["cpu_code_features"]["structured_features"]["computation"]["notable_callees"])

        context = platform_app.prepare_context(
            "kb_rag",
            platform_app.pasted_code_unit(
                "bool AstcSoftwareEncodeCore(uint8_t *pixels) { return astcenc_compress_image(); }",
                "AstcSoftwareEncodeCore",
                "AstcSoftwareEncodeCore",
            ),
            records,
            [],
            [],
            optimization_kb_metadata={
                "kb_id": str(optimization["kb_id"]),
                "version": str(optimization["kb_version"]),
                "content_sha256": str(optimization["content_sha256"]),
            },
            hardware_kb_metadata={
                "kb_id": str(hardware["kb_id"]),
                "version": str(hardware["kb_version"]),
                "content_sha256": str(hardware["content_sha256"]),
            },
        )
        astc_context = next(item for item in context["context_records"] if item["evidence_id"] == "kb:astc_opencl_encode_to_cpu_software_fallback")
        self.assertEqual(astc_context["record"]["cpu_code_features"]["availability"], "ast_available")

    def test_reasoning_transcript_is_immutable_and_redacts_secret_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), project_id="local-default")
            artifact_arguments = {
                "source_connector_id": "connector_test",
                "source_binding": {"declared_revision": None, "resolved_snapshot_id": "snapshot_test", "match_level": "exact", "resolver_version": "1.0"},
                "producer": {"name": "test", "version": "1.0"},
            }
            profile = platform_app._default_target_platform_profile()
            context = store.write(
                "reasoning_context_pack",
                {
                    "strategy_id": "direct_baseline",
                    "context_sha256": "c" * 64,
                    "target_platform_profile": profile,
                    "code": "void Hot() {}",
                    "context_records": [],
                },
                **artifact_arguments,
            )
            result = store.write(
                "opportunity_reasoning_result",
                {
                    "strategy_id": "direct_baseline",
                    "context_artifact_id": context["artifact_id"],
                    "context_sha256": context["context_sha256"],
                    "result": {"recommendation": "evidence_required"},
                },
                **artifact_arguments,
            )
            request = platform_app.ReasoningTranscriptRequest(
                analysis_id="analysis_test",
                inference_id="inference_test",
                run_id="run_test",
                strategy_id="direct_baseline",
                strategy_label="直接代码推理",
                context_artifact_id=str(context["artifact_id"]),
                result_artifact_id=str(result["artifact_id"]),
                model_provider="browser_configured_local_relay",
                model_name="kimi-for-coding",
                system_prompt="系统约束",
                user_prompt="用户上下文",
                raw_output='{"recommendation":"evidence_required"}',
                model_usage={
                    "inputTokens": 1250,
                    "outputTokens": 320,
                    "totalTokens": 1570,
                    "inputSource": "provider",
                    "outputSource": "estimated",
                },
                token_budget={
                    "contextWindowTokens": 131072,
                    "inputTokens": 1250,
                    "outputReserveTokens": 4096,
                    "occupiedTokens": 5346,
                    "remainingTokens": 125726,
                    "utilization": 0.0408,
                    "nearLimit": False,
                    "exceedsWindow": False,
                    "compactionApplied": False,
                    "compactedItems": 0,
                },
                outcome={"status": "succeeded", "api_key": "must-not-be-persisted"},
                timeline=[{"stage": "context", "status": "succeeded", "title": "上下文已准备", "detail": "ok", "timestamp": "2026-01-01T00:00:00Z"}],
            )
            with patch.object(platform_app, "store", store):
                saved = platform_app.save_reasoning_transcript("local-default", request)["artifact"]
                loaded = platform_app.get_reasoning_transcript("local-default", str(saved["artifact_id"]))["artifact"]

            self.assertEqual(loaded["context_artifact_id"], context["artifact_id"])
            self.assertEqual(loaded["result_artifact_id"], result["artifact_id"])
            self.assertEqual(loaded["target_platform_profile"], profile)
            self.assertEqual(loaded["outcome"]["api_key"], "[redacted]")
            self.assertEqual(loaded["prompt"]["system"], "系统约束")
            self.assertEqual(loaded["model_usage"]["inputTokens"], 1250)
            self.assertEqual(loaded["token_budget"]["contextWindowTokens"], 131072)

    def test_target_platform_profile_requires_model_and_system_version(self) -> None:
        profile = platform_app._default_target_platform_profile()
        profile["chipset"].pop("model_id")
        with self.assertRaises(ValueError):
            platform_app.TargetPlatformProfileRequest.model_validate(profile)

    def test_hardware_knowledge_base_uses_explicit_device_evidence_gates(self) -> None:
        optimization = platform_app._optimization_kb()
        hardware = platform_app._hardware_kb()
        records = platform_app._hardware_kb_records()
        strict_units = {case["target_unit"] for case in optimization["strict_heterogeneous_cases"]}
        hardware_units = {record["record_id"] for record in records}

        self.assertEqual(hardware["artifact_type"], "hardware_knowledge_base")
        self.assertEqual(hardware["summary"]["record_count"], 22)
        self.assertEqual(hardware["summary"]["strict_integration_unit_count"], 11)
        self.assertEqual(hardware["summary"]["strict_case_count"], 40)
        self.assertTrue(strict_units.issubset(hardware_units))
        self.assertTrue(all(record["record"].get("device_availability") == "evidence_required" for record in records))

    def test_separate_kb_searches_do_not_cross_return_records(self) -> None:
        optimization = platform_app._search_kb("Kirin NPU", include_unverified=False, include_cases=False, knowledge_base="optimization")
        hardware = platform_app._search_kb("Kirin NPU", include_unverified=False, include_cases=False, knowledge_base="hardware")

        self.assertEqual(optimization["knowledge_base"], "optimization")
        self.assertEqual(hardware["knowledge_base"], "hardware")
        self.assertTrue(all(record["record_type"] != "hardware_unit" for record in optimization["records"]))
        self.assertTrue(all(record["record_type"] in {"hardware_unit", "source_capability_record"} for record in hardware["records"]))

    def test_hardware_reasoning_context_uses_real_hardware_evidence_ids(self) -> None:
        code_unit = platform_app.pasted_code_unit(
            "void KirinNpuHotspot() { /* NPU candidate workload */ }",
            "KirinNpuHotspot",
            "Kirin NPU hotspot",
        )
        optimization = platform_app._optimization_kb()
        hardware = platform_app._hardware_kb()
        context = platform_app.prepare_context(
            "hardware_kb",
            code_unit,
            platform_app._optimization_reasoning_records(),
            [],
            platform_app._hardware_reasoning_records(),
            optimization_kb_metadata={
                "kb_id": str(optimization["kb_id"]),
                "version": str(optimization["kb_version"]),
                "content_sha256": str(optimization["content_sha256"]),
            },
            hardware_kb_metadata={
                "kb_id": str(hardware["kb_id"]),
                "version": str(hardware["kb_version"]),
                "content_sha256": str(hardware["content_sha256"]),
            },
            allow_legacy_static_baseline=True,
        )

        evidence_ids = [record["evidence_id"] for record in context["context_records"]]
        self.assertTrue(evidence_ids)
        # This test explicitly exercises the retained historical baseline,
        # rather than the production model-first Hardware-KB path.
        self.assertEqual(evidence_ids, ["hardware:KIRIN_NPU_NNRT"])
        self.assertEqual(context["hardware_routing"]["selected_units"][0]["unit_id"], "npu")
        self.assertFalse(any(evidence_id.endswith(":None") for evidence_id in evidence_ids))
        self.assertEqual(len(evidence_ids), len(set(evidence_ids)))

    def test_candidate_catalog_has_exactly_thirty_unpromoted_cases(self) -> None:
        workspace_root = Path(__file__).resolve().parents[3]
        catalog = build_catalog(workspace_root)
        self.assertEqual(catalog["case_count"], 30)
        self.assertEqual(len(catalog["cases"]), 30)
        self.assertTrue(all(case["lifecycle"] == "candidate" for case in catalog["cases"]))
        self.assertTrue(all(case["promotion_links"] == [] for case in catalog["cases"]))
        self.assertTrue(all(case["source_evidence"][0]["sha256"] for case in catalog["cases"]))
        self.assertTrue(all(case["source_evidence"][0]["path"].startswith("openharmony/") for case in catalog["cases"]))

    def test_git_url_contract_rejects_local_forms(self) -> None:
        self.assertEqual(validate_git_url("https://github.com/example/repository.git"), "https://github.com/example/repository.git")
        self.assertEqual(validate_git_url("ssh://git@example.com/org/repository.git"), "ssh://git@example.com/org/repository.git")
        for invalid in ("file:///tmp/repository", "/tmp/repository", "git@example.com:org/repository.git"):
            with self.assertRaises(PlatformError) as raised:
                validate_git_url(invalid)
            self.assertEqual(raised.exception.code, "GIT_URL_REJECTED")

    def test_external_report_separates_per_invocation_pressure(self) -> None:
        result = normalize_external_report(
            {
                "hotspots": [
                    {"rank": 1, "module": "IPC", "function": "reply", "total_duration_ms": 10000, "avg_duration_ms": 0.2, "max_duration_ms": 0.5, "call_count": 50000},
                    {"rank": 2, "module": "Render", "function": "RenderFrame", "total_duration_ms": 50, "avg_duration_ms": 10, "max_duration_ms": 20, "call_count": 5},
                ]
            },
            trace_name="sample.json",
            trace_sha256="a" * 64,
        )
        candidates = {item["function_ref"]["symbol"]: item for item in result["hotspot_candidates"]}
        self.assertEqual(candidates["reply"]["temporal_pressure"], "low")
        self.assertEqual(candidates["RenderFrame"]["temporal_pressure"], "high")

    def test_typed_external_report_preserves_exact_source_inputs(self) -> None:
        result = normalize_external_report(
            {
                "trace_id": "typed-contract-fixture",
                "source_revision": "source_snapshot_fixture",
                "hotspots": [
                    {
                        "hotspot_id": "upstream-hotspot-1",
                        "symbol": "CpuGaussianBlur",
                        "module": "ImagePipeline",
                        "file": "docs_ffrt/tools/test_blur.cpp",
                        "line": 5,
                        "metrics": {"wall_time_ms": 12.0, "call_count": 3},
                        "critical_path": {"is_critical": True, "path_id": "frame-1"},
                        "cluster_id": "cluster_typed_fixture",
                    }
                ],
            },
            trace_name="typed-contract.json",
            trace_sha256="b" * 64,
        )
        candidate = result["hotspot_candidates"][0]
        self.assertEqual(candidate["function_ref"]["file"], "docs_ffrt/tools/test_blur.cpp")
        self.assertEqual(candidate["function_ref"]["line"], 5)
        self.assertEqual(candidate["function_ref"]["resolution"], "source_location_declared")
        self.assertNotIn("unresolved_reason", candidate)
        self.assertTrue(candidate["critical_path_metrics"]["available"])
        self.assertTrue(candidate["critical_path_metrics"]["is_critical"])
        self.assertTrue(any("source revision declaration" in warning for warning in result["quality"]["warnings"]))

    def test_stage_flow_report_keeps_trace_anchors_separate_from_cpu_hotness(self) -> None:
        repeated = {
            "name": "H:HIAI_HCL_ModelManager_RunV3|I38",
            "ts_ms": 323930.357188,
            "dur_ms": 18.069844,
            "process_name": "hiaiserver",
            "pid": 1104,
            "thread_name": "hiaiserver",
            "tid": 16965,
        }
        result = normalize_external_report(
            {
                "summary": {"total_stages": 2},
                "stages": [
                    {
                        "id": "input_event_dispatch",
                        "name": "Input Event Dispatch",
                        "order": 1,
                        "start_ns": 100,
                        "end_ns": None,
                        "duration_ms": None,
                        "event_count": 0,
                        "total_event_duration_ms": 0,
                        "start_event": None,
                        "end_event": None,
                        "top_events": [],
                    },
                    {
                        "id": "decode",
                        "name": "Decode",
                        "order": 11,
                        "start_ns": 323930357188,
                        "end_ns": 324556019844,
                        "duration_ms": 625.662656,
                        "event_count": 37,
                        "total_event_duration_ms": 616.845781,
                        "start_event": repeated,
                        "end_event": {**repeated, "ts_ms": 324539.811094, "dur_ms": 16.20875},
                        # Duplicate representative rows must not become two calls.
                        "top_events": [repeated, dict(repeated)],
                    },
                ],
            },
            trace_name="stage-flow.json",
            trace_sha256="c" * 64,
        )

        self.assertEqual(result["analysis_profile"], "external-stage-flow-v1")
        self.assertEqual(len(result["stage_catalog"]), 2)
        self.assertEqual(len(result["clusters"]), 2)
        self.assertEqual(len(result["hotspot_candidates"]), 1)
        candidate = result["hotspot_candidates"][0]
        self.assertEqual(candidate["trace_event_name"], "H:HIAI_HCL_ModelManager_RunV3|I38")
        self.assertEqual(candidate["function_ref"]["symbol"], "HIAI_HCL_ModelManager_RunV3")
        self.assertEqual(candidate["function_ref"]["module"], "hiaiserver")
        self.assertEqual(candidate["active_time_metrics"]["call_count"], 1)
        self.assertEqual(candidate["active_time_metrics"]["reported_stage_event_count"], 37)
        self.assertEqual(candidate["active_time_metrics"]["aggregation_scope"], "stage_report_listed_top_events")
        self.assertEqual(candidate["stage_context"]["duration_ms"], 625.662656)
        self.assertFalse(candidate["measurement_semantics"]["cpu_self_time_available"])
        self.assertFalse(candidate["measurement_semantics"]["callee_attribution_available"])
        self.assertTrue(any("not attributed" in warning for warning in result["quality"]["warnings"]))

    def test_stage_flow_report_preserves_supplied_aggregate_event_metrics(self) -> None:
        result = normalize_external_report(
            {
                "stages": [
                    {
                        "id": "rotate",
                        "name": "Rotate",
                        "event_count": 51,
                        "top_events": [
                            {
                                "name": "H:ImageEffect::Render|I34",
                                "ts_ms": 10.0,
                                "dur_ms": 7.929,
                                "process_name": "photos",
                                "metrics": {
                                    "occurrence_count": 51,
                                    "self_time_estimate_total_duration_ms": 151.689,
                                    "inclusive_total_duration_ms": 151.689,
                                    "max_self_time_estimate_duration_ms": 7.929,
                                    "max_span_duration_ms": 7.929,
                                },
                            }
                        ],
                    }
                ]
            },
            trace_name="aggregate-stage.json",
            trace_sha256="a" * 64,
        )

        metrics = result["hotspot_candidates"][0]["active_time_metrics"]
        self.assertEqual(metrics["call_count"], 51)
        self.assertEqual(metrics["listed_occurrence_count"], 51)
        self.assertEqual(metrics["total_duration_ms"], 151.689)
        self.assertEqual(metrics["self_time_estimate_total_duration_ms"], 151.689)
        self.assertTrue(metrics["aggregate_metrics_supplied"])

    def test_stage_flow_report_accepts_nested_camel_case_collections(self) -> None:
        result = normalize_external_report(
            {
                "result": {
                    "traceId": "production-shape",
                    "stageList": {
                        "decode": {
                            "stageName": "Decode",
                            "sequence": 4,
                            "startNs": 1_000_000,
                            "endNs": 21_000_000,
                            "eventCount": 2,
                            "totalEventDurationMs": 12.0,
                            "topEvents": {
                                "items": [
                                    {
                                        "eventName": "H:Runtime::Execute|I38",
                                        "timestampNs": 2_000_000,
                                        "durationNs": 8_000_000,
                                        "processName": "runtime_service",
                                        "threadId": 42,
                                    }
                                ]
                            },
                        }
                    },
                }
            },
            trace_name="production.json",
            trace_sha256="d" * 64,
        )

        self.assertEqual(result["trace_id"], "production-shape")
        self.assertEqual(result["quality"]["normalization"]["stage_container_path"], "$.result.stageList")
        self.assertEqual(result["quality"]["normalization"]["event_container_aliases"], ["topEvents"])
        candidate = result["hotspot_candidates"][0]
        self.assertEqual(candidate["candidate_role"], "reported_hot_event")
        self.assertEqual(candidate["function_ref"]["symbol"], "Runtime::Execute")
        self.assertEqual(candidate["function_ref"]["module"], "runtime_service")
        self.assertEqual(candidate["active_time_metrics"]["max_duration_ms"], 8.0)
        self.assertEqual(candidate["stage_context"]["duration_ms"], 20.0)

    def test_stage_flow_schema_detection_is_independent_of_trace_values(self) -> None:
        def report(stage_id: str, stage_name: str, event_name: str, process_name: str) -> dict[str, object]:
            return {
                # Empty or unrelated same-name collections must not shadow the
                # structurally complete stage report below.
                "stages": [],
                "hotspots": [{"name": "transport-diagnostic-only"}],
                "vendorEnvelope": {
                    "job": {
                        "opaquePayload": {
                            "stages": [
                                {
                                    "id": stage_id,
                                    "name": stage_name,
                                    "order": 7,
                                    "start_ns": 1_000_000,
                                    "end_ns": 9_000_000,
                                    "event_count": 1,
                                    "top_events": [
                                        {
                                            "name": event_name,
                                            "ts_ms": 1.0,
                                            "dur_ms": 3.5,
                                            "process_name": process_name,
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                },
            }

        first = normalize_external_report(
            report("alpha", "No Domain Meaning", "opaque-event-001", "opaque-process-a"),
            trace_name="first.json",
            trace_sha256="1" * 64,
        )
        second = normalize_external_report(
            report("totally-different", "Changed Label", "unrelated-value-999", "different-process"),
            trace_name="second.json",
            trace_sha256="2" * 64,
        )

        for result in (first, second):
            normalization = result["quality"]["normalization"]
            self.assertEqual(result["analysis_profile"], "external-stage-flow-v1")
            self.assertEqual(normalization["stage_container_path"], "$.vendorEnvelope.job.opaquePayload.stages")
            self.assertEqual(normalization["input_stage_count"], 1)
            self.assertEqual(normalization["reported_hot_event_count"], 1)
            self.assertEqual(normalization["recognition_method"], "json-field-structure/v1")
            self.assertTrue(normalization["value_independent_schema_detection"])
            self.assertEqual(len(result["hotspot_candidates"]), 1)
            self.assertEqual(result["hotspot_candidates"][0]["candidate_role"], "reported_hot_event")

        self.assertEqual(first["hotspot_candidates"][0]["trace_event_name"], "opaque-event-001")
        self.assertEqual(second["hotspot_candidates"][0]["trace_event_name"], "unrelated-value-999")

    def test_stage_flow_merges_nonempty_event_alias_after_empty_alias(self) -> None:
        result = normalize_external_report(
            {
                "stages": [
                    {
                        "id": "stage-with-aliases",
                        "top_events": [],
                        "hotEvents": [
                            {
                                "eventName": "arbitrary-event-value",
                                "durationMs": 6.25,
                                "processName": "arbitrary-module-value",
                            }
                        ],
                    }
                ]
            },
            trace_name="event-aliases.json",
            trace_sha256="3" * 64,
        )

        self.assertEqual(result["quality"]["normalization"]["event_container_aliases"], ["hotEvents", "top_events"])
        self.assertEqual(result["quality"]["normalization"]["reported_hot_event_count"], 1)
        self.assertEqual(result["hotspot_candidates"][0]["trace_event_name"], "arbitrary-event-value")
        self.assertEqual(result["hotspot_candidates"][0]["active_time_metrics"]["max_duration_ms"], 6.25)

    def test_stage_flow_report_exposes_boundary_as_unmeasured_investigation_anchor(self) -> None:
        result = normalize_external_report(
            {
                "analysis": {
                    "stages": [
                        {
                            "stageId": "layout",
                            "stageName": "Layout",
                            "startEvent": "H:LayoutTask::Run|I38",
                            "endEvent": "H:LayoutTask::Run|I38",
                        }
                    ]
                }
            },
            trace_name="anchor-only.json",
            trace_sha256="e" * 64,
        )

        self.assertEqual(len(result["hotspot_candidates"]), 1)
        candidate = result["hotspot_candidates"][0]
        self.assertEqual(candidate["candidate_role"], "trace_anchor_only")
        self.assertEqual(candidate["function_ref"]["symbol"], "LayoutTask::Run")
        self.assertEqual(candidate["active_time_metrics"]["total_duration_ms"], 0.0)
        self.assertEqual(candidate["active_time_metrics"]["aggregation_scope"], "stage_boundary_anchor_fallback")
        self.assertEqual(candidate["measurement_semantics"]["duration_kind"], "unmeasured_trace_anchor")
        self.assertFalse(candidate["measurement_semantics"]["cpu_self_time_available"])
        self.assertEqual(result["quality"]["normalization"]["boundary_anchor_fallback_count"], 1)

    def test_sys_spike_is_explicitly_experimental(self) -> None:
        sample = """ taskA-1    (      1) [001] .... 1.000000: sched_wakeup: comm=taskB pid=2 prio=120 target_cpu=001\n taskB-2    (      1) [001] .... 1.001000: sched_switch: prev_comm=idle prev_pid=0 ==> next_comm=taskB next_pid=2\n"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.sys"
            path.write_text(sample, encoding="utf-8")
            result = cluster_sys_trace(path)
        self.assertTrue(result["experimental"])
        self.assertEqual(result["quality"]["supported_event_counts"]["sched_wakeup"], 1)
        self.assertEqual(len(result["clusters"]), 1)
        self.assertTrue(result["clusters"][0]["experimental"])

    def test_sys_spike_marks_mixed_runtime_and_render_chain_visible(self) -> None:
        sample = """ OS_FFRT_0-1    (      1) [001] .... 1.000000: sched_wakeup: comm=RenderService pid=2 prio=120 target_cpu=001\n"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.sys"
            path.write_text(sample, encoding="utf-8")
            result = cluster_sys_trace(path)
        summary = result["clusters"][0]["module_summary"]
        self.assertIn("FFRT runtime", summary["module_names"])
        self.assertIn("Render / Rosen", summary["module_names"])
        self.assertFalse(summary["system_module_only"])
        self.assertEqual(summary["top_function_name"], "RenderService")

    def test_trace_module_summary_keeps_pure_system_chain_collapsible(self) -> None:
        summary = summarize_trace_modules(["OS_FFRT_2_20072", "OS_IPC_0_5893", "kworker/u25:3"])
        self.assertEqual(summary["module_names"], ["FFRT runtime", "Binder / IPC", "Kernel scheduler"])
        self.assertTrue(summary["system_module_only"])


class RawHtraceAnalysisTests(unittest.TestCase):
    def test_invalid_htrace_header_is_rejected_before_converter_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-trace.htrace"
            path.write_bytes(b"not-an-ohosprof-trace")
            with self.assertRaises(PlatformError) as raised:
                validate_htrace_header(path)
            self.assertEqual(raised.exception.code, "HTRACE_HEADER_INVALID")

    def test_textual_ftrace_header_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "captured.htrace"
            path.write_text(
                "# tracer: nop\n task-1 ( 1) [000] .... 1.000000: tracing_mark_write: B|1|Stage\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_htrace_header(path), "ftrace_text")
            database = root / "converted.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE trace_range(start_ts INTEGER, end_ts INTEGER);
                    CREATE TABLE callstack(id INTEGER, ts INTEGER, dur INTEGER, callid INTEGER, name TEXT);
                    INSERT INTO trace_range VALUES (1000000000, 1001000000);
                    INSERT INTO callstack VALUES (1, 1000000000, 1000000, 7, 'Stage');
                    """
                )
            converter = root / "fake-trace-streamer"
            converter.write_text("#!/bin/sh\ncp \"$HETERO_TEST_TRACE_DB\" \"$3\"\n", encoding="utf-8")
            converter.chmod(converter.stat().st_mode | stat.S_IXUSR)
            with patch.dict(
                os.environ,
                {"HETERO_TRACE_STREAMER": str(converter), "HETERO_TEST_TRACE_DB": str(database)},
                clear=False,
            ):
                normalized = analyze_htrace(path, state_root=root / "state", workspace_root=root)["normalized"]
            self.assertEqual(normalized["raw_htrace"]["format"], "ftrace_text")
            self.assertNotIn("header_magic", normalized["raw_htrace"])

    def test_raw_htrace_adapter_degrades_when_optional_sqlite_fields_are_absent(self) -> None:
        """A minimal TraceStreamer-like DB must not require parent/thread tables."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            htrace = root / "fixture.htrace"
            htrace.write_bytes(b"OHOSPROF" + b"\0" * 128)
            database = root / "converted.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE meta(name TEXT, value TEXT);
                    CREATE TABLE trace_range(start_ts INTEGER, end_ts INTEGER);
                    CREATE TABLE callstack(id INTEGER, ts INTEGER, dur INTEGER, callid INTEGER, name TEXT);
                    INSERT INTO meta VALUES ('tool_version', 'test-converter-1.0');
                    INSERT INTO meta VALUES ('source_type', 'proto-based-trace');
                    INSERT INTO trace_range VALUES (1000000000, 11000000000);
                    INSERT INTO callstack VALUES (1, 2000000000, 20000000, 7, 'RenderFrame');
                    INSERT INTO callstack VALUES (2, 4000000000, 1000000, 7, 'RenderFrame');
                    """
                )

            converter = root / "fake-trace-streamer"
            converter.write_text("#!/bin/sh\ncp \"$HETERO_TEST_TRACE_DB\" \"$3\"\n", encoding="utf-8")
            converter.chmod(converter.stat().st_mode | stat.S_IXUSR)
            environment = {
                "HETERO_TRACE_STREAMER": str(converter),
                "HETERO_TEST_TRACE_DB": str(database),
            }
            with patch.dict(os.environ, environment, clear=False):
                first = analyze_htrace(htrace, state_root=root / "state", workspace_root=root)
                second = analyze_htrace(htrace, state_root=root / "state", workspace_root=root)

            result = first["normalized"]
            self.assertEqual(first, second)
            self.assertEqual(result["input_kind"], "raw_htrace")
            self.assertEqual(result["analysis_profile"], "htrace-basic-span-v1")
            self.assertEqual(len(result["clusters"]), 1)
            self.assertFalse(result["quality"]["capabilities"]["callstack_parent_links"]["available"])
            candidate = result["hotspot_candidates"][0]
            self.assertEqual(candidate["function_ref"]["resolution"], "raw_htrace_symbol_only")
            self.assertNotIn("file", candidate["function_ref"])
            self.assertFalse(candidate["critical_path_metrics"]["available"])
            self.assertIsNone(candidate["active_time_metrics"]["exclusive_estimate_total_duration_ms"])
            self.assertTrue(any("parent links" in warning for warning in result["quality"]["warnings"]))
            self_time_catalog = list_htrace_self_time_events(result["raw_htrace"], state_root=root / "state")
            self.assertEqual(self_time_catalog["quality"]["status"], "unavailable")
            self.assertIn("parent_id", self_time_catalog["quality"]["missing_columns"])

    def test_runtime_event_catalog_keeps_low_cost_boundary_names_without_parent_links(self) -> None:
        """Stage anchors are discovered independently from self-time ranking."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            htrace = root / "fixture.htrace"
            htrace.write_bytes(b"OHOSPROF" + b"\0" * 128)
            database = root / "converted.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE trace_range(start_ts INTEGER, end_ts INTEGER);
                    CREATE TABLE process(id INTEGER, ipid INTEGER, pid INTEGER, name TEXT);
                    CREATE TABLE thread(id INTEGER, itid INTEGER, tid INTEGER, name TEXT, ipid INTEGER);
                    CREATE TABLE callstack(id INTEGER, ts INTEGER, dur INTEGER, callid INTEGER, name TEXT);
                    INSERT INTO trace_range VALUES (1000000000, 2000000000);
                    INSERT INTO process VALUES (1, 1, 100, 'com.example.camera');
                    INSERT INTO process VALUES (2, 2, 200, 'camera.service');
                    INSERT INTO thread VALUES (1, 7, 701, 'main', 1);
                    INSERT INTO thread VALUES (2, 8, 801, 'worker', 2);
                    INSERT INTO callstack VALUES (1, 1000000000, 1000, 7, 'CameraStart');
                    INSERT INTO callstack VALUES (2, 1500000000, 500, 7, 'PreviewReady');
                    INSERT INTO callstack VALUES (3, 1600000000, 400000000, 7, 'UnrelatedHeavyWork');
                    INSERT INTO callstack VALUES (4, 1100000000, 750, 8, 'CameraStart');
                    """
                )
            converter = root / "fake-trace-streamer"
            converter.write_text("#!/bin/sh\ncp \"$HETERO_TEST_TRACE_DB\" \"$3\"\n", encoding="utf-8")
            converter.chmod(converter.stat().st_mode | stat.S_IXUSR)
            with patch.dict(
                os.environ,
                {"HETERO_TRACE_STREAMER": str(converter), "HETERO_TEST_TRACE_DB": str(database)},
                clear=False,
            ):
                normalized = analyze_htrace(htrace, state_root=root / "state", workspace_root=root)["normalized"]
                catalog = list_htrace_runtime_event_names(
                    normalized["raw_htrace"],
                    state_root=root / "state",
                    search_terms=["CameraStart", "PreviewReady"],
                    limit=4,
                )

        names = [event["event_name"] for event in catalog["events"]]
        self.assertEqual(names[:3], ["CameraStart", "CameraStart", "PreviewReady"])
        self.assertIn("UnrelatedHeavyWork", names)
        self.assertEqual(catalog["quality"]["total_distinct_event_names"], 3)
        self.assertEqual(catalog["quality"]["total_distinct_event_contexts"], 4)
        self.assertEqual(
            {event["trace_context"]["process_name"] for event in catalog["events"] if event["event_name"] == "CameraStart"},
            {"com.example.camera", "camera.service"},
        )
        self.assertEqual(catalog["quality"]["measurement_semantics"], "inclusive_trace_span")
        self.assertEqual(catalog["events"][0]["trace_context"]["process_name"], "com.example.camera")
        self.assertEqual(catalog["events"][0]["source_resolution"], "observed_runtime_trace_name")

    def test_htrace_stage_window_analysis_preserves_stage_and_event_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            htrace = root / "fixture.htrace"
            htrace.write_bytes(b"OHOSPROF" + b"\0" * 128)
            database = root / "converted.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE trace_range(start_ts INTEGER, end_ts INTEGER);
                    CREATE TABLE process(id INTEGER, ipid INTEGER, pid INTEGER, name TEXT);
                    CREATE TABLE thread(id INTEGER, itid INTEGER, tid INTEGER, name TEXT, ipid INTEGER);
                    CREATE TABLE callstack(id INTEGER, ts INTEGER, dur INTEGER, callid INTEGER, name TEXT, parent_id INTEGER);
                    INSERT INTO trace_range VALUES (1000000000, 1100000000);
                    INSERT INTO process VALUES (1, 1, 100, 'com.example.camera');
                    INSERT INTO thread VALUES (1, 7, 701, 'main', 1);
                    INSERT INTO callstack VALUES (1, 1000000000, 20000000, 7, 'RootEvent', 4294967295);
                    INSERT INTO callstack VALUES (2, 1001000000, 8000000, 7, 'ChildEvent', 1);
                    INSERT INTO callstack VALUES (3, 1010000000, 3000000, 7, 'ChildEvent', 1);
                    INSERT INTO callstack VALUES (4, 1030000000, 12000000, 7, 'OutsideStage', 4294967295);
                    """
                )
            converter = root / "fake-trace-streamer"
            converter.write_text("#!/bin/sh\ncp \"$HETERO_TEST_TRACE_DB\" \"$3\"\n", encoding="utf-8")
            converter.chmod(converter.stat().st_mode | stat.S_IXUSR)
            with patch.dict(
                os.environ,
                {"HETERO_TRACE_STREAMER": str(converter), "HETERO_TEST_TRACE_DB": str(database)},
                clear=False,
            ):
                normalized = analyze_htrace(htrace, state_root=root / "state", workspace_root=root)["normalized"]
                result = analyze_htrace_stage_windows(
                    normalized["raw_htrace"],
                    state_root=root / "state",
                    stages=[{
                        "id": "camera_boot",
                        "start_ns": 1_000_000_000,
                        "end_ns": 1_025_000_000,
                        "process_names": ["com.example.camera"],
                    }],
                    top_event_limit=8,
                )
                density_result = analyze_htrace_stage_windows(
                    normalized["raw_htrace"],
                    state_root=root / "state",
                    stages=[{
                        "id": "camera_boot",
                        "start_ns": 1_000_000_000,
                        "end_ns": 1_025_000_000,
                        "process_names": ["com.example.camera"],
                    }],
                    top_event_limit=8,
                    timeline_occurrence_limit=2,
                    timeline_bucket_count=16,
                )

        self.assertEqual(result["quality"]["status"], "available")
        stage = result["stages"][0]
        self.assertEqual(stage["event_count"], 3)
        self.assertEqual(stage["total_event_duration_ms"], 31.0)
        self.assertEqual([item["name"] for item in stage["top_events"]], ["ChildEvent", "RootEvent"])
        self.assertEqual(stage["top_events"][0]["metrics"]["self_time_estimate_total_duration_ms"], 11.0)
        self.assertEqual(stage["top_events"][1]["metrics"]["self_time_estimate_total_duration_ms"], 9.0)
        self.assertFalse(stage["top_events"][0]["measurement_semantics"]["cpu_self_time_proven"])
        timeline = stage["event_timeline"]
        self.assertEqual(timeline["representation"], "observed_occurrences")
        self.assertEqual(timeline["source_occurrence_count"], 3)
        self.assertEqual(timeline["returned_occurrence_count"], 3)
        self.assertFalse(timeline["truncated"])
        self.assertEqual(
            [item["self_time_estimate_duration_ms"] for item in timeline["occurrences"]],
            [9.0, 8.0, 3.0],
        )
        density = density_result["stages"][0]["event_timeline"]
        self.assertEqual(density["representation"], "binned_density")
        self.assertEqual(density["source_occurrence_count"], 3)
        self.assertEqual(density["returned_occurrence_count"], 0)
        self.assertTrue(density["truncated"])
        self.assertEqual(sum(item["occurrence_count"] for item in density["density_buckets"]), 3)

    def test_htrace_self_time_events_use_direct_child_subtraction_and_stable_rank(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            htrace = root / "fixture.htrace"
            htrace.write_bytes(b"OHOSPROF" + b"\0" * 128)
            database = root / "converted.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE trace_range(start_ts INTEGER, end_ts INTEGER);
                    CREATE TABLE callstack(id INTEGER, ts INTEGER, dur INTEGER, callid INTEGER, name TEXT, parent_id INTEGER);
                    INSERT INTO trace_range VALUES (1000000000, 1100000000);
                    INSERT INTO callstack VALUES (1, 1000000000, 20000000, 7, 'RootEvent', 4294967295);
                    INSERT INTO callstack VALUES (2, 1001000000, 8000000, 7, 'ChildEvent', 1);
                    INSERT INTO callstack VALUES (3, 1010000000, 3000000, 7, 'ChildEvent', 1);
                    INSERT INTO callstack VALUES (4, 1030000000, 12000000, 8, 'WorkerEvent', 4294967295);
                    """
                )
            converter = root / "fake-trace-streamer"
            converter.write_text("#!/bin/sh\ncp \"$HETERO_TEST_TRACE_DB\" \"$3\"\n", encoding="utf-8")
            converter.chmod(converter.stat().st_mode | stat.S_IXUSR)
            with patch.dict(os.environ, {"HETERO_TRACE_STREAMER": str(converter), "HETERO_TEST_TRACE_DB": str(database)}, clear=False):
                normalized = analyze_htrace(htrace, state_root=root / "state", workspace_root=root)["normalized"]
                catalog = list_htrace_self_time_events(normalized["raw_htrace"], state_root=root / "state")
                grouped_catalog = list_htrace_self_time_events(
                    normalized["raw_htrace"],
                    state_root=root / "state",
                    group_by_module=True,
                    per_module_limit=1,
                )

        self.assertEqual(catalog["quality"]["source"], "converted_callstack_direct_child_subtraction")
        self.assertEqual(catalog["quality"]["ranking"], "self_time_estimate_total_duration_desc")
        self.assertEqual([event["event_name"] for event in catalog["events"][:3]], ["WorkerEvent", "ChildEvent", "RootEvent"])
        self.assertEqual(catalog["events"][0]["metrics"]["self_time_estimate_total_duration_ms"], 12.0)
        self.assertEqual(catalog["events"][1]["metrics"]["self_time_estimate_total_duration_ms"], 11.0)
        self.assertEqual(catalog["events"][2]["metrics"]["self_time_estimate_total_duration_ms"], 9.0)
        self.assertEqual(catalog["events"][0]["trace_event_refs"][0]["event_id"], "callstack:4")
        self.assertEqual(grouped_catalog["quality"]["ranking"], "self_time_estimate_total_duration_desc_top_n_per_module")
        self.assertEqual(sum(len(group["events"]) for group in grouped_catalog["module_event_groups"]), len(grouped_catalog["events"]))
        self.assertTrue(all(group["returned_event_count"] <= 1 for group in grouped_catalog["module_event_groups"]))
        self.assertTrue(all("module_rank" in event for event in grouped_catalog["events"]))

    def test_htrace_self_time_module_filter_uses_hitrace_tag_before_global_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            htrace = root / "fixture.htrace"
            htrace.write_bytes(b"OHOSPROF" + b"\0" * 128)
            database = root / "converted.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE trace_range(start_ts INTEGER, end_ts INTEGER);
                    CREATE TABLE process(id INTEGER, ipid INTEGER, pid INTEGER, name TEXT);
                    CREATE TABLE thread(id INTEGER, itid INTEGER, tid INTEGER, name TEXT, ipid INTEGER);
                    CREATE TABLE callstack(id INTEGER, ts INTEGER, dur INTEGER, callid INTEGER, name TEXT, parent_id INTEGER);
                    INSERT INTO trace_range VALUES (1000000000, 1020000000);
                    INSERT INTO process VALUES (1, 1, 100, 'system');
                    INSERT INTO process VALUES (2, 2, 200, 'com.example.app');
                    INSERT INTO thread VALUES (1, 7, 701, 'OS_FFRT_0', 1);
                    INSERT INTO thread VALUES (2, 8, 801, 'main', 2);
                    INSERT INTO callstack VALUES (1, 1000000000, 12000000, 7, 'H:FFRTTask|I13', 4294967295);
                    INSERT INTO callstack VALUES (2, 1010000000, 3000000, 8, 'H:AceLayout|I39', 4294967295);
                    """
                )
            converter = root / "fake-trace-streamer"
            converter.write_text("#!/bin/sh\ncp \"$HETERO_TEST_TRACE_DB\" \"$3\"\n", encoding="utf-8")
            converter.chmod(converter.stat().st_mode | stat.S_IXUSR)
            tag_catalog = {
                "tag_by_bit": {
                    13: {"bit": 13, "macro": "HITRACE_TAG_FFRT", "display_name": "FFRT", "category": "module"},
                    39: {"bit": 39, "macro": "HITRACE_TAG_ACE", "display_name": "ACE", "category": "module"},
                }
            }
            with patch.dict(os.environ, {"HETERO_TRACE_STREAMER": str(converter), "HETERO_TEST_TRACE_DB": str(database)}, clear=False):
                normalized = analyze_htrace(htrace, state_root=root / "state", workspace_root=root)["normalized"]
                all_modules = list_htrace_self_time_events(
                    normalized["raw_htrace"],
                    state_root=root / "state",
                    limit=1,
                    tag_catalog=tag_catalog,
                )
                arkui_only = list_htrace_self_time_events(
                    normalized["raw_htrace"],
                    state_root=root / "state",
                    limit=1,
                    module_key="arkui_ui",
                    tag_catalog=tag_catalog,
                )

        self.assertEqual(all_modules["events"][0]["event_name"], "H:FFRTTask|I13")
        self.assertEqual(arkui_only["events"][0]["event_name"], "H:AceLayout|I39")
        self.assertEqual(arkui_only["events"][0]["trace_module"]["module_key"], "arkui_ui")
        self.assertEqual(arkui_only["events"][0]["trace_module"]["classification"], "hitrace_tag_macro_v1")
        self.assertTrue(any(group["module_key"] == "arkui_ui" for group in arkui_only["module_catalog"]["groups"]))
        self.assertEqual(arkui_only["quality"]["module_filter"], "arkui_ui")

    def test_raw_htrace_scheduler_catalog_joins_wakeup_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            htrace = root / "fixture.htrace"
            htrace.write_bytes(b"OHOSPROF" + b"\0" * 128)
            database = root / "converted.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE trace_range(start_ts INTEGER, end_ts INTEGER);
                    CREATE TABLE raw(id INTEGER, ts INTEGER, name TEXT, cpu INTEGER, itid INTEGER);
                    CREATE TABLE instant(ts INTEGER, name TEXT, ref INTEGER, wakeup_from INTEGER, ref_type TEXT, value REAL);
                    CREATE TABLE thread(id INTEGER, itid INTEGER, tid INTEGER, name TEXT, ipid INTEGER);
                    CREATE TABLE process(id INTEGER, ipid INTEGER, pid INTEGER, name TEXT);
                    INSERT INTO trace_range VALUES (1000000000, 1020000000);
                    INSERT INTO process VALUES (1, 1, 10, 'system');
                    INSERT INTO process VALUES (2, 2, 20, 'RenderService');
                    INSERT INTO thread VALUES (1, 2, 101, 'OS_FFRT_0', 1);
                    INSERT INTO thread VALUES (2, 3, 202, 'RenderThread', 2);
                    INSERT INTO raw VALUES (1, 1000000001, 'sched_wakeup', 0, 3);
                    INSERT INTO instant VALUES (1000000001, 'sched_wakeup', 3, 2, 'itid', 0.0);
                    """
                )
            converter = root / "fake-trace-streamer"
            converter.write_text("#!/bin/sh\ncp \"$HETERO_TEST_TRACE_DB\" \"$3\"\n", encoding="utf-8")
            converter.chmod(converter.stat().st_mode | stat.S_IXUSR)
            with patch.dict(os.environ, {"HETERO_TRACE_STREAMER": str(converter), "HETERO_TEST_TRACE_DB": str(database)}, clear=False):
                result = analyze_htrace(htrace, state_root=root / "state", workspace_root=root)["normalized"]

        self.assertEqual(result["analysis_profile"], "htrace-basic-scheduler-module-catalog-v2")
        self.assertEqual(len(result["clusters"]), 1)
        summary = result["clusters"][0]["module_summary"]
        self.assertIn("FFRT runtime", summary["module_names"])
        self.assertIn("Render / Rosen", summary["module_names"])
        self.assertFalse(summary["system_module_only"])
        self.assertTrue(result["quality"]["scheduler_catalog"]["endpoint_mapping"]["source_wakeup_edge_map"])

    def test_htrace_cluster_events_are_ranked_by_selected_window_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            htrace = root / "fixture.htrace"
            htrace.write_bytes(b"OHOSPROF" + b"\0" * 128)
            database = root / "converted.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE trace_range(start_ts INTEGER, end_ts INTEGER);
                    CREATE TABLE callstack(id INTEGER, ts INTEGER, dur INTEGER, callid INTEGER, name TEXT);
                    INSERT INTO trace_range VALUES (1000000000, 1040000000);
                    INSERT INTO callstack VALUES (1, 1000000000, 20000000, 7, 'RenderFrame');
                    INSERT INTO callstack VALUES (2, 1001000000, 1000000, 7, 'SmallEvent');
                    """
                )
            converter = root / "fake-trace-streamer"
            converter.write_text("#!/bin/sh\ncp \"$HETERO_TEST_TRACE_DB\" \"$3\"\n", encoding="utf-8")
            converter.chmod(converter.stat().st_mode | stat.S_IXUSR)
            with patch.dict(os.environ, {"HETERO_TRACE_STREAMER": str(converter), "HETERO_TEST_TRACE_DB": str(database)}, clear=False):
                normalized = analyze_htrace(htrace, state_root=root / "state", workspace_root=root)["normalized"]
                event_catalog = list_htrace_cluster_events(
                    normalized["raw_htrace"],
                    normalized["clusters"][0],
                    state_root=root / "state",
                )

        self.assertEqual(event_catalog["quality"]["source"], "converted_callstack_overlap")
        self.assertEqual(event_catalog["events"][0]["event_name"], "RenderFrame")
        self.assertEqual(event_catalog["events"][0]["event_kind"], "callstack_span")
        self.assertEqual(event_catalog["events"][0]["metrics"]["occurrence_count"], 1)


class HitraceInstrumentationTests(unittest.TestCase):
    def _write_header(self, root: Path) -> None:
        header = root / "base/hiviewdfx/hitrace/interfaces/native/innerkits/include/hitrace_meter/hitrace_meter.h"
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text(
            "\n".join(
                [
                    "constexpr uint64_t HITRACE_TAG_COMMERCIAL = (1ULL << 5);",
                    "constexpr uint64_t HITRACE_TAG_GRAPHIC_AGP = (1ULL << 38);",
                    "constexpr uint64_t HITRACE_TAG_ACE = (1ULL << 39);",
                    "constexpr uint64_t HITRACE_TAG_ARK = (1ULL << 47);",
                    "constexpr uint64_t HITRACE_TAG_WINDOW_MANAGER = (1ULL << 48);",
                    "constexpr uint64_t HITRACE_TAG_APP = (1ULL << 62);",
                ]
            ),
            encoding="utf-8",
        )

    def test_marker_decodes_level_and_multiple_serialized_tag_bits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_header(root)
            catalog = load_hitrace_tag_catalog(root, root)
            marker = describe_hitrace_event("H:vkQueueSubmit|M0538", catalog)

        self.assertIsNotNone(marker)
        assert marker is not None
        self.assertEqual(marker["output_level"], {"code": "M", "name": "commercial"})
        self.assertEqual([tag["macro"] for tag in marker["tags"]], ["HITRACE_TAG_COMMERCIAL", "HITRACE_TAG_GRAPHIC_AGP"])
        self.assertEqual([tag["display_name"] for tag in marker["module_tags"]], ["GRAPHIC_AGP"])

    def test_exact_label_and_direct_tag_find_the_instrumentation_point_and_function(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_header(root)
            source = root / "window/display_listener.cpp"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                """void JsDisplayListener::OnChange(int id)
{
    auto task = [id]() {
        HITRACE_METER_FMT(HITRACE_TAG_WINDOW_MANAGER, \"JsDisplayListener::OnChange\");
    };
}
""",
                encoding="utf-8",
            )
            result = locate_hitrace_instrumentation(
                root,
                root,
                "H:JsDisplayListener::OnChange|I48",
                tag_catalog=load_hitrace_tag_catalog(root, root),
            )

        self.assertEqual(result["status"], "available")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["path"], "window/display_listener.cpp")
        self.assertEqual(candidate["instrumentation"]["event_label_match"], "exact_literal")
        self.assertEqual(candidate["instrumentation"]["tag_match"], "direct")
        self.assertEqual(candidate["enclosing_function"]["name"], "JsDisplayListener::OnChange")

    def test_wrapper_tag_and_dynamic_format_are_retained_as_distinct_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_header(root)
            header = root / "graphics/trace.h"
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text(
                "#define RS_TRACE_NAME(name) HITRACE_METER_NAME(HITRACE_TAG_GRAPHIC_AGP | HITRACE_TAG_COMMERCIAL, name)\n",
                encoding="utf-8",
            )
            source = root / "graphics/submit.cpp"
            source.write_text(
                """void Renderer::Submit(int frame)
{
    RS_TRACE_NAME(\"vkQueueSubmit\");
    HITRACE_METER_FMT(HITRACE_TAG_GRAPHIC_AGP, \"RenderFrame[%d]\", frame);
}
""",
                encoding="utf-8",
            )
            catalog = load_hitrace_tag_catalog(root, root)
            wrapper = locate_hitrace_instrumentation(root, root, "H:vkQueueSubmit|M0538", tag_catalog=catalog)
            formatted = locate_hitrace_instrumentation(root, root, "H:RenderFrame[42]|I38", tag_catalog=catalog)

        self.assertEqual(wrapper["candidates"][0]["instrumentation"]["tag_match"], "wrapper_definition")
        self.assertEqual(formatted["candidates"][0]["instrumentation"]["event_label_match"], "normalized_format")
        self.assertEqual(formatted["candidates"][0]["instrumentation"]["tag_match"], "direct")

    def test_ohos_hitrace_wrapper_resolves_ark_literal_and_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_header(root)
            wrapper = root / "arkcompiler/ets_runtime/common_components/log/log.h"
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            wrapper.write_text(
                "#define OHOS_HITRACE(level, name, customArgs) \\\n+    HITRACE_METER_NAME_EX(level, HITRACE_TAG_ARK, name, customArgs)\n",
                encoding="utf-8",
            )
            source = root / "arkcompiler/ets_runtime/ecmascript/js_thread.cpp"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                """void JSThread::IterateMegaIC()
{
    OHOS_HITRACE(HITRACE_LEVEL_COMMERCIAL, \"VisitRootGlobalRefHandle\", \"\");
}
""",
                encoding="utf-8",
            )
            result = locate_hitrace_instrumentation(
                root,
                root,
                "H:VisitRootGlobalRefHandle|M47",
                tag_catalog=load_hitrace_tag_catalog(root, root),
            )

        self.assertEqual(result["status"], "available")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["path"], "arkcompiler/ets_runtime/ecmascript/js_thread.cpp")
        self.assertEqual(candidate["instrumentation"]["event_label_match"], "exact_literal")
        self.assertEqual(candidate["instrumentation"]["tag_match"], "wrapper_definition")
        self.assertEqual(platform_app._hitrace_instrumentation_resolution(candidate)["state"], "instrumentation_point_exact")

    def test_instrumentation_scope_ignores_a_previous_closed_function(self) -> None:
        """A nearby earlier definition must not become the AST target by proximity."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_header(root)
            source = root / "arkcompiler/ets_runtime/ecmascript/js_thread.cpp"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                """void JSThread::IterateMegaIC()
{
    DoPreviousWork();
}

void JSThread::Iterate()
{
    HITRACE_METER_FMT(HITRACE_TAG_ARK, \"VisitRootGlobalRefHandle\");
}
""",
                encoding="utf-8",
            )
            result = locate_hitrace_instrumentation(
                root,
                root,
                "H:VisitRootGlobalRefHandle|M47",
                tag_catalog=load_hitrace_tag_catalog(root, root),
            )

        candidate = result["candidates"][0]
        scope = candidate["enclosing_function"]
        self.assertEqual(scope["name"], "JSThread::Iterate")
        self.assertEqual(scope["declaration_line"], 6)
        self.assertEqual(scope["body_start_line"], 7)
        self.assertEqual(scope["body_end_line"], 9)
        evidence = platform_app._hitrace_code_match_evidence(candidate)
        self.assertEqual(evidence["ast_target"]["name"], "JSThread::Iterate")
        self.assertEqual({item["line"] for item in evidence["matched_lines"]}, {6, 8})

    def test_dynamic_ace_label_uses_static_prefix_in_napi_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_header(root)
            source = root / "foundation/arkui/napi/native_engine/native_async_work.cpp"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                """void NativeAsyncWork::AsyncAfterWorkCallback()
{
    StartTrace(HITRACE_TAG_ACE, \"Native async work complete callback, \" + that->GetTraceDescription());
}
""",
                encoding="utf-8",
            )
            result = locate_hitrace_instrumentation(
                root,
                root,
                "H:Native async work complete callback, name:fs.access#393828626432, traceid:0x0|I39",
                tag_catalog=load_hitrace_tag_catalog(root, root),
            )

        self.assertEqual(result["status"], "available")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["path"], "foundation/arkui/napi/native_engine/native_async_work.cpp")
        self.assertEqual(candidate["instrumentation"]["event_label_match"], "static_prefix")
        self.assertEqual(candidate["instrumentation"]["tag_match"], "direct")
        self.assertEqual(candidate["enclosing_function"]["name"], "NativeAsyncWork::AsyncAfterWorkCallback")
        self.assertIn("foundation/arkui/napi", result["source_scope"]["roots"])

    def test_tag_scope_supports_an_openharmony_workspace_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_header(root)
            source = root / "openharmony/foundation/arkui/napi/native_engine/native_async_work.cpp"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                """void NativeAsyncWork::AsyncAfterWorkCallback()
{
    StartTrace(HITRACE_TAG_ACE, \"Native async work complete callback, \" + that->GetTraceDescription());
}
""",
                encoding="utf-8",
            )
            result = locate_hitrace_instrumentation(
                root,
                root,
                "H:Native async work complete callback, name:fs.access#393828626432, traceid:0x0|I39",
                tag_catalog=load_hitrace_tag_catalog(root, root),
            )

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["candidates"][0]["path"], "openharmony/foundation/arkui/napi/native_engine/native_async_work.cpp")
        self.assertIn("openharmony/foundation/arkui/napi", result["source_scope"]["roots"])
        self.assertNotIn("foundation/arkui/napi", result["source_scope"]["missing_relative_roots"])

    def test_source_resolution_prefers_an_exact_hitrace_instrumentation_point(self) -> None:
        class TemporaryConnector:
            def __init__(self, root: Path) -> None:
                self.root = root

            def get(self, _: str) -> dict[str, str]:
                return {"source_root": str(self.root)}

            def resolve_source_path(self, _: str, path: str) -> Path:
                source = (self.root / path).resolve()
                source.relative_to(self.root.resolve())
                if not source.is_file():
                    raise PlatformError("SOURCE_FILE_NOT_FOUND", "Test source is unavailable.", status_code=404)
                return source

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_header(root)
            source = root / "window/listener.cpp"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                """void JsDisplayListener::OnChange(int id)
{
    HITRACE_METER_FMT(HITRACE_TAG_WINDOW_MANAGER, \"JsDisplayListener::OnChange\");
}
""",
                encoding="utf-8",
            )
            store = ArtifactStore(root / "state", project_id="local-default")
            analysis = {
                "artifact_id": "art_analysis",
                "content_sha256": "a" * 64,
                "source_connector_id": "connector_test",
                "source_binding": {"match_level": "unknown"},
            }
            event = {
                "event_id": "event_1",
                "event_name": "H:JsDisplayListener::OnChange|I48",
                "event_kind": "callstack_span",
            }
            with patch.object(platform_app, "store", store), patch.object(platform_app, "connectors", TemporaryConnector(root)), patch.object(platform_app, "WORKSPACE_ROOT", root):
                artifact = platform_app._resolve_trace_event_source(
                    analysis,
                    None,
                    event,
                    {"artifact_id": "art_catalog", "content_sha256": "b" * 64},
                    catalog_scope={
                        "kind": "trace_hotspot_discovery",
                        "scope": "trace_wide",
                        "discovery_id": "event_self_time",
                        "profile_id": "htrace-event-self-time-v2",
                    },
                )
                payload = store.read("trace_event_source_resolution", artifact["artifact_id"])

        self.assertEqual(payload["resolution"]["state"], "instrumentation_point_exact")
        self.assertEqual(payload["catalog_scope"]["discovery_id"], "event_self_time")
        self.assertNotIn("cluster_id", payload)
        self.assertEqual(payload["located_function"]["name"], "JsDisplayListener::OnChange")
        self.assertEqual(payload["code_block"]["source"]["path"], "window/listener.cpp")
        self.assertEqual(payload["code_block"]["match_evidence"]["ast_target"]["name"], "JsDisplayListener::OnChange")
        self.assertEqual(
            {item["role"] for item in payload["code_block"]["match_evidence"]["matched_lines"]},
            {"instrumentation_point", "enclosing_function"},
        )
        scope_sha256 = payload["hitrace_instrumentation"]["source_scope"]["knowledge"]["sha256"]
        self.assertIn(f"sha256:{scope_sha256}", payload["input_artifacts"])

    def test_source_resolution_keeps_missing_hitrace_scope_reason(self) -> None:
        class TemporaryConnector:
            def __init__(self, root: Path, alternate_root: Path) -> None:
                self.root = root
                self.alternate_root = alternate_root

            def get(self, connector_id: str) -> dict[str, str]:
                if connector_id == "connector_full":
                    return {"source_root": str(self.alternate_root)}
                return {"source_root": str(self.root)}

            def list(self) -> list[dict[str, object]]:
                return [
                    {"connector_id": "connector_test", "source_root": str(self.root), "snapshot": {"snapshot_id": "source_snapshot_partial", "clean": True}},
                    {"connector_id": "connector_full", "display_name": "Full OpenHarmony", "source_root": str(self.alternate_root), "snapshot": {"snapshot_id": "source_snapshot_full", "clean": True}, "snapshot_artifact_id": "art_snapshot_full"},
                ]

            def resolve_source_path(self, _: str, path: str) -> Path:
                source = (self.root / path).resolve()
                source.relative_to(self.root.resolve())
                if not source.is_file():
                    raise PlatformError("SOURCE_FILE_NOT_FOUND", "Test source is unavailable.", status_code=404)
                return source

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_header(root)
            present_scope = root / "arkui_ace_engine/placeholder.cpp"
            present_scope.parent.mkdir(parents=True, exist_ok=True)
            present_scope.write_text("void Placeholder() {}\n", encoding="utf-8")
            alternate_root = root / "full_workspace"
            (alternate_root / "openharmony/foundation/arkui/napi").mkdir(parents=True, exist_ok=True)
            (alternate_root / "openharmony/foundation/arkui/ace_engine").mkdir(parents=True, exist_ok=True)
            store = ArtifactStore(root / "state", project_id="local-default")
            analysis = {
                "artifact_id": "art_analysis",
                "content_sha256": "a" * 64,
                "source_connector_id": "connector_test",
                "source_binding": {"match_level": "unknown"},
            }
            event = {
                "event_id": "event_1",
                "event_name": "H:Native async work complete callback, name:fs.access#393828626432, traceid:0x0|I39",
                "event_kind": "callstack_span",
            }
            with patch.object(platform_app, "store", store), patch.object(platform_app, "connectors", TemporaryConnector(root, alternate_root)), patch.object(platform_app, "WORKSPACE_ROOT", root):
                artifact = platform_app._resolve_trace_event_source(
                    analysis,
                    None,
                    event,
                    {"artifact_id": "art_catalog", "content_sha256": "b" * 64},
                )
                payload = store.read("trace_event_source_resolution", artifact["artifact_id"])

        self.assertEqual(payload["resolution"]["state"], "unresolved")
        self.assertIn("Curated candidate source scope(s) absent", payload["resolution"]["reason"])
        self.assertIn("foundation/arkui/napi", payload["resolution"]["reason"])
        recommendations = payload["hitrace_instrumentation"]["source_connector_recommendations"]
        self.assertEqual(recommendations[0]["connector_id"], "connector_full")
        self.assertIn("openharmony/foundation/arkui/napi", recommendations[0]["covered_relative_roots"])
        self.assertIn("artifact:art_snapshot_full", payload["input_artifacts"])

    def test_pretty_function_marker_finds_the_enclosing_instrumentation_macro(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_header(root)
            source = root / "bundle/bundle_mgr_proxy.cpp"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                """bool BundleMgrProxy::GetBundleNameForUid(const int uid, std::string &bundleName)
{
    HITRACE_METER_NAME_EX(HITRACE_LEVEL_INFO, HITRACE_TAG_APP, __PRETTY_FUNCTION__, nullptr);
    return true;
}
""",
                encoding="utf-8",
            )
            result = locate_hitrace_instrumentation(
                root,
                root,
                "H:virtual bool OHOS::AppExecFwk::BundleMgrProxy::GetBundleNameForUid(const int, std::string &)|I62",
                tag_catalog=load_hitrace_tag_catalog(root, root),
            )

        self.assertEqual(result["status"], "available")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["instrumentation"]["event_label_match"], "function_derived")
        self.assertEqual(candidate["instrumentation"]["label_derivation"], "__PRETTY_FUNCTION__")
        self.assertEqual(candidate["instrumentation"]["tag_match"], "direct")
        self.assertEqual(candidate["enclosing_function"]["name"], "BundleMgrProxy::GetBundleNameForUid")
        self.assertEqual(platform_app._hitrace_instrumentation_resolution(candidate)["state"], "instrumentation_point_function_derived")


class WorkspaceFileActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "source"
        self.root.mkdir(parents=True)
        for command in (
            ["git", "init", str(self.root)],
            ["git", "-C", str(self.root), "config", "user.email", "workspace-test@example.invalid"],
            ["git", "-C", str(self.root), "config", "user.name", "Workspace Test"],
        ):
            subprocess.run(command, check=True, capture_output=True, text=True)
        self.source_file = self.root / "sample.cpp"
        self.source_file.write_text("int sample() { return 1; }\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "sample.cpp"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "fixture"], check=True, capture_output=True, text=True)
        self.store = ArtifactStore(Path(self.tempdir.name) / "state", project_id="local-default")
        self.connectors = SourceConnectorManager(Path(self.tempdir.name) / "state", self.store, workspace_root=Path(__file__).resolve().parents[3])
        self.connector = self.connectors.register_local(self.root, display_name="workspace fixture")
        self.patchers = [
            patch.object(platform_app, "store", self.store),
            patch.object(platform_app, "connectors", self.connectors),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_read_then_hash_guarded_full_access_write(self) -> None:
        read = platform_app.read_workspace_file(
            platform_app.WorkspaceFileReadRequest(
                source_connector_id=self.connector["connector_id"],
                path="sample.cpp",
            )
        )
        self.assertIn("return 1", read["content"])
        self.assertFalse(read["truncated"])

        written = platform_app.write_workspace_file(
            platform_app.WorkspaceFileWriteRequest(
                source_connector_id=self.connector["connector_id"],
                path="sample.cpp",
                content="int sample() { return 2; }\n",
                expected_sha256=read["sha256"],
                execution_policy="full_access",
            )
        )
        self.assertEqual(written["artifact"]["artifact_type"], "agent_workspace_file_write")
        self.assertIn("return 2", self.source_file.read_text(encoding="utf-8"))

    def test_write_rejects_policy_conflict_and_protected_paths(self) -> None:
        with self.assertRaises(PlatformError) as policy_error:
            platform_app.write_workspace_file(
                platform_app.WorkspaceFileWriteRequest(
                    source_connector_id=self.connector["connector_id"],
                    path="sample.cpp",
                    content="changed\n",
                    execution_policy="confirm_device_execution",
                )
            )
        self.assertEqual(policy_error.exception.code, "WORKSPACE_WRITE_REQUIRES_FULL_ACCESS")

        with self.assertRaises(PlatformError) as path_error:
            platform_app.read_workspace_file(
                platform_app.WorkspaceFileReadRequest(
                    source_connector_id=self.connector["connector_id"],
                    path=".git/config",
                )
            )
        self.assertEqual(path_error.exception.code, "WORKSPACE_FILE_PATH_PROTECTED")

        with self.assertRaises(PlatformError) as conflict_error:
            platform_app.write_workspace_file(
                platform_app.WorkspaceFileWriteRequest(
                    source_connector_id=self.connector["connector_id"],
                    path="sample.cpp",
                    content="changed\n",
                    expected_sha256="0" * 64,
                    execution_policy="full_access",
                )
            )
        self.assertEqual(conflict_error.exception.code, "WORKSPACE_FILE_CONFLICT")

    def test_read_rejects_artifact_and_evidence_identifiers_as_paths(self) -> None:
        for identifier in (
            "art_5178f91ff26e8e73fb5551f6",
            "artifact:art_5178f91ff26e8e73fb5551f6",
            "trace:event_htrace_self_1234",
            "source:0650dfd7fcfd3cec",
        ):
            with self.subTest(identifier=identifier), self.assertRaises(PlatformError) as raised:
                platform_app.read_workspace_file(
                    platform_app.WorkspaceFileReadRequest(
                        source_connector_id=self.connector["connector_id"],
                        path=identifier,
                    )
                )
            self.assertEqual(raised.exception.code, "WORKSPACE_FILE_IDENTIFIER_NOT_PATH")


class SkillCatalogMaterializationTests(unittest.TestCase):
    def test_page_actions_materialize_device_validation_evolution_events(self) -> None:
        payload = platform_app.page_actions()
        skills = {item["skill_id"]: item for item in payload["skills"]}
        event_ids = set(skills["agent-native-device-experimentation"]["evolution"]["event_ids"])

        self.assertIn("device-validation-input-contract-20260731", event_ids)
        self.assertIn("official-test-framework-selection-20260805", event_ids)
        self.assertIn("curated-api-template-library-20260805", event_ids)
        self.assertTrue(any(action["action_id"] == "workspace.read_file" for action in payload["actions"]))


class AgentUiResultEndpointTests(unittest.TestCase):
    def test_ui_result_does_not_reconfigure_an_active_kimi_prompt(self) -> None:
        request = platform_app.AgentRunUiActionResultRequest(
            request_id="act_test",
            result={"status": "succeeded"},
            credential=platform_app.AgentRunCredentialRequest(
                base_url="https://api.kimi.com/coding/v1",
                api_key="test-key",
                model="kimi-for-coding",
            ),
        )
        http_request = Request({
            "type": "http",
            "method": "POST",
            "path": "/v1/agent-runs/run_test/ui-action-results",
            "query_string": b"",
            "headers": [(b"idempotency-key", b"test-ui-result")],
        })
        expected_run = {"run_id": "run_test", "state": "running"}
        with (
            patch.object(platform_app, "AGENT_ENGINE_KIND", "kimi_server"),
            patch.object(platform_app, "_idempotency_lookup", return_value=None),
            patch.object(platform_app, "_idempotency_store"),
            patch.object(platform_app, "_configure_kimi_browser_credential") as configure,
            patch.object(platform_app.agent_runtime, "submit_ui_result", return_value=expected_run) as submit,
        ):
            response = platform_app.submit_agent_ui_action_result("run_test", request, http_request)

        self.assertEqual(response, {"run": expected_run})
        configure.assert_not_called()
        submit.assert_called_once_with("run_test", "act_test", {"status": "succeeded"}, error=None)


class ModelRelayTests(unittest.TestCase):
    class _Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def read(self, _: int) -> bytes:
            return self.body

        def __enter__(self) -> "ModelRelayTests._Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def test_relay_forwards_kimi_coding_without_persisting_the_key(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-coding-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "ping"}],
        )
        with patch.object(platform_app, "urlopen", return_value=self._Response(b'{"choices":[{"message":{"content":"OK"}}]}')) as opener:
            response = platform_app._relay_chat_completion(request)

        self.assertEqual(response["choices"][0]["message"]["content"], "OK")
        upstream = opener.call_args.args[0]
        self.assertEqual(upstream.full_url, "https://api.kimi.com/coding/v1/chat/completions")
        self.assertEqual(upstream.get_header("Authorization"), "Bearer test-coding-key")
        self.assertNotIn("test-coding-key", upstream.data.decode("utf-8"))
        self.assertNotIn("temperature", upstream.data.decode("utf-8"))

    def test_direct_relay_mode_bypasses_the_coding_agent_session(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-coding-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=384,
            execution_mode="direct_relay",
        )
        expected = {"choices": [{"message": {"content": "hello"}}]}
        with (
            patch.object(platform_app, "AGENT_ENGINE_KIND", "kimi_server"),
            patch.object(platform_app, "_relay_chat_completion", return_value=expected) as direct_relay,
            patch.object(platform_app.kimi_completion_adapter, "complete") as agent_completion,
        ):
            response = platform_app.relay_model_chat_completion(request)

        self.assertEqual(response, expected)
        direct_relay.assert_called_once_with(request)
        agent_completion.assert_not_called()

    def test_direct_relay_stream_mode_bypasses_the_coding_agent_session(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-coding-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=384,
            execution_mode="direct_relay",
        )
        sentinel = object()
        with (
            patch.object(platform_app, "AGENT_ENGINE_KIND", "kimi_server"),
            patch.object(platform_app, "_relay_chat_completion_stream", return_value=sentinel) as direct_relay,
            patch.object(platform_app.kimi_completion_adapter, "complete") as agent_completion,
        ):
            response = platform_app.relay_model_chat_completion_stream(request)

        self.assertIs(response, sentinel)
        direct_relay.assert_called_once_with(request)
        agent_completion.assert_not_called()

    def test_relay_preserves_bounded_retry_after_for_upstream_overload(self) -> None:
        error = platform_app.HTTPError(
            "https://api.kimi.com/coding/v1/chat/completions",
            429,
            "Too Many Requests",
            {"Retry-After": "12"},
            BytesIO(b'{"error":{"message":"overloaded"}}'),
        )
        self.addCleanup(error.close)

        details = platform_app._model_relay_http_error_details(error)

        self.assertEqual(details["upstream_status"], 429)
        self.assertEqual(details["retry_after_seconds"], 12)

    def test_relay_retries_a_transient_connection_refusal(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-coding-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "ping"}],
        )
        refusal = platform_app.URLError(ConnectionRefusedError(61, "Connection refused"))
        with (
            patch.object(platform_app, "getproxies", return_value={}),
            patch.object(platform_app.time, "sleep") as sleeper,
            patch.object(
                platform_app,
                "urlopen",
                side_effect=[refusal, self._Response(b'{"choices":[{"message":{"content":"OK"}}]}')],
            ) as opener,
        ):
            response = platform_app._relay_chat_completion(request)

        self.assertEqual(response["choices"][0]["message"]["content"], "OK")
        self.assertEqual(opener.call_count, 2)
        sleeper.assert_called_once()

    def test_relay_bypasses_a_failed_loopback_proxy_once(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-coding-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "ping"}],
        )
        refusal = platform_app.URLError(ConnectionRefusedError(61, "Connection refused"))
        direct_urls: list[str] = []

        class DirectOpener:
            def open(self, upstream, **__: object) -> "ModelRelayTests._Response":
                direct_urls.append(upstream.full_url)
                return ModelRelayTests._Response(b'{"choices":[{"message":{"content":"DIRECT_OK"}}]}')

        def failed_proxy_open(upstream, **__: object):
            upstream.set_proxy("127.0.0.1:7897", "http")
            raise refusal

        with (
            patch.object(platform_app, "getproxies", return_value={"https": "http://127.0.0.1:7897"}),
            patch.object(platform_app.time, "sleep"),
            patch.object(platform_app, "urlopen", side_effect=failed_proxy_open) as opener,
            patch.object(platform_app, "build_opener", return_value=DirectOpener()) as direct_builder,
        ):
            response = platform_app._relay_chat_completion(request)

        self.assertEqual(response["choices"][0]["message"]["content"], "DIRECT_OK")
        self.assertEqual(opener.call_count, platform_app.MODEL_RELAY_CONNECT_ATTEMPTS)
        direct_builder.assert_called_once()
        self.assertEqual(direct_urls, ["https://api.kimi.com/coding/v1/chat/completions"])

    def test_relay_returns_a_classified_chinese_connection_error(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="test-coding-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "ping"}],
        )
        dns_failure = platform_app.URLError(platform_app.socket.gaierror(-2, "Name or service not known"))
        with (
            patch.object(platform_app, "getproxies", return_value={}),
            patch.object(platform_app.time, "sleep"),
            patch.object(platform_app, "urlopen", side_effect=dns_failure),
        ):
            with self.assertRaises(PlatformError) as raised:
                platform_app._relay_chat_completion(request)

        self.assertEqual(raised.exception.code, "MODEL_RELAY_UPSTREAM_UNAVAILABLE")
        self.assertIn("DNS 解析失败", raised.exception.message)
        self.assertEqual(raised.exception.details["failure_kind"], "dns")
        self.assertEqual(raised.exception.details["attempts"], platform_app.MODEL_RELAY_CONNECT_ATTEMPTS)

    def test_relay_rejects_arbitrary_url_before_network_access(self) -> None:
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="http://127.0.0.1:1234/v1",
            api_key="test-coding-key",
            model="local-model",
            messages=[{"role": "user", "content": "ping"}],
        )
        with patch.object(platform_app, "urlopen") as opener:
            with self.assertRaises(PlatformError) as raised:
                platform_app._relay_chat_completion(request)

        self.assertEqual(raised.exception.code, "MODEL_RELAY_PROVIDER_UNSUPPORTED")
        opener.assert_not_called()

    def test_managed_kimi_engine_accepts_custom_http_and_https_provider_roots(self) -> None:
        for value, expected in (
            ("https://models.example.internal/openai/v1/", "https://models.example.internal/openai/v1"),
            ("http://127.0.0.1:9000/v1/", "http://127.0.0.1:9000/v1"),
        ):
            with self.subTest(value=value):
                self.assertEqual(platform_app._kimi_provider_base_url(value), expected)

    def test_managed_kimi_engine_rejects_unsafe_or_endpoint_urls(self) -> None:
        for value in (
            "ftp://models.example.internal/v1",
            "https://user:secret@models.example.internal/v1",
            "https://models.example.internal:invalid/v1",
            "https://models.example.internal/v1;tenant=one",
            "https://models.example.internal/v1?tenant=one",
            "https://models.example.internal/v1/chat/completions",
        ):
            with self.subTest(value=value), self.assertRaises(PlatformError) as raised:
                platform_app._kimi_provider_base_url(value)
            self.assertEqual(raised.exception.code, "KIMI_PROVIDER_BASE_URL_INVALID")


if __name__ == "__main__":
    unittest.main()
