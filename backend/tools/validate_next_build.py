"""Validate that a Next.js production build is internally complete."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


ASSET_INDEX_NAME = ".hetero-current-static-assets"


class NextBuildValidationError(RuntimeError):
    """Raised when a production build cannot safely be served."""


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _strings(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NextBuildValidationError(f"invalid JSON manifest: {path}") from exc


def _safe_static_reference(value: str) -> str | None:
    normalized = value.split("?", 1)[0].lstrip("/")
    if normalized.startswith("_next/"):
        normalized = normalized[len("_next/") :]
    if not normalized.startswith("static/"):
        return None
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise NextBuildValidationError(f"unsafe static asset reference: {value}")
    return path.as_posix()


def validate_next_build(build_dir: Path) -> dict[str, Any]:
    root = build_dir.expanduser().resolve()
    build_id_path = root / "BUILD_ID"
    build_manifest_path = root / "build-manifest.json"
    app_paths_path = root / "server" / "app-paths-manifest.json"
    static_root = root / "static"

    if not build_id_path.is_file() or not build_id_path.read_text(encoding="utf-8").strip():
        raise NextBuildValidationError(f"missing or empty BUILD_ID: {build_id_path}")
    if not build_manifest_path.is_file():
        raise NextBuildValidationError(f"missing build manifest: {build_manifest_path}")
    if not app_paths_path.is_file():
        raise NextBuildValidationError(f"missing App Router manifest: {app_paths_path}")
    if not static_root.is_dir():
        raise NextBuildValidationError(f"missing static asset directory: {static_root}")

    build_manifest = _load_json(build_manifest_path)
    root_main_files = build_manifest.get("rootMainFiles") if isinstance(build_manifest, dict) else None
    if not isinstance(root_main_files, list) or not root_main_files:
        raise NextBuildValidationError("build-manifest.json has no rootMainFiles")

    app_paths = _load_json(app_paths_path)
    if not isinstance(app_paths, dict) or "/page" not in app_paths:
        raise NextBuildValidationError("App Router manifest does not contain /page")

    manifest_paths = sorted(
        path
        for path in root.rglob("*.json")
        if "manifest" in path.name.casefold()
        and path.is_file()
        and "dev" not in path.relative_to(root).parts
    )
    references: set[str] = set()
    for manifest_path in manifest_paths:
        payload = _load_json(manifest_path)
        for value in _strings(payload):
            reference = _safe_static_reference(value)
            if reference:
                references.add(reference)

    missing = sorted(reference for reference in references if not (root / reference).is_file())
    if missing:
        preview = ", ".join(missing[:8])
        suffix = "" if len(missing) <= 8 else f" (+{len(missing) - 8} more)"
        raise NextBuildValidationError(f"{len(missing)} referenced static assets are missing: {preview}{suffix}")

    current_assets = sorted(
        path.relative_to(static_root).as_posix()
        for path in static_root.rglob("*")
        if path.is_file()
    )
    if not any(path.endswith(".js") for path in current_assets):
        raise NextBuildValidationError("production build contains no JavaScript assets")
    if not any(path.endswith(".css") for path in current_assets):
        raise NextBuildValidationError("production build contains no CSS assets")

    return {
        "status": "ready",
        "build_dir": str(root),
        "build_id": build_id_path.read_text(encoding="utf-8").strip(),
        "manifest_count": len(manifest_paths),
        "referenced_asset_count": len(references),
        "static_asset_count": len(current_assets),
        "current_assets": current_assets,
    }


def write_asset_index(build_dir: Path, assets: list[str]) -> Path:
    path = build_dir.expanduser().resolve() / ASSET_INDEX_NAME
    path.write_text("".join(f"{asset}\n" for asset in assets), encoding="utf-8")
    return path


def retain_previous_static_assets(build_dir: Path, previous_build_dir: Path) -> int:
    current_root = build_dir.expanduser().resolve() / "static"
    previous_root = previous_build_dir.expanduser().resolve() / "static"
    if not previous_root.is_dir():
        return 0

    index_path = previous_build_dir.expanduser().resolve() / ASSET_INDEX_NAME
    if index_path.is_file():
        candidates = [line.strip() for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        candidates = [
            path.relative_to(previous_root).as_posix()
            for path in previous_root.rglob("*")
            if path.is_file()
        ]

    retained = 0
    for candidate in candidates:
        relative = PurePosixPath(candidate)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            continue
        source = previous_root.joinpath(*relative.parts)
        destination = current_root.joinpath(*relative.parts)
        if not source.is_file() or destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        retained += 1
    return retained


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("--write-asset-index", action="store_true")
    parser.add_argument("--retain-from")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = validate_next_build(Path(args.build_dir))
        if args.write_asset_index:
            result["asset_index"] = str(write_asset_index(Path(args.build_dir), result["current_assets"]))
        if args.retain_from:
            result["retained_previous_assets"] = retain_previous_static_assets(
                Path(args.build_dir), Path(args.retain_from)
            )
    except (OSError, UnicodeError, NextBuildValidationError) as exc:
        if args.json:
            print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"Next.js build validation failed: {exc}")
        return 1

    if args.json:
        printable = {key: value for key, value in result.items() if key != "current_assets"}
        print(json.dumps(printable, ensure_ascii=False))
    else:
        print(
            "Next.js build validated: "
            f"build_id={result['build_id']}, manifests={result['manifest_count']}, "
            f"referenced_assets={result['referenced_asset_count']}, "
            f"static_assets={result['static_asset_count']}, "
            f"retained_previous_assets={result.get('retained_previous_assets', 0)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
