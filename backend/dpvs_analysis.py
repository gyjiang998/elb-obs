"""Offline, evidence-preserving DPVS/CVS profile comparison.

No uploaded text is executed. FlameGraph SVG is parsed for frame titles only,
never embedded in a generated report. Sample weights are not CPU time.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import re
import statistics
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .core import PlatformError, atomic_write_json, utc_now

SCHEMA = "dpvs_analysis/v1"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_RUN_BYTES = 48 * 1024 * 1024
MAX_ARTIFACTS = 32
DESKTOP_MAX_REQUEST_BYTES = 12 * 1024 * 1024
KINDS = ("perf_folded", "perf_report", "flamegraph_svg", "sar", "sadf", "numastat", "numa_maps")
LIMITATIONS = [
    "样本权重、事件次数、CPU 时间是不同量；采样占比上升不等于绝对开销上升。",
    "不同 event、采集对象或范围不可直接比较；node_count 不会自动把单机样本乘成集群样本。",
    "缺失或未展示的函数不自动记为零；Self 与 Inclusive 分开，后者不能跨函数相加作为总量。",
    "numastat / numa_maps 记录内存驻留或分配，不能推出实际远端访问率或伪共享。",
    "SAR 快照与 perf 可能不在同一时间窗；本报告不自动推断时间对齐或因果关系。",
    "扩展性只使用用户提供的集群总 PPS/CPS；未验证吞吐量口径，不会把单节点速率自动乘以节点数。",
]


def _error(message: str, **details):
    raise PlatformError("dpvs_analysis_invalid_input", message, details=details)


def _number(value: Any, *, positive: bool = False) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        _error("布尔值不能作为测量数值。")
    try:
        result = float(value)
    except (ValueError, TypeError):
        _error("测量数值格式不正确。", value=str(value)[:120])
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        _error("测量数值必须有限且非负；时长和采样频率必须大于零。")
    return result


def _float(value: str) -> float | None:
    try:
        result = float(value.rstrip("%"))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _profile(fmt: str, event: str | None, unit: str, coverage: str) -> dict:
    return {"format": fmt, "event": event or "unspecified", "weight_unit": unit,
            "coverage": coverage, "total_weight": None, "functions": [], "warnings": []}


def _functions(weights, total, *, self_available=True):
    rows = []
    for name, values in weights.items():
        own, inclusive = values
        rows.append({"name": name, "self_weight": own if self_available else None,
                     "inclusive_weight": inclusive,
                     "self_percent": own / total * 100 if total and self_available else None,
                     "inclusive_percent": inclusive / total * 100 if total else None})
    return sorted(rows, key=lambda row: (-(row["self_weight"] if self_available else row["inclusive_weight"]), row["name"]))


def parse_folded(content: str, event: str | None = None) -> dict:
    profile = _profile("perf_folded", event, "samples", "imported_stacks")
    weights = defaultdict(lambda: [0.0, 0.0])
    total = 0.0
    skipped = 0
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(.*?)\s+(\d+(?:\.\d+)?)$", line)
        if not match:
            skipped += 1
            continue
        stack = [frame.strip() for frame in match[1].split(";")]
        count = _number(match[2])
        if not all(stack) or not count:
            skipped += 1
            continue
        total += count
        weights[stack[-1]][0] += count
        # Recursion must not count a function twice in one sampled stack.
        for name in set(stack):
            weights[name][1] += count
    if not total:
        _error("folded 文件没有可识别的带权调用栈。")
    profile.update(total_weight=total, functions=_functions(weights, total), skipped_lines=skipped)
    if skipped:
        profile["coverage"] = "partial_stacks"
        profile["warnings"].append(f"忽略了 {skipped} 条无法解析或零权重记录；缺失函数不能按零处理。")
    profile["warnings"].append("folded 权重按样本处理；若实际来自 off-CPU 微秒或其他单位，请显式提供 weight_unit。")
    return profile


def parse_perf_report(content: str, event: str | None = None) -> dict:
    """Import the flat Self/Children section of perf report --stdio.

    Percentages are preserved verbatim. The reported approximate event total
    can provide labeled event-weight estimates, never invented sample counts.
    """
    profile = _profile("perf_report", event, "reported_percent", "displayed_rows")
    event_headers = re.findall(r"Samples:.*?of event ['\"]([^'\"]+)['\"]", content)
    if len(set(event_headers)) > 1:
        _error("一个 perf report 含多个事件；请按事件分别导出后导入。")
    if event_headers:
        if event and event != event_headers[0]:
            _error("声明的 event 与 perf report 头不一致。", declared=event, observed=event_headers[0])
        profile["event"] = event_headers[0]
    total_match = re.search(r"Event count \(approx\.\):\s*([\d,]+)", content)
    total = float(total_match[1].replace(",", "")) if total_match else None
    sample_total_match = re.search(r"Samples:\s*([\d,]+)\s+of event", content)
    sample_total = float(sample_total_match[1].replace(",", "")) if sample_total_match else None
    header = None
    collected = {}
    has_sample_column = False
    for line in content.splitlines():
        stripped = line.lstrip("# ")
        if "Symbol" in stripped and ("Self" in stripped or "Overhead" in stripped):
            header = stripped
            has_sample_column = "Samples" in header
            continue
        match = re.match(r"^\s*(\d+(?:\.\d+)?)%\s+(?:(\d+(?:\.\d+)?)%\s+)?(.*)$", line)
        if not match:
            continue
        # perf call-graph tree lines have no flat-row symbol marker.
        symbol = re.search(r"\[([.kugH])\]\s+(.+?)\s*$", match[3])
        if not symbol:
            continue
        first, second = float(match[1]), float(match[2]) if match[2] else None
        if second is not None:
            if not header or not ("Children" in header and "Self" in header):
                _error("两列百分比需要 Children/Self 表头，避免错误归因。")
            inclusive, own = (first, second) if header.index("Children") < header.index("Self") else (second, first)
        else:
            own, inclusive = first, None
        name = symbol[2].strip()
        row = collected.setdefault(name, {"name": name, "self_percent": 0.0, "inclusive_percent": None, "_samples": None})
        row["self_percent"] += own
        if has_sample_column:
            count_match = re.match(r"^([\d,]+)\s", match[3])
            if count_match:
                row["_samples"] = (row["_samples"] or 0) + int(count_match[1].replace(",", ""))
        if inclusive is not None:
            row["inclusive_percent"] = (row["inclusive_percent"] or 0.0) + inclusive
    if not collected:
        _error("无法识别 perf report 平面函数表；请导出 LC_ALL=C perf report --stdio --percent-limit 0。")
    for row in collected.values():
        samples = row.pop("_samples")
        for basis in ("self", "inclusive"):
            share = row[f"{basis}_percent"]
            row[f"{basis}_weight"] = (samples if basis == "self" else None) if has_sample_column else (
                total * share / 100 if total is not None and share is not None else None)
    if has_sample_column:
        profile["weight_unit"] = "samples"
        total = sample_total
    profile.update(total_weight=total, functions=sorted(collected.values(), key=lambda row: -row["self_percent"]))
    if total is not None and not has_sample_column:
        profile["weight_unit"] = "estimated_event_count"
        profile["warnings"].append("函数事件权重由已取整的百分比 × Event count (approx.) 估算，不是原始样本数。")
    if has_sample_column:
        profile["warnings"].append("Self 权重来自 Samples 列；perf 百分比可能按采样 period 加权，不据百分比反推样本数或 Inclusive 样本。")
    profile["warnings"].extend(["perf report 可能过滤或截断；未展示函数不当作零。",
                                "按 Symbol 聚合；不同 DSO 中的同名函数可能合并，请保持导出范围一致。"])
    return profile


def parse_flamegraph_svg(content: str, event: str | None = None) -> dict:
    # Standard FlameGraph emits an external SVG DOCTYPE. ElementTree does not
    # fetch that DTD. Internal subsets/entities are unnecessary and rejected.
    if re.search(r"<!\s*ENTITY\b|<!\s*DOCTYPE[^>]*\[", content, re.I):
        _error("SVG 不接受内部 DTD 或实体定义。")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        _error("SVG XML 无法解析。", reason=str(exc))
    if root.tag.split("}")[-1] != "svg":
        _error("上传内容不是 SVG。")
    parsed = []
    units = set()
    for group in root.iter():
        if group.tag.split("}")[-1] != "g":
            continue
        titles = [child for child in group if child.tag.split("}")[-1] == "title"]
        rects = [child for child in group if child.tag.split("}")[-1] == "rect"]
        if not titles or not rects:
            continue
        match = re.match(r"^(.*?)\s+\(([\d,]+(?:\.\d+)?)\s+([^,]+),\s*([\d.]+)%\)\s*$", "".join(titles[0].itertext()))
        if not match:
            continue
        count = _number(match[2].replace(",", ""))
        unit = match[3].strip()
        units.add("samples" if unit in ("sample", "samples") else unit)
        parsed.append((match[1], count, float(match[4])))
    if not parsed or len(units) != 1:
        _error("SVG 未包含同一单位的标准 FlameGraph title（函数名、权重、百分比）。")
    roots = [count for name, count, share in parsed if name == "all" and abs(share - 100) < .02]
    if len(roots) != 1:
        _error("SVG 缺少唯一 all / 100% 根帧；无法可靠建立占比分母，请使用 folded 文件。")
    total = roots[0]
    if not total:
        _error("SVG 根帧权重必须大于零。")
    weights = defaultdict(lambda: [0.0, 0.0])
    for name, count, _ in parsed:
        if name != "all":
            weights[name][1] += count
    profile = _profile("flamegraph_svg", event, units.pop(), "displayed_frames")
    profile.update(total_weight=total, functions=_functions(weights, total, self_available=False))
    profile["warnings"] = ["SVG 只导入可见 Inclusive 帧；裁剪/隐藏的小帧不能反推出 Self。",
                           "同名帧按显示权重累加；递归路径可能重复计算 Inclusive，优先用 folded 比较。",
                           "原始 SVG 的脚本、链接和样式不会执行或嵌入报告。"]
    return profile


_DIMS = {"CPU", "IFACE", "DEV", "INTR", "TTY", "FCHOST", "BUS", "TEMP", "FAN", "MHz"}
_METRIC = re.compile(r"^(?:%|kb|kB|rx|tx|[rtw]kB|[rtw]MB|pg|pswp|fault|majflt|commit|runq|plist|ldavg|blocked|tps|await|aqu|areq|bread|bwrtn|cswch|proc|dentunusd|file-nr|inode-nr|pty-nr)")


def _sar_clock_seconds(timestamp: str | None) -> float | None:
    """Return seconds in the displayed day for sar/ISO timestamps."""
    if not timestamp:
        return None
    match = re.search(r"(?:T|\s|^)(\d{1,2}):(\d{2}):(\d{2}(?:\.\d+)?)(?:\s*(AM|PM))?", timestamp, re.I)
    if not match:
        return None
    hour, minute, second = int(match[1]), int(match[2]), float(match[3])
    marker = (match[4] or "").upper()
    if marker:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if marker == "PM" else 0)
    if hour > 23 or minute > 59 or second >= 60:
        return None
    return hour * 3600 + minute * 60 + second


def _sar_timing(records: list[dict], restart_count: int) -> tuple[bool, list[str], str]:
    blockers = []
    if restart_count:
        blockers.append("sar_restart_boundary")
    explicit = [row["interval_seconds"] for row in records if row["interval_seconds"] is not None]
    if explicit and len(explicit) != len(records):
        blockers.append("mixed_interval_metadata")
    if explicit and len(explicit) == len(records):
        return not blockers, blockers, "explicit_interval_weighted"

    # Standard sar text has no interval column. Infer only whether each entity
    # uses a uniform cadence; do not invent weights for irregular windows.
    series = defaultdict(list)
    for row in records:
        seconds = _sar_clock_seconds(row.get("timestamp"))
        if seconds is not None:
            entity = tuple(sorted(row["dimensions"].items()))
            series[(row["boot_segment"], entity)].append(seconds)
    intervals = []
    for timestamps in series.values():
        for previous, current in zip(timestamps, timestamps[1:]):
            delta = current - previous
            if delta < 0:
                delta += 24 * 3600
            if delta > 0:
                intervals.append(delta)
    if len(intervals) >= 2 and max(intervals) - min(intervals) > 1e-6:
        blockers.append("variable_interval_unweighted")
        timing = "variable_interval_unweighted"
    elif intervals:
        timing = "uniform_interval_inferred"
    else:
        timing = "interval_unknown"
    return not blockers, blockers, timing


def parse_sar(content: str) -> dict:
    records, averages = [], []
    header = None
    delimiter = None
    boot_segment = 0
    restart_count = 0
    for original in content.splitlines():
        line = original.strip()
        if not line or line.startswith("Linux "):
            continue
        if "LINUX RESTART" in line:
            # A marker before the first measurement establishes the initial
            # boot. Only a marker after data creates a cross-boot boundary.
            if records or averages:
                restart_count += 1
                boot_segment += 1
            header = None
            delimiter = None
            continue
        clean = line.lstrip("# ")
        if ";" in clean or ("," in clean and ("timestamp" in clean.lower() or "%" in clean)):
            delimiter = ";" if ";" in clean else ","
        cells = next(csv.reader([clean], delimiter=delimiter)) if delimiter else clean.split()
        cells = [cell.strip() for cell in cells]
        # Remove sar's timestamp and optional AM/PM marker before header/data.
        timestamp = None
        is_average = bool(cells and cells[0] in ("Average:", "平均时间:"))
        if not delimiter and cells and (re.match(r"^\d{1,2}:\d{2}:\d{2}", cells[0]) or is_average):
            timestamp = cells.pop(0)
            if cells and cells[0] in ("AM", "PM"):
                timestamp += " " + cells.pop(0)
        if any(_METRIC.match(cell) and _float(cell) is None for cell in cells):
            # Numeric data can contain an interface name such as 'tx0': only
            # accept a header if at least one established metric name appears.
            if any(cell.startswith("%") or cell.endswith("/s") or cell in ("kbmemfree", "runq-sz", "ldavg-1", "tps", "await", "kbavail") for cell in cells):
                header = cells
                continue
        if not header or len(cells) != len(header):
            continue
        raw = dict(zip(header, cells))
        if any(key.lower() == "timestamp" for key in header):
            timestamp = next(raw[key] for key in header if key.lower() == "timestamp")
        dimensions = {key: value for key, value in raw.items() if key in _DIMS or key.lower() in ("hostname", "host")}
        metrics = {key: value for key, text in raw.items()
                   if key not in dimensions and key.lower() not in ("timestamp", "interval", "date", "time")
                   and (value := _float(text)) is not None}
        if not metrics:
            continue
        interval = _float(raw.get("interval", ""))
        row = {"timestamp": timestamp, "dimensions": dimensions, "metrics": metrics,
               "boot_segment": boot_segment,
               "interval_seconds": interval if interval and interval > 0 else None}
        (averages if is_average else records).append(row)
    if not records and not averages:
        _error("无法解析 sar/sadf 表；支持 LC_ALL=C 的 sar 文本和 sadf -d 分号 CSV。")
    comparison_allowed, comparison_blockers, timing = _sar_timing(records, restart_count)
    groups = defaultdict(list)
    for row in records or averages:
        entity = ", ".join(f"{key}={value}" for key, value in row["dimensions"].items()) or "all"
        for metric, value in row["metrics"].items():
            groups[(entity, metric)].append((value, row["interval_seconds"]))
    summary = []
    for (entity, metric), values in groups.items():
        weighted = all(interval is not None for _, interval in values)
        average = (sum(value * interval for value, interval in values) / sum(interval for _, interval in values)
                   if weighted else statistics.mean(value for value, _ in values))
        summary.append({"entity": entity, "metric": metric, "mean": average,
                        "min": min(value for value, _ in values), "max": max(value for value, _ in values),
                        "samples": len(values), "aggregation": "interval_weighted_mean" if weighted else "arithmetic_mean"})
    if not records:
        for row in summary:
            row["aggregation"] = "reported_average"
    warnings = ["保留各 CPU/接口维度；不把 all 与各 CPU 相加。均值不是累计事件差分；跨文件未自动时间对齐。"]
    if "sar_restart_boundary" in comparison_blockers:
        warnings.append("检测到 LINUX RESTART；保留各 boot_segment 原始行，但不把重启前后均值用于场景对比。")
    if "variable_interval_unweighted" in comparison_blockers:
        warnings.append("检测到无 interval 列的变间隔 sar 行；算术均值不是时间加权均值，不用于场景对比。")
    if "mixed_interval_metadata" in comparison_blockers:
        warnings.append("仅部分行提供 interval，无法建立一致时间权重，不用于场景对比。")
    return {"records": records, "averages": averages, "summary": summary,
            "coverage": "multiple_boot_segments" if restart_count else "interval_rows" if records else "average_only",
            "timing": timing, "boot_segments": boot_segment + 1, "restart_count": restart_count,
            "comparison_allowed": comparison_allowed, "comparison_blockers": comparison_blockers,
            "warnings": warnings}


def parse_numastat(content: str) -> dict:
    nodes = None
    rows = []
    process_memory = "Per-node process memory" in content or "in MB" in content
    for line in content.splitlines():
        matches = re.findall(r"\bnode\s*(\d+)\b", line, re.I)
        if matches:
            nodes = matches
            continue
        cells = line.split()
        if not nodes or len(cells) < len(nodes) + 1:
            continue
        values = [_float(cell) for cell in cells[1:]]
        if any(value is None for value in values):
            continue
        if cells[0] in ("Huge", "Heap", "Stack", "Private", "Total"):
            process_memory = True
        rows.append({"name": cells[0], "nodes": dict(zip(nodes, values)),
                     "total": values[len(nodes)] if len(values) > len(nodes) else sum(values)})
    if not rows:
        _error("无法解析 numastat；请导入包含 Node 列头的完整输出。")
    return {"format": "numastat", "unit": "MiB" if process_memory else "allocation_counter",
            "rows": rows, "warnings": ["驻留内存/分配计数不是远端访问次数；不能由各节点内存占比计算 remote 访问率。"]}


def parse_numa_maps(content: str) -> dict:
    entries, pages, known_bytes = [], defaultdict(int), defaultdict(int)
    unknown_sizes = 0
    for line in content.splitlines():
        cells = line.split()
        if len(cells) < 2 or not re.fullmatch(r"[0-9a-fA-F]+", cells[0]):
            continue
        node_pages = {node: int(count) for node, count in re.findall(r"\bN(\d+)=(\d+)", line)}
        if not node_pages:
            continue
        size_match = re.search(r"\bkernelpagesize_kB=(\d+)", line)
        page_kib = int(size_match[1]) if size_match else None
        if page_kib is None:
            unknown_sizes += 1
        entry = {"address": cells[0], "policy": cells[1], "node_pages": node_pages,
                 "kernel_page_kib": page_kib, "mapping": next((cell[5:] for cell in cells if cell.startswith("file=")), None)}
        entries.append(entry)
        for node, count in node_pages.items():
            pages[node] += count
            if page_kib is not None:
                known_bytes[node] += count * page_kib * 1024
    if not entries:
        _error("numa_maps 未找到带 N<node>=<pages> 的映射记录。")
    return {"format": "numa_maps", "entries": entries, "node_pages": dict(pages),
            "node_bytes_known": dict(known_bytes), "complete_page_sizes": not unknown_sizes,
            "warnings": ["只按各映射明确的 kernelpagesize_kB 换算字节；不默认页面为 4KiB。",
                         "这是映射驻留信息，重复映射可能重复计数；不是访问率或远端访存证明。"]}


def _detect(content: str, name: str) -> str:
    stripped = content.lstrip()
    if name.lower().endswith(".svg") or "<svg" in stripped[:2000]:
        return "flamegraph_svg"
    if re.search(r"\bN\d+=\d+", content) and re.search(r"^[0-9a-f]+\s", content, re.M):
        return "numa_maps"
    if "Per-node process memory" in content or re.search(r"numa_(hit|miss)", content):
        return "numastat"
    if "# Samples:" in content or re.search(r"(Children|Overhead).*Symbol", content):
        return "perf_report"
    if name.lower().endswith(".folded") or re.search(r"^\S[^\n]*;[^\n]*\s+\d+\s*$", content, re.M):
        return "perf_folded"
    return "sadf" if ";timestamp;" in content.lower() else "sar"


def _parse_artifact(artifact: dict, event: str | None) -> dict:
    content, name = artifact.get("content"), artifact.get("name", "input.txt")
    if not isinstance(content, str) or not isinstance(name, str):
        _error("artifact.name/content 必须为字符串。")
    if len(content.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        _error("单个导入文件超过 16MiB。")
    kind = artifact.get("kind", "auto")
    if kind == "auto":
        kind = _detect(content, name)
    if kind not in KINDS:
        _error("不支持的文件格式。", kind=kind)
    effective_event = artifact.get("event") or event
    if effective_event is not None and not isinstance(effective_event, str):
        _error("event 必须为字符串。")
    if kind == "perf_folded":
        parsed = parse_folded(content, effective_event)
    elif kind == "perf_report":
        parsed = parse_perf_report(content, effective_event)
    elif kind == "flamegraph_svg":
        parsed = parse_flamegraph_svg(content, effective_event)
    elif kind in ("sar", "sadf"):
        parsed = parse_sar(content)
    elif kind == "numastat":
        parsed = parse_numastat(content)
    else:
        parsed = parse_numa_maps(content)
    if "functions" in parsed and artifact.get("weight_unit"):
        if kind != "perf_folded" or not isinstance(artifact["weight_unit"], str):
            _error("weight_unit 覆盖只适用于 folded 格式。")
        parsed["weight_unit"] = artifact["weight_unit"]
    role = artifact.get("role", artifact.get("context", "unspecified"))
    if role not in ("host", "guest", "unspecified"):
        role = "unspecified"
    return {"name": name, "kind": kind, "sha256": _sha(content),
            "bytes": len(content.encode("utf-8")), "context": str(artifact.get("context", "unspecified"))[:240],
            "role": role, "parser": SCHEMA + ":" + kind,
            "confidence": "source_values" if kind in ("sar", "sadf", "numastat", "numa_maps", "perf_folded") else "displayed_or_estimated",
            "parsed": parsed}


def _run_summary(run: dict) -> dict:
    return {key: run.get(key) for key in ("id", "name", "created_at", "node_count", "scope", "duration_seconds", "event",
                                           "sampling_hz", "sampling_config", "sampling_target", "sar_window_signature", "workload")}


def _sampling_signature(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _pick_profile(run: dict, name: str | None = None) -> dict | None:
    profiles = [artifact for artifact in run["artifacts"] if "functions" in artifact["parsed"]]
    if name:
        profiles = [profile for profile in profiles if profile["name"] == name]
        if not profiles:
            _error("指定的 perf artifact 不存在。", name=name)
    if len(profiles) > 1:
        _error("运行包含多个 perf 文件，请指定 baseline_profile / candidate_profile 的文件名。")
    return profiles[0]["parsed"] if profiles else None


def _sar_comparison(baseline: dict, candidate: dict) -> dict:
    def collect(run):
        grouped = defaultdict(list)
        for artifact in run["artifacts"]:
            if artifact["kind"] not in ("sar", "sadf"):
                continue
            for row in artifact["parsed"]["summary"]:
                grouped[(artifact["role"], artifact["context"], row["entity"], row["metric"])].append({
                    **row, "source": artifact["name"], "coverage": artifact["parsed"]["coverage"],
                    "comparison_allowed": artifact["parsed"].get("comparison_allowed", True),
                    "comparison_blockers": artifact["parsed"].get("comparison_blockers", [])})
        return grouped
    left, right = collect(baseline), collect(candidate)
    baseline_window = _sampling_signature(baseline.get("sar_window_signature"))
    candidate_window = _sampling_signature(candidate.get("sar_window_signature"))
    windows_aligned = bool(baseline_window and baseline_window == candidate_window)
    rows = []
    for role, context, entity, metric in sorted(left.keys() | right.keys()):
        key = (role, context, entity, metric)
        a, b = left.get(key, []), right.get(key, [])
        blocked = [entry for entry in a + b if not entry.get("comparison_allowed", True)]
        av = a[0]["mean"] if len(a) == 1 and not blocked else None
        bv = b[0]["mean"] if len(b) == 1 and not blocked else None
        unit = "percent" if metric.startswith("%") else "source_defined"
        reason = ("multiple_sources_not_merged" if len(a) > 1 or len(b) > 1 else
                  "missing_side" if not a or not b else
                  "sar_source_not_comparable" if blocked else
                  "windows_not_aligned" if not windows_aligned else None)
        paired = reason is None
        rows.append({"role": role, "context": context, "entity": entity, "metric": metric, "unit": unit,
                     "baseline": av, "candidate": bv, "delta": bv - av if paired and av is not None and bv is not None else None,
                     "delta_unit": "percentage_points" if unit == "percent" else "source_defined",
                     "relative_change_percent": (bv / av - 1) * 100 if paired and av and bv is not None else None,
                     "baseline_sources": a, "candidate_sources": b,
                     "reason": reason, "baseline_window_signature": baseline.get("sar_window_signature"),
                     "candidate_window_signature": candidate.get("sar_window_signature"),
                     "comparison_blockers": sorted({item for entry in blocked for item in entry.get("comparison_blockers", [])})})
    return {"metrics": rows, "limitations": [
        "只匹配相同 role/context/entity/原始 metric 名称；%sys 与 %system 不自动合并。",
        "缺失侧保持 null；同维度多个来源不擅自合并。Average-only 不代表完整时间序列。",
        "百分比的 delta 单位为百分点；其他指标单位保留 source_defined，未确认单位不作跨指标比较。",
        "只有两侧非空且相同的 sar_window_signature 才计算差值；平台不自动对齐时间窗。"]}


def _numa_analysis(baseline: dict, candidate: dict) -> dict:
    result = {"baseline": [], "candidate": [], "remote_access_fraction": None,
              "conclusion": "NUMA 驻留/分配证据不能推出实际远端访问率、伪共享或退化原因。"}
    for side, run in (("baseline", baseline), ("candidate", candidate)):
        for artifact in run["artifacts"]:
            parsed = artifact["parsed"]
            if artifact["kind"] == "numastat":
                result[side].append({"source": artifact["name"], "role": artifact["role"],
                                     "kind": "residency" if parsed["unit"] == "MiB" else "allocation_counters",
                                     "unit": parsed["unit"], "rows": parsed["rows"]})
            elif artifact["kind"] == "numa_maps":
                result[side].append({"source": artifact["name"], "role": artifact["role"], "kind": "mapping_residency",
                                     "node_bytes_known": parsed["node_bytes_known"], "complete_page_sizes": parsed["complete_page_sizes"],
                                     "policies": sorted({entry["policy"] for entry in parsed["entries"]})})
    result["paired_evidence"] = bool(result["baseline"] and result["candidate"])
    return result


def _scaling_analysis(baseline: dict, candidate: dict) -> dict:
    node_ratio = candidate["node_count"] / baseline["node_count"]
    metrics = {}
    for metric in ("pps", "cps"):
        a, b = baseline["workload"].get(metric), candidate["workload"].get(metric)
        valid = a is not None and b is not None and a > 0 and b > 0
        speedup = b / a if valid else None
        efficiency = speedup / node_ratio if valid else None
        metrics[metric] = {"baseline": a, "candidate": b, "speedup": speedup,
                           "efficiency": efficiency,
                           "efficiency_percent": efficiency * 100 if efficiency is not None else None,
                           "reason": None if valid else "missing_or_zero_throughput"}
    return {"baseline_nodes": baseline["node_count"], "candidate_nodes": candidate["node_count"],
            "node_ratio": node_ratio, "input_scope": "cluster_total_required", "scope_verified": False,
            "metrics": metrics, "limitations": [
                "workload.pps/cps 必须是各场景集群总吞吐量；单节点采样值不得直接代入，本模块未验证输入口径。",
                "run.scope 描述 perf 采集范围，不用于推断吞吐量范围，也不会乘节点数补成集群总量。",
                "效率 = 吞吐量倍率 / 节点数倍率；缺失或零吞吐量不计算，重复运行与相同业务条件仍需另行验证。"]}


def compare_runs(baseline: dict, candidate: dict, *, metric: str = "self", baseline_profile: str | None = None,
                 candidate_profile: str | None = None) -> dict:
    if metric not in ("self", "inclusive"):
        _error("metric 必须为 self 或 inclusive。")
    left, right = _pick_profile(baseline, baseline_profile), _pick_profile(candidate, candidate_profile)
    reasons = []
    if not left or not right:
        reasons.append("需要两组 perf 数据才能计算函数差异；仍可比较已有 SAR/NUMA 证据。")
    else:
        if left["event"] != right["event"] or left["event"] == "unspecified":
            reasons.append("采样 event 不同或未声明，函数占比不作等价性能比较。")
        if not any(row.get(f"{metric}_percent") is not None for row in left["functions"]) or not any(row.get(f"{metric}_percent") is not None for row in right["functions"]):
            reasons.append(f"至少一组不包含 {metric}；SVG 请显式选择 inclusive，不能冒充 Self。")
    scope_match = baseline["scope"] == candidate["scope"] or (baseline["node_count"] == candidate["node_count"] == 1)
    if not scope_match:
        reasons.append("node / cluster 采集范围不同，绝对值不能直接比较。")
    weights_match = bool(left and right and left["weight_unit"] == right["weight_unit"])
    baseline_target = _sampling_signature(baseline.get("sampling_target"))
    candidate_target = _sampling_signature(candidate.get("sampling_target"))
    sampling_target_match = bool(baseline_target and baseline_target == candidate_target)
    if left and right and not sampling_target_match:
        reasons.append("采样目标（PID/TID/CPU/lcore 范围）未声明相同，函数占比不可直接对比。")
    baseline_config = _sampling_signature(baseline.get("sampling_config"))
    candidate_config = _sampling_signature(candidate.get("sampling_config"))
    sampling_config_match = bool(baseline_config and baseline_config == candidate_config)
    # sampling_hz is a backwards-compatible shorthand only when neither side
    # supplied the richer freq/period signature. It must never override a
    # conflicting explicit configuration.
    if not baseline_config and not candidate_config:
        sampling_config_match = (baseline.get("sampling_hz") is not None and
                                 baseline.get("sampling_hz") == candidate.get("sampling_hz"))
    comparable = not reasons
    absolute_reasons = list(reasons)
    if not weights_match:
        absolute_reasons.append("权重单位不一致或不可用。")
    if left and left["weight_unit"] == "samples" and not sampling_config_match:
        absolute_reasons.append("采样配置（freq/period）未确认相同；样本率变化不能解释为绝对开销变化。")
    rate_reasons = list(absolute_reasons)
    if not baseline["duration_seconds"] or not candidate["duration_seconds"]:
        rate_reasons.append("至少一组缺采集时长，不能计算绝对权重率。")
    lrows = {row["name"]: row for row in left["functions"]} if left else {}
    rrows = {row["name"]: row for row in right["functions"]} if right else {}

    def value(profile, rows, name, field):
        if name in rows:
            return rows[name].get(field)
        if profile and profile["coverage"] == "imported_stacks":
            return 0.0
        return None

    def measurement(run, profile, rows, name):
        weight = value(profile, rows, name, f"{metric}_weight")
        duration = run["duration_seconds"]
        packets = run["workload"].get("packets")
        connections = run["workload"].get("connections")
        return {"present": name in rows, "percent": value(profile, rows, name, f"{metric}_percent"),
                "weight": weight, "weight_per_second": weight / duration if weight is not None and duration else None,
                "weight_per_packet": weight / packets if weight is not None and packets else None,
                "weight_per_connection": weight / connections if weight is not None and connections else None}

    def delta(a, b):
        return b - a if a is not None and b is not None else None

    functions = []
    for name in sorted(lrows.keys() | rrows.keys()):
        a = measurement(baseline, left, lrows, name)
        b = measurement(candidate, right, rrows, name)
        functions.append({"name": name, "baseline": a, "candidate": b,
                          "share_delta_pp": delta(a["percent"], b["percent"]) if comparable else None,
                          "weight_delta": delta(a["weight"], b["weight"]) if comparable and weights_match else None,
                          "rate_delta": delta(a["weight_per_second"], b["weight_per_second"]) if not rate_reasons else None,
                          "per_packet_delta": delta(a["weight_per_packet"], b["weight_per_packet"]) if not absolute_reasons else None,
                          "per_connection_delta": delta(a["weight_per_connection"], b["weight_per_connection"]) if not absolute_reasons else None,
                          "relative_share_change_percent": ((b["percent"] / a["percent"] - 1) * 100)
                          if comparable and a["percent"] and b["percent"] is not None else None})
    functions.sort(key=lambda row: -(abs(row["share_delta_pp"]) if row["share_delta_pp"] is not None else -1))
    findings = []
    if functions and functions[0]["share_delta_pp"] is not None:
        top = functions[0]
        findings.append({"level": "observation", "title": f"最大占比变化：{top['name']}",
                         "detail": f"{metric} 改变 {top['share_delta_pp']:+.3f} 个百分点；这不是 CPU 耗时增长率。"})
    for reason in rate_reasons:
        findings.append({"level": "limitation", "title": "比较条件不足", "detail": reason})
    if any("do_lcore_job" in name for name in lrows.keys() | rrows.keys()):
        findings.append({"level": "next_check", "title": "轮询与业务归因",
                         "detail": "do_lcore_job 是调度包装；结合调用栈、逐核 RX 与 empty burst，不能单凭符号占比判定空轮询。"})
    telemetry = {"baseline": [a for a in baseline["artifacts"] if "functions" not in a["parsed"]],
                 "candidate": [a for a in candidate["artifacts"] if "functions" not in a["parsed"]]}
    if any(a["kind"] in ("numastat", "numa_maps") for a in telemetry["baseline"] + telemetry["candidate"]):
        findings.append({"level": "next_check", "title": "NUMA 驻留证据",
                         "detail": "对齐 vCPU、OVS PMD、NIC 节点与 Guest RAM 映射；驻留各半不等于一半实际访问远端。"})
    result = {"schema_version": SCHEMA, "created_at": utc_now(), "baseline": _run_summary(baseline),
            "candidate": _run_summary(candidate), "comparison": {
                "metric": metric, "comparable": comparable, "reasons": reasons,
                "absolute_rate_comparable": not rate_reasons, "absolute_reasons": rate_reasons,
                "baseline_profile": left and {key: left[key] for key in ("format", "event", "weight_unit", "coverage", "total_weight", "warnings")},
                "candidate_profile": right and {key: right[key] for key in ("format", "event", "weight_unit", "coverage", "total_weight", "warnings")},
                "functions": functions}, "telemetry": telemetry, "findings": findings, "limitations": LIMITATIONS}
    result["manifest"] = {"schema_version": SCHEMA, "scenarios": {"baseline": result["baseline"], "candidate": result["candidate"]}, "metric": metric}
    result["coverage"] = {side: {"perf": bool(_pick_profile(run, baseline_profile if side == "baseline" else candidate_profile)),
                                "sar": any(a["kind"] in ("sar", "sadf") for a in run["artifacts"]),
                                "numa": any(a["kind"] in ("numastat", "numa_maps") for a in run["artifacts"]),
                                "duration": run["duration_seconds"] is not None,
                                "packet_denominator": bool(run["workload"].get("packets")),
                                "repeated_runs": False}
                          for side, run in (("baseline", baseline), ("candidate", candidate))}
    result["perf_comparison"] = result["comparison"]
    result["sar_comparison"] = _sar_comparison(baseline, candidate)
    result["host_numa_analysis"] = _numa_analysis(baseline, candidate)
    result["scaling_analysis"] = _scaling_analysis(baseline, candidate)
    result["recommendations"] = [
        {"priority": 1, "action": "用相同 event、CPU/TID 范围、采样频率与采集窗口重测，记录实际 packets/connections。", "purpose": "比较权重率与每包/每连接成本，避免只比较占比。"},
        {"priority": 2, "action": "对齐逐核 RX/TX、OVS PMD 和 Host vCPU 调度时间线；进行独立重复采集。", "purpose": "区分供包变化、运行时间变化与函数成本，当前报告不提供统计显著性。"}]
    result["provenance"] = [{"scenario": side, **{key: a[key] for key in ("name", "kind", "sha256", "bytes", "role", "context", "parser", "confidence")}}
                            for side, run in (("baseline", baseline), ("candidate", candidate)) for a in run["artifacts"]]
    return result


def _display(value, digits=3):
    return "—" if value is None else f"{value:.{digits}f}" if isinstance(value, (int, float)) else str(value)


def render_markdown(report: dict) -> str:
    def safe(value):
        value = str(value).replace("\\", "\\\\").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")
        for character in "|`[]":
            value = value.replace(character, "\\" + character)
        return value
    lines = ["# DPVS / CVS 性能对比报告", "", f"{safe(report['baseline']['name'])} → {safe(report['candidate']['name'])}",
             "", f"归因：{report['comparison']['metric']}；event/范围比较条件：{'满足' if report['comparison']['comparable'] else '不足'}。",
             "", "| 函数 | 基线占比 % | 对照占比 % | 差异 pp | 基线权重/s | 对照权重/s |",
             "|---|---:|---:|---:|---:|---:|"]
    for row in report["comparison"]["functions"]:
        lines.append("| " + " | ".join([safe(row["name"]), _display(row["baseline"]["percent"]),
                     _display(row["candidate"]["percent"]), _display(row["share_delta_pp"]),
                     _display(row["baseline"]["weight_per_second"]), _display(row["candidate"]["weight_per_second"])]) + " |")
    lines += ["", "## 证据与解释", ""]
    for finding in report["findings"]:
        lines.append(f"- {safe(finding['title'])}：{safe(finding['detail'])}")
    lines += ["", "## 下一步验证", ""]
    for recommendation in report["recommendations"]:
        lines.append(f"- {safe(recommendation['action'])} {safe(recommendation['purpose'])}")
    lines += ["", "## 限制", ""] + [f"- {safe(text)}" for text in report["limitations"]]
    for side in ("baseline", "candidate"):
        profile = report["comparison"].get(f"{side}_profile")
        if profile:
            lines += [f"- {side}：{safe(profile['weight_unit'])}，{safe(profile['coverage'])}。"]
            lines += [f"- {safe(text)}" for text in profile["warnings"]]
    details = {key: report[key] for key in ("manifest", "coverage", "scaling_analysis", "sar_comparison", "host_numa_analysis", "provenance", "telemetry")}
    lines += ["", "## 采集范围、SAR / NUMA 与来源证据", "", "```json", json.dumps(details, ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def render_html(report: dict) -> str:
    esc = lambda value: html.escape(str(value), quote=True)
    rows = []
    for row in report["comparison"]["functions"]:
        cells = [row["name"], _display(row["baseline"]["percent"]), _display(row["candidate"]["percent"]),
                 _display(row["share_delta_pp"]), _display(row["baseline"]["weight"]), _display(row["candidate"]["weight"]),
                 _display(row["baseline"]["weight_per_second"]), _display(row["candidate"]["weight_per_second"])]
        rows.append("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in cells) + "</tr>")
    findings = "".join(f"<li><b>{esc(f['title'])}</b>：{esc(f['detail'])}</li>" for f in report["findings"])
    warnings = list(report["limitations"])
    for side in ("baseline", "candidate"):
        profile = report["comparison"].get(f"{side}_profile")
        if profile:
            warnings.extend(profile["warnings"])
    metadata = {"baseline": report["baseline"], "candidate": report["candidate"],
                "comparison_basis": {k: v for k, v in report["comparison"].items() if k != "functions"}}
    metadata.update({key: report[key] for key in ("manifest", "coverage", "scaling_analysis", "provenance")})
    telemetry = {key: report[key] for key in ("sar_comparison", "host_numa_analysis", "telemetry")}
    recommendations = "".join(f"<li>{esc(r['action'])} {esc(r['purpose'])}</li>" for r in report["recommendations"])
    return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<title>DPVS / CVS 性能对比报告</title><style>body{font:15px/1.6 system-ui,sans-serif;margin:32px;color:#182331}"
            "h1{font-size:26px}table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #d8dfe7;text-align:right}"
            "td:first-child,th:first-child{text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f8;padding:16px}"
            ".scroll{overflow-x:auto}li{margin:8px 0}</style><body><h1>DPVS / CVS 性能对比报告</h1>"
            f"<p>{esc(report['baseline']['name'])} → {esc(report['candidate']['name'])} · {esc(report['comparison']['metric'])}</p>"
            "<div class='scroll'><table><thead><tr>"
            + "".join(f"<th>{h}</th>" for h in ("函数", "基线 %", "对照 %", "差异 pp", "基线权重", "对照权重", "基线权重/s", "对照权重/s"))
            + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div><h2>证据与解释</h2><ul>" + findings
            + "</ul><h2>下一步验证</h2><ul>" + recommendations + "</ul><h2>限制</h2><ul>" + "".join(f"<li>{esc(w)}</li>" for w in warnings)
            + "</ul><details><summary>采集范围与比较依据</summary><pre>" + esc(json.dumps(metadata, ensure_ascii=False, indent=2))
            + "</pre></details><details><summary>SAR / NUMA 解析证据</summary><pre>"
            + esc(json.dumps(telemetry, ensure_ascii=False, indent=2)) + "</pre></details></body></html>")


class DPVSAnalysisManager:
    def __init__(self, state_root: Path):
        self.state_root = Path(state_root) / "dpvs-analysis"
        self.runs_root = self.state_root / "runs"
        self.reports_root = self.state_root / "reports"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.reports_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def catalog():
        return {"schema_version": SCHEMA, "artifact_kinds": list(KINDS), "max_artifact_bytes": MAX_ARTIFACT_BYTES,
                "max_run_bytes": MAX_RUN_BYTES, "max_artifacts": MAX_ARTIFACTS,
                "desktop_max_request_bytes": DESKTOP_MAX_REQUEST_BYTES,
                "transport_limits": "HTTP 后端文件上限 16MiB/次导入 48MiB；Desktop 整个 JSON RPC 请求上限 12MiB，包含 JSON 转义与请求封装。",
                "scope_options": ["node", "cluster"], "comparison_metrics": ["self", "inclusive"],
                "sampling_fields": {
                    "sampling_target": "PID/TID/CPU/lcore 目标范围签名；两侧非空且相同时才比较函数占比",
                    "sampling_config": "freq/period 等采样策略签名；样本权重率比较还要求两侧相同",
                    "sampling_hz": "兼容 freq=N 的数值简写",
                    "sar_window_signature": "SAR 稳态阶段/时长标签；两侧非空且相同才计算均值差值",
                },
                "workload_semantics": {"packets": "perf 采集范围与时间窗内的实际包总数", "connections": "perf 采集范围与时间窗内的实际连接总数",
                                       "pps": "该场景集群总包率，用于扩展性计算", "cps": "该场景集群总连接率，用于扩展性计算"},
                "export_formats": ["json", "markdown", "html"], "limitations": LIMITATIONS,
                "collection_commands": ["LC_ALL=C perf report --stdio -n --percent-limit 0",
                                        "LC_ALL=C sadf -d <sar-file> -- -u ALL", "numastat -p <QEMU_PID>",
                                        "cat /proc/<QEMU_PID>/numa_maps"]}

    def import_run(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            _error("导入请求必须是对象。")
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_ARTIFACTS or not all(isinstance(a, dict) for a in artifacts):
            _error("artifacts 必须含 1–32 个文件对象。")
        if any(not isinstance(a.get("content"), str) for a in artifacts):
            _error("每个 artifact 必须有字符串 content。")
        if sum(len(a["content"].encode("utf-8")) for a in artifacts) > MAX_RUN_BYTES:
            _error("一次导入超过 48MiB。")
        names = [a.get("name", "input.txt") for a in artifacts]
        if not all(isinstance(name, str) and 0 < len(name) <= 240 for name in names) or len(names) != len(set(names)):
            _error("同一次导入的文件名必须是 1–240 字符、互不重复的字符串。")
        nodes = payload.get("node_count", 1)
        if type(nodes) is not int or not 1 <= nodes <= 4096:
            _error("node_count 必须是 1–4096 的整数。")
        scope = payload.get("scope", "node")
        if scope not in ("node", "cluster"):
            _error("scope 必须为 node 或 cluster。")
        event = payload.get("event")
        if event is not None and (not isinstance(event, str) or len(event) > 240):
            _error("event 必须是最多 240 字符的字符串。")
        sampling_labels = {}
        for key in ("sampling_config", "sampling_target", "sar_window_signature"):
            value = payload.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 500):
                _error(f"{key} 必须是最多 500 字符的字符串。")
            sampling_labels[key] = value.strip() if isinstance(value, str) and value.strip() else None
        workload = payload.get("workload") or {}
        if not isinstance(workload, dict):
            _error("workload 必须是对象。")
        workload = {key: _number(workload.get(key)) for key in ("packets", "connections", "pps", "cps") if key in workload}
        parsed = [_parse_artifact(artifact, event) for artifact in artifacts]
        identity = "run_" + uuid.uuid4().hex[:16]
        run = {"schema_version": SCHEMA, "id": identity, "name": str(payload.get("name") or identity)[:240],
               "created_at": utc_now(), "node_count": nodes, "scope": scope, "event": event or "unspecified",
               "duration_seconds": _number(payload.get("duration_seconds"), positive=True),
               "sampling_hz": _number(payload.get("sampling_hz"), positive=True), **sampling_labels,
               "workload": workload,
               "artifacts": parsed, "warnings": [warning for artifact in parsed for warning in artifact["parsed"].get("warnings", [])]}
        raw_root = self.runs_root / identity
        raw_root.mkdir()
        for index, artifact in enumerate(artifacts):
            # User names are metadata only, never filesystem paths.
            (raw_root / f"source-{index:02}.txt").write_text(artifact["content"], encoding="utf-8")
        atomic_write_json(self.runs_root / f"{identity}.json", run)
        return run

    def _load(self, root: Path, identity: str, prefix: str) -> dict:
        if not isinstance(identity, str) or not re.fullmatch(prefix + r"_[0-9a-f]{16}", identity):
            raise PlatformError("dpvs_analysis_not_found", "记录不存在。", status_code=404)
        path = root / f"{identity}.json"
        if not path.is_file():
            raise PlatformError("dpvs_analysis_not_found", "记录不存在。", status_code=404)
        return json.loads(path.read_text(encoding="utf-8"))

    def list_runs(self) -> dict:
        runs = [_run_summary(json.loads(path.read_text(encoding="utf-8"))) for path in self.runs_root.glob("run_*.json")]
        return {"runs": sorted(runs, key=lambda run: run["created_at"], reverse=True)}

    def get_run(self, identity: str) -> dict:
        return self._load(self.runs_root, identity, "run")

    def compare(self, payload: dict) -> dict:
        baseline = self.get_run(payload.get("baseline_id"))
        candidate = self.get_run(payload.get("candidate_id"))
        report = compare_runs(baseline, candidate, metric=payload.get("metric", "self"),
                              baseline_profile=payload.get("baseline_profile"), candidate_profile=payload.get("candidate_profile"))
        report["id"] = "report_" + uuid.uuid4().hex[:16]
        report["analysis_id"] = report["id"]
        atomic_write_json(self.reports_root / f"{report['id']}.json", report)
        return report

    def get_report(self, identity: str) -> dict:
        return self._load(self.reports_root, identity, "report")

    def export_report(self, identity: str, fmt: str) -> tuple[str, str, str]:
        report = self.get_report(identity)
        if fmt == "json":
            return json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), "application/json", "json"
        if fmt in ("markdown", "md"):
            return render_markdown(report), "text/markdown", "md"
        if fmt == "html":
            return render_html(report), "text/html", "html"
        _error("不支持的导出格式。", format=fmt)


def create_dpvs_analysis_router(manager: DPVSAnalysisManager):
    from fastapi import APIRouter, Body, Query
    from fastapi.responses import Response

    router = APIRouter(prefix="/v1/dpvs-analysis", tags=["dpvs-analysis"])

    @router.get("/catalog")
    def catalog():
        return manager.catalog()

    @router.get("/runs")
    def runs():
        return manager.list_runs()

    @router.post("/runs")
    def import_run(payload: dict[str, Any] = Body(...)):
        return manager.import_run(payload)

    @router.get("/runs/{identity}")
    def get_run(identity: str):
        return manager.get_run(identity)

    @router.post("/compare")
    def compare(payload: dict[str, Any] = Body(...)):
        return manager.compare(payload)

    @router.get("/reports/{identity}")
    def report(identity: str):
        return manager.get_report(identity)

    @router.get("/reports/{identity}/export")
    def export(identity: str, format: str = Query("json")):
        content, media, extension = manager.export_report(identity, format)
        return Response(content=content, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{identity}.{extension}"',
                                 "X-Content-Type-Options": "nosniff"})

    return router


def create_dpvs_router(state_root: Path):
    return create_dpvs_analysis_router(DPVSAnalysisManager(state_root))


def dispatch_dpvs_analysis(request: dict[str, Any], state_root: Path) -> dict | None:
    """Desktop RPC adapter; importing this module does not import FastAPI."""
    parsed = urlsplit(str(request.get("path") or ""))
    prefix = "/v1/dpvs-analysis"
    path = parsed.path.rstrip("/")
    if path != prefix and not path.startswith(prefix + "/"):
        return None
    if len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")) > DESKTOP_MAX_REQUEST_BYTES:
        _error("Desktop JSON RPC 请求超过 12MiB（包含 JSON 转义与请求封装）。")
    method = str(request.get("method") or "GET").upper()
    body = request.get("body") or {}
    if not isinstance(body, dict):
        _error("请求 body 必须为对象。")
    manager = DPVSAnalysisManager(state_root)
    if method == "GET" and path == prefix + "/catalog":
        return manager.catalog()
    if path == prefix + "/runs":
        if method == "GET":
            return manager.list_runs()
        if method == "POST":
            return manager.import_run(body)
    if method == "POST" and path == prefix + "/compare":
        return manager.compare(body)
    match = re.fullmatch(re.escape(prefix) + r"/(runs|reports)/([^/]+)(/export)?", path)
    if method == "GET" and match:
        kind, identity, export = match.groups()
        if kind == "runs" and not export:
            return manager.get_run(identity)
        if kind == "reports" and not export:
            return manager.get_report(identity)
        if kind == "reports" and export:
            fmt = parse_qs(parsed.query).get("format", ["json"])[-1]
            content, media, extension = manager.export_report(identity, fmt)
            return {"__desktop_response_kind__": "text", "content": content, "content_type": media,
                    "filename": f"{identity}.{extension}"}
    raise PlatformError("dpvs_analysis_not_found", "未找到该分析接口。", status_code=404)
