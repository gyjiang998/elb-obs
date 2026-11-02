"""Portable legacy service-DAG simulator; conditional proxy predictions, not an application calibration."""
import copy
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from .interference import calibrated_factor
from .frequency_roofline import WORKLOAD_CLASSES, profile_factor


SERVICE_NAMES = {1020: 'resource_schedule', 1623: 'distributeddata', 1684: 'render_service',
                 1744: 'foundation', 1918: 'accountmgr', 3612: 'SceneBoard'}

SOURCE_TRANSFER_SCHEMA = 'source_placement_cost_transfer/v1'
CPU_RESIDENCY_EVIDENCE_SCHEMA = 'cpu_residency_evidence/v1'
COMPLETE_RESIDENCY_COVERAGE_MIN = .95
COMPLETE_RESIDENCY_COVERAGE_MAX = 1.05
RESIDENCY_COVERAGE_EPSILON = 1e-9


def _positive_number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def _transfer_reference_identity(reference):
    """Return the three-field absolute-curve reference or ``None``.

    Source normalization and target prediction are composable only when they
    name the exact same measured reference condition.  Treating two unrelated
    absolute curves as interchangeable would silently change the work unit.
    """
    if not isinstance(reference, dict):
        return None
    core_kind = reference.get('core_kind')
    frequency = reference.get('frequency_khz')
    cost = reference.get('cpu_ms_per_work_unit')
    if (not isinstance(core_kind, str) or not core_kind
            or isinstance(frequency, bool) or not isinstance(frequency, int) or frequency <= 0
            or not _positive_number(cost)):
        return None
    try:
        return json.dumps(reference, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        return None


def _source_target_factor(source_transfer, target_workload_class, target_detail):
    """Resolve source normalization only for a compatible absolute target.

    The legacy ratio/elasticity path has a different reference convention and
    therefore cannot consume an absolute source normalization.  Incompatible
    pairs fall back as a whole to the historical observed-time reference.
    """
    if source_transfer.get('status') != 'verified_source_normalization':
        return 1.0, False, source_transfer.get(
            'fallback_reason', 'source_placement_transfer_missing')
    if source_transfer.get('workload_class') != target_workload_class:
        return 1.0, False, 'source_target_workload_class_mismatch'
    if (target_detail.get('mode') != 'class_roofline'
            or target_detail.get('core_transfer_mode') != 'absolute_curve_ratio'):
        reason = ('source_target_absolute_curve_unavailable_explicit_sensitivity'
                  if target_detail.get('reason') == 'explicit_sensitivity_override'
                  else 'source_target_absolute_curve_unavailable')
        return 1.0, False, reason
    source_reference = _transfer_reference_identity(source_transfer.get('transfer_reference'))
    target_reference = _transfer_reference_identity(target_detail.get('transfer_reference'))
    if source_reference is None or target_reference is None or source_reference != target_reference:
        return 1.0, False, 'source_target_transfer_reference_mismatch'
    source_factor = source_transfer.get('reference_work_per_observed_cpu_ms')
    if not _positive_number(source_factor):
        return 1.0, False, 'source_reference_factor_invalid'
    return source_factor, True, None


def _classification_provenance(stage, workload_class):
    classification = stage.get('classification') if isinstance(stage.get('classification'), dict) else {}
    running = (classification.get('cpu_candidate')
               if isinstance(classification.get('cpu_candidate'), dict) else classification)
    confidence = running.get('confidence') if isinstance(running.get('confidence'), dict) else {}
    result = {
        'workload_class': workload_class,
        'source': 'classification.cpu_candidate' if running is not classification else 'classification',
        'classified_id': running.get('id'),
        'confidence': copy.deepcopy(confidence) if confidence else None,
        'evidence_quality': running.get('evidence_quality'),
        'available_metrics': copy.deepcopy(running.get('available_metrics', [])),
        'missing_evidence': copy.deepcopy(running.get('missing_evidence', [])),
    }
    risk_reasons = []
    if not running:
        risk_reasons.append('source_workload_class_evidence_missing')
    else:
        if running.get('id') != workload_class:
            risk_reasons.append('source_workload_class_evidence_inconsistent')
        if confidence.get('level') != 'high':
            risk_reasons.append('source_workload_class_confidence_below_high')
        if running.get('evidence_quality') != 'complete':
            risk_reasons.append('source_workload_class_evidence_incomplete')
    return result, risk_reasons


def _source_transfer_fallback(stage, reason, **details):
    return {
        'schema_version': SOURCE_TRANSFER_SCHEMA,
        'component_id': stage.get('id'),
        'workload_class': stage.get('workload_class', 'unknown'),
        'status': 'fallback_reference_anchor',
        'reference_work_per_observed_cpu_ms': 1.0,
        'fallback_reason': reason,
        'formula': 'W_ref = T_observed (legacy reference-anchor fallback)',
        **details,
    }


def _residency_coverage(stage, residency):
    """Resolve the evidence contract without promoting partial coverage.

    Workload feature profiles produced before the explicit coverage contract
    can still be verified by conservation: every observed on-CPU millisecond
    must be represented by a mapped CPU/frequency residency row.  Newer
    profiles may additionally provide status/coverage/observed/expected fields;
    those declarations are fail-closed.
    """
    scheduling = stage.get('scheduling') if isinstance(stage.get('scheduling'), dict) else {}
    placement = scheduling.get('cpu_placement') if isinstance(scheduling.get('cpu_placement'), dict) else {}
    evidence = (placement.get('residency_evidence') or placement.get('cpu_residency_evidence')
                or scheduling.get('cpu_residency_evidence') or {})
    expected = stage.get('cpu_ms')
    observed = sum(row.get('cpu_ms', 0) for row in residency
                   if isinstance(row, dict) and _positive_number(row.get('cpu_ms')))
    if isinstance(evidence, dict) and evidence:
        evidence_schema = evidence.get('schema_version')
        declared_status = evidence.get('status')
        declared_coverage = evidence.get('coverage_ratio')
        if declared_coverage is None and isinstance(evidence.get('coverage'), (int, float)):
            declared_coverage = evidence.get('coverage')
        declared_observed = evidence.get('observed_on_cpu_ms',
                                           evidence.get('observed_cpu_ms', evidence.get('observed_ms')))
        declared_expected = evidence.get('expected_on_cpu_ms',
                                           evidence.get('expected_cpu_ms', evidence.get('expected_ms')))
        if evidence_schema != CPU_RESIDENCY_EVIDENCE_SCHEMA:
            return False, {'schema_version': evidence_schema, 'status': declared_status,
                           'coverage': declared_coverage, 'observed_cpu_ms': declared_observed,
                           'expected_cpu_ms': declared_expected,
                           'reason': 'source_residency_evidence_schema_unsupported'}
        if declared_status != 'complete' or evidence.get('source_anchor_eligible') is not True:
            return False, {'status': declared_status, 'coverage': declared_coverage,
                           'observed_cpu_ms': declared_observed, 'expected_cpu_ms': declared_expected,
                           'reason': 'source_residency_evidence_incomplete'}
        if declared_coverage is not None and (
                isinstance(declared_coverage, bool) or not isinstance(declared_coverage, (int, float))
                or not math.isfinite(declared_coverage)
                or not (COMPLETE_RESIDENCY_COVERAGE_MIN - RESIDENCY_COVERAGE_EPSILON
                        <= declared_coverage
                        <= COMPLETE_RESIDENCY_COVERAGE_MAX + RESIDENCY_COVERAGE_EPSILON)):
            return False, {'status': declared_status, 'coverage': declared_coverage,
                           'observed_cpu_ms': declared_observed, 'expected_cpu_ms': declared_expected,
                           'reason': 'source_residency_coverage_invalid'}
        if not _positive_number(declared_observed) or not _positive_number(declared_expected):
            return False, {'status': declared_status, 'coverage': declared_coverage,
                           'observed_cpu_ms': declared_observed, 'expected_cpu_ms': declared_expected,
                           'reason': 'source_residency_coverage_unknown'}
        if (_positive_number(stage.get('cpu_ms'))
                and abs(declared_expected - stage['cpu_ms']) > max(.001, stage['cpu_ms'] * .001)):
            return False, {'status': declared_status, 'coverage': declared_coverage,
                           'observed_cpu_ms': declared_observed, 'expected_cpu_ms': declared_expected,
                           'stage_cpu_ms': stage['cpu_ms'],
                           'reason': 'source_residency_evidence_inconsistent'}
        calculated_coverage = declared_observed / declared_expected
        if not (COMPLETE_RESIDENCY_COVERAGE_MIN - RESIDENCY_COVERAGE_EPSILON
                <= calculated_coverage
                <= COMPLETE_RESIDENCY_COVERAGE_MAX + RESIDENCY_COVERAGE_EPSILON):
            return False, {'status': declared_status, 'coverage': declared_coverage,
                           'calculated_coverage': calculated_coverage,
                           'observed_cpu_ms': declared_observed, 'expected_cpu_ms': declared_expected,
                           'reason': 'source_residency_coverage_invalid'}
        if (declared_coverage is not None
                and abs(declared_coverage - calculated_coverage) > .001):
            return False, {'status': declared_status, 'coverage': declared_coverage,
                           'calculated_coverage': calculated_coverage,
                           'observed_cpu_ms': declared_observed, 'expected_cpu_ms': declared_expected,
                           'reason': 'source_residency_evidence_inconsistent'}
        tolerance_ms = max(.001, declared_observed * .001)
        if abs(observed - declared_observed) > tolerance_ms:
            return False, {'status': declared_status, 'coverage': declared_coverage,
                           'observed_cpu_ms': declared_observed, 'expected_cpu_ms': declared_expected,
                           'residency_sum_cpu_ms': observed,
                           'reason': 'source_residency_evidence_inconsistent'}
        return True, {'schema_version': evidence.get('schema_version'), 'status': declared_status,
                      'coverage': declared_coverage, 'observed_cpu_ms': declared_observed,
                      'expected_cpu_ms': declared_expected, 'source_anchor_eligible': True,
                      'unobserved_share': max(0.0, 1.0 - declared_observed / declared_expected),
                      'weighting_policy': 'observed_residency_mix_applied_to_full_stage_on_cpu'}
    if not _positive_number(expected) or not _positive_number(observed):
        return False, {'status': 'unresolved', 'coverage': None, 'observed_cpu_ms': observed,
                       'expected_cpu_ms': expected, 'reason': 'source_residency_coverage_unknown'}
    coverage = observed / expected
    tolerance_ms = max(.001, expected * .001)
    complete = abs(observed - expected) <= tolerance_ms
    return complete, {'status': 'verified' if complete else 'partial', 'coverage': coverage,
                      'observed_cpu_ms': observed, 'expected_cpu_ms': expected,
                      'tolerance_ms': tolerance_ms,
                      **({} if complete else {'reason': 'source_residency_coverage_incomplete'})}


def resolve_source_cost_transfers(feature_profile, profiles, model_hardware_profile_id,
                                  model_hardware_profile_sha256=None,
                                  current_hardware_profile_sha256=None):
    """Normalize measured placement into model-reference CPU work.

    For a stage observed for ``t_i`` milliseconds at source placement ``i``,
    an absolute calibrated source curve gives ``s_i = T_source/T_reference``.
    The observed mix gives ``R_source→ref = sum(t_i/s_i)/sum(t_i)``.  The
    conserved stage reference work is ``W_ref = T_stage * R_source→ref``; this
    also makes the explicit 95–105% complete-coverage contract auditable.  The
    simulator predicts ``T_target = W_ref * s_target``.

    Any unresolved source evidence falls back for the *whole stage* to the
    historical assumption ``W_ref = T_observed``.  Mixing verified and guessed
    source rows would create a plausible-looking but unauditable result.
    """
    transfers = {}
    profile_quality = feature_profile.get('quality', {}) if isinstance(feature_profile, dict) else {}
    stages = feature_profile.get('stages', []) if isinstance(feature_profile, dict) else []
    profile_hardware_id = feature_profile.get('hardware_profile_id') if isinstance(feature_profile, dict) else None
    profile_hardware_sha256 = (feature_profile.get('hardware_profile_sha256')
                               if isinstance(feature_profile, dict) else None)
    identity_verified = profile_quality.get('hardware_identity_verified') is True
    profile_matches_model = bool(profile_hardware_id and profile_hardware_id == model_hardware_profile_id)
    for stage in stages if isinstance(stages, list) else []:
        if not isinstance(stage, dict) or not isinstance(stage.get('id'), str):
            continue
        workload_class = stage.get('workload_class', 'unknown')
        if not identity_verified:
            transfers[stage['id']] = _source_transfer_fallback(
                stage, 'source_hardware_identity_unverified',
                source_hardware_profile_id=profile_hardware_id,
                model_hardware_profile_id=model_hardware_profile_id)
            continue
        if not profile_matches_model:
            transfers[stage['id']] = _source_transfer_fallback(
                stage, 'source_hardware_profile_mismatch',
                source_hardware_profile_id=profile_hardware_id,
                model_hardware_profile_id=model_hardware_profile_id)
            continue
        if (not isinstance(profile_hardware_sha256, str)
                or not isinstance(current_hardware_profile_sha256, str)):
            transfers[stage['id']] = _source_transfer_fallback(
                stage, 'source_hardware_profile_sha256_unavailable',
                source_hardware_profile_id=profile_hardware_id,
                source_hardware_profile_sha256=profile_hardware_sha256,
                current_hardware_profile_sha256=current_hardware_profile_sha256)
            continue
        if profile_hardware_sha256 != current_hardware_profile_sha256:
            transfers[stage['id']] = _source_transfer_fallback(
                stage, 'source_hardware_profile_sha256_mismatch',
                source_hardware_profile_id=profile_hardware_id,
                source_hardware_profile_sha256=profile_hardware_sha256,
                current_hardware_profile_sha256=current_hardware_profile_sha256)
            continue
        if (not isinstance(model_hardware_profile_sha256, str)
                or model_hardware_profile_sha256 != current_hardware_profile_sha256):
            transfers[stage['id']] = _source_transfer_fallback(
                stage, ('model_hardware_profile_sha256_unavailable'
                        if not isinstance(model_hardware_profile_sha256, str)
                        else 'model_hardware_profile_sha256_mismatch'),
                model_hardware_profile_id=model_hardware_profile_id,
                model_hardware_profile_sha256=model_hardware_profile_sha256,
                current_hardware_profile_sha256=current_hardware_profile_sha256)
            continue
        if workload_class not in WORKLOAD_CLASSES:
            transfers[stage['id']] = _source_transfer_fallback(stage, 'source_workload_class_unknown')
            continue
        classification, risk_reasons = _classification_provenance(stage, workload_class)
        if 'source_workload_class_evidence_inconsistent' in risk_reasons:
            transfers[stage['id']] = _source_transfer_fallback(
                stage, 'source_workload_class_evidence_inconsistent',
                classification_provenance=classification)
            continue
        scheduling = stage.get('scheduling') if isinstance(stage.get('scheduling'), dict) else {}
        residency = scheduling.get('cpu_residency')
        if not isinstance(residency, list) or not residency:
            transfers[stage['id']] = _source_transfer_fallback(stage, 'source_residency_missing')
            continue
        coverage_ok, coverage = _residency_coverage(stage, residency)
        if not coverage_ok:
            transfers[stage['id']] = _source_transfer_fallback(
                stage, coverage.get('reason', 'source_residency_coverage_incomplete'),
                residency_coverage=coverage)
            continue
        rows, reference_identity, reference_value, failure = [], None, None, None
        for item in residency:
            core_kind = item.get('core_group_id') if isinstance(item, dict) else None
            frequency = item.get('frequency_khz') if isinstance(item, dict) else None
            duration = item.get('cpu_ms') if isinstance(item, dict) else None
            if not isinstance(core_kind, str) or core_kind not in profiles:
                failure = 'source_core_group_unknown'
                break
            if isinstance(frequency, bool) or not isinstance(frequency, int) or frequency <= 0:
                failure = 'source_frequency_unknown'
                break
            if not _positive_number(duration):
                failure = 'source_residency_duration_invalid'
                break
            source_profile = profiles[core_kind]
            try:
                factor_value, detail = profile_factor(
                    source_profile, frequency, workload_class,
                    source_profile.get('frequency_elasticity', .75))
            except (KeyError, TypeError, ValueError):
                failure = 'source_curve_invalid'
                break
            if detail.get('mode') != 'class_roofline':
                reason = detail.get('reason')
                failure = ('source_frequency_outside_calibrated_domain'
                           if reason == 'outside_calibrated_frequency_domain'
                           else 'source_curve_missing')
                break
            if detail.get('core_transfer_mode') != 'absolute_curve_ratio':
                failure = 'source_curve_not_absolute'
                break
            reference = detail.get('transfer_reference')
            identity = _transfer_reference_identity(reference)
            if identity is None:
                failure = 'source_transfer_reference_invalid'
                break
            if reference_identity is None:
                reference_identity = identity
                reference_value = copy.deepcopy(reference)
            elif identity != reference_identity:
                failure = 'source_transfer_reference_mismatch'
                break
            rows.append({
                'cpu': item.get('cpu'), 'core_group_id': core_kind,
                'frequency_khz': frequency, 'cpu_ms': duration,
                'source_time_per_reference_time': factor_value,
                'reference_cpu_ms': duration / factor_value,
                'source_curve': detail,
            })
        if failure:
            transfers[stage['id']] = _source_transfer_fallback(
                stage, failure, residency_coverage=coverage)
            continue
        residency_observed = sum(row['cpu_ms'] for row in rows)
        residency_reference_work = sum(row['reference_cpu_ms'] for row in rows)
        ratio = residency_reference_work / residency_observed
        stage_observed = stage['cpu_ms']
        stage_reference_work = stage_observed * ratio
        coverage_ratio = coverage.get('coverage')
        if (_positive_number(coverage_ratio)
                and abs(coverage_ratio - 1.0) > .001 + RESIDENCY_COVERAGE_EPSILON):
            risk_reasons.append('source_residency_complete_tolerance_extrapolation')
        transfers[stage['id']] = {
            'schema_version': SOURCE_TRANSFER_SCHEMA,
            'component_id': stage['id'], 'workload_class': workload_class,
            'status': 'verified_source_normalization',
            'observed_cpu_ms': stage_observed, 'reference_cpu_ms': stage_reference_work,
            'source_residency_observed_cpu_ms': residency_observed,
            'source_residency_reference_cpu_ms': residency_reference_work,
            'reference_work_per_observed_cpu_ms': ratio,
            'residency_coverage': coverage,
            'classification_provenance': classification,
            'risk_reasons': risk_reasons,
            'risk_level': 'medium' if risk_reasons else 'none',
            'residency': [{**row, 'observed_time_weight': row['cpu_ms'] / residency_observed}
                          for row in rows],
            'transfer_reference': reference_value,
            'formula': ('R_source_to_ref = sum_i(T_residency_i / F_source_i) / sum_i(T_residency_i); '
                        'W_ref = T_stage * R_source_to_ref; T_target = W_ref * F_target'),
        }
    verified = sum(row.get('status') == 'verified_source_normalization' for row in transfers.values())
    reasons = Counter(row.get('fallback_reason') for row in transfers.values()
                      if row.get('status') != 'verified_source_normalization')
    evidence_risks = Counter(reason for row in transfers.values()
                             for reason in row.get('risk_reasons', []))
    risk_components = sum(bool(row.get('risk_reasons')) for row in transfers.values())
    risk_level = ('high' if not verified else 'medium'
                  if verified != len(transfers) or risk_components else 'none')
    summary_status = ('verified' if transfers and verified == len(transfers) and not risk_components
                      else 'verified_with_evidence_risk'
                      if transfers and verified == len(transfers)
                      else 'mixed_verified_and_fallback' if verified
                      else 'fallback_reference_anchor')
    summary = {
        'schema_version': SOURCE_TRANSFER_SCHEMA,
        'status': summary_status,
        'feature_profile_id': feature_profile.get('id') if isinstance(feature_profile, dict) else None,
        'source_hardware_profile_id': profile_hardware_id,
        'source_hardware_profile_sha256': profile_hardware_sha256,
        'model_hardware_profile_id': model_hardware_profile_id,
        'model_hardware_profile_sha256': model_hardware_profile_sha256,
        'current_hardware_profile_sha256': current_hardware_profile_sha256,
        'hardware_identity_verified': identity_verified,
        'verification_scope': ('source_normalization_preconditions_only; target workload class, '
                               'absolute curve, and transfer reference are checked per prediction'),
        'component_count': len(transfers), 'verified_component_count': verified,
        'fallback_component_count': len(transfers) - verified,
        'fallback_reasons': dict(reasons),
        'evidence_risk_component_count': risk_components,
        'evidence_risk_reasons': dict(evidence_risks),
        'risk_level': risk_level,
        'risk_summary': ('源驻留、分类证据与曲线参考系已全部核验。' if risk_level == 'none' else
                         '源驻留迁移包含分类/覆盖容差风险或部分阶段回退；配置间比较包含源成本不确定性。' if risk_level == 'medium' else
                         '没有阶段形成可核验源成本锚；结果沿用旧参考条件假设。'),
        'formula': ('R_source_to_ref = sum_i(T_residency_i / F_source_i) / sum_i(T_residency_i); '
                    'W_ref = T_stage * R_source_to_ref; T_target = W_ref * F_target'),
        'fallback_formula': 'W_ref = T_observed when source placement cannot be verified',
        'off_cpu_policy': 'unchanged_trace_observation',
        'components': [{key: row.get(key) for key in (
            'component_id', 'workload_class', 'status', 'observed_cpu_ms',
            'reference_cpu_ms', 'reference_work_per_observed_cpu_ms',
            'residency_coverage', 'classification_provenance', 'risk_level',
            'risk_reasons', 'fallback_reason')}
                       for row in transfers.values()],
    }
    return transfers, summary


def predict_isolated_component_cpu_ms(observed_cpu_ms, source_transfer, target_profile,
                                      target_frequency_khz, workload_class):
    """Apply the same nominal source→reference→target transfer as simulation."""
    fallback_elasticity = target_profile.get('frequency_elasticity', .75)
    target_factor, target_detail = profile_factor(
        target_profile, target_frequency_khz, workload_class, fallback_elasticity)
    if target_detail.get('core_transfer_mode') != 'absolute_curve_ratio':
        target_factor *= target_profile['build_ratio']
    source_factor, source_verified, source_fallback_reason = _source_target_factor(
        source_transfer, workload_class, target_detail)
    return observed_cpu_ms * source_factor * target_factor, {
        'source_status': ('verified_source_normalization' if source_verified
                          else 'fallback_reference_anchor'),
        'source_reference_work_per_observed_cpu_ms': source_factor,
        'source_fallback_reason': source_fallback_reason,
        'target_curve': target_detail,
        'formula': 'T_target = T_observed * (W_ref/T_observed) * F_target',
    }


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def extract(directory):
    directory = Path(directory)
    frame = read(directory / 'frame.json')
    chains = [chain for chain in read(directory / 'binder_chains.json')['chains']
              if chain['kind'] == 'synchronous_stack_matched']
    waits = read(directory / 'wait_ledger.json')
    wait_by_request = {request: wait for wait in waits for request in wait['binder_request_ids']}
    builds = sorted([span for span in frame['spans'] if 'CustomNode:BuildItem' in span['name']],
                    key=lambda span: span['start_ns'])
    for previous, current in zip(builds, builds[1:]):
        if previous['end_ns'] > current['start_ns']:
            raise ValueError('Nested BuildItem scopes require exclusive ownership extraction')
    components = []
    for index, span in enumerate(builds):
        components.append({'id': f'build_{index:02d}', 'name': re.search(r'BuildItem \[([^]]+)', span['name'])[1],
                           'start_ns': span['start_ns'], 'end_ns': span['end_ns'], 'line': span['line'],
                           'wall_ms': (span['end_ns'] - span['start_ns']) / 1e6, 'cpu_ms': 0.0,
                           'binder_blocked_ms': 0.0, 'request_ids': []})

    def owner(timestamp):
        return next((item['id'] for item in components if item['start_ns'] <= timestamp < item['end_ns']), None)

    nodes, app_order, service_edges, requests = [], [], [], []
    submits = {chain['send']['ns']: chain for chain in chains}
    if len(submits) != len(chains):
        raise ValueError('Multiple submissions at one timestamp require line-order handling')
    boundaries = {frame['start_ns'], frame['end_ns']}
    boundaries.update(submits)
    for item in components:
        boundaries.update((item['start_ns'], item['end_ns']))
    phases = []
    for unit in frame['units']:
        for phase in unit['phases']:
            phases.append((phase, unit))
            boundaries.update((phase['start_ns'], phase['end_ns']))
    for wait in waits:
        boundaries.update((wait['start_ns'], wait['end_ns']))
    boundaries = sorted(boundaries)
    phase_index = 0
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        while phases[phase_index][0]['end_ns'] <= start:
            phase_index += 1
        phase, unit = phases[phase_index]
        if not phase['start_ns'] <= start < end <= phase['end_ns']:
            raise ValueError('App state partition is not contiguous')
        component = owner(start)
        if start in submits:
            chain = submits[start]
            node = {'id': f'submit_{chain["id"]}', 'kind': 'marker', 'duration_ms': 0.0,
                    'component': component, 'observed_start_ns': start, 'observed_end_ns': start,
                    'source_unit': unit['id'], 'label': f'IPC {chain["id"]} send', 'request_id': chain['id']}
            nodes.append(node)
            app_order.append(node['id'])
        matching = [wait for wait in waits if wait['start_ns'] <= start < end <= wait['end_ns']
                    and wait['binder_request_ids']]
        request = matching[0]['binder_request_ids'][0] if phase['kind'] == 'blocked' and matching else None
        kind = 'cpu' if phase['kind'] == 'on_cpu' else 'await' if request else 'delay'
        node = {'id': f'app_{index:04d}', 'kind': kind,
                'duration_ms': 0.0 if request else (end - start) / 1e6,
                'observed_duration_ms': (end - start) / 1e6, 'state': phase['kind'],
                'observed_start_ns': start, 'observed_end_ns': end, 'source_unit': unit['id'],
                'source_line': unit['source_line'], 'component': component, 'label': unit['name'],
                'request_id': request}
        nodes.append(node)
        app_order.append(node['id'])
        if request:
            service_edges.append({'source': f'reply_{request}', 'target': node['id'], 'kind': 'synchronous_result'})
        if component:
            record = next(item for item in components if item['id'] == component)
            record['cpu_ms'] += node['duration_ms'] if kind == 'cpu' else 0.0
            record['binder_blocked_ms'] += node['observed_duration_ms'] if request else 0.0

    for chain in chains:
        request = chain['id']
        wait = wait_by_request[request]
        send = chain['send']['ns']
        receive = chain['receive']['ns']
        response = chain['response']['send']['ns']
        wake = wait['end_ns']
        if not send <= receive <= response <= wake:
            raise ValueError(f'Unsupported request timing: {request}')
        resource = f'service_{chain["dest_pid"]}'
        service = chain.get('service_name', SERVICE_NAMES.get(chain['dest_pid'], str(chain['dest_pid'])))
        component = owner(send)
        request_nodes = []

        def append(suffix, kind, duration, label, **extra):
            identity = f'{suffix}_{request}'
            node = {'id': identity, 'kind': kind, 'duration_ms': duration, 'resource': resource,
                    'request_id': request, 'service': service, 'service_tid': chain['service_tid'],
                    'component': component, 'label': label, **extra}
            nodes.append(node)
            if request_nodes:
                service_edges.append({'source': request_nodes[-1], 'target': identity, 'kind': 'service_sequence'})
            request_nodes.append(identity)

        append('transport', 'delay', (receive - send) / 1e6, 'IPC入口延迟')
        service_edges.append({'source': f'submit_{request}', 'target': request_nodes[0], 'kind': 'ipc_request'})
        append('acquire', 'acquire', 0.0, service + '执行通道')
        total = 0
        for index, segment in enumerate(chain['service_state_segments']):
            start = max(receive, round(segment['start_s'] * 1e9))
            end = min(response, round(segment['end_s'] * 1e9))
            if end <= start:
                continue
            total += end - start
            append(f'server_{index}', 'server_cpu' if segment['state'] == 'on_cpu' else 'delay',
                   (end - start) / 1e6, service + ':' + segment['state'],
                   state=segment['state'], observed_start_ns=start, observed_end_ns=end)
        if abs(total - (response - receive)) > 100:
            raise ValueError(f'Service state partition mismatch: {request}')
        append('release', 'release', 0.0, service + '回复')
        append('return', 'delay', (wake - response) / 1e6, '回复到唤醒')
        append('reply', 'marker', 0.0, '调用方结果就绪')
        requests.append({'id': request, 'service': service, 'resource': resource,
                         'service_tid': chain['service_tid'], 'component': component,
                         'send_ns': send, 'receive_ns': receive, 'reply_send_ns': response, 'wake_ns': wake,
                         'blocked_ms': wait['blocked_us'] / 1000,
                         'residence_ms': (response - receive) / 1e6,
                         'server_cpu_ms': chain['service_states_ms']['on_cpu_ms'],
                         'server_wait_ms': chain['service_states_ms']['blocked_ms'],
                         'internal_wakers': [{'tid': item['tid'], 'name': item['comm']} for item in chain['service_wakeups']],
                         'send_line': chain['send']['line'], 'receive_line': chain['receive']['line'],
                         'reply_line': chain['response']['send']['line'], 'scope': chain.get('caller_scopes', [{}])[-1].get('name', 'unknown')})
        if component:
            next(item for item in components if item['id'] == component)['request_ids'].append(request)
    state_totals = Counter()
    for node in nodes:
        if node['id'].startswith('app_'):
            state_totals[node['state']] += node['observed_duration_ms']
    if abs(sum(state_totals.values()) - frame['duration_us'] / 1000) > 1e-6:
        raise ValueError('Frame duration does not conserve')
    frequency_totals = Counter()
    for unit in frame['units']:
        for interval in unit.get('cpu_frequency_us', []):
            frequency_totals[(interval['cpu'], interval['frequency_khz'])] += interval['duration_us'] / 1000
    return {'frame': {key: value for key, value in frame.items() if key not in ('spans', 'units')},
            'components': components, 'requests': requests, 'nodes': nodes, 'app_order': app_order,
            'service_edges': service_edges, 'states_ms': dict(state_totals),
            'cpu_frequency_residency': [{'cpu': cpu, 'khz': khz, 'cpu_ms': duration}
                                        for (cpu, khz), duration in sorted(frequency_totals.items(), key=lambda item: (item[0][0], item[0][1] or 0))],
            'feature_source': 'Original stage PMU models retained by source_unit; never apportioned to fragments',
            'sources': [{'path': str(directory / name),
                         'sha256': hashlib.sha256((directory / name).read_bytes()).hexdigest()}
                        for name in ('frame.json', 'binder_chains.json', 'wait_ledger.json')]}


def make_graph(extracted, parallel=False, selection=None):
    nodes = copy.deepcopy(extracted['nodes'])
    indexed = {node['id']: node for node in nodes}
    selected = set(selection if selection is not None else [item['id'] for item in extracted['components']])
    order = extracted['app_order']
    groups = defaultdict(list)
    if not parallel:
        groups['main_serial'] = list(order)
    else:
        selected_positions = [index for index, identity in enumerate(order) if indexed[identity]['component'] in selected]
        if not selected_positions:
            raise ValueError('Parallel candidate selects no components')
        first, last = min(selected_positions), max(selected_positions)
        for index, identity in enumerate(order):
            component = indexed[identity]['component']
            group = ('main_pre' if index < first else 'main_post' if index > last else component
                     if component in selected else 'main_residual')
            groups[group].append(identity)
        groups.setdefault('main_residual', [])
        groups.setdefault('main_pre', [])
        groups.setdefault('main_post', [])
    edges = copy.deepcopy(extracted['service_edges'])
    metadata = {}
    for group, identities in groups.items():
        branch = group.startswith('build_')
        metadata[group] = {'branch': branch, 'cpu_ms': sum(indexed[identity]['duration_ms'] for identity in identities
                                                          if indexed[identity]['kind'] == 'cpu'),
                           'label': next((item['name'] for item in extracted['components'] if item['id'] == group), group)}
        for identity in identities:
            indexed[identity]['group'] = group
        start, end = f'begin_{group}', f'end_{group}'
        nodes.extend([{'id': start, 'kind': 'begin', 'group': group, 'duration_ms': 0.0, 'label': group},
                      {'id': end, 'kind': 'end', 'group': group, 'duration_ms': 0.0, 'label': group}])
        sequence = [start, *identities, end]
        edges.extend({'source': source, 'target': target, 'kind': 'within_task_order'}
                     for source, target in zip(sequence, sequence[1:]))
    if parallel:
        if 'main_pre' not in groups or 'main_post' not in groups:
            raise ValueError('Candidate requires nonempty pre/post boundaries')
        for group in groups:
            if group not in ('main_pre', 'main_post'):
                edges.extend([{'source': 'end_main_pre', 'target': f'begin_{group}', 'kind': 'assumed_fork'},
                              {'source': f'end_{group}', 'target': 'begin_main_post', 'kind': 'required_join'}])
    graph = {'variant': 'conditional_build_fanout' if parallel else 'observed_serial',
             'nodes': nodes, 'edges': edges, 'groups': metadata,
             'terminal': 'end_main_post' if parallel else 'end_main_serial',
             'assumptions': ['All selected component inputs are ready at the common fork',
                             'Selected components may build independently, including their preserved IPC calls',
                             'Residual main work does not produce inputs required by detached branches',
                             'Result consumption is deferred to the common join'] if parallel else [],
             'service_model': 'Effective receive-to-reply residence gate; internal waits preserved, not pure CPU demand',
             'serial_retained': ['pre-fork main', 'non-BuildItem main residual', 'post-join main',
                                 'intra-component order and IPC-result edges', 'finite-capacity service gates']}
    validate_graph(graph)
    return graph


def validate_graph(graph):
    indexed = {node['id']: node for node in graph['nodes']}
    if len(indexed) != len(graph['nodes']):
        raise ValueError('Duplicate node')
    counts = Counter()
    children = defaultdict(list)
    for edge in graph['edges']:
        if edge['source'] not in indexed or edge['target'] not in indexed:
            raise ValueError('Missing endpoint')
        counts[edge['target']] += 1
        children[edge['source']].append(edge['target'])
    ready = [identity for identity in indexed if not counts[identity]]
    visited = 0
    while ready:
        identity = ready.pop()
        visited += 1
        for child in children[identity]:
            counts[child] -= 1
            if not counts[child]:
                ready.append(child)
    if visited != len(indexed):
        raise ValueError('Dependency cycle')
    return True


def simulate(graph, configuration, profiles, elasticity=None, contention=True,
             service_capacity=1, overhead_ms=0.05, keep_timeline=False, interference_model=None):
    """Simulate reference work; None selects each used core profile's nominal response.

    An explicit scalar elasticity is a sensitivity override. A profile without
    frequency_elasticity retains the historical nominal 0.75 response. A typed
    curve with absolute target/reference anchors replaces the legacy core ratio
    for classified component CPU work; old normalized curves and unclassified
    work keep the historical ratio and max_khz interpretation.
    """
    if (elasticity is not None and
            (not isinstance(elasticity, (int, float)) or not math.isfinite(elasticity) or not 0 <= elasticity <= 1)):
        raise ValueError('Invalid cost parameters')
    if not math.isfinite(overhead_ms) or overhead_ms < 0:
        raise ValueError('Invalid cost parameters')
    capacities = service_capacity.values() if isinstance(service_capacity, dict) else [service_capacity]
    if any(not isinstance(capacity, int) or capacity < 1 for capacity in capacities):
        raise ValueError('Service capacity must be a positive integer')
    indexed = {node['id']: node for node in graph['nodes']}
    observed_app_states = Counter()
    for node in graph['nodes']:
        if node.get('group') is not None and node.get('state') and node.get('kind') in ('cpu', 'delay', 'await'):
            observed_app_states[node['state']] += node.get('observed_duration_ms', node.get('duration_ms', 0.0))
    observed_app_on_cpu_ms = observed_app_states.get('on_cpu', 0.0)
    observed_app_off_cpu_ms = sum(duration for state, duration in observed_app_states.items() if state != 'on_cpu')
    observed_synchronous_blocked_ms = sum(
        node.get('observed_duration_ms', 0.0) for node in graph['nodes']
        if node.get('group') is not None and node.get('kind') == 'await')
    observed_non_binder_off_cpu_ms = observed_app_off_cpu_ms - observed_synchronous_blocked_ms
    parents = defaultdict(set)
    children = defaultdict(list)
    for edge in graph['edges']:
        parents[edge['target']].add(edge['source'])
        children[edge['source']].append(edge['target'])
    pending = {identity: len(parents[identity]) for identity in indexed}
    ready = {identity for identity in indexed if pending[identity] == 0}
    lanes = [configuration['main'], *configuration['workers']]
    profile_elasticities = {}
    if elasticity is None:
        for kind in sorted({lane['kind'] for lane in lanes}):
            value = profiles[kind].get('frequency_elasticity', 0.75)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f'Invalid frequency_elasticity for core group {kind}: expected a finite value in [0, 1]')
            profile_elasticities[kind] = value
    owners = [None] * len(lanes)
    group_lanes = {}
    gates = Counter()
    active, finished = {}, {}
    starts, ready_at, queue_waits = {}, defaultdict(float), {}
    causal_parent, causal_kind = {}, {}
    lane_release = {}
    gate_release = {}
    timeline = []
    interference = {'schema_version': 'parallel_interference_usage/v1',
                    'model_version': (interference_model or {}).get('version'),
                    'scope': 'directional two-task proxy transfer at measured core-group frequencies; physical placement equivalence is not established',
                    'calibrated_cpu_ms': 0.0, 'fallback_cpu_ms': 0.0,
                    'fallback_reasons': {}, 'matched_cells': []}
    matched_cells, fallback_reasons = set(), defaultdict(float)
    frequency_model_usage = {'schema_version': 'frequency_model_usage/v1',
                             'class_roofline_core_ms': 0.0, 'legacy_fallback_core_ms': 0.0,
                             'absolute_curve_core_ms': 0.0, 'legacy_ratio_curve_core_ms': 0.0,
                             'matched_models': [], 'fallback_reasons': {}}
    matched_frequency_models, frequency_fallback_reasons = set(), defaultdict(float)
    source_transfers = graph.get('component_source_cost_transfers', {})
    source_transfer_usage = {
        'schema_version': SOURCE_TRANSFER_SCHEMA,
        'formula': ('R_source_to_ref = sum_i(T_residency_i / F_source_i) / sum_i(T_residency_i); '
                    'W_ref = T_stage * R_source_to_ref; T_target = W_ref * F_target'),
        'verified_predicted_core_ms': 0.0, 'fallback_predicted_core_ms': 0.0,
        'verified_components': set(), 'fallback_components': set(), 'fallback_reasons': defaultdict(float),
    }
    timestamp, cpu_ms, server_cpu_ms, peak, overhead_pending = 0.0, 0.0, 0.0, 0, set()

    def finish(identity):
        node = indexed[identity]
        finished[identity] = timestamp
        group = node.get('group')
        if node['kind'] == 'end':
            lane = group_lanes[group]
            owners[lane] = None
            lane_release[lane] = identity
        if node['kind'] == 'release':
            gates[node['resource']] -= 1
            gate_release[node['resource']] = identity
        for child in children[identity]:
            pending[child] -= 1
            if timestamp >= ready_at[child]:
                ready_at[child] = timestamp
                causal_parent[child] = identity
                causal_kind[child] = 'dag_dependency'
            if pending[child] == 0:
                ready.add(child)

    def priority(identity):
        node = indexed[identity]
        group = node.get('group')
        if node['kind'] == 'acquire':
            return (0, ready_at[identity], identity)
        if node['kind'] == 'begin':
            branch = graph['groups'][group]['branch']
            main_first = configuration.get('dispatch', 'main_first') == 'main_first'
            return (2 if branch == main_first else 1, -graph['groups'][group]['cpu_ms'], identity)
        return (0, 0, identity)

    while graph['terminal'] not in finished:
        progressed = True
        while progressed:
            progressed = False
            for identity in sorted(ready, key=priority):
                node = indexed[identity]
                group = node.get('group')
                if node['kind'] == 'begin':
                    branch = graph['groups'][group]['branch']
                    candidates = list(range(1, len(lanes))) if branch else [0]
                    if branch and configuration.get('main_participates'):
                        candidates = [0, *candidates]
                    available = [lane for lane in candidates if owners[lane] is None]
                    if not available:
                        continue
                    lane = available[0]
                    owners[lane] = group
                    group_lanes[group] = lane
                    if lane in lane_release and finished[lane_release[lane]] > ready_at[identity]:
                        causal_parent[identity] = lane_release[lane]
                        causal_kind[identity] = 'application_lane_available'
                    if branch and overhead_ms:
                        overhead_pending.add(identity)
                if node['kind'] == 'acquire':
                    capacity = service_capacity[node['resource']] if isinstance(service_capacity, dict) else service_capacity
                    if gates[node['resource']] >= capacity:
                        continue
                    gates[node['resource']] += 1
                    queue_waits[identity] = timestamp - ready_at[identity]
                    predecessor = gate_release.get(node['resource'])
                    if predecessor and finished[predecessor] > ready_at[identity]:
                        causal_parent[identity] = predecessor
                        causal_kind[identity] = 'service_capacity_available'
                ready.remove(identity)
                starts[identity] = timestamp
                duration = overhead_ms if identity in overhead_pending else node['duration_ms']
                if duration > 1e-10:
                    active[identity] = duration
                else:
                    finish(identity)
                progressed = True
        if graph['terminal'] in finished:
            break
        if not active:
            raise RuntimeError('DAG/resource deadlock')
        running = [identity for identity in active if indexed[identity]['kind'] == 'cpu' or identity in overhead_pending]
        peak = max(peak, len(running))
        rates = {}
        interval_interference = {}
        interval_frequency = {}
        interval_source_transfer = {}
        for identity in active:
            node = indexed[identity]
            if identity not in running:
                rates[identity] = 1.0
                continue
            lane = lanes[group_lanes[node['group']]]
            profile = profiles[lane['kind']]
            lane_elasticity = profile_elasticities[lane['kind']] if elasticity is None else elasticity
            category = 'build_ratio' if node.get('component') else 'residual_ratio'
            classes = graph.get('component_classes', {})
            workload_class = node.get('workload_class', classes.get(node.get('component'), 'unknown'))
            source_transfer = source_transfers.get(node.get('component'), {})
            if elasticity is None and node.get('component'):
                frequency_multiplier, frequency_detail = profile_factor(
                    profile, lane['khz'], workload_class, lane_elasticity)
            else:
                frequency_multiplier = ((1 - lane_elasticity)
                                        + lane_elasticity * profile['max_khz'] / lane['khz'])
                frequency_detail = {'mode': 'legacy_elasticity_fallback',
                                    'workload_class': workload_class,
                                    'frequency_elasticity': lane_elasticity,
                                    'reason': 'explicit_sensitivity_override' if elasticity is not None
                                              else 'non_component_cpu'}
            source_factor, source_verified, source_fallback_reason = _source_target_factor(
                source_transfer, workload_class, frequency_detail)
            # A new calibrated class curve carries an absolute unit-cost ratio
            # to the model reference group. It already represents the core
            # transfer as well as the target OPP response, so multiplying the
            # legacy build_ratio would count the core difference twice. Old v1
            # normalized curves have no such anchor and retain their original
            # ratio × curve behavior.
            multiplier = (frequency_multiplier
                          if frequency_detail.get('core_transfer_mode') == 'absolute_curve_ratio'
                          else profile[category] * frequency_multiplier)
            # ``node.duration_ms`` is an observed source-placement cost.  A
            # verified placement first converts it to model-reference work;
            # the target curve above then converts that work to target time.
            # Off-CPU nodes never enter this branch and remain unchanged.
            multiplier *= source_factor
            interval_frequency[identity] = (lane['kind'], frequency_detail)
            if node.get('component'):
                interval_source_transfer[identity] = (
                    node['component'], source_verified,
                    source_fallback_reason,
                    source_factor)
            if contention and node.get('component'):
                factor = None
                if len(running) > 1:
                    peer_identity = next(value for value in running if value != identity)
                    peer = indexed[peer_identity]
                    peer_lane = lanes[group_lanes[peer['group']]]
                    victim_class = node.get('workload_class', classes.get(node.get('component'), 'unknown'))
                    peer_class = peer.get('workload_class', classes.get(peer.get('component'), 'unknown')) if peer_identity not in overhead_pending else 'unknown'
                    factor, reason, cell = calibrated_factor(interference_model, victim_class, peer_class, lane, peer_lane, len(running))
                    interval_interference[identity] = (reason, cell)
                multiplier *= factor if factor is not None else 1 + profile['alpha'] * max(0, len(running) - 1)
            rates[identity] = 1 / multiplier
        delta = min(active[identity] / rates[identity] for identity in active)
        for identity, (core_kind, detail) in interval_frequency.items():
            if detail['mode'] == 'class_roofline':
                frequency_model_usage['class_roofline_core_ms'] += delta
                transfer_mode = detail.get('core_transfer_mode', 'legacy_profile_ratio')
                frequency_model_usage['absolute_curve_core_ms' if transfer_mode == 'absolute_curve_ratio'
                                      else 'legacy_ratio_curve_core_ms'] += delta
                matched_frequency_models.add((core_kind, detail['workload_class'], detail['knee_khz'],
                                              detail['above_knee_exponent'], detail['below_knee_exponent'],
                                              transfer_mode, detail.get('cross_core_time_ratio', 1.0)))
            else:
                frequency_model_usage['legacy_fallback_core_ms'] += delta
                frequency_fallback_reasons[detail['reason']] += delta
        for reason, cell in interval_interference.values():
            if cell:
                interference['calibrated_cpu_ms'] += delta
                matched_cells.add(cell)
            else:
                interference['fallback_cpu_ms'] += delta
                fallback_reasons[reason] += delta
        for component, verified, reason, _ in interval_source_transfer.values():
            if verified:
                source_transfer_usage['verified_predicted_core_ms'] += delta
                source_transfer_usage['verified_components'].add(component)
            else:
                source_transfer_usage['fallback_predicted_core_ms'] += delta
                source_transfer_usage['fallback_components'].add(component)
                source_transfer_usage['fallback_reasons'][reason] += delta
        cpu_ms += delta * len(running)
        server_cpu_ms += delta * sum(indexed[identity]['kind'] == 'server_cpu' for identity in active)
        if keep_timeline:
            for identity in active:
                node = indexed[identity]
                timeline.append({'node': identity, 'group': node.get('group'), 'kind': node['kind'],
                                 'resource': node.get('resource'), 'request_id': node.get('request_id'),
                                 'lane': group_lanes.get(node.get('group')), 'start_ms': timestamp,
                                 'end_ms': timestamp + delta})
        timestamp += delta
        done = []
        for identity in active:
            active[identity] -= delta * rates[identity]
            if active[identity] < 1e-8:
                done.append(identity)
        for identity in done:
            del active[identity]
            finish(identity)
    path, identity = [], graph['terminal']
    while identity is not None:
        path.append(identity)
        identity = causal_parent.get(identity)
    result = {'finish_ms': timestamp, 'app_core_ms': cpu_ms, 'service_core_ms': server_cpu_ms,
              'app_off_cpu_observed_ms': observed_app_off_cpu_ms,
              'service_queue_sum_ms': sum(queue_waits.values()),
              'service_queue_max_ms': max(queue_waits.values(), default=0),
              'peak_app_cpu_concurrency': peak, 'critical_chain': list(reversed(path)),
              'group_lanes': group_lanes, 'completed_nodes': len(finished)}
    frequency_model_usage.update(
        matched_models=[{'core_kind': kind, 'workload_class': workload_class, 'knee_khz': knee,
                         'above_knee_exponent': above, 'below_knee_exponent': below,
                         'core_transfer_mode': transfer_mode,
                         **({'cross_core_time_ratio': ratio}
                            if transfer_mode == 'absolute_curve_ratio' else {})}
                        for kind, workload_class, knee, above, below, transfer_mode, ratio
                        in sorted(matched_frequency_models)],
        fallback_reasons=dict(frequency_fallback_reasons),
        status='class_roofline' if matched_frequency_models and not frequency_fallback_reasons
               else 'mixed_roofline_and_fallback' if matched_frequency_models
               else 'legacy_elasticity_fallback')
    result['frequency_model_usage'] = frequency_model_usage
    source_transfer_usage.update(
        verified_components=sorted(source_transfer_usage['verified_components']),
        fallback_components=sorted(source_transfer_usage['fallback_components']),
        fallback_reasons=dict(source_transfer_usage['fallback_reasons']))
    source_transfer_usage['status'] = (
        'verified' if source_transfer_usage['verified_components'] and not source_transfer_usage['fallback_components']
        else 'mixed_verified_and_fallback' if source_transfer_usage['verified_components']
        else 'fallback_reference_anchor')
    result['source_placement_cost_transfer'] = source_transfer_usage
    result['time_accounting'] = {
        'schema_version': 'parallel_time_accounting/v1',
        'equation': 'observed_app_wall = observed_app_on_cpu + observed_app_off_cpu; predicted_total_latency = DAG_makespan(dependencies, application_lanes, service_resources)',
        'observed_reference': {
            'app_on_cpu_ms': observed_app_on_cpu_ms,
            'app_off_cpu_ms': observed_app_off_cpu_ms,
            'app_wall_ms': observed_app_on_cpu_ms + observed_app_off_cpu_ms,
            'accounting_identity': 'app_wall_ms = app_on_cpu_ms + app_off_cpu_ms',
            'off_cpu_states_ms': {state: observed_app_states.get(state, 0.0)
                                  for state in ('runnable', 'blocked', 'unknown')},
            'non_binder_off_cpu_ms': observed_non_binder_off_cpu_ms,
            'synchronous_blocked_ms': observed_synchronous_blocked_ms,
        },
        'prediction': {'total_latency_ms': timestamp, 'dag_makespan_ms': timestamp,
                       'application_core_ms': cpu_ms, 'service_core_ms': server_cpu_ms,
                       'meaning': 'Predicted latency is the dependency/resource DAG makespan, not observed_off_cpu plus modeled_on_cpu.'},
        'off_cpu_policy': {
            'non_binder_delay': 'Trace-observed duration remains on its delay node and is not frequency-scaled.',
            'synchronous_blocked': 'The caller await has no directly added duration; request transport, service residence, reply and resource dependencies determine the wait in the DAG.',
            'observed_totals': 'Observed blocked/runnable/unknown totals are accounting evidence and are not added again to the predicted makespan.',
        },
    }
    interference.update(fallback_reasons=dict(fallback_reasons), matched_cells=sorted(matched_cells),
                        status='disabled' if not contention else 'mixed_calibrated_and_fallback' if matched_cells and fallback_reasons else 'calibrated_pair_proxy' if matched_cells else 'legacy_alpha_fallback' if fallback_reasons else 'no_overlapping_build_cpu')
    result['interference'] = interference
    if len(finished) != len(indexed) or any(gates.values()) or any(owner is not None for owner in owners):
        raise RuntimeError('Terminal reached with unfinished work or reserved resources')
    if keep_timeline:
        result.update(timeline=timeline, node_start_ms=starts, node_end_ms=finished,
                      queue_waits_ms=queue_waits,
                      critical_edges=[{'source': source, 'target': target, 'kind': causal_kind[target]}
                                      for source, target in zip(result['critical_chain'], result['critical_chain'][1:])])
    return result
