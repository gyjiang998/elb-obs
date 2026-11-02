"""Portable filesystem layout for the deployable platform repository.

The Git repository owns code, versioned knowledge and evaluation fixtures.
Mutable state, source checkouts, SDK headers and compiler binaries are runtime
resources and may live anywhere selected by the administrator.
"""

from __future__ import annotations

import os
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
AGENT_RESOURCES_ROOT = APP_ROOT / "agent-resources"
DATA_ROOT = APP_ROOT / "data"
KNOWLEDGE_ROOT = DATA_ROOT / "knowledge-bases"
OPTIMIZATION_KB_ROOT = KNOWLEDGE_ROOT / "optimization"
HARDWARE_KB_ROOT = KNOWLEDGE_ROOT / "hardware"
PATTERN_KB_ROOT = KNOWLEDGE_ROOT / "patterns"
EVALUATION_ROOT = DATA_ROOT / "evaluation"
ANALYSIS_TOOLS_ROOT = APP_ROOT / "tools" / "analysis"


def _environment_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return (Path(value).expanduser() if value else default).resolve()


def optional_environment_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else None


def state_root() -> Path:
    return _environment_path("HETERO_STATE_ROOT", APP_ROOT / ".local-artifacts")


def default_source_root() -> Path:
    # The platform repository is a small, valid Git connector for a fresh
    # installation. Real product source is added through the UI or this env.
    return _environment_path("HETERO_DEFAULT_SOURCE_ROOT", APP_ROOT)


def runtime_asset_root() -> Path:
    return _environment_path("HETERO_RUNTIME_ASSET_ROOT", state_root() / "runtime-assets")


def llvm_bin_root() -> Path:
    return _environment_path("HETERO_LLVM_BIN_ROOT", runtime_asset_root() / "llvm" / "bin")


def glslang_binary() -> Path:
    return _environment_path("HETERO_GLSLANG_BIN", runtime_asset_root() / "glslang" / "bin" / "glslang")


def openharmony_sdk_root() -> Path:
    """Return a compact SDK/header bundle root, not an OpenHarmony checkout."""

    return _environment_path("HETERO_OPENHARMONY_SDK_ROOT", runtime_asset_root() / "openharmony-sdk")
