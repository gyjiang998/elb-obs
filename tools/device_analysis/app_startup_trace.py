#!/usr/bin/env python3
"""Cold-start a set of OpenHarmony apps while collecting HiTrace and HiPerf.

The tool keeps capture, workload execution, conversion, and evidence extraction
in one reproducible run directory. It does not clear application data, install
packages, or interact with application content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


WEB_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = WEB_ROOT.parent.resolve()
if str(WEB_ROOT) not in sys.path:
    sys.path.insert(0, str(WEB_ROOT))

from backend.htrace_analysis import list_htrace_self_time_events  # noqa: E402
from backend.hitrace_instrumentation import (  # noqa: E402
    describe_hitrace_event,
    load_hitrace_tag_catalog,
)
from backend.trace_modules import classify_trace_event  # noqa: E402


DEFAULT_HDC_CANDIDATES = (
    WORKSPACE_ROOT / "tools/hdc_install/hwsdk/openharmony/9/toolchains/hdc",
    WEB_ROOT / "heterogeneous-enable-web-runtime/toolchains/hdc",
)

# Deliberately curated launcher-facing applications. The runner verifies that a
# bundle is installed and that implicit launcher resolution succeeds before it
# counts the bundle toward the requested workload size.
APP_CANDIDATES = (
    "com.huawei.hmos.calculator",
    "com.huawei.hmos.clock",
    "com.huawei.hmos.settings",
    "com.huawei.hmos.tips",
    "com.huawei.hmos.remotecontroller",
    "com.huawei.hmos.soundrecorder",
    "com.huawei.hmos.videoplayer",
    "com.huawei.hmos.browser",
    "com.huawei.hmos.filemanager",
    "com.huawei.hmos.calendar",
    "com.huawei.hmos.notepad",
    "com.huawei.hmos.photos",
    "com.huawei.hmos.maps.app",
    "com.huawei.hmos.health",
    "com.huawei.hmos.myhuawei",
    "com.huawei.hmos.vmall",
    "com.huawei.hmsapp.compass",
    "com.huawei.hmsapp.appgallery",
    "com.huawei.hmsapp.books",
    "com.huawei.hmsapp.himovie",
    "com.huawei.hmsapp.music",
    "com.huawei.hmsapp.thememanager",
    "com.huawei.hmsapp.totemweather",
    "com.ohos.contacts",
    "com.ohos.mms",
    "com.huawei.hmos.email",
    "com.huawei.hmos.betaclub",
    "com.huawei.hmos.armeasure",
    "com.huawei.hmos.files",
    "com.huawei.hmos.camera",
)

ARKUI_STARTUP_TRACE_CATEGORIES = (
    "ace",
    "ark",
    "app",
    "graphic",
    "window",
    "ability",
    "binder",
    "rpc",
    "ffrt",
)
BROAD_TRACE_CATEGORIES = (
    *ARKUI_STARTUP_TRACE_CATEGORIES,
    "sched",
    "freq",
    "idle",
)
TRACE_PROFILES = {
    "arkui-startup": ARKUI_STARTUP_TRACE_CATEGORIES,
    "broad": BROAD_TRACE_CATEGORIES,
}

BUNDLE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TARGET_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
PID_LINE_RE = re.compile(r"^HETERO_PID=(.*)$", re.MULTILINE)
LAUNCH_MARKER_RE = re.compile(r"H:HETERO_APP_LAUNCH::(\d+)::([^|]+)\|I1")
HIPERF_ROW_RE = re.compile(
    r"^\s*(?P<percent>\d+(?:\.\d+)?)%\s+"
    r"(?P<count>\d+)\s+(?P<comm>\S+)\s+(?P<pid>\d+)\s+"
    r"(?P<dso>\S+)\s+(?P<function>.*?\S)\s*$"
)
ARKUI_SYMBOL_TERMS = (
    "arkui",
    "libace",
    "ace_engine",
    "ace.z.so",
    "libark",
    "ark_js",
    "arkcompiler",
)


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    elapsed_seconds: float

    @property
    def combined_output(self) -> str:
        return clean_hdc_output("\n".join(part for part in (self.stdout, self.stderr) if part))


def clean_hdc_output(text: str) -> str:
    lines = []
    for raw_line in ANSI_RE.sub("", text).splitlines():
        if "FreeChannelContinue handle->data is nullptr" in raw_line:
            continue
        lines.append(raw_line.rstrip())
    return "\n".join(lines).strip()


def parse_hdc_targets(text: str) -> list[str]:
    targets = []
    for line in clean_hdc_output(text).splitlines():
        value = line.strip()
        if value and TARGET_RE.fullmatch(value) and value.lower() not in {"empty", "unknown"}:
            targets.append(value)
    return list(dict.fromkeys(targets))


def parse_installed_bundles(text: str) -> set[str]:
    bundles = set()
    for line in clean_hdc_output(text).splitlines():
        value = line.strip()
        if BUNDLE_RE.fullmatch(value):
            bundles.add(value)
    return bundles


def parse_launch_result(output: str, return_code: int) -> tuple[bool, list[int], str]:
    clean = clean_hdc_output(output)
    lowered = clean.casefold()
    matched = PID_LINE_RE.search(clean)
    pids = sorted(
        {
            int(value)
            for value in re.findall(r"\d+", matched.group(1) if matched else "")
            if int(value) > 0
        }
    )
    success = (
        return_code == 0
        and "start ability successfully" in lowered
        and "failed to start" not in lowered
        and "error:" not in lowered
    )
    reason = "launcher_resolved" if success else (clean[-500:] or f"return_code={return_code}")
    return success, pids, reason


def parse_hiperf_report(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        matched = HIPERF_ROW_RE.match(line)
        if not matched:
            continue
        rows.append(
            {
                "heat_percent": float(matched.group("percent")),
                "event_count": int(matched.group("count")),
                "comm": matched.group("comm"),
                "pid": int(matched.group("pid")),
                "dso": matched.group("dso"),
                "function": matched.group("function"),
            }
        )
    return rows


def validate_bundle(bundle: str) -> str:
    if not BUNDLE_RE.fullmatch(bundle):
        raise ValueError(f"Unsafe bundle name: {bundle!r}")
    return bundle


def build_launch_shell(
    bundle: str,
    *,
    attempt_index: int,
    dwell_seconds: float,
    marker_path: str | None,
) -> str:
    bundle = validate_bundle(bundle)
    dwell = min(max(float(dwell_seconds), 0.2), 30.0)
    lines = [
        f"aa force-stop {shlex.quote(bundle)} >/dev/null 2>&1 || true",
        "sleep 0.2",
    ]
    if marker_path:
        lines.extend(
            [
                f"exec 3>{shlex.quote(marker_path)}",
                f'echo "B|$$|H:HETERO_APP_LAUNCH::{attempt_index:02d}::{bundle}|I1" >&3',
            ]
        )
    lines.extend(
        [
            f"aa start -b {shlex.quote(bundle)} -A action.system.home",
            "START_RC=$?",
            f"sleep {dwell:.3f}",
            f'echo "HETERO_PID=$(pidof {shlex.quote(bundle)} 2>/dev/null || true)"',
        ]
    )
    if marker_path:
        lines.extend(['echo "E|$$|I1" >&3', "exec 3>&-"])
    lines.append("exit $START_RC")
    return "; ".join(lines)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_process(command: Iterable[str], *, timeout: float, check: bool = False) -> CommandResult:
    argv = tuple(str(value) for value in command)
    started = time.monotonic()
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )
    result = CommandResult(
        command=argv,
        return_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    if check and result.return_code != 0:
        raise RuntimeError(f"Command failed ({result.return_code}): {' '.join(argv)}\n{result.combined_output}")
    return result


class HdcClient:
    def __init__(self, binary: Path, serial: str) -> None:
        self.binary = binary.resolve()
        self.serial = serial

    def run(self, args: Iterable[str], *, timeout: float = 30, check: bool = False) -> CommandResult:
        return run_process(
            (str(self.binary), "-t", self.serial, *tuple(args)),
            timeout=timeout,
            check=check,
        )

    def shell(self, command: str, *, timeout: float = 30, check: bool = False) -> CommandResult:
        return self.run(("shell", command), timeout=timeout, check=check)

    def receive(self, remote_path: str, local_path: Path, *, timeout: float = 300) -> CommandResult:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        return self.run(("file", "recv", remote_path, str(local_path)), timeout=timeout, check=True)


def resolve_hdc(explicit: str | None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if configured := os.environ.get("HETERO_HDC_BIN"):
        candidates.append(Path(configured).expanduser())
    candidates.extend(DEFAULT_HDC_CANDIDATES)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise RuntimeError("No executable HDC found. Pass --hdc or set HETERO_HDC_BIN.")


def resolve_device(hdc: Path, explicit: str | None) -> str:
    result = run_process((str(hdc), "list", "targets"), timeout=15, check=True)
    targets = parse_hdc_targets(result.combined_output)
    if explicit:
        if explicit not in targets:
            raise RuntimeError(f"Requested device {explicit!r} is not connected; available: {targets}")
        return explicit
    if len(targets) != 1:
        raise RuntimeError(f"Expected exactly one connected device; available: {targets}")
    return targets[0]


def resolve_trace_streamer(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if configured := os.environ.get("HETERO_TRACE_STREAMER"):
        candidates.append(Path(configured).expanduser())
    discovered = list(
        WEB_ROOT.glob(
            ".local-artifacts/tools/trace_streamer_*/trace_streamer_binary/trace_streamer_*"
        )
    )
    system = platform.system().casefold()
    preferred_suffix = {"darwin": "_mac", "linux": "_linux", "windows": ".exe"}.get(system, "")
    discovered.sort(key=lambda path: (not path.name.casefold().endswith(preferred_suffix), path.name))
    candidates.extend(discovered)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise RuntimeError("TraceStreamer was not found. Pass --trace-streamer or set HETERO_TRACE_STREAMER.")


def discover_trace_marker(client: HdcClient) -> str | None:
    command = (
        "for p in /sys/kernel/tracing/trace_marker /sys/kernel/debug/tracing/trace_marker; "
        'do if [ -w "$p" ]; then echo "$p"; break; fi; done'
    )
    result = client.shell(command, timeout=10)
    for line in result.combined_output.splitlines():
        value = line.strip()
        if value in {"/sys/kernel/tracing/trace_marker", "/sys/kernel/debug/tracing/trace_marker"}:
            return value
    return None


def extract_launch_windows(database_path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT id, ts, dur, name, callid
            FROM callstack
            WHERE name LIKE 'H:HETERO_APP_LAUNCH::%'
              AND dur > 0
            ORDER BY ts, id
            """
        ).fetchall()
    finally:
        connection.close()
    windows = []
    for row in rows:
        matched = LAUNCH_MARKER_RE.search(str(row["name"]))
        if not matched:
            continue
        windows.append(
            {
                "attempt_index": int(matched.group(1)),
                "bundle": matched.group(2),
                "start_ns": int(row["ts"]),
                "end_ns": int(row["ts"]) + int(row["dur"]),
                "duration_ms": round(int(row["dur"]) / 1_000_000.0, 6),
                "callstack_id": int(row["id"]),
                "callid": int(row["callid"]),
            }
        )
    return windows


def _database_schema(connection: sqlite3.Connection) -> dict[str, set[str]]:
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    return {
        table: {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()}
        for table in tables
    }


def rank_arkui_events_in_window(
    database_path: Path,
    window: dict[str, Any],
    *,
    tag_catalog: dict[str, Any],
    process_ids: set[int] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        schema = _database_schema(connection)
        if not {"id", "ts", "dur", "name", "parent_id"}.issubset(schema.get("callstack", set())):
            return []
        callstack_columns = schema["callstack"]
        thread_columns = schema.get("thread", set())
        process_columns = schema.get("process", set())
        has_callid = "callid" in callstack_columns
        has_thread = has_callid and {"itid", "name"}.issubset(thread_columns)
        has_process = has_thread and "ipid" in thread_columns and {"ipid", "name"}.issubset(process_columns)
        callid = "COALESCE(c.callid, -1)" if has_callid else "-1"
        thread_join = "LEFT JOIN thread t ON c.callid = t.itid" if has_thread else ""
        process_join = "LEFT JOIN process p ON t.ipid = p.ipid" if has_process else ""
        thread_name = "COALESCE(t.name, 'unknown')" if has_thread else "'unknown'"
        process_name = "COALESCE(p.name, 'unknown')" if has_process else "'unknown'"
        process_id = "COALESCE(p.pid, -1)" if has_process and "pid" in process_columns else "-1"
        normalized_pids = sorted({int(pid) for pid in (process_ids or set()) if int(pid) > 0})
        pid_filter = ""
        parameters: list[int] = [int(window["start_ns"]), int(window["end_ns"])]
        if normalized_pids and has_process and "pid" in process_columns:
            pid_filter = f" AND p.pid IN ({','.join('?' for _ in normalized_pids)})"
            parameters.extend(normalized_pids)
        rows = connection.execute(
            f"""
            WITH child_duration AS (
                SELECT parent_id, SUM(dur) AS total_direct_child_duration
                FROM callstack
                WHERE dur > 0 AND parent_id IS NOT NULL
                GROUP BY parent_id
            ), span_self AS (
                SELECT c.name,
                       {callid} AS callid,
                       {process_name} AS process_name,
                       {process_id} AS process_id,
                       {thread_name} AS thread_name,
                       c.dur,
                       CASE WHEN c.dur > COALESCE(child_duration.total_direct_child_duration, 0)
                            THEN c.dur - COALESCE(child_duration.total_direct_child_duration, 0)
                            ELSE 0 END AS self_ns
                FROM callstack c
                {thread_join}
                {process_join}
                LEFT JOIN child_duration ON c.id = child_duration.parent_id
                WHERE c.dur > 0 AND c.ts >= ? AND c.ts < ?
                  AND TRIM(COALESCE(c.name, '')) != ''
                  {pid_filter}
            )
            SELECT name, callid, process_name, process_id, thread_name,
                   COUNT(*) AS occurrence_count,
                   SUM(self_ns) AS self_total_ns,
                   SUM(dur) AS inclusive_total_ns,
                   MAX(self_ns) AS max_self_ns,
                   MAX(dur) AS max_duration_ns
            FROM span_self
            GROUP BY name, callid, process_name, process_id, thread_name
            HAVING SUM(self_ns) > 0
            ORDER BY self_total_ns DESC, max_self_ns DESC
            """,
            parameters,
        ).fetchall()
    finally:
        connection.close()

    events = []
    for row in rows:
        event_name = str(row["name"])
        marker = describe_hitrace_event(event_name, tag_catalog)
        module = classify_trace_event(
            event_name,
            process_name=row["process_name"],
            thread_name=row["thread_name"],
            hitrace_marker=marker,
        )
        if module["module_key"] != "arkui_ui":
            continue
        events.append(
            {
                "event_name": event_name,
                "process_name": str(row["process_name"]),
                "process_id": int(row["process_id"]),
                "thread_name": str(row["thread_name"]),
                "callid": int(row["callid"]),
                "occurrence_count": int(row["occurrence_count"]),
                "self_time_estimate_total_duration_ms": round(int(row["self_total_ns"]) / 1_000_000.0, 6),
                "inclusive_total_duration_ms": round(int(row["inclusive_total_ns"]) / 1_000_000.0, 6),
                "max_self_time_estimate_duration_ms": round(int(row["max_self_ns"]) / 1_000_000.0, 6),
                "max_span_duration_ms": round(int(row["max_duration_ns"]) / 1_000_000.0, 6),
                "trace_module": module,
                "hitrace_marker": marker,
            }
        )
        if len(events) >= limit:
            break
    return events


def _table_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _event_metrics(event: dict[str, Any]) -> dict[str, Any]:
    return event.get("metrics", event)


def render_markdown_report(summary: dict[str, Any]) -> str:
    device = summary["device"]
    lines = [
        "# 20 应用连续启动 HiTrace / HiPerf 分析",
        "",
        "## 实验概况",
        "",
        f"- 设备：`{_table_cell(device.get('model', 'unknown'))}`，序列号 `{_table_cell(device['serial'])}`",
        f"- 系统：`{_table_cell(device.get('software_version', 'unknown'))}`",
        f"- 成功启动：**{summary['workload']['successful_launches']} / {summary['workload']['requested_launches']}**",
        f"- 应用驻留时间：`{summary['workload']['dwell_seconds']:.3f}s`",
        f"- HiTrace 类别：`{' / '.join(summary['capture']['trace_categories'])}`",
        f"- HiPerf：系统级 CPU cycle 采样，`{summary['capture']['hiperf_frequency_hz']} Hz`，调用栈为 frame pointer 模式",
        f"- 成功启动窗口保留率：**{summary['hitrace']['launch_window_retention']['retained_successful_windows']} / {summary['hitrace']['launch_window_retention']['expected_successful_windows']}**",
        "",
        "## 应用启动结果",
        "",
        "| 序号 | Bundle | 采集命令总耗时(ms，含驻留) | PID | 状态 |",
        "| ---: | --- | ---: | --- | --- |",
    ]
    for item in summary["launches"]:
        lines.append(
            "| {index} | `{bundle}` | {elapsed:.1f} | `{pids}` | {status} |".format(
                index=item["success_index"] or item["attempt_index"],
                bundle=_table_cell(item["bundle"]),
                elapsed=float(item["command_elapsed_ms"]),
                pids=",".join(str(pid) for pid in item["pids"]) or "unknown",
                status="成功" if item["success"] else "跳过",
            )
        )

    lines.extend(
        [
            "",
            "## HiTrace 模块概览",
            "",
            "| 模块 | 事件组 | 次数 | self-time 估算(ms) | inclusive(ms) |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for group in summary["hitrace"]["module_groups"][:15]:
        lines.append(
            f"| {_table_cell(group['module_name'])} | {group['event_group_count']} | "
            f"{group['occurrence_count']} | {group['self_time_estimate_total_duration_ms']:.3f} | "
            f"{group['inclusive_total_duration_ms']:.3f} |"
        )

    lines.extend(
        [
            "",
            "## ArkUI HiTrace 热点",
            "",
            "| 排名 | 事件 | 进程 / 线程 | 次数 | self-time 估算(ms) | inclusive(ms) | 最大单次(ms) |",
            "| ---: | --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for rank, event in enumerate(summary["hitrace"]["arkui_hotspots"][:25], start=1):
        metrics = _event_metrics(event)
        context = event.get("trace_context", {})
        lines.append(
            f"| {rank} | `{_table_cell(event['event_name'])}` | "
            f"`{_table_cell(context.get('process_name', 'unknown'))}` / "
            f"`{_table_cell(context.get('thread_name', 'unknown'))}` | "
            f"{metrics['occurrence_count']} | {metrics['self_time_estimate_total_duration_ms']:.3f} | "
            f"{metrics['inclusive_total_duration_ms']:.3f} | {metrics['max_span_duration_ms']:.3f} |"
        )
    if not summary["hitrace"]["arkui_hotspots"]:
        lines.append("| - | 未发现可归类的 ArkUI span | - | - | - | - | - |")

    lines.extend(["", "## 各应用启动窗口中的 ArkUI Top 事件", ""])
    for window in summary["hitrace"]["launch_windows"]:
        lines.append(f"### {window['attempt_index']:02d}. `{_table_cell(window['bundle'])}`")
        lines.append("")
        lines.append(f"窗口 `{window['duration_ms']:.3f} ms`。")
        lines.append("")
        if not window["arkui_hotspots"]:
            lines.append("该窗口未发现具有正 self-time 估算的 ArkUI span。")
            lines.append("")
            continue
        lines.extend(
            [
                "| 事件 | 进程 | 次数 | self-time 估算(ms) | inclusive(ms) |",
                "| --- | --- | ---: | ---: | ---: |",
            ]
        )
        for event in window["arkui_hotspots"]:
            lines.append(
                f"| `{_table_cell(event['event_name'])}` | `{_table_cell(event['process_name'])}` | "
                f"{event['occurrence_count']} | {event['self_time_estimate_total_duration_ms']:.3f} | "
                f"{event['inclusive_total_duration_ms']:.3f} |"
            )
        lines.append("")

    lines.extend(
        [
            "## HiPerf 应用进程热度",
            "",
            "下表为 PID 过滤报告中超过阈值的符号热度之和，是进程热度下界，不是完整 CPU 占比。",
            "",
            "| 排名 | Bundle | PID | 已报告热度之和 | 符号数 |",
            "| ---: | --- | --- | ---: | ---: |",
        ]
    )
    for rank, row in enumerate(summary["hiperf"]["launched_app_process_heat"], start=1):
        lines.append(
            f"| {rank} | `{_table_cell(row['bundle'])}` | `{','.join(str(pid) for pid in row['pids'])}` | "
            f"{row['reported_heat_percent_sum']:.3f}% | {row['reported_symbol_count']} |"
        )

    lines.extend(
        [
            "",
            "## HiPerf CPU 热点（已启动应用 PID）",
            "",
            "| 排名 | 热度 | 进程 | PID | DSO | 函数 |",
            "| ---: | ---: | --- | ---: | --- | --- |",
        ]
    )
    for rank, row in enumerate(summary["hiperf"]["launched_app_hotspots"][:30], start=1):
        lines.append(
            f"| {rank} | {row['heat_percent']:.2f}% | `{_table_cell(row['comm'])}` | {row['pid']} | "
            f"`{_table_cell(row['dso'])}` | `{_table_cell(row['function'])}` |"
        )
    if not summary["hiperf"]["launched_app_hotspots"]:
        lines.append("| - | - | - | - | - | 未解析到已启动应用 PID 的 flat report 行 |")

    lines.extend(
        [
            "",
            "## 解释边界",
            "",
            "- HiTrace self-time 是父 span 时长减去直接子 span 时长的估算，不等同于采样 CPU self time。",
            "- HiPerf 是系统级采样；应用 PID 过滤可减少系统噪声，但服务端、RenderService、驱动等跨进程成本仍需结合 HiTrace 判断。",
            "- 每个应用窗口由同一设备 shell 写入的 B/E marker 定义；窗口可用于时序归因，但不是跨线程调用树。",
            "- 本实验识别热点和候选调查点，不直接证明关键路径、优化收益或异构卸载可行性。",
            "",
        ]
    )
    return "\n".join(lines)


def collect_device_metadata(client: HdcClient) -> dict[str, str]:
    commands = {
        "model": "param get const.product.model",
        "software_version": "param get const.product.software.version",
        "kernel": "uname -a",
    }
    metadata = {"serial": client.serial}
    for key, command in commands.items():
        result = client.shell(command, timeout=10)
        metadata[key] = result.combined_output.splitlines()[0].strip() if result.combined_output else "unknown"
    return metadata


def analyze_local_run(
    run_dir: Path,
    args: argparse.Namespace,
    *,
    device: dict[str, str],
    launches: list[dict[str, Any]],
    marker_path: str | None,
    trace_streamer: Path,
    command_log: list[dict[str, Any]],
    reuse_database: bool = False,
) -> dict[str, Any]:
    run_id = run_dir.name
    database_path = run_dir / "trace_streamer.db"
    if reuse_database and database_path.is_file():
        print("[6/7] 复用现有 TraceStreamer 数据库", flush=True)
    else:
        database_path.unlink(missing_ok=True)
        print("[6/7] TraceStreamer 转换中", flush=True)
        conversion = run_process(
            (str(trace_streamer), str(run_dir / "startup.ftrace"), "-e", str(database_path)),
            timeout=600,
            check=True,
        )
        command_log.append(
            {
                "label": "trace-streamer-convert",
                "command": list(conversion.command),
                "return_code": conversion.return_code,
                "elapsed_seconds": conversion.elapsed_seconds,
                "output": conversion.combined_output[-4000:],
            }
        )

    print("[7/7] 计算模块与 ArkUI self-time 热点", flush=True)
    source_root = Path(args.source_root).expanduser().resolve()
    tag_catalog = load_hitrace_tag_catalog(source_root, WORKSPACE_ROOT)
    state_root = run_dir.parent
    raw_htrace = {
        "conversion": {
            "database": {
                "managed_path": str(database_path.relative_to(state_root)),
                "sha256": sha256_file(database_path),
            }
        }
    }
    grouped = list_htrace_self_time_events(
        raw_htrace,
        state_root=state_root,
        limit=100,
        tag_catalog=tag_catalog,
        group_by_module=True,
        per_module_limit=10,
    )
    arkui = list_htrace_self_time_events(
        raw_htrace,
        state_root=state_root,
        limit=100,
        module_key="arkui_ui",
        tag_catalog=tag_catalog,
    )
    marker_windows = extract_launch_windows(database_path)
    successful_window_keys = {
        (int(launch["attempt_index"]), str(launch["bundle"]))
        for launch in launches
        if launch["success"]
    }
    windows = [
        window
        for window in marker_windows
        if (int(window["attempt_index"]), str(window["bundle"])) in successful_window_keys
    ]
    successful_launch_by_key = {
        (int(launch["attempt_index"]), str(launch["bundle"])): launch
        for launch in launches
        if launch["success"]
    }
    for window in windows:
        launch = successful_launch_by_key[(int(window["attempt_index"]), str(window["bundle"]))]
        window["pids"] = [int(pid) for pid in launch["pids"]]
        window["arkui_hotspots"] = rank_arkui_events_in_window(
            database_path,
            window,
            tag_catalog=tag_catalog,
            process_ids=set(window["pids"]),
            limit=args.per_app_arkui_limit,
        )

    launched_pids = sorted({pid for launch in launches if launch["success"] for pid in launch["pids"]})
    flat_rows = parse_hiperf_report((run_dir / "perf_report.txt").read_text(encoding="utf-8", errors="replace"))
    app_report_path = run_dir / "perf_apps_report.txt"
    app_rows = (
        parse_hiperf_report(app_report_path.read_text(encoding="utf-8", errors="replace"))
        if app_report_path.is_file()
        else [row for row in flat_rows if row["pid"] in launched_pids]
    )
    process_heat = []
    for launch in launches:
        if not launch["success"]:
            continue
        pids = {int(pid) for pid in launch["pids"]}
        rows = [row for row in app_rows if row["pid"] in pids]
        process_heat.append(
            {
                "bundle": launch["bundle"],
                "pids": sorted(pids),
                "reported_heat_percent_sum": round(sum(float(row["heat_percent"]) for row in rows), 6),
                "reported_event_count_sum": sum(int(row["event_count"]) for row in rows),
                "reported_symbol_count": len(rows),
                "report_limit_percent": 0.005,
                "interpretation": "lower_bound_after_report_threshold",
            }
        )
    process_heat.sort(key=lambda item: (-float(item["reported_heat_percent_sum"]), str(item["bundle"])))
    arkui_cpu_rows = [
        row
        for row in flat_rows
        if any(
            term in f"{row['comm']} {row['dso']} {row['function']}".casefold()
            for term in ARKUI_SYMBOL_TERMS
        )
    ]

    successful_launches = sum(1 for launch in launches if launch["success"])
    capture_config_path = run_dir / "capture_config.json"
    capture_config = (
        json.loads(capture_config_path.read_text(encoding="utf-8"))
        if capture_config_path.is_file()
        else {}
    )
    trace_categories = capture_config.get("trace_categories", list(TRACE_PROFILES[args.trace_profile]))
    summary = {
        "schema_version": "device_app_startup_analysis/v1",
        "run_id": run_id,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "device": device,
        "workload": {
            "requested_launches": int(args.app_count),
            "successful_launches": successful_launches,
            "attempted_launches": len(launches),
            "dwell_seconds": float(args.dwell_seconds),
            "cold_start_method": "aa_force_stop_then_implicit_home_action",
            "application_data_cleared": False,
        },
        "capture": {
            "trace_profile": capture_config.get("trace_profile", args.trace_profile),
            "trace_categories": trace_categories,
            "trace_buffer_kb": int(args.trace_buffer_kb),
            "trace_marker_path": marker_path,
            "hiperf_frequency_hz": int(args.hiperf_frequency),
            "hiperf_cpu_limit_percent": int(args.hiperf_cpu_limit),
            "hiperf_callstack_mode": "fp",
            "trace_streamer": str(trace_streamer),
        },
        "launches": launches,
        "hitrace": {
            "tag_catalog_provenance": tag_catalog.get("provenance"),
            "quality": arkui["quality"],
            "launch_window_retention": {
                "expected_successful_windows": successful_launches,
                "retained_successful_windows": len(windows),
                "retained_all_markers": len(marker_windows),
                "complete": len(windows) == successful_launches,
            },
            "module_groups": grouped["module_catalog"]["groups"],
            "arkui_hotspots": arkui["events"],
            "launch_windows": windows,
        },
        "hiperf": {
            "launched_pids": launched_pids,
            "launched_app_process_heat": process_heat,
            "global_hotspots": flat_rows[:50],
            "launched_app_hotspots": app_rows[:100],
            "arkui_symbol_hotspots": arkui_cpu_rows[:100],
        },
        "artifacts": {
            path.name: {"size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
            for path in (
                run_dir / "startup.ftrace",
                run_dir / "perf.data",
                run_dir / "perf_report.txt",
                run_dir / "perf_callstack.txt",
                database_path,
            )
            if path.is_file()
        },
    }
    write_json(run_dir / "summary.json", summary)
    write_json(run_dir / "commands.json", command_log)
    (run_dir / "report.md").write_text(render_markdown_report(summary), encoding="utf-8")
    return summary


def run_capture(args: argparse.Namespace) -> Path:
    hdc = resolve_hdc(args.hdc)
    serial = resolve_device(hdc, args.serial)
    trace_streamer = resolve_trace_streamer(args.trace_streamer)
    client = HdcClient(hdc, serial)
    run_id = datetime.now().strftime("app_startup_%Y%m%d_%H%M%S")
    output_root = Path(args.output_root).expanduser().resolve()
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    trace_categories = TRACE_PROFILES[args.trace_profile]
    write_json(
        run_dir / "capture_config.json",
        {
            "trace_profile": args.trace_profile,
            "trace_categories": list(trace_categories),
            "trace_buffer_kb": int(args.trace_buffer_kb),
            "hiperf_frequency_hz": int(args.hiperf_frequency),
            "hiperf_cpu_limit_percent": int(args.hiperf_cpu_limit),
            "dwell_seconds": float(args.dwell_seconds),
            "requested_launches": int(args.app_count),
        },
    )
    remote_root = f"/data/local/tmp/hetero_{run_id}"
    remote_trace = f"{remote_root}/startup.ftrace"
    remote_perf = f"{remote_root}/perf.data"
    remote_flat_report = f"{remote_root}/perf_report.txt"
    remote_stack_report = f"{remote_root}/perf_callstack.txt"
    remote_apps_report = f"{remote_root}/perf_apps_report.txt"
    command_log: list[dict[str, Any]] = []

    def record(label: str, result: CommandResult) -> CommandResult:
        command_log.append(
            {
                "label": label,
                "command": list(result.command),
                "return_code": result.return_code,
                "elapsed_seconds": result.elapsed_seconds,
                "output": result.combined_output[-4000:],
            }
        )
        return result

    print(f"[1/7] 设备 {serial}，运行目录 {run_dir}", flush=True)
    device = collect_device_metadata(client)
    marker_path = discover_trace_marker(client)
    installed_result = record("list-installed-bundles", client.shell("bm dump -a", timeout=30, check=True))
    installed = parse_installed_bundles(installed_result.combined_output)
    candidates = [bundle for bundle in APP_CANDIDATES if bundle in installed]
    if len(candidates) < args.app_count:
        raise RuntimeError(
            f"Only {len(candidates)} curated launcher bundles are installed; {args.app_count} are required."
        )

    print(f"[2/7] 已确认 {len(candidates)} 个候选应用，trace marker: {marker_path or 'unavailable'}", flush=True)
    record("create-remote-root", client.shell(f"mkdir -p {shlex.quote(remote_root)}", check=True))
    record("cleanup-old-hiperf", client.shell("hiperf record --control stop >/dev/null 2>&1 || true"))
    record("cleanup-old-hitrace", client.shell("hitrace --trace_finish_nodump >/dev/null 2>&1 || true"))

    trace_started = False
    perf_prepared = False
    perf_started = False
    launches: list[dict[str, Any]] = []
    try:
        categories = " ".join(trace_categories)
        trace_begin = record(
            "hitrace-begin",
            client.shell(
                f"hitrace --trace_begin -b {int(args.trace_buffer_kb)} {categories}",
                timeout=30,
                check=True,
            ),
        )
        trace_started = trace_begin.return_code == 0
        perf_prepare = record(
            "hiperf-prepare",
            client.shell(
                "hiperf record --control prepare -a --exclude-hiperf "
                f"-f {int(args.hiperf_frequency)} --cpu-limit {int(args.hiperf_cpu_limit)} "
                f"-s fp -o {shlex.quote(remote_perf)}",
                timeout=30,
                check=True,
            ),
        )
        perf_prepared = perf_prepare.return_code == 0
        perf_start = record(
            "hiperf-start",
            client.shell("hiperf record --control start", timeout=30, check=True),
        )
        perf_started = perf_start.return_code == 0

        print("[3/7] HiTrace 与 HiPerf 已启动，开始连续冷启动应用", flush=True)
        success_count = 0
        for attempt_index, bundle in enumerate(candidates, start=1):
            if success_count >= args.app_count:
                break
            shell = build_launch_shell(
                bundle,
                attempt_index=attempt_index,
                dwell_seconds=args.dwell_seconds,
                marker_path=marker_path,
            )
            started = time.monotonic()
            result = record(
                f"launch-{attempt_index:02d}-{bundle}",
                client.shell(shell, timeout=max(20.0, args.dwell_seconds + 15.0)),
            )
            success, pids, reason = parse_launch_result(result.combined_output, result.return_code)
            if success:
                success_count += 1
            launches.append(
                {
                    "attempt_index": attempt_index,
                    "success_index": success_count if success else None,
                    "bundle": bundle,
                    "success": success,
                    "pids": pids,
                    "command_elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                    "reason": reason,
                }
            )
            print(
                f"      {success_count:02d}/{args.app_count:02d} "
                f"{'OK' if success else 'SKIP'} {bundle} pid={','.join(map(str, pids)) or '-'}",
                flush=True,
            )
        if success_count < args.app_count:
            raise RuntimeError(f"Only {success_count} applications launched successfully; expected {args.app_count}.")
    finally:
        if perf_started or perf_prepared:
            record("hiperf-stop", client.shell("hiperf record --control stop", timeout=60))
        if trace_started:
            record(
                "hitrace-finish",
                client.shell(
                    f"hitrace --trace_finish -o {shlex.quote(remote_trace)}",
                    timeout=180,
                ),
            )
        write_json(run_dir / "commands.json", command_log)
        write_json(run_dir / "launches.json", launches)

    print("[4/7] 采集完成，正在设备侧生成 HiPerf 报告", flush=True)
    report_commands = (
        (
            "hiperf-flat-report",
            f"hiperf report -i {shlex.quote(remote_perf)} --limit-percent 0.02 "
            f"--sort comm,pid,dso,func -o {shlex.quote(remote_flat_report)}",
        ),
        (
            "hiperf-callstack-report",
            f"hiperf report -i {shlex.quote(remote_perf)} -s --limit-percent 0.10 "
            f"--call-stack-limit-percent 0.50 --sort comm,pid,dso,func "
            f"-o {shlex.quote(remote_stack_report)}",
        ),
    )
    for label, command in report_commands:
        record(label, client.shell(command, timeout=240, check=True))

    launched_pids = sorted({pid for launch in launches if launch["success"] for pid in launch["pids"]})
    if launched_pids:
        pid_csv = ",".join(str(pid) for pid in launched_pids)
        record(
            "hiperf-app-report",
            client.shell(
                f"hiperf report -i {shlex.quote(remote_perf)} --pids {pid_csv} "
                f"--limit-percent 0.005 --sort comm,pid,dso,func -o {shlex.quote(remote_apps_report)}",
                timeout=240,
                check=True,
            ),
        )
    write_json(run_dir / "commands.json", command_log)

    print("[5/7] 正在拉取 trace、perf.data 与文本报告", flush=True)
    pull_items = [
        (remote_trace, run_dir / "startup.ftrace"),
        (remote_perf, run_dir / "perf.data"),
        (remote_flat_report, run_dir / "perf_report.txt"),
        (remote_stack_report, run_dir / "perf_callstack.txt"),
    ]
    if launched_pids:
        pull_items.append((remote_apps_report, run_dir / "perf_apps_report.txt"))
    for remote_path, local_path in pull_items:
        record(f"pull-{local_path.name}", client.receive(remote_path, local_path))
    write_json(run_dir / "commands.json", command_log)

    analyze_local_run(
        run_dir,
        args,
        device=device,
        launches=launches,
        marker_path=marker_path,
        trace_streamer=trace_streamer,
        command_log=command_log,
    )

    if not args.keep_remote:
        record("cleanup-run-remote-root", client.shell(f"rm -rf {shlex.quote(remote_root)}", timeout=60))
        write_json(run_dir / "commands.json", command_log)

    print(f"完成：{run_dir / 'report.md'}", flush=True)
    return run_dir


def resume_run(args: argparse.Namespace) -> Path:
    run_dir = Path(args.resume_run).expanduser().resolve()
    required = ("startup.ftrace", "perf.data", "perf_report.txt", "perf_callstack.txt", "launches.json")
    missing = [name for name in required if not (run_dir / name).is_file()]
    if missing:
        raise RuntimeError(f"Cannot resume {run_dir}; missing local artifacts: {missing}")

    hdc = resolve_hdc(args.hdc)
    serial = resolve_device(hdc, args.serial)
    client = HdcClient(hdc, serial)
    trace_streamer = resolve_trace_streamer(args.trace_streamer)
    device = collect_device_metadata(client)
    marker_path = discover_trace_marker(client)
    launches_value = json.loads((run_dir / "launches.json").read_text(encoding="utf-8"))
    if not isinstance(launches_value, list):
        raise RuntimeError("launches.json must contain a list")
    launches = [item for item in launches_value if isinstance(item, dict)]
    command_path = run_dir / "commands.json"
    command_value = json.loads(command_path.read_text(encoding="utf-8")) if command_path.is_file() else []
    command_log = [item for item in command_value if isinstance(item, dict)] if isinstance(command_value, list) else []

    print(f"恢复本地采集：{run_dir}", flush=True)
    analyze_local_run(
        run_dir,
        args,
        device=device,
        launches=launches,
        marker_path=marker_path,
        trace_streamer=trace_streamer,
        command_log=command_log,
        reuse_database=True,
    )
    if not args.keep_remote:
        remote_root = f"/data/local/tmp/hetero_{run_dir.name}"
        cleanup = client.shell(f"rm -rf {shlex.quote(remote_root)}", timeout=60)
        command_log.append(
            {
                "label": "cleanup-run-remote-root",
                "command": list(cleanup.command),
                "return_code": cleanup.return_code,
                "elapsed_seconds": cleanup.elapsed_seconds,
                "output": cleanup.combined_output[-4000:],
            }
        )
        write_json(command_path, command_log)
    print(f"完成：{run_dir / 'report.md'}", flush=True)
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hdc", help="HDC executable; otherwise HETERO_HDC_BIN or local runtime is used")
    parser.add_argument("--serial", help="Connected device serial; required only when multiple devices exist")
    parser.add_argument("--app-count", type=int, default=20, choices=range(1, 31))
    parser.add_argument("--dwell-seconds", type=float, default=1.5)
    parser.add_argument("--trace-buffer-kb", type=int, default=262144)
    parser.add_argument("--trace-profile", choices=tuple(TRACE_PROFILES), default="arkui-startup")
    parser.add_argument("--hiperf-frequency", type=int, default=300)
    parser.add_argument("--hiperf-cpu-limit", type=int, default=10)
    parser.add_argument("--per-app-arkui-limit", type=int, default=8)
    parser.add_argument(
        "--trace-streamer",
        help="TraceStreamer executable; otherwise HETERO_TRACE_STREAMER or a local artifact is used",
    )
    parser.add_argument("--source-root", default=str(WORKSPACE_ROOT), help="Root containing openharmony source")
    parser.add_argument(
        "--output-root",
        default=str(WEB_ROOT / ".local-artifacts/device-app-launch-runs"),
    )
    parser.add_argument("--keep-remote", action="store_true", help="Keep generated files under /data/local/tmp")
    parser.add_argument(
        "--resume-run",
        help="Resume TraceStreamer conversion and analysis from an existing local run directory",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.resume_run:
            resume_run(args)
        else:
            run_capture(args)
    except (RuntimeError, subprocess.TimeoutExpired, sqlite3.Error, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
