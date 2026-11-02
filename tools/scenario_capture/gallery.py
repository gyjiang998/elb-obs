"""Gallery UI primitives ported from scheduler_time_aggregation; see provenance.json."""

import re
import math


SYSTEM_UI_BUNDLES = {"com.ohos.sceneboard", "com.ohos.systemui", "com.ohos.screenlock", "com.huawei.hmos.screenlock"}
DESKTOP_IDS = {"SCBDesktop_Flex_Desktop", "SCBDesktop_Column_PageDesktopLayout", "SwiperPage_Grid_WorkSpace_0"}
LOCK_SCREEN_IDS = {"ScreenLockRootComponent", "MainPageView_Screen_Lock_Home"}


def usb_dialog_nodes(tree, inherited_bundle="", inherited_visible=True):
    """Scope dismissal controls to the visible USB function chooser itself."""
    if not isinstance(tree, dict):
        return []
    attributes = tree.get("attributes", {})
    bundle = attributes.get("bundleName") or inherited_bundle
    visible = inherited_visible and str(attributes.get("visible")).lower() != "false"
    if not visible:
        return []
    if any("UsbFunctionSwitchExtAbility" in str(attributes.get(key, "")) for key in ("id", "abilityName")):
        return flatten(tree, bundle, visible)
    return [node for child in tree.get("children", []) for node in usb_dialog_nodes(child, bundle, visible)]


def usb_close_target(tree, profile):
    # This chooser's standard confirm button only terminates its dialog. Mode
    # radio buttons are deliberately outside this semantic selector.
    matches = []
    for node in usb_dialog_nodes(tree):
        if (not node["_visible"] or str(node.get("enabled")).lower() == "false"
                or str(node.get("clickable")).lower() != "true"
                or node.get("id") != "advanced_dialog_button_0" or node.get("type") != "Button"
                or str(node.get("text", "")).strip() not in {"确定", "OK"}):
            continue
        rectangle = bounds(node.get("bounds"))
        if rectangle:
            left, top, right, bottom = rectangle
            width, height = profile["screen_size"]
            if 0 <= left < right <= width and 0 <= top < bottom <= height:
                matches.append({"point": [round((left + right) / 2), round((top + bottom) / 2)],
                                "id": node["id"], "text": node["text"], "bounds": list(rectangle)})
    return matches[0] if len(matches) == 1 else None


class CapturePreparationError(ValueError):
    """Actionable device-page failure retained with the capture result."""

    def __init__(self, code, message, suggestion, *, phase="preparation"):
        super().__init__(message)
        self.code = code
        self.suggestion = suggestion
        self.phase = phase

STEPS = [
    (1, "launch", "启动图库", "launch", None, ["photos", "albums"]),
    (2, "photos", "点击照片", "click", "photos", ["date_mode_button"]),
    (3, "year", "切换到年", "date_mode", "year", ["year"]),
    (4, "zoom", "放大到最大再缩小到最小，重复三轮", "zoom", None, ["year"]),
    (5, "day", "切换到日", "date_mode", "day", ["day", "first_photo"]),
    (6, "first_photo", "点击第一张图片", "click", "first_photo", ["edit"]),
    (7, "edit", "点击编辑", "click", "edit", ["filters"]),
    (8, "filters", "点击滤镜", "click", "filters", ["classic"]),
    (9, "classic", "点击经典", "click", "classic", ["save_menu"]),
    (10, "save_as", "另存为", "click", "save_as", ["edit"]),
    (11, "albums", "返回相册", "albums", None, ["albums_title"]),
    (12, "places", "点击地点", "click", "places", ["cities"]),
    (13, "cities", "点击城市", "click", "cities", ["first_city"]),
    (14, "first_city", "点击第一个城市", "click", "first_city", ["city_page"]),
    (15, "scroll", "城市照片页面向上滑动3次、向下滑动3次", "scroll", None, ["city_page"]),
    (16, "return_albums", "返回相册页面", "albums", None, ["albums_title"]),
    (17, "exit", "退出图库，回到桌面", "home", None, []),
]

def bounds(value):
    values = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", value) if isinstance(value, str) else None
    return tuple(map(int, values.groups())) if values else None


def sync_screen_geometry(tree, profile):
    """Use display-root evidence, retaining the reference in a run-local profile.

    The runner passes its own profile copy. Child/window bounds and a reference
    resolution are never substituted for missing display geometry.
    """
    attributes = tree.get("attributes") if isinstance(tree, dict) else None
    attributes = attributes if isinstance(attributes, dict) else {}
    rectangle = bounds(attributes.get("bounds"))
    identity = {key: str(attributes[key]).strip() if attributes.get(key) is not None else "" for key in
                ("bundleName", "abilityName", "type", "hostWindowId", "hierarchy")}
    # Real dumpLayout output wraps application/window roots in an aggregate
    # root. A window can also start at (0, 0), so its bounds alone are not
    # display evidence. Do not reject neutral root/display IDs or type="root"
    # alone: those can be aggregate metadata on other device versions.
    explicit_window = bool(identity["bundleName"] or identity["abilityName"]
        or identity["type"].lower() in {"window", "windowscene"}
        or (identity["hostWindowId"].isdigit()
            and (identity["type"].lower() == "root" or re.fullmatch(r"ROOT\d+", identity["hierarchy"]))))
    if explicit_window or rectangle is None or rectangle[:2] != (0, 0) or not all(100 <= value <= 16000 for value in rectangle[2:]):
        raise CapturePreparationError("SCENARIO_SCREEN_GEOMETRY_INVALID", "当前 UI 布局未提供有效的屏幕根边界，无法生成操作坐标。",
                                      "请重新获取完整 dumpLayout；根边界应从 [0,0] 开始，不能用应用窗口或参考分辨率代替。", phase="screen_geometry")
    actual = list(rectangle[2:])
    reference = list(profile.get("screen_size_reference", profile["screen_size"]))
    mode = profile.get("screen_size_mode", "dynamic")
    if mode not in {"dynamic", "fixed"}:
        raise CapturePreparationError("SCENARIO_SCREEN_GEOMETRY_INVALID", "屏幕尺寸模式无效。",
                                      "请使用 dynamic 自动读取真机尺寸，或 fixed 校验指定尺寸。", phase="screen_geometry")
    if mode == "fixed" and reference != actual:
        raise CapturePreparationError("SCENARIO_SCREEN_SIZE_MISMATCH", f"真机布局尺寸 {actual} 与固定尺寸 {reference} 不一致。",
                                      "请切换为 dynamic 按实际控件与归一化手势执行，或确认固定尺寸与设备方向。", phase="screen_geometry")
    profile["screen_size_reference"] = reference
    profile["screen_size_mode"] = mode
    profile["screen_size"] = actual
    return {"actual_size": actual, "root_bounds": list(rectangle),
            "orientation": "landscape" if actual[0] > actual[1] else "portrait" if actual[0] < actual[1] else "square",
            "source": "dumpLayout.root.bounds"}

def flatten(tree, inherited_bundle="", inherited_visible=True):
    if isinstance(tree, list):
        return [node for child in tree for node in flatten(child, inherited_bundle, inherited_visible)]
    if not isinstance(tree, dict):
        return []
    attributes = dict(tree.get("attributes", {}))
    bundle = attributes.get("bundleName") or inherited_bundle
    attributes["_bundle"] = bundle
    attributes["_visible"] = inherited_visible and str(attributes.get("visible")).lower() != "false"
    result = [attributes]
    for child in tree.get("children", []):
        result.extend(flatten(child, bundle, attributes["_visible"]))
    return result

def point_from_ratio(point, size):
    if not isinstance(point, (list, tuple)) or len(point) != 2 or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value < 1 for value in point):
        raise ValueError(f"Coordinates must be normalized strictly between 0 and 1: {point}")
    return [min(size[index] - 1, max(0, round(value * size[index]))) for index, value in enumerate(point)]

def select_node(tree, selector, profile):
    sync_screen_geometry(tree, profile)
    if not selector:
        raise ValueError("Missing selector; use --inspect and configure the profile before capture")
    if "point" in selector:
        point = point_from_ratio(selector["point"], profile["screen_size"])
        return {"point": point, "selection": "calibrated_coordinate", "selector": selector,
                "coordinate_space": "normalized_display", "screen_size": list(profile["screen_size"])}
    alternatives = selector.get("alternatives", [selector])
    size = profile["screen_size"]
    for candidate in alternatives:
        matches = []
        for attributes in flatten(tree):
            rectangle = bounds(attributes.get("bounds"))
            if attributes["_bundle"] != profile["bundle"] or not rectangle:
                continue
            if not attributes["_visible"] or str(attributes.get("enabled")).lower() == "false":
                continue
            left, top, right, bottom = rectangle
            center = [round((left + right) / 2), round((top + bottom) / 2)]
            if not (right > left and bottom > top and 0 < center[0] < size[0] and 0 < center[1] < size[1]):
                continue
            restrictions = [(key, value) for key, value in candidate.items() if key not in ("index", "region")]
            if not restrictions:
                raise ValueError("A selector must specify text, id, description, or another node attribute")
            matched = all(
                bool(re.fullmatch(str(value), str(attributes.get(key[:-6], "")))) if key.endswith("_regex")
                else str(attributes.get(key, "")) == str(value)
                for key, value in restrictions
            )
            if "region" in candidate:
                region = candidate["region"]
                matched = matched and region[0] * size[0] <= center[0] <= region[2] * size[0] and region[1] * size[1] <= center[1] <= region[3] * size[1]
            if matched:
                matches.append({"point": center, "attributes": attributes, "selection": "node", "selector": candidate,
                                "coordinate_space": "layout_bounds", "screen_size": list(size)})
        matches.sort(key=lambda node: (node["point"][1], node["point"][0]))
        if "index" in candidate:
            index = candidate["index"]
            if 0 <= index < len(matches):
                return matches[index]
        elif len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            raise ValueError(f"Ambiguous selector {candidate}: {len(matches)} matches; specify id/region/index")
    raise ValueError(f"No visible gallery control matches {selector}")

def verify_screen(tree, profile, gallery=True):
    sync_screen_geometry(tree, profile)
    nodes = flatten(tree)
    has_gallery = any(node["_bundle"] == profile["bundle"] and node["_visible"] for node in nodes)
    if gallery and not has_gallery:
        raise ValueError("Gallery is not visible; unlock the phone and prepare the expected page")
    return has_gallery


def screen_state(tree, profile):
    """Classify UI structure, without inspecting photo content or screen text."""
    visible = [node for node in flatten(tree) if node["_visible"]]
    ids = {str(node.get("id", "")) for node in visible}
    system_nodes = [node for node in visible if node["_bundle"] in SYSTEM_UI_BUNDLES]
    locked = bool(ids & LOCK_SCREEN_IDS)
    credential_id = re.compile(r"password|passcode|pattern.?lock|unlock.?pattern|pin(?:input|entry|code|view|keyboard|pad)|(?:^|[_-])pin(?:$|[_-])", re.I)
    if any(credential_id.search(str(node.get("id", "")))
           or str(node.get("type", "")) == "PatternLock"
           or str(node.get("password", node.get("isPassword", ""))).lower() == "true"
           or (locked and str(node.get("type", "")) == "TextInput") for node in system_nodes):
        return "locked_credentials"
    if usb_dialog_nodes(tree):
        return "usb_dialog"
    if locked:
        return "locked"
    gallery_nodes = [node for node in visible if node["_bundle"] == profile["bundle"]]
    if not gallery_nodes:
        if any(node.get("id") in DESKTOP_IDS for node in system_nodes):
            return "desktop"
        return "other"
    gallery_ids = {str(node.get("id", "")) for node in gallery_nodes}
    # Confirmation dialogs take precedence over the page underneath them.
    if any(str(node.get("type", "")) in {"Dialog", "AlertDialog", "CustomDialog"} for node in visible):
        return "gallery_dialog"
    if any(name.startswith("petalClip_") for name in gallery_ids):
        return "gallery_editor"
    try:
        for target, fallback in (("photos", "TabPhotos"), ("albums", "TabAlbums")):
            selector = profile.get("targets", {}).get(target) or {"id": fallback}
            if "point" in selector:
                selector = {"id": fallback}
            select_node(tree, selector, profile)
        return "gallery_home"
    except ValueError:
        pass
    if "PhotoBrowserBg" in gallery_ids and ("PhotoBrowserActionBarButtonBack" in gallery_ids
            or "AccessibilityComponent_BrowserBack" in gallery_ids):
        return "gallery_browser"
    return "gallery_other"


def verify_page_available(tree, profile, *, phase="preparation"):
    """Distinguish a connected device from a usable, unobstructed app page."""
    verify_screen(tree, profile, gallery=False)
    state = screen_state(tree, profile)
    if state == "locked_credentials":
        raise CapturePreparationError("SCENARIO_UNLOCK_AUTH_REQUIRED", "设备正在等待锁屏密码、PIN 或图案验证，采集尚未开始。",
                                      "请在手机上完成身份验证后重新执行采集；平台会继续核验当前界面。", phase=phase)
    if state == "locked":
        raise CapturePreparationError("SCENARIO_SCREEN_LOCKED", "设备处于锁屏界面，采集尚未开始。",
                                      "请在手机上解锁屏幕，再重新执行采集。", phase=phase)
    if state == "usb_dialog":
        raise CapturePreparationError("SCENARIO_USB_DIALOG", "USB 连接方式弹窗遮挡了应用页面。",
                                      "请在手机上关闭 USB 连接方式弹窗，再重新执行采集。", phase=phase)
    return state


# Compatibility imports are lazy: shared preparation reuses the UI primitives
# above, while all capture scenarios call the shared entry point directly.
def is_preparation_interruption(exc):
    from .screen_preparation import is_preparation_interruption as implementation
    return implementation(exc)


def parse_power_state_dump(output):
    from .screen_preparation import parse_power_state_dump as implementation
    return implementation(output)


def wake_display_before_capture(client, record):
    from .screen_preparation import wake_display_before_capture as implementation
    return implementation(client, record)


def prepare_unlocked_page(tree, profile, client, record, read_page, **options):
    from .screen_preparation import prepare_unlocked_page as implementation
    return implementation(tree, profile, client, record, read_page, **options)


def build_actions(step, tree, profile, cache):
    sync_screen_geometry(tree, profile)
    number, slug, label, operation, target, expected = step
    actions = []

    def add(name, command):
        actions.append({"name": name, "command": command})

    if operation == "launch":
        add("LAUNCH", ["aa", "start", "-a", profile["ability"], "-b", profile["bundle"]])
    elif operation == "click":
        selected = select_node(tree, profile["targets"][target], profile)
        add("CLICK_" + target.upper(), ["uitest", "uiInput", "click", *selected["point"]])
        actions[-1]["target"] = selected
    elif operation == "date_mode":
        selected = select_node(tree, profile["targets"]["date_mode_button"], profile)
        rectangle = bounds(selected["attributes"]["bounds"])
        cycle = profile["navigation"]["date_mode_cycle"]
        current = []
        for node in flatten(tree):
            child = bounds(node.get("bounds"))
            if node["_bundle"] == profile["bundle"] and node["_visible"] and child and node.get("text") in cycle:
                if rectangle[0] <= child[0] < child[2] <= rectangle[2] and rectangle[1] <= child[1] < child[3] <= rectangle[3]:
                    current.append(node["text"])
        if len(current) != 1:
            raise ValueError(f"Cannot determine current date mode: {current}")
        desired = profile["targets"][target]["text"]
        count = (cycle.index(desired) - cycle.index(current[0])) % len(cycle)
        if not count:
            add("DATE_MODE_ALREADY_" + target.upper(), ["true"])
        for index in range(count):
            add(f"DATE_MODE_CLICK_{index + 1}", ["uitest", "uiInput", "click", *selected["point"]])
            actions[-1]["target"] = selected
            add("DATE_MODE_SETTLE", ["sleep", profile["navigation"]["back_gap_seconds"]])
    elif operation == "home":
        add("HOME", ["uitest", "uiInput", "keyEvent", "Home"])
    elif operation == "albums":
        for index in range(profile["navigation"][f"step{number}_back_count"]):
            add(f"BACK_{index + 1}", ["uitest", "uiInput", "keyEvent", "Back"])
            add(f"BACK_SETTLE_{index + 1}", ["sleep", profile["navigation"]["back_gap_seconds"]])
        selector = profile["targets"]["albums"]
        selected = select_node(tree, selector, profile) if "point" in selector else cache.get("albums")
        if selected and selected.get("screen_size") != profile["screen_size"]:
            selected = None
        if not selected:
            raise CapturePreparationError("SCENARIO_CONTROL_POSITION_UNAVAILABLE", "当前屏幕尺寸下尚未确认相册标签位置，不能复用旧坐标。",
                                          "请从图库主页重新准备以获取控件位置，或提供经当前页面核验的归一化 albums.point。")
        add("CLICK_ALBUMS", ["uitest", "uiInput", "click", *selected["point"]])
        actions[-1]["target"] = selected
    elif operation == "zoom":
        zoom = profile["zoom"]
        center_x, center_y = point_from_ratio(zoom["center"], profile["screen_size"])
        inner = round(zoom["inner_half_width"] * profile["screen_size"][0])
        outer = round(zoom["outer_half_width"] * profile["screen_size"][0])
        for round_index in range(zoom["rounds"]):
            for direction, source, destination in (("OUT", inner, outer), ("IN", outer, inner)):
                for sweep in range(zoom["sweeps_per_extreme"]):
                    add(f"PINCH_{direction}_R{round_index + 1}_S{sweep + 1}", [
                        "uinput", "-T", "-m", center_x - source, center_y, center_x - destination, center_y,
                        center_x + source, center_y, center_x + destination, center_y, zoom["duration_ms"],
                    ])
                    add("GESTURE_SETTLE", ["sleep", zoom["gesture_gap_seconds"]])
    elif operation == "scroll":
        settings = profile["scroll"]
        directions = ["UP"] * 3 + (["UP", "DOWN"] * 3 if settings["step15_mode"] == "up3_then_roundtrip3" else ["DOWN"] * 3)
        upper = point_from_ratio(settings["upper"], profile["screen_size"])
        lower = point_from_ratio(settings["lower"], profile["screen_size"])
        for index, direction in enumerate(directions, 1):
            source, destination = (lower, upper) if direction == "UP" else (upper, lower)
            add(f"SWIPE_{direction}_{index}", ["uitest", "uiInput", "swipe", *source, *destination, settings["velocity"]])
            add("GESTURE_SETTLE", ["sleep", settings["gesture_gap_seconds"]])
    return actions
