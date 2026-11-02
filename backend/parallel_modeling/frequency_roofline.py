"""Measured OPP-latency curves for PMU-classified application CPU work.

The model is deliberately small enough to fit from an offline frequency sweep.
Every curve describes time relative to its own maximum OPP. New calibrated
models also retain the absolute unit cost at that point and the matching model
reference cost. That measured target/source ratio replaces ``build_ratio`` for
classified BuildItem CPU work. Older v1 records without the absolute anchors
remain valid and continue to use the legacy ratio.

Below the knee the curve has the steeper, frequency-sensitive branch.  Above
the knee only the residual exponent remains.  A compute proxy normally fits a
knee close to the maximum OPP and similar exponents on both branches, while a
memory proxy normally fits a lower knee and a small above-knee exponent.
"""
from __future__ import annotations

import math
import statistics


SCHEMA = "frequency_roofline/v1"
WORKLOAD_CLASSES = ("compute", "memory", "mixed")


def factor(max_khz: int, frequency_khz: int, knee_khz: int,
           above_knee_exponent: float, below_knee_exponent: float) -> float:
    """Return normalized on-CPU time at ``frequency_khz``.

    ``factor(max_khz, max_khz, ...)`` is exactly one.  The two branches are
    continuous at ``knee_khz``:

      (max/f)^e_hi                                      f >= knee
      (max/knee)^e_hi * (knee/f)^e_lo                  f < knee

    This is a compact CPU-frequency analogue of Roofline: ``e_hi`` is the small
    residual frequency response after memory throughput
    has become limiting.  ``e_lo`` is the response below the knee.  The model
    requires ``0 <= e_hi <= e_lo <= 1`` and does not multiply an additional
    legacy elasticity, avoiding double-counting CPU frequency.
    """
    values = (max_khz, frequency_khz, knee_khz, above_knee_exponent, below_knee_exponent)
    if (any(isinstance(value, bool) for value in values)
            or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)
            or max_khz <= 0 or not 0 < frequency_khz <= max_khz or not 0 < knee_khz <= max_khz
            or not 0 <= above_knee_exponent <= below_knee_exponent <= 1):
        raise ValueError("Invalid frequency Roofline parameters")
    if frequency_khz >= knee_khz:
        return (max_khz / frequency_khz) ** above_knee_exponent
    return ((max_khz / knee_khz) ** above_knee_exponent
            * (knee_khz / frequency_khz) ** below_knee_exponent)


def fit(points: list[tuple[int, float]], max_khz: int) -> dict:
    """Fit knee and post-knee sensitivity from normalized time observations.

    ``points`` contains ``(frequency_khz, time_relative_to_max_opp)``.  Repeats
    should be reduced to a median before calling.  Candidate knees are measured
    OPPs; for each candidate, the two exponents are fitted in log space under
    ``0 <= e_hi <= e_lo <= 1``.  Deterministic tie-breaking prefers the higher
    knee; when both exponents are equal the knee is mathematically unresolved,
    and the maximum-OPP representation correctly expresses a compute-like
    curve without inventing an early bandwidth plateau.
    """
    if not isinstance(points, list) or len({frequency for frequency, _ in points}) < 5:
        raise ValueError("Frequency Roofline fitting needs at least five distinct OPPs")
    cleaned = []
    for frequency, relative in points:
        if (isinstance(frequency, bool) or not isinstance(frequency, int) or frequency <= 0
                or isinstance(relative, bool) or not isinstance(relative, (int, float))
                or not math.isfinite(relative) or relative <= 0):
            raise ValueError("Invalid frequency Roofline measurement")
        cleaned.append((frequency, float(relative)))
    if max(frequency for frequency, _ in cleaned) != max_khz:
        raise ValueError("Frequency Roofline measurements need a maximum-OPP anchor")

    def bounded_fit(rows):
        """Constrained two-variable least squares on the triangular domain."""
        aa = sum(hi * hi for hi, _, _ in rows)
        bb = sum(lo * lo for _, lo, _ in rows)
        ab = sum(hi * lo for hi, lo, _ in rows)
        ay = sum(hi * target for hi, _, target in rows)
        by = sum(lo * target for _, lo, target in rows)
        choices = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
        determinant = aa * bb - ab * ab
        if determinant > 1e-15:
            choices.append(((ay * bb - by * ab) / determinant,
                            (by * aa - ay * ab) / determinant))
        if bb > 1e-15: choices.append((0.0, by / bb))
        if aa > 1e-15: choices.append(((ay - ab) / aa, 1.0))
        diagonal = aa + 2 * ab + bb
        if diagonal > 1e-15:
            shared = (ay + by) / diagonal
            choices.append((shared, shared))
        valid = [(max(0.0, min(1.0, hi)), max(0.0, min(1.0, lo)))
                 for hi, lo in choices if math.isfinite(hi) and math.isfinite(lo)]
        valid = [(hi, lo) for hi, lo in valid if hi <= lo + 1e-12]
        return min(valid, key=lambda pair: sum((pair[0] * hi + pair[1] * lo - target) ** 2
                                               for hi, lo, target in rows))

    candidates = []
    for knee in sorted({frequency for frequency, _ in cleaned}):
        rows = []
        for frequency, observed in cleaned:
            if frequency >= knee:
                hi, lo = math.log(max_khz / frequency), 0.0
            else:
                hi, lo = math.log(max_khz / knee), math.log(knee / frequency)
            rows.append((hi, lo, math.log(observed)))
        above, below = bounded_fit(rows)
        predictions = [math.exp(above * hi + below * lo) for hi, lo, _ in rows]
        relative_errors = [abs(predicted - observed) / observed
                           for predicted, (_, observed) in zip(predictions, cleaned)]
        sse = sum((above * hi + below * lo - target) ** 2 for hi, lo, target in rows)
        candidates.append((sse, knee, above, below, relative_errors))
    _, knee, above, below, errors = min(candidates, key=lambda row: (round(row[0], 14), -row[1], row[2], row[3]))
    # At a domain boundary one branch has no non-zero basis and its exponent is
    # not independently identifiable.  Canonicalize it to the measured branch:
    # max-OPP knee becomes the expected compute-like single slope; min-OPP knee
    # becomes the measured post-knee plateau over the supported OPP domain.
    if knee == max_khz:
        above = below
    elif knee == min(frequency for frequency, _ in cleaned):
        below = above
    return {
        "schema_version": SCHEMA,
        "knee_khz": knee,
        "above_knee_exponent": above,
        "below_knee_exponent": below,
        "training_mape": statistics.mean(errors),
        "training_p90_ape": sorted(errors)[min(len(errors) - 1, math.ceil(.9 * len(errors)) - 1)],
        "frequency_domain_khz": sorted({frequency for frequency, _ in cleaned}),
        "fit_method": "measured_opp_grid_search_plus_bounded_least_squares",
    }


def profile_factor(profile: dict, frequency_khz: int, workload_class: str,
                   fallback_elasticity: float) -> tuple[float, dict]:
    """Resolve a typed OPP-latency transfer, with an explicit legacy fallback.

    For new fitted records the return value contains both the measured
    target/reference maximum-OPP ratio and the target frequency response. The
    detail flag tells the caller that the legacy core ratio must not be applied
    again. Old ``frequency_roofline/v1`` records contain only a normalized
    frequency curve and therefore keep their historical core-ratio behavior.
    """
    maximum = profile["max_khz"]
    model = profile.get("frequency_roofline")
    parameters = (model.get("classes", {}).get(workload_class)
                  if isinstance(model, dict) and model.get("schema_version") == SCHEMA else None)
    fallback_reason = None
    if isinstance(parameters, dict):
        domain = parameters.get("frequency_domain_khz")
        if (not isinstance(domain, list) or len(domain) < 2
                or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in domain)):
            fallback_reason = "missing_calibrated_frequency_domain"
        elif frequency_khz < min(domain) or frequency_khz > max(domain):
            fallback_reason = "outside_calibrated_frequency_domain"
    if isinstance(parameters, dict) and fallback_reason is None:
        frequency_value = factor(maximum, frequency_khz, parameters.get("knee_khz"),
                                 parameters.get("above_knee_exponent"), parameters.get("below_knee_exponent"))
        ratio = parameters.get("cross_core_time_ratio")
        reference = parameters.get("transfer_reference")
        absolute_transfer = (
            not isinstance(ratio, bool) and isinstance(ratio, (int, float)) and math.isfinite(ratio) and ratio > 0
            and isinstance(reference, dict)
            and isinstance(reference.get("core_kind"), str) and bool(reference["core_kind"])
            and not isinstance(reference.get("frequency_khz"), bool)
            and isinstance(reference.get("frequency_khz"), int) and reference["frequency_khz"] > 0
            and not isinstance(reference.get("cpu_ms_per_work_unit"), bool)
            and isinstance(reference.get("cpu_ms_per_work_unit"), (int, float))
            and math.isfinite(reference["cpu_ms_per_work_unit"])
            and reference["cpu_ms_per_work_unit"] > 0
        )
        value = frequency_value * ratio if absolute_transfer else frequency_value
        return value, {"mode": "class_roofline", "workload_class": workload_class,
                       "knee_khz": parameters["knee_khz"],
                       "above_knee_exponent": parameters["above_knee_exponent"],
                       "below_knee_exponent": parameters["below_knee_exponent"],
                       "frequency_domain_khz": list(parameters["frequency_domain_khz"]),
                       "frequency_factor": frequency_value,
                       "core_transfer_mode": ("absolute_curve_ratio" if absolute_transfer
                                              else "legacy_profile_ratio"),
                       **({"cross_core_time_ratio": ratio,
                           "transfer_reference": dict(reference)} if absolute_transfer else {})}
    if (isinstance(fallback_elasticity, bool)
            or not isinstance(fallback_elasticity, (int, float))
            or not math.isfinite(fallback_elasticity) or not 0 <= fallback_elasticity <= 1):
        raise ValueError("Invalid fallback frequency elasticity")
    value = (1 - fallback_elasticity) + fallback_elasticity * maximum / frequency_khz
    return value, {"mode": "legacy_elasticity_fallback", "workload_class": workload_class,
                   "frequency_elasticity": fallback_elasticity,
                   "reason": fallback_reason or (
                       "missing_class_roofline" if workload_class in WORKLOAD_CLASSES else "unclassified_stage")}
