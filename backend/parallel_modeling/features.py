"""Component statistics without optional numerical or plotting packages."""

import statistics


def describe(values):
    values = sorted(values)
    if not values:
        return {"count": 0}

    def quantile(q):
        position = (len(values) - 1) * q
        lower = int(position)
        return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (position - lower)

    return {"count": len(values), "sum": sum(values), "mean": statistics.mean(values),
            "min": values[0], "p50": quantile(.5), "p90": quantile(.9),
            "p95": quantile(.95), "max": values[-1]}


def component_features(extracted, supplied=None):
    supplied = {item["id"]: item for item in supplied or []}
    rows = []
    for index, item in enumerate(extracted["components"]):
        calls = [row for row in extracted["requests"] if row["component"] == item["id"]]
        app = [node for node in extracted["nodes"] if node.get("component") == item["id"]
               and node["id"].startswith("app_")]
        row = {**item, "display_id": f"B{index + 1:02d}",
               "frame_share_pct": item["wall_ms"] / (extracted["frame"]["duration_us"] / 1000) * 100,
               "service_cpu_ms": sum(call["server_cpu_ms"] for call in calls),
               "service_residence_ms": sum(call["residence_ms"] for call in calls),
               "runnable_ms": sum(node["observed_duration_ms"] for node in app if node["state"] == "runnable"),
               "other_wait_ms": sum(node["observed_duration_ms"] for node in app
                                    if node["state"] in ("blocked", "unknown") and node["kind"] != "await"),
               "parallelizable": True, "eligibility": "conditional_assumption",
               "dependency_evidence": "Observed intra-component order and synchronous Binder result edges retained; cross-component independence is unproven without source.",
               "pmu": supplied.get(item["id"], {}).get("pmu", {"bounds_valid": False,
                   "feature_usable_under_25pct_instruction_boundary_rule": False, "reason": "not_collected_or_not_clock_aligned"}),
               "dram_bytes": None, "cache_level_mapping": None, "working_set_bytes": None}
        counter = row["pmu"]
        for key, name in (("instruction_count_bounds", "instructions"), ("cycle_count_bounds", "cycles"),
                          ("cache_reference_bounds", "generic_cache_references"), ("cache_miss_bounds", "generic_cache_misses")):
            sources = [counter, *counter.get("groups", {}).values()]
            source = next((entry for entry in sources if entry.get("bounds_valid")
                           and (entry.get("lower") or {}).get(name) is not None
                           and (entry.get("upper") or {}).get(name) is not None), None)
            row[key] = [source["lower"][name], source["upper"][name]] if source else None
        rows.append(row)
    return rows


def summarize(rows):
    return {"component_count": len(rows), "wall_ms": describe(row["wall_ms"] for row in rows),
            "cpu_ms": describe(row["cpu_ms"] for row in rows),
            "binder_calls": sum(len(row["request_ids"]) for row in rows),
            "binder_blocked_ms": sum(row["binder_blocked_ms"] for row in rows),
            "service_cpu_ms": sum(row["service_cpu_ms"] for row in rows),
            "pmu_qualified_components": sum(bool(row["pmu"].get("feature_usable_under_25pct_instruction_boundary_rule")) for row in rows),
            "top5_wall_share_pct": 100 * sum(sorted((row["wall_ms"] for row in rows), reverse=True)[:5]) / sum(row["wall_ms"] for row in rows) if rows else 0,
            "histogram": [{"range_ms": label, "count": sum(low <= row["wall_ms"] < high for row in rows)}
                          for label, low, high in [("<0.5", 0, .5), ("0.5–1", .5, 1), ("1–2", 1, 2), ("2–5", 2, 5), ("≥5", 5, float("inf"))]]}
