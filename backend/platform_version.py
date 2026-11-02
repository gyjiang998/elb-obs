"""Build and runtime identity used to diagnose mixed platform deployments."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .core import utc_now
from .mcp_contract import platform_mcp_contract
from .platform_paths import APP_ROOT


PLATFORM_RUNTIME_STARTED_AT = utc_now()
BUILD_MANIFEST_PATH = APP_ROOT / ".platform-build.json"


def _text(value: Any, limit: int = 512) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _package_version() -> str:
    return _text(_read_json(APP_ROOT / "package.json").get("version"), 80) or "unknown"


def _git_revision() -> str:
    configured = _text(os.environ.get("HETERO_SOURCE_REVISION"), 160)
    if configured:
        return configured
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=APP_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()[:160]
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def load_platform_build() -> dict[str, Any]:
    raw = _read_json(BUILD_MANIFEST_PATH)
    revision = _text(raw.get("source_revision"), 160) or _git_revision()
    version = _text(raw.get("version"), 80) or _package_version()
    build_id = _text(raw.get("build_id"), 120) or f"unbuilt-{revision[:12]}"
    return {
        "schema_version": "platform_build/v1",
        "product": "heterogeneous-enable-web",
        "version": version,
        "release": _text(raw.get("release"), 120) or f"v{version}",
        "build_id": build_id,
        "source_revision": revision,
        "source_revision_short": _text(raw.get("source_revision_short"), 24) or revision[:12],
        "source_fingerprint_sha256": _text(raw.get("source_fingerprint_sha256"), 80),
        "source_dirty": raw.get("source_dirty") is True,
        "built_at": _text(raw.get("built_at"), 80) or None,
    }


PLATFORM_BUILD = load_platform_build()


def platform_version() -> dict[str, Any]:
    mcp_contract = platform_mcp_contract(APP_ROOT)
    return {
        "schema_version": "platform_version/v1",
        "build": dict(PLATFORM_BUILD),
        "api_runtime": {
            "service": "heterogeneous-enablement-api",
            "started_at": PLATFORM_RUNTIME_STARTED_AT,
            "process_id": os.getpid(),
        },
        "contracts": {
            "agent_cli_adapter": "agent_cli_adapter/v1",
            "agent_cli_catalog": "agent_cli_adapter_catalog/v1",
            "platform_mcp": mcp_contract["platform_mcp"],
            "mcp_tool_count": mcp_contract["tool_count"],
            "mcp_facade_sha256": mcp_contract["facade_sha256"],
            "mcp_tool_catalog_sha256": mcp_contract["tool_catalog_sha256"],
            "required_reasoning_tools": [
                "reasoning_prepare_hardware_semantic_route",
                "reasoning_run_hardware_semantic_route",
            ],
        },
    }
