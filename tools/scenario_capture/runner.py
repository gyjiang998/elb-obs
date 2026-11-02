"""Fixed device-side runner. No local HDC or execution of Agent-authored Python.

UI primitives originate in the two capture_gallery scripts in provenance.json.
All device I/O is supplied by a registered DeviceClient. Collectors and UI input
share one bounded device shell, so a Bridge's single HDC lock cannot deadlock
an active collector against an independently submitted UI input command.
"""
from __future__ import annotations

import hashlib
import csv
import json
import re
import shlex
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol

from .gallery import CapturePreparationError, build_actions, point_from_ratio, select_node, sync_screen_geometry, verify_page_available, verify_screen
from .screen_preparation import prepare_capture_device
from .screen_awake import ScreenAwakeGuard
from .gallery_reset import GALLERY_BUNDLES, reset_gallery_launch
from .collector_cleanup import CollectorSession, _PROCESS_HELPERS, step_shell_prelude

RUNNER_VERSION = "1.2.15"


def pmu_validation_schema(group):
    return "pmu_capture_validation/v3" if group in {"auto", "all"} else "pmu_capture_validation/v2"


REMOTE_LOCK = "/data/local/tmp/hetero_scenario_capture.lock"
PREPARATION_SCREENSHOT_MAX_BYTES = 16 * 1024 * 1024
PREPARATION_SCREENSHOT_KIND = "screen_preparation_screenshot"
SCREEN_EVIDENCE_BUDGET_SECONDS = 20
STEP_SHELL_CLEANUP_ALLOWANCE_SECONDS = 45
CHILD_POLL_SECONDS = 0.05
CHILD_TERM_GRACE_TICKS = 5
CHILD_KILL_GRACE_TICKS = 20
TRACE_FINISH_MAX_TICKS = 500
TRACE_FINISH_BASE_TICKS = 60
TRACE_FINISH_BUFFER_CHUNK_KB = 64 * 1024
TRACE_FINISH_BUFFER_EXTRA_TICKS = 20
# Worst sequential path: watchdog timer + watchdog, four ordinary children
# (action, heartbeat, sampler, logger), then trace serialization. PMU probing
# happens before those children start and has its own shorter bound.
STEP_SHELL_CLEANUP_BOUND_SECONDS = CHILD_POLL_SECONDS * (
    2 * (10 + CHILD_TERM_GRACE_TICKS + CHILD_KILL_GRACE_TICKS)
    + 4 * (20 + CHILD_TERM_GRACE_TICKS + CHILD_KILL_GRACE_TICKS)
    + TRACE_FINISH_MAX_TICKS + CHILD_TERM_GRACE_TICKS + CHILD_KILL_GRACE_TICKS
)
if STEP_SHELL_CLEANUP_BOUND_SECONDS >= STEP_SHELL_CLEANUP_ALLOWANCE_SECONDS:
    raise RuntimeError("Step cleanup bound must remain below its host timeout allowance")


class DeviceClient(Protocol):
    def shell(self, command: str, timeout: int = 40, *, cleanup: bool = False) -> str: ...
    def collect(self, remote: str, destination: Path, *, timeout: int = 600, cleanup: bool = True) -> dict[str, Any]: ...
    def check_cancelled(self) -> None: ...


class PreparationClient:
    """Bound individual device operations and report cancellable preparation.

    Normal preparation has no cumulative deadline: each supported method can
    run even when earlier bridge round trips were slow. Callers may explicitly
    bound failure-evidence collection; that deadline also reserves the device
    adapter's process-reaping/result-delivery allowance before each operation.
    """
    def __init__(self, client, record, emit, *, seconds=None,
                 key="screen_preparation_timing", phase="screen_preparation"):
        self.client, self.record, self.emit = client, record, emit
        self.started = time.monotonic()
        self.deadline = self.started + seconds if seconds is not None else None
        self.timing = {"phase": phase, "budget_seconds": seconds, "status": "running",
                       "operation": "start", "message": "正在准备采集界面", "operations": []}
        self.record[key] = self.timing
        self.publish()

    def publish(self):
        now = time.monotonic()
        self.timing.update(elapsed_seconds=round(now - self.started, 3),
                           remaining_seconds=round(max(0, self.deadline - now), 3) if self.deadline is not None else None)
        self.emit()

    def timeout_error(self):
        stage = "失败现场回收" if self.timing["phase"] == "screen_evidence" else "屏幕准备"
        self.timing.update(status="timed_out", message=stage + "预算已用尽：" + self.timing["operation"])
        self.publish()
        return CapturePreparationError("SCENARIO_SCREEN_PREPARATION_TIMEOUT",
            f"{stage}未能在 {self.timing['budget_seconds']} 秒预算内完成，停在：{self.timing['operation']}。",
            "请查看任务详情中的具体操作、耗时和失败现场；远端采集同时核对 Windows Bridge 版本与连接状态。",
            phase=self.timing["phase"])

    def check_cancelled(self):
        self.client.check_cancelled()
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise self.timeout_error()

    def invoke(self, operation, command, timeout, callback):
        self.check_cancelled()
        self.timing.update(operation=operation, command=command, status="running",
                           message=("正在回收失败现场：" if self.timing["phase"] == "screen_evidence" else "采集前准备：") + operation)
        bounded = min(max(1, int(timeout)), 8)
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            grace = max(0, float(getattr(self.client, "operation_timeout_grace_seconds", 3)))
            bounded = min(bounded, int(remaining - grace))
            if bounded < 1:
                raise self.timeout_error()
        entry = {"operation": operation, "command": command, "timeout_seconds": bounded, "status": "running"}
        self.timing["operations"].append(entry)
        self.publish()  # Persist the current command before a potentially slow RPC.
        started = time.monotonic()
        try:
            result = callback(bounded)
            self.check_cancelled()
            entry["status"] = "completed"
            return result
        except Exception as exc:
            entry.update(status="cancelled" if type(exc).__name__ == "CaptureCancelled" else "failed",
                         reason=str(exc)[:500])
            self.timing["status"] = entry["status"]
            if (type(exc).__name__ != "CaptureCancelled" and self.deadline is not None
                    and time.monotonic() >= self.deadline):
                raise self.timeout_error() from exc
            raise
        finally:
            entry["duration_seconds"] = round(time.monotonic() - started, 3)
            self.publish()

    def shell(self, command, timeout=8, *, cleanup=False):
        operation = ("读取 UI 布局" if command.startswith("uitest dumpLayout") else
                     "电源键亮屏" if command == "uitest uiInput keyEvent Power" else
                     "上滑解锁" if command.startswith("uitest uiInput swipe ") else
                     "返回键准备界面" if command == "uitest uiInput keyEvent Back" else
                     "关闭图库并重置场景起点" if command.startswith("aa force-stop ") else
                     "重新启动图库" if command.startswith("aa start ") and self.timing["phase"] == "gallery_preparation" else
                     "拉起目标应用并核验解锁" if command.startswith("aa start ") else
                     "唤醒屏幕" if command == "power-shell wakeup" else
                     "读取电源状态" if command.startswith("power-shell dump") else
                     "读取系统锁屏状态" if command.startswith("hidumper -s 3704") else
                     "截取失败现场" if command.startswith("uitest screenCap") else
                     "等待界面稳定" if command.startswith("sleep ") else command[:160])
        return self.invoke(operation, command, timeout,
                           lambda bounded: self.client.shell(command, timeout=bounded, cleanup=False))

    def collect(self, remote, destination, *, timeout=8, cleanup=False):
        return self.invoke("回收界面布局" if str(remote).endswith(".json") else "回收失败截图", remote, timeout,
                           lambda bounded: self.client.collect(remote, destination, timeout=bounded, cleanup=False))

    def finish(self, status):
        stage = ("失败现场回收" if self.timing["phase"] == "screen_evidence" else
                 "图库起点准备" if self.timing["phase"] == "gallery_preparation" else "屏幕准备")
        ending = {"completed": "完成", "cancelled": "已取消", "timed_out": "预算已用尽"}.get(status, "失败，保留操作诊断")
        self.timing.update(status=status, message=stage + ending + "；最后操作：" + self.timing["operation"])
        self.publish()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def stage_script(client: DeviceClient, remote: str, text: str, *, directory_ready: bool = False) -> None:
    # Size the escaped UTF-8 command, not its character count. 12 KiB leaves
    # room for the completion marker in the existing 15 KiB transport contract.
    if not directory_ready:
        client.shell("mkdir -p " + shlex.quote(str(PurePosixPath(remote).parent)))
    position = 0
    while position < len(text) or position == 0:
        suffix = (" > " if position == 0 else " >> ") + shlex.quote(remote)
        low, high = 0, min(len(text) - position, 12 * 1024)
        while low < high:
            middle = (low + high + 1) // 2
            command = "printf %s " + shlex.quote(text[position:position + middle]) + suffix
            if len(command.encode("utf-8")) <= 12 * 1024:
                low = middle
            else:
                high = middle - 1
        if low == 0 and text:
            raise ValueError("Device script path exceeds the Bridge command budget")
        client.shell("printf %s " + shlex.quote(text[position:position + low]) + suffix)
        position += low
        if not text:
            break


def pmu_capability_guard(period_us: int, *, timeout_seconds: float = 2, group: str | None = None,
                         bounded_wait: bool = False) -> list[str]:
    """Inspect the executable contract before requesting sub-20us sampling.

    The device only needs POSIX shell/sed, not Python or jq. No output from the
    capability executable becomes shell code; numeric range fields are bounded
    and format checked before integer comparisons. Legacy binaries remain usable
    at their historical request range, with their capability identity unknown.
    """
    ticks = max(1, min(100, round(timeout_seconds / .05)))
    probe = r'''pmu_probe=; pmu_probe_timer=; pmu_probe_group=0; pmu_caps_rc=0
pmu_probe_status=cancelled; pmu_probe_timed_out=false
pmu_probe_descendants() {
  for pmu_child in $(cat "/proc/$1/task/$1/children" 2>/dev/null); do
    case "$pmu_child" in ''|*[!0-9]*) continue;; esac
    printf '%s ' "$pmu_child"
    pmu_probe_descendants "$pmu_child"
  done
}
pmu_probe_signal() {
  if [ "$pmu_probe_group" = 1 ]; then kill -"$1" -"$pmu_probe" 2>/dev/null || true; fi
  for pmu_child in $pmu_probe_children; do kill -"$1" "$pmu_child" 2>/dev/null || true; done
  kill -"$1" "$pmu_probe" 2>/dev/null || true
}
stop_pmu_capability_probe() {
  if [ -n "$pmu_probe_timer" ]; then
    kill "$pmu_probe_timer" 2>/dev/null || true
    wait "$pmu_probe_timer" 2>/dev/null || true
    pmu_probe_timer=
  fi
  [ -n "$pmu_probe" ] || return 0
  pmu_probe_children=$(pmu_probe_descendants "$pmu_probe")
  pmu_probe_signal TERM
  if kill -0 "$pmu_probe" 2>/dev/null || [ "$pmu_probe_group" = 1 ] || [ -n "$pmu_probe_children" ]; then
    sleep 0.05 & pmu_probe_timer=$!
    wait "$pmu_probe_timer" 2>/dev/null || true
    pmu_probe_timer=
    pmu_probe_signal KILL
  fi
  wait "$pmu_probe" 2>/dev/null; pmu_caps_rc=$?
  printf '{"schema_version":"pmu_capability_probe/v1","status":"%s","exit_code":%s,"timed_out":%s,"timeout_seconds":%s,"process_group":%s}\n' "$pmu_probe_status" "$pmu_caps_rc" "$pmu_probe_timed_out" "$pmu_probe_timeout" "$pmu_probe_group" > "$out/pmu_capability_probe.json"
  if command -v forget_collector >/dev/null 2>&1; then forget_collector pmu_probe; fi
  pmu_probe=; pmu_probe_group=0
}
if command -v setsid >/dev/null 2>&1; then
  (ulimit -f 16 || exit 125; exec setsid "$pmu_bin" --capabilities) > "$out/pmu_capabilities.json" 2> "$out/pmu_capabilities.log" &
  pmu_probe=$!; pmu_probe_group=1
else
  (ulimit -f 16 || exit 125; exec "$pmu_bin" --capabilities) > "$out/pmu_capabilities.json" 2> "$out/pmu_capabilities.log" &
  pmu_probe=$!
fi
if command -v register_collector >/dev/null 2>&1; then register_collector pmu_probe "$pmu_probe"; fi
pmu_probe_attempts=0
while kill -0 "$pmu_probe" 2>/dev/null && [ "$pmu_probe_attempts" -lt "$pmu_probe_ticks" ]; do
  sleep 0.05 & pmu_probe_timer=$!
  wait "$pmu_probe_timer" 2>/dev/null || true
  pmu_probe_timer=
  pmu_probe_attempts=$((pmu_probe_attempts + 1))
done
if kill -0 "$pmu_probe" 2>/dev/null; then pmu_probe_status=timeout; pmu_probe_timed_out=true; else pmu_probe_status=exited; fi
stop_pmu_capability_probe'''.splitlines()
    if bounded_wait:
        # A full step script defines the exact-child bounded waiter before this
        # guard runs.  Standalone capability tests retain the small POSIX-only
        # fallback above, while production capture has no unbounded probe reap.
        probe = [
            ('    stop_child_bounded "$pmu_probe_timer" 10 pmu_probe_timer || true'
             if line == '    kill "$pmu_probe_timer" 2>/dev/null || true' else
             '' if line == '    wait "$pmu_probe_timer" 2>/dev/null || true' and
             index > 0 and probe[index - 1] == '    kill "$pmu_probe_timer" 2>/dev/null || true' else
             '  wait_child_bounded "$pmu_probe" 0 pmu_capability_probe; pmu_caps_rc=$?'
             if line == '  wait "$pmu_probe" 2>/dev/null; pmu_caps_rc=$?' else line)
            for index, line in enumerate(probe)
        ]
        probe = [line for line in probe if line]
    lines = [f'pmu_probe_ticks={ticks}; pmu_probe_timeout={ticks * .05:.2f}', *probe,
            '  pmu_min=0; pmu_max=0; pmu_caps_known=0; pmu_compatible=1; pmu_compat_status=legacy_unverified',
            # Some HarmonyOS device shells ship sed but no tr. Use the same
            # available parser for both compact and multiline capability JSON.
            '  pmu_caps_envelope=$(sed -e \':compact\' -e \'$!{\' -e N -e \'b compact\' -e \'}\' -e \'s/[[:space:]]//g\' "$out/pmu_capabilities.json"); pmu_caps_object=0',
            '  case "$pmu_caps_envelope" in \\{*\\}) pmu_caps_object=1;; esac',
            '  if [ "$pmu_caps_rc" = 0 ] && [ "$pmu_probe_timed_out" = false ] && [ "$pmu_caps_object" = 1 ] && grep -q \'"schema_version"[[:space:]]*:[[:space:]]*"gallery_thread_pmu_capabilities/v1"\' "$out/pmu_capabilities.json"; then',
            '    pmu_min=$(sed -n -e \'s/.*"period_us_min"[[:space:]]*:[[:space:]]*\\([0-9][0-9]*\\)[[:space:]]*[,}].*/\\1/p\' -e \'s/.*"period_us_min"[[:space:]]*:[[:space:]]*\\([0-9][0-9]*\\)[[:space:]]*$/\\1/p\' "$out/pmu_capabilities.json" | head -n 1)',
            '    pmu_max=$(sed -n -e \'s/.*"period_us_max"[[:space:]]*:[[:space:]]*\\([0-9][0-9]*\\)[[:space:]]*[,}].*/\\1/p\' -e \'s/.*"period_us_max"[[:space:]]*:[[:space:]]*\\([0-9][0-9]*\\)[[:space:]]*$/\\1/p\' "$out/pmu_capabilities.json" | head -n 1)',
            '    case "$pmu_min:$pmu_max" in *[!0-9:]*|:*|*:|0*|*:0*) pmu_min=0; pmu_max=0;; esac',
            '    if [ "${#pmu_min}" -le 5 ] && [ "${#pmu_max}" -le 5 ] && [ "$pmu_min" -gt 0 ] && [ "$pmu_max" -ge "$pmu_min" ]; then pmu_caps_known=1; else pmu_min=0; pmu_max=0; fi',
            '  fi',
            '  if [ "$pmu_caps_known" = 1 ]; then',
            f'    if [ {period_us} -lt "$pmu_min" ] || [ {period_us} -gt "$pmu_max" ]; then pmu_compatible=0; pmu_compat_status=period_unsupported; else pmu_compat_status=supported; fi',
            f'  elif [ {period_us} -lt 20 ]; then pmu_compatible=0; pmu_compat_status=upgrade_required; fi',
            ]
    if group in {"auto", "all"}:
        lines += ['  pmu_group_modes=$(printf \'%s\\n\' "$pmu_caps_envelope" | sed -n \'s/.*"event_groups"[[:space:]]*:[[:space:]]*\\[\\([^]]*\\)\\].*/\\1/p\')',
                  f'  case "$pmu_group_modes" in *\'"{group}"\'*) ;; *) pmu_compatible=0; pmu_compat_status=multi_mode_upgrade_required;; esac']
    lines += [f'  printf \'{{"schema_version":"pmu_sampler_compatibility/v1","requested_period_us":{period_us},"status":"%s","reported_min_period_us":%s,"reported_max_period_us":%s,"capabilities_exit_code":%s,"capabilities_timed_out":%s}}\\n\' "$pmu_compat_status" "$pmu_min" "$pmu_max" "$pmu_caps_rc" "$pmu_probe_timed_out" > "$out/pmu_compatibility.json"']
    return lines


def hitrace_begin_command(profile: dict[str, Any]) -> list[str]:
    """HiTrace categories are individual argv entries, never a CSV argument."""
    categories = profile["trace_categories"]
    if not isinstance(categories, list) or not categories or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in categories):
        raise ValueError("HiTrace requires individual category names, for example ['sched', 'ace', 'app']")
    return ["hitrace", "--trace_begin", "--trace_clock", "mono", "-b", str(profile["trace_buffer_kb"]), *categories]


def build_step_script(step: dict[str, Any], actions: list[dict[str, Any]], profile: dict[str, Any],
                      capture: dict[str, Any], remote: str, trace_root: str, pid: str,
                      *, session: CollectorSession | None = None) -> str:
    selected = step["step_id"] in capture["selected_step_ids"]
    collectors = capture["collectors"] if selected else []
    trace = "trace" in collectors
    pmu = capture["pmu"]
    pmu_preparation = pmu.get("preparation") if isinstance(pmu.get("preparation"), dict) else {}
    pmu_execution_allowed = pmu_preparation.get("allow_execution") is not False
    number, slug = step["ordinal"], step["slug"]
    hold = profile["hold_seconds"] if selected and step["operation"] != "home" else profile["prepare_settle_seconds"] if not selected else 0
    # Trace serialization has its own bounded window.  Do not derive it from
    # the action timeout: a short action may still need to flush a large trace
    # buffer.  Scale beyond the first 64 MiB while retaining a short stuck-
    # process bound for ordinary buffers; the 1 GiB plan maximum gets 18 s.
    trace_buffer_chunks = max(1, (int(profile.get("trace_buffer_kb", 1024))
                                  + TRACE_FINISH_BUFFER_CHUNK_KB - 1)
                                 // TRACE_FINISH_BUFFER_CHUNK_KB)
    trace_finish_ticks = min(
        TRACE_FINISH_MAX_TICKS,
        TRACE_FINISH_BASE_TICKS
        + max(0, trace_buffer_chunks - 1) * TRACE_FINISH_BUFFER_EXTRA_TICKS,
    )
    ownership = (step_shell_prelude(session.run_id, remote, session.token, lock_path=session.lock_path) if session else [
        # Unit/offline scripts do not own a recoverable CollectorSession, but
        # keep the generated shell self-contained when the trace heartbeat
        # formats its diagnostic marker.
        "session_token=unmanaged",
        "register_collector() { :; }; forget_collector() { :; }; mark_trace_intent() { :; }; mark_trace_owned() { :; }; mark_trace_finished() { :; }"])
    # Trace and HiLog clocks differ; clock brackets are retained as measurements,
    # never claimed to be a calibrated clock transformation by themselves.
    lines = ["#!/system/bin/sh", "set -u", f"out={shlex.quote(remote)}", f"root={shlex.quote(trace_root)}",
             'mkdir -p "$out"', 'echo "$$" > "$out/owner.pid"',
             *ownership,
             "owned=0; logger=; sampler=; watchdog=; action_child=; pmu_probe=; pmu_probe_timer=; trace_heartbeat=; depth=0; status=1; pmu_state=not_requested; cleanup_incomplete=0; cleanup_child_alive=0",
             'marker="$root/trace_marker"',
             'begin() { if [ "$owned" = 1 ]; then echo "B|$$|$1" > "$marker"; depth=$((depth + 1)); fi; }',
             'end() { if [ "$owned" = 1 ] && [ "$depth" -gt 0 ]; then echo "E|$$" > "$marker"; depth=$((depth - 1)); fi; }',
             'clock_bracket() { date +%s; cat /proc/uptime; date +%s; }',
             # All collector PIDs below are exact children registered by this
             # script.  A bounded TERM -> KILL -> reap sequence prevents a
             # blocked PMU/HiLog/HiTrace child from consuming the coarser host
             # operation deadline.  /proc distinguishes an exited zombie from
             # a still-running child before the final (therefore immediate)
             # wait/reap.
             'child_running() {',
             '  case "$1" in \'\'|*[!0-9]*) return 1;; esac',
             '  child_stat=$(sed \'s/^.*) //\' "/proc/$1/stat" 2>/dev/null) || child_stat=$(ps -o stat= -p "$1" 2>/dev/null)',
             '  case "$child_stat" in Z|Z\\ *) return 1;; esac',
             '  kill -0 "$1" 2>/dev/null',
             '}',
             'wait_child_bounded() {',
             '  cleanup_child=$1; cleanup_grace=$2; cleanup_label=$3; cleanup_child_alive=0; cleanup_attempts=0',
             '  printf \'%s\\n\' "$cleanup_label" > "$out/cleanup_phase.txt"',
             '  while child_running "$cleanup_child" && [ "$cleanup_attempts" -lt "$cleanup_grace" ]; do',
             f'    sleep {CHILD_POLL_SECONDS}; cleanup_attempts=$((cleanup_attempts + 1))',
             '  done',
             '  if child_running "$cleanup_child"; then',
             '    printf \'%s TERM after %s polls\\n\' "$cleanup_label" "$cleanup_attempts" >> "$out/cleanup_events.log"',
             '    kill -TERM "$cleanup_child" 2>/dev/null || true; cleanup_attempts=0',
             f'    while child_running "$cleanup_child" && [ "$cleanup_attempts" -lt {CHILD_TERM_GRACE_TICKS} ]; do sleep {CHILD_POLL_SECONDS}; cleanup_attempts=$((cleanup_attempts + 1)); done',
             '  fi',
             '  if child_running "$cleanup_child"; then',
             '    printf \'%s KILL after TERM grace\\n\' "$cleanup_label" >> "$out/cleanup_events.log"',
             '    kill -KILL "$cleanup_child" 2>/dev/null || true; cleanup_attempts=0',
             f'    while child_running "$cleanup_child" && [ "$cleanup_attempts" -lt {CHILD_KILL_GRACE_TICKS} ]; do sleep {CHILD_POLL_SECONDS}; cleanup_attempts=$((cleanup_attempts + 1)); done',
             '  fi',
             '  if child_running "$cleanup_child"; then',
             '    cleanup_child_alive=1; cleanup_incomplete=1; printf \'%s STILL_RUNNING\\n\' "$cleanup_label" >> "$out/cleanup_events.log"; return 124',
             '  fi',
             '  wait "$cleanup_child" 2>/dev/null; return $?',
             '}',
             'stop_child_bounded() { kill -TERM "$1" 2>/dev/null || true; wait_child_bounded "$1" "$2" "$3"; }',
             'cleanup() {', '  trap - EXIT INT TERM HUP',
             '  echo cleanup_start > "$out/cleanup_phase.txt"', '  : > "$out/cleanup_events.log"',
             # Stop the watchdog shell before its sleep child. Otherwise a
             # reaped timer can let the watchdog signal the parent during its
             # own cleanup trap.
             '  [ -z "$watchdog" ] || kill -TERM "$watchdog" 2>/dev/null || true',
             '  if [ -r "$out/timer.pid" ]; then timer_child=$(cat "$out/timer.pid"); stop_child_bounded "$timer_child" 10 watchdog_timer || true; [ "$cleanup_child_alive" = 1 ] || forget_collector timer; fi',
             '  if [ -n "$watchdog" ]; then wait_child_bounded "$watchdog" 10 watchdog || true; [ "$cleanup_child_alive" = 1 ] || forget_collector watchdog; fi',
             '  if [ -n "$pmu_probe" ]; then stop_pmu_capability_probe; fi',
             '  if [ -n "$action_child" ]; then stop_child_bounded "$action_child" 20 action || true; [ "$cleanup_child_alive" = 1 ] || forget_collector action; fi',
             '  if [ -n "$trace_heartbeat" ]; then stop_child_bounded "$trace_heartbeat" 20 trace_heartbeat || true; [ "$cleanup_child_alive" = 1 ] || forget_collector trace_heartbeat; fi',
             '  if [ -n "$sampler" ]; then',
             '    stop_child_bounded "$sampler" 20 pmu_sampler; sample_rc=$?',
             '    if [ "$pmu_state" = started ] && [ -s "$out/pmu.csv" ] && [ -s "$out/pmu.json" ]; then pmu_state=collected; else pmu_state=failed; fi',
             '    echo "$sample_rc" > "$out/pmu_exit_code.txt"', '    [ "$cleanup_child_alive" = 1 ] || forget_collector sampler', '  fi',
             '  echo "$pmu_state" > "$out/pmu_status.txt"',
             '  if [ "$owned" = 1 ]; then',
             '    if [ "$status" != 0 ]; then echo "C|$$|GALLERY_STEP_FAILED|1" > "$marker"; fi',
             '    while [ "$depth" -gt 0 ]; do end; done',
             '    cat "$root/trace_clock" > "$out/trace_clock.txt" 2>&1',
             '    for stats in "$root"/per_cpu/cpu*/stats; do [ ! -r "$stats" ] || { echo "$stats"; cat "$stats"; }; done > "$out/buffer_stats.txt" 2>&1',
             '    hitrace --trace_finish -o "$out/trace.ftrace" > "$out/hitrace_finish.log" 2>&1 &',
             '    finisher=$!; register_collector trace_finisher "$finisher"',
             f'    wait_child_bounded "$finisher" {trace_finish_ticks} trace_finisher || status=1',
             '    [ "$cleanup_child_alive" = 1 ] || forget_collector trace_finisher',
             '    if [ "$(cat "$root/tracing_on")" = 0 ]; then mark_trace_finished; else status=1; fi',
             '    case "$(cat "$out/hitrace_finish.log")" in *[Ff]ail*|*ERROR*|*Error*) status=1;; esac',
             '    [ -s "$out/trace.ftrace" ] || status=1', '  fi',
             '  if [ -n "$logger" ]; then',
             '    if kill -0 "$logger" 2>/dev/null; then echo stopped > "$out/hilog_status.txt";',
             '    else echo exited_early > "$out/hilog_status.txt"; fi',
             '    stop_child_bounded "$logger" 20 hilog || true', '    [ "$cleanup_child_alive" = 1 ] || forget_collector logger', '  fi',
             '  clock_bracket > "$out/clock_after.txt"',
             '  if [ "$cleanup_incomplete" = 1 ]; then status=1; echo cleanup_incomplete > "$out/cleanup_phase.txt"; else echo cleanup_complete > "$out/cleanup_phase.txt"; fi',
             '  echo "$status" > "$out/status.txt"', '  rm -f "$out/owner.pid"', '  forget_collector owner', '  exit "$status"', '}',
             'trap cleanup EXIT', "trap 'status=130; exit 130' INT TERM HUP",
             'parent=$$',
             f'(sleep {profile["step_timeout_seconds"]} & timer=$!; echo "$timer" > "$out/timer.pid"; register_collector timer "$timer"; wait "$timer"; forget_collector timer; kill -TERM "$parent" 2>/dev/null) >/dev/null 2>&1 &',
             'watchdog=$!; register_collector watchdog "$watchdog"', 'clock_bracket > "$out/clock_before.txt"',
             'command_action() { name=$1; shift; begin "$name"; "$@" > "$out/action_current.log" 2>&1 & action_child=$!;',
             '  register_collector action "$action_child";',
             '  wait "$action_child"; action_status=$?; action_child=; forget_collector action; cat "$out/action_current.log" >> "$out/actions.log";',
             '  action_errors=$(sed \'/^[[:space:]]*No Error[[:space:]]*$/d\' "$out/action_current.log");',
             '  case "$action_errors" in *[Ff]ail*|*ERROR*|*Error*|*"Missing parameter"*|*"not found"*) action_status=1;; esac;',
             '  end; if [ "$action_status" != 0 ]; then echo "Action failed: $name"; exit 1; fi; }']
    if "pmu" in collectors:
        lines += ['pmu_state=unavailable; pmu_compatible=0']
        if not pmu_execution_allowed:
            receipt = json.dumps(pmu_preparation, ensure_ascii=False)
            message = str(pmu_preparation.get("code", "PMU_PREPARATION_FAILED")) + ": " + str(pmu_preparation.get("message", "PMU 采集器准备失败，未执行采样器。"))
            lines += ["printf '%s\\n' " + shlex.quote(receipt) + ' > "$out/pmu_preparation.json"',
                      "printf '%s\\n' " + shlex.quote(message) + ' > "$out/pmu.log"']
        else:
            lines += [f"pmu_bin={shlex.quote(pmu['binary_path'])}"]
        if pmu_execution_allowed and pid.isdigit():
            lines += ['if [ -x "$pmu_bin" ]; then',
                      '  sha256sum "$pmu_bin" > "$out/pmu_binary_identity.txt" 2>&1 || true',
                      *pmu_capability_guard(pmu["period_us"], group=pmu["group"], bounded_wait=True), 'fi']
    if trace:
        lines += ['test -w "$marker" || exit 1',
                  'if [ "$(cat "$root/tracing_on")" != 0 ]; then echo "Existing trace active; not replacing it"; exit 1; fi',
                  'mark_trace_intent "$root" || exit 1',
                  shlex.join(hitrace_begin_command(profile)) + ' > "$out/hitrace_begin.log" 2>&1',
                  'begin_status=$?',
                  'if [ "$begin_status" != 0 ]; then cat "$out/hitrace_begin.log"; exit 1; fi',
                  'case "$(cat "$out/hitrace_begin.log")" in *[Ff]ail*|*ERROR*|*Error*) cat "$out/hitrace_begin.log"; exit 1;; esac',
                  'test "$(cat "$root/tracing_on")" = 1 || exit 1',
                  'owned=1',
                  'mark_trace_owned "$root" || exit 1',
                  '( trap \'printf "C|%s|HETERO_TRACE_OWNER_%s|1\\n" "$$" "$session_token" > "$marker" 2>/dev/null || true\' EXIT TERM HUP;',
                  '  while [ -f "$out/trace.owner" ] && [ "$(cat "$root/tracing_on" 2>/dev/null)" = 1 ]; do',
                  '    printf "C|%s|HETERO_TRACE_OWNER_%s|1\\n" "$$" "$session_token" > "$marker" 2>/dev/null || true; sleep 1; done) >/dev/null 2>&1 &',
                  'trace_heartbeat=$!; register_collector trace_heartbeat "$trace_heartbeat"']
    if "hilog" in collectors:
        command = ["hilog", *(["-P", pid] if pid.isdigit() else []), "-v", "epoch", "-v", "nsec"]
        lines += ['if command -v hilog >/dev/null 2>&1; then',
                  "  " + shlex.join(command) + ' > "$out/hilog.log" 2>&1 &', '  logger=$!; register_collector logger "$logger"',
                  'else echo unavailable > "$out/hilog_status.txt"; fi']
    if "pmu" in collectors and pmu_execution_allowed:
        if pid.isdigit():
            lines += ['if [ -x "$pmu_bin" ]; then', '  if [ "$pmu_compatible" = 1 ]; then',
                      '  "$pmu_bin" --clock > "$out/clock_before.json" 2> "$out/pmu_clock_error.log" || true',
                      '  "$pmu_bin" ' + shlex.join([pid, str(pmu["period_us"]), str(pmu["duration_ms"]), str(pmu["reader_cpu"]), pmu["group"]]) + ' "$out/pmu.csv" "$out/pmu.json" "$out/pmu.ready" normal > "$out/pmu.log" 2>&1 &',
                      '  sampler=$!; register_collector sampler "$sampler"; attempts=0',
                      '  while [ ! -s "$out/pmu.ready" ] && kill -0 "$sampler" 2>/dev/null && [ "$attempts" -lt 100 ]; do attempts=$((attempts + 1)); sleep 0.01; done',
                      '  if [ -s "$out/pmu.ready" ]; then pmu_state=started; else pmu_state=failed; fi',
                      '  else echo "PMU request rejected before sampling: $pmu_compat_status; update sampler or use its supported period range" > "$out/pmu.log"; fi',
                      'else echo "Preinstalled PMU sampler unavailable" > "$out/pmu.log"; fi']
        else:
            lines += ['echo "No unique pre-action target PID; PMU not started" > "$out/pmu.log"']
    if selected:
        lines += [f"sleep {profile['guard_seconds']}", f"begin GALLERY_STEP_{number:02d}_{slug}", f"begin GALLERY_ACTION_{number:02d}"]
    for action in actions:
        lines.append("command_action " + shlex.quote(action["name"]) + " " + shlex.join([str(value) for value in action["command"]]))
    if selected:
        lines += ["end", f"begin GALLERY_HOLD_{number:02d}", f"sleep {hold}", "end", "end", f"sleep {profile['guard_seconds']}"]
    else:
        lines += [f"sleep {hold}"]
    if "pmu" in collectors and pmu_execution_allowed and pid.isdigit():
        lines += ['if [ -x "$pmu_bin" ] && [ "$pmu_compatible" = 1 ]; then "$pmu_bin" --clock > "$out/clock_after.json" 2>> "$out/pmu_clock_error.log" || true; fi']
    lines += ['status=0', 'exit 0', '']
    return "\n".join(lines)


def validate_trace(path: Path, step: dict[str, Any], hold_seconds: float, buffer_stats: str = "") -> dict[str, Any]:
    number = step["ordinal"]
    expected = {f"GALLERY_STEP_{number:02d}_{step['slug']}", f"GALLERY_ACTION_{number:02d}", f"GALLERY_HOLD_{number:02d}"}
    stacks: dict[str, list[tuple[str, float]]] = {}
    spans: list[dict[str, Any]] = []
    sched = loss = 0
    failed = False
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            loss += int("LOST" in line or "lost events" in line)
            match = re.search(r"\s(\d+\.\d+):\s+(\w+):\s*(.*)", line)
            if not match:
                continue
            stamp, event, payload = float(match[1]), match[2], match[3]
            sched += event == "sched_switch"
            if event != "tracing_mark_write":
                continue
            failed |= "GALLERY_STEP_FAILED|1" in payload
            begin, end = re.match(r"B\|(\d+)\|(.+)", payload), re.match(r"E\|(\d+)", payload)
            if begin and (begin[2] in expected or begin[1] in stacks):
                stacks.setdefault(begin[1], []).append((begin[2], stamp))
            elif end and stacks.get(end[1]):
                name, start = stacks[end[1]].pop()
                spans.append({"name": name, "start_s": start, "end_s": stamp, "duration_ms": (stamp - start) * 1000})
    observed = {span["name"] for span in spans}
    overruns = [int(value) for value in re.findall(r"(?m)^\s*(?:overrun|commit overrun|dropped events)\s*:\s*(\d+)", buffer_stats)]
    hold = next((s["duration_ms"] for s in spans if s["name"] == f"GALLERY_HOLD_{number:02d}"), -1)
    unique = all(sum(s["name"] == name for s in spans) == 1 for name in expected)
    valid = expected <= observed and unique and not any(stacks.values()) and sched > 0 and not failed and loss == 0 and not any(overruns) and hold >= hold_seconds * 1000 - 10
    return {"valid": valid, "expected_markers": sorted(expected), "missing_markers": sorted(expected - observed), "unique_markers": unique,
            "open_markers": sum(map(len, stacks.values())), "sched_switch_count": sched, "loss_text_lines": loss,
            "buffer_overruns": sum(overruns), "buffer_stats_available": bool(overruns), "step_failed_marker": failed, "spans": spans,
            "limitation": "Action markers bound command execution, not first-frame presentation; missing buffer statistics do not prove zero loss."}


def _resolve_actions(step: dict[str, Any], tree: dict[str, Any], profile: dict[str, Any], cache: dict[str, Any]) -> list[dict[str, Any]]:
    operation = step["operation"]
    if operation in {"launch", "click", "date_mode", "home", "albums", "zoom", "scroll"}:
        return build_actions((step["ordinal"], step["slug"], step["title"], operation, step.get("target"), step.get("expected_targets", [])), tree, profile, cache)
    if operation == "key":
        return [{"name": "KEY", "command": ["uitest", "uiInput", "keyEvent", step["key"]]}]
    if operation == "wait":
        return [{"name": "WAIT", "command": ["sleep", step["seconds"]]}]
    if operation == "swipe":
        sync_screen_geometry(tree, profile)
        points = point_from_ratio(step["points"][:2], profile["screen_size"]) + point_from_ratio(step["points"][2:], profile["screen_size"])
        return [{"name": "SWIPE", "command": ["uitest", "uiInput", "swipe", *points, step.get("velocity", 1000)]}]
    raise ValueError(f"Step has no executable operation: {operation}")


def prepare_gallery_launch(tree: dict[str, Any], profile: dict[str, Any], client: DeviceClient,
                           record: dict[str, Any], read_page: Callable[[int], dict[str, Any]],
                           *, captured: bool, max_back_steps: int = 4, reset: bool = False) -> tuple[dict[str, Any], bool]:
    """Prepare Gallery launch state; automatic entry may restart its session."""
    state = verify_page_available(tree, profile)
    record["initial_page_state"] = state
    if profile.get("bundle") not in GALLERY_BUNDLES:
        return tree, False
    if reset:
        return reset_gallery_launch(tree, profile, client, record, read_page,
                                    captured=captured, max_back_steps=max_back_steps)
    if not state.startswith("gallery_"):
        return tree, False
    preparation = record.setdefault("outside_capture_preparation", [])
    if captured:
        if state not in {"gallery_home", "gallery_browser"}:
            raise CapturePreparationError("SCENARIO_GALLERY_PREPARATION_REQUIRED", "启动步骤被选中采集，但图库当前页面需要先完成处理。",
                                          "请先完成或退出编辑及确认弹窗，返回图库主页或照片浏览页后重试。")
        client.check_cancelled()
        command = ["uitest", "uiInput", "keyEvent", "Home"]
        preparation.append({"action": "return_to_desktop_for_launch", "command": command, "page_state_before": state,
                            "process_policy": "kept_running"})
        client.shell(shlex.join(command))
        for attempt in range(1, 4):
            client.check_cancelled()
            client.shell("sleep 0.35")
            tree = read_page(attempt)
            state = verify_page_available(tree, profile)
            if state == "desktop":
                record["launch_preparation"] = "verified_desktop_existing_app_preserved"
                return tree, False
        raise CapturePreparationError("SCENARIO_LAUNCH_REQUIRES_HOME", "自动返回桌面后，尚未确认可用于启动测量的桌面界面。",
                                      "请返回已解锁的桌面后重试；图库进程已保留，本次尚未执行被测启动。")
    for index in range(max_back_steps + 1):
        if state == "gallery_home":
            preparation.append({"action": "reuse_gallery_home", "page_state": state})
            return tree, True
        if state != "gallery_browser" or index == max_back_steps:
            raise CapturePreparationError("SCENARIO_GALLERY_PREPARATION_REQUIRED", "图库当前页面无法自动准备为场景起点。",
                                          "请先完成或退出编辑及确认弹窗，返回含“照片 / 相册”的图库主页后重试；若已停在待采集步骤前，也可选择“已准备”。")
        selected = select_node(tree, {"alternatives": [
            {"id": "AccessibilityComponent_BrowserBack"}, {"id": "PhotoBrowserActionBarButtonBack"},
        ]}, profile)
        client.check_cancelled()
        command = ["uitest", "uiInput", "click", *map(str, selected["point"])]
        client.shell(shlex.join(command))
        preparation.append({"action": "return_from_photo_browser", "command": command})
        client.shell("sleep " + str(profile["navigation"]["back_gap_seconds"]))
        tree = read_page(index + 1)
        state = verify_page_available(tree, profile)
    raise AssertionError("Preparation loop did not terminate")


def wait_for_step_ready(step: dict[str, Any], profile: dict[str, Any], client: DeviceClient,
                        read_page: Callable[[int], dict[str, Any]], *, timeout_seconds: float = 5,
                        poll_seconds: float = 0.25) -> dict[str, Any]:
    """Poll semantic postconditions after collectors stop, without replaying actions."""
    deadline = time.monotonic() + max(0, timeout_seconds)
    attempt = 0
    while True:
        client.check_cancelled()
        tree = read_page(attempt)
        verify_page_available(tree, profile, phase="postcondition")
        try:
            if step["operation"] == "home":
                if verify_screen(tree, profile, gallery=False):
                    raise ValueError("Home action completed but Gallery remains visible")
            else:
                verify_screen(tree, profile, gallery=step["operation"] not in {"key", "wait"})
                for target in step.get("expected_targets", []):
                    selector = profile["targets"].get(target)
                    if selector and "point" in selector:
                        raise ValueError("Postcondition needs a semantic selector, not a coordinate: " + target)
                    select_node(tree, selector, profile)
            return tree
        except ValueError as exc:
            if time.monotonic() >= deadline:
                raise CapturePreparationError("SCENARIO_PAGE_NOT_READY", "操作完成后，页面未达到预期状态。",
                    "请确认手机停在该步骤预期页面，并检查控件选择器；原始原因：" + str(exc), phase="postcondition") from exc
        client.shell("sleep " + str(min(poll_seconds, max(0, deadline - time.monotonic()))))
        attempt += 1


def validate_pmu(directory: Path, *, target_tid: int | None = None, step_window: dict[str, Any] | None = None,
                 collector_state: str | None = None) -> dict[str, Any]:
    """Separate cumulative zero baselines, read errors and sampling resolution.

    A successful zero/zero read before the first active counter is a valid
    baseline for a thread-scoped group, not a failed read. At least two positive
    counter samples are still required; a never-running event is not success.
    """
    diagnostics: list[dict[str, str]] = []
    compatibility: dict[str, Any] = {}
    capabilities: dict[str, Any] = {}
    preparation: dict[str, Any] = {}

    def note(code: str, message: str, suggestion: str = "", severity: str = "warning") -> None:
        diagnostics.append({"code": code, "severity": severity, "message": message, "suggestion": suggestion})

    def fail(code: str, message: str, suggestion: str = "") -> dict[str, Any]:
        note(code, message, suggestion, "error")
        return {"schema_version": "pmu_capture_validation/v2", "validator_version": "2.0.0", "valid": False, "quality_complete": False,
                "reason": message, "diagnostics": diagnostics, "sampler_compatibility": compatibility, "sampler_capabilities": capabilities,
                "sampler_preparation": preparation}

    try:
        preparation_path = directory / "pmu_preparation.json"
        if preparation_path.is_file():
            value = json.loads(preparation_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return fail("PMU_PREPARATION_INVALID", "PMU 准备记录格式错误，无法确认采样器执行状态。")
            preparation = value
            if preparation.get("allow_execution") is False:
                code = preparation.get("code")
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", code):
                    code = "PMU_PREPARATION_FAILED"
                return fail(code, str(preparation.get("message") or "PMU 采集器准备失败，本轮未执行采样器。")[:2000],
                            "查看 PMU 准备记录，修复资产部署、能力或设备连接问题后重试；本轮 Trace/HiLog 独立采集。")
        log_path, metadata_path = directory / "pmu.log", directory / "pmu.json"
        log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        compatibility_path, capabilities_path = directory / "pmu_compatibility.json", directory / "pmu_capabilities.json"
        if compatibility_path.is_file():
            value = json.loads(compatibility_path.read_text(encoding="utf-8"))
            if isinstance(value, dict): compatibility = value
        if capabilities_path.is_file():
            try:
                value = json.loads(capabilities_path.read_text(encoding="utf-8"))
                if isinstance(value, dict): capabilities = value
            except (ValueError, TypeError): pass  # Legacy CLI usage output is preserved as evidence.
        if compatibility.get("status") == "upgrade_required":
            return fail("PMU_SAMPLER_UPGRADE_REQUIRED", f"请求{compatibility.get('requested_period_us')} μs，但设备采样器未报告支持该精度的能力，PMU未启动。",
                        "更新设备PMU采样器至支持10 μs与--capabilities的版本，或使用现有采样器支持的间隔；平台保留Trace和HiLog。")
        if compatibility.get("status") == "period_unsupported":
            return fail("PMU_PERIOD_UNSUPPORTED", f"请求{compatibility.get('requested_period_us')} μs，设备采样器报告范围为{compatibility.get('reported_min_period_us')}–{compatibility.get('reported_max_period_us')} μs，PMU未启动。",
                        "选择该二进制支持的间隔，或更新设备采样器并保留版本与构建来源。")
        if compatibility.get("status") == "legacy_unverified":
            note("PMU_CAPABILITIES_UNKNOWN", "现有采样器未提供能力版本，按历史参数合同执行并保留二进制SHA-256。", "查询能力需更新支持--capabilities的采样器。", "info")
        if compatibility.get("status") == "multi_mode_upgrade_required":
            return fail("PMU_MULTI_GROUP_UPGRADE_REQUIRED", "设备采样器未声明支持自动多事件组，本次未启动 PMU。",
                        "使用平台默认采样器路径自动部署新版，或更新自定义路径的采样器。")
        if not metadata_path.is_file():
            if "No unique pre-action target PID" in log:
                return fail("PMU_TARGET_NOT_RESOLVED", "采集开始前未识别到唯一目标进程，PMU未启动。", "先打开目标应用并保持运行，再采集指定操作。")
            if "Preinstalled PMU sampler unavailable" in log:
                return fail("PMU_SAMPLER_UNAVAILABLE", "设备上未找到可执行的PMU采样器。", "检查采样器路径、文件部署与执行权限。")
            # ``pmu_state`` starts as ``unavailable`` and is only changed when
            # the sampler process is launched.  A preceding collector can
            # still abort the step after the sampler binary and capabilities
            # were successfully verified (for example, HiTrace begin can
            # reject an already-active trace).  In that case, treating the
            # initial state as evidence that the binary is missing sends the
            # user to repair the wrong component.  The identity receipt is
            # written only inside the executable-path branch, and a supported
            # compatibility receipt proves the verified binary was invoked.
            identity_path = directory / "pmu_binary_identity.txt"
            if (collector_state == "unavailable" and identity_path.is_file()
                    and compatibility.get("status") in {"supported", "legacy_unverified"}):
                return fail("PMU_NOT_STARTED",
                            "PMU采样器已经核验，但步骤在采样器启动前失败，本轮未产生PMU数据。",
                            "先处理本步骤的前置采集器或操作失败；采样器路径与部署无需重复修复。")
            if collector_state == "unavailable":
                return fail("PMU_SAMPLER_UNAVAILABLE", "设备上未找到可执行的PMU采样器。", "检查采样器路径、文件部署与执行权限。")
            exit_path = directory / "pmu_exit_code.txt"
            if exit_path.is_file() and exit_path.read_text().strip() == "2":
                return fail("PMU_PARAMETERS_REJECTED", "设备采样器拒绝当前参数，未产出PMU数据。", "检查设备二进制版本、支持的采样间隔和pmu.log参数说明。")
            return fail("PMU_METADATA_MISSING", "未返回PMU元数据，无法确认采样状态。", "查看该步骤pmu.log和采样器退出码。")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict): return fail("PMU_METADATA_INVALID", "PMU元数据不是JSON对象。")
        if metadata.get("schema_version") == "thread_pmu_capture/v2":
            from .pmu_validation import validate_multi_pmu
            result = validate_multi_pmu(directory, metadata, target_tid=target_tid, step_window=step_window)
            result.update(sampler_compatibility=compatibility, sampler_capabilities=capabilities,
                          sampler_preparation=preparation)
            result["diagnostics"] = diagnostics + result["diagnostics"]
            return result
        if metadata.get("open_error"):
            error = metadata["open_error"]
            if error in (1, 13):
                result = fail("PMU_PERMISSION_DENIED", "设备拒绝perf_event_open，PMU计数器访问权限不足。", "检查设备调试权限与perf_event访问策略。")
            elif error == 3:
                result = fail("PMU_TARGET_EXITED", "打开PMU事件时目标线程已不存在。", "确认应用已启动且PID稳定后重新采集。")
            elif error in (2, 19, 22, 95):
                result = fail("PMU_EVENT_UNSUPPORTED", "设备无法打开当前PMU事件组。", "核对该芯片事件支持和内核映射，选择设备支持的事件组。")
            else:
                result = fail("PMU_OPEN_FAILED", f"PMU事件打开失败，设备返回errno={error}。", "查看pmu.log中的perf_event_open错误。")
            result["metadata"] = metadata
            return result
        rows = good = baseline = read_errors = invalid_counters = unscheduled = multiplexed = 0
        first_ns = last_ns = previous_begin = None
        ordered = counters_monotonic = True
        active_seen = False
        prior_counters: dict[str, int] = {}
        intervals: list[int] = []
        running_ratios: list[float] = []
        with (directory / "pmu.csv").open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                rows += 1
                begin, end = int(row.get("read_begin_ns", "-1")), int(row.get("read_end_ns", "-1"))
                ordered &= 0 <= begin <= end and (last_ns is None or begin >= last_ns)
                if first_ns is None: first_ns = begin
                if previous_begin is not None and begin >= previous_begin: intervals.append(begin - previous_begin)
                previous_begin, last_ns = begin, end
                if int(row.get("read_error", "1")) != 0:
                    read_errors += 1
                    continue
                enabled, running = int(row.get("enabled_ns", "-1")), int(row.get("running_ns", "-1"))
                counters = {key: int(row[key]) for key in ("cycles", "instructions", "event2", "event3") if row.get(key) not in (None, "")}
                cumulative = {"enabled_ns": enabled, "running_ns": running, **counters}
                regressions = any(value < prior_counters.get(key, 0) for key, value in cumulative.items())
                counters_monotonic &= not regressions
                prior_counters = cumulative
                event_count_valid = (not metadata.get("event_names") or not row.get("event_count")
                                     or int(row["event_count"]) == len(metadata["event_names"]))
                if enabled < 0 or running < 0 or enabled < running or any(value < 0 for value in counters.values()) or regressions or not event_count_valid:
                    invalid_counters += 1
                elif enabled == running == 0 and all(value == 0 for value in counters.values()) and not active_seen:
                    baseline += 1
                elif running == 0:
                    unscheduled += 1
                else:
                    good += 1
                    active_seen = True
                    running_ratios.append(running / enabled)
                    multiplexed += running < enabled
        metadata_valid = (metadata.get("sample_count") == rows and metadata.get("clock") == "CLOCK_MONOTONIC"
                          and (target_tid is None or metadata.get("target_tid") == target_tid))
        valid = good >= 2 and ordered and counters_monotonic and metadata_valid
        coverage: dict[str, Any] = {"status": "not_measured", "first_sample_ns": first_ns, "last_sample_ns": last_ns}
        if step_window is not None:
            start_ns, end_ns = round(step_window["start_s"] * 1e9), round(step_window["end_s"] * 1e9)
            full = first_ns is not None and last_ns is not None and first_ns <= start_ns and last_ns >= end_ns
            coverage.update(status="full" if full else "partial", step_start_ns=start_ns, step_end_ns=end_ns,
                            missing_start_ms=max(0, (first_ns or start_ns) - start_ns) / 1e6,
                            missing_end_ms=max(0, end_ns - (last_ns or end_ns)) / 1e6)
            if not full: note("PMU_WINDOW_PARTIAL", "PMU采样时间未完整覆盖选中操作。", "检查PMU采样时长，覆盖操作、保持与前后保护时间。")
        if baseline: note("PMU_INACTIVE_BASELINE", f"{baseline}条初始零累计样本保留为基线，未计为读取失败。", "首次有效运行后的计数差分用于阶段特征。", "info")
        if read_errors: note("PMU_READ_ERRORS", f"{read_errors}条样本发生实际计数读取错误。", "查看pmu.csv的read_error及设备采样器日志。")
        if invalid_counters: note("PMU_COUNTER_INVALID", f"{invalid_counters}条样本存在异常计数、事件数量不符或累计回退。", "检查计数器是否重置、目标进程变化或数据文件完整性。")
        if unscheduled: note("PMU_COUNTER_NOT_RUNNING", f"{unscheduled}条样本事件已启用但尚未运行，无法形成有效计数。", "核验事件支持、计数器资源及目标线程执行状态。")
        if not metadata_valid: note("PMU_METADATA_MISMATCH", "PMU样本数、时钟或目标TID与采集记录不一致。", "保留同一次采集的CSV及元数据。", "error")
        if not ordered: note("PMU_SAMPLE_ORDER_INVALID", "PMU采样时间戳倒序或读取区间无效。", severity="error")
        if good < 2: note("PMU_ACTIVE_SAMPLES_INSUFFICIENT", "有效活动计数不足两条，尚不能形成可靠差分。", "确认目标线程在操作期间运行、事件可用，并延长目标操作采样窗口。", "error")
        if multiplexed: note("PMU_MULTIPLEXED", f"{multiplexed}条样本的计数器运行时间少于启用时间。", "阶段分析应使用enabled/running差分核验复用比例，不能直接当成完整硬件计数。")
        ordered_intervals = sorted(intervals)

        def percentile(fraction: float) -> float | None:
            if not ordered_intervals: return None
            position = (len(ordered_intervals) - 1) * fraction
            lower = int(position)
            return (ordered_intervals[lower] + (ordered_intervals[min(lower + 1, len(ordered_intervals) - 1)] - ordered_intervals[lower]) * (position - lower)) / 1000

        requested_ns = metadata.get("period_ns_requested")
        period = requested_ns / 1000 if isinstance(requested_ns, (int, float)) and requested_ns > 0 else None
        skipped = max(0, int(metadata.get("skipped_deadlines", 0)))
        maximum = max(intervals, default=0) / 1000 if intervals else None
        degraded = bool(skipped or period and maximum is not None and maximum > 2 * period)
        sampling = {"status": "degraded" if degraded else "observed" if intervals else "not_measured",
                    "requested_period_us": period, "observed_median_interval_us": percentile(.5),
                    "observed_p95_interval_us": percentile(.95), "maximum_interval_us": maximum,
                    "skipped_deadlines": skipped, "skipped_deadline_fraction": skipped / (rows + skipped) if rows + skipped else 0,
                    "running_ratio_min": min(running_ratios, default=None)}
        if degraded: note("PMU_SAMPLING_CADENCE_DEGRADED", f"实际采样节奏未达到请求间隔，跳过{skipped}个采样时点。", "可适当调大采样间隔，并依据实际间隔和最长空窗判断可分析的最小阶段；累计计数仍需按边界配对。")
        if metadata.get("affinity_error", 0): note("PMU_READER_AFFINITY_FAILED", "采样线程绑定指定CPU失败。", "检查reader_cpu是否在线以及设备亲和性权限。")
        exit_path = directory / "pmu_exit_code.txt"
        exit_code = int(exit_path.read_text().strip()) if exit_path.is_file() else None
        signal = metadata.get("stopped_signal", 0)
        expected_stop = exit_code == 0 and not signal or exit_code == 5 and signal == 15
        stop = {"exit_code": exit_code, "signal": signal, "expected_stop": expected_stop}
        if exit_code not in (None, 0) and not expected_stop: note("PMU_UNEXPECTED_STOP", f"PMU采样器非预期退出：退出码{exit_code}，信号{signal}。", "查看采样器日志和操作执行状态。")
        quality_complete = valid and read_errors == invalid_counters == unscheduled == 0 and coverage["status"] != "partial"
        if exit_code is not None and not expected_stop: quality_complete = False
        return {"schema_version": "pmu_capture_validation/v2", "validator_version": "2.0.0", "valid": valid, "quality_complete": quality_complete,
                "rows": rows, "usable_counter_rows": good, "inactive_baseline_rows": baseline,
                "read_error_rows": read_errors, "invalid_counter_rows": invalid_counters, "unscheduled_counter_rows": unscheduled,
                "ordered_samples": ordered, "counters_monotonic": counters_monotonic, "metadata_valid": metadata_valid,
                "coverage": coverage, "metadata": metadata, "sampling_quality": sampling, "stop": stop, "diagnostics": diagnostics,
                "sampler_compatibility": compatibility, "sampler_capabilities": capabilities,
                "scope": "target_main_thread", "limitation": "Collection completeness and sampling resolution are separate. Thread-scoped cumulative counters need delta, multiplexing, temporal attribution and overhead checks; zero initial baseline is not a read error."}
    except (OSError, ValueError, TypeError) as exc:
        return fail("PMU_DATA_INVALID", str(exc)[:1000], "检查同轮PMU CSV与元数据文件是否完整。")


def execute_plan(plan: dict[str, Any], client: DeviceClient, run_dir: Path, run_id: str,
                 progress: Callable[[dict[str, Any]], None] | None = None, *,
                 page_ready_timeout_seconds: float = 5,
                 terminal_run_ids: set[str] | None = None,
                 private_device: bool = False) -> dict[str, Any]:
    profile = json.loads(json.dumps(plan["profile"]))
    # A reused effective-profile export must not override this plan's reference.
    profile.pop("screen_size_reference", None)
    capture = plan["capture"]
    selected = set(capture["selected_step_ids"])
    last = max(index for index, step in enumerate(plan["steps"]) if step["step_id"] in selected)
    first = min(index for index, step in enumerate(plan["steps"]) if step["step_id"] in selected) if capture.get("preparation_mode") == "already_prepared" else 0
    steps = plan["steps"][first:last + 1]
    remote = f"/data/local/tmp/hetero_scenarios/{run_id}"
    manifest: dict[str, Any] = {"schema_version": "scenario_capture_manifest/v1", "runner_version": RUNNER_VERSION, "pmu_validation_schema": pmu_validation_schema(capture.get("pmu", {}).get("group")), "run_id": run_id,
        "plan_id": plan["artifact_id"], "title": str(plan.get("title") or "")[:300], "status": "running", "execution_policy": "through_last_selected", "preparation_mode": capture.get("preparation_mode", "auto"),
        "device_access_mode": "private" if private_device else "shared",
        "capture_step_ids": sorted(selected), "steps": [], "files": [], "quality": {"warnings": [], "errors": []},
        "provenance": plan.get("provenance", {}), "remote_root": remote, "cleanup": {"status": "not_started"},
        "device_preparation": {"policy": "step_preflight_with_layout_handoff", "strategy_version": "screen_preparation/v7",
                               "outside_capture_window": True, "screen_size_source": "current_device_layout"},
        "clock_contract": "Trace mono; HiLog epoch/nsec; retained clock brackets must be calibrated before combining streams."}
    geometry = manifest["screen_geometry"] = {"schema_version": "screen_geometry/v1",
        "mode": profile.get("screen_size_mode", "dynamic"), "reference_size": list(profile["screen_size"]),
        "actual_size": None, "source": "dumpLayout.root.bounds", "status": "pending", "observations": []}
    run_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[str, Any] = {}
    power_capabilities: dict[str, Any] = {}
    manifest["device_capabilities"] = power_capabilities
    lock_owned = False
    active_remote = ""
    preparation_client = None
    awake_guard = None
    collector_session = None

    def emit() -> None:
        write_json(run_dir / "manifest.json", manifest)
        if progress:
            progress(manifest)

    def retain(path: Path, step_id: str, kind: str, extra: dict[str, Any] | None = None) -> None:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest["files"].append({"step_id": step_id, "kind": kind, "name": path.name,
            "relative_path": path.relative_to(run_dir).as_posix(), "size_bytes": path.stat().st_size,
            "sha256": digest.hexdigest(), **(extra or {})})

    def collect(remote_path: str, local: Path, step_id: str, kind: str, *, reader=None) -> bool:
        try:
            metadata = ((reader or client).collect(remote_path, local, timeout=8, cleanup=False)
                        if kind == "ui_layout" else client.collect(remote_path, local))
            retain(local, step_id, kind, {"device_job_id": metadata.get("job_id"),
                **({"storage_locations": metadata["storage_locations"]} if metadata.get("storage_locations") else {})})
            return True
        except Exception as exc:
            if kind == "ui_layout" and (type(exc).__name__ == "CaptureCancelled" or isinstance(exc, CapturePreparationError)):
                raise
            manifest["quality"]["warnings"].append({"step_id": step_id, "file": local.name, "reason": str(exc)[:1000]})
            if kind == "ui_layout":
                raise CapturePreparationError("SCENARIO_SCREEN_LAYOUT_UNAVAILABLE", "界面布局回收失败：" + str(exc)[:250],
                                              "请查看当前命令及连接诊断；布局回传已使用短超时并支持取消。") from exc
            return False

    def retain_preparation_failure_screen(failed_step: dict[str, Any]) -> None:
        """Best-effort evidence before collectors start; never changes the failure.

        screenCap -p is shared with the original capture_gallery_case.py dump_ui.
        Transfer uses the same registered client and manifest as trace/layouts.
        """
        diagnostic: dict[str, Any] = {"status": "unavailable"}
        failed_step["screen_preparation_screenshot"] = diagnostic
        remote_path = active_remote + "/screen_preparation_failed.png"
        local = run_dir / Path(active_remote).name / "screen_preparation_failed.png"
        evidence = None
        try:
            client.check_cancelled()
            evidence = PreparationClient(client, failed_step, emit, seconds=SCREEN_EVIDENCE_BUDGET_SECONDS,
                                         key="screen_evidence_timing", phase="screen_evidence")
            states = failed_step["screen_state_diagnostics"] = {}
            for label, command in (("power", "power-shell dump -s"), ("lock_service", "hidumper -s 3704 -a '-all'")):
                item = states[label] = {"command": command}
                try:
                    output = evidence.shell(command, timeout=3)
                    item.update(status="collected", output=output[:4000])
                    if label == "lock_service":
                        item["screen_locked_by_user"] = {match[2]: match[1] == "true" for match in
                            re.finditer(r"(?m)^\s*\*\s*screenLocked\s+(true|false)\s+(\d+)\s*$", output)}
                        match = re.search(r"(?m)^\s*\*\s*screenState\s+(true|false)\b", output)
                        item["screen_on"] = match[1] == "true" if match else None
                except Exception as exc:
                    item.update(status="unavailable", reason=str(exc)[:500])
                    if type(exc).__name__ == "CaptureCancelled" or getattr(exc, "code", "") == "SCENARIO_SCREEN_PREPARATION_TIMEOUT":
                        raise
            evidence.shell("uitest screenCap -p " + shlex.quote(remote_path), timeout=8)
            size = evidence.shell("wc -c < " + shlex.quote(remote_path), timeout=3).strip()
            if not size.isdigit() or not 8 <= int(size) <= PREPARATION_SCREENSHOT_MAX_BYTES:
                raise ValueError("失败截图为空或超过 16 MiB，未传输截图。")
            client.check_cancelled()
            metadata = evidence.collect(remote_path, local, timeout=8, cleanup=False)
            evidence.check_cancelled()
            with local.open("rb") as handle:
                signature = handle.read(8)
            if local.stat().st_size != int(size) or signature != b"\x89PNG\r\n\x1a\n":
                raise ValueError("失败截图不是完整的 PNG 文件。")
            retain(local, str(failed_step["step_id"]), PREPARATION_SCREENSHOT_KIND,
                   {"device_job_id": metadata.get("job_id"), "captured_at_epoch": time.time()})
            diagnostic.update(status="retained", relative_path=local.relative_to(run_dir).as_posix())
            evidence.finish("completed")
        except Exception as screenshot_error:
            if evidence is not None:
                status = ("cancelled" if type(screenshot_error).__name__ == "CaptureCancelled" else
                          "timed_out" if getattr(screenshot_error, "code", "") == "SCENARIO_SCREEN_PREPARATION_TIMEOUT" else "failed")
                evidence.finish(status)
            diagnostic.update(status="skipped" if type(screenshot_error).__name__ == "CaptureCancelled" else "unavailable",
                              reason=str(screenshot_error)[:500])

    def dump_ui(directory: Path, remote_step: str, name: str, step_id: str, *, reader=None) -> dict[str, Any]:
        # No action uses a new geometry until a second real layout confirms it.
        # Every poll is retained separately; measured operations are never replayed.
        for attempt in range(3):
            device = reader or client
            device.check_cancelled()
            suffix = "" if attempt == 0 else f"_geometry_{attempt:02d}"
            remote_path = remote_step + "/" + name + suffix + ".json"
            try:
                device.shell("uitest dumpLayout -p " + shlex.quote(remote_path), timeout=8)
            except Exception as exc:
                if type(exc).__name__ == "CaptureCancelled" or isinstance(exc, CapturePreparationError):
                    raise
                raise CapturePreparationError("SCENARIO_SCREEN_LAYOUT_UNAVAILABLE", "读取 UI 布局失败：" + str(exc)[:250],
                                              "请查看当前命令、失败现场及系统锁屏诊断，确认 UITest 与 HDC 响应。") from exc
            local = directory / (name + suffix + ".json")
            if not collect(remote_path, local, step_id, "ui_layout", reader=device):
                raise RuntimeError("UI layout could not be recovered")
            try:
                tree = json.loads(local.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise CapturePreparationError("SCENARIO_SCREEN_LAYOUT_UNAVAILABLE", "界面布局无法解析：" + str(exc)[:250],
                                              "请查看设备操作诊断和失败现场，确认 UITest 已输出完整布局。") from exc
            try:
                observed = sync_screen_geometry(tree, profile)
            except CapturePreparationError:
                geometry["status"] = "invalid"
                raise
            previous_size = geometry["actual_size"]
            changed = previous_size is not None and previous_size != observed["actual_size"]
            needs_confirmation = previous_size is None or changed
            invalidated = sorted(cache) if needs_confirmation else []
            if needs_confirmation:
                cache.clear()
            evidence = {**observed, "step_id": step_id, "layout_file": local.relative_to(run_dir).as_posix(),
                        "changed": changed, "confirmed": not needs_confirmation, "invalidated_cache_keys": invalidated}
            geometry["observations"].append(evidence)
            geometry.update(actual_size=observed["actual_size"], orientation=observed["orientation"],
                            status="awaiting_confirmation" if needs_confirmation else "confirmed")
            if not needs_confirmation:
                return tree
            if attempt < 2:
                device.shell("sleep 0.15", timeout=3)
        geometry["status"] = "unstable"
        raise CapturePreparationError("SCENARIO_SCREEN_GEOMETRY_UNSTABLE", "屏幕尺寸或方向持续变化，尚未获得稳定布局，未生成后续操作坐标。",
                                      "请保持设备方向和窗口模式稳定后重试；已清除之前缓存的控件坐标。", phase="screen_geometry")

    try:
        client.check_cancelled()
        collector_record = manifest["collector_preparation"] = {
            "message": "正在核验采集会话并清理上次采集残留", "outside_capture_window": True}
        emit()
        collector_session = CollectorSession(client, run_id, remote, collector_record,
            terminal_run_ids=terminal_run_ids or (), on_change=emit,
            private_device=private_device)
        collector_session.acquire()
        lock_owned = True
        client = collector_session.lease_client
        collector_record["message"] = "采集会话已就绪，上次采集残留检查完成"
        emit()
        # This runtime-only idle timeout protects the bounded device script.
        # It is refreshed at handoff and restored on every exit; it is not a
        # deadline for preparation. No synthetic inputs run inside capture.
        awake_guard = ScreenAwakeGuard(client, manifest.setdefault("screen_awake_guard", {}),
            timeout_ms=int(max(180, profile["step_timeout_seconds"] + 180)) * 1000,
            capability_cache=power_capabilities)
        awake_guard.acquire()
        client.shell("mkdir -p " + shlex.quote(remote))
        info = client.shell('param get const.ohos.fullname; param get const.product.model; param get const.product.software.version; command -v uitest; command -v uinput')
        (run_dir / "device_environment.txt").write_text(info, encoding="utf-8")
        retain(run_dir / "device_environment.txt", "", "device_environment")
        trace_root = ""
        if "trace" in capture["collectors"]:
            trace_info = client.shell('for root in /sys/kernel/tracing /sys/kernel/debug/tracing; do '
                'if [ -r "$root/tracing_on" ]; then attempt=1; while [ "$attempt" -le 3 ]; do '
                'observed=$(cat "$root/tracing_on"); printf "%s=%s attempt=%s\\n" "$root" "$observed" "$attempt"; '
                '[ "$observed" = 0 ] && break; [ "$attempt" = 3 ] || sleep 0.1; attempt=$((attempt + 1)); '
                'done; break; fi; done')
            # Older bridge/test clients normalize this probe to ``root=0``;
            # accept that compatible form while new clients retain each
            # observation so a transient read does not become a false alarm.
            observations = re.findall(r"(?m)^(/sys/[^=\s]+)=([01])(?: attempt=([1-3]))?$", trace_info)
            if not observations:
                raise CapturePreparationError("SCENARIO_TRACE_STATE_UNAVAILABLE", "无法读取设备的 Trace 状态，采集尚未开始。",
                    "检查 tracefs 访问权限和设备连接；平台已保留会话与清理记录。", phase="collector_preparation")
            trace_root, trace_state, _ = observations[-1]
            collector_record["trace_preflight"] = {"status": "available" if trace_state == "0" else "foreign_or_unclaimed_active",
                "root": trace_root, "observations": [{"state": state, "attempt": int(attempt or index)}
                    for index, (root, state, attempt) in enumerate(observations, 1) if root == trace_root]}
            emit()
            if trace_state != "0":
                raise CapturePreparationError("SCENARIO_TRACE_ALREADY_ACTIVE", "设备仍有未能确认归属的 Trace 采集，尚未启动新的采集。",
                    "平台已复核 3 次并保留现有 Trace；查看 collector_preparation 的归属记录。其他工具或旧版本未登记的 Trace 需由原采集工具结束。", phase="collector_preparation")
            categories = client.shell("hitrace -l")
            supported = set(re.findall(r"(?m)^\s*([a-z][a-z0-9_]*)\s+-", categories))
            if not supported or "sched" not in supported:
                raise RuntimeError("Cannot verify sched category support from hitrace -l")
            requested = profile["trace_categories"]
            profile["trace_categories"] = [name for name in requested if name in supported]
            manifest["trace_categories"] = {"selected": profile["trace_categories"], "unavailable": sorted(set(requested) - supported),
                "argument_format": "space_separated", "begin_argv": hitrace_begin_command(profile)}
        if any(step["operation"] == "zoom" for step in steps):
            help_text = client.shell("uinput --help || true")
            if "three finger" not in help_text.lower() and "三指" not in help_text:
                raise RuntimeError("uinput multi-finger injection was not verified; single-finger substitution is not allowed")
            if not profile.get("calibration", {}).get("zoom_extrema_verified"):
                manifest["quality"]["warnings"].append({"reason": "Pinch gesture is executable, but reaching zoom extrema is not verified by the profile."})
        manifest["profile_effective"] = profile
        emit()
        for step in steps:
            client.check_cancelled()
            ordinal, step_id = step["ordinal"], step["step_id"]
            captured = step_id in selected
            directory = run_dir / f"{ordinal:02d}_{step['slug']}"
            directory.mkdir()
            active_remote = remote + "/" + directory.name
            client.shell("mkdir -p " + shlex.quote(active_remote))
            record: dict[str, Any] = {"step_id": step_id, "ordinal": ordinal, "title": step["title"], "captured": captured,
                "status": "preparing", "started_at_epoch": time.time(), "collector_status": {}}
            manifest["steps"].append(record)
            emit()
            preparation_client = PreparationClient(client, record, emit)
            before_name = "before_menu" if step.get("template_step") == 10 else "before"
            before = prepare_capture_device(profile, preparation_client, record,
                lambda index: dump_ui(directory, active_remote, before_name if index == 0 else f"unlock_{index:02d}",
                                      step_id, reader=preparation_client), power_capabilities=power_capabilities)
            verify_page_available(before, profile)
            preparation_client.finish("completed")
            preparation_client = None
            if step.get("template_step") == 10:
                verify_screen(before, profile)
                node = select_node(before, profile["targets"]["save_menu"], profile)
                client.shell(shlex.join(["uitest", "uiInput", "click", *map(str, node["point"])]))
                client.shell("sleep 0.7")
                record.setdefault("outside_capture_preparation", []).append({"action": "open_save_menu"})
                before = dump_ui(directory, active_remote, "before", step_id)
                verify_page_available(before, profile)
            launch_prepared = False
            if step["operation"] == "launch" and capture.get("preparation_mode", "auto") != "already_prepared":
                preparation_client = PreparationClient(client, record, emit,
                    key="gallery_preparation_timing", phase="gallery_preparation")
                before, launch_prepared = prepare_gallery_launch(before, profile, preparation_client, record,
                    lambda index: dump_ui(directory, active_remote, f"preparation_{index:02d}", step_id,
                                          reader=preparation_client), captured=captured, reset=True)
                preparation_client.finish("completed")
                preparation_client = None
            verify_screen(before, profile, gallery=step["operation"] not in {"launch", "key", "home", "wait"})
            try:
                cache["albums"] = select_node(before, profile["targets"].get("albums"), profile)
            except ValueError:
                pass
            record["screen_geometry"] = dict(geometry["observations"][-1])
            actions = ([{"name": "GALLERY_HOME_ALREADY_READY", "command": ["true"]}]
                       if launch_prepared else _resolve_actions(step, before, profile, cache))
            pid = client.shell("pidof " + shlex.quote(profile["bundle"]) + " || true").strip()
            source = build_step_script(step, actions, profile, capture, active_remote, trace_root, pid, session=collector_session)
            stage_script(client, active_remote + "/capture.sh", source, directory_ready=True)
            # A remote upload can outlast the phone's original idle timeout.
            # Re-read after the upload; recovering the UI can change controls or
            # PID, so rebuild and verify once more instead of using stale input.
            handoffs = record["capture_handoffs"] = []
            for handoff_index in range(2):
                awake_guard.refresh_if_needed(required_seconds=profile["step_timeout_seconds"] + 60)
                handoff = {"attempt": handoff_index + 1}
                handoffs.append(handoff)
                preparation_client = PreparationClient(client, record, emit, key="screen_handoff_timing")
                before = prepare_capture_device(profile, preparation_client, handoff,
                    lambda index: dump_ui(directory, active_remote, f"handoff_{handoff_index}_{index:02d}",
                                          step_id, reader=preparation_client),
                    layout_first=awake_guard.record.get("status") == "active", power_capabilities=power_capabilities)
                verify_page_available(before, profile)
                if step["operation"] == "launch" and capture.get("preparation_mode", "auto") != "already_prepared":
                    before, launch_prepared = prepare_gallery_launch(before, profile, preparation_client, handoff,
                        lambda index: dump_ui(directory, active_remote, f"handoff_{handoff_index}_launch_{index:02d}",
                                              step_id, reader=preparation_client), captured=captured)
                verify_screen(before, profile, gallery=step["operation"] not in {"launch", "key", "home", "wait"})
                try:
                    cache["albums"] = select_node(before, profile["targets"].get("albums"), profile)
                except ValueError:
                    pass
                actions = ([{"name": "GALLERY_HOME_ALREADY_READY", "command": ["true"]}]
                           if launch_prepared else _resolve_actions(step, before, profile, cache))
                pid = preparation_client.shell("pidof " + shlex.quote(profile["bundle"]) + " || true", timeout=3).strip()
                updated_source = build_step_script(step, actions, profile, capture, active_remote, trace_root, pid, session=collector_session)
                handoff["script_changed"] = updated_source != source
                preparation_client.finish("completed")
                preparation_client = None
                if updated_source == source:
                    break
                if handoff_index == 1:
                    raise CapturePreparationError("SCENARIO_CAPTURE_HANDOFF_UNSTABLE",
                        "采集前页面或目标进程持续变化，尚未获得稳定的操作配置，采集尚未开始。",
                        "请查看临采前布局、屏幕准备和进程记录；本次未使用旧坐标或旧 PID 执行。")
                source = updated_source
                stage_script(client, active_remote + "/capture.sh", source, directory_ready=True)
            record["screen_geometry"] = dict(geometry["observations"][-1])
            write_json(directory / "resolved_actions.json", actions)
            retain(directory / "resolved_actions.json", step_id, "resolved_actions")
            record["target_pid"] = int(pid) if pid.isdigit() else None
            if step["operation"] == "launch":
                record["launch_condition"] = "already_ready" if launch_prepared else "existing_process" if pid.isdigit() else "process_state_unknown"
                if not launch_prepared:
                    record["launch_process_state"] = "already_running" if pid.isdigit() else "unknown"
                    record["launch_temperature"] = "warm_or_resume" if pid.isdigit() else "unknown"
                reset_verified = captured and record.get("gallery_reset", {}).get("process_stop_verified") is True
                if reset_verified and not pid:
                    record.update(launch_condition="process_stopped_before_launch",
                                  launch_process_state="process_stopped_before_launch", launch_temperature="process_restart")
                if any(item.get("screen_preparation", {}).get("application_launch_attempted")
                       for item in (handoffs if reset_verified else [record, *handoffs])):
                    record["launch_temperature"] = "prelaunched_during_screen_preparation"
                    record["launch_precondition_changed"] = True
                    if captured:
                        manifest["quality"]["warnings"].append({"step_id": step_id, "code": "SCREEN_PREPARATION_PRELAUNCHED_APP",
                            "reason": "解锁准备曾尝试拉起目标应用，本次启动测量不代表冷启动。"})
            record["hilog_scope"] = "target_pid" if pid.isdigit() else "system_no_unique_preaction_pid"
            (directory / "capture.sh").write_text(source, encoding="utf-8")
            retain(directory / "capture.sh", step_id, "device_script")
            record["status"] = "capturing" if captured else "executing_preparation"
            emit()
            execution_error = None
            try:
                client.shell("sh " + shlex.quote(active_remote + "/capture.sh"),
                             timeout=profile["step_timeout_seconds"] + STEP_SHELL_CLEANUP_ALLOWANCE_SECONDS)
            except Exception as exc:
                execution_error = exc
                collector_session.recover()
            names = {"status.txt": "execution_status", "actions.log": "action_log", "clock_before.txt": "clock_bracket", "clock_after.txt": "clock_bracket"}
            if captured and "trace" in capture["collectors"]:
                names.update({"trace.ftrace": "trace", "trace_clock.txt": "trace_clock", "buffer_stats.txt": "trace_buffer_stats", "hitrace_begin.log": "collector_log", "hitrace_finish.log": "collector_log"})
            if captured and "hilog" in capture["collectors"]:
                names.update({"hilog.log": "hilog", "hilog_status.txt": "collector_status"})
            if captured and "pmu" in capture["collectors"]:
                pmu_gate = capture["pmu"].get("preparation")
                if isinstance(pmu_gate, dict) and pmu_gate.get("allow_execution") is False:
                    names.update({"pmu_preparation.json": "collector_preparation", "pmu.log": "collector_log", "pmu_status.txt": "collector_status"})
                else:
                    names.update({"pmu.csv": "pmu", "pmu.json": "pmu_metadata", "pmu.log": "collector_log", "pmu_clock_error.log": "collector_log", "pmu_status.txt": "collector_status", "pmu_exit_code.txt": "collector_status", "pmu_binary_identity.txt": "collector_identity", "clock_before.json": "clock_calibration", "clock_after.json": "clock_calibration"})
                    names.update({"pmu_capabilities.json": "collector_capabilities", "pmu_capabilities.log": "collector_log", "pmu_compatibility.json": "collector_compatibility", "pmu_capability_probe.json": "collector_probe"})
            for name, kind in names.items():
                collect(active_remote + "/" + name, directory / name, step_id, kind)
            status_path = directory / "status.txt"
            if not status_path.exists() or status_path.read_text().strip() != "0":
                execution_error = execution_error or RuntimeError("Device did not report successful completion")
            # Keep the successful hot path unchanged: cleanup diagnostics cost
            # two additional HDC transfers only when the host command failed,
            # status.txt is absent, or the device script reported non-zero.
            if execution_error is not None:
                for name, kind in (("cleanup_phase.txt", "collector_cleanup_phase"),
                                   ("cleanup_events.log", "collector_cleanup_log")):
                    collect(active_remote + "/" + name, directory / name, step_id, kind)
                cleanup_phase_path = directory / "cleanup_phase.txt"
                cleanup_events_path = directory / "cleanup_events.log"
                if cleanup_phase_path.exists():
                    cleanup_phase = cleanup_phase_path.read_text(encoding="utf-8", errors="replace").strip()[:200]
                    cleanup_events = (cleanup_events_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                                      if cleanup_events_path.exists() else "")
                    record["device_script_cleanup"] = {"last_phase": cleanup_phase or "unknown",
                                                        "events": cleanup_events, "bounded": True}
                    diagnostic = []
                    if cleanup_phase != "cleanup_complete":
                        diagnostic.append("cleanup stopped at " + (cleanup_phase or "unknown"))
                    event_lines = [line.strip() for line in cleanup_events.splitlines() if line.strip()]
                    if event_lines:
                        diagnostic.append("last cleanup event: " + event_lines[-1][:300])
                    if diagnostic:
                        execution_error = RuntimeError(f"{execution_error}; device script " + "; ".join(diagnostic))
            if captured:
                # Coverage checks need trace windows even when the caller listed
                # PMU first; collector order is not an execution dependency.
                for collector in sorted(capture["collectors"], key=lambda item: item != "trace"):
                    if collector == "trace":
                        trace = directory / "trace.ftrace"
                        clock_path, stats_path = directory / "trace_clock.txt", directory / "buffer_stats.txt"
                        validation = validate_trace(trace, step, profile["hold_seconds"] if step["operation"] != "home" else 0,
                            stats_path.read_text() if stats_path.exists() else "") if trace.exists() else {"valid": False, "reason": "trace_missing"}
                        validation["monotonic_clock_verified"] = clock_path.exists() and "[mono]" in clock_path.read_text()
                        validation["valid"] &= validation["monotonic_clock_verified"]
                        record["trace_validation"] = validation
                        write_json(directory / "trace_validation.json", validation)
                        retain(directory / "trace_validation.json", step_id, "trace_validation")
                        record["collector_status"][collector] = "collected" if validation["valid"] else "invalid"
                    else:
                        state_path = directory / f"{collector}_status.txt"
                        state = state_path.read_text().strip() if state_path.exists() else "missing"
                        record["collector_status"][collector] = "collected" if collector == "hilog" and state == "stopped" and (directory / "hilog.log").exists() else state
                        if collector == "pmu":
                            window = next((span for span in record.get("trace_validation", {}).get("spans", [])
                                           if span["name"] == f"GALLERY_STEP_{ordinal:02d}_{step['slug']}"), None)
                            record["pmu_validation"] = validate_pmu(directory, target_tid=record["target_pid"], step_window=window, collector_state=state)
                            write_json(directory / "pmu_validation.json", record["pmu_validation"])
                            retain(directory / "pmu_validation.json", step_id, "pmu_validation")
                            for diagnostic in record["pmu_validation"].get("diagnostics", []):
                                if diagnostic["severity"] != "info":
                                    manifest["quality"]["warnings"].append({"step_id": step_id, "collector": "pmu", "code": diagnostic["code"],
                                        "reason": diagnostic["message"], "suggestion": diagnostic.get("suggestion", "")})
                            if state == "collected" and not record["pmu_validation"]["valid"]:
                                record["collector_status"][collector] = "invalid"
                            elif state == "collected" and not record["pmu_validation"]["quality_complete"]:
                                record["collector_status"][collector] = "partial"
                if any(status != "collected" for status in record["collector_status"].values()):
                    manifest["quality"]["warnings"].append({"step_id": step_id, "reason": "Some requested collectors are unavailable, failed or invalid", "collectors": record["collector_status"]})
            if execution_error:
                raise execution_error
            wait_for_step_ready(step, profile, client,
                lambda index: dump_ui(directory, active_remote, "after" if not index else f"after_ready_{index:02d}", step_id),
                timeout_seconds=page_ready_timeout_seconds)
            record.update(status="succeeded", finished_at_epoch=time.time())
            active_remote = ""
            emit()
        manifest["status"] = "partial" if manifest["quality"]["warnings"] else "succeeded"
    except Exception as exc:
        manifest["status"] = "cancelled" if type(exc).__name__ == "CaptureCancelled" else "failed"
        if preparation_client is not None:
            preparation_client.finish("cancelled" if manifest["status"] == "cancelled" else
                                      "timed_out" if getattr(exc, "code", "") == "SCENARIO_SCREEN_PREPARATION_TIMEOUT" else "failed")
        failed_step = manifest["steps"][-1] if manifest["steps"] else {}
        error = {"reason": str(exc)[:3000], "type": type(exc).__name__,
                 "code": getattr(exc, "code", "SCENARIO_CAPTURE_FAILED"),
                 "suggestion": getattr(exc, "suggestion", "请查看该步骤的执行日志和设备页面，处理后重新执行采集。"),
                 "step_id": failed_step.get("step_id"), "step_title": failed_step.get("title"),
                 "phase": getattr(exc, "phase", failed_step.get("status", "preparation"))}
        manifest["quality"]["errors"].append(error)
        if (manifest["status"] != "cancelled" and failed_step.get("status") == "preparing" and active_remote
                and isinstance(exc, CapturePreparationError) and error["code"] in {
                    "SCENARIO_SCREEN_LOCKED", "SCENARIO_UNLOCK_AUTH_REQUIRED",
                    "SCENARIO_UNLOCK_UNVERIFIED", "SCENARIO_SCREEN_PREPARATION_FAILED", "SCENARIO_USB_DIALOG",
                    "SCENARIO_SCREEN_PREPARATION_TIMEOUT", "SCENARIO_SCREEN_LAYOUT_UNAVAILABLE", "SCENARIO_DISPLAY_NOT_AWAKE",
                    "SCENARIO_CAPTURE_HANDOFF_UNSTABLE", "SCENARIO_GALLERY_RESET_FAILED", "SCENARIO_GALLERY_STOP_UNCONFIRMED",
                    "SCENARIO_GALLERY_RESTART_FAILED", "SCENARIO_GALLERY_RESET_NOT_READY", "SCENARIO_LAUNCH_REQUIRES_HOME"}):
            retain_preparation_failure_screen(failed_step)
        if manifest["steps"] and manifest["steps"][-1]["status"] not in {"succeeded"}:
            manifest["steps"][-1].update(status=manifest["status"], finished_at_epoch=time.time(), error=error)
    finally:
        # Restore independently of collector/lock cleanup, including cancellation.
        if awake_guard is not None:
            awake_guard.restore()
            if manifest["screen_awake_guard"].get("restoration_needed"):
                manifest["quality"]["warnings"].append({"code": "SCREEN_TIMEOUT_RESTORE_FAILED",
                    "reason": "临时熄屏超时未确认恢复，请查看 screen_awake_guard 的恢复记录。"})
                if manifest["status"] == "succeeded":
                    manifest["status"] = "partial"
        try:
            released = collector_session.release() if collector_session is not None else {"status": "unowned"}
            if released["status"] != "released" and (lock_owned or released["status"] != "unowned"):
                raise RuntimeError("采集残留清理尚未确认：" + str(released.get("reason") or released["status"]))
            manifest["cleanup"] = {"status": "completed" if released["status"] == "released" else "not_owned", "remote_outputs_retained": True}
        except Exception as exc:
            manifest["cleanup"] = {"status": "pending", "reason": str(exc)[:2000], "remote_lock_retained": True}
            manifest["status"] = "failed" if manifest["status"] != "cancelled" else "cancelled"
        emit()
    return manifest


def cancel_owned_capture(client: DeviceClient, remote: str) -> None:
    command = f'''out={shlex.quote(remote)}
{_PROCESS_HELPERS}
device_boot=$(cat /proc/sys/kernel/random/boot_id)
[ -n "$device_boot" ] || exit 1
if [ -r "$out/owner.pid" ]; then
 owner=$(cat "$out/owner.pid")
 case "$owner" in ''|*[!0-9]*) exit 1;; esac
 owner_ticks=$(collector_ticks "$owner") || exit 1
 commandline=$(collector_cmdline "$owner") || exit 1
 collector_has_arg "$commandline" "$out/capture.sh" || exit 1
 collector_alive "$owner" "$owner_ticks" "$device_boot" || exit 1
 kill -TERM "$owner" || exit 1
 attempts=0
 while [ -r "$out/owner.pid" ] && [ "$attempts" -lt 50 ]; do sleep 0.2; attempts=$((attempts + 1)); done
 test ! -r "$out/owner.pid"
fi'''
    client.shell(command, timeout=30, cleanup=True)
