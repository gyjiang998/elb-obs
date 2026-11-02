"""Reset Gallery's running session before automatic scenario preparation."""
from __future__ import annotations

import re
import shlex

from .gallery import CapturePreparationError, verify_page_available
from .screen_preparation import ABILITY_COMPATIBILITY_ALIASES, ability_not_found, is_preparation_interruption


GALLERY_BUNDLES = frozenset({"com.huawei.hmos.photos", "com.huawei.photos"})


def reset_gallery_launch(tree, profile, client, record, read_page, *, captured, max_back_steps=4):
    """Stop the existing session, then prove the requested launch precondition.

    A measured launch starts from the desktop with its launch action retained.
    An unmeasured launch prepares the verified Gallery home page directly.
    """
    if profile.get("bundle") not in GALLERY_BUNDLES:
        return tree, False
    state = verify_page_available(tree, profile)
    details = record["gallery_reset"] = {"strategy_version": "gallery_session_reset/v1", "status": "running",
        "bundle": profile["bundle"], "initial_page_state": state, "outside_capture_window": True,
        "captured_launch": captured, "application_data_policy": "preserved", "attempts": []}
    preparation = record.setdefault("outside_capture_preparation", [])

    def fail(code, message, suggestion):
        details.update(status="failed", code=code, final_page_state=state)
        raise CapturePreparationError(code, message, suggestion)

    def check_cancelled():
        try:
            client.check_cancelled()
        except Exception as exc:
            details.update(status="interrupted" if is_preparation_interruption(exc) else "failed", reason=str(exc)[:1000])
            raise

    def run(command, action, *, timeout=8, allow_failure=False, missing_is_ok=False):
        check_cancelled()
        entry = {"action": action, "command": command, "status": "running", "outside_capture_window": True}
        details["attempts"].append(entry)
        preparation.append(entry)
        try:
            output = client.shell(shlex.join(command) + (" || true" if missing_is_ok else ""), timeout=timeout) or ""
            entry["output"] = output[:2000]
            errors = re.sub(r"(?im)^\s*No Error\s*$", "", output)
            if re.search(r"(?i)\berror\b|\bfailed\b|permission denied|not found|unsupported|not supported|"
                         r"unknown (?:command|option|key)|invalid (?:argument|parameter|option)|\busage\s*:", errors):
                raise RuntimeError(errors[:1000])
            entry["status"] = "completed"
            return True, output
        except Exception as exc:
            entry.update(status="failed", reason=str(exc)[:1000])
            if is_preparation_interruption(exc):
                details["status"] = entry["status"] = "interrupted"
                raise
            check_cancelled()
            if allow_failure:
                return False, str(exc)
            fail("SCENARIO_GALLERY_RESET_FAILED", "图库启动准备命令失败，采集尚未开始。",
                 "查看 gallery_reset 中的命令输出；" + str(exc)[:500])

    def observe(attempt):
        check_cancelled()
        try:
            value = read_page(attempt)
            return value, verify_page_available(value, profile)
        except Exception as exc:
            details.update(status="interrupted" if is_preparation_interruption(exc) else "failed",
                           code=getattr(exc, "code", "SCENARIO_GALLERY_RESET_FAILED"), reason=str(exc)[:1000])
            raise

    run(["aa", "force-stop", profile["bundle"]], "stop_gallery_before_restart")
    for attempt in range(3):
        check_cancelled()
        _, observed = run(["pidof", profile["bundle"]], "verify_gallery_stopped", timeout=3, missing_is_ok=True)
        details["pid_after_stop"] = observed.strip()
        if not observed.strip():
            details["process_stop_verified"] = True
            break
        if attempt < 2:
            run(["sleep", "0.2"], "wait_for_gallery_stop", timeout=3)
    else:
        fail("SCENARIO_GALLERY_STOP_UNCONFIRMED", "关闭图库后仍检测到应用进程，采集尚未开始。",
             "查看强制停止命令和进程检查记录；平台尚未执行后续场景动作。")

    if captured:
        run(["uitest", "uiInput", "keyEvent", "Home"], "return_to_desktop_after_gallery_stop")
        for attempt in range(1, 4):
            run(["sleep", "0.35"], "wait_for_desktop", timeout=3)
            tree, state = observe(attempt)
            if state == "desktop":
                details.update(status="verified", final_page_state=state, launch_action="retained_in_capture")
                record["launch_preparation"] = "verified_desktop_after_gallery_stop"
                record["launch_reset_policy"] = "stopped_before_measured_launch"
                return tree, False
        fail("SCENARIO_LAUNCH_REQUIRES_HOME", "图库已关闭，但尚未确认启动测量前的桌面界面。",
             "查看启动准备后的布局与截图；被测启动动作尚未执行。")

    configured = str(profile["ability"])
    launched, reason = run(["aa", "start", "-a", configured, "-b", profile["bundle"]],
                           "restart_gallery_before_scenario", allow_failure=True)
    if not launched and ability_not_found(reason):
        for ability in ABILITY_COMPATIBILITY_ALIASES.get(profile["bundle"], ()):
            if ability == configured:
                continue
            launched, reason = run(["aa", "start", "-a", ability, "-b", profile["bundle"]],
                                   "restart_gallery_compatible_ability", allow_failure=True)
            if launched:
                details.update(configured_ability=configured, resolved_ability=ability)
                # The runner supplies an effective-profile copy; saved plans stay unchanged.
                profile["ability"] = ability
                break
    if not launched:
        fail("SCENARIO_GALLERY_RESTART_FAILED", "图库已关闭，但重新启动失败，采集尚未开始。",
             "检查目标 Ability 与命令输出；" + reason[:500])

    back_steps = 0
    for attempt in range(1, max_back_steps + 4):
        run(["sleep", "0.35"], "wait_for_gallery_home", timeout=3)
        tree, state = observe(attempt)
        details["final_page_state"] = state
        if state == "gallery_home":
            details.update(status="verified", back_steps=back_steps, launch_action="completed_before_capture")
            record["launch_preparation"] = "verified_gallery_home_after_restart"
            return tree, True
        if state.startswith("gallery_") and back_steps < max_back_steps:
            completed, reason = run(["uitest", "uiInput", "keyEvent", "Back"],
                                    "exit_restored_gallery_page", allow_failure=True)
            if not completed and re.search(r"(?i)unknown|unsupported|not supported|invalid|usage", reason):
                completed, reason = run(["uitest", "uiinput", "keyevent", "back"],
                                        "exit_restored_gallery_page_compatible", allow_failure=True)
            if not completed:
                fail("SCENARIO_GALLERY_RESET_FAILED", "图库已重启，但无法退出恢复的页面。", reason[:500])
            back_steps += 1
    fail("SCENARIO_GALLERY_RESET_NOT_READY", "图库已重启，但未确认包含“照片 / 相册”的主页，采集尚未开始。",
         "当前页面状态：" + state + "。请查看准备布局；恢复的编辑页或未知弹窗未能通过返回操作关闭，平台保留应用数据。")
