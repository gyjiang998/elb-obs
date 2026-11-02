"""Extract a compact OpenHarmony SDK/header bundle for the Build Worker."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

from ..core import atomic_write_json, sha256_file


SDK_DIRECTORY_PATHS = (
    Path("interface/sdk_c/ai"),
    Path("interface/sdk_c/graphic/graphic_2d"),
    Path("interface/sdk_c/multimedia/av_codec"),
    Path("interface/sdk_c/multimedia/media_foundation"),
    Path("interface/sdk_c/third_party/egl"),
    Path("interface/sdk_c/third_party/openGLES"),
    Path("interface/sdk_c/third_party/vulkan-headers"),
    Path("interface/sdk_c/third_party/musl/ndk_musl_include"),
    Path("third_party/musl/arch/aarch64"),
    Path("third_party/musl/arch/generic"),
)
SDK_FILE_PATHS = (Path("third_party/musl/include/alltypes.h.in"),)
SDK_PATHS = (*SDK_DIRECTORY_PATHS, *SDK_FILE_PATHS)


def resolve_source_root(requested: Path) -> Path:
    """Accept either an OpenHarmony checkout or its immediate parent."""

    resolved = requested.expanduser().resolve()
    nested = resolved / "openharmony"
    return nested if nested.is_dir() else resolved


def resolve_destination(*, destination: Path | None, runtime_root: Path | None) -> Path:
    if (destination is None) == (runtime_root is None):
        raise ValueError("exactly one of destination or runtime_root is required")
    if runtime_root is not None:
        return runtime_root.expanduser().resolve() / "openharmony-sdk"
    assert destination is not None
    return destination.expanduser().resolve()


def missing_sdk_paths(source: Path) -> list[str]:
    missing: list[str] = []
    for relative in SDK_DIRECTORY_PATHS:
        if not (source / relative).is_dir():
            missing.append(relative.as_posix())
    for relative in SDK_FILE_PATHS:
        if not (source / relative).is_file():
            missing.append(relative.as_posix())
    return missing


def inspect_sdk_bundle(root: Path) -> dict[str, Any]:
    """Inspect a source checkout or generated bundle against the canonical surface."""

    resolved = root.expanduser().resolve()
    missing = missing_sdk_paths(resolved)
    manifest_path = resolved / "bundle-manifest.json"
    manifest: dict[str, Any] | None = None
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = {
                    "schema_version": loaded.get("schema_version"),
                    "source_root_name": loaded.get("source_root_name"),
                    "file_count": loaded.get("file_count"),
                }
        except (OSError, UnicodeError, json.JSONDecodeError):
            manifest = None
    return {
        "status": "ready" if not missing else "unavailable",
        "sdk_root": str(resolved),
        "missing_paths": missing,
        "manifest": manifest,
        "manifest_path": str(manifest_path) if manifest_path.is_file() else None,
        "manifest_sha256": sha256_file(manifest_path) if manifest_path.is_file() else None,
    }


def _copy_sdk_surface(source: Path, destination: Path) -> None:
    for relative in SDK_DIRECTORY_PATHS:
        shutil.copytree(source / relative, destination / relative)
    for relative in SDK_FILE_PATHS:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)


def _bundle_manifest(source: Path, destination: Path) -> dict[str, Any]:
    records = [
        {
            "path": path.relative_to(destination).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(destination.rglob("*"))
        if path.is_file() and path.name != "bundle-manifest.json"
    ]
    return {
        "schema_version": "openharmony_sdk_header_bundle/v1",
        "source_root_name": source.name,
        "file_count": len(records),
        "files": records,
    }


def _replace_directory(temporary: Path, destination: Path) -> None:
    """Install a complete bundle while retaining the old one until rename."""

    backup = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.backup")
    had_destination = destination.exists() or destination.is_symlink()
    if had_destination:
        os.replace(destination, backup)
    try:
        os.replace(temporary, destination)
    except BaseException:
        if had_destination and (backup.exists() or backup.is_symlink()):
            os.replace(backup, destination)
        raise
    finally:
        if backup.is_symlink() or backup.is_file():
            backup.unlink(missing_ok=True)
        elif backup.is_dir():
            shutil.rmtree(backup)


def provision(*, source_root: Path, destination: Path) -> dict[str, Any]:
    source = resolve_source_root(source_root)
    if not source.is_dir():
        raise ValueError(f"OpenHarmony source root is not a directory: {source}")
    missing = missing_sdk_paths(source)
    if missing:
        raise ValueError(
            "OpenHarmony source root is missing required SDK paths: " + ", ".join(missing)
        )

    target = destination.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.mkdir()
        _copy_sdk_surface(source, temporary)
        manifest = _bundle_manifest(source, temporary)
        atomic_write_json(temporary / "bundle-manifest.json", manifest)
        _replace_directory(temporary, target)
    finally:
        if temporary.is_dir():
            shutil.rmtree(temporary)

    return {
        "status": "ready",
        "runtime_root": str(target.parent),
        "sdk_root": str(target),
        "environment": {"HETERO_OPENHARMONY_SDK_ROOT": str(target)},
        "file_count": manifest["file_count"],
        "manifest": str(target / "bundle-manifest.json"),
        "manifest_sha256": sha256_file(target / "bundle-manifest.json"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision the compact OpenHarmony SDK/header bundle used by the Build Worker"
    )
    parser.add_argument(
        "--source-root",
        required=True,
        help="OpenHarmony checkout, or an immediate parent containing openharmony/",
    )
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument(
        "--runtime-root",
        help="platform runtime root; the bundle is installed as <runtime-root>/openharmony-sdk",
    )
    output.add_argument(
        "--destination",
        help="backward-compatible explicit OpenHarmony SDK bundle destination",
    )
    args = parser.parse_args()
    try:
        destination = resolve_destination(
            destination=Path(args.destination) if args.destination else None,
            runtime_root=Path(args.runtime_root) if args.runtime_root else None,
        )
        result = provision(source_root=Path(args.source_root), destination=destination)
    except ValueError as exc:
        print(json.dumps({"status": "blocked", "message": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
