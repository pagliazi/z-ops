"""macOS 驱动层的平台闸门、窗口策略与能力声明。

这些用例用假后端替代 CoreGraphics/AX，证明的是“接线正确”：闸门、过滤、能力表和
采集上限。它们不构成 macOS 桌面验收（Spec 第 84 条同类纪律）——真实 AX 调用只能在
Mac 上实测，验收由 --accept-desktop 在实机写入记录。
"""
import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import psutil

from companion import __version__
from companion.config import Config
from companion.desktop import (ACCEPTANCE_CHECKS, ACCEPTANCE_CHECKS_MACOS, ACCEPTANCE_CHECKS_X11,
                               CAPABILITIES_BY_PLATFORM, DESKTOP_CAPABILITIES, FRAME_BYTES,
                               MacDriver, X11_CAPABILITIES, acceptance_platform,
                               validate_acceptance, write_acceptance)
from companion.security import Denied, Paused

WINDOW = {"hwnd": 701, "title": "无标题 — TextEdit", "class_name": "TextEdit", "pid": 4242,
          "rect": [10, 20, 410, 320]}
SECURITY = {"hwnd": 702, "title": "登录", "class_name": "Login Window", "pid": 5050,
            "rect": [0, 0, 800, 600]}
ELEMENTS = [{"index": 0, "role": "button", "name": "保存", "enabled": True},
            {"index": 1, "role": "menuitem", "name": "拷贝", "enabled": True}]
PROCESS_NAMES = {4242: "TextEdit", 5050: "loginwindow"}


class FakeProcess:
    """替身 psutil.Process：只按 pid 返回进程名，用于核对"按进程授权"。"""

    def __init__(self, pid):
        self._pid = int(pid)

    def name(self):
        value = PROCESS_NAMES.get(self._pid)
        if value is None:
            raise RuntimeError("no such process")
        return value


class FakeBackend:
    """只实现 MacDriver 用到的原语；所有调用都被记录下来供断言。"""

    def __init__(self):
        self.infos = [dict(WINDOW), dict(SECURITY)]
        self.trusted_ok = True
        self.lock_value = False
        self.idle = 5000
        self.focused = WINDOW["hwnd"]
        self.focus_ok = True
        self.top_level_override = None
        self.grab = True
        self.hotkey = False
        self.capture_ok = True
        self.sensitive = False
        self.ax_available = True
        self.frame = "aGk="  # "hi"
        self.elements = [dict(item) for item in ELEMENTS]
        self.typed, self.pressed, self.clicked, self.scrolled = [], [], [], []
        self.invoked = []
        self.closed = False

    # ---- 窗口 ----
    def windows(self, limit):
        return [dict(info) for info in self.infos[:limit]]

    def info(self, number):
        for info in self.infos:
            if info["hwnd"] == int(number):
                return dict(info)
        raise Denied("desktop_window_missing")

    # ---- 权限与状态 ----
    def trusted(self):
        return self.trusted_ok

    def lock_state(self):
        return self.lock_value

    def locked(self):
        return bool(self.lock_value)

    def capture_allowed(self):
        return True if self.capture_ok else False

    def idle_ms(self):
        return self.idle

    def focus(self):
        return self.focused

    def top_level(self, number):
        return int(number) if self.top_level_override is None else self.top_level_override

    def set_focus(self, number):
        if self.focus_ok:
            self.focused = int(number)
        return self.focus_ok

    def sensitive_focus(self, number):
        return self.sensitive

    # ---- 注入 ----
    def type_text(self, value):
        self.typed.append(value)

    def press(self, keys):
        self.pressed.append(list(keys))

    def click(self, x, y):
        self.clicked.append((x, y))

    def scroll(self, x, y, amount):
        self.scrolled.append((x, y, amount))

    # ---- 控件树 ----
    def list_elements(self, number, limit):
        if not self.ax_available:
            raise Denied("desktop_window_ax_unavailable")
        return [dict(item) for item in self.elements[:limit]]

    def invoke_element(self, number, index, role, name):
        if not self.ax_available:
            raise Denied("desktop_window_ax_unavailable")
        self.invoked.append((int(number), int(index), role, name))

    # ---- 采集 ----
    def capture(self, number, limit=FRAME_BYTES):
        if not self.capture_ok:
            raise Denied("desktop_capture_unavailable")
        self.capture_limit = limit
        return self.frame

    # ---- 热键与托盘 ----
    def grab_hotkey(self):
        return self.grab

    def hotkey_pending(self):
        return self.hotkey

    def tray_available(self):
        return False

    def probe(self):
        return {"windows": len(self.infos), "ax": self.trusted_ok, "focus": True,
                "idle_ms": self.idle, "xss": self.lock_value is not None,
                "capture": self.capture_ok, "hotkey": "Ctrl+Option+Cmd+S",
                "tray": True, "elements": "ax_subrole"}

    def close(self):
        self.closed = True


def make_driver(backend=None):
    driver = MacDriver()
    driver._backend = backend if backend is not None else FakeBackend()
    return driver


def prepare(driver, backend, dialog=True):
    """在 darwin 假环境下跑真实 prepare：只替换平台判定、AX 构造与对话框自检。"""
    with mock.patch("companion.desktop.sys", mock.Mock(platform="darwin")), \
            mock.patch("companion.ax.AX", return_value=backend), \
            mock.patch.object(MacDriver, "_dialog_ready", return_value=dialog), \
            mock.patch.object(MacDriver, "_start_hotkey_loop"), \
            mock.patch.object(MacDriver, "_start_tray"):
        return driver.prepare()


class PlatformGatingTest(unittest.TestCase):
    """预检逐项失败必须给出固定原因码，远端才分得清"没授权"和"没适配"。"""

    def setUp(self):
        self.patcher = mock.patch.object(psutil, "Process", FakeProcess)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_non_darwin_is_refused(self):
        with mock.patch("companion.desktop.sys", mock.Mock(platform="linux")):
            with self.assertRaises(Denied) as caught:
                MacDriver().prepare()
        self.assertEqual(str(caught.exception), "macos_only")

    def test_full_preflight_reports_all_macos_checks(self):
        checks = prepare(MacDriver(), FakeBackend())
        self.assertEqual(set(checks), set(ACCEPTANCE_CHECKS_MACOS))
        self.assertEqual(set(checks), set(acceptance_platform("darwin")[1]))
        self.assertTrue(all(value is True for value in checks.values()))

    def test_missing_accessibility_permission_is_refused(self):
        backend = FakeBackend()
        backend.trusted_ok = False
        with self.assertRaises(Denied) as caught:
            prepare(MacDriver(), backend)
        self.assertEqual(str(caught.exception), "macos_accessibility_permission_required")

    def test_no_window_is_refused(self):
        backend = FakeBackend()
        backend.infos = []
        with self.assertRaises(Denied) as caught:
            prepare(MacDriver(), backend)
        self.assertEqual(str(caught.exception), "macos_no_visible_windows")

    def test_unusable_lock_query_is_refused(self):
        backend = FakeBackend()
        backend.lock_value = None
        with self.assertRaises(Denied) as caught:
            prepare(MacDriver(), backend)
        self.assertEqual(str(caught.exception), "lock_query_unavailable")

    def test_missing_screen_capture_permission_is_refused(self):
        backend = FakeBackend()
        backend.capture_ok = False
        with self.assertRaises(Denied) as caught:
            prepare(MacDriver(), backend)
        self.assertEqual(str(caught.exception), "macos_screen_capture_permission_required")

    def test_hotkey_failure_disables_the_desktop(self):
        """Spec 第 53 条：热键注册不起来就不开放桌面接管，绝不"少一条通道也照样开"。"""
        backend = FakeBackend()
        backend.grab = False
        with self.assertRaises(Denied) as caught:
            prepare(MacDriver(), backend)
        self.assertEqual(str(caught.exception), "emergency_hotkey_unavailable")

    def test_backend_is_closed_when_preflight_fails(self):
        backend = FakeBackend()
        backend.trusted_ok = False
        with self.assertRaises(Denied):
            prepare(MacDriver(), backend)
        self.assertTrue(backend.closed)

    def test_stop_channel_uses_the_macos_hotkey_text(self):
        driver = make_driver()
        driver.stop()
        self.assertIn("Ctrl+Option+Cmd+S", driver.status)
        self.assertNotIn("Pause", driver.status)


class AcceptanceRecordTest(unittest.TestCase):
    """macOS 记录只含 AX 检查项；拿 Windows/Linux 项来冒充必须被拒。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config("https://example.test", "测试设备", device_id="device-123",
                          desktop_apps=["textedit"], path=Path(self.tmp.name) / "config.json")

    def test_platform_reports_macos_checks(self):
        name, checks = acceptance_platform("darwin")
        self.assertEqual(name, "macos")
        self.assertEqual(set(checks), set(ACCEPTANCE_CHECKS_MACOS))
        self.assertNotEqual(set(checks), set(ACCEPTANCE_CHECKS))
        self.assertNotEqual(set(checks), set(ACCEPTANCE_CHECKS_X11))

    def test_macos_record_roundtrip(self):
        checks = {name: True for name in ACCEPTANCE_CHECKS_MACOS}
        body = write_acceptance(self.cfg, checks,
                                {"textedit": {"readable": True, "elements": 3}}, "darwin")
        self.assertEqual(body["platform"], "macos")
        validate_acceptance(body, self.cfg, "darwin")

    def test_other_platform_record_is_rejected(self):
        for checks in ({name: True for name in ACCEPTANCE_CHECKS},
                       {name: True for name in ACCEPTANCE_CHECKS_X11}):
            with self.assertRaises(Denied):
                write_acceptance(self.cfg, checks, platform="darwin")

    def test_incomplete_macos_record_is_rejected(self):
        checks = {name: True for name in ACCEPTANCE_CHECKS_MACOS}
        checks["capture"] = False
        with self.assertRaises(Denied):
            write_acceptance(self.cfg, checks, platform="darwin")


class WindowPolicyTest(unittest.TestCase):
    """窗口按进程授权：Companion 自己、认证/登录界面、查不到进程的都不可接管。"""

    def setUp(self):
        self.patcher = mock.patch.object(psutil, "Process", FakeProcess)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.backend = FakeBackend()
        self.driver = make_driver(self.backend)

    def test_window_resolves_process_name(self):
        window = self.driver.window(WINDOW["hwnd"])
        self.assertEqual(window["process"], "TextEdit")
        self.assertEqual(window["rect"], [10, 20, 410, 320])

    def test_security_ui_is_refused(self):
        with self.assertRaises(Denied) as caught:
            self.driver.window(SECURITY["hwnd"])
        self.assertEqual(str(caught.exception), "security_ui_not_targetable")

    def test_own_process_is_refused(self):
        self.backend.infos.append({"hwnd": 703, "title": "Companion", "class_name": "Python",
                                   "pid": os.getpid(), "rect": [0, 0, 10, 10]})
        with self.assertRaises(Denied) as caught:
            self.driver.window(703)
        self.assertEqual(str(caught.exception), "security_ui_not_targetable")

    def test_unresolvable_process_is_refused(self):
        self.backend.infos.append({"hwnd": 704, "title": "幽灵窗口", "class_name": "Ghost",
                                   "pid": 9999, "rect": [0, 0, 10, 10]})
        with self.assertRaises(Denied) as caught:
            self.driver.window(704)
        self.assertEqual(str(caught.exception), "desktop_window_pid_unavailable")

    def test_list_windows_only_offers_targetable_windows(self):
        locked, windows = self.driver.list_windows(50)
        self.assertFalse(locked)
        self.assertEqual([item["hwnd"] for item in windows], [WINDOW["hwnd"]])

    def test_locked_desktop_lists_nothing_and_is_secure(self):
        self.backend.lock_value = True
        self.assertEqual(self.driver.list_windows(10), (True, []))
        self.assertTrue(self.driver.secure())
        # 锁屏时不信任后端报的密码框状态，直接算敏感。
        self.backend.sensitive = False
        self.assertTrue(self.driver.sensitive_focus(WINDOW["hwnd"]))

    def test_sensitive_focus_is_fail_closed(self):
        self.backend.sensitive_focus = mock.Mock(side_effect=RuntimeError("ax broken"))
        self.assertTrue(self.driver.sensitive_focus(WINDOW["hwnd"]))

    def test_same_window_compares_pid_and_class(self):
        self.assertTrue(self.driver.same_window({"hwnd": WINDOW["hwnd"], "pid": 4242,
                                                 "class_name": "TextEdit"}))
        self.assertFalse(self.driver.same_window({"hwnd": WINDOW["hwnd"], "pid": 4243,
                                                  "class_name": "TextEdit"}))

    def test_focus_denied_raises_paused(self):
        """焦点拿不到就暂停，不能退成“隔空注入当前前台窗口”。"""
        self.backend.focus_ok = False
        with self.assertRaises(Paused) as caught:
            self.driver.focus(WINDOW["hwnd"])
        self.assertEqual(str(caught.exception), "desktop_focus_denied")

    def test_focus_must_land_on_the_approved_window(self):
        self.backend.top_level_override = 999
        with self.assertRaises(Paused):
            self.driver.focus(WINDOW["hwnd"])

    def test_perform_forwards_every_action(self):
        window = dict(WINDOW)
        self.driver.perform("type_text", {"text": "你好abc"}, window)
        self.driver.perform("keys", {"keys": ["ctrl", "a"]}, window)
        self.driver.perform("click", {"x": 100, "y": 100}, window)
        self.driver.perform("scroll", {"x": 100, "y": 100, "amount": -2}, window)
        self.assertEqual(self.backend.typed, ["你好abc"])
        self.assertEqual(self.backend.pressed, [["ctrl", "a"]])
        self.assertEqual(self.backend.clicked, [(100, 100)])
        self.assertEqual(self.backend.scrolled, [(100, 100, -2)])

    def test_point_outside_window_is_refused(self):
        for params in ({"x": 9, "y": 100}, {"x": 410, "y": 100}, {"x": 100, "y": 19},
                       {"x": 100, "y": 320}):
            with self.assertRaises(Denied) as caught:
                self.driver.perform("click", params, dict(WINDOW))
            self.assertEqual(str(caught.exception), "desktop_point_outside_window")
        self.assertEqual(self.backend.clicked, [])


class CapabilitySurfaceTest(unittest.TestCase):
    """能力声明必须与实际做到的一致：macOS 有控件树与窗口截图，Linux 没有。"""

    def test_macos_declares_the_full_desktop_surface(self):
        darwin = CAPABILITIES_BY_PLATFORM["darwin"]
        self.assertEqual(list(darwin), list(DESKTOP_CAPABILITIES))
        self.assertIn("desktop.screen", darwin)
        self.assertNotIn("desktop.screen", X11_CAPABILITIES)

    def test_elements_and_capture_are_forwarded(self):
        driver = make_driver()
        self.assertEqual(driver.list_elements(WINDOW["hwnd"], 80), ELEMENTS)
        driver.invoke_element(WINDOW["hwnd"], 0, "button", "保存")
        self.assertEqual(driver._backend.invoked, [(701, 0, "button", "保存")])

    def test_capture_is_checked_against_the_frame_budget(self):
        driver = make_driver()
        driver._backend.frame = "a" * (FRAME_BYTES * 4 // 3 + 8)
        with self.assertRaises(Denied) as caught:
            driver.capture_window(WINDOW["hwnd"])
        self.assertEqual(str(caught.exception), "desktop_capture_too_large")

    def test_capture_uses_the_shared_frame_budget(self):
        driver = make_driver()
        driver.capture_window(WINDOW["hwnd"])
        self.assertEqual(driver._backend.capture_limit, FRAME_BYTES)

    def test_ax_probe_reports_measured_values_only(self):
        driver = make_driver()
        self.assertEqual(prepare(driver, driver._backend),
                         {name: True for name in ACCEPTANCE_CHECKS_MACOS})
        self.assertEqual(driver.uia_probe["elements"], "ax_subrole")
        self.assertEqual(driver.uia_probe["hotkey"], "Ctrl+Option+Cmd+S")


class DesktopWiringTest(unittest.TestCase):
    """Desktop 侧接线：darwin 走 MacDriver，能力表与采集暂停都要按平台事实来。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config("https://example.test", "测试设备", device_id="device-123",
                          desktop_apps=["textedit"], path=Path(self.tmp.name) / "config.json")
        self.acceptance = {"version": 1, "device_id": self.cfg.device_id, "platform": "macos",
                           "accepted_version": __version__, "accepted_at": int(time.time()),
                           "checks": {name: True for name in ACCEPTANCE_CHECKS_MACOS}}
        self.backend = FakeBackend()

    def _desktop(self):
        from companion.desktop import Desktop

        return Desktop(self.cfg, acceptance=self.acceptance, platform="darwin")

    def test_prepare_enables_macos_capabilities(self):
        desktop = self._desktop()
        with mock.patch("companion.desktop.sys", mock.Mock(platform="darwin")), \
                mock.patch("companion.ax.AX", return_value=self.backend), \
                mock.patch("companion.desktop.MacDriver._dialog_ready", return_value=True), \
                mock.patch("companion.desktop.MacDriver._start_hotkey_loop"), \
                mock.patch("companion.desktop.MacDriver._start_tray"):
            asyncio.run(desktop.prepare())
        self.addCleanup(lambda: desktop.driver.close() if desktop.driver else None)
        self.assertTrue(desktop.enabled, desktop.reason)
        self.assertEqual(desktop.capabilities, list(DESKTOP_CAPABILITIES))
        self.assertNotIn("desktop.screen", desktop.unavailable)

    def test_suspicious_acceptance_keeps_desktop_disabled(self):
        desktop = desktop_without_acceptance(self.acceptance, self.cfg)
        self.assertFalse(desktop.enabled)
        self.assertEqual(desktop.reason, "desktop_acceptance_required")

    def test_windows_acceptance_cannot_unlock_macos(self):
        """验收记录按平台隔离：拿 Windows 的记录也开不了 Mac 的桌面。"""
        from companion.desktop import Desktop

        forged = dict(self.acceptance, platform="windows",
                      checks={name: True for name in ACCEPTANCE_CHECKS})
        desktop = Desktop(self.cfg, acceptance=forged, platform="darwin")
        asyncio.run(desktop.prepare())
        self.assertFalse(desktop.enabled)


def desktop_without_acceptance(acceptance, cfg):
    """验收记录里少一项检查：远端不能拿到桌面能力。"""
    from companion.desktop import Desktop

    broken = dict(acceptance, checks=dict(acceptance["checks"], capture=False))
    desktop = Desktop(cfg, acceptance=broken, platform="darwin")
    asyncio.run(desktop.prepare())
    return desktop


if __name__ == "__main__":
    unittest.main()
