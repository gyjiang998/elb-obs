"""Back/USB preparation regressions using synthetic UI and no device transport."""
from __future__ import annotations

from copy import deepcopy
import unittest

from backend.scenario_capture import CaptureCancelled
from tools.scenario_capture.gallery import CapturePreparationError, prepare_unlocked_page, screen_state


BACK = "uitest uiInput keyEvent Back"
BACK_COMPAT = "uitest uiinput keyevent back"
SLOW_SWIPE = "uitest uiInput swipe 638 2400 638 500 1000"
BUNDLE = "com.huawei.hmos.photos"


def ui_node(identifier, *, bundle=BUNDLE, kind="Column", **attributes):
    return {"attributes": {"id": identifier, "bundleName": bundle, "type": kind,
                           "bounds": "[20,100][220,300]", "visible": "true", "enabled": "true",
                           **attributes}, "children": []}


def close_button(**attributes):
    return ui_node("advanced_dialog_button_0", bundle="com.ohos.sceneboard", kind="Button",
                   **{"text": "确定", "clickable": "true", "bounds": "[100,600][300,700]", **attributes})


def ui_page(state, *, button=None):
    nodes = {
        "locked": [ui_node("ScreenLockRootComponent", bundle="com.ohos.sceneboard")],
        "credentials": [ui_node("ScreenLockRootComponent", bundle="com.ohos.sceneboard"),
                        ui_node("PasswordInputView", bundle="com.ohos.sceneboard", kind="TextInput")],
        "desktop": [ui_node("SCBDesktop_Flex_Desktop", bundle="com.ohos.sceneboard")],
        "gallery_home": [ui_node("TabPhotos"), ui_node("TabAlbums")],
        "gallery_browser": [ui_node("PhotoBrowserBg"), ui_node("AccessibilityComponent_BrowserBack")],
        "gallery_editor": [ui_node("petalClip_bottomToolBar_4")],
        "gallery_dialog": [ui_node("PhotoBrowserBg"), ui_node("confirm", kind="AlertDialog")],
        "other": [],
    }
    if state == "usb":
        chooser = ui_node("session-UsbFunctionSwitchExtAbility-123", bundle="com.ohos.sceneboard")
        chooser["children"] = [] if button is None else [button]
        children = [chooser]
    else:
        children = nodes[state]
    return {"attributes": {"bounds": "[0,0][1280,2832]"}, "children": children}


class UiDevice:
    def __init__(self, tree, on_shell=None, on_read=None):
        self.tree = deepcopy(tree)
        self.on_shell, self.on_read = on_shell, on_read
        self.commands = []
        self.reads = 0
        self.cancelled = False

    def check_cancelled(self):
        if self.cancelled:
            raise CaptureCancelled("cancelled fixture")

    def shell(self, command, timeout=40, *, cleanup=False):
        self.check_cancelled()
        self.commands.append(command)
        if self.on_shell:
            result = self.on_shell(self, command)
            if result is not None:
                return result
        return "No Error\n"

    def read(self, _index):
        self.check_cancelled()
        self.reads += 1
        if self.on_read:
            self.on_read(self)
        return deepcopy(self.tree)


class CaptureBackUsbTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"bundle": BUNDLE, "screen_size": [1280, 2832], "targets": {}}
        self.record = {"initial_display_wakeup": {"status": "completed"}}

    def prepare(self, client):
        return prepare_unlocked_page(deepcopy(client.tree), self.profile, client, self.record, client.read,
                                     settle_seconds=0, layout_poll_attempts=2)

    def test_back_fallback_after_slow_swipe_requires_a_fresh_usable_layout(self):
        def respond(client, command):
            if command == BACK:
                self.assertGreaterEqual(client.reads, 1)
                client.tree = ui_page("desktop")
        client = UiDevice(ui_page("locked"), respond)
        result = self.prepare(client)
        self.assertEqual(screen_state(result, self.profile), "desktop")
        self.assertEqual(client.commands, [SLOW_SWIPE, BACK])
        self.assertGreaterEqual(client.reads, 2)
        self.assertEqual(self.record["screen_preparation"]["lock_back_attempts"], 1)
        self.assertEqual(self.record["screen_preparation"]["swipes"], 1)

    def test_successful_back_command_alone_is_not_unlock_proof(self):
        client = UiDevice(ui_page("locked"))
        with self.assertRaises(CapturePreparationError) as raised:
            self.prepare(client)
        self.assertEqual(raised.exception.code, "SCENARIO_SCREEN_LOCKED")
        self.assertEqual(client.commands.count(BACK), 1)
        self.assertNotIn(BACK_COMPAT, client.commands)
        self.assertEqual(sum(command.startswith("uitest uiInput swipe ") for command in client.commands), 3)
        self.assertEqual(self.record["screen_preparation"]["final_state"], "locked")

    def test_lowercase_compatibility_requires_parser_rejection_and_new_locked_layout(self):
        reads_at_rejection = []
        def respond(client, command):
            if command == BACK:
                reads_at_rejection.append(client.reads)
                return "Error: Illegal argument Back"
            if command == BACK_COMPAT:
                self.assertGreater(client.reads, reads_at_rejection[0])
                client.tree = ui_page("desktop")
        client = UiDevice(ui_page("locked"), respond)
        self.prepare(client)
        self.assertEqual(client.commands, [SLOW_SWIPE, BACK, BACK_COMPAT])
        actions = self.record["outside_capture_preparation"]
        self.assertEqual([action["status"] for action in actions], ["completed", "unsupported", "completed"])

    def test_late_unlock_after_parser_error_never_sends_compat_back_into_gallery(self):
        def respond(client, command):
            if command == BACK:
                client.tree = ui_page("gallery_browser")
                return "Error: unknown key Back"
        client = UiDevice(ui_page("locked"), respond)
        result = self.prepare(client)
        self.assertEqual(screen_state(result, self.profile), "gallery_browser")
        self.assertEqual(client.commands, [SLOW_SWIPE, BACK])

    def test_back_failure_allows_distinct_gesture_only_after_a_fresh_locked_layout(self):
        for error in (RuntimeError("permission denied"), TimeoutError("Bridge request timed out")):
            with self.subTest(error=str(error)):
                self.record = {"initial_display_wakeup": {"status": "completed"}}
                reads_when_failed = []
                def respond(client, command):
                    if command == BACK:
                        reads_when_failed.append(client.reads)
                        raise error
                    if command.startswith("uitest uiInput swipe ") and reads_when_failed:
                        self.assertGreater(client.reads, reads_when_failed[0])
                        client.tree = ui_page("desktop")
                client = UiDevice(ui_page("locked"), respond)
                self.prepare(client)
                self.assertEqual(self.record["screen_preparation"]["status"], "verified")
                self.assertIn(str(error), self.record["screen_preparation"]["attempt_failures"][0]["reason"])
                self.assertEqual(client.commands[:2], [SLOW_SWIPE, BACK])
                self.assertNotIn(BACK_COMPAT, client.commands)
                self.assertEqual(sum(command.startswith("uitest uiInput swipe ") for command in client.commands), 2)

    def test_swipe_failure_allows_back_after_a_fresh_locked_layout(self):
        reads_when_failed = []
        def respond(client, command):
            if command == SLOW_SWIPE:
                reads_when_failed.append(client.reads)
                raise RuntimeError("uitest: permission denied")
            if command == BACK:
                self.assertGreater(client.reads, reads_when_failed[0])
                client.tree = ui_page("desktop")
        client = UiDevice(ui_page("locked"), respond)
        self.prepare(client)
        self.assertEqual(client.commands, [SLOW_SWIPE, BACK])
        self.assertEqual(self.record["screen_preparation"]["status"], "verified")
        self.assertEqual(self.record["screen_preparation"]["attempt_failures"][0]["action"], "dismiss_swipe_lock")

    def test_late_unlock_after_back_failure_does_not_inject_another_gesture(self):
        def respond(client, command):
            if command == BACK:
                client.tree = ui_page("gallery_browser")
                raise TimeoutError("Bridge response timed out")
        client = UiDevice(ui_page("locked"), respond)
        self.prepare(client)
        self.assertEqual(client.commands, [SLOW_SWIPE, BACK])
        self.assertEqual(self.record["screen_preparation"]["final_state"], "gallery_browser")

    def test_usb_back_failure_allows_scoped_close_button_after_fresh_dialog_read(self):
        reads_when_failed = []
        def respond(client, command):
            if command == BACK:
                reads_when_failed.append(client.reads)
                raise TimeoutError("Bridge response timed out")
            if command == "uitest uiInput click 200 650":
                self.assertGreater(client.reads, reads_when_failed[0])
                client.tree = ui_page("desktop")
        client = UiDevice(ui_page("usb", button=close_button()), respond)
        self.prepare(client)
        self.assertEqual(client.commands, [BACK, "uitest uiInput click 200 650"])
        self.assertNotIn(BACK_COMPAT, client.commands)

    def test_usb_back_restores_original_gallery_without_lock_back_or_swipe(self):
        def respond(client, command):
            if command == BACK:
                client.tree = ui_page("gallery_browser")
        client = UiDevice(ui_page("usb"), respond)
        result = self.prepare(client)
        self.assertEqual(screen_state(result, self.profile), "gallery_browser")
        self.assertEqual(client.commands, [BACK])
        self.assertEqual(self.record["screen_preparation"]["lock_back_attempts"], 0)

    def test_usb_over_lock_is_closed_then_lock_gets_its_own_verified_back(self):
        def respond(client, command):
            if command == BACK:
                client.tree = ui_page("locked" if client.commands.count(BACK) == 1 else "desktop")
        client = UiDevice(ui_page("usb"), respond)
        self.prepare(client)
        self.assertEqual(client.commands, [BACK, SLOW_SWIPE, BACK])
        actions = self.record["outside_capture_preparation"]
        self.assertEqual([action["action"] for action in actions], ["dismiss_usb_dialog_back", "dismiss_swipe_lock", "dismiss_lock_back"])

    def test_usb_appearing_after_lock_back_is_closed_once(self):
        def respond(client, command):
            if command == BACK:
                client.tree = ui_page("usb" if client.commands.count(BACK) == 1 else "desktop")
        client = UiDevice(ui_page("locked"), respond)
        self.prepare(client)
        self.assertEqual(client.commands, [SLOW_SWIPE, BACK, BACK])
        self.assertEqual(self.record["screen_preparation"]["usb_dismiss_attempts"], 1)

    def test_usb_confirmation_uses_fresh_scoped_button_coordinates(self):
        initial = ui_page("usb", button=close_button())
        initial["children"].append(close_button(bounds="[700,900][900,1000]"))
        def respond(client, command):
            if command == BACK:
                client.tree["children"][0]["children"][0]["attributes"]["bounds"] = "[400,800][600,900]"
            if command == "uitest uiInput click 500 850":
                client.tree = ui_page("desktop")
        client = UiDevice(initial, respond)
        self.prepare(client)
        self.assertEqual(client.commands, [BACK, "uitest uiInput click 500 850"])
        self.assertEqual(self.record["outside_capture_preparation"][-1]["target"]["bounds"], [400, 800, 600, 900])

    def test_usb_confirmation_rejects_unrelated_ambiguous_disabled_or_hidden_buttons(self):
        for kind in ("outside", "duplicate", "text", "clickable", "disabled", "hidden", "bounds"):
            with self.subTest(kind=kind):
                self.record = {"initial_display_wakeup": {"status": "completed"}}
                tree = ui_page("usb", button=close_button())
                button = tree["children"][0]["children"][0]
                if kind == "outside":
                    tree["children"][0]["children"] = []
                    tree["children"].append(button)
                elif kind == "duplicate":
                    tree["children"][0]["children"].append(deepcopy(button))
                else:
                    field, value = {"text": ("text", "文件传输"), "clickable": ("clickable", "false"),
                                    "disabled": ("enabled", "false"), "hidden": ("visible", "false"),
                                    "bounds": ("bounds", "[1200,800][1400,900]")}[kind]
                    button["attributes"][field] = value
                client = UiDevice(tree)
                with self.assertRaises(CapturePreparationError) as raised:
                    self.prepare(client)
                self.assertEqual(raised.exception.code, "SCENARIO_USB_DIALOG")
                self.assertEqual(client.commands, [BACK])

    def test_recurring_usb_is_not_an_unbounded_back_loop(self):
        def respond(client, command):
            if command == BACK:
                client.tree = ui_page("locked" if client.commands.count(BACK) == 1 else "usb")
        client = UiDevice(ui_page("usb"), respond)
        with self.assertRaises(CapturePreparationError) as raised:
            self.prepare(client)
        self.assertEqual(raised.exception.code, "SCENARIO_USB_DIALOG")
        self.assertEqual(client.commands, [BACK, SLOW_SWIPE, BACK])

    def test_successful_confirmation_command_cannot_prove_usb_dialog_closed(self):
        client = UiDevice(ui_page("usb", button=close_button()))
        with self.assertRaises(CapturePreparationError) as raised:
            self.prepare(client)
        self.assertEqual(raised.exception.code, "SCENARIO_USB_DIALOG")
        self.assertEqual(client.commands, [BACK, "uitest uiInput click 200 650"])
        self.assertEqual(self.record["screen_preparation"]["final_state"], "usb_dialog")
        self.assertEqual(self.record["screen_preparation"]["status"], "failed")

    def test_initial_or_revealed_credentials_stop_navigation(self):
        client = UiDevice(ui_page("credentials"))
        with self.assertRaises(CapturePreparationError) as raised:
            self.prepare(client)
        self.assertEqual(raised.exception.code, "SCENARIO_UNLOCK_AUTH_REQUIRED")
        self.assertEqual(client.commands, [])
        def respond(client, command):
            if command == BACK:
                client.tree = ui_page("credentials")
        client = UiDevice(ui_page("usb"), respond)
        with self.assertRaises(CapturePreparationError) as raised:
            self.prepare(client)
        self.assertEqual(raised.exception.code, "SCENARIO_UNLOCK_AUTH_REQUIRED")
        self.assertEqual(client.commands, [BACK])

    def test_usable_initial_pages_do_not_receive_back(self):
        for state in ("desktop", "gallery_home", "gallery_browser", "gallery_editor", "gallery_dialog"):
            with self.subTest(state=state):
                client = UiDevice(ui_page(state))
                self.prepare(client)
                self.assertEqual(client.commands, [])
                self.assertEqual(client.reads, 0)

    def test_empty_layout_is_not_unlock_proof_and_does_not_navigate_blindly(self):
        client = UiDevice(ui_page("other"))
        with self.assertRaises(CapturePreparationError) as raised:
            self.prepare(client)
        self.assertEqual(raised.exception.code, "SCENARIO_UNLOCK_UNVERIFIED")
        self.assertEqual(client.commands, [])

    def test_cancellation_before_or_after_back_is_not_converted_to_preparation_failure(self):
        for stage in ("before", "after", "read"):
            with self.subTest(stage=stage):
                self.record = {"initial_display_wakeup": {"status": "completed"}}
                def respond(client, command):
                    if command == BACK and stage == "after":
                        client.cancelled = True
                def read(client):
                    if stage == "read" and BACK in client.commands:
                        raise CaptureCancelled("cancelled during layout")
                client = UiDevice(ui_page("locked"), respond, read)
                client.cancelled = stage == "before"
                with self.assertRaises(CaptureCancelled):
                    self.prepare(client)
                self.assertEqual(client.commands, [] if stage == "before" else [SLOW_SWIPE, BACK])

    def test_layout_failure_after_back_stops_without_blind_next_action(self):
        def read(client):
            if BACK in client.commands:
                raise TimeoutError("layout unavailable")
        client = UiDevice(ui_page("locked"), on_read=read)
        with self.assertRaises(CapturePreparationError) as raised:
            self.prepare(client)
        self.assertEqual(raised.exception.code, "SCENARIO_SCREEN_PREPARATION_FAILED")
        self.assertEqual(client.commands, [SLOW_SWIPE, BACK])
        self.assertIn("layout unavailable", self.record["screen_preparation"]["failure_reason"])


if __name__ == "__main__":
    unittest.main()
