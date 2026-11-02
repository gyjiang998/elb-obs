"""Directional two-task proxy interference; exact core/frequency domain matching.

Measured pairs are never extrapolated to three or more active CPU tasks. The
simulator reports when it instead uses its historical count-only alpha model.
"""
from __future__ import annotations

import math

SCHEMA = "directional_pairwise_interference/v1"
CLASSES = ("compute", "memory", "mixed")
PAIR_FIELDS = ("workload_class", "peer_workload_class", "peer_core_kind", "peer_frequency_khz", "working_set_bytes")
CELL_FIELDS = ("victim_class", "peer_class", "victim_core_kind", "peer_core_kind", "victim_frequency_khz", "peer_frequency_khz")


def cell_key(row):
    return (row["workload_class"], row["peer_workload_class"], row["core_kind"],
            row["peer_core_kind"], row["frequency_khz"], row["peer_frequency_khz"])


def calibrated_factor(model, victim_class, peer_class, victim_lane, peer_lane, concurrency):
    if not model or model.get("schema_version") != SCHEMA or model.get("status") != "proxy_holdout_validated":
        return None, "no_validated_pair_model", None
    if concurrency != 2:
        return None, "outside_two_task_domain", None
    if victim_class not in CLASSES or peer_class not in CLASSES:
        return None, "missing_workload_class", None
    key = (victim_class, peer_class, victim_lane["kind"], peer_lane["kind"], victim_lane["khz"], peer_lane["khz"])
    for cell in model.get("cells", []):
        if tuple(cell.get(field) for field in CELL_FIELDS) == key:
            beta = cell.get("beta")
            if isinstance(beta, (int, float)) and not isinstance(beta, bool) and math.isfinite(beta) and 0 <= beta <= 5:
                return 1 + beta, None, cell["id"]
            return None, "invalid_pair_coefficient", None
    return None, "unmeasured_class_core_frequency_pair", None
