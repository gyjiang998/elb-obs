from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from backend.agent_cli_adapters import AgentCliAdapterError
from backend.tools.validate_agent_cli_adapters import validate_catalog


class AgentCliAdapterValidationTests(unittest.TestCase):
    @staticmethod
    def _executable(path: Path) -> Path:
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_valid_custom_default_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = self._executable(root / "codeagent")
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-codeagent",
                "display_name": "Team CodeAgent",
                "executable": str(binary),
            }]}), encoding="utf-8")

            catalog = validate_catalog(
                manifest=manifest,
                default_adapter="team-codeagent",
                kimi_bin=root / "missing-kimi",
            )

            self.assertEqual(catalog["default_adapter_id"], "team-codeagent")
            selected = next(item for item in catalog["adapters"] if item["adapter_id"] == "team-codeagent")
            self.assertTrue(selected["available"])

    def test_missing_manifest_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(AgentCliAdapterError) as captured:
                validate_catalog(
                    manifest=root / "missing.json",
                    default_adapter="team-codeagent",
                    kimi_bin=root / "missing-kimi",
                )

            self.assertEqual(captured.exception.code, "AGENT_CLI_MANIFEST_MISSING")

    def test_unregistered_default_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "adapters.json"
            manifest.write_text('{"adapters": []}\n', encoding="utf-8")

            with self.assertRaises(AgentCliAdapterError) as captured:
                validate_catalog(
                    manifest=manifest,
                    default_adapter="team-codeagent",
                    kimi_bin=root / "missing-kimi",
                )

            self.assertEqual(captured.exception.code, "AGENT_CLI_ADAPTER_NOT_FOUND")

    def test_missing_builtin_kimi_can_be_allowed_for_core_platform_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            catalog = validate_catalog(
                manifest=None,
                default_adapter="kimi",
                kimi_bin=root / "missing-kimi",
                allow_missing_kimi=True,
            )

            selected = next(item for item in catalog["adapters"] if item["adapter_id"] == "kimi")
            self.assertFalse(selected["available"])

    def test_missing_custom_default_remains_an_error_when_kimi_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "adapters.json"
            manifest.write_text(json.dumps({"adapters": [{
                "adapter_id": "team-codeagent",
                "display_name": "Team CodeAgent",
                "executable": str(root / "missing-codeagent"),
            }]}), encoding="utf-8")

            with self.assertRaises(AgentCliAdapterError) as captured:
                validate_catalog(
                    manifest=manifest,
                    default_adapter="team-codeagent",
                    kimi_bin=root / "missing-kimi",
                    allow_missing_kimi=True,
                )

            self.assertEqual(captured.exception.code, "AGENT_CLI_BINARY_MISSING")


if __name__ == "__main__":
    unittest.main()
