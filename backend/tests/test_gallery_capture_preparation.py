"""Page-state regressions; replay synthetic UI structure without a real phone."""
from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path

from backend.core import ArtifactStore
from backend.scenario_capture import CaptureCancelled, ScenarioCaptureManager
from backend.tests.test_scenario_capture import SimulatedDeviceClient
from tools.scenario_capture.gallery import CapturePreparationError, prepare_unlocked_page, screen_state, wake_display_before_capture
from tools.scenario_capture.runner import execute_plan, prepare_gallery_launch, wait_for_step_ready


BUNDLE = "com.huawei.hmos.photos"


def node(identifier: str, *, bundle: str = BUNDLE, kind: str = "Column", visible: str = "true") -> dict:
    return {"attributes": {"id": identifier, "bundleName": bundle, "type": kind,
                           "bounds": "[40,200][240,400]", "visible": visible, "enabled": "true"}, "children": []}


def page(state: str) -> dict:
    # Identifiers reproduced from the failed run layouts; photo data omitted.
    children = {
        "other": [],
        "other_app": [node("OtherAppContent", bundle="com.example.reader")],
        "locked": [node("ScreenLockRootComponent", bundle="com.ohos.sceneboard")],
        "locked_credentials": [node("ScreenLockRootComponent", bundle="com.ohos.sceneboard"),
                               node("PasswordInputView", bundle="com.ohos.sceneboard", kind="TextInput")],
        "desktop": [node("SCBDesktop_Flex_Desktop", bundle="com.ohos.sceneboard"),
                    node("SwiperPage_Grid_WorkSpace_0", bundle="com.ohos.sceneboard")],
        "usb_dialog": [node("session-UsbFunctionSwitchExtAbility-123", bundle="com.ohos.sceneboard")],
        "gallery_home": [node("TabPhotos"), node("TabAlbums")],
        "gallery_browser": [node("PhotoBrowserBg"), node("AccessibilityComponent_BrowserBack"), node("ToolBarButtonEdit")],
        "gallery_editor": [node("petalClip_bottomToolBar_4")],
        "gallery_dialog": [node("PhotoBrowserBg"), node("AccessibilityComponent_BrowserBack"), node("confirm", kind="AlertDialog")],
        "gallery_other": [node("unrecognized-page")],
    }[state]
    return {"attributes": {"bounds": "[0,0][1280,2832]"}, "children": children}


class PageReplayClient(SimulatedDeviceClient):
    def __init__(self, initial_state: str, *, after_launch: str = "gallery_home") -> None:
        super().__init__()
        self.state = initial_state
        self.after_launch = after_launch
        self.back_count = 0
        self.running = True
        self.reset_stopped = False

    def shell(self, command: str, timeout: int = 40, *, cleanup: bool = False) -> str:
        result = super().shell(command, timeout, cleanup=cleanup)
        if command.startswith("aa force-stop "):
            self.state, self.running, self.reset_stopped = "desktop", False, True
            result = "force stop process successfully."
        if command.startswith("aa start ") and self.reset_stopped:
            self.state, self.running, self.reset_stopped = self.after_launch, True, False
            result = "start ability successfully."
        if command.startswith("pidof "):
            result = "123" if self.running else ""
        if command.startswith("uitest uiInput click"):
            self.back_count += 1
            self.state = "gallery_home"
        if command == "uitest uiInput keyEvent Home":
            self.state = "desktop"
        if command.startswith("sh "):
            source = self.files[shlex.split(command)[1]].decode()
            if "command_action LAUNCH aa start" in source:
                self.state = self.after_launch
                self.running = True
            if "command_action CLICK_EDIT" in source:
                self.state = "gallery_editor"
        if command.startswith("uitest dumpLayout"):
            self.files[shlex.split(command)[-1]] = json.dumps(page(self.state)).encode()
        return result


class UnlockReplayClient(PageReplayClient):
    def __init__(self, *, after_wake="locked", after_swipe="desktop", after_home="desktop"):
        super().__init__("locked")
        self.after_wake = after_wake
        self.after_swipe = after_swipe
        self.after_home = after_home
        self.swipes = 0

    def shell(self, command, timeout=40, *, cleanup=False):
        if command == "power-shell wakeup":
            self.state = self.after_wake
        elif command.startswith("uitest uiInput swipe "):
            self.swipes += 1
            self.state = self.after_swipe
        elif command == "uitest uiInput keyEvent Home":
            self.state = self.after_home
        result = super().shell(command, timeout, cleanup=cleanup)
        if command == "uitest uiInput keyEvent Home":
            self.state = self.after_home
        return result


class GalleryCapturePreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), object())
        self.profile = self.manager.create_plan({})["profile"]

    def plan(self, *, selected: str = "step_02", mode: str = "auto", edit: bool = False) -> dict:
        second = ({"title": "点击编辑", "operation": "click", "target": "edit", "expected_targets": ["filters"]}
                  if edit else {"title": "测量步骤", "operation": "wait", "seconds": 0})
        return self.manager.create_plan({"template_id": "custom", "steps": [
            {"title": "启动图库", "operation": "launch", "expected_targets": ["photos", "albums"]}, second,
        ], "capture": {"selected_step_ids": [selected], "preparation_mode": mode, "collectors": ["trace"]}})

    def run_plan(self, plan: dict, client: PageReplayClient) -> dict:
        return execute_plan(plan, client, self.root / "run", "capture_test", page_ready_timeout_seconds=0)

    def test_classifier_matches_recorded_lock_usb_and_browser_structure(self) -> None:
        for state in ("locked", "locked_credentials", "desktop", "usb_dialog", "gallery_browser", "gallery_home", "gallery_editor", "gallery_dialog", "gallery_other", "other"):
            with self.subTest(state=state):
                self.assertEqual(screen_state(page(state), self.profile), state)
        tree = page("gallery_home")
        tree["children"].append(node("ScreenLockRootComponent", bundle="com.ohos.sceneboard", visible="false"))
        self.assertEqual(screen_state(tree, self.profile), "gallery_home")

    def test_lock_and_usb_fail_before_actions_with_actionable_metadata(self) -> None:
        for state, code in (("locked", "SCENARIO_SCREEN_LOCKED"), ("usb_dialog", "SCENARIO_USB_DIALOG")):
            with self.subTest(state=state):
                client = PageReplayClient(state)
                manifest = execute_plan(self.plan(), client, self.root / state, "capture_" + state, page_ready_timeout_seconds=0)
                self.assertEqual(manifest["status"], "failed")
                error = manifest["quality"]["errors"][0]
                self.assertEqual(error["code"], code)
                self.assertEqual(error["step_id"], "step_01")
                self.assertEqual(error["step_title"], "启动图库")
                self.assertEqual(error["phase"], "preparation")
                self.assertTrue(error["suggestion"])
                self.assertEqual(manifest["steps"][0]["error"], error)
                self.assertFalse(client.executed)
                self.assertEqual(manifest["cleanup"]["status"], "completed")

    def test_auto_restarts_gallery_home_before_selected_step(self) -> None:
        client = PageReplayClient("gallery_home")
        manifest = self.run_plan(self.plan(), client)
        self.assertEqual(manifest["status"], "succeeded", manifest["quality"])
        preparation_source = client.files[client.executed[0]].decode()
        self.assertNotIn("aa start", preparation_source)
        self.assertNotIn("hitrace --trace_begin", preparation_source)
        self.assertEqual(manifest["steps"][0]["launch_condition"], "already_ready")
        self.assertEqual(client.back_count, 0)
        self.assertTrue(manifest["steps"][0]["gallery_reset"]["process_stop_verified"])
        self.assertEqual(client.commands.count("aa force-stop " + BUNDLE), 1)

    def test_auto_browser_is_closed_and_restarted_outside_capture(self) -> None:
        client = PageReplayClient("gallery_browser")
        manifest = self.run_plan(self.plan(), client)
        self.assertEqual(manifest["status"], "succeeded", manifest["quality"])
        self.assertEqual(client.back_count, 0)
        stop_index = client.commands.index("aa force-stop " + BUNDLE)
        start_index = next(i for i, command in enumerate(client.commands) if command.startswith("aa start "))
        capture_index = client.commands.index("sh " + client.executed[1])
        self.assertLess(stop_index, start_index)
        self.assertLess(start_index, capture_index)
        self.assertEqual(client.commands.count("aa force-stop " + BUNDLE), 1)
        self.assertEqual(manifest["steps"][0]["gallery_reset"]["final_page_state"], "gallery_home")

    def test_editor_and_confirmation_are_not_discarded(self) -> None:
        for state in ("gallery_editor", "gallery_dialog", "gallery_other"):
            client = PageReplayClient(state)
            with self.subTest(state=state), self.assertRaises(CapturePreparationError) as caught:
                prepare_gallery_launch(page(state), self.profile, client, {}, lambda _: page("gallery_home"), captured=False)
            self.assertEqual(caught.exception.code, "SCENARIO_GALLERY_PREPARATION_REQUIRED")
            self.assertFalse(client.commands)

    def test_return_to_home_has_a_bound(self) -> None:
        client = PageReplayClient("gallery_browser")
        with self.assertRaises(CapturePreparationError):
            prepare_gallery_launch(page("gallery_browser"), self.profile, client, {},
                                   lambda _: page("gallery_browser"), captured=False, max_back_steps=2)
        self.assertEqual(client.back_count, 2)

    def test_already_prepared_keeps_browser_and_executes_only_selected_edit(self) -> None:
        client = PageReplayClient("gallery_browser")
        manifest = self.run_plan(self.plan(mode="already_prepared", edit=True), client)
        self.assertEqual(manifest["status"], "succeeded", manifest["quality"])
        self.assertEqual([item["step_id"] for item in manifest["steps"]], ["step_02"])
        self.assertEqual(client.back_count, 0)
        self.assertIn("command_action CLICK_EDIT", client.files[client.executed[0]].decode())

    def test_selected_launch_stops_process_and_never_skips_measurement(self) -> None:
        client = PageReplayClient("gallery_home")
        manifest = self.run_plan(self.plan(selected="step_01"), client)
        self.assertNotEqual(manifest["status"], "failed", manifest["quality"])
        self.assertEqual(client.commands.count("uitest uiInput keyEvent Home"), 1)
        self.assertIn("command_action LAUNCH aa start", client.files[client.executed[0]].decode())
        self.assertIsNone(manifest["steps"][0]["target_pid"])
        self.assertTrue(manifest["steps"][0]["gallery_reset"]["process_stop_verified"])
        self.assertFalse(any(command.startswith("aa start ") for command in client.commands))

    def test_selected_launch_from_other_page_retains_launch_action(self) -> None:
        client = PageReplayClient("other_app")
        manifest = self.run_plan(self.plan(selected="step_01"), client)
        source = client.files[client.executed[0]].decode()
        self.assertIn("command_action LAUNCH aa start", source)
        self.assertIn("hitrace --trace_begin", source)
        self.assertIsNone(manifest["steps"][0]["target_pid"])
        self.assertNotEqual(manifest["status"], "failed", manifest["quality"])

    def test_unknown_gallery_page_can_be_reset_before_selected_launch(self) -> None:
        client = PageReplayClient("gallery_other")
        manifest = self.run_plan(self.plan(selected="step_01"), client)
        self.assertNotEqual(manifest["status"], "failed", manifest["quality"])
        self.assertEqual(manifest["steps"][0]["gallery_reset"]["status"], "verified")
        self.assertIn("command_action LAUNCH aa start", client.files[client.executed[0]].decode())
        self.assertIn("uitest uiInput keyEvent Home", client.commands)

    def test_reset_recovers_editor_and_confirmation_without_clearing_data(self):
        for state in ("gallery_editor", "gallery_dialog", "gallery_other"):
            with self.subTest(state=state):
                client, record = PageReplayClient(state), {}
                tree, prepared = prepare_gallery_launch(page(state), self.profile, client, record,
                    lambda _: page(client.state), captured=False, reset=True)
                self.assertTrue(prepared)
                self.assertEqual(screen_state(tree, self.profile), "gallery_home")
                self.assertEqual(record["gallery_reset"]["status"], "verified")
                self.assertFalse(any("bm clean" in command or "rm " in command for command in client.commands))

    def test_reset_command_failure_or_timeout_does_not_start_gallery(self):
        for problem in ("error: failed to force stop process.", TimeoutError("device command timed out")):
            client, record = PageReplayClient("gallery_editor"), {}
            original = client.shell
            def shell(command, timeout=40, **kwargs):
                if command.startswith("aa force-stop "):
                    client.commands.append(command)
                    if isinstance(problem, Exception): raise problem
                    return problem
                return original(command, timeout, **kwargs)
            client.shell = shell
            with self.subTest(problem=str(problem)), self.assertRaises(CapturePreparationError) as caught:
                prepare_gallery_launch(page("gallery_editor"), self.profile, client, record,
                    lambda _: self.fail("Failed stop must not inspect a restored app"), captured=False, reset=True)
            self.assertEqual(caught.exception.code, "SCENARIO_GALLERY_RESET_FAILED")
            self.assertEqual(record["gallery_reset"]["status"], "failed")
            self.assertFalse(any(command.startswith("aa start ") for command in client.commands))

    def test_reset_checks_process_exit_and_cancellation_before_restart(self):
        for cancelled in (False, True):
            client, record = PageReplayClient("gallery_editor"), {}
            original = client.shell
            def shell(command, timeout=40, **kwargs):
                output = original(command, timeout, **kwargs)
                if command.startswith("pidof "):
                    if cancelled: raise CaptureCancelled("cancelled during reset")
                    return "123"
                return output
            client.shell = shell
            expected = CaptureCancelled if cancelled else CapturePreparationError
            with self.subTest(cancelled=cancelled), self.assertRaises(expected) as caught:
                prepare_gallery_launch(page("gallery_editor"), self.profile, client, record,
                    lambda _: self.fail("Process must stop before restarting"), captured=False, reset=True)
            if not cancelled:
                self.assertEqual(caught.exception.code, "SCENARIO_GALLERY_STOP_UNCONFIRMED")
                self.assertEqual(sum(command.startswith("pidof ") for command in client.commands), 3)
            self.assertFalse(any(command.startswith("aa start ") for command in client.commands))

    def test_reset_restored_popup_uses_bounded_back_without_guessing_buttons(self):
        client, record, reads = PageReplayClient("gallery_editor", after_launch="gallery_dialog"), {}, []
        def read(index):
            reads.append(index)
            return page(client.state)
        with self.assertRaises(CapturePreparationError) as caught:
            prepare_gallery_launch(page("gallery_editor"), self.profile, client, record,
                read, captured=False, reset=True, max_back_steps=2)
        self.assertEqual(caught.exception.code, "SCENARIO_GALLERY_RESET_NOT_READY")
        self.assertEqual(client.commands.count("uitest uiInput keyEvent Back"), 2)
        self.assertEqual(len(reads), 5)
        self.assertEqual(record["gallery_reset"]["final_page_state"], "gallery_dialog")
        self.assertFalse(any("uiInput click" in command for command in client.commands))

    def test_reset_uses_known_gallery_ability_alias_only_for_resolution_failure(self):
        client, record = PageReplayClient("gallery_editor"), {}
        original = client.shell
        def shell(command, timeout=40, **kwargs):
            if command.startswith("aa start -a com.huawei.hmos.photos.MainAbility"):
                client.commands.append(command)
                return "error: The specified ability does not exist."
            return original(command, timeout, **kwargs)
        client.shell = shell
        profile = json.loads(json.dumps(self.profile))
        _, prepared = prepare_gallery_launch(page("gallery_editor"), profile, client, record,
            lambda _: page(client.state), captured=False, reset=True)
        self.assertTrue(prepared)
        self.assertEqual(profile["ability"], "com.huawei.photos.MainAbility")
        self.assertEqual(record["gallery_reset"]["resolved_ability"], profile["ability"])
        self.assertEqual(self.profile["ability"], "com.huawei.hmos.photos.MainAbility")

    def test_non_gallery_and_already_prepared_launch_keep_user_state(self):
        client, record = PageReplayClient("other_app"), {}
        profile = {**self.profile, "bundle": "com.example.reader"}
        _, prepared = prepare_gallery_launch(page("other_app"), profile, client, record,
            lambda _: self.fail("Other applications are not reset"), captured=False, reset=True)
        self.assertFalse(prepared)
        self.assertFalse(client.commands)
        client = PageReplayClient("gallery_editor")
        self.run_plan(self.plan(selected="step_01", mode="already_prepared"), client)
        self.assertFalse(any(command.startswith("aa force-stop") or command == "uitest uiInput keyEvent Home"
                             for command in client.commands))
        self.assertIn("command_action LAUNCH aa start", client.files[client.executed[0]].decode())

    def test_semantic_readiness_retries_layout_only_without_replaying_actions(self) -> None:
        client = PageReplayClient("other")
        attempts = []
        def read(index):
            attempts.append(index)
            return page("other" if index == 0 else "gallery_home")
        step = {"operation": "launch", "expected_targets": ["photos", "albums"]}
        result = wait_for_step_ready(step, self.profile, client, read, timeout_seconds=1)
        self.assertEqual(screen_state(result, self.profile), "gallery_home")
        self.assertEqual(attempts, [0, 1])
        self.assertTrue(all(command.startswith("sleep ") for command in client.commands))

    def test_postcondition_usb_has_specific_error_and_never_retries_actions(self) -> None:
        client = PageReplayClient("usb_dialog")
        with self.assertRaises(CapturePreparationError) as caught:
            wait_for_step_ready({"operation": "launch"}, self.profile, client,
                                lambda _: page("usb_dialog"), timeout_seconds=0)
        self.assertEqual(caught.exception.code, "SCENARIO_USB_DIALOG")
        self.assertEqual(caught.exception.phase, "postcondition")
        self.assertFalse(client.commands)

    def test_failed_postcondition_has_structured_error(self) -> None:
        client = PageReplayClient("other_app", after_launch="other")
        manifest = self.run_plan(self.plan(), client)
        error = manifest["quality"]["errors"][0]
        self.assertEqual(error["code"], "SCENARIO_GALLERY_RESET_NOT_READY")
        self.assertEqual(error["phase"], "preparation")
        self.assertEqual(client.executed, [])

    def test_wake_and_swipe_are_outside_capture_and_fresh_layout_proves_unlock(self):
        client = UnlockReplayClient()
        manifest = self.run_plan(self.plan(), client)
        self.assertEqual(manifest["status"], "succeeded", manifest["quality"])
        preparation = manifest["steps"][0]["screen_preparation"]
        self.assertEqual(preparation["status"], "verified")
        self.assertEqual(preparation["final_state"], "desktop")
        self.assertEqual(preparation["swipes"], 1)
        actions = manifest["steps"][0]["outside_capture_preparation"]
        self.assertEqual([entry["action"] for entry in actions[:4]],
                         ["wake_display_before_capture", "foreground_target_application",
                          "fallback_display_wakeup", "dismiss_swipe_lock"])
        self.assertGreaterEqual(client.commands.count("power-shell wakeup"), 2 * len(manifest["steps"]))
        self.assertTrue(all(step["capture_handoffs"][-1]["screen_preparation"]["status"] == "verified"
                            for step in manifest["steps"]))
        first_script = client.commands.index("sh " + client.executed[0])
        self.assertLess(client.commands.index("power-shell wakeup"), first_script)
        self.assertTrue(any(file["name"].startswith("unlock_") for file in manifest["files"]))
        self.assertFalse(any("keyEvent Power" in command for command in client.commands))

    def test_selected_launch_still_measures_launch_after_no_password_unlock(self):
        client = UnlockReplayClient()
        manifest = self.run_plan(self.plan(selected="step_01"), client)
        self.assertNotEqual(manifest["status"], "failed", manifest["quality"])
        source = client.files[client.executed[0]].decode()
        self.assertIn("command_action LAUNCH aa start", source)
        self.assertIn("hitrace --trace_begin", source)
        self.assertTrue(manifest["steps"][0]["gallery_reset"]["process_stop_verified"])
        self.assertEqual(manifest["steps"][0]["launch_temperature"], "prelaunched_during_screen_preparation")
        self.assertNotIn("power-shell", source)
        self.assertNotIn("uiInput swipe", source)

    def test_already_prepared_unlock_restores_browser_without_replaying_launch(self):
        client = UnlockReplayClient(after_swipe="gallery_browser")
        manifest = self.run_plan(self.plan(mode="already_prepared", edit=True), client)
        self.assertEqual(manifest["status"], "succeeded", manifest["quality"])
        self.assertEqual([step["step_id"] for step in manifest["steps"]], ["step_02"])
        self.assertEqual(client.back_count, 0)
        self.assertNotIn("uitest uiInput keyEvent Home", client.commands)
        self.assertNotIn("aa start", client.files[client.executed[0]].decode())

    def test_usable_page_is_preserved_and_wake_only_may_already_reveal_it(self):
        for state in ("desktop", "gallery_home", "gallery_browser", "gallery_editor", "gallery_dialog", "other_app"):
            with self.subTest(state=state):
                client = PageReplayClient(state)
                tree = page(state)
                result = prepare_unlocked_page(tree, self.profile, client, {}, lambda _: self.fail("No fresh dump is needed"))
                self.assertIs(result, tree)
                self.assertEqual(client.commands, [])
        client = UnlockReplayClient(after_wake="gallery_browser")
        record = {}
        result = prepare_unlocked_page(page("locked"), self.profile, client, record, lambda _: page(client.state), settle_seconds=0)
        self.assertEqual(screen_state(result, self.profile), "gallery_browser")
        self.assertEqual(client.swipes, 0)
        self.assertEqual(client.commands, ["power-shell dump -s", "hidumper -s 3301 -a -a", "power-shell wakeup", "power-shell dump -s", "hidumper -s 3301 -a -a"])

    def test_credentials_stop_and_persistent_usb_fails_without_authentication(self):
        for initial, after, expected in (("locked_credentials", "desktop", "SCENARIO_UNLOCK_AUTH_REQUIRED"),
                                         ("locked", "locked_credentials", "SCENARIO_UNLOCK_AUTH_REQUIRED"),
                                         ("locked", "usb_dialog", "SCENARIO_USB_DIALOG")):
            with self.subTest(initial=initial, after=after):
                client = UnlockReplayClient(after_swipe=after)
                with self.assertRaises(CapturePreparationError) as caught:
                    prepare_unlocked_page(page(initial), self.profile, client, {}, lambda _: page(client.state), settle_seconds=0)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(client.swipes, 0 if initial == "locked_credentials" else 1)
                self.assertFalse(any("click" in command or "inputText" in command for command in client.commands))
                keys = [command for command in client.commands if "keyEvent" in command]
                self.assertEqual(keys, ["uitest uiInput keyEvent Back"] if after == "usb_dialog" else [])
        for identifier, kind in (("PinEntry", "Column"), ("CustomPattern", "PatternLock")):
            tree = page("locked"); tree["children"].append(node(identifier, bundle="com.ohos.sceneboard", kind=kind))
            self.assertEqual(screen_state(tree, self.profile), "locked_credentials")

    def test_obscuring_usb_precedes_lock_and_hidden_credentials_are_ignored(self):
        tree = page("locked")
        tree["children"].extend(page("usb_dialog")["children"])
        client = UnlockReplayClient()
        snapshots = iter([tree, page("desktop")])
        record = {}
        result = prepare_unlocked_page(tree, self.profile, client, record, lambda _: next(snapshots), settle_seconds=0)
        self.assertEqual(screen_state(result, self.profile), "desktop")
        self.assertEqual(client.commands, ["uitest uiInput keyEvent Back"])
        self.assertEqual(record["outside_capture_preparation"][0]["action"], "dismiss_usb_dialog_back")
        tree = page("locked")
        tree["children"].append(node("PasswordInputView", bundle="com.ohos.sceneboard", visible="false"))
        self.assertEqual(screen_state(tree, self.profile), "locked")

    def test_lock_retries_are_bounded_and_unknown_is_not_claimed_unlocked(self):
        client = UnlockReplayClient(after_swipe="locked")
        record = {}
        with self.assertRaises(CapturePreparationError) as caught:
            prepare_unlocked_page(page("locked"), self.profile, client, record, lambda _: page(client.state),
                                  max_swipes=100, settle_seconds=0)
        self.assertEqual(caught.exception.code, "SCENARIO_SCREEN_LOCKED")
        self.assertEqual(client.swipes, 3)
        self.assertEqual(record["screen_preparation"]["status"], "failed")
        client = UnlockReplayClient(after_swipe="other", after_home="other")
        with self.assertRaises(CapturePreparationError) as caught:
            prepare_unlocked_page(page("locked"), self.profile, client, {}, lambda _: page(client.state), settle_seconds=0)
        self.assertEqual(caught.exception.code, "SCENARIO_UNLOCK_UNVERIFIED")
        self.assertNotIn("uitest uiInput keyEvent Home", client.commands)

    def test_unknown_after_unlock_does_not_reset_app_and_animation_requires_fresh_evidence(self):
        client = UnlockReplayClient(after_swipe="other", after_home="desktop")
        with self.assertRaises(CapturePreparationError) as caught:
            prepare_unlocked_page(page("locked"), self.profile, client, {}, lambda _: page(client.state), settle_seconds=0)
        self.assertEqual(caught.exception.code, "SCENARIO_UNLOCK_UNVERIFIED")
        self.assertNotIn("uitest uiInput keyEvent Home", client.commands)
        snapshots = iter([page("locked"), page("other"), page("desktop")])
        client = UnlockReplayClient()
        result = prepare_unlocked_page(page("locked"), self.profile, client, {}, lambda _: next(snapshots), settle_seconds=0)
        self.assertEqual(screen_state(result, self.profile), "desktop")
        self.assertEqual(client.swipes, 0)
        self.assertEqual(sum(command.startswith("aa start ") for command in client.commands), 1)
        self.assertNotIn("uitest uiInput keyEvent Back", client.commands)
        self.assertNotIn("uitest uiInput keyEvent Home", client.commands)

    def test_wake_command_failure_and_invalid_geometry_never_fall_back_to_power(self):
        client = UnlockReplayClient()
        calls, reads = [], []
        def shell(command, **_kwargs):
            calls.append(command)
            return "power-shell: Permission denied"
        def read(index):
            reads.append(index)
            return page("locked")
        client.shell = shell
        with self.assertRaises(CapturePreparationError) as caught:
            prepare_unlocked_page(page("locked"), self.profile, client, {}, read, settle_seconds=0)
        self.assertEqual(caught.exception.code, "SCENARIO_SCREEN_LOCKED")
        self.assertGreaterEqual(len(reads), 7)
        self.assertEqual(calls[:8], ["power-shell dump -s", "hidumper -s 3301 -a -a", "power-shell wakeup", "power-shell dump -s", "hidumper -s 3301 -a -a", "uinput -K -d 2802 -u 2802", "power-shell dump -s", "hidumper -s 3301 -a -a"])
        self.assertTrue(calls[8].startswith("aa start "))
        self.assertTrue(calls[9].startswith("uitest uiInput swipe "))
        self.assertEqual(sum(command.startswith("uitest uiInput swipe ") for command in calls), 3)
        self.assertEqual(calls.count("uitest uiInput keyEvent Back"), 1)
        self.assertFalse(any("keyEvent Power" in command for command in calls))
        client = UnlockReplayClient()
        tree = page("locked"); tree["attributes"]["bounds"] = "[10,20][999,999]"
        with self.assertRaises(ValueError):
            prepare_unlocked_page(tree, self.profile, client, {}, lambda _: page("desktop"))
        self.assertEqual(client.commands, [])

    def test_initial_wake_precedes_first_layout_even_when_gallery_structure_is_visible(self):
        client = PageReplayClient("gallery_home")
        manifest = self.run_plan(self.plan(), client)
        self.assertEqual(manifest["status"], "succeeded", manifest["quality"])
        first_layout = next(i for i, command in enumerate(client.commands) if command.startswith("uitest dumpLayout"))
        self.assertLess(client.commands.index("power-shell wakeup"), first_layout)
        self.assertGreaterEqual(client.commands.count("power-shell wakeup"), 2 * len(manifest["steps"]))
        self.assertEqual(manifest["steps"][0]["initial_display_wakeup"]["status"], "completed")
        self.assertNotIn("uitest uiInput keyEvent Home", client.commands)
        self.assertFalse(any(command.startswith("uitest uiInput swipe") for command in client.commands))

    def test_completed_wake_still_settles_and_reads_before_first_gesture(self):
        client = UnlockReplayClient()
        record = {"initial_display_wakeup": {"status": "completed"}}
        reads = []

        def read_page(_):
            reads.append(client.swipes)
            return page(client.state)

        prepare_unlocked_page(page("locked"), self.profile, client, record, read_page)
        self.assertEqual(client.commands[0], "sleep 1")
        self.assertEqual(reads[0], 0)
        self.assertEqual(client.swipes, 1)
        self.assertEqual(record["screen_preparation"]["observations"][0]["visible_lock_ids"], ["ScreenLockRootComponent"])

    def test_slow_unlock_animation_does_not_trigger_a_second_gesture(self):
        client = UnlockReplayClient(after_swipe="locked")
        reads_after_swipe = 0

        def read_page(_):
            nonlocal reads_after_swipe
            if client.swipes:
                reads_after_swipe += 1
            return page("desktop" if reads_after_swipe >= 5 else "locked")

        result = prepare_unlocked_page(page("locked"), self.profile, client, {}, read_page, settle_seconds=0)
        self.assertEqual(screen_state(result, self.profile), "desktop")
        self.assertEqual(client.swipes, 1)
        self.assertEqual(reads_after_swipe, 5)

    def test_slow_gesture_and_two_fallbacks_scale_for_different_displays(self):
        for width, height in [(1080, 2400), (1440, 3120)]:
            with self.subTest(size=(width, height)):
                client = UnlockReplayClient(after_swipe="locked")
                record = {}

                def read_page(_):
                    snapshot = page("desktop" if client.swipes == 3 else "locked")
                    snapshot["attributes"]["bounds"] = f"[0,0][{width},{height}]"
                    return snapshot

                prepare_unlocked_page(read_page(0), self.profile, client, record, read_page, settle_seconds=0)
                gestures = [entry for entry in record["outside_capture_preparation"] if entry["action"] == "dismiss_swipe_lock"]
                self.assertEqual(len(gestures), 3)
                self.assertEqual([item["target_duration_ms"] for item in gestures], [1900, 550, 900])
                self.assertEqual(gestures[0]["command"][3:7], [round(width*638/1280), round(height*2400/2832), round(width*638/1280), round(height*500/2832)])
                self.assertEqual(gestures[1]["command"][3:7], [round(width*.5), round(height*.82), round(width*.5), round(height*.22)])
                self.assertEqual(gestures[2]["command"][3:7], [round(width*.5), round(height*.92), round(width*.5), round(height*.12)])
                for item in gestures:
                    command = item["command"]
                    actual_duration_ms = (command[4] - command[6]) * 1000 / command[7]
                    self.assertAlmostEqual(actual_duration_ms, item["target_duration_ms"], delta=1)

    def test_swipe_failure_retains_remote_command_error(self):
        client = UnlockReplayClient()
        original_shell = client.shell

        def shell(command, *args, **kwargs):
            if command.startswith("uitest uiInput swipe"):
                raise RuntimeError("uitest: input injection failed (permission denied)")
            return original_shell(command, *args, **kwargs)

        client.shell = shell
        record = {}
        with self.assertRaises(CapturePreparationError) as caught:
            prepare_unlocked_page(page("locked"), self.profile, client, record, lambda _: page(client.state), settle_seconds=0)
        self.assertEqual(caught.exception.code, "SCENARIO_SCREEN_LOCKED")
        self.assertIn("permission denied", record["outside_capture_preparation"][-1]["reason"])
        failures = record["screen_preparation"]["attempt_failures"]
        self.assertEqual([item["action"] for item in failures], ["dismiss_swipe_lock"] * 3)
        self.assertTrue(all("permission denied" in item["reason"] for item in failures))

    def test_credentials_revealed_while_waking_stop_before_any_swipe(self):
        client = UnlockReplayClient()
        with self.assertRaises(CapturePreparationError) as caught:
            prepare_unlocked_page(page("locked"), self.profile, client,
                                  {"initial_display_wakeup": {"status": "completed"}},
                                  lambda _: page("locked_credentials"), settle_seconds=0)
        self.assertEqual(caught.exception.code, "SCENARIO_UNLOCK_AUTH_REQUIRED")
        self.assertEqual(client.swipes, 0)

    def test_layout_transport_failure_is_diagnosed_and_cancellation_is_preserved(self):
        for error in (TimeoutError("Bridge layout read timed out"), CaptureCancelled("cancelled")):
            with self.subTest(error=type(error).__name__):
                client = UnlockReplayClient()
                record = {}

                def read_page(_):
                    raise error

                if isinstance(error, CaptureCancelled):
                    with self.assertRaises(CaptureCancelled):
                        prepare_unlocked_page(page("locked"), self.profile, client, record, read_page, settle_seconds=0)
                else:
                    with self.assertRaises(CapturePreparationError) as caught:
                        prepare_unlocked_page(page("locked"), self.profile, client, record, read_page, settle_seconds=0)
                    self.assertEqual(caught.exception.code, "SCENARIO_SCREEN_PREPARATION_FAILED")
                    self.assertEqual(record["screen_preparation"]["status"], "failed")
                    self.assertIn("timed out", record["screen_preparation"]["failure_reason"])
                self.assertEqual(client.swipes, 0)

    def test_initial_wake_failure_can_continue_only_with_verified_usable_layout(self):
        for state in ("desktop", "gallery_home", "gallery_browser", "other"):
            with self.subTest(state=state):
                client = PageReplayClient(state)
                client.shell = lambda *_args, **_kwargs: "power-shell: not found"
                record = {}
                wake_display_before_capture(client, record)
                self.assertEqual(record["initial_display_wakeup"]["status"], "failed")
                if state == "other":
                    with self.assertRaises(CapturePreparationError):
                        prepare_unlocked_page(page(state), self.profile, client, record, lambda _: self.fail("No gesture is authorized"))
                else:
                    result = prepare_unlocked_page(page(state), self.profile, client, record, lambda _: self.fail("No gesture is needed"))
                    self.assertEqual(screen_state(result, self.profile), state)
                    self.assertEqual(record["initial_display_wakeup"]["continued_on_verified_page"], state)

    def test_selected_launch_browser_is_stopped_without_saving_or_clearing_data(self):
        client = PageReplayClient("gallery_browser")
        manifest = self.run_plan(self.plan(selected="step_01"), client)
        self.assertNotEqual(manifest["status"], "failed", manifest["quality"])
        self.assertEqual(client.back_count, 0)
        self.assertEqual(manifest["steps"][0]["launch_preparation"], "verified_desktop_after_gallery_stop")
        preparation_commands = [" ".join(entry.get("command", [])) for entry in manifest["steps"][0]["outside_capture_preparation"]]
        self.assertIn("aa force-stop " + BUNDLE, preparation_commands)
        self.assertFalse(any("bm clean" in command or "kill " in command or "uiInput click" in command for command in preparation_commands))
        for state in ("gallery_editor", "gallery_dialog", "gallery_other"):
            client = PageReplayClient(state)
            with self.subTest(state=state), self.assertRaises(CapturePreparationError):
                prepare_gallery_launch(page(state), self.profile, client, {}, lambda _: self.fail("No navigation allowed"), captured=True)
            self.assertEqual(client.commands, [])

    def test_selected_launch_home_action_requires_positive_desktop_evidence(self):
        for state in ("gallery_home", "other"):
            client = PageReplayClient("gallery_browser")
            reads = []
            def read(index):
                reads.append(index)
                return page(state)
            with self.subTest(state=state), self.assertRaises(CapturePreparationError) as caught:
                prepare_gallery_launch(page("gallery_browser"), self.profile, client, {}, read, captured=True)
            self.assertEqual(caught.exception.code, "SCENARIO_LAUNCH_REQUIRES_HOME")
            self.assertEqual(client.commands.count("uitest uiInput keyEvent Home"), 1)
            self.assertEqual(reads, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
