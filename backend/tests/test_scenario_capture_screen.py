"""Resolution and rotation replays; never connect to a device or use photo text."""
from __future__ import annotations

import copy
import json
import shlex
import tempfile
import unittest
from pathlib import Path

from backend.core import ArtifactStore
from backend.scenario_capture import ScenarioCaptureManager
from backend.tests.test_gallery_capture_preparation import PageReplayClient, page
from tools.scenario_capture.gallery import (
    CapturePreparationError, build_actions, point_from_ratio, select_node,
    sync_screen_geometry, verify_screen,
)
from tools.scenario_capture.runner import execute_plan


def layout(size, state="gallery_home"):
    tree = page(state)
    tree["attributes"]["bounds"] = f"[0,0][{size[0]},{size[1]}]"
    return tree


class GeometryReplayClient(PageReplayClient):
    def __init__(self, snapshots):
        super().__init__("gallery_home")
        self.snapshots = list(snapshots)
        self.layout_count = 0

    def shell(self, command, timeout=40, *, cleanup=False):
        result = super().shell(command, timeout, cleanup=cleanup)
        if command.startswith("uitest dumpLayout"):
            snapshot = self.snapshots[min(self.layout_count, len(self.snapshots) - 1)]
            self.files[shlex.split(command)[-1]] = json.dumps(snapshot).encode()
            self.layout_count += 1
        return result


class DynamicScreenGeometryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), object())
        self.profile = self.manager.create_plan({})["profile"]

    def plan(self, *, operation="swipe", steps=None):
        return self.manager.create_plan({"template_id": "custom", "steps": steps or [
            {"title": "滑动", "operation": operation, "points": [.5, .75, .5, .25], "seconds": 0}],
            "capture": {"selected_step_ids": ["step_01" if not steps else f"step_{len(steps):02d}"], "collectors": ["hilog"]}})

    def run_plan(self, plan, client, suffix="run"):
        return execute_plan(plan, client, self.root / suffix, "capture_" + suffix, page_ready_timeout_seconds=0)

    def actions(self, manifest, step_id, suffix="run"):
        artifact = next(item for item in manifest["files"] if item["name"] == "resolved_actions.json" and item["step_id"] == step_id)
        return json.loads((self.root / suffix / artifact["relative_path"]).read_text())

    def test_dimensions_come_from_root_and_plan_remains_immutable(self):
        for size in ([1080, 2400], [1440, 3120], [2400, 1080]):
            with self.subTest(size=size):
                plan = self.plan()
                plan["profile"].pop("screen_size_mode", None)  # Saved legacy plans also adapt.
                original = copy.deepcopy(plan)
                client = GeometryReplayClient([layout(size)])
                manifest = self.run_plan(plan, client, str(size[0]))
                self.assertNotEqual(manifest["status"], "failed", manifest["quality"])
                self.assertEqual(plan, original)
                self.assertEqual(manifest["profile_effective"]["screen_size"], size)
                self.assertEqual(manifest["profile_effective"]["screen_size_reference"], [1280, 2832])
                geometry = manifest["screen_geometry"]
                self.assertEqual(geometry["status"], "confirmed")
                self.assertEqual(geometry["actual_size"], size)
                self.assertEqual(geometry["reference_size"], [1280, 2832])
                self.assertEqual(geometry["source"], "dumpLayout.root.bounds")
                before = geometry["observations"][:2]
                self.assertEqual([item["confirmed"] for item in before], [False, True])
                self.assertNotEqual(before[0]["layout_file"], before[1]["layout_file"])
                expected = ["uitest", "uiInput", "swipe", round(size[0]*.5), round(size[1]*.75),
                            round(size[0]*.5), round(size[1]*.25), 1000]
                self.assertEqual(self.actions(manifest, "step_01", str(size[0]))[0]["command"], expected)
                action_index = client.commands.index("sh " + client.executed[0])
                self.assertEqual(sum(cmd.startswith("uitest dumpLayout") for cmd in client.commands[:action_index]), 3)

    def test_semantic_control_center_is_not_scaled_from_reference(self):
        for size in ([1080, 2400], [1440, 3120]):
            tree = layout(size, "gallery_browser")
            selected = select_node(tree, {"id": "ToolBarButtonEdit"}, copy.deepcopy(self.profile))
            self.assertEqual(selected["point"], [140, 300])
            self.assertEqual(selected["screen_size"], size)
            self.assertEqual(selected["coordinate_space"], "layout_bounds")

    def test_ratio_controls_zoom_and_scroll_follow_actual_geometry(self):
        profile = copy.deepcopy(self.profile)
        tree = layout([1080, 2400])
        selected = select_node(tree, {"point": [.5, .75]}, profile)
        self.assertEqual(selected["point"], [540, 1800])
        self.assertEqual(selected["coordinate_space"], "normalized_display")
        zoom = build_actions((4, "zoom", "zoom", "zoom", None, []), tree, profile, {})
        self.assertEqual(zoom[0]["command"][:12], ["uinput", "-T", "-m", 454, 1320, 108, 1320, 626, 1320, 972, 1320, 350])
        scroll = build_actions((15, "scroll", "scroll", "scroll", None, []), tree, profile, {})
        self.assertEqual(scroll[0]["command"], ["uitest", "uiInput", "swipe", 540, 1800, 540, 840, 1000])

    def test_no_password_unlock_uses_actual_display_dimensions(self):
        class SwipeUnlockGeometryClient(GeometryReplayClient):
            def shell(self, command, timeout=40, *, cleanup=False):
                result = super().shell(command, timeout, cleanup=cleanup)
                if command.startswith("uitest dumpLayout"):
                    unlocked = any(item.startswith("uitest uiInput swipe") for item in self.commands)
                    self.files[shlex.split(command)[-1]] = json.dumps(
                        layout([1080, 2400], "desktop" if unlocked else "locked")).encode()
                return result

        client = SwipeUnlockGeometryClient([layout([1080, 2400], "locked")])
        manifest = self.run_plan(self.plan(operation="wait"), client)
        self.assertNotEqual(manifest["status"], "failed", manifest["quality"])
        swipe = next(command.split() for command in client.commands if command.startswith("uitest uiInput swipe"))
        self.assertEqual(swipe[:7], ["uitest", "uiInput", "swipe", "538", "2034", "538", "424"])
        self.assertAlmostEqual(1610 / int(swipe[7]), 1.9, delta=.002)
        self.assertEqual(manifest["steps"][0]["screen_preparation"]["final_state"], "desktop")
        self.assertEqual(client.commands.count("power-shell wakeup"), 2)
        self.assertEqual(manifest["steps"][0]["capture_handoffs"][0]["screen_preparation"]["status"], "verified")

    def test_rotation_invalidates_cached_controls_and_confirms_before_next_action(self):
        plan = self.plan(steps=[{"title": "准备", "operation": "wait", "seconds": 0},
                               {"title": "滑动", "operation": "swipe", "points": [.5, .75, .5, .25]}])
        snapshots = [layout([1080, 2400])] * 3 + [layout([2400, 1080], "gallery_browser")] * 3
        client = GeometryReplayClient(snapshots)
        manifest = self.run_plan(plan, client)
        self.assertNotEqual(manifest["status"], "failed", manifest["quality"])
        changed = [item for item in manifest["screen_geometry"]["observations"] if item["changed"]]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["invalidated_cache_keys"], ["albums"])
        self.assertFalse(changed[0]["confirmed"])
        self.assertEqual(manifest["steps"][1]["screen_geometry"]["orientation"], "landscape")
        self.assertTrue(manifest["steps"][1]["screen_geometry"]["confirmed"])
        self.assertEqual(self.actions(manifest, "step_02")[0]["command"],
                         ["uitest", "uiInput", "swipe", 1200, 810, 1200, 270, 1000])

    def test_continuously_changing_geometry_stops_without_replaying_actions(self):
        client = GeometryReplayClient([layout([1080, 2400]), layout([2400, 1080]), layout([1080, 2400])])
        manifest = self.run_plan(self.plan(), client)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["quality"]["errors"][0]["code"], "SCENARIO_SCREEN_GEOMETRY_UNSTABLE")
        self.assertEqual(manifest["screen_geometry"]["status"], "unstable")
        self.assertEqual(client.layout_count, 3)
        self.assertEqual(client.executed, [])

    def test_missing_or_non_display_root_never_uses_reference_or_child_bounds(self):
        for bad in (None, {}, "[10,20][1080,2400]", "[0,0][0,2400]", "[0,0][1080,999999]"):
            tree = layout([1080, 2400]); tree["attributes"]["bounds"] = bad
            with self.subTest(bounds=bad), self.assertRaises(CapturePreparationError) as caught:
                sync_screen_geometry(tree, copy.deepcopy(self.profile))
            self.assertEqual(caught.exception.code, "SCENARIO_SCREEN_GEOMETRY_INVALID")
        client = GeometryReplayClient([{"attributes": {}, "children": [layout([1080, 2400])]}])
        manifest = self.run_plan(self.plan(), client)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["screen_geometry"]["actual_size"], None)
        self.assertEqual(client.executed, [])

    def test_fixed_mode_stays_explicit_and_absolute_points_are_not_scaled(self):
        profile = {**self.profile, "screen_size_mode": "fixed"}
        with self.assertRaises(CapturePreparationError) as caught:
            verify_screen(layout([1080, 2400]), profile)
        self.assertEqual(caught.exception.code, "SCENARIO_SCREEN_SIZE_MISMATCH")
        for point in ([640, 1400], [True, .5], [float("nan"), .5], [.5, float("inf")]):
            with self.subTest(point=point), self.assertRaises(ValueError):
                point_from_ratio(point, [1080, 2400])

    def test_window_root_at_origin_is_not_display_geometry(self):
        for identity in ({"bundleName": "com.huawei.hmos.photos"},
                         {"abilityName": "MainAbility"},
                         {"type": "WindowScene"},
                         {"type": "root", "hostWindowId": "35"},
                         {"type": "root", "hostWindowId": 0},
                         {"hierarchy": "ROOT35", "hostWindowId": "35"}):
            window = layout([720, 1000])
            window["attributes"].update(identity)
            profile = copy.deepcopy(self.profile)
            with self.subTest(identity=identity), self.assertRaises(CapturePreparationError) as caught:
                sync_screen_geometry(window, profile)
            self.assertEqual(caught.exception.code, "SCENARIO_SCREEN_GEOMETRY_INVALID")
            self.assertEqual(profile, self.profile)
        client = GeometryReplayClient([window])
        manifest = self.run_plan(self.plan(), client)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["screen_geometry"]["actual_size"], None)
        self.assertEqual(client.executed, [])

    def test_aggregate_root_accepts_neutral_metadata_and_child_window_identity(self):
        # Empty aggregate attributes reproduce real dumps. Other versions can
        # provide a root type/display ID without identifying an app window.
        for metadata in ({"type": "", "id": "", "hostWindowId": "", "displayId": ""},
                         {"type": "root", "id": "display-root", "displayId": "0", "hostWindowId": "-1"},
                         {"type": "Display", "displayId": "0", "description": "Primary display"}):
            tree = layout([1080, 2400])
            tree["attributes"].update(metadata)
            window = layout([720, 1000])
            window["attributes"].update(bundleName="com.huawei.hmos.photos", type="root", hostWindowId="35")
            tree["children"].append(window)
            with self.subTest(metadata=metadata):
                result = sync_screen_geometry(tree, copy.deepcopy(self.profile))
                self.assertEqual(result["actual_size"], [1080, 2400])

    def test_ratio_rounding_keeps_points_inside_actual_pixel_dimensions(self):
        for size in ([1080, 2400], [1440, 3120], [2400, 1080]):
            with self.subTest(size=size):
                self.assertEqual(point_from_ratio([.999999, .999999], size), [size[0] - 1, size[1] - 1])
                self.assertEqual(point_from_ratio([.000001, .000001], size), [0, 0])
                self.assertEqual(point_from_ratio([.5, .5], size), [size[0] // 2, size[1] // 2])
                selected = select_node(layout(size), {"point": [.999999, .999999]}, copy.deepcopy(self.profile))
                self.assertEqual(selected["point"], [size[0] - 1, size[1] - 1])

    def test_old_cached_absolute_control_is_rejected_after_orientation_change(self):
        profile = copy.deepcopy(self.profile)
        cached = select_node(layout([1080, 2400]), {"id": "TabAlbums"}, profile)
        with self.assertRaises(CapturePreparationError) as caught:
            build_actions((11, "albums", "albums", "albums", None, []), layout([2400, 1080], "gallery_browser"),
                          profile, {"albums": cached})
        self.assertEqual(caught.exception.code, "SCENARIO_CONTROL_POSITION_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
