from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.analysis_observability import AnalysisTraceStore
from backend.app import app


class AnalysisTraceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / ".local-artifacts" / "analysis-traces"
        self.store = AnalysisTraceStore(self.root)

    def test_trace_appends_full_events_and_redacts_credentials(self) -> None:
        self.store.append(
            "analysis-123",
            {
                "event_id": "event-1",
                "occurred_at": "2026-07-31T03:00:00Z",
                "category": "model_request",
                "operation": "device_validation.agent_decision",
                "status": "started",
                "payload": {
                    "messages": [{"role": "user", "content": "test"}],
                    "api_key": "must-not-be-written",
                    "nested": {"authorization": "Bearer secret"},
                    "token_usage": {"input_tokens": 12},
                },
            },
        )
        self.store.append(
            "analysis-123",
            {
                "event_id": "event-2",
                "occurred_at": "2026-07-31T03:00:01Z",
                "category": "model_error",
                "operation": "device_validation.agent_decision",
                "status": "failed",
                "payload": {"code": "REQUEST_VALIDATION_FAILED", "status_code": 422},
            },
        )

        trace = self.store.read("analysis-123")
        raw = self.store.download_path("analysis-123").read_text(encoding="utf-8")
        raw_events = [json.loads(line) for line in raw.splitlines()]
        catalog = self.store.catalog(order_by="created_at")

        self.assertEqual(trace["event_count"], 2)
        self.assertEqual(trace["events"][0]["payload"]["api_key"], "[已脱敏]")
        self.assertEqual(trace["events"][0]["payload"]["nested"]["authorization"], "[已脱敏]")
        self.assertEqual(trace["events"][0]["payload"]["token_usage"]["input_tokens"], 12)
        self.assertNotIn("must-not-be-written", raw)
        self.assertNotIn("Bearer secret", raw)
        self.assertEqual(len(raw_events), 2)
        self.assertEqual(catalog[0]["created_at"], raw_events[0]["received_at"])


class RequestValidationDiagnosticsTests(unittest.TestCase):
    def test_model_relay_message_limit_returns_safe_field_diagnostic(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/v1/model-relay/chat-completions",
            json={
                "base_url": "https://api.kimi.com/coding/v1",
                "api_key": "secret-key-must-not-be-reflected",
                "model": "kimi-for-coding",
                "messages": [{"role": "user", "content": f"message-{index}"} for index in range(25)],
            },
        )

        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "REQUEST_VALIDATION_FAILED")
        error = payload["error"]["details"]["validation_errors"][0]
        self.assertEqual(error["field"], "messages")
        self.assertEqual(error["context"]["max_length"], 24)
        self.assertEqual(error["context"]["actual_length"], 25)
        self.assertNotIn("secret-key-must-not-be-reflected", response.text)
        self.assertNotIn("message-24", response.text)
