"""Administrator-owned shared runtime-library source for E0 builds.

Build profiles declare which library *names* they may link.  This module keeps
that logical allowlist separate from the physical SDK/runtime directory so one
read-only AArch64 library root can serve GPU, NPU and media profiles without
copying a bundle per profile.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .core import PlatformError, atomic_write_json, sha256_file, sha256_json, utc_now
from .device_experiment import EXPERIMENT_PROFILES


RUNTIME_LIBRARY_SOURCE_SCHEMA = "build_runtime_library_source/v1"
RUNTIME_LIBRARY_SOURCE_ENV = "HETERO_BUILD_RUNTIME_LIBRARY_ROOT"
RUNTIME_LIBRARY_SOURCE_RELATIVE_PATH = Path("build-worker") / "runtime-library-source.json"
ELF_MACHINE_AARCH64 = 183


def runtime_library_requirements() -> dict[str, list[str]]:
    """Return the fixed dynamic-library allowlist grouped by build profile."""

    return {
        profile_id: [local_name for _, local_name in profile["libraries"]]
        for profile_id, profile in EXPERIMENT_PROFILES.items()
        if profile["link_mode"] != "static"
    }


def known_runtime_library_names() -> list[str]:
    return sorted({name for names in runtime_library_requirements().values() for name in names})


def _runtime_library_record(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            header = stream.read(20)
    except OSError as exc:
        raise ValueError(f"runtime library cannot be read: {path.name}") from exc
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] not in {1, 2}:
        raise ValueError(f"runtime library is not a 64-bit ELF file: {path.name}")
    byte_order = "little" if header[5] == 1 else "big"
    machine = int.from_bytes(header[18:20], byte_order)
    if machine != ELF_MACHINE_AARCH64:
        raise ValueError(f"runtime library is not AArch64 ELF: {path.name} (e_machine={machine})")
    return {
        "name": path.name,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "elf_class": "ELF64",
        "elf_machine": "AArch64",
    }


def _inventory(library_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name in known_runtime_library_names():
        path = library_root / name
        if path.is_file():
            records.append(_runtime_library_record(path))
    return records


def _profile_statuses(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    available = {str(record["name"]) for record in records}
    statuses: list[dict[str, Any]] = []
    for profile_id, expected in runtime_library_requirements().items():
        missing = [name for name in expected if name not in available]
        statuses.append({
            "profile_id": profile_id,
            "status": "ready" if not missing else "missing_libraries",
            "expected_library_names": expected,
            "missing_library_names": missing,
        })
    return statuses


def inspect_runtime_library_root(library_root: Path) -> dict[str, Any]:
    root = library_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"runtime library root is not a directory: {root}")
    records = _inventory(root)
    if not records:
        expected = ", ".join(known_runtime_library_names())
        raise ValueError(f"runtime library root contains none of the known library names: {expected}")
    return {
        "schema_version": RUNTIME_LIBRARY_SOURCE_SCHEMA,
        "library_root": str(root),
        "libraries": records,
        "profiles": _profile_statuses(records),
    }


def configure_runtime_library_source(*, library_root: Path, state_root: Path) -> dict[str, Any]:
    """Persist one shared root and its reviewed content hashes without copying libraries."""

    inspection = inspect_runtime_library_root(library_root)
    registry_path = state_root.expanduser().resolve() / RUNTIME_LIBRARY_SOURCE_RELATIVE_PATH
    registry = {
        **inspection,
        "configured_at": utc_now(),
        "content_sha256": sha256_json({
            "schema_version": RUNTIME_LIBRARY_SOURCE_SCHEMA,
            "libraries": inspection["libraries"],
        }),
    }
    atomic_write_json(registry_path, registry)
    return {
        "status": "configured",
        "registry_path": str(registry_path),
        **registry,
    }


def _load_registry(state_root: Path) -> tuple[Path, dict[str, Any]] | None:
    registry_path = state_root.resolve() / RUNTIME_LIBRARY_SOURCE_RELATIVE_PATH
    if not registry_path.is_file():
        return None
    try:
        value = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformError(
            "BUILD_PROFILE_RUNTIME_INVALID",
            "共享运行库配置文件不可读取。",
            status_code=503,
            details={"registry_path": str(registry_path)},
        ) from exc
    if not isinstance(value, dict) or value.get("schema_version") != RUNTIME_LIBRARY_SOURCE_SCHEMA:
        raise PlatformError(
            "BUILD_PROFILE_RUNTIME_INVALID",
            "共享运行库配置文件 schema 不匹配。",
            status_code=503,
            details={"registry_path": str(registry_path)},
        )
    return registry_path, value


def resolve_shared_profile_libraries(
    *,
    state_root: Path,
    profile_id: str,
) -> tuple[list[Path], dict[str, Any]] | None:
    """Resolve a profile from the configured shared root.

    The environment override is useful for immutable deployments.  Otherwise
    the small state-root registry written by ``heteroctl runtime-libs`` is used.
    ``None`` means no shared source was configured, allowing legacy bundle
    compatibility in ``BuildWorker``.
    """

    configured_environment = os.environ.get(RUNTIME_LIBRARY_SOURCE_ENV, "").strip()
    registry_path: Path | None = None
    registry: dict[str, Any] | None = None
    if configured_environment:
        library_root = Path(configured_environment).expanduser().resolve()
        configuration_source = "environment"
    else:
        loaded = _load_registry(state_root)
        if loaded is None:
            return None
        registry_path, registry = loaded
        raw_root = registry.get("library_root")
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise PlatformError(
                "BUILD_PROFILE_RUNTIME_INVALID",
                "共享运行库配置缺少 library_root。",
                status_code=503,
                details={"registry_path": str(registry_path)},
            )
        library_root = Path(raw_root).expanduser().resolve()
        configuration_source = "registry"

    expected = runtime_library_requirements().get(profile_id, [])
    paths = [library_root / name for name in expected]
    missing = [path.name for path in paths if not path.is_file()]
    if not library_root.is_dir() or missing:
        raise PlatformError(
            "BUILD_PROFILE_RUNTIME_UNAVAILABLE",
            "共享运行库根目录尚未提供该 profile 所需的完整 AArch64 运行库。",
            status_code=503,
            details={
                "profile_id": profile_id,
                "runtime_library_source": "administrator_shared_root",
                "configured_root": str(library_root),
                "registry_path": str(registry_path) if registry_path else None,
                "expected_library_names": expected,
                "missing_library_names": missing if library_root.is_dir() else expected,
                "next_action_zh": "由平台管理员把目标 AArch64 OpenHarmony 运行库集中放入该目录并重新登记；无需创建按 profile 划分的目录。",
            },
        )

    try:
        records = [_runtime_library_record(path) for path in paths]
    except ValueError as exc:
        raise PlatformError(
            "BUILD_PROFILE_RUNTIME_INVALID",
            "共享运行库包含非 AArch64 ELF 文件，不能用于 OpenHarmony arm64 目标构建。",
            status_code=422,
            details={
                "profile_id": profile_id,
                "configured_root": str(library_root),
                "reason": str(exc),
                "next_action_zh": "请选择 AArch64-linux-ohos/arm64 目标库目录，不要使用 x86-linux-ohos 宿主库。",
            },
        ) from exc
    if registry is not None:
        declared = {
            str(item.get("name")): str(item.get("sha256"))
            for item in registry.get("libraries", [])
            if isinstance(item, dict) and item.get("name") and item.get("sha256")
        }
        mismatched = [
            str(record["name"])
            for record in records
            if declared.get(str(record["name"])) != record["sha256"]
        ]
        if mismatched:
            raise PlatformError(
                "BUILD_PROFILE_RUNTIME_INVALID",
                "共享运行库文件与管理员登记的哈希不一致。",
                status_code=409,
                details={
                    "profile_id": profile_id,
                    "configured_root": str(library_root),
                    "registry_path": str(registry_path),
                    "mismatched_library_names": mismatched,
                    "next_action_zh": "确认运行库更新来源后重新执行 runtime-libs 登记，不要绕过哈希校验。",
                },
            )

    content_identity = {
        "schema_version": RUNTIME_LIBRARY_SOURCE_SCHEMA,
        "profile_id": profile_id,
        "libraries": records,
    }
    return paths, {
        "source": "administrator_shared_runtime_root",
        "configuration_source": configuration_source,
        "profile_id": profile_id,
        "root": str(library_root),
        "registry_path": str(registry_path) if registry_path else None,
        "registry_sha256": sha256_file(registry_path) if registry_path else None,
        "bundle_sha256": sha256_json(content_identity),
        "libraries": records,
    }
