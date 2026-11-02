# Heterogeneous Evaluation Benchmark

This directory is an evaluation boundary, not a knowledge source.  Nothing in
this directory may be consumed by KB construction, pattern promotion, or
runtime matching configuration.

## Hardware-KB strategy regression

`hardware_kb_strategy_skill_cases.json` is a separate, synthetic semantic
regression suite for the browser-facing `strategy-hardware-kb` Prompt. It has
60 public code/Trace inputs in smoke/development/validation/holdout splits and
evaluator-side hidden expectations for API context, hardware-unit
rejection/selection, CPU parallelism and device gates.
It must never be used as a KB source, a production matching rule, a hardware
capability claim, or a substitute for the independent source-backed benchmark
defined below. See `../../hardware_kb_context_strategy_evaluation_spec.md`.

```bash
python3 docs_ffrt/tools/evaluate_hardware_kb_strategy.py preflight
python3 docs_ffrt/tools/evaluate_hardware_kb_strategy.py self-check
```

Rebuild the versioned synthetic suite before changing its case source:

```bash
python3 docs_ffrt/tools/build_hardware_kb_strategy_evaluation_suite.py
```

Those commands validate context retrieval and scorer consistency only. A real
model run is started from the local workbench with a user-owned browser model
configuration; the model receives no oracle fields. The diagnostic 100-point
case score is not a release target; compare hardware selection/rejection, CPU
parallel, calibration, verification and split/family metrics instead.

`evaluation_policy.json` defines the minimum evidence and quality gates for the
first 20-case baseline.  The generated `initial_annotation_queue.json` contains
real source-backed discovery leads, but it has no oracle labels and must not be
used to report precision, recall, or KB coverage.

## Lifecycle

1. Select source-backed leads into an annotation queue.
2. Record an external, independent decision with two distinct reviewers.
3. Materialize only approved decisions into `heterogeneous_evaluation_case`
   JSONL records.
4. Freeze the case hashes, one source snapshot, KB manifest, recognizer version,
   policy, and exclusions into a benchmark manifest.
5. Export oracle-free run inputs, build a masked KB/source view for
   generalization cases, and run the recognizer only on those inputs.
6. Keep golden cases and the full manifest evaluator-side, then score the
   predictions against that manifest.

Use `annotation_decision.template.json` as the field template for each
external decision. It is intentionally only a template: placeholder values are
not a valid approved case and must never be counted as benchmark evidence.

The supported oracle classes are:

- `existing_heterogeneous_implementation`: KB extraction/retrieval should
  recognize an implementation that is already enabled; it is not a new action.
- `optimization_opportunity`: the reviewed workload/constraint evidence supports
  a new heterogeneous optimization.
- `ineligible`: a hardware/cost/semantic constraint makes the code unsuitable.
- `evidence_required`: the tool must stop rather than upgrade an incomplete
  signal into an opportunity.

## Commands

```bash
# Generate 20 real, source-backed items for external annotation.  This is not a
# benchmark result and does not label a case positive or negative.
python docs_ffrt/tools/evaluation_benchmark.py bootstrap-review-queue \
  --catalog heterogeneous-enable-web/data/knowledge_case_catalog.json \
  --sample-size 20 \
  --seed openharmony-evaluation-bootstrap-v1 \
  --out docs_ffrt/tools/evaluation/initial_annotation_queue.json

# After independent reviewers supply annotation decisions, produce the only
# case format accepted by the scorer.
python docs_ffrt/tools/evaluation_benchmark.py materialize-cases \
  --queue docs_ffrt/tools/evaluation/initial_annotation_queue.json \
  --decisions /path/to/approved_decisions.jsonl \
  --policy docs_ffrt/tools/evaluation/evaluation_policy.json \
  --out /tmp/evaluation_cases.jsonl

# Freeze the experiment.  A benchmark may use only one source snapshot.
python docs_ffrt/tools/evaluation_benchmark.py build-manifest \
  --cases /tmp/evaluation_cases.jsonl \
  --policy docs_ffrt/tools/evaluation/evaluation_policy.json \
  --kb docs_ffrt/tools/heterogeneous_kb \
  --detector-name heterogeneous-opportunity-engine \
  --detector-version 1.0.0 \
  --out /tmp/evaluation_manifest.json

# Generalization cases must not leave their reviewed implementation/mapping in
# the recognizer's KB view.  The derived KB is development-only.
python docs_ffrt/tools/evaluation_benchmark.py mask-kb \
  --manifest /tmp/evaluation_manifest.json \
  --kb docs_ffrt/tools/heterogeneous_kb \
  --source-root /path/to/oh-workspace \
  --out-kb /tmp/evaluation_masked_kb

# Give the recognizer this sanitized input, not evaluation_cases.jsonl or the
# evaluator manifest. It contains no oracle, leakage guard, or benchmark role.
python docs_ffrt/tools/evaluation_benchmark.py export-inputs \
  --cases /tmp/evaluation_cases.jsonl \
  --manifest /tmp/evaluation_manifest.json \
  --policy docs_ffrt/tools/evaluation/evaluation_policy.json \
  --out /tmp/evaluation_run_inputs.jsonl

# Prediction records must contain the frozen manifest ID and case hash. The
# scorer refuses missing, duplicate, stale, unbound, or leaked input.
python docs_ffrt/tools/evaluation_benchmark.py evaluate \
  --cases /tmp/evaluation_cases.jsonl \
  --run-inputs /tmp/evaluation_run_inputs.jsonl \
  --predictions /tmp/evaluation_predictions.jsonl \
  --manifest /tmp/evaluation_manifest.json \
  --policy docs_ffrt/tools/evaluation/evaluation_policy.json \
  --analysis-kb /tmp/evaluation_masked_kb \
  --out /tmp/evaluation_report.json \
  --require-quality-gate
```

An `in_repo_coverage` case may run against the complete checkout to measure
coverage of known implementations.  A `generalization` case must use
`masked_kb` or `masked_kb_and_source`; its exact mapping/source evidence is
listed in the manifest exclusions and checked before scoring.

Exact `mapping_id` Recall@K is an `in_repo_coverage` metric only. A masked
generalization run is scored by its target-unit Recall@K and classification;
it is rejected if a prediction echoes one of that case's excluded mapping IDs.
This prevents an oracle identifier from being mistaken for generalization.

The scorer also requires the supplied policy ID/version/content hash to equal
the frozen manifest and requires the analysis KB to carry the matching
`evaluation_mask` binding. A full-coverage report produced from a different
policy or a plain KB is therefore rejected rather than silently reinterpreted.

`heterogeneous_evaluation_cases.jsonl` and the complete benchmark manifest are
golden evaluator artifacts. In the future web/agent integration, only the
oracle-free `heterogeneous_evaluation_run_input` artifact may be supplied to a
recognizer or model during a held-out run.

`mask-kb` preserves source-provenance validation while creating its temporary
output. It auto-discovers a workspace containing `source_code/` from the base
KB; pass `--source-root` when the KB artifact is detached from that checkout.
