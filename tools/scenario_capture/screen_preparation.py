"""Shared screen preparation for every on-device capture scenario.

The generic gate runs before measured actions and accepts only fresh, usable UI
state. Gallery imports remain compatibility facades for older integrations.
"""
import math
import re
import shlex
import time

from .gallery import (
    CapturePreparationError, LOCK_SCREEN_IDS, SYSTEM_UI_BUNDLES, bounds, flatten, point_from_ratio,
    screen_state, usb_close_target, usb_dialog_nodes, verify_page_available,
    verify_screen,
)
from .power_query import query_power_compat


ABILITY_COMPATIBILITY_ALIASES = {
    "com.huawei.hmos.photos": ("com.huawei.photos.MainAbility",),
}
USABLE_PAGE_STATES = {"desktop", "gallery_home", "gallery_browser", "gallery_editor",
                     "gallery_dialog", "target_app", "foreground_app"}


def ability_not_found(reason):
    """Allow a known Ability alias only for an explicit resolution failure."""
    return bool(re.search(r"(?i)ability.{0,60}(?:not found|not exist|does not exist)|"
                          r"(?:not found|not exist|does not exist).{0,60}ability|"
                          r"resolve ability\s+(?:err|error|failed)|ability.{0,30}不存在", reason or ""))


def is_preparation_interruption(exc):
    return (type(exc).__name__ == "CaptureCancelled"
            or getattr(exc, "code", None) == "SCENARIO_SCREEN_PREPARATION_TIMEOUT")


def parse_power_state_dump(output):
    """Parse the current power state, not the historical states in the same dump.

    Power state is independent evidence; it never proves that the UI is unlocked.
    FREEZE and unrecognized/ambiguous output intentionally remain unknown.
    """
    states = re.findall(r"(?im)^\s*Current State:\s*([A-Z_]+)\b", output or "")
    state = states[0].upper() if len(states) == 1 else "UNKNOWN"
    awake = (True if state in {"AWAKE", "DIM"} else
             False if state in {"INACTIVE", "STAND_BY", "DOZE", "SLEEP", "HIBERNATE"} else None)
    return {"power_state": state, "power_awake": awake}


def wake_display_before_capture(client, record, *, reserve_application_fallback=False, power_capabilities=None):
    """Use Power only for a confirmed inactive display; recheck every fallback."""
    client.check_cancelled()
    entry = {"action": "wake_display_before_capture", "command": ["power-shell", "wakeup"],
             "strategy_version": "power_key_stateful/v2", "status": "started",
             "attempts": [], "power_observations": [], "fallback_attempted": False,
             "power_state": "UNKNOWN", "power_awake": None, "power_key_attempted": False,
             "settled_seconds": 0}
    record.setdefault("outside_capture_preparation", []).append(entry)
    record["initial_display_wakeup"] = entry

    def run(command, stage, *, timeout, observation=False):
        client.check_cancelled()
        attempt = {"command": command, "stage": stage, "status": "started"}
        entry["power_observations" if observation else "attempts"].append(attempt)
        try:
            output = client.shell(shlex.join(command), timeout=timeout)
            attempt["output"] = (output or "") if observation else (output or "")[:2000]
            errors = re.sub(r"(?im)^\s*No Error\s*$", "", output or "")
            # A power dump includes historical failure/error reasons. They do
            # not invalidate its current-state line or imply this query failed.
            if not observation and re.search(r"(?i)not found|permission denied|not supported|unsupported|\bfailed\b|\berror\b|"
                         r"illegal argument|invalid (?:argument|parameter|option|command)|unknown (?:command|option|key)|"
                         r"missing parameter|\busage\s*:", errors):
                raise RuntimeError(errors[:300])
            attempt["status"] = "completed"
        except Exception as exc:
            if is_preparation_interruption(exc):
                attempt.update(status="interrupted", reason=str(exc)[:500])
                entry.update(status="interrupted", reason=str(exc)[:500])
                raise
            client.check_cancelled()
            attempt.update(status="failed", reason=str(exc)[:500])
        return attempt

    def query(stage):
        observation, power = query_power_compat(
            lambda command: run(shlex.split(command), stage, timeout=3, observation=True),
            parse_power_state_dump, lambda value: value is not None and value["power_state"] != "UNKNOWN",
            unavailable_reason="No unique current power state was returned", capability_cache=power_capabilities)
        observation.update(power or {"power_state": "UNKNOWN", "power_awake": None})
        entry.update(power_state=observation["power_state"], power_awake=observation["power_awake"])

    query("before_wakeup")
    if reserve_application_fallback:
        # One safe first attempt reaches application foreground and UI
        # verification without redundant power-state round trips. Repeated
        # power-shell queries are particularly expensive through a bridge.
        if entry["power_awake"] is not True:
            confirmed_inactive = entry["power_awake"] is False
            command = (["uitest", "uiInput", "keyEvent", "Power"] if confirmed_inactive else entry["command"])
            entry["power_key_attempted"] = confirmed_inactive
            attempt = run(command, "power_key" if confirmed_inactive else "standard_wakeup", timeout=4)
            entry["command_succeeded"] = attempt["status"] == "completed"
            entry["state_before_wakeup"] = entry["power_state"]
            entry.update(power_state="UNKNOWN", power_awake=None,
                         status="completed" if entry["command_succeeded"] else "failed")
            if entry["status"] == "failed":
                entry["reason"] = attempt.get("reason", "Wakeup command did not complete")
        else:
            entry.update(status="completed", command_succeeded=False)
        entry["application_fallback_reserved"] = True
        return entry
    if entry["power_awake"] is False:
        entry["power_key_attempted"] = True
        run(["uitest", "uiInput", "keyEvent", "Power"], "power_key", timeout=8)
        settled = run(["sleep", "1"], "after_power_settle", timeout=3)
        if settled["status"] == "completed":
            entry["settled_seconds"] = 1
        query("after_power_key")
    if entry["power_awake"] is not True:
        standard = run(entry["command"], "standard_wakeup", timeout=8)
        entry["output"] = standard.get("output", "")[:500]
        # A later wake requires its own settle before the first UI observation.
        entry["settled_seconds"] = 0
        query("after_standard_wakeup")
        if ((standard["status"] == "failed" and entry["power_awake"] is not True)
                or entry["power_awake"] is False):
            entry["fallback_attempted"] = True
            entry["fallback_reason"] = "standard_wakeup_failed" if standard["status"] == "failed" else "power_still_inactive"
            run(["uinput", "-K", "-d", "2802", "-u", "2802"], "dedicated_wakeup_key", timeout=8)
            query("after_wakeup_key")
    entry["command_succeeded"] = any(attempt["status"] == "completed" and attempt["stage"] != "after_power_settle"
                                     for attempt in entry["attempts"])
    # A completed command with unknown power state still requires a fresh UI.
    entry["status"] = "completed" if (entry["power_awake"] is True or
             (entry["command_succeeded"] and entry["power_awake"] is not False)) else "failed"
    if entry["status"] == "failed":
        entry["reason"] = ("Power state remains " + entry["power_state"] if entry["power_awake"] is False else
                           entry["attempts"][-1].get("reason", "Wakeup command did not complete"))
    return entry


def prepared_page_state(tree, profile):
    """Recognize usable apps without relying on Gallery controls or Home."""
    state = screen_state(tree, profile)
    if state == "gallery_other":
        return "target_app"
    if state == "other":
        width, height = profile["screen_size"]
        for node in flatten(tree):
            rectangle = bounds(node.get("bounds"))
            if (node["_visible"] and node["_bundle"] and node["_bundle"] not in SYSTEM_UI_BUNDLES
                    and str(node.get("focused", "true")).lower() != "false" and rectangle):
                left, top, right, bottom = rectangle
                if 0 <= left < right <= width and 0 <= top < bottom <= height:
                    return "foreground_app"
    return state


def _preparation_status(record):
    return record.setdefault("screen_preparation", {
        "strategy_version": "screen_preparation/v7", "status": "preparing",
        "initial_state": "unknown", "swipes": 0, "lock_back_attempts": 0,
        "usb_dismiss_attempts": 0, "observed_states": [], "observations": [],
    })


def prepare_capture_device(profile, client, record, read_page, *, layout_first=False, power_capabilities=None):
    """Shared pre-action gate; read_page(0) captures the fresh initial UI."""
    status = _preparation_status(record)
    try:
        offset = 0
        if layout_first:
            # The runner has verified finite idle protection. Retain a fresh
            # layout at handoff, but enter wake/recovery only on an abnormal UI.
            client.check_cancelled()
            tree = read_page(0)
            if prepared_page_state(tree, profile) in USABLE_PAGE_STATES:
                status["path"] = "fresh_layout"
                return prepare_unlocked_page(tree, profile, client, record, read_page,
                    initial_page_fresh=True, power_capabilities=power_capabilities)
            offset = 1
        status["path"] = "power_and_layout"
        wakeup = wake_display_before_capture(client, record,
                    reserve_application_fallback=bool(profile.get("bundle") and profile.get("ability")),
                    power_capabilities=power_capabilities)
        if wakeup.get("command_succeeded") and wakeup.get("settled_seconds", 0) < 1:
            client.check_cancelled()
            client.shell("sleep 1", timeout=3)
            wakeup["settled_seconds"] = 1
        client.check_cancelled()
        tree = read_page(offset)
        return prepare_unlocked_page(tree, profile, client, record, lambda index: read_page(index + offset),
                                     initial_page_fresh=True, power_capabilities=power_capabilities)
    except Exception as exc:
        status.update(status="cancelled" if type(exc).__name__ == "CaptureCancelled" else
                      "timed_out" if getattr(exc, "code", None) == "SCENARIO_SCREEN_PREPARATION_TIMEOUT" else "failed",
                      failure_reason=str(exc)[:500])
        if getattr(exc, "code", None):
            status["failure_code"] = exc.code
        raise


def prepare_unlocked_page(tree, profile, client, record, read_page, *, max_swipes=3,
                          layout_poll_attempts=2, settle_seconds=0.35, initial_page_fresh=False, power_capabilities=None):
    """Prepare a recognized lock or USB chooser before starting any collectors.

    The fresh UI tree, never a successful command exit, proves the unlocked page.
    Known app pages are preserved; credentials are never entered or dismissed.
    """
    verify_screen(tree, profile, gallery=False)
    state = prepared_page_state(tree, profile)
    usable = USABLE_PAGE_STATES
    initial_wakeup = record.get("initial_display_wakeup", {})
    preparation = record.setdefault("outside_capture_preparation", [])
    if not isinstance(preparation, list):
        preparation = [{"action": str(preparation)}]
        record["outside_capture_preparation"] = preparation
    status = _preparation_status(record)
    status.update(initial_state=state, observed_states=[state])
    read_count = 0

    def fail(code, message, suggestion):
        status["status"] = "failed"
        status["final_state"] = state
        raise CapturePreparationError(code, message, suggestion)

    def observe(stage):
        status["observations"].append({"stage": stage, "state": state,
            "screen_size": list(profile["screen_size"]),
            "usb_dialog_visible": bool(usb_dialog_nodes(tree)),
            "visible_lock_ids": sorted({str(node.get("id", "")) for node in flatten(tree)
                                         if node["_visible"]} & LOCK_SCREEN_IDS)})

    def verify_current_power(stage):
        """A fresh app layout can still be returned by an inactive display."""
        nonlocal state
        def query(command):
            client.check_cancelled()
            observation = {"stage": stage, "command": command, "status": "started"}
            status.setdefault("power_observations", []).append(observation)
            try:
                output = client.shell(command, timeout=3)
                observation.update(output=output or "", status="completed")
            except Exception as exc:
                if is_preparation_interruption(exc):
                    observation.update(status="interrupted", reason=str(exc)[:500])
                    raise
                client.check_cancelled()
                observation.update(status="unavailable", reason=str(exc)[:500])
            return observation
        observation, power = query_power_compat(
            query, parse_power_state_dump, lambda value: value is not None and value["power_state"] != "UNKNOWN",
            unavailable_reason="No unique current power state was returned", capability_cache=power_capabilities)
        observation.update(power or {"power_state": "UNKNOWN", "power_awake": None})
        status["final_power_state"] = observation["power_state"]
        status["final_power_awake"] = observation["power_awake"]
        if observation["power_awake"] is False:
            status["inactive_page_state"] = state
            state = "display_inactive"
        return observation["power_awake"]

    observe("initial")

    if state == "locked_credentials":
        status.update(status="failed", final_state=state)
        verify_page_available(tree, profile)
    if state in usable and (initial_wakeup.get("power_awake") is False or
                           initial_wakeup.get("state_before_wakeup") is not None):
        verify_current_power("initial_usable_page")
    if state in usable:
        if initial_wakeup.get("status") == "failed":
            initial_wakeup["continued_on_verified_page"] = state
        status.update(status="verified", final_state=state, already_ready=True)
        return tree

    def act(action, command, *, allow_unsupported=False, allow_failure=False, **details):
        client.check_cancelled()
        entry = {"action": action, "command": command, "page_state_before": state, "status": "started", **details}
        preparation.append(entry)
        try:
            output = client.shell(shlex.join(map(str, command)), timeout=10)
            entry["output"] = (output or "")[:500]
            errors = re.sub(r"(?im)^\s*No Error\s*$", "", output or "")
            if re.search(r"(?i)not found|permission denied|not supported|unsupported|\bfailed\b|\berror\b|"
                         r"illegal argument|invalid (?:argument|parameter|option|command)|unknown (?:command|option|key)|"
                         r"missing parameter|\busage\s*:", errors) or (
                             action == "foreground_target_application" and ability_not_found(errors)):
                raise RuntimeError(errors[:300])
        except Exception as exc:
            if is_preparation_interruption(exc):
                entry.update(status="interrupted", reason=str(exc)[:500])
                raise
            client.check_cancelled()
            entry.update(status="failed", reason=str(exc)[:500])
            # Some device versions parse only one spelling. A transport or
            # permission failure is not a reason to inject another key.
            if allow_unsupported and re.search(r"(?i)illegal argument|invalid (?:argument|parameter|option|command)|"
                    r"unknown (?:command|option|key)|missing parameter|\busage\s*:|unsupported (?:command|key)|"
                    r"(?:command|key).{0,30}not supported", str(exc)):
                entry["status"] = "unsupported"
                return False
            if allow_failure:
                status.setdefault("attempt_failures", []).append({"action": action, "reason": entry["reason"]})
                return False
            status.update(failure_action=action, failure_reason=entry["reason"])
            fail("SCENARIO_SCREEN_PREPARATION_FAILED", "采集前的亮屏、解锁或弹窗关闭操作未完成：" + entry["reason"][:200],
                 "请查看失败现场与命令记录，核对 power-shell / uitest 权限及连接状态。")
        entry["status"] = "completed"
        return True

    def refresh(*, stage, wait_for_unlock=False, wait_for_usb=False, initial_delay=None):
        nonlocal tree, state, read_count
        # Two observations allow one animation transition without consuming the
        # remote round trips on repeated layouts before a useful gesture.
        deadline = time.monotonic() + 4.0
        for index in range(max(1, min(int(layout_poll_attempts), 2))):
            client.check_cancelled()
            try:
                if settle_seconds > 0:
                    delay = initial_delay if index == 0 and initial_delay is not None else min(settle_seconds, 1)
                    if delay > 0:
                        client.shell("sleep " + str(delay), timeout=3)
                read_count += 1
                tree = read_page(read_count)
                verify_screen(tree, profile, gallery=False)
                state = prepared_page_state(tree, profile)
            except Exception as exc:
                if is_preparation_interruption(exc):
                    raise
                client.check_cancelled()
                status.update(status="failed", final_state=state, failure_action=stage, failure_reason=str(exc)[:500])
                if isinstance(exc, CapturePreparationError):
                    raise
                fail("SCENARIO_SCREEN_PREPARATION_FAILED", "等待亮屏或读取解锁界面失败：" + str(exc)[:200],
                     "请查看失败现场和设备操作诊断，核对连接状态与布局读取结果。")
            status["observed_states"].append(state)
            observe(stage)
            if state == "locked_credentials":
                status["status"] = "failed"
                status["final_state"] = state
                verify_page_available(tree, profile)
            if (state in usable or (state == "locked" and not wait_for_unlock)
                    or (state == "usb_dialog" and not wait_for_usb)):
                break
            if time.monotonic() >= deadline:
                break

    back_command = None

    def back(action, expected_state, counter):
        nonlocal back_command
        commands = [back_command] if back_command else [
            ["uitest", "uiInput", "keyEvent", "Back"], ["uitest", "uiinput", "keyevent", "back"]]
        for command in commands:
            if state != expected_state:
                return
            status[counter] += 1
            completed = act(action, command, allow_unsupported=True, allow_failure=True)
            attempt_status = preparation[-1]["status"]
            # Re-read even after a parser error, before considering a different
            # spelling: a late UI transition must not cause Back in the app.
            refresh(stage="after_" + action, wait_for_unlock=completed and expected_state == "locked",
                    wait_for_usb=completed and expected_state == "usb_dialog")
            if completed:
                back_command = command
                return
            # A transport/permission failure has ambiguous delivery; do not
            # replay Back with another spelling. A fresh matching layout can
            # still authorize the next distinct gesture or USB close button.
            if attempt_status != "unsupported":
                return

    usb_preparations = 0

    def dismiss_usb():
        nonlocal usb_preparations
        if state != "usb_dialog":
            return
        # One dismissal sequence per preparation keeps recurring dialogs from
        # turning into an unbounded navigation loop.
        if usb_preparations == 0:
            usb_preparations += 1
            back("dismiss_usb_dialog_back", "usb_dialog", "usb_dismiss_attempts")
            if state == "usb_dialog":
                target = usb_close_target(tree, profile)
                if target:
                    status["usb_dismiss_attempts"] += 1
                    act("dismiss_usb_dialog_button", ["uitest", "uiInput", "click", *target["point"]],
                        target=target, screen_size=list(profile["screen_size"]))
                    refresh(stage="after_usb_close_button", wait_for_usb=True)
        if state == "usb_dialog":
            fail("SCENARIO_USB_DIALOG", "已尝试关闭 USB 连接方式弹窗，当前布局仍显示弹窗，采集尚未开始。",
                 "请查看失败现场截图、弹窗布局和关闭命令记录，以适配当前设备的弹窗形式。")

    if state == "locked" and not initial_wakeup:
        initial_wakeup = wake_display_before_capture(client, record, power_capabilities=power_capabilities)
    status["wake_settle_seconds"] = 1 if settle_seconds > 0 else 0
    if not initial_page_fresh:
        refresh(stage="after_wakeup", initial_delay=0 if initial_wakeup.get("settled_seconds", 0) >= 1 else 1)
        if state in usable and initial_wakeup.get("power_awake") is False:
            verify_current_power("after_wakeup")
    if state in usable and initial_wakeup.get("status") == "failed":
        initial_wakeup["continued_on_verified_page"] = state
    dismiss_usb()

    # Some no-password HarmonyOS builds only release the lock overlay when an
    # Ability is brought to the foreground. Try this before repeated swipes or
    # expensive state polling, using the current scenario's own application.
    if state not in usable and state != "usb_dialog" and profile.get("bundle") and profile.get("ability"):
        def foreground(ability, *, compatibility_alias=False):
            command = ["aa", "start", "-a", str(ability), "-b", str(profile["bundle"])]
            status["application_launch_attempted"] = True
            completed = act("foreground_target_application", command,
                allow_failure=True, bundle=profile["bundle"], ability=ability, before_capture=True,
                compatibility_alias=compatibility_alias,
                launch_effect="target_application_may_be_started_or_resumed")
            attempt = preparation[-1]
            status["application_launch_completed"] = completed
            if completed:
                status["resolved_launch_ability"] = ability
                if compatibility_alias:
                    status["configured_launch_ability"] = profile["ability"]
                    # The runner supplies its run-local profile copy. Keep
                    # subsequent measured launches consistent with the proven
                    # Ability without changing the stored capture plan.
                    profile["ability"] = ability
            refresh(stage="after_application_foreground", wait_for_unlock=completed)
            dismiss_usb()
            if state in usable:
                verify_current_power("after_application_foreground")
            return completed, attempt.get("reason", "")

        completed, reason = foreground(profile["ability"])
        if not completed and ability_not_found(reason) and state not in usable:
            for ability in ABILITY_COMPATIBILITY_ALIASES.get(str(profile["bundle"]), ()):
                if ability == profile["ability"]:
                    continue
                foreground(ability, compatibility_alias=True)
                break
        status["application_foreground_verified"] = state in usable

    # Complete the non-toggle wake fallbacks only if the fast first wake and
    # Ability launch did not establish a usable page. Command failures remain
    # diagnostic evidence; a fresh page determines whether to continue.
    if state in {"locked", "display_inactive"} and initial_wakeup.get("application_fallback_reserved"):
        attempted = {tuple(item["command"]) for item in initial_wakeup.get("attempts", [])}
        for command in (["power-shell", "wakeup"], ["uinput", "-K", "-d", "2802", "-u", "2802"]):
            if tuple(command) in attempted:
                continue
            act("fallback_display_wakeup", command, allow_failure=True)
            refresh(stage="after_fallback_wakeup", wait_for_unlock=False)
            dismiss_usb()
            if state in usable:
                verify_current_power("after_fallback_wakeup")
            if state not in {"locked", "display_inactive"}:
                break

    # The reference gesture is 638,2400 -> 638,500 on 1280x2832
    # at 1000px/s. Preserve its 1.9s duration while scaling both endpoints.
    gestures = [([638 / 1280, 2400 / 2832], [638 / 1280, 500 / 2832], 1.9),
                ([0.5, 0.82], [0.5, 0.22], 0.55), ([0.5, 0.92], [0.5, 0.12], 0.9)]
    for index, (start, end, duration) in enumerate(gestures[:max(0, min(int(max_swipes), 3))]):
        if state != "locked":
            break
        if index == 1:
            back("dismiss_lock_back", "locked", "lock_back_attempts")
            dismiss_usb()
            if state != "locked":
                break
        # Refresh immediately before every swipe. A late unlock must never
        # turn the next compatibility gesture into scrolling an application.
        if index > 0:
            refresh(stage="before_swipe", initial_delay=0)
        dismiss_usb()
        if state != "locked":
            break
        lower = point_from_ratio(start, profile["screen_size"])
        upper = point_from_ratio(end, profile["screen_size"])
        # UITest takes px/s, not milliseconds. Preserve the target duration
        # across screen sizes, with the two older gestures as fallbacks.
        velocity = max(200, min(40000, round(math.dist(lower, upper) / duration)))
        act("dismiss_swipe_lock", ["uitest", "uiInput", "swipe", *lower, *upper, velocity],
            allow_unsupported=True, allow_failure=True,
            screen_size=list(profile["screen_size"]), start_ratio=start, end_ratio=end,
            target_duration_ms=round(duration * 1000), velocity_px_per_second=velocity)
        status["swipes"] += 1
        refresh(stage="after_swipe", wait_for_unlock=True)
        dismiss_usb()

    if state in usable and initial_page_fresh and not status.get("application_foreground_verified"):
        verify_current_power("after_unlock")
    if state == "display_inactive" or (state == "locked" and initial_page_fresh and
                                       verify_current_power("final_locked_page") is False):
        fail("SCENARIO_DISPLAY_NOT_AWAKE", "已尝试亮屏和启动目标应用，最新电源状态仍为灭屏，采集尚未开始。",
             "请查看应用启动、亮屏命令与最新电源状态记录，核对设备的远程操作权限。")

    if state == "locked":
        attempted = "启动目标应用、" if status.get("application_launch_attempted") else ""
        fail("SCENARIO_SCREEN_LOCKED", f"已尝试亮屏、{attempted}Back 及 {status['swipes']} 次上滑，设备布局仍被识别为锁屏，采集尚未开始。",
             "请查看失败现场截图、应用启动/Back/上滑命令及布局记录，确认设备状态与操作结果。")
    if state not in usable:
        fail("SCENARIO_UNLOCK_UNVERIFIED", "亮屏后当前页面未能通过解锁核验，采集尚未开始。",
             "请查看最新布局与截图，确认设备已进入桌面或当前场景的应用页面。")
    status.update(status="verified", final_state=state)
    return tree
