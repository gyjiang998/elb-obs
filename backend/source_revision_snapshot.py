#!/usr/bin/env python3
"""Create a deterministic multi-repository source revision snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True)
    if completed.returncode:
        raise ValueError(f"git -C {repo} {' '.join(args)}: {completed.stderr.strip()}")
    return completed.stdout


def _git_bytes(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if completed.returncode:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git -C {repo} {' '.join(args)}: {stderr}")
    return completed.stdout


def _git_null_paths(repo: Path, *args: str) -> list[str]:
    """Read Git's NUL-delimited path output without filename quoting ambiguity."""

    return [os.fsdecode(item) for item in _git_bytes(repo, *args).split(b"\0") if item]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _worktree_path_record(repo: Path, relative_path: str) -> Dict[str, Any]:
    """Fingerprint the worktree representation that local AST analysis reads."""

    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe Git worktree path: {relative_path!r}")
    candidate = repo / path
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return {"path": relative_path, "kind": "missing", "mode": None, "sha256": None}
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISREG(metadata.st_mode):
        return {"path": relative_path, "kind": "file", "mode": mode, "sha256": _hash_file(candidate)}
    if stat.S_ISLNK(metadata.st_mode):
        target = os.fsencode(os.readlink(candidate))
        return {"path": relative_path, "kind": "symlink", "mode": mode, "sha256": hashlib.sha256(target).hexdigest()}
    return {"path": relative_path, "kind": "other", "mode": mode, "sha256": None}


def _repository_worktree_fingerprint(repo: Path) -> Dict[str, Any]:
    """Hash changed tracked and untracked worktree files, not only their status."""

    changed_paths: set[str] = set()
    for command in (
        ("diff", "--name-only", "--no-renames", "-z"),
        ("diff", "--cached", "--name-only", "--no-renames", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        changed_paths.update(_git_null_paths(repo, *command))
    records = [_worktree_path_record(repo, path) for path in sorted(changed_paths)]
    material = json.dumps(records, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "worktree_sha256": hashlib.sha256(material).hexdigest(),
        "changed_path_count": len(records),
    }


def build_worktree_fingerprint(
    source_root: Path,
    *,
    repositories: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Build a content fingerprint for the dirty portion of a multi-repo tree.

    This is the binding-friendly aggregate form of the content hashes that a
    v1.1 source snapshot records for each dirty repository.  It makes the
    local-edit evidence explicit without exposing source paths or contents in
    a trace-to-source binding artifact.
    """

    source_root = source_root.resolve()
    repository_records = repositories
    if repository_records is None:
        repository_records = []
        for repo in discover_repositories(source_root):
            status = _git(repo, "status", "--porcelain=v1", "-z")
            repository_records.append({"path": str(repo.relative_to(source_root)) or ".", "dirty": bool(status)})
    fingerprints: List[Dict[str, Any]] = []
    for record in repository_records:
        if not isinstance(record, dict) or not record.get("dirty"):
            continue
        relative = record.get("path")
        if not isinstance(relative, str) or not relative:
            continue
        repo = source_root if relative == "." else source_root / relative
        if not repo.is_dir():
            raise ValueError(f"dirty repository path is unavailable: {relative}")
        fingerprint = _repository_worktree_fingerprint(repo)
        fingerprints.append({"path": relative, **fingerprint})
    material = json.dumps(fingerprints, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "algorithm": "git_dirty_worktree_content_v1",
        "sha256": hashlib.sha256(material).hexdigest(),
        "dirty_repository_count": len(fingerprints),
        "changed_path_count": sum(int(item["changed_path_count"]) for item in fingerprints),
        "repositories": fingerprints,
    }


def discover_repositories(source_root: Path) -> List[Path]:
    repos: List[Path] = []
    for current, dirs, _ in os.walk(source_root):
        path = Path(current)
        if ".git" in dirs or (path / ".git").is_file():
            repos.append(path)
            dirs[:] = []
        else:
            dirs[:] = [name for name in dirs if name not in {".git", "out", "build", "node_modules"}]
    return sorted(set(repos))


def build_snapshot(source_root: Path, *, require_clean: bool = False) -> Dict[str, Any]:
    source_root = source_root.resolve()
    repos = discover_repositories(source_root)
    if not repos:
        raise ValueError("no Git repositories found below source root")
    records: List[Dict[str, Any]] = []
    for repo in repos:
        status = _git(repo, "status", "--porcelain=v1", "-z")
        dirty = bool(status)
        if require_clean and dirty:
            raise ValueError(f"repository is dirty: {repo.relative_to(source_root)}")
        record: Dict[str, Any] = {
            "path": str(repo.relative_to(source_root)) or ".",
            "head": _git(repo, "rev-parse", "HEAD").strip(),
            "dirty": dirty,
            "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        }
        # The porcelain status alone is insufficient for a mutable local
        # workspace: replacing one modified file with another edit can leave
        # the same `M` status.  Include the effective worktree representation
        # so a v1.1 snapshot remains an exact development snapshot without a
        # compulsory Git commit.
        if dirty:
            record.update(_repository_worktree_fingerprint(repo))
        records.append(record)
    material = json.dumps(records, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "artifact_type": "source_revision_snapshot",
        "schema_version": "1.1",
        "snapshot_id": f"source_snapshot_{hashlib.sha256(material).hexdigest()[:16]}",
        "source_root": source_root.name,
        "repositories": records,
        "clean": not any(record["dirty"] for record in records),
    }


def validate_snapshot(snapshot: Any, *, require_clean: bool = False) -> List[str]:
    """Validate a stored snapshot without consulting the current filesystem."""
    errors: List[str] = []
    if not isinstance(snapshot, dict) or snapshot.get("artifact_type") != "source_revision_snapshot" or snapshot.get("schema_version") not in {"1.0", "1.1"}:
        return ["unsupported source snapshot artifact"]
    records = snapshot.get("repositories")
    if not isinstance(records, list) or not records:
        return ["source snapshot requires repositories"]
    if any(not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"] or not isinstance(item.get("head"), str) or len(item["head"]) != 40 or not isinstance(item.get("dirty"), bool) or not isinstance(item.get("status_sha256"), str) or len(item["status_sha256"]) != 64 for item in records):
        return ["source snapshot has invalid repository record"]
    if snapshot.get("schema_version") == "1.1":
        for item in records:
            if not item["dirty"]:
                continue
            if not isinstance(item.get("worktree_sha256"), str) or len(item["worktree_sha256"]) != 64:
                errors.append("dirty v1.1 source snapshot repository lacks worktree_sha256")
            if not isinstance(item.get("changed_path_count"), int) or item["changed_path_count"] < 0:
                errors.append("dirty v1.1 source snapshot repository has invalid changed_path_count")
    material = json.dumps(records, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = f"source_snapshot_{hashlib.sha256(material).hexdigest()[:16]}"
    if snapshot.get("snapshot_id") != expected:
        errors.append("source snapshot_id does not match repositories")
    if snapshot.get("clean") != (not any(item["dirty"] for item in records)):
        errors.append("source snapshot clean flag does not match repositories")
    if require_clean and not snapshot.get("clean"):
        errors.append("source snapshot is not clean")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a deterministic multi-repository source revision snapshot")
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    root = Path(args.source_root)
    if not root.is_dir():
        print("ERROR: source root does not exist", file=sys.stderr)
        return 2
    try:
        snapshot = build_snapshot(root, require_clean=args.require_clean)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(f"Source snapshot: repos={len(snapshot['repositories'])} clean={snapshot['clean']} id={snapshot['snapshot_id']} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
