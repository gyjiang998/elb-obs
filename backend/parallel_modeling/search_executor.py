"""Bounded process-parallel execution for independent search simulations.

Only pure ``service_dag.simulate`` calls run in child processes.  Selection,
ranking and persistence remain in the caller so parallel execution cannot
change result ordering or expose partially written search records.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
import math
import multiprocessing
import os
from typing import Any, Callable, Iterable

from . import service_dag


DEFAULT_PROCESS_LIMIT = 8
PARALLEL_MIN_CONFIGURATIONS = 64
_WORKER_PARALLEL_GRAPH: dict[str, Any] | None = None
_WORKER_OBSERVED_GRAPH: dict[str, Any] | None = None
_WORKER_PROFILES: dict[str, Any] | None = None
_WORKER_NOMINAL_KWARGS: dict[str, Any] | None = None
_WORKER_CANCEL_EVENT: Any = None
_SEARCH_RESULT_FIELDS = (
    "finish_ms", "app_core_ms", "app_off_cpu_observed_ms", "service_core_ms", "service_queue_sum_ms",
    "service_queue_max_ms", "peak_app_cpu_concurrency", "interference", "frequency_model_usage",
    "source_placement_cost_transfer", "time_accounting",
)


class SearchEvaluationCancelled(Exception):
    """The caller cancelled a search before all simulations completed."""


def _available_cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def process_worker_count(configuration_count: int) -> int:
    """Resolve a conservative worker count, including an operator override."""

    if configuration_count < 1:
        return 1
    configured = os.environ.get("HETERO_PARALLEL_SEARCH_WORKERS", "").strip()
    if configured:
        try:
            requested = max(1, min(DEFAULT_PROCESS_LIMIT, int(configured)))
        except ValueError:
            requested = min(DEFAULT_PROCESS_LIMIT, _available_cpu_count())
        return min(configuration_count, requested, _available_cpu_count())
    if configuration_count < PARALLEL_MIN_CONFIGURATIONS:
        return 1
    return min(configuration_count, DEFAULT_PROCESS_LIMIT, _available_cpu_count())


def _initialize_worker(
    parallel_graph: dict[str, Any],
    observed_graph: dict[str, Any],
    profiles: dict[str, Any],
    nominal_kwargs: dict[str, Any],
    cancel_event: Any,
) -> None:
    global _WORKER_PARALLEL_GRAPH, _WORKER_OBSERVED_GRAPH, _WORKER_PROFILES
    global _WORKER_NOMINAL_KWARGS, _WORKER_CANCEL_EVENT
    _WORKER_PARALLEL_GRAPH = parallel_graph
    _WORKER_OBSERVED_GRAPH = observed_graph
    _WORKER_PROFILES = profiles
    _WORKER_NOMINAL_KWARGS = nominal_kwargs
    _WORKER_CANCEL_EVENT = cancel_event


def _simulate_batch(batch: list[tuple[int, dict[str, Any]]]) -> list[tuple[int, dict[str, Any]]]:
    if (_WORKER_PARALLEL_GRAPH is None or _WORKER_OBSERVED_GRAPH is None
            or _WORKER_PROFILES is None or _WORKER_NOMINAL_KWARGS is None):
        raise RuntimeError("parallel search worker was not initialized")
    rows: list[tuple[int, dict[str, Any]]] = []
    for index, configuration in batch:
        if _WORKER_CANCEL_EVENT is not None and _WORKER_CANCEL_EVENT.is_set():
            break
        graph = _WORKER_PARALLEL_GRAPH if configuration["workers"] else _WORKER_OBSERVED_GRAPH
        result = service_dag.simulate(
            graph, configuration, _WORKER_PROFILES, **_WORKER_NOMINAL_KWARGS)
        rows.append((index, {key: result[key] for key in _SEARCH_RESULT_FIELDS}))
    return rows


def _batches(configurations: list[dict[str, Any]], worker_count: int) -> Iterable[list[tuple[int, dict[str, Any]]]]:
    # Keep several batches per process for load balancing without placing one
    # future per configuration on the executor queue.
    size = max(1, min(32, math.ceil(len(configurations) / max(1, worker_count * 8))))
    indexed = list(enumerate(configurations))
    for offset in range(0, len(indexed), size):
        yield indexed[offset:offset + size]


def _serial_simulations(
    parallel_graph: dict[str, Any],
    observed_graph: dict[str, Any],
    configurations: list[dict[str, Any]],
    profiles: dict[str, Any],
    nominal_kwargs: dict[str, Any],
    cancelled: Callable[[], bool],
    on_progress: Callable[[int, int], None],
) -> list[tuple[int, dict[str, Any]]]:
    rows: list[tuple[int, dict[str, Any]]] = []
    total = len(configurations)
    for index, configuration in enumerate(configurations):
        if cancelled():
            raise SearchEvaluationCancelled
        graph = parallel_graph if configuration["workers"] else observed_graph
        rows.append((index, service_dag.simulate(graph, configuration, profiles, **nominal_kwargs)))
        on_progress(index + 1, total)
    return rows


def _parallel_simulations(
    parallel_graph: dict[str, Any],
    observed_graph: dict[str, Any],
    configurations: list[dict[str, Any]],
    profiles: dict[str, Any],
    nominal_kwargs: dict[str, Any],
    worker_count: int,
    cancelled: Callable[[], bool],
    on_progress: Callable[[int, int], None],
) -> list[tuple[int, dict[str, Any]]]:
    # ``spawn`` is safe when Web search runs inside its background thread and
    # is portable to every Python platform used by the repository.  The static
    # graphs are transferred once per worker through the initializer.
    context = multiprocessing.get_context("spawn")
    process_cancel = context.Event()
    executor: ProcessPoolExecutor | None = None
    futures = set()
    results: list[tuple[int, dict[str, Any]]] = []
    completed = 0
    total = len(configurations)
    clean_shutdown = False
    try:
        executor = ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=context,
            initializer=_initialize_worker,
            initargs=(parallel_graph, observed_graph, profiles, nominal_kwargs, process_cancel),
        )
        futures = {executor.submit(_simulate_batch, batch)
                   for batch in _batches(configurations, worker_count)}
        while futures:
            if cancelled():
                process_cancel.set()
                raise SearchEvaluationCancelled
            done, futures = wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
            for future in done:
                for item in future.result():
                    results.append(item)
                    completed += 1
                    on_progress(completed, total)
                    if cancelled():
                        process_cancel.set()
                        raise SearchEvaluationCancelled
        if len(results) != total:
            raise RuntimeError(f"parallel search returned {len(results)} of {total} simulations")
        clean_shutdown = True
        return sorted(results, key=lambda item: item[0])
    finally:
        if not clean_shutdown:
            process_cancel.set()
            for future in futures:
                future.cancel()
        if executor is not None:
            executor.shutdown(wait=clean_shutdown, cancel_futures=not clean_shutdown)


def simulate_configurations(
    *,
    parallel_graph: dict[str, Any],
    observed_graph: dict[str, Any],
    configurations: list[dict[str, Any]],
    profiles: dict[str, Any],
    nominal_kwargs: dict[str, Any],
    cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    """Simulate configurations with bounded processes and deterministic order.

    Process infrastructure failures fall back to the side-effect-free serial
    evaluator.  Simulation errors themselves are propagated instead of being
    hidden by a retry.
    """

    is_cancelled = cancelled or (lambda: False)
    report = on_progress or (lambda _completed, _total: None)
    workers = process_worker_count(len(configurations))
    if workers <= 1:
        return _serial_simulations(
            parallel_graph, observed_graph, configurations, profiles,
            nominal_kwargs, is_cancelled, report)
    try:
        return _parallel_simulations(
            parallel_graph, observed_graph, configurations, profiles,
            nominal_kwargs, workers, is_cancelled, report)
    except SearchEvaluationCancelled:
        raise
    except (BrokenProcessPool, OSError, AssertionError):
        if is_cancelled():
            raise SearchEvaluationCancelled
        return _serial_simulations(
            parallel_graph, observed_graph, configurations, profiles,
            nominal_kwargs, is_cancelled, report)

