"""Device session ownership and recovery of registered platform collectors.

The lease is concurrency protection, not a task deadline. Outputs are retained;
process names alone never authorize a signal or stopping global tracing.
"""
from __future__ import annotations

import math
from pathlib import PurePosixPath
import re
import shlex
import time
import uuid

from .gallery import CapturePreparationError

REMOTE_LOCK = "/data/local/tmp/hetero_scenario_capture.lock"
GLOBAL_TRACE_CLAIM_SUFFIX = ".trace_owner"
TRACE_ROOTS = ("/sys/kernel/tracing", "/sys/kernel/debug/tracing")
ROLES = ("owner", "logger", "sampler", "watchdog", "timer", "action", "pmu_probe", "trace_heartbeat", "trace_finisher")
LEASE_RENEWAL_MARGIN_SECONDS = 15


def _path(value):
    if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in value.split("/"):
        raise ValueError("Collector paths must be absolute platform paths")
    return value


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError("Invalid collector session identity")
    return value


# /proc/stat field 22 is field 20 after removing the parenthesized comm. It
# prevents a stale PID file from signalling a different process after PID reuse.
_PROCESS_HELPERS = r'''
collector_ticks() {
  case "$1" in ''|*[!0-9]*) return 1;; esac
  [ "$1" -gt 1 ] || return 1
  collector_stat=$(sed 's/^.*) //' "/proc/$1/stat" 2>/dev/null) || return 1
  set -- $collector_stat
  [ "$#" -ge 20 ] || return 1
  shift 19
  case "$1" in ''|*[!0-9]*) return 1;; esac
  printf '%s\n' "$1"
}
collector_alive() {
  [ "$3" = "$device_boot" ] || return 1
  [ -n "$2" ] && [ "$(collector_ticks "$1")" = "$2" ] || return 1
  collector_stat=$(sed 's/^.*) //' "/proc/$1/stat" 2>/dev/null) || return 1
  case "$collector_stat" in ''|Z|Z\ *) return 1;; esac
  kill -0 "$1" 2>/dev/null
}
collector_hex() {
  od -An -v -t x1 | sed -e ':compact' -e '$!{' -e N -e 'b compact' -e '}' -e 's/[[:space:]][[:space:]]*/:/g' -e 's/^://' -e 's/:$//'
}
collector_cmdline() {
  collector_cmd_hex=$(collector_hex < "/proc/$1/cmdline" 2>/dev/null) || return 1
  case "$collector_cmd_hex" in ''|*[!0-9a-f:]*) return 1;; esac
  case "$collector_cmd_hex" in *:00) printf '%s\n' "$collector_cmd_hex";; *) return 1;; esac
}
collector_has_arg() {
  collector_arg_hex=$(printf '%s' "$2" | collector_hex) || return 1
  [ -n "$collector_arg_hex" ] || return 1
  case "00:$1:" in *"00:${collector_arg_hex}:00:"*) return 0;; *) return 1;; esac
}
collector_executable_is() {
  collector_arg_hex=$(printf '%s' "$2" | collector_hex) || return 1
  [ -n "$collector_arg_hex" ] || return 1
  collector_first_arg=${1%%:00*}
  case "$collector_first_arg" in "$collector_arg_hex"|"2f:$collector_arg_hex"|*":2f:$collector_arg_hex") return 0;; *) return 1;; esac
}
'''


def step_shell_prelude(run_id, remote_step, token, *, lock_path=REMOTE_LOCK, trace_claim_path=None):
    """Insert after out/root setup; register each child immediately after launch."""
    trace_claim_path = trace_claim_path or lock_path + GLOBAL_TRACE_CLAIM_SUFFIX
    _identity(run_id); _identity(token); _path(remote_step); _path(lock_path); _path(trace_claim_path)
    return [f"session_lock={shlex.quote(lock_path)}", f"session_token={shlex.quote(token)}",
        f"session_run={shlex.quote(run_id)}", 'device_boot=$(cat /proc/sys/kernel/random/boot_id)',
        f"global_trace_claim={shlex.quote(trace_claim_path)}",
        _PROCESS_HELPERS,
        '[ "$(cat "$session_lock/token" 2>/dev/null)" = "$session_token" ] || exit 75',
        '[ -n "$device_boot" ] && [ "$(cat "$session_lock/boot_id" 2>/dev/null)" = "$device_boot" ] || exit 75',
        '[ ! -d "$session_lock/recovery" ] || exit 75',
        'mkdir -p "$out/collector_processes"',
        r'''register_collector() {
  role=$1; child=$2
  case "$role" in owner|logger|sampler|watchdog|timer|action|pmu_probe|trace_heartbeat|trace_finisher) ;; *) return 1;; esac
  ticks=$(collector_ticks "$child")
  [ -n "$ticks" ] || return 0
  printf '%s %s %s %s\n' "$child" "$ticks" "$device_boot" "$session_token" > "$out/collector_processes/$role.tmp"
  mv "$out/collector_processes/$role.tmp" "$out/collector_processes/$role"
}
forget_collector() {
  case "$1" in owner|logger|sampler|watchdog|timer|action|pmu_probe|trace_heartbeat|trace_finisher) rm -f "$out/collector_processes/$1";; esac
}
write_global_trace_claim() {
  trace_claim_tmp="$global_trace_claim.$session_token.tmp"
  printf '%s %s %s %s %s %s\n' "$1" "$session_token" "$session_run" "$out" "$2" "$device_boot" > "$trace_claim_tmp" || return 1
  mv "$trace_claim_tmp" "$global_trace_claim"
}
clear_global_trace_claim() {
  [ -f "$global_trace_claim" ] || return 0
  read claim_root claim_token claim_run claim_step claim_state claim_boot < "$global_trace_claim" || return 0
  if [ "$claim_token" = "$session_token" ] && [ "$claim_run" = "$session_run" ] && [ "$claim_step" = "$out" ]; then
    rm -f "$global_trace_claim"
  fi
}
mark_trace_intent() {
  printf '%s %s intent\n' "$1" "$session_token" > "$out/trace.owner" || return 1
  write_global_trace_claim "$1" intent
}
mark_trace_owned() {
  trace_owner_marker="HETERO_TRACE_OWNER_$session_token"
  printf 'C|%s|%s|1\n' "$$" "$trace_owner_marker" > "$1/trace_marker" || return 1
  printf '%s %s owned\n' "$1" "$session_token" > "$out/trace.owner" || return 1
  write_global_trace_claim "$1" owned
}
mark_trace_finished() { clear_global_trace_claim; rm -f "$out/trace.owner"; }
register_collector owner "$$"''']


_SHELL = _PROCESS_HELPERS + r'''
session_result() { printf 'HETERO_COLLECTOR_SESSION|%s|%s\n' "$1" "${2:-}"; }
session_event() { printf 'HETERO_COLLECTOR_EVENT|%s|%s|%s\n' "$1" "$2" "${3:-}"; }
for identity_tool in sed od; do
  command -v "$identity_tool" >/dev/null 2>&1 || { session_result pending process_identity_tools_unavailable; exit 0; }
done
now_uptime() { cut -d. -f1 /proc/uptime; }
device_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
now=$(now_uptime)
case "$now" in ''|*[!0-9]*) session_result pending uptime_unavailable; exit 0;; esac
[ -n "$device_boot" ] || { session_result pending boot_identity_unavailable; exit 0; }
claim_file="$session_lock/recovery/claim.$session_token"
release_claim() { rm -f "$claim_file"; rmdir "$session_lock/recovery" 2>/dev/null || true; }
claim_recovery() {
  if ! mkdir "$session_lock/recovery" 2>/dev/null; then
    for claim in "$session_lock"/recovery/claim.*; do
      [ -f "$claim" ] || continue
      read claim_pid claim_ticks claim_boot < "$claim"
      if collector_alive "$claim_pid" "$claim_ticks" "$claim_boot"; then return 1; fi
      rm -f "$claim"
    done
    rmdir "$session_lock/recovery" 2>/dev/null || return 1
    mkdir "$session_lock/recovery" 2>/dev/null || return 1
  fi
  printf '%s %s %s\n' "$$" "$(collector_ticks "$$")" "$device_boot" > "$claim_file" || return 1
  [ -f "$claim_file" ] || return 1
  trap release_claim EXIT
}
renew_session_lease() {
  renew_until=$((now + lease_seconds))
  existing_until=$(cat "$session_lock/lease_until" 2>/dev/null)
  case "$existing_until" in ''|*[!0-9]*) ;;
    *) if [ "$existing_until" -gt "$renew_until" ] 2>/dev/null; then renew_until=$existing_until; fi;;
  esac
  printf '%s\n' "$renew_until" > "$session_lock/lease_until"
}
write_session() {
  mkdir -p "$session_root" || return 1
  printf '%s\n' "$session_run" > "$session_lock/owner"
  printf '%s\n' "$session_root" > "$session_lock/remote_root"
  printf '%s\n' "$device_boot" > "$session_lock/boot_id"
  printf '%s\n' "$((now + lease_seconds))" > "$session_lock/lease_until"
  printf '%s\n' "$session_token" > "$session_lock/token"
  printf '%s\n' 1 > "$session_lock/schema"
}
acquired_session_result() {
  if [ -f "$global_trace_claim" ]; then session_result acquired trace_claim; else session_result acquired; fi
}
read_owner() {
  old_run=$(cat "$session_lock/owner" 2>/dev/null)
  old_token=$(cat "$session_lock/token" 2>/dev/null)
  old_boot=$(cat "$session_lock/boot_id" 2>/dev/null)
  old_until=$(cat "$session_lock/lease_until" 2>/dev/null)
  old_root=$(cat "$session_lock/remote_root" 2>/dev/null)
  old_schema=$(cat "$session_lock/schema" 2>/dev/null)
  case "$old_run" in ''|*[!a-zA-Z0-9_-]*) return 1;; esac
  [ -n "$old_root" ] || old_root="$remote_base/$old_run"
  [ "$old_root" = "$remote_base/$old_run" ] || return 1
}
terminal_owner() { case "$terminal_runs" in *"|$old_run|"*) return 0;; *) return 1;; esac; }
defer_private_trace() {
  [ "$operation" = acquire ] && [ "$private_device" = 1 ] && terminal_owner || return 1
  session_event deferred trace "$1"
}
signal_identity() {
  signal_name=$1; signal_action=$2; signal_role=$3; signal_child=$4; signal_ticks=$5; signal_boot=$6
  if kill -"$signal_name" "$signal_child" 2>/dev/null; then
    session_event "$signal_action" "$signal_role" "$signal_child"
  elif collector_alive "$signal_child" "$signal_ticks" "$signal_boot"; then
    session_event pending "$signal_role" "${signal_action}_failed"
    return 1
  else
    session_event exited "$signal_role" "$signal_child"
  fi
}
clear_matching_global_trace_claim() {
  [ -f "$global_trace_claim" ] || return 0
  read claim_root claim_token claim_run claim_step claim_state claim_boot < "$global_trace_claim" || return 0
  if [ "$claim_token" = "$1" ] && [ "$claim_step" = "$2" ]; then rm -f "$global_trace_claim"; fi
}
lease_expired() {
  [ "$old_schema" = 1 ] || return 1
  [ -n "$old_boot" ] && [ -n "$old_token" ] || return 1
  [ "$old_boot" != "$device_boot" ] && return 0
  case "$old_until" in ''|*[!0-9]*) return 1;; esac
  [ "$now" -ge "$old_until" ]
}
ledger_alive() {
  ledger=$1
  [ -f "$ledger" ] || return 1
  read child child_ticks child_boot child_token < "$ledger"
  [ "$child_token" = "$old_token" ] && collector_alive "$child" "$child_ticks" "$child_boot"
}
legacy_owner_alive() {
  [ -f "$1/owner.pid" ] || return 1
  child=$(cat "$1/owner.pid")
  child_ticks=$(collector_ticks "$child")
  [ -n "$child_ticks" ] || return 1
  cmd=$(collector_cmdline "$child") || return 1
  collector_has_arg "$cmd" "$1/capture.sh" && collector_alive "$child" "$child_ticks" "$device_boot"
}
stop_ledger() {
  ledger=$1
  ledger_alive "$ledger" || return 0
  signal_identity TERM signal "${ledger##*/}" "$child" "$child_ticks" "$child_boot"
}
owned_processes_alive() {
  for step in "$old_root"/*; do
    [ -d "$step" ] || continue
    for role in owner logger sampler watchdog timer action pmu_probe trace_heartbeat trace_finisher; do
      ledger_alive "$step/collector_processes/$role" && return 0
    done
    legacy_owner_alive "$step" && return 0
  done
  return 1
}
recover_output_bound_children() {
  for descriptor in /proc/[0-9]*/fd/1; do
    destination=$(readlink "$descriptor" 2>/dev/null) || continue
    case "$destination" in "$old_root"/*/hilog.log) role=logger;; "$old_root"/*/pmu.log) role=sampler;; *) continue;; esac
    step=${destination%/*}
    [ -f "$step/capture.sh" ] && grep -F "$step" "$step/capture.sh" >/dev/null 2>&1 || continue
    grep -F 'command_action' "$step/capture.sh" >/dev/null 2>&1 || continue
    process_path=${descriptor%/fd/1}; candidate=${process_path##*/}
    candidate_ticks=$(collector_ticks "$candidate")
    [ -n "$candidate_ticks" ] || continue
    cmd=$(collector_cmdline "$candidate") || continue
    if [ "$role" = logger ]; then
      collector_executable_is "$cmd" hilog || continue
    else
      collector_has_arg "$cmd" "$step/pmu.csv" && collector_has_arg "$cmd" "$step/pmu.json" || continue
    fi
    mkdir -p "$step/collector_processes"
    printf '%s %s %s %s\n' "$candidate" "$candidate_ticks" "$device_boot" "$old_token" > "$step/collector_processes/$role"
    session_event identified "$role" "$candidate"
  done
}
cleanup_owned() {
  for step in "$old_root"/*; do
    [ -d "$step" ] || continue
    if ledger_alive "$step/collector_processes/owner" || legacy_owner_alive "$step"; then
      [ "$allow_owner_stop" = 1 ] || { session_event protected owner "$child"; return 2; }
      signal_identity TERM signal owner "$child" "$child_ticks" "$device_boot" || return 1
    fi
  done
  recover_output_bound_children
  for step in "$old_root"/*; do
    [ -d "$step" ] || continue
    for role in logger sampler watchdog timer action pmu_probe trace_heartbeat trace_finisher; do
      stop_ledger "$step/collector_processes/$role" || return 1
    done
  done
  attempts=0
  while owned_processes_alive && [ "$attempts" -lt 20 ]; do sleep 0.1; attempts=$((attempts + 1)); done
  force_roles="owner logger watchdog timer action pmu_probe trace_heartbeat"
  [ "$allow_owner_stop" = 1 ] && force_roles="$force_roles sampler trace_finisher"
  for step in "$old_root"/*; do
    [ -d "$step" ] || continue
    for role in $force_roles; do
      if ledger_alive "$step/collector_processes/$role"; then
        signal_identity KILL kill "$role" "$child" "$child_ticks" "$child_boot" || return 1
      fi
    done
    if legacy_owner_alive "$step"; then kill -KILL "$child" 2>/dev/null || true; fi
  done
  attempts=0
  while owned_processes_alive && [ "$attempts" -lt 10 ]; do sleep 0.1; attempts=$((attempts + 1)); done
  if owned_processes_alive; then session_event pending processes still_running; return 1; fi
  for step in "$old_root"/*; do
    [ -f "$step/trace.owner" ] || continue
    read trace_root trace_token trace_state < "$step/trace.owner"
    case "$trace_roots" in *"|$trace_root|"*) ;;
      *)
        defer_private_trace private_preflight_invalid_root && continue
        session_event protected trace invalid_root; return 1;;
    esac
    if [ "$trace_token" != "$old_token" ] || [ -z "$old_token" ]; then
      defer_private_trace private_preflight_owner_mismatch && continue
      session_event protected trace owner_mismatch; return 1
    fi
    tracing=$(cat "$trace_root/tracing_on" 2>/dev/null)
    if [ "$tracing" = 0 ]; then
      clear_matching_global_trace_claim "$old_token" "$step"
      rm -f "$step/trace.owner"
      session_event trace already_stopped "$step"
      continue
    fi
    if [ "$tracing" != 1 ]; then
      defer_private_trace private_preflight_unavailable && continue
      session_event pending trace unavailable; return 1
    fi
    if [ "$trace_state" != owned ] || ! grep -F "HETERO_TRACE_OWNER_$old_token" "$trace_root/trace" >/dev/null 2>&1; then
      defer_private_trace private_preflight_ownership_marker_missing && continue
      session_event protected trace ownership_marker_missing; return 1
    fi
    recovered="$step/trace.recovered.$session_token.ftrace"
    hitrace --trace_finish -o "$recovered" > "$step/hitrace_recovery.$session_token.log" 2>&1 &
    finish_pid=$!; finish_ticks=$(collector_ticks "$finish_pid")
    printf '%s %s %s %s\n' "$finish_pid" "$finish_ticks" "$device_boot" "$old_token" > "$step/collector_processes/trace_finisher"
    attempts=0
    while collector_alive "$finish_pid" "$finish_ticks" "$device_boot" && [ "$attempts" -lt 30 ]; do sleep 0.1; attempts=$((attempts + 1)); done
    if collector_alive "$finish_pid" "$finish_ticks" "$device_boot"; then
      kill -TERM "$finish_pid" 2>/dev/null || true
      session_event pending trace finish_timeout; return 1
    fi
    wait "$finish_pid"; finish_status=$?
    [ "$finish_status" = 0 ] && [ "$(cat "$trace_root/tracing_on" 2>/dev/null)" = 0 ] || {
      session_event pending trace finish_unverified; return 1;
    }
    clear_matching_global_trace_claim "$old_token" "$step"
    rm -f "$step/trace.owner"
    session_event recovered trace "$recovered"
  done
  return 0
}
if [ "$operation" = acquire ]; then
  if mkdir "$session_lock" 2>/dev/null; then
    write_session && acquired_session_result || session_result pending session_write_failed
    exit 0
  fi
  read_owner || { session_result busy ownership_unverified; exit 0; }
  if [ "$old_token" = "$session_token" ]; then
    claim_recovery || { session_result busy recovery_active; exit 0; }
    [ "$(cat "$session_lock/token" 2>/dev/null)" = "$session_token" ] || { session_result busy owner_changed; exit 0; }
    write_session && acquired_session_result || session_result pending session_write_failed
    exit 0
  fi
  if ! terminal_owner && ! lease_expired; then
    if [ "$old_schema" = 1 ]; then session_result busy active_lease; else session_result busy legacy_owner_unverified; fi
    exit 0
  fi
  expected_token=$old_token; expected_run=$old_run
  claim_recovery || { session_result busy recovery_active; exit 0; }
  read_owner || { session_result pending owner_changed; exit 0; }
  [ "$old_token" = "$expected_token" ] && [ "$old_run" = "$expected_run" ] || { session_result busy owner_changed; exit 0; }
  if ! terminal_owner && ! lease_expired; then session_result busy lease_refreshed; exit 0; fi
  allow_owner_stop=0; terminal_owner && allow_owner_stop=1
  cleanup_owned; cleanup_status=$?
  if [ "$cleanup_status" != 0 ]; then session_result pending collector_recovery_incomplete; exit 0; fi
  session_event recovered session "$old_run"
  write_session && acquired_session_result || session_result pending session_write_failed
elif [ "$operation" = refresh ]; then
  claim_recovery || { session_result busy recovery_active; exit 0; }
  [ "$(cat "$session_lock/token" 2>/dev/null)" = "$session_token" ] || {
    session_result busy session_ownership_lost; exit 0;
  }
  renew_session_lease
  session_result refreshed
elif [ "$operation" = recover ] || [ "$operation" = release ]; then
  read_owner || { session_result unowned ownership_unverified; exit 0; }
  [ "$old_token" = "$session_token" ] || { session_result unowned session_ownership_lost; exit 0; }
  claim_recovery || { session_result pending recovery_active; exit 0; }
  read_owner && [ "$old_token" = "$session_token" ] || { session_result unowned session_ownership_lost; exit 0; }
  allow_owner_stop=1
  cleanup_owned || { session_result pending collector_recovery_incomplete; exit 0; }
  if [ "$operation" = recover ]; then
    renew_session_lease
    session_result recovered
  else
    for entry in "$session_lock"/*; do
      case "${entry##*/}" in owner|remote_root|boot_id|lease_until|token|schema|recovery) ;;
        *) session_result pending unknown_lock_contents; exit 0;; esac
    done
    rm -f "$session_lock/owner" "$session_lock/remote_root" "$session_lock/boot_id" "$session_lock/lease_until" "$session_lock/token" "$session_lock/schema"
    release_claim
    if rmdir "$session_lock" 2>/dev/null; then session_result released; else session_result pending lock_not_empty; fi
  fi
else session_result pending unknown_operation
fi
'''


# The hot heartbeat path uses the same claim/identity helpers but does not ship
# collector enumeration and recovery code on every UI or file-transfer RPC.
_REFRESH_SHELL = _SHELL[:_SHELL.index("write_session()")]
_REFRESH_SHELL += r'''
claim_recovery || { session_result busy recovery_active; exit 0; }
[ "$(cat "$session_lock/token" 2>/dev/null)" = "$session_token" ] &&
[ "$(cat "$session_lock/boot_id" 2>/dev/null)" = "$device_boot" ] || {
  session_result busy session_ownership_lost; exit 0;
}
renew_session_lease
session_result refreshed
'''


# Kept out of the already size-constrained acquire/release command. It runs
# only after the new session owns the lock, before the runner inspects or starts
# tracefs. Exact run/step/token, boot and in-buffer marker evidence are all
# required before finishing an abandoned platform trace.
_TRACE_RECONCILE_SHELL = _PROCESS_HELPERS + r'''
trace_result() { printf 'HETERO_TRACE_RECONCILE|%s|%s\n' "$1" "${2:-}"; }
device_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
[ "$(cat "$session_lock/token" 2>/dev/null)" = "$session_token" ] || { trace_result protected session_ownership_lost; exit 0; }
[ "$(cat "$session_lock/boot_id" 2>/dev/null)" = "$device_boot" ] || { trace_result protected session_boot_mismatch; exit 0; }
[ -f "$global_trace_claim" ] || { trace_result clear no_claim; exit 0; }
read claim_root claim_token claim_run claim_step claim_state claim_boot < "$global_trace_claim" || { trace_result protected claim_unreadable; exit 0; }
case "$trace_roots" in *"|$claim_root|"*) ;; *) trace_result protected claim_root_invalid; exit 0;; esac
case "$claim_run" in ''|*[!a-zA-Z0-9_-]*) trace_result protected claim_run_invalid; exit 0;; esac
case "$claim_step" in "$remote_base/$claim_run"/*) ;; *) trace_result protected claim_step_invalid; exit 0;; esac
if [ "$claim_boot" != "$device_boot" ]; then rm -f "$global_trace_claim"; trace_result clear stale_boot; exit 0; fi
tracing=$(cat "$claim_root/tracing_on" 2>/dev/null)
if [ "$tracing" = 0 ]; then
  rm -f "$global_trace_claim"
  if [ -f "$claim_step/trace.owner" ]; then
    read local_root local_token local_state < "$claim_step/trace.owner"
    [ "$local_root" = "$claim_root" ] && [ "$local_token" = "$claim_token" ] && rm -f "$claim_step/trace.owner"
  fi
  trace_result clear already_stopped
  exit 0
fi
[ "$tracing" = 1 ] || { trace_result pending trace_state_unavailable; exit 0; }
[ -f "$claim_step/trace.owner" ] || { trace_result protected local_claim_missing; exit 0; }
read local_root local_token local_state < "$claim_step/trace.owner"
[ "$local_root" = "$claim_root" ] && [ "$local_token" = "$claim_token" ] && [ "$local_state" = owned ] || {
  trace_result protected local_claim_mismatch; exit 0;
}
heartbeat_ledger="$claim_step/collector_processes/trace_heartbeat"
if [ -f "$heartbeat_ledger" ]; then
  read heartbeat_pid heartbeat_ticks heartbeat_boot heartbeat_token < "$heartbeat_ledger"
  if [ "$heartbeat_token" = "$claim_token" ] && collector_alive "$heartbeat_pid" "$heartbeat_ticks" "$heartbeat_boot"; then
    kill -TERM "$heartbeat_pid" 2>/dev/null || true
    heartbeat_attempts=0
    while collector_alive "$heartbeat_pid" "$heartbeat_ticks" "$heartbeat_boot" && [ "$heartbeat_attempts" -lt 10 ]; do
      sleep 0.1; heartbeat_attempts=$((heartbeat_attempts + 1))
    done
  fi
fi
grep -F "HETERO_TRACE_OWNER_$claim_token" "$claim_root/trace" >/dev/null 2>&1 || {
  trace_result protected ownership_marker_missing; exit 0;
}
mkdir -p "$claim_step/collector_processes" || { trace_result pending output_unavailable; exit 0; }
recovered="$claim_step/trace.recovered.$session_token.ftrace"
hitrace --trace_finish -o "$recovered" > "$claim_step/hitrace_recovery.$session_token.log" 2>&1 &
finish_pid=$!; finish_ticks=$(collector_ticks "$finish_pid")
printf '%s %s %s %s\n' "$finish_pid" "$finish_ticks" "$device_boot" "$claim_token" > "$claim_step/collector_processes/trace_finisher"
attempts=0
while collector_alive "$finish_pid" "$finish_ticks" "$device_boot" && [ "$attempts" -lt 30 ]; do sleep 0.1; attempts=$((attempts + 1)); done
if collector_alive "$finish_pid" "$finish_ticks" "$device_boot"; then
  kill -TERM "$finish_pid" 2>/dev/null || true
  trace_result pending finish_timeout
  exit 0
fi
wait "$finish_pid"; finish_status=$?
[ "$finish_status" = 0 ] && [ "$(cat "$claim_root/tracing_on" 2>/dev/null)" = 0 ] || {
  trace_result pending finish_unverified; exit 0;
}
rm -f "$claim_step/trace.owner" "$global_trace_claim"
trace_result recovered "$recovered"
'''


# Desktop direct-HDC capture freezes one explicitly selected, privately owned
# device for the whole run.  In that narrow mode an old tool/version may have
# left tracefs active without enough ownership evidence for the conservative
# shared-device reconciliation above.  Finish that trace through hitrace (the
# same supported stop path used by normal recovery), verify tracing_on=0, and
# retain the discarded trace under the new session root for diagnosis.  This
# path is never enabled by bridge/shared execution.
_PRIVATE_TRACE_PREFLIGHT_SHELL = _PROCESS_HELPERS + r'''
trace_result() { printf 'HETERO_PRIVATE_TRACE_PREFLIGHT|%s|%s\n' "$1" "${2:-}"; }
private_hitrace_log_ok() {
  [ -r "$1" ] || return 1
  private_hitrace_log=$(cat "$1" 2>/dev/null) || return 1
  # HiTrace releases in the field do not consistently return non-zero when
  # the service rejects Close().  Match its error tokens, but do not classify
  # benign summaries such as "0 failures" as an error.
  case "$private_hitrace_log" in
    *"errorCode("*|*"ErrorCode("*|*"error:"*|*"Error:"*|*"ERROR:"*) return 1;;
  esac
  return 0
}
private_trace_roots_idle() {
  # The root selected before reset must remain readable; skipping it here
  # would turn a permission or mount transition into a false recovery.
  private_trace_state=$(cat "$trace_root/tracing_on" 2>/dev/null) || return 1
  [ "$private_trace_state" = 0 ] || return 1
  for private_trace_candidate in $trace_root_words; do
    [ "$private_trace_candidate" = "$trace_root" ] && continue
    [ -r "$private_trace_candidate/tracing_on" ] || continue
    private_trace_state=$(cat "$private_trace_candidate/tracing_on" 2>/dev/null) || return 1
    [ "$private_trace_state" = 0 ] || return 1
  done
  return 0
}
private_stop_finisher() {
  private_stop_pid=$1; private_stop_ticks=$2; private_stop_ledger=$3
  if collector_alive "$private_stop_pid" "$private_stop_ticks" "$device_boot"; then
    # Re-check exact PID/start-time/boot identity immediately before every
    # signal.  A stale ledger never authorizes signalling a reused PID.
    kill -TERM "$private_stop_pid" 2>/dev/null || true
    private_stop_attempts=0
    while collector_alive "$private_stop_pid" "$private_stop_ticks" "$device_boot" && [ "$private_stop_attempts" -lt 5 ]; do
      sleep 0.1; private_stop_attempts=$((private_stop_attempts + 1))
    done
  fi
  if collector_alive "$private_stop_pid" "$private_stop_ticks" "$device_boot"; then
    kill -KILL "$private_stop_pid" 2>/dev/null || true
    private_stop_attempts=0
    while collector_alive "$private_stop_pid" "$private_stop_ticks" "$device_boot" && [ "$private_stop_attempts" -lt 10 ]; do
      sleep 0.1; private_stop_attempts=$((private_stop_attempts + 1))
    done
  fi
  collector_alive "$private_stop_pid" "$private_stop_ticks" "$device_boot" && return 1
  wait "$private_stop_pid" 2>/dev/null || true
  rm -f "$private_stop_ledger"
  return 0
}
device_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
[ "$(cat "$session_lock/token" 2>/dev/null)" = "$session_token" ] || { trace_result protected session_ownership_lost; exit 0; }
[ "$(cat "$session_lock/boot_id" 2>/dev/null)" = "$device_boot" ] || { trace_result protected session_boot_mismatch; exit 0; }
trace_root=
trace_state=
trace_any_active=0
for candidate in $trace_root_words; do
  [ -r "$candidate/tracing_on" ] || continue
  candidate_state=$(cat "$candidate/tracing_on" 2>/dev/null) || {
    trace_result pending trace_state_unavailable
    exit 0
  }
  case "$candidate_state" in
    0) ;;
    1) trace_any_active=1;;
    *) trace_result pending trace_state_unavailable; exit 0;;
  esac
  if [ -z "$trace_root" ]; then
    trace_root=$candidate
    trace_state=$candidate_state
  fi
done
[ -n "$trace_root" ] || { trace_result pending trace_root_unavailable; exit 0; }
[ "$trace_any_active" = 1 ] && trace_state=1
preflight="$session_root/private_trace_preflight"
mkdir -p "$preflight/collector_processes" || { trace_result pending output_unavailable; exit 0; }
case "$trace_state" in
  0)
    # tracefs can be idle while the HiTrace service still owns the recording
    # session (for example, after tracing_on was cleared outside Close()).
    # Close that service-side session on the explicitly selected private
    # device.  The shared/bridge path never executes this preflight.
    reset_log="$preflight/hitrace_service_reset.$session_token.log"
    hitrace --trace_finish_nodump > "$reset_log" 2>&1 &
    finish_pid=$!; finish_ticks=$(collector_ticks "$finish_pid")
    printf '%s %s %s %s\n' "$finish_pid" "$finish_ticks" "$device_boot" "$session_token" > "$preflight/collector_processes/trace_finisher"
    attempts=0
    while collector_alive "$finish_pid" "$finish_ticks" "$device_boot" && [ "$attempts" -lt 30 ]; do
      sleep 0.1; attempts=$((attempts + 1))
    done
    if collector_alive "$finish_pid" "$finish_ticks" "$device_boot"; then
      private_stop_finisher "$finish_pid" "$finish_ticks" "$preflight/collector_processes/trace_finisher" || {
        trace_result pending service_reset_cleanup_incomplete
        exit 0
      }
      trace_result pending service_reset_timeout
      exit 0
    fi
    wait "$finish_pid"; finish_status=$?
    rm -f "$preflight/collector_processes/trace_finisher"
    [ "$finish_status" = 0 ] && private_hitrace_log_ok "$reset_log" || {
      trace_result pending service_reset_unverified
      exit 0
    }
    private_trace_roots_idle || { trace_result pending service_reset_trace_active; exit 0; }
    # A claim from a prior device boot or a completed trace is only metadata
    # after both tracefs and the service-side recording session are idle.
    rm -f "$global_trace_claim"
    trace_result recovered "$reset_log"
    exit 0
    ;;
  1) ;;
  *) trace_result pending trace_state_unavailable; exit 0;;
esac
recovered="$preflight/trace.recovered.$session_token.ftrace"
hitrace --trace_finish -o "$recovered" > "$preflight/hitrace_recovery.$session_token.log" 2>&1 &
finish_pid=$!; finish_ticks=$(collector_ticks "$finish_pid")
printf '%s %s %s %s\n' "$finish_pid" "$finish_ticks" "$device_boot" "$session_token" > "$preflight/collector_processes/trace_finisher"
attempts=0
while collector_alive "$finish_pid" "$finish_ticks" "$device_boot" && [ "$attempts" -lt 30 ]; do
  sleep 0.1; attempts=$((attempts + 1))
done
if collector_alive "$finish_pid" "$finish_ticks" "$device_boot"; then
  private_stop_finisher "$finish_pid" "$finish_ticks" "$preflight/collector_processes/trace_finisher" || {
    trace_result pending finish_cleanup_incomplete
    exit 0
  }
  trace_result pending finish_timeout
  exit 0
fi
wait "$finish_pid"; finish_status=$?
rm -f "$preflight/collector_processes/trace_finisher"
[ "$finish_status" = 0 ] && private_hitrace_log_ok "$preflight/hitrace_recovery.$session_token.log" && private_trace_roots_idle || {
  trace_result pending finish_unverified
  exit 0
}
# Remove only a local claim whose step path belongs to the platform run root.
# The global claim itself is platform metadata and no longer describes the now
# stopped trace, even when an old version wrote a malformed local record.
if [ -f "$global_trace_claim" ]; then
  read claim_root claim_token claim_run claim_step claim_state claim_boot < "$global_trace_claim" || true
  case "$claim_step" in
    "$remote_base"/[a-zA-Z0-9_-]*/[a-zA-Z0-9_-]*)
      if [ -f "$claim_step/trace.owner" ]; then
        read local_root local_token local_state < "$claim_step/trace.owner" || true
        [ "$local_root" = "$claim_root" ] && [ "$local_token" = "$claim_token" ] && rm -f "$claim_step/trace.owner"
      fi
      ;;
  esac
fi
rm -f "$global_trace_claim"
trace_result recovered "$recovered"
'''


class _LeasedClient:
    def __init__(self, session): self.session = session
    def __getattr__(self, name): return getattr(self.session.client, name)
    def check_cancelled(self): self.session.client.check_cancelled()
    def shell(self, command, timeout=40, *, cleanup=False):
        self.session.refresh(timeout, cleanup=cleanup)
        try:
            return self.session.client.shell(command, timeout=timeout, cleanup=cleanup)
        except Exception:
            self.session.invalidate_lease_cache()
            raise
    def collect(self, remote, destination, *, timeout=600, cleanup=True):
        self.session.refresh(timeout, cleanup=cleanup)
        try:
            return self.session.client.collect(remote, destination, timeout=timeout, cleanup=cleanup)
        except Exception:
            self.session.invalidate_lease_cache()
            raise
    def deploy(self, *args, timeout=180, **kwargs):
        self.session.refresh(timeout)
        try:
            return self.session.client.deploy(*args, timeout=timeout, **kwargs)
        except Exception:
            self.session.invalidate_lease_cache()
            raise


class CollectorSession:
    def __init__(self, client, run_id, remote_root, record, *, lock_path=REMOTE_LOCK,
                 terminal_run_ids=(), trace_roots=TRACE_ROOTS, on_change=None,
                 private_device=False):
        self.client, self.run_id, self.remote_root = client, _identity(run_id), _path(remote_root)
        if PurePosixPath(remote_root).name != run_id: raise ValueError("Session output path must end in run identity")
        self.lock_path, self.record = _path(lock_path), record
        self.trace_claim_path = _path(lock_path + GLOBAL_TRACE_CLAIM_SUFFIX)
        self.token, self.owned = uuid.uuid4().hex, False
        self.terminal_run_ids = frozenset(_identity(value) for value in terminal_run_ids)
        self.selected_terminal_run_ids = ()
        self.on_change = on_change
        self.trace_roots = tuple(_path(value) for value in trace_roots)
        self.private_device = private_device is True
        self.host_lease_until = 0.0
        self.lease_client = _LeasedClient(self)
        record.update(schema_version="collector_session/v1", run_id=run_id, token=self.token,
                      status="pending", remote_outputs_retained=True, operations=[],
                      lease_cache_hits=0, lease_remote_refreshes=0, lease_cache_invalidations=0,
                      private_device=self.private_device)

    def invalidate_lease_cache(self):
        self.host_lease_until = 0.0
        self.record["lease_cache_invalidations"] += 1

    def _operation_grace(self):
        return max(0.0, float(getattr(self.client, "operation_timeout_grace_seconds", 3)))

    def _command(self, operation, lease_seconds):
        values = {"operation": operation, "session_lock": self.lock_path, "session_token": self.token,
                  "session_run": self.run_id, "session_root": self.remote_root,
                  "remote_base": str(PurePosixPath(self.remote_root).parent), "lease_seconds": str(lease_seconds),
                  "private_device": "1" if self.private_device else "0",
                  "terminal_runs": "|" + "|".join(self.selected_terminal_run_ids) + "|",
                  "trace_roots": "|" + "|".join(self.trace_roots) + "|",
                  "global_trace_claim": self.trace_claim_path}
        script = _REFRESH_SHELL if operation == "refresh" else _SHELL
        return f"# HETERO_COLLECTOR_SESSION {operation}\n" + "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n" + script

    def _notify(self):
        if self.on_change is not None:
            try: self.on_change()
            except Exception as exc: self.record["notification_error"] = str(exc)[:500]

    def _peek_terminal_owner(self):
        if not self.terminal_run_ids: return
        command = ("# HETERO_COLLECTOR_SESSION inspect\n" + f"session_lock={shlex.quote(self.lock_path)}\n"
            + 'owner=$(cat "$session_lock/owner" 2>/dev/null); printf "HETERO_COLLECTOR_OWNER|%s\\n" "$owner"')
        item = {"operation": "inspect", "command": command, "timeout_seconds": 3, "cleanup": False, "status": "started"}
        self.record["operations"].append(item); self._notify()
        self.client.check_cancelled()
        try:
            output = self.client.shell(command, timeout=3, cleanup=False) or ""
            match = re.fullmatch(r"HETERO_COLLECTOR_OWNER\|([A-Za-z0-9_-]*)\s*", output)
            self.selected_terminal_run_ids = (match[1],) if match and match[1] in self.terminal_run_ids else ()
            item.update(status="completed", output=output[:1000])
        except Exception as exc:
            item.update(status="failed", reason=str(exc)[:1000]); raise
        finally: self._notify()

    def _reconcile_trace_claim(self):
        values = {"session_lock": self.lock_path, "session_token": self.token,
                  "remote_base": str(PurePosixPath(self.remote_root).parent),
                  "trace_roots": "|" + "|".join(self.trace_roots) + "|",
                  "global_trace_claim": self.trace_claim_path}
        command = "# HETERO_TRACE_RECONCILE\n" + "\n".join(
            f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n" + _TRACE_RECONCILE_SHELL
        item = {"operation": "trace_reconcile", "command": command, "timeout_seconds": 8,
                "cleanup": False, "status": "started"}
        self.record["operations"].append(item); self._notify()
        try:
            self.client.check_cancelled()
            output = self.client.shell(command, timeout=8, cleanup=False) or ""
            item["output"] = output[-4000:]
            matches = re.findall(r"(?m)^HETERO_TRACE_RECONCILE\|([a-z_]+)(?:\|([^\n]*))?$", output)
            if len(matches) != 1: raise ValueError("Trace ownership reconciliation response is missing or ambiguous")
            status, reason = matches[0]
            item.update(status=status, reason=reason)
            return status, reason
        except Exception as exc:
            item.update(status="cancelled" if type(exc).__name__ == "CaptureCancelled" else "failed",
                        reason=str(exc)[:1000])
            raise
        finally:
            self._notify()

    def _private_trace_preflight(self):
        values = {"session_lock": self.lock_path, "session_token": self.token,
                  "session_root": self.remote_root,
                  "remote_base": str(PurePosixPath(self.remote_root).parent),
                  "trace_root_words": " ".join(self.trace_roots),
                  "global_trace_claim": self.trace_claim_path}
        command = "# HETERO_PRIVATE_TRACE_PREFLIGHT\n" + "\n".join(
            f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n" + _PRIVATE_TRACE_PREFLIGHT_SHELL
        item = {"operation": "private_trace_preflight", "command": command,
                "timeout_seconds": 8, "cleanup": False, "status": "started"}
        self.record["operations"].append(item); self._notify()
        try:
            self.client.check_cancelled()
            output = self.client.shell(command, timeout=8, cleanup=False) or ""
            item["output"] = output[-4000:]
            matches = re.findall(r"(?m)^HETERO_PRIVATE_TRACE_PREFLIGHT\|([a-z_]+)(?:\|([^\n]*))?$", output)
            if len(matches) != 1: raise ValueError("Private-device trace preflight response is missing or ambiguous")
            status, reason = matches[0]
            item.update(status=status, reason=reason)
            if status == "recovered": self.record["private_trace_recovery_path"] = reason
            return status, reason
        except Exception as exc:
            item.update(status="cancelled" if type(exc).__name__ == "CaptureCancelled" else "failed",
                        reason=str(exc)[:1000])
            raise
        finally:
            self._notify()

    def _run(self, operation, *, cleanup=False, operation_timeout=8):
        previous_deadline = self.host_lease_until
        duration = max(90, math.ceil(operation_timeout + self._operation_grace()) + 60,
                       math.ceil(max(0, previous_deadline - time.monotonic())))
        command = self._command(operation, duration)
        timeout = 3 if operation == "refresh" else 12
        item = {"operation": operation, "command": command, "timeout_seconds": timeout,
                "cleanup": cleanup, "status": "started"}
        self.record["operations"].append(item)
        self.record.update(operation=operation, message={"acquire": "检查设备采集会话并回收残留采集器", "refresh": "更新设备采集会话租约", "recover": "回收本次采集器并保留输出", "release": "核验采集器已停止并释放设备会话"}[operation])
        self._notify()
        try:
            if not cleanup: self.client.check_cancelled()
            # A missing/failed response cannot leave an earlier success cached.
            self.host_lease_until = 0.0
            started = time.monotonic()
            if operation == "refresh": self.record["lease_remote_refreshes"] += 1
            output = self.client.shell(command, timeout=timeout, cleanup=cleanup) or ""
            item["output"] = output[-12000:]
            matches = re.findall(r"(?m)^HETERO_COLLECTOR_SESSION\|([a-z_]+)(?:\|([^\n]*))?$", output)
            if len(matches) != 1: raise ValueError("Collector session response is missing or ambiguous")
            status, reason = matches[0]
            item.update(status=status, reason=reason)
            self.record.update(status=status, reason=reason)
            if status in {"acquired", "refreshed", "recovered"}:
                # Device uptime is truncated to seconds. Counting from the RPC
                # start and reserving that fraction never adds response latency
                # to the usable lease. The remote renewal cannot shorten it.
                self.owned = True
                self.host_lease_until = max(previous_deadline, started + duration - 1)
            elif status == "released": self.owned = False
            return status
        except Exception as exc:
            self.invalidate_lease_cache()
            item.update(status="cancelled" if type(exc).__name__ == "CaptureCancelled" else "failed", reason=str(exc)[:1000])
            self.record.update(status=item["status"], reason=item["reason"])
            if type(exc).__name__ == "CaptureCancelled" and not cleanup: raise
            if cleanup: return "pending"
            raise
        finally: self._notify()

    def acquire(self):
        self._peek_terminal_owner()
        status = self._run("acquire")
        # The exact terminal run can race with the last collector exiting after
        # TERM.  Retry that recovery once after a short host-side grace; routine
        # acquisitions and unrelated busy sessions never gain another HDC RPC.
        if (status != "acquired" and self.private_device and self.selected_terminal_run_ids
                and self.record.get("reason") == "collector_recovery_incomplete"):
            self.record["recovery_retry_count"] = self.record.get("recovery_retry_count", 0) + 1
            self._notify()
            time.sleep(0.15)
            status = self._run("acquire")
        if status != "acquired":
            raise CapturePreparationError("SCENARIO_COLLECTOR_SESSION_BUSY", "设备采集会话尚未就绪：" + self.record.get("reason", "unknown"),
                "平台会保留已有输出；请查看采集准备中的会话归属及残留回收记录。", phase="collector_preparation")
        if self.record.get("reason") == "trace_claim":
            try:
                trace_status, trace_reason = self._reconcile_trace_claim()
            except Exception:
                self._run("release", cleanup=True)
                raise
            if trace_status not in {"clear", "recovered"} and not self.private_device:
                self._run("release", cleanup=True)
                self.record.update(status="pending", reason="trace_" + (trace_reason or trace_status))
                raise CapturePreparationError("SCENARIO_COLLECTOR_SESSION_BUSY",
                    "设备上一次平台 Trace 的归属核验尚未完成：" + (trace_reason or trace_status),
                    "平台只会回收同时具备会话记录、设备启动标识和 Trace 内所有权标记的采集；其他 Trace 会原样保留。",
                    phase="collector_preparation")
        if self.private_device:
            try:
                trace_status, trace_reason = self._private_trace_preflight()
            except Exception:
                self._run("release", cleanup=True)
                raise
            if trace_status not in {"clear", "recovered"}:
                self._run("release", cleanup=True)
                self.record.update(status="pending", reason="private_trace_" + (trace_reason or trace_status))
                raise CapturePreparationError("SCENARIO_COLLECTOR_SESSION_BUSY",
                    "所选私有设备的残留 Trace 未能安全结束：" + (trace_reason or trace_status),
                    "平台已停止本次采样；请查看 private_trace_preflight 的命令、Trace 状态与回收记录。",
                    phase="collector_preparation")
        return self.record

    def refresh(self, operation_timeout=8, *, cleanup=False, force=False):
        """Cover the next operation; force renews and verifies ownership now."""
        try:
            if not cleanup: self.client.check_cancelled()
        except Exception:
            self.invalidate_lease_cache()
            raise
        required = operation_timeout + self._operation_grace() + LEASE_RENEWAL_MARGIN_SECONDS
        remaining = max(0, self.host_lease_until - time.monotonic())
        self.record.update(lease_required_seconds=required, lease_remaining_seconds=round(remaining, 3))
        if not force and self.owned and remaining >= required:
            self.record["lease_cache_hits"] += 1
            return self.record
        if self._run("refresh", cleanup=cleanup, operation_timeout=operation_timeout) != "refreshed":
            raise CapturePreparationError("SCENARIO_COLLECTOR_SESSION_LOST", "设备采集会话归属已变化，停止后续采集操作。",
                "查看采集会话及残留回收记录。", phase="collector_preparation")
        if self.host_lease_until - time.monotonic() < required:
            self.invalidate_lease_cache()
            raise CapturePreparationError("SCENARIO_COLLECTOR_SESSION_LOST", "设备会话续租响应耗时过长，剩余租期不足以覆盖下一次操作。",
                "请检查设备连接及 Bridge 响应后重试。", phase="collector_preparation")
        return self.record

    def recover(self):
        self._run("recover", cleanup=True)
        return self.record

    def release(self):
        self._run("release", cleanup=True)
        return self.record
