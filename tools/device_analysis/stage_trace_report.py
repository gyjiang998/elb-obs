#!/usr/bin/env python3
"""Build a stage-oriented hotspot report from a TraceStreamer database."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    name: str
    start_marker: str
    end_marker: str


def parse_stage_spec(value: str) -> StageSpec:
    parts = [part.strip() for part in value.split("|", 3)]
    if len(parts) != 4 or any(not part for part in parts):
        raise argparse.ArgumentTypeError("阶段格式必须为 ID|名称|START_MARKER|END_MARKER")
    return StageSpec(*parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _event_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": str(row["name"]),
        "ts_ms": round(int(row["ts"]) / 1_000_000.0, 6),
        "dur_ms": round(int(row["dur"]) / 1_000_000.0, 6),
        "process_name": str(row["process_name"] or "unknown"),
        "pid": int(row["pid"] if row["pid"] is not None else -1),
        "thread_name": str(row["thread_name"] or "unknown"),
        "tid": int(row["tid"] if row["tid"] is not None else -1),
        "itid": int(row["itid"] if row["itid"] is not None else -1),
    }


def _marker(connection: sqlite3.Connection, name: str) -> sqlite3.Row:
    rows = connection.execute(
        """
        SELECT c.name, c.ts, c.dur,
               COALESCE(p.name, 'unknown') AS process_name, p.pid,
               COALESCE(t.name, 'unknown') AS thread_name, t.tid, t.itid
        FROM callstack c
        LEFT JOIN thread t ON t.itid = c.callid
        LEFT JOIN process p ON p.ipid = t.ipid
        WHERE c.name = ?
        ORDER BY c.ts
        """,
        (name,),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"阶段锚点 {name!r} 应恰好出现一次，实际为 {len(rows)} 次")
    return rows[0]


def _filter_sql(
    process_ids: list[int],
    exclude_patterns: list[str],
    excluded_names: list[str],
) -> tuple[str, list[Any]]:
    clauses = ["TRIM(COALESCE(c.name, '')) != ''"]
    parameters: list[Any] = []
    if excluded_names:
        clauses.append(f"c.name NOT IN ({','.join('?' for _ in excluded_names)})")
        parameters.extend(excluded_names)
    if process_ids:
        clauses.append(f"p.pid IN ({','.join('?' for _ in process_ids)})")
        parameters.extend(process_ids)
    for pattern in exclude_patterns:
        clauses.append("c.name NOT LIKE ?")
        parameters.append(f"%{pattern}%")
    return " AND ".join(clauses), parameters


def _stage_events(
    connection: sqlite3.Connection,
    *,
    start_ns: int,
    end_ns: int,
    process_ids: list[int],
    exclude_patterns: list[str],
    excluded_names: list[str],
    limit: int,
) -> tuple[int, float, list[dict[str, Any]]]:
    filters, filter_parameters = _filter_sql(process_ids, exclude_patterns, excluded_names)
    parameters: list[Any] = [start_ns, end_ns, *filter_parameters]
    rows = connection.execute(
        f"""
        WITH child_duration AS (
            SELECT parent_id, SUM(dur) AS direct_child_duration
            FROM callstack
            WHERE dur > 0 AND parent_id IS NOT NULL
            GROUP BY parent_id
        ), span_self AS (
            SELECT c.id, c.name, c.ts, c.dur,
                   COALESCE(p.name, 'unknown') AS process_name, p.pid,
                   COALESCE(t.name, 'unknown') AS thread_name, t.tid, t.itid,
                   CASE WHEN c.dur > COALESCE(cd.direct_child_duration, 0)
                        THEN c.dur - COALESCE(cd.direct_child_duration, 0)
                        ELSE 0 END AS self_ns
            FROM callstack c
            LEFT JOIN thread t ON t.itid = c.callid
            LEFT JOIN process p ON p.ipid = t.ipid
            LEFT JOIN child_duration cd ON cd.parent_id = c.id
            WHERE c.dur > 0 AND c.ts >= ? AND c.ts < ? AND {filters}
        ), group_metrics AS (
            SELECT name, process_name, pid, thread_name,
                   COUNT(*) AS occurrence_count,
                   SUM(dur) AS inclusive_total_ns,
                   SUM(self_ns) AS self_total_ns,
                   MAX(dur) AS max_duration_ns,
                   MAX(self_ns) AS max_self_ns
            FROM span_self
            GROUP BY name, process_name, pid, thread_name
            HAVING SUM(self_ns) > 0
        ), representatives AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY name, process_name, pid, thread_name
                ORDER BY self_ns DESC, dur DESC, ts ASC
            ) AS occurrence_rank
            FROM span_self
        )
        SELECT r.name, r.ts, r.dur, r.process_name, r.pid,
               r.thread_name, r.tid, r.itid,
               g.occurrence_count, g.inclusive_total_ns, g.self_total_ns,
               g.max_duration_ns, g.max_self_ns
        FROM group_metrics g
        JOIN representatives r
          ON r.name = g.name
         AND r.process_name = g.process_name
         AND r.pid = g.pid
         AND r.thread_name = g.thread_name
         AND r.occurrence_rank = 1
        ORDER BY g.self_total_ns DESC, g.max_self_ns DESC, g.inclusive_total_ns DESC
        LIMIT ?
        """,
        [*parameters, limit],
    ).fetchall()
    totals = connection.execute(
        f"""
        SELECT COUNT(*) AS event_count, COALESCE(SUM(c.dur), 0) AS inclusive_total_ns
        FROM callstack c
        LEFT JOIN thread t ON t.itid = c.callid
        LEFT JOIN process p ON p.ipid = t.ipid
        WHERE c.dur > 0 AND c.ts >= ? AND c.ts < ? AND {filters}
        """,
        parameters,
    ).fetchone()

    top_events: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        event = _event_record(row)
        event["metrics"] = {
            "rank": rank,
            "occurrence_count": int(row["occurrence_count"]),
            "self_time_estimate_total_duration_ms": round(int(row["self_total_ns"]) / 1_000_000.0, 6),
            "inclusive_total_duration_ms": round(int(row["inclusive_total_ns"]) / 1_000_000.0, 6),
            "max_self_time_estimate_duration_ms": round(int(row["max_self_ns"]) / 1_000_000.0, 6),
            "max_span_duration_ms": round(int(row["max_duration_ns"]) / 1_000_000.0, 6),
        }
        event["measurement_semantics"] = {
            "duration_kind": "representative_inclusive_trace_span",
            "ranking_kind": "parent_span_minus_direct_children_estimate",
            "cpu_self_time_proven": False,
        }
        top_events.append(event)
    return (
        int(totals["event_count"]),
        round(int(totals["inclusive_total_ns"]) / 1_000_000.0, 6),
        top_events,
    )


def build_report(
    database_path: Path,
    stages: Iterable[StageSpec],
    *,
    process_ids: Iterable[int] = (),
    exclude_patterns: Iterable[str] = (),
    top_event_limit: int = 30,
    scenario_id: str = "device_scenario",
    scenario_name: str = "真机操作场景",
    trace_path: Path | None = None,
    device: dict[str, str] | None = None,
) -> dict[str, Any]:
    normalized_pids = sorted({int(pid) for pid in process_ids if int(pid) > 0})
    normalized_excludes = [value for value in (str(item).strip() for item in exclude_patterns) if value]
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    report_stages: list[dict[str, Any]] = []
    try:
        for order, spec in enumerate(stages, start=1):
            start = _marker(connection, spec.start_marker)
            end = _marker(connection, spec.end_marker)
            start_ns = int(start["ts"])
            end_ns = int(end["ts"])
            if end_ns < start_ns:
                raise ValueError(f"阶段 {spec.stage_id!r} 的结束锚点早于开始锚点")
            event_count, total_event_duration_ms, top_events = _stage_events(
                connection,
                start_ns=start_ns,
                end_ns=end_ns,
                process_ids=normalized_pids,
                exclude_patterns=normalized_excludes,
                excluded_names=[spec.start_marker, spec.end_marker],
                limit=top_event_limit,
            )
            report_stages.append(
                {
                    "id": spec.stage_id,
                    "name": spec.name,
                    "order": order,
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "start_ms": round(start_ns / 1_000_000.0, 6),
                    "end_ms": round(end_ns / 1_000_000.0, 6),
                    "duration_ms": round((end_ns - start_ns) / 1_000_000.0, 6),
                    "event_count": event_count,
                    "total_event_duration_ms": total_event_duration_ms,
                    "start_event": _event_record(start),
                    "end_event": _event_record(end),
                    "top_events": top_events,
                }
            )
    finally:
        connection.close()

    summary: dict[str, Any] = {
        "total_stages": len(report_stages),
        "scenario_id": scenario_id,
        "scenario_name": scenario_name,
        "capture_kind": "real_device_hitrace",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_process_ids": normalized_pids,
        "top_event_limit_per_stage": top_event_limit,
        "ranking_method": "inclusive span minus direct child spans; grouped by event, process, and thread",
        "stage_semantics": "Injected interaction windows include UI stabilization waits and are not CPU self time.",
        "event_semantics": "Top events are representative spans ranked by an estimated self-time signal, not proven CPU samples.",
    }
    if normalized_excludes:
        summary["excluded_event_name_substrings"] = normalized_excludes
    if trace_path is not None:
        summary["trace_file"] = trace_path.name
        summary["trace_sha256"] = _sha256(trace_path)
    if device:
        summary["device"] = device
    return {"summary": summary, "stages": report_stages}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True, help="TraceStreamer SQLite 数据库")
    parser.add_argument("--output", type=Path, required=True, help="输出 stage analysis JSON")
    parser.add_argument("--stage", action="append", type=parse_stage_spec, required=True)
    parser.add_argument("--process-id", action="append", type=int, default=[])
    parser.add_argument("--exclude-pattern", action="append", default=[])
    parser.add_argument("--top-events", type=int, default=30)
    parser.add_argument("--scenario-id", default="device_scenario")
    parser.add_argument("--scenario-name", default="真机操作场景")
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--device-serial", default="")
    parser.add_argument("--device-model", default="")
    parser.add_argument("--device-os", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.top_events < 1 or args.top_events > 200:
        raise SystemExit("--top-events 必须位于 1 到 200")
    device = {
        key: value
        for key, value in {
            "serial": args.device_serial,
            "model": args.device_model,
            "os": args.device_os,
        }.items()
        if value
    }
    report = build_report(
        args.database,
        args.stage,
        process_ids=args.process_id,
        exclude_patterns=args.exclude_pattern,
        top_event_limit=args.top_events,
        scenario_id=args.scenario_id,
        scenario_name=args.scenario_name,
        trace_path=args.trace,
        device=device,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(report['stages'])} stages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
