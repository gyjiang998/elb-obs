#!/usr/bin/env python3
"""Validate layered heterogeneous-KB records and their cross-layer references.

This validator intentionally uses only the Python standard library so it can be
used in CI before optional analysis dependencies are installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


LAYER_SPECS: Dict[str, Tuple[str, Tuple[str, ...], str]] = {
    "code_signature_patterns.json": ("pattern_id", ("category", "algorithm", "name", "features", "provenance"), "code_algorithm_signature"),
    "algorithm_hardware_mapping.json": ("mapping_id", ("category", "algorithm", "name", "target_unit", "hardware_id", "cost_model_id", "provenance"), "algorithm_to_heterogeneous_mapping"),
    "hardware_capabilities.json": ("hardware_id", ("category", "unit_type", "entry_points", "constraints", "provenance"), "hardware_capability"),
    "cost_models.json": ("cost_model_id", ("category", "applies_to", "positive_factors", "negative_factors", "decision_rules", "provenance"), "cost_model"),
    "fallback_pairs.json": ("pair_id", ("category", "algorithm", "target_unit", "cpu_impl", "fallback_impl", "fallback_execution_domain", "hw_impl", "dispatcher", "dispatcher_type", "dispatch_conditions", "fallback_conditions", "failure_reasons", "fallback_guarantee", "evidence", "provenance"), "fallback_pair"),
    "existing_heterogeneous_implementation_cases.json": (
        "case_id",
        (
            "category",
            "name",
            "module",
            "algorithm",
            "target_unit",
            "case_scope",
            "implementation_kind",
            "execution_roles",
            "source_anchors",
            "constraints",
            "fallback",
            "evidence",
            "provenance",
        ),
        "existing_heterogeneous_implementation",
    ),
    "runtime_hotspot_rules.json": ("rule_id", ("category", "applies_to", "conditions", "provenance"), "runtime_hotspot_rule"),
}
MANIFEST_NAME = "kb_manifest.json"

DISPATCHER_TYPES = {"capability_guard", "status_fallback", "capability_guard_with_status_fallback", "feature_switch", "unknown"}
FALLBACK_GUARANTEES = {"required", "best_effort", "unknown"}
FALLBACK_EXECUTION_DOMAINS = {"cpu", "software", "gpu", "heterogeneous", "unknown"}
EVIDENCE_CONFIDENCE = {"candidate", "confirmed", "unverified"}
PROVENANCE_ORIGINS = {"source_code", "hardware_documentation", "benchmark", "manual_review", "seed_example"}
SOURCE_REFERENCE_RE = re.compile(r"^(?P<path>[^:]+):(?P<line>[1-9][0-9]*)$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
IMPLEMENTATION_CASE_SCOPES = {"end_to_end", "dispatch_component", "execution_component", "data_path_component"}
IMPLEMENTATION_KINDS = {
    "hardware_codec_offload",
    "accelerated_image_processing",
    "specialized_post_processing",
    "gpu_render_path",
    "gpu_interop",
    "hardware_video_codec",
    "nn_device_runtime",
}
IMPLEMENTATION_EXECUTION_ROLES = {
    "accelerator_executor",
    "buffer_interop",
    "cache",
    "capability_guard",
    "data_path",
    "dispatcher",
    "fallback",
    "scheduler",
    "synchronization",
    "task_submission",
}


def _read_records(path: Path, errors: List[str]) -> List[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{path.name}: invalid JSON: {exc}")
        return []
    if not isinstance(data, list):
        errors.append(f"{path.name}: top level must be a JSON array")
        return []
    return [item for item in data if isinstance(item, dict)]


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(_non_empty_string(item) for item in value)


def _discover_workspace_root(kb_root: Path) -> Optional[Path]:
    """Find the workspace that owns source_code/, if this is a checked-out KB."""
    for candidate in (kb_root, *kb_root.parents):
        if (candidate / "source_code").is_dir():
            return candidate
    return None


def _validate_source_references(
    references: List[str],
    where: str,
    errors: List[str],
    source_root: Optional[Path],
    line_count_cache: Dict[Path, int],
    validate_source_files: bool,
) -> None:
    if not references:
        errors.append(f"{where}: confirmed source_code provenance requires at least one reference")
        return
    if source_root is None:
        if validate_source_files:
            errors.append(f"{where}: cannot validate source_code provenance; provide --source-root")
        return
    for reference in references:
        match = SOURCE_REFERENCE_RE.fullmatch(reference)
        if match is None:
            errors.append(f"{where}: source reference must use relative_path:line, got {reference!r}")
            continue
        source_path = source_root / match.group("path")
        if not source_path.is_file():
            errors.append(f"{where}: source reference file not found: {reference}")
            continue
        if source_path not in line_count_cache:
            try:
                with source_path.open("rb") as source_file:
                    line_count_cache[source_path] = sum(1 for _ in source_file)
            except OSError as exc:
                errors.append(f"{where}: cannot read source reference {reference}: {exc}")
                continue
        if int(match.group("line")) > line_count_cache[source_path]:
            errors.append(f"{where}: source reference line is out of range: {reference}")


def _validate_source_fingerprints(
    provenance: Dict[str, Any],
    where: str,
    errors: List[str],
    source_root: Optional[Path],
) -> None:
    """Bind confirmed source knowledge to the exact reviewed file contents."""
    references = provenance.get("references", [])
    referenced_paths = {match.group("path") for reference in references if (match := SOURCE_REFERENCE_RE.fullmatch(reference))}
    fingerprints = provenance.get("file_sha256")
    if not isinstance(fingerprints, dict) or not fingerprints:
        errors.append(f"{where}: confirmed source_code provenance requires file_sha256")
        return
    if set(fingerprints) != referenced_paths:
        errors.append(f"{where}: file_sha256 keys must exactly match referenced source files")
        return
    for relative_path, expected_hash in fingerprints.items():
        if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
            errors.append(f"{where}: invalid sha256 for {relative_path}")
            continue
        if source_root is None:
            continue
        source_path = source_root / relative_path
        if not source_path.is_file():
            continue
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            errors.append(f"{where}: source content hash changed: {relative_path}")


def _validate_condition_list(value: Any, label: str, where: str, errors: List[str], *, non_empty: bool) -> None:
    if not isinstance(value, list) or (non_empty and not value):
        errors.append(f"{where}: {label} must be a {'non-empty ' if non_empty else ''}array")
        return
    for index, condition in enumerate(value):
        if not isinstance(condition, dict) or not _non_empty_string(condition.get("kind")) or not _non_empty_string(condition.get("expression")):
            errors.append(f"{where}: {label}[{index}] requires non-empty kind and expression")


def _validate_layer(
    name: str,
    records: Iterable[Dict[str, Any]],
    errors: List[str],
    warnings: List[str],
    source_root: Optional[Path],
    line_count_cache: Dict[Path, int],
    validate_source_files: bool,
) -> Set[str]:
    id_key, required, category = LAYER_SPECS[name]
    ids: Set[str] = set()
    for index, item in enumerate(records):
        where = f"{name}[{index}]"
        record_id = item.get(id_key)
        if not _non_empty_string(record_id):
            errors.append(f"{where}: {id_key} must be a non-empty string")
            continue
        if record_id in ids:
            errors.append(f"{where}: duplicate {id_key}: {record_id}")
        ids.add(record_id)
        for field in required:
            if field not in item:
                errors.append(f"{where}: missing required field {field}")
        if item.get("category") != category:
            errors.append(f"{where}: category must be {category}")
        provenance = item.get("provenance")
        if (
            not isinstance(provenance, dict)
            or provenance.get("origin") not in PROVENANCE_ORIGINS
            or provenance.get("confidence") not in EVIDENCE_CONFIDENCE
            or not isinstance(provenance.get("references"), list)
            or not all(_non_empty_string(ref) for ref in provenance.get("references", []))
        ):
            errors.append(f"{where}: provenance requires supported origin/confidence and a string references array")
        elif provenance["confidence"] != "confirmed":
            warnings.append(f"{where}: record provenance is not confirmed and must not be treated as production knowledge")
        elif not provenance["references"]:
            warnings.append(f"{where}: confirmed provenance has no source reference and must not be treated as production knowledge")
        elif provenance["origin"] == "source_code":
            _validate_source_references(
                provenance["references"], where, errors, source_root, line_count_cache, validate_source_files
            )
            if provenance["confidence"] == "confirmed":
                _validate_source_fingerprints(provenance, where, errors, source_root)

        if name == "algorithm_hardware_mapping.json":
            if not isinstance(item.get("threshold", 0.0), (int, float)) or not 0 <= item.get("threshold", 0.0) <= 1:
                errors.append(f"{where}: threshold must be in [0, 1]")
            if "weights" in item and (not isinstance(item["weights"], dict) or any(not isinstance(v, (int, float)) or v < 0 for v in item["weights"].values())):
                errors.append(f"{where}: weights must be non-negative numbers")
        elif name == "hardware_capabilities.json" and not _string_list(item.get("entry_points")):
            errors.append(f"{where}: entry_points must be a non-empty string array")
        elif name == "cost_models.json":
            for field in ("applies_to", "positive_factors", "negative_factors", "decision_rules"):
                if not _string_list(item.get(field)):
                    errors.append(f"{where}: {field} must be a non-empty string array")
        elif name == "fallback_pairs.json":
            if item.get("fallback_execution_domain") not in FALLBACK_EXECUTION_DOMAINS:
                errors.append(f"{where}: unsupported fallback_execution_domain")
            if item.get("dispatcher_type") not in DISPATCHER_TYPES:
                errors.append(f"{where}: unsupported dispatcher_type")
            if item.get("fallback_guarantee") not in FALLBACK_GUARANTEES:
                errors.append(f"{where}: unsupported fallback_guarantee")
            _validate_condition_list(item.get("dispatch_conditions"), "dispatch_conditions", where, errors, non_empty=False)
            _validate_condition_list(item.get("fallback_conditions"), "fallback_conditions", where, errors, non_empty=True)
            if not _string_list(item.get("failure_reasons")):
                errors.append(f"{where}: failure_reasons must be a non-empty string array")
            evidence = item.get("evidence")
            if not isinstance(evidence, dict) or not _non_empty_string(evidence.get("origin")) or evidence.get("confidence") not in EVIDENCE_CONFIDENCE:
                errors.append(f"{where}: evidence requires origin and supported confidence")
            elif evidence["confidence"] != "confirmed":
                warnings.append(f"{where}: fallback pair is not confirmed and must not be treated as production evidence")
        elif name == "existing_heterogeneous_implementation_cases.json":
            if item.get("case_scope") not in IMPLEMENTATION_CASE_SCOPES:
                errors.append(f"{where}: unsupported case_scope")
            if item.get("implementation_kind") not in IMPLEMENTATION_KINDS:
                errors.append(f"{where}: unsupported implementation_kind")
            roles = item.get("execution_roles")
            if not isinstance(roles, list) or not roles or any(role not in IMPLEMENTATION_EXECUTION_ROLES for role in roles):
                errors.append(f"{where}: execution_roles must be a non-empty array of supported roles")
            if not _string_list(item.get("constraints")):
                errors.append(f"{where}: constraints must be a non-empty string array")
            implementation_family = item.get("implementation_family")
            if implementation_family is not None and not _non_empty_string(implementation_family):
                errors.append(f"{where}: implementation_family must be a non-empty string when present")
            fallback = item.get("fallback")
            if not isinstance(fallback, dict) or not isinstance(fallback.get("present"), bool):
                errors.append(f"{where}: fallback requires boolean present")
            elif fallback["present"] and not _non_empty_string(fallback.get("description")):
                errors.append(f"{where}: fallback.present requires a description")
            source_anchors = item.get("source_anchors")
            anchor_references: List[str] = []
            if not isinstance(source_anchors, list) or not source_anchors:
                errors.append(f"{where}: source_anchors must be a non-empty array")
            else:
                for anchor_index, anchor in enumerate(source_anchors):
                    anchor_where = f"{where}: source_anchors[{anchor_index}]"
                    if (
                        not isinstance(anchor, dict)
                        or anchor.get("role") not in IMPLEMENTATION_EXECUTION_ROLES
                        or not _non_empty_string(anchor.get("reference"))
                        or not _non_empty_string(anchor.get("marker"))
                        or not isinstance(anchor.get("evidence_markers"), list)
                        or any(not _non_empty_string(marker) for marker in anchor.get("evidence_markers", []))
                    ):
                        errors.append(f"{anchor_where}: requires supported role, reference, marker and string evidence_markers")
                        continue
                    anchor_references.append(anchor["reference"])
            if isinstance(provenance, dict) and isinstance(provenance.get("references"), list):
                if set(anchor_references) != set(provenance["references"]):
                    errors.append(f"{where}: provenance.references must exactly match source_anchors references")
            discovery = item.get("discovery")
            if discovery is not None:
                if (
                    not isinstance(discovery, dict)
                    or not _non_empty_string(discovery.get("rule_id"))
                    or not isinstance(discovery.get("rules_sha256"), str)
                    or not SHA256_RE.fullmatch(discovery["rules_sha256"])
                    or not _non_empty_string(discovery.get("relative_path"))
                    or not _non_empty_string(discovery.get("function"))
                    or not isinstance(discovery.get("function_start_line"), int)
                    or not isinstance(discovery.get("function_end_line"), int)
                    or discovery["function_start_line"] < 1
                    or discovery["function_end_line"] < discovery["function_start_line"]
                    or not _non_empty_string(discovery.get("evidence_reference"))
                ):
                    errors.append(f"{where}: discovery requires rule hash, function range and evidence reference")
                else:
                    evidence_match = SOURCE_REFERENCE_RE.fullmatch(discovery["evidence_reference"])
                    if evidence_match is None or evidence_match.group("path") != discovery["relative_path"]:
                        errors.append(f"{where}: discovery evidence_reference must use its relative_path:line")
                    else:
                        evidence_line = int(evidence_match.group("line"))
                        if not discovery["function_start_line"] <= evidence_line <= discovery["function_end_line"]:
                            errors.append(f"{where}: discovery evidence line must be inside the function range")
                        _validate_source_references(
                            [discovery["evidence_reference"]],
                            where,
                            errors,
                            source_root,
                            line_count_cache,
                            validate_source_files,
                        )
            evidence = item.get("evidence")
            if (
                not isinstance(evidence, dict)
                or not _non_empty_string(evidence.get("origin"))
                or evidence.get("confidence") not in EVIDENCE_CONFIDENCE
                or not isinstance(evidence.get("markers"), list)
                or not evidence.get("markers")
            ):
                errors.append(f"{where}: evidence requires origin, supported confidence and non-empty markers")
    return ids


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_manifest(root: Path, layers: Dict[str, List[Dict[str, Any]]], errors: List[str], warnings: List[str], *, require_manifest: bool, strict: bool) -> None:
    path = root / MANIFEST_NAME
    if not path.exists():
        if require_manifest:
            errors.append(f"missing required KB manifest: {MANIFEST_NAME}")
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{MANIFEST_NAME}: invalid JSON: {exc}")
        return
    if not isinstance(manifest, dict):
        errors.append(f"{MANIFEST_NAME}: top level must be an object")
        return
    for field in ("artifact_type", "schema_version", "kb_id", "kb_version", "source_revision", "owners", "build_tool", "layers"):
        if field not in manifest:
            errors.append(f"{MANIFEST_NAME}: missing required field {field}")
    if manifest.get("artifact_type") != "heterogeneous_kb_manifest":
        errors.append(f"{MANIFEST_NAME}: artifact_type must be heterogeneous_kb_manifest")
    if manifest.get("schema_version") != "1.0":
        errors.append(f"{MANIFEST_NAME}: unsupported schema_version")
    if not _non_empty_string(manifest.get("kb_id")) or not _non_empty_string(manifest.get("kb_version")):
        errors.append(f"{MANIFEST_NAME}: kb_id and kb_version must be non-empty strings")
    if not _non_empty_string(manifest.get("source_revision")):
        errors.append(f"{MANIFEST_NAME}: source_revision must be non-empty")
    snapshot = manifest.get("source_snapshot")
    if snapshot is not None:
        from backend.source_revision_snapshot import validate_snapshot
        snapshot_errors = validate_snapshot(snapshot, require_clean=strict)
        errors.extend(f"{MANIFEST_NAME}: {error}" for error in snapshot_errors)
        if isinstance(snapshot, dict) and manifest.get("source_revision") != snapshot.get("snapshot_id"):
            errors.append(f"{MANIFEST_NAME}: source_revision must equal source_snapshot.snapshot_id")
    elif strict:
        errors.append(f"{MANIFEST_NAME}: strict mode requires a source_snapshot")
    owners = manifest.get("owners")
    if not isinstance(owners, list) or not owners or any(not isinstance(owner, dict) or not _non_empty_string(owner.get("role")) or not _non_empty_string(owner.get("team")) for owner in owners):
        errors.append(f"{MANIFEST_NAME}: owners must contain role and team")
    build_tool = manifest.get("build_tool")
    if not isinstance(build_tool, dict) or not _non_empty_string(build_tool.get("name")) or not _non_empty_string(build_tool.get("version")):
        errors.append(f"{MANIFEST_NAME}: build_tool requires name and version")
    entries = manifest.get("layers")
    if not isinstance(entries, list):
        errors.append(f"{MANIFEST_NAME}: layers must be an array")
        return
    by_path = {entry.get("path"): entry for entry in entries if isinstance(entry, dict) and _non_empty_string(entry.get("path"))}
    for name, records in layers.items():
        entry = by_path.get(name)
        if entry is None:
            errors.append(f"{MANIFEST_NAME}: missing layer checksum for {name}")
            continue
        if entry.get("record_count") != len(records):
            errors.append(f"{MANIFEST_NAME}: record_count mismatch for {name}")
        expected_hash = _sha256(root / name)
        if entry.get("sha256") != expected_hash:
            errors.append(f"{MANIFEST_NAME}: sha256 mismatch for {name}")
    if strict:
        if manifest.get("source_revision") in {"workspace-unpinned", "UNSET", "unknown"}:
            errors.append(f"{MANIFEST_NAME}: strict mode requires an immutable source_revision")
        if any(owner.get("team") in {"UNASSIGNED", "unknown"} for owner in owners if isinstance(owner, dict)):
            errors.append(f"{MANIFEST_NAME}: strict mode requires assigned owners")


def validate(
    root: Path,
    *,
    require_manifest: bool = True,
    validate_manifest: bool = True,
    strict: bool = False,
    source_root: Optional[Path] = None,
    validate_source_files: bool = True,
) -> Tuple[List[str], List[str], Dict[str, List[Dict[str, Any]]]]:
    errors: List[str] = []
    warnings: List[str] = []
    layers: Dict[str, List[Dict[str, Any]]] = {}
    ids: Dict[str, Set[str]] = {}
    effective_source_root = source_root or _discover_workspace_root(root)
    line_count_cache: Dict[Path, int] = {}
    for name in LAYER_SPECS:
        path = root / name
        if not path.exists():
            errors.append(f"missing required KB layer: {name}")
            layers[name] = []
            ids[name] = set()
            continue
        layers[name] = _read_records(path, errors)
        ids[name] = _validate_layer(
            name,
            layers[name],
            errors,
            warnings,
            effective_source_root,
            line_count_cache,
            validate_source_files,
        )

    algorithms = {item.get("algorithm") for item in layers["code_signature_patterns.json"]}
    hardware_ids = ids["hardware_capabilities.json"]
    cost_ids = ids["cost_models.json"]
    mapping_targets = {item.get("target_unit") for item in layers["algorithm_hardware_mapping.json"]}
    for index, mapping in enumerate(layers["algorithm_hardware_mapping.json"]):
        where = f"algorithm_hardware_mapping.json[{index}]"
        if mapping.get("algorithm") not in algorithms:
            errors.append(f"{where}: algorithm has no code signature: {mapping.get('algorithm')}")
        if mapping.get("hardware_id") not in hardware_ids:
            errors.append(f"{where}: hardware_id not found: {mapping.get('hardware_id')}")
        if mapping.get("cost_model_id") not in cost_ids:
            errors.append(f"{where}: cost_model_id not found: {mapping.get('cost_model_id')}")
    for index, pair in enumerate(layers["fallback_pairs.json"]):
        where = f"fallback_pairs.json[{index}]"
        if pair.get("algorithm") not in algorithms:
            errors.append(f"{where}: algorithm has no code signature: {pair.get('algorithm')}")
        if pair.get("target_unit") not in mapping_targets and pair.get("target_unit") not in hardware_ids:
            errors.append(f"{where}: target_unit is not present in mappings or hardware capabilities: {pair.get('target_unit')}")
    for index, implementation_case in enumerate(layers["existing_heterogeneous_implementation_cases.json"]):
        where = f"existing_heterogeneous_implementation_cases.json[{index}]"
        if implementation_case.get("target_unit") not in hardware_ids:
            errors.append(
                f"{where}: target_unit is not present in hardware capabilities: "
                f"{implementation_case.get('target_unit')}"
            )
    if validate_manifest:
        _validate_manifest(root, layers, errors, warnings, require_manifest=require_manifest, strict=strict)
    return errors, warnings, layers


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a layered heterogeneous optimization knowledge base")
    parser.add_argument("--kb", required=True, help="Path to heterogeneous_kb directory")
    parser.add_argument("--strict", action="store_true", help="Treat non-confirmed fallback evidence as an error")
    parser.add_argument("--allow-missing-manifest", action="store_true", help="Only for bootstrap before build_heterogeneous_kb.py creates the manifest")
    parser.add_argument("--source-root", help="Workspace root containing source_code/; auto-detected by default")
    parser.add_argument(
        "--structure-only",
        action="store_true",
        help="Validate schemas, cross-references and checksums without opening referenced source files",
    )
    args = parser.parse_args()
    if args.strict and args.structure_only:
        parser.error("--strict requires source evidence and cannot be combined with --structure-only")
    root = Path(args.kb)
    if not root.is_dir():
        print(f"ERROR: KB directory not found: {root}", file=sys.stderr)
        return 2
    source_root = Path(args.source_root) if args.source_root else None
    errors, warnings, layers = validate(
        root,
        require_manifest=not args.allow_missing_manifest,
        strict=args.strict,
        source_root=source_root,
        validate_source_files=not args.structure_only,
    )
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if args.strict:
        errors.extend(f"strict mode: {warning}" for warning in warnings)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    counts = ", ".join(f"{name}={len(records)}" for name, records in layers.items())
    print(f"KB validation passed: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
