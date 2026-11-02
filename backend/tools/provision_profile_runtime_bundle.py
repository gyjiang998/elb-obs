"""Provision a hash-pinned runtime-library bundle for one E0 build profile.

This is an administrator utility, not an Agent action.  It copies only the
library names declared by ``EXPERIMENT_PROFILES`` into the Build Worker-owned
bundle root and writes the manifest required by ``BuildWorker``.  It never
contacts a device, downloads a library, or accepts model-provided paths.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
from pathlib import Path

from ..core import atomic_write_json, sha256_file, sha256_json
from ..device_experiment import EXPERIMENT_PROFILES


def _profile_version(profile_id: str) -> str:
    profile = EXPERIMENT_PROFILES[profile_id]
    return sha256_json({
        "profile_id": profile_id,
        "unit": profile["unit"],
        "language": profile["language"],
        "compiler": profile["compiler"],
        "target": profile["target"],
        "standard": profile["standard"],
        "include_dirs": list(profile["include_dirs"]),
        "libraries": list(profile["libraries"]),
        "link_mode": profile["link_mode"],
        "generated_headers": list(profile.get("generated_headers") or ()),
        "shader_contract": profile.get("shader_contract"),
    })


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def provision(*, profile_id: str, source_root: Path, state_root: Path, replace: bool) -> dict[str, object]:
    if profile_id not in EXPERIMENT_PROFILES:
        raise ValueError(f"unknown profile: {profile_id}")
    profile = EXPERIMENT_PROFILES[profile_id]
    expected = [local_name for _, local_name in profile["libraries"]]
    if not expected:
        raise ValueError(f"profile {profile_id} is static and does not require a runtime library bundle")
    source_root = source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise ValueError(f"source root is not a directory: {source_root}")
    sources = [source_root / name for name in expected]
    missing = [path.name for path in sources if not path.is_file()]
    if missing:
        raise ValueError(f"source root does not contain fixed runtime libraries: {', '.join(missing)}")
    target_root = state_root.expanduser().resolve() / "build-worker" / "profile-runtime-libraries" / profile_id
    target_root.mkdir(parents=True, exist_ok=True)
    copied: list[dict[str, object]] = []
    for source in sources:
        target = target_root / source.name
        source_hash = sha256_file(source)
        if target.is_file() and sha256_file(target) != source_hash and not replace:
            raise ValueError(f"existing bundle member differs: {target.name}; rerun with --replace after administrator review")
        if not target.is_file() or sha256_file(target) != source_hash:
            _copy_atomic(source, target)
        copied.append({"name": target.name, "sha256": sha256_file(target), "bytes": target.stat().st_size})
    manifest = {
        "schema_version": "build_profile_runtime_bundle/v1",
        "profile_id": profile_id,
        "profile_version": _profile_version(profile_id),
        "libraries": copied,
    }
    atomic_write_json(target_root / "bundle.json", manifest)
    return {
        "status": "provisioned",
        "profile_id": profile_id,
        "bundle_root": str(target_root),
        "bundle_manifest_sha256": sha256_file(target_root / "bundle.json"),
        "libraries": copied,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a fixed E0 Build Worker runtime-library bundle")
    parser.add_argument("--profile", required=True, choices=sorted(EXPERIMENT_PROFILES))
    parser.add_argument("--source-root", required=True, help="administrator-provisioned SDK/runtime directory containing the exact declared library file names")
    parser.add_argument("--state-root", required=True, help="platform .local-artifacts root")
    parser.add_argument("--replace", action="store_true", help="replace a differing existing bundle member after administrator review")
    args = parser.parse_args()
    try:
        result = provision(
            profile_id=args.profile,
            source_root=Path(args.source_root),
            state_root=Path(args.state_root),
            replace=bool(args.replace),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
