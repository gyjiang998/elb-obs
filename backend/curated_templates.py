"""Validation and workspace materialization for curated Agent templates."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from .core import sha256_file


CATALOG_SCHEMA_VERSION = "hardware_api_template_catalog/v1"
MAX_TEMPLATE_FILE_BYTES = 2 * 1024 * 1024
MAX_TEMPLATE_CATALOG_BYTES = 512 * 1024
TEMPLATE_COVERAGE_LEVELS = {
    "executable_harness",
    "lifecycle_with_adapter",
    "integration_recipe",
}
API_KB_FILES = {
    "gpu": "gpu_api_knowledge_base.json",
    "npu": "npu_api_knowledge_base.json",
    "dsp": "dsp_api_knowledge_base.json",
    "media": "media_accelerator_api_knowledge_base.json",
}


class CuratedTemplateError(RuntimeError):
    """Raised when a deployed template catalog is incomplete or mutable."""


class CuratedTemplateNotFoundError(CuratedTemplateError):
    """Raised when a requested template ID is not registered."""


def _relative_file(value: object) -> Path:
    text = str(value or "").strip()
    posix = PurePosixPath(text)
    if not text or posix.is_absolute() or ".." in posix.parts or "." in posix.parts:
        raise CuratedTemplateError(f"invalid curated template path: {text!r}")
    return Path(*posix.parts)


def validate_curated_template_catalog(source_root: Path) -> tuple[dict[str, Any], list[Path]]:
    """Validate the catalog and return its registered immutable files."""

    root = source_root.resolve()
    catalog_path = root / "catalog.json"
    if not catalog_path.is_file() or catalog_path.is_symlink():
        raise CuratedTemplateError("curated template catalog.json is missing or is a symlink")
    if catalog_path.stat().st_size > MAX_TEMPLATE_CATALOG_BYTES:
        raise CuratedTemplateError("curated template catalog exceeds the size limit")
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CuratedTemplateError("curated template catalog is not valid UTF-8 JSON") from exc
    if not isinstance(catalog, dict) or catalog.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise CuratedTemplateError("curated template catalog schema_version is unsupported")

    templates = catalog.get("templates")
    if not isinstance(templates, list) or not templates:
        raise CuratedTemplateError("curated template catalog has no templates")
    template_ids: set[str] = set()
    registered: list[Path] = []
    registered_names: set[str] = set()
    for entry in templates:
        if not isinstance(entry, dict):
            raise CuratedTemplateError("curated template entry must be an object")
        template_id = str(entry.get("template_id") or "").strip()
        if not template_id or template_id in template_ids:
            raise CuratedTemplateError(f"invalid or duplicate curated template id: {template_id!r}")
        template_ids.add(template_id)
        if entry.get("asset_class") != "curated_template" or not str(entry.get("version") or "").strip():
            raise CuratedTemplateError(f"template {template_id} lacks curated asset metadata")
        covered_families = entry.get("covered_semantic_families")
        if not isinstance(covered_families, list) or not all(str(item or "").strip() for item in covered_families):
            raise CuratedTemplateError(f"template {template_id} lacks semantic-family coverage")
        if entry.get("coverage_level") not in TEMPLATE_COVERAGE_LEVELS:
            raise CuratedTemplateError(f"template {template_id} has an unsupported coverage level")
        files = entry.get("files")
        hashes = entry.get("file_sha256")
        if not isinstance(files, list) or not files or not isinstance(hashes, dict):
            raise CuratedTemplateError(f"template {template_id} lacks files or file_sha256")
        for raw_path in files:
            relative = _relative_file(raw_path)
            relative_name = relative.as_posix()
            expected_hash = str(hashes.get(relative_name) or "").strip().lower()
            if len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash):
                raise CuratedTemplateError(f"template {template_id} has no valid hash for {relative_name}")
            source = root / relative
            if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(root):
                raise CuratedTemplateError(f"registered template file is missing or unsafe: {relative_name}")
            if source.stat().st_size > MAX_TEMPLATE_FILE_BYTES:
                raise CuratedTemplateError(f"registered template file exceeds the size limit: {relative_name}")
            if sha256_file(source) != expected_hash:
                raise CuratedTemplateError(f"registered template file hash mismatch: {relative_name}")
            if relative_name not in registered_names:
                registered_names.add(relative_name)
                registered.append(relative)
    return catalog, registered


def validate_curated_template_coverage(source_root: Path, knowledge_root: Path) -> dict[str, Any]:
    """Verify that every current hardware API record has a family template."""

    catalog, _ = validate_curated_template_catalog(source_root)
    covered: dict[tuple[str, str], list[str]] = {}
    for entry in catalog["templates"]:
        unit = str(entry.get("unit") or "").strip().lower()
        for raw_family in entry.get("covered_semantic_families", []):
            family = str(raw_family or "").strip()
            covered.setdefault((unit, family), []).append(str(entry.get("template_id") or ""))

    missing: list[dict[str, str]] = []
    record_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    for unit, filename in API_KB_FILES.items():
        path = knowledge_root / filename
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CuratedTemplateError(f"hardware API knowledge base is unreadable: {filename}") from exc
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise CuratedTemplateError(f"hardware API knowledge base has no records: {filename}")
        record_counts[unit] = len(records)
        for record in records:
            if not isinstance(record, dict):
                continue
            semantic_family = record.get("semantic_family")
            family = str(semantic_family.get("id") or "").strip() if isinstance(semantic_family, dict) else ""
            if not family and unit == "npu":
                family = "unclassified_nnrt_operator"
            family_counts[f"{unit}:{family or 'unclassified'}"] = family_counts.get(
                f"{unit}:{family or 'unclassified'}", 0
            ) + 1
            if (unit, family) not in covered:
                missing.append({
                    "record_id": str(record.get("record_id") or ""),
                    "unit": unit,
                    "semantic_family": family or "unclassified",
                })

    snapshot = catalog.get("coverage_policy", {}).get("knowledge_record_snapshot", {})
    total = sum(record_counts.values())
    if snapshot and (
        any(snapshot.get(unit) != count for unit, count in record_counts.items())
        or snapshot.get("total") != total
    ):
        raise CuratedTemplateError("hardware API template coverage snapshot is stale")
    if missing:
        first = missing[0]
        raise CuratedTemplateError(
            "hardware API template coverage is incomplete: "
            f"{first['unit']}:{first['semantic_family']} ({first['record_id']})"
        )
    return {
        "schema_version": "hardware_api_template_coverage_report/v1",
        "record_count": total,
        "record_counts": record_counts,
        "semantic_family_count": len(family_counts),
        "covered_semantic_families": sorted(family_counts),
        "missing_records": [],
    }


def materialize_curated_template_catalog(source_root: Path, destination_root: Path) -> dict[str, Any]:
    """Copy only hash-pinned catalog files into one Agent workspace."""

    catalog, registered = validate_curated_template_catalog(source_root)
    shutil.rmtree(destination_root, ignore_errors=True)
    destination_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_root / "catalog.json", destination_root / "catalog.json")
    for relative in registered:
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)
    return {
        "catalog_id": catalog.get("catalog_id"),
        "version": catalog.get("version"),
        "template_count": len(catalog["templates"]),
        "file_count": len(registered),
    }


def list_curated_templates(
    source_root: Path,
    *,
    unit: str = "",
    profile_id: str = "",
    api_family: str = "",
    query: str = "",
) -> dict[str, Any]:
    """Return discoverable, hash-pinned metadata without loading source files."""

    catalog, _ = validate_curated_template_catalog(source_root)
    filters = {
        "unit": unit.strip().lower(),
        "profile_id": profile_id.strip().lower(),
        "api_family": api_family.strip().lower(),
    }
    normalized_query = query.strip().lower()
    catalog_templates = [dict(entry) for entry in catalog["templates"]]

    def searchable_values(entry: dict[str, Any]) -> list[str]:
        values: list[str] = []

        def append(value: Any) -> None:
            if isinstance(value, dict):
                for nested in value.values():
                    append(nested)
            elif isinstance(value, list):
                for nested in value:
                    append(nested)
            elif str(value or "").strip():
                values.append(str(value).strip().lower())

        for field in (
            "template_id",
            "title_zh",
            "summary_zh",
            "unit",
            "profile_id",
            "api_family",
            "exposure_layer",
            "template_role",
            "coverage_level",
            "covered_semantic_families",
            "api_symbols",
            "semantic_match",
            "adaptation_points",
            "tags",
            "verification",
            "knowledge_record_ids",
        ):
            append(entry.get(field))
        return values

    def terms(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", value.lower())
            if len(token) > 1
        }

    def query_score(entry: dict[str, Any], requested: str) -> int:
        if not requested:
            return 0
        values = searchable_values(entry)
        haystack = " ".join(values)
        requested_terms = terms(requested)
        candidate_terms = terms(haystack)
        score = 0
        if requested in values:
            score += 100
        if requested in haystack:
            score += 40
        score += 12 * len(requested_terms & candidate_terms)
        return score

    facets = {
        field: sorted(
            {
                str(entry.get(field) or "").strip().lower()
                for entry in catalog_templates
                if str(entry.get(field) or "").strip()
            }
        )
        for field in filters
    }

    filter_diagnostics: list[dict[str, Any]] = []
    for requested_field, requested_value in filters.items():
        if not requested_value:
            continue
        recognized_as = [field for field, values in facets.items() if requested_value in values]
        filter_diagnostics.append(
            {
                "field": requested_field,
                "value": requested_value,
                "recognized_in_field": requested_value in facets[requested_field],
                "recognized_as": recognized_as,
            }
        )

    def matches(entry: dict[str, Any]) -> bool:
        return all(
            not expected or str(entry.get(field) or "").strip().lower() == expected
            for field, expected in filters.items()
        )

    exact_templates = [entry for entry in catalog_templates if matches(entry)]
    if normalized_query:
        ranked_exact = sorted(
            ((query_score(entry, normalized_query), entry) for entry in exact_templates),
            key=lambda item: (-item[0], str(item[1].get("template_id") or "")),
        )
        positive_matches = [entry for score, entry in ranked_exact if score > 0]
        templates = positive_matches if positive_matches else exact_templates
    else:
        templates = exact_templates

    suggested_templates: list[dict[str, Any]] = []
    if not templates and any(filters.values()):
        requested_text = " ".join(value for value in filters.values() if value)
        ranked_suggestions = sorted(
            ((query_score(entry, requested_text), entry) for entry in catalog_templates),
            key=lambda item: (-item[0], str(item[1].get("template_id") or "")),
        )
        for score, entry in ranked_suggestions:
            if score <= 0:
                continue
            suggested_templates.append(
                {
                    "template_id": entry.get("template_id"),
                    "unit": entry.get("unit"),
                    "profile_id": entry.get("profile_id"),
                    "api_family": entry.get("api_family"),
                    "api_symbols": entry.get("api_symbols"),
                    "semantic_match": entry.get("semantic_match"),
                    "match_score": score,
                    "canonical_filters": {
                        field: entry.get(field)
                        for field in filters
                        if entry.get(field)
                    },
                }
            )
            if len(suggested_templates) >= 5:
                break

    match_status = "exact_match"
    if normalized_query:
        match_status = "query_ranked" if templates else "no_query_match"
    elif not templates:
        match_status = "no_exact_match"
    return {
        "schema_version": "curated_template_listing/v2",
        "catalog_id": catalog.get("catalog_id"),
        "catalog_version": catalog.get("version"),
        "selection_policy": catalog.get("selection_policy"),
        "filters": filters,
        "query": normalized_query,
        "match_status": match_status,
        "catalog_template_count": len(catalog_templates),
        "template_count": len(templates),
        "templates": templates,
        "available_facets": facets,
        "filter_diagnostics": filter_diagnostics,
        "suggested_templates": suggested_templates,
        "next_action_zh": (
            "从 templates 中选择语义相容的 template_id 并读取；不要仅凭排名认定适配。"
            if templates
            else "精确筛选没有命中。检查 available_facets 与 filter_diagnostics；可读取语义相容的 suggested_templates，或省略未知筛选字段并用 query 重查。"
        ),
    }


def read_curated_template(source_root: Path, template_id: str) -> dict[str, Any]:
    """Read one registered template bundle after catalog and hash validation."""

    catalog, _ = validate_curated_template_catalog(source_root)
    normalized_id = template_id.strip()
    entry = next(
        (item for item in catalog["templates"] if str(item.get("template_id") or "") == normalized_id),
        None,
    )
    if entry is None:
        raise CuratedTemplateNotFoundError(f"curated template is not registered: {normalized_id!r}")

    files: list[dict[str, Any]] = []
    for raw_path in entry["files"]:
        relative = _relative_file(raw_path)
        relative_name = relative.as_posix()
        source = source_root.resolve() / relative
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CuratedTemplateError(f"curated template file is not readable UTF-8 text: {relative_name}") from exc
        files.append(
            {
                "path": relative_name,
                "sha256": entry["file_sha256"][relative_name],
                "content": content,
            }
        )

    return {
        "schema_version": "curated_template_bundle/v1",
        "catalog_id": catalog.get("catalog_id"),
        "catalog_version": catalog.get("version"),
        "selection_policy": catalog.get("selection_policy"),
        "template": dict(entry),
        "files": files,
        "claim_boundary_zh": "模板是实现先验，不是本轮设备能力、功能正确、热点集成或性能收益证据。",
    }
