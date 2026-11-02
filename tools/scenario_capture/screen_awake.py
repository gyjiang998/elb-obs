"""Finite, reversible idle-timeout protection around on-device capture.

The timeout is a delay before automatic screen-off, not a lease that expires by
itself. The runner owns restoration in its cleanup path. Each operation uses a
short RPC timeout and retains evidence, including uncertain writes.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any

from .power_query import query_power_compat

_MAX_TIMEOUT_MS = 2_147_483_647  # The power-shell CLI parses a signed int32.
_COMMAND_FAILURE = re.compile(
    r"(?i)\bfailed\b|\berror\b|not found|permission denied|not supported|unsupported|"
    r"unknown (?:command|option)|invalid (?:argument|parameter|option)|\busage\s*:"
)


def parse_screen_off_timeout(output: str) -> dict[str, int | None] | None:
    """Read the unique current timeout, excluding historical power-state text."""
    lines = re.findall(r"(?im)^\s*ScreenOffTime:\s*([^\r\n]*)", output or "")
    if len(lines) != 1:
        return None
    match = re.fullmatch(
        r"Timeout\s*=\s*(-?\d+)\s*ms(?:\s+OverrideTimeout\s*=\s*(-?\d+)\s*ms)?\s*",
        lines[0], re.IGNORECASE,
    )
    if not match:
        return None
    timeout, override = int(match[1]), int(match[2]) if match[2] is not None else None
    if any(value is not None and not -1 <= value <= _MAX_TIMEOUT_MS for value in (timeout, override)):
        return None
    return {"timeout_ms": timeout, "override_timeout_ms": override}


class ScreenAwakeGuard:
    """Extend idle timeout, refresh it before execution, and restore ownership."""

    def __init__(self, client, record: dict[str, Any], timeout_ms: int, *, capability_cache=None):
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 0 < timeout_ms <= _MAX_TIMEOUT_MS:
            raise ValueError("Screen idle timeout must be a positive signed-32-bit number of milliseconds")
        self.client, self.record = client, record
        self.timeout_ms = timeout_ms
        self.capability_cache = capability_cache
        self._last_verified_set_started: float | None = None
        self.original: dict[str, int | None] | None = None
        self.attempted = False
        record.update(schema_version="screen_awake_guard/v1", status="not_started",
                      requested_timeout_ms=timeout_ms, timeout_ms=timeout_ms,
                      owned=False, restoration_needed=False, restore_status="not_needed",
                      protection_degraded=False, refresh_count=0, operations=[])

    def _run(self, command: str, stage: str, *, cleanup: bool = False,
             observation: bool = False) -> dict[str, Any]:
        entry: dict[str, Any] = {"stage": stage, "command": command, "timeout_seconds": 3,
                                 "cleanup": cleanup, "status": "started", "output": ""}
        self.record["operations"].append(entry)
        started = time.monotonic()
        try:
            if not cleanup:
                self.client.check_cancelled()
            output = self.client.shell(command, timeout=3, cleanup=cleanup) or ""
            entry["output"] = output if observation else output[:4000]
            errors = re.sub(r"(?im)^\s*No Error\s*$", "", output)
            if not observation and _COMMAND_FAILURE.search(errors):
                raise RuntimeError(errors[:500])
            entry["status"] = "completed"
        except Exception as exc:
            cancelled = type(exc).__name__ == "CaptureCancelled"
            entry.update(status="cancelled" if cancelled else "failed", reason=str(exc)[:500])
            if cancelled and not cleanup:
                self.record.update(status="cancelled", reason=str(exc)[:500], owned=False)
                raise
        finally:
            entry["duration_seconds"] = round(time.monotonic() - started, 3)
        return entry

    def _query(self, stage: str, *, cleanup: bool = False) -> dict[str, int | None] | None:
        entry, state = query_power_compat(
            lambda command: self._run(command, stage, cleanup=cleanup, observation=True),
            parse_screen_off_timeout, lambda value: value is not None,
            unavailable_reason="No unique screen-off timeout was returned", output_limit=4000,
            capability_cache=self.capability_cache)
        if state is not None:
            entry["screen_off_timeout"] = state
            self.record["last_observed"] = state
        return state

    def _degrade(self, reason: str) -> dict[str, Any]:
        self.record.update(status="degraded", reason=reason, owned=False,
                           protection_degraded=True, protection_degraded_reason=reason)
        return self.record

    def acquire(self) -> dict[str, Any]:
        if self.attempted:
            return self.record
        self.attempted = True
        self.record["status"] = "acquiring"
        self.original = self._query("original_timeout")
        if self.original is None:
            return self._degrade("Original screen-off timeout is unavailable; settings were left unchanged")
        self.record["original"] = dict(self.original)
        effective = self.original["override_timeout_ms"]
        effective = self.original["timeout_ms"] if effective is None else effective
        if effective is None or effective <= 0 or self.original["timeout_ms"] <= 0:
            return self._degrade("Original screen-off timeout is not finite; settings were left unchanged")
        # Extending protection must not shorten an existing, longer idle delay.
        self.timeout_ms = max(self.timeout_ms, effective)
        self.record.update(timeout_ms=self.timeout_ms, restoration_needed=True, restore_status="pending")
        # Mark restoration before the RPC: a transport timeout/cancellation can
        # occur after the device applied this setting.
        set_started = time.monotonic()
        attempt = self._run(f"power-shell timeout -o {self.timeout_ms}", "acquire_timeout")
        current = self._query("verify_acquire")
        if current is not None and current["override_timeout_ms"] == self.timeout_ms:
            if attempt["status"] == "completed":
                self._last_verified_set_started = set_started
            self.record.update(status="active", owned=True)
            self.record.pop("reason", None)
            return self.record
        if current is not None:
            self.record.update(restoration_needed=False, restore_status="not_needed")
        return self._degrade("Screen-off timeout override could not be verified")

    def refresh(self) -> dict[str, Any]:
        """Reset the idle timer only while the verified override is still ours."""
        if not self.record["owned"] or not self.record["restoration_needed"]:
            self.record["refresh_status"] = "skipped_not_owned"
            return self.record
        current = self._query("before_refresh")
        if current is None:
            self.record["refresh_status"] = "unverified"
            return self._degrade("Current timeout is unavailable; refresh was skipped")
        if current["override_timeout_ms"] != self.timeout_ms:
            self.record.update(refresh_status="skipped_changed_externally", restoration_needed=False,
                               restore_status="skipped_changed_externally")
            return self._degrade("Screen-off timeout changed externally; it was left unchanged")
        self.record["refresh_count"] += 1
        set_started = time.monotonic()
        attempt = self._run(f"power-shell timeout -o {self.timeout_ms}", "refresh_timeout")
        current = self._query("verify_refresh")
        if attempt["status"] == "completed" and current is not None and current["override_timeout_ms"] == self.timeout_ms:
            self._last_verified_set_started = set_started
            self.record.update(status="active", owned=True, refresh_status="completed")
            self.record.pop("reason", None)
            return self.record
        if current is not None and current["override_timeout_ms"] != self.timeout_ms:
            self.record.update(restoration_needed=False, restore_status="skipped_changed_externally")
        self.record["refresh_status"] = "unverified"
        return self._degrade("Idle timer refresh could not be verified")

    def refresh_if_needed(self, *, required_seconds: float) -> dict[str, Any]:
        """Refresh when the last verified write cannot cover the next operation.

        Count elapsed time from before the write RPC, including transport and
        verification. Inputs and layout reads never extend this local estimate.
        ``refresh()`` remains the explicit unconditional ownership check.
        """
        if (isinstance(required_seconds, bool) or not isinstance(required_seconds, (int, float))
                or not math.isfinite(required_seconds) or required_seconds < 0):
            raise ValueError("Required protection must be a finite nonnegative number of seconds")
        self.client.check_cancelled()
        remaining = (self._last_verified_set_started + self.timeout_ms / 1000 - time.monotonic()
                     if self._last_verified_set_started is not None else None)
        self.record.update(refresh_required_seconds=required_seconds,
                           protected_remaining_seconds=round(max(0, remaining), 3) if remaining is not None else None)
        if (self.record["status"] == "active" and self.record["owned"]
                and self.record["restoration_needed"] and remaining is not None
                and remaining >= required_seconds):
            self.record.update(refresh_status="skipped_sufficient_timeout",
                               refresh_skipped_count=self.record.get("refresh_skipped_count", 0) + 1)
            return self.record
        return self.refresh()

    def restore(self) -> dict[str, Any]:
        """Best-effort cleanup, including writes whose RPC outcome was uncertain."""
        if self.original is None or not self.record["restoration_needed"]:
            return self.record
        self.record["restore_status"] = "restoring"
        current = self._query("before_restore", cleanup=True)
        if current is None:
            self.record.update(status="restore_failed", restore_status="unverified", owned=False,
                               reason="Current timeout is unavailable; the temporary override may remain")
            return self.record
        if current == self.original:
            self.record.update(status="restored", restore_status="already_original", owned=False,
                               restoration_needed=False)
            return self.record
        if current["override_timeout_ms"] != self.timeout_ms:
            self.record.update(status="restored", restore_status="skipped_changed_externally", owned=False,
                               restoration_needed=False)
            return self.record
        original_override = self.original["override_timeout_ms"]
        command = "power-shell timeout -r" if original_override is None else f"power-shell timeout -o {original_override}"
        self._run(command, "restore_timeout", cleanup=True)
        restored = self._query("verify_restore", cleanup=True)
        # A user may change the underlying system default during capture. The
        # restore command must preserve that current default, not rewrite it.
        expected = {"timeout_ms": current["timeout_ms"], "override_timeout_ms": original_override}
        if restored == expected:
            self.record.update(status="restored", restore_status="completed", owned=False, restoration_needed=False)
            self.record.pop("reason", None)
        else:
            self.record.update(status="restore_failed", restore_status="unverified", owned=False,
                               reason="Original timeout restoration is unverified; the temporary override may remain")
        return self.record
