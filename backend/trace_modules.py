"""Low-cardinality module labels derived from trace-visible identities.

The trace adapters receive process names, thread names, scheduler task names, and
occasionally span symbols.  Those identifiers are useful for triage, but they
are not source-module ownership proof.  This module therefore emits a small,
explicitly heuristic taxonomy for the Cluster Explorer and keeps the raw label
as the fallback display value.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class TraceModuleRule:
    key: str
    name: str
    system_collapsed: bool
    tokens: tuple[str, ...]


# Order is intentional: specific platform runtimes must win before broad UI or
# graphics terms that can also appear in application-owned thread names.
TRACE_MODULE_RULES: tuple[TraceModuleRule, ...] = (
    TraceModuleRule(
        "ffrt_runtime",
        "FFRT runtime",
        True,
        ("ffrt", "os_ffrt", "ffrtd"),
    ),
    TraceModuleRule(
        "binder_ipc",
        "Binder / IPC",
        True,
        ("binder", "os_ipc", "ipc_", "iremote", "talkwithdriver", "hwbinder", "rpc"),
    ),
    TraceModuleRule(
        "kernel_scheduler",
        "Kernel scheduler",
        True,
        ("kworker", "ksoftirqd", "softirq", "rcu_", "rcu-", "migration", "cpuhp", "swapper", "irq/", "irq-"),
    ),
    TraceModuleRule(
        "system_service",
        "System service",
        True,
        ("sainit", "samgr", "hiview", "hilogd", "system_ability", "sysmgr", "hdf", "hiprofiler"),
    ),
    TraceModuleRule(
        "render_rosen",
        "Render / Rosen",
        False,
        ("render_service", "renderthread", "renderframe", "rosen", "render", "surface", "compositor"),
    ),
    TraceModuleRule(
        "graphics_gpu",
        "Graphics / GPU",
        False,
        ("gpu", "vulkan", "opengl", "egl", "hwc", "composer", "gralloc", "skia", "drawing", "raster"),
    ),
    TraceModuleRule(
        "arkui_ui",
        "ArkUI / UI",
        False,
        ("arkui", "ace::", "ace_", "uicontent", "ui_content"),
    ),
    TraceModuleRule(
        "ark_runtime",
        "Ark runtime",
        False,
        ("arkts", "ark::", "ark_runtime", "ets"),
    ),
    TraceModuleRule(
        "application_framework",
        "Application framework",
        False,
        ("appexecfwk", "aafwk", "abilitymanager", "ability_manager", "bundlemgr", "bundle_mgr"),
    ),
    TraceModuleRule(
        "input_interaction",
        "Input / interaction",
        False,
        ("multimodalinput", "inputmanager", "mmi_", "mmi_service"),
    ),
    TraceModuleRule(
        "media_codec",
        "Media / Codec",
        False,
        ("media", "codec", "audio", "camera", "video", "image", "avcodec"),
    ),
    TraceModuleRule(
        "network_io",
        "Network / I-O",
        False,
        ("network", "netd", "wifi"),
    ),
)

UNCLASSIFIED_TRACE_MODULE = TraceModuleRule("unclassified", "Unclassified", False, ())
_RULE_BY_KEY = {rule.key: rule for rule in TRACE_MODULE_RULES}
_RULE_BY_KEY[UNCLASSIFIED_TRACE_MODULE.key] = UNCLASSIFIED_TRACE_MODULE

# The raw trace serializes a bit number, while the source header resolves that
# number to a macro.  Keep this mapping at the macro level so the bit values
# remain versioned by hitrace_meter.h rather than hard-coded in the analyzer.
HITRACE_TAG_MACRO_MODULE_KEYS: dict[str, str] = {
    "HITRACE_TAG_FFRT": "ffrt_runtime",
    "HITRACE_TAG_RPC": "binder_ipc",
    "HITRACE_TAG_HDF": "system_service",
    "HITRACE_TAG_GRAPHIC_AGP": "graphics_gpu",
    "HITRACE_TAG_WINDOW_MANAGER": "render_rosen",
    "HITRACE_TAG_DISTRIBUTED_SCREEN": "render_rosen",
    "HITRACE_TAG_ACE": "arkui_ui",
    "HITRACE_TAG_ANIMATION": "arkui_ui",
    "HITRACE_TAG_ARK": "ark_runtime",
    "HITRACE_TAG_APP": "application_framework",
    "HITRACE_TAG_ABILITY_MANAGER": "application_framework",
    "HITRACE_TAG_MULTIMODALINPUT": "input_interaction",
    "HITRACE_TAG_NET": "network_io",
    "HITRACE_TAG_NWEB": "network_io",
    "HITRACE_TAG_DRM": "media_codec",
    "HITRACE_TAG_DISTRIBUTED_AUDIO": "media_codec",
    "HITRACE_TAG_ZCAMERA": "media_codec",
    "HITRACE_TAG_ZMEDIA": "media_codec",
    "HITRACE_TAG_ZIMAGE": "media_codec",
    "HITRACE_TAG_ZAUDIO": "media_codec",
    "HITRACE_TAG_DISTRIBUTED_CAMERA": "media_codec",
}


def _clean_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.casefold() in {"unknown", "[no process]", "-"}:
        return ""
    return text[:512]


def classify_trace_label(value: Any) -> TraceModuleRule | None:
    """Return one conservative category for a trace-visible label."""

    label = _clean_label(value).casefold()
    if not label:
        return None
    return next((rule for rule in TRACE_MODULE_RULES if any(token in label for token in rule.tokens)), None)


def known_trace_module_keys() -> tuple[str, ...]:
    """Return the stable keys accepted by module-scoped trace discovery."""

    return tuple(_RULE_BY_KEY)


def _module_payload(
    rule: TraceModuleRule,
    *,
    classification: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "module_key": rule.key,
        "module_name": rule.name,
        "system_collapsed": rule.system_collapsed,
        "classification": classification,
        "evidence": evidence,
    }


def _prefer_module_rule(candidates: Iterable[TraceModuleRule]) -> TraceModuleRule | None:
    """Prefer a product-facing module when an identity exposes both layers."""

    ordered = list(candidates)
    if not ordered:
        return None
    return next((rule for rule in ordered if not rule.system_collapsed), ordered[0])


def classify_trace_event(
    event_name: Any,
    *,
    process_name: Any = None,
    thread_name: Any = None,
    hitrace_marker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify one trace event without claiming source-module ownership.

    A decoded HiTrace module tag is stronger evidence than a process/thread or
    text-token heuristic.  When no tag is available, an explicit event label
    wins over execution context; generic labels then use a product-facing
    process/thread category before a runtime/system category.
    """

    marker = hitrace_marker if isinstance(hitrace_marker, dict) else {}
    module_tags = marker.get("module_tags") if isinstance(marker.get("module_tags"), list) else []
    tagged: list[tuple[TraceModuleRule, dict[str, Any]]] = []
    for tag in module_tags:
        if not isinstance(tag, dict):
            continue
        macro = tag.get("macro")
        if not isinstance(macro, str):
            continue
        module_key = HITRACE_TAG_MACRO_MODULE_KEYS.get(macro)
        rule = _RULE_BY_KEY.get(module_key or "")
        if rule is not None:
            tagged.append((rule, tag))
    if tagged:
        rule = _prefer_module_rule(rule for rule, _ in tagged)
        assert rule is not None
        selected_tags = [tag for candidate, tag in tagged if candidate.key == rule.key]
        return _module_payload(
            rule,
            classification="hitrace_tag_macro_v1",
            evidence={
                "kind": "hitrace_module_tag",
                "tag_macros": [str(tag.get("macro")) for tag in selected_tags],
                "tag_bits": [int(tag["bit"]) for tag in selected_tags if isinstance(tag.get("bit"), int)],
                "tag_display_names": [str(tag.get("display_name")) for tag in selected_tags if isinstance(tag.get("display_name"), str)],
            },
        )

    event_label = _clean_label(event_name)
    event_rule = classify_trace_label(event_label)
    if event_rule is not None:
        return _module_payload(
            event_rule,
            classification="trace_identity_heuristic_v2",
            evidence={"kind": "event_label", "label": event_label},
        )

    contextual = [
        ("process_name", _clean_label(process_name)),
        ("thread_name", _clean_label(thread_name)),
    ]
    contextual_matches = [(kind, label, classify_trace_label(label)) for kind, label in contextual]
    contextual_matches = [(kind, label, rule) for kind, label, rule in contextual_matches if rule is not None]
    if contextual_matches:
        rule = _prefer_module_rule(rule for _, _, rule in contextual_matches)
        assert rule is not None
        kind, label, _ = next(item for item in contextual_matches if item[2].key == rule.key)
        return _module_payload(
            rule,
            classification="trace_identity_heuristic_v2",
            evidence={"kind": kind, "label": label},
        )

    return _module_payload(
        UNCLASSIFIED_TRACE_MODULE,
        classification="unclassified",
        evidence={"kind": "none"},
    )


def summarize_trace_modules(
    labels: Iterable[Any],
    *,
    fallback_top_function: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic Explorer summary from non-authoritative labels.

    ``top_function_name`` retains the preferred display fallback even when the
    source is only a scheduler task name.  Consumers must use ``fallback_kind``
    to avoid presenting that value as resolved source information.
    """

    label_counts: Counter[str] = Counter()
    module_counts: Counter[str] = Counter()
    for value in labels:
        label = _clean_label(value)
        if not label:
            continue
        label_counts[label] += 1
        rule = classify_trace_label(label)
        if rule:
            module_counts[rule.key] += 1

    ordered_keys = sorted(
        module_counts,
        key=lambda key: (
            _RULE_BY_KEY[key].system_collapsed,
            -module_counts[key],
            next(index for index, rule in enumerate(TRACE_MODULE_RULES) if rule.key == key),
        ),
    )
    fallback = _clean_label(fallback_top_function)
    if not fallback and label_counts:
        fallback = label_counts.most_common(1)[0][0]
    system_module_only = bool(ordered_keys) and all(_RULE_BY_KEY[key].system_collapsed for key in ordered_keys)

    return {
        "classification": "trace_label_heuristic_v1",
        "module_names": [_RULE_BY_KEY[key].name for key in ordered_keys],
        "module_keys": ordered_keys,
        "module_counts": [
            {
                "module_key": key,
                "module_name": _RULE_BY_KEY[key].name,
                "evidence_count": module_counts[key],
                "system_collapsed": _RULE_BY_KEY[key].system_collapsed,
            }
            for key in ordered_keys
        ],
        "top_function_name": fallback or "unknown",
        "fallback_kind": "trace_label",
        "system_module_only": system_module_only,
        "has_system_module": any(_RULE_BY_KEY[key].system_collapsed for key in ordered_keys),
    }
