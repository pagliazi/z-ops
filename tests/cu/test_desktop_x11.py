"""Linux X11 桌面适配测试：平台闸门、窗口策略、输入基线、注入与真实 Xvfb 集成。

纯逻辑用例不需要显示器；`RealX11Test` 起一个独立 Xvfb 和真实 GUI 客户端，
用真事件证明"注入到达了应用"，而不是只到达 X 服务器。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import psutil

from companion import __version__
from companion.config import Config
from companion.desktop import (ACCEPTANCE_CHECKS, ACCEPTANCE_CHECKS_X11, X11_CAPABILITIES,
                               X11Driver, acceptance_platform, validate_acceptance, write_acceptance)
from companion.security import Denied, Paused

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "scripts" / "x11_target_window.py"

# 假窗口：正常应用、缺 _NET_WM_PID、Companion 自己、认证代理。
WINDOW = {"hwnd": 501, "title": "笔记 - gedit", "class_name": "Gedit", "pid": 4242,
          "rect": [10, 20, 410, 320]}
NO_PID = {"hwnd": 502, "title": "无 PID 窗口", "class_name": "Tk", "pid": 0,
          "rect": [0, 0, 100, 100]}
OWN = {"hwnd": 503, "title": "Z-OPS Companion", "class_name": "Tk", "pid": os.getpid(),
       "rect": [0, 0, 400, 300]}
SECURITY = {"hwnd": 504, "title": "认证", "class_name": "GcrPrompter", "pid": 5050,
            "rect": [0, 0, 300, 200]}
PROCESS_NAMES = {4242: "gedit", 5050: "gcr-prompter"}


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
    """只实现 X11Driver 用到的原语；所有调用都被记录下来供断言。"""

    def __init__(self):
        self.infos = [dict(WINDOW), dict(NO_PID), dict(OWN), dict(SECURITY)]
        self.lock = False
        self.lock_error = None
        self.idle = 5000
        self.xtest = True
        self.xss = True
        self.focused = WINDOW["hwnd"]
        self.focus_ok = True
        self.top_level_override = None
        self.grab = "Ctrl+Alt+Pause"
        self.typed, self.pressed, self.clicked, self.scrolled = [], [], [], []
        self.closed = False

    def windows(self, limit):
        return [dict(info) for info in self.infos[:limit]]

    def info(self, xid):
        for info in self.infos:
            if info["hwnd"] == int(xid):
                return dict(info)
        raise Denied("desktop_window_missing")

    def locked(self):
        if self.lock_error:
            raise self.lock_error
        return self.lock

    def idle_ms(self):
        return self.idle

    def focus(self):
        return self.focused

    def top_level(self, xid):
        return int(xid) if self.top_level_override is None else self.top_level_override

    def set_focus(self, xid):
        if self.focus_ok:
            self.focused = int(xid)
        return self.focus_ok

    def type_text(self, value):
        self.typed.append(value)

    def press(self, keys):
        self.pressed.append(list(keys))

    def click(self, x, y):
        self.clicked.append((x, y))

    def scroll(self, x, y, amount):
        self.scrolled.append((x, y, amount))

    def grab_hotkey(self):
        return self.grab

    def tray_available(self):
        return False

    def hotkey_pending(self):
        return False

    def probe(self):
        return {"windows": len(self.windows(8)), "focus": True, "idle_ms": self.idle,
                "xtest": self.xtest, "xss": self.xss, "hotkey": self.grab}

    def close(self):
        self.closed = True


def make_config(path):
    return Config("https://example.test", "测试设备", device_id="device-123",
                  desktop_apps=["gedit"], path=path)


class PlatformGatingTest(unittest.TestCase):
    """prepare 的第一道门：平台、会话类型、显示号，任何一项不符都不继续。"""

    def setUp(self):
        self.driver = X11Driver()

    def test_non_linux_platform_is_refused(self):
        with mock.patch("companion.desktop.sys", mock.Mock(platform="win32")):
            with self.assertRaises(Denied) as caught:
                self.driver.prepare()
        self.assertEqual(str(caught.exception), "linux_only")

    def test_wayland_session_is_refused(self):
        with mock.patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":99"}):
            with self.assertRaises(Denied) as caught:
                self.driver.prepare()
        self.assertEqual(str(caught.exception), "wayland_desktop_not_accepted")

    def test_wayland_display_without_session_type_is_refused(self):
        """只有 WAYLAND_DISPLAY 时 DISPLAY 指向 XWayland：同样不能当成可信 X11。"""
        env = {key: value for key, value in os.environ.items()
               if key not in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY")}
        env.update({"DISPLAY": ":99", "WAYLAND_DISPLAY": "wayland-0"})
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(Denied) as caught:
                self.driver.prepare()
        self.assertEqual(str(caught.exception), "wayland_desktop_not_accepted")

    def test_missing_display_is_refused(self):
        env = {key: value for key, value in os.environ.items()
               if key not in ("DISPLAY", "XDG_SESSION_TYPE")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(Denied) as caught:
                self.driver.prepare()
        self.assertEqual(str(caught.exception), "x11_display_unavailable")

    def test_unreachable_display_is_refused(self):
        with mock.patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":1988"}):
            with self.assertRaises(Denied) as caught:
                self.driver.prepare()
        self.assertEqual(str(caught.exception), "x11_display_unavailable")

    def test_driver_close_is_idempotent_without_backend(self):
        self.driver.close()
        self.driver.close()


class AcceptanceRecordTest(unittest.TestCase):
    """Linux 记录只能含 X11 检查项；写成 Windows 记录或不齐全都必须拒绝。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_config(Path(self.tmp.name) / "config.json")

    def test_platform_reports_linux_checks(self):
        name, checks = acceptance_platform("linux")
        self.assertEqual(name, "linux")
        self.assertEqual(set(checks), set(ACCEPTANCE_CHECKS_X11))
        self.assertNotEqual(set(checks), set(ACCEPTANCE_CHECKS))

    def test_linux_record_roundtrip(self):
        checks = {name: True for name in ACCEPTANCE_CHECKS_X11}
        body = write_acceptance(self.cfg, checks)
        self.assertEqual(body["platform"], "linux")
        validate_acceptance(body, self.cfg, "posix")

    def test_incomplete_linux_record_is_rejected(self):
        checks = {name: True for name in ACCEPTANCE_CHECKS_X11}
        checks["injection"] = False
        with self.assertRaises(Denied):
            write_acceptance(self.cfg, checks)
        checks.pop("injection")
        with self.assertRaises(Denied):
            write_acceptance(self.cfg, checks)

    def test_windows_checks_cannot_be_written_as_linux(self):
        with self.assertRaises(Denied):
            write_acceptance(self.cfg, {name: True for name in ACCEPTANCE_CHECKS})


class WindowPolicyTest(unittest.TestCase):
    """窗口策略：按进程授权，缺 pid 或安全界面一律拒绝，锁屏时不列窗口。"""

    def setUp(self):
        patcher = mock.patch.object(psutil, "Process", FakeProcess)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = FakeBackend()
        self.driver = X11Driver()
        self.driver._backend = self.backend

    def test_window_resolves_process_name(self):
        window = self.driver.window(WINDOW["hwnd"])
        self.assertEqual(window["process"], "gedit")
        self.assertEqual(window["pid"], 4242)
        self.assertEqual(window["class_name"], "Gedit")
        self.assertEqual(window["rect"], [10, 20, 410, 320])

    def test_window_without_pid_is_refused(self):
        with self.assertRaises(Denied) as caught:
            self.driver.window(NO_PID["hwnd"])
        self.assertEqual(str(caught.exception), "desktop_window_pid_unavailable")

    def test_own_and_security_windows_are_refused(self):
        for info in (OWN, SECURITY):
            with self.assertRaises(Denied) as caught:
                self.driver.window(info["hwnd"])
            self.assertEqual(str(caught.exception), "security_ui_not_targetable")

    def test_unknown_window_is_refused(self):
        with self.assertRaises(Denied):
            self.driver.window(9999)

    def test_list_windows_filters_and_truncates(self):
        locked, windows = self.driver.list_windows(50)
        self.assertFalse(locked)
        # 502 没有 _NET_WM_PID：无法核对进程授权，所以连列表都不出现。
        self.assertEqual([item["hwnd"] for item in windows], [WINDOW["hwnd"]])
        self.assertEqual([item["hwnd"] for item in self.driver.list_windows(1)[1]],
                         [WINDOW["hwnd"]])

    def test_locked_desktop_lists_nothing(self):
        self.backend.lock = True
        self.assertEqual(self.driver.list_windows(10), (True, []))
        self.assertTrue(self.driver.secure())
        self.assertTrue(self.driver.sensitive_focus(WINDOW["hwnd"]))

    def test_secure_is_fail_closed_on_error(self):
        self.backend.lock_error = RuntimeError("xss broken")
        self.assertTrue(self.driver.secure())

    def test_same_window_compares_pid_and_class(self):
        self.assertTrue(self.driver.same_window({"hwnd": WINDOW["hwnd"], "pid": 4242,
                                                 "class_name": "Gedit"}))
        self.assertFalse(self.driver.same_window({"hwnd": WINDOW["hwnd"], "pid": 4243,
                                                  "class_name": "Gedit"}))
        self.assertFalse(self.driver.same_window({"hwnd": 9999, "pid": 4242,
                                                  "class_name": "Gedit"}))

    def test_elements_and_capture_are_not_claimed(self):
        self.assertEqual(self.driver.list_elements(WINDOW["hwnd"], 80), [])
        with self.assertRaises(Denied) as caught:
            self.driver.invoke_element(WINDOW["hwnd"], 0, "button", "保存")
        self.assertEqual(str(caught.exception), "desktop_element_not_supported_on_platform")
        with self.assertRaises(Denied) as caught:
            self.driver.capture_window(WINDOW["hwnd"])
        self.assertEqual(str(caught.exception), "desktop_capture_not_accepted_on_platform")


class InputBaselineTest(unittest.TestCase):
    """输入基线的语义：idle 增长不算用户输入，idle 归零才算。"""

    def setUp(self):
        self.backend = FakeBackend()
        self.driver = X11Driver()
        self.driver._backend = self.backend

    def test_idle_growth_is_not_user_input(self):
        self.backend.idle = 2000
        baseline = self.driver.last_input()
        time.sleep(0.05)
        self.backend.idle = 2050
        self.assertFalse(self.driver.external_input_since(baseline))

    def test_idle_reset_is_user_input(self):
        self.backend.idle = 2000
        baseline = self.driver.last_input()
        self.backend.idle = 0
        self.assertTrue(self.driver.external_input_since(baseline))

    def test_missing_sentinel_is_refused(self):
        self.backend.idle = -1
        with self.assertRaises(Denied) as caught:
            self.driver.last_input()
        self.assertEqual(str(caught.exception), "input_sentinel_unavailable")

    def test_mark_baseline_swallows_own_injection(self):
        self.backend.idle = 0
        baseline = self.driver.mark_baseline()
        self.assertLess(time.monotonic() - baseline, 0.5)
        self.assertFalse(self.driver.external_input_since(baseline))

    def test_mark_baseline_waits_for_idle_to_drop(self):
        self.backend.idle = 9000

        def reset():
            time.sleep(0.05)
            self.backend.idle = 0

        threading.Thread(target=reset, daemon=True).start()
        baseline = self.driver.mark_baseline()
        self.assertLess(time.monotonic() - baseline, 0.5)


class FocusAndInjectionTest(unittest.TestCase):
    """焦点是注入的前提：拿不到焦点就暂停，动作只转发到已批准的窗口。"""

    def setUp(self):
        self.backend = FakeBackend()
        self.driver = X11Driver()
        self.driver._backend = self.backend

    def test_focus_denied_raises_paused(self):
        self.backend.focus_ok = False
        with self.assertRaises(Paused) as caught:
            self.driver.focus(WINDOW["hwnd"])
        self.assertEqual(str(caught.exception), "desktop_focus_denied")

    def test_focus_must_land_on_the_same_top_level(self):
        self.backend.top_level_override = 999
        with self.assertRaises(Paused) as caught:
            self.driver.focus(WINDOW["hwnd"])
        self.assertEqual(str(caught.exception), "desktop_focus_denied")

    def test_perform_forwards_all_actions(self):
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

    def test_status_survives_without_tray(self):
        self.assertIsNone(self.driver._tray)
        self.driver.set_status("桌面接管中")
        self.assertIn("桌面接管中", self.driver.status)
        self.driver.stop()
        self.assertIn("已停止", self.driver.status)


def _free_display():
    for number in range(90, 160):
        if not os.path.exists("/tmp/.X%d-lock" % number) \
                and not os.path.exists("/tmp/.X11-unix/X%d" % number):
            return number
    return None


def _tk_ready():
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(shutil.which("Xvfb") and _tk_ready() and TARGET.exists(),
                     "需要 Xvfb、tkinter 与 scripts/x11_target_window.py")
class RealX11Test(unittest.TestCase):
    """真实 X 服务器：枚举真实窗口、按 pid/class 核对、注入真实事件到达应用。"""

    @classmethod
    def setUpClass(cls):
        number = _free_display()
        if number is None:
            raise unittest.SkipTest("no free X display")
        cls.display = ":%d" % number
        cls.env = {**os.environ, "DISPLAY": cls.display, "XDG_SESSION_TYPE": "x11",
                   "WAYLAND_DISPLAY": ""}
        cls.xvfb = subprocess.Popen(
            ["Xvfb", cls.display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        if not cls._wait(lambda: cls._probe_display(), 15):
            cls._cleanup_processes()
            raise unittest.SkipTest("Xvfb 未能在 15 秒内就绪")
        cls.tmp = tempfile.mkdtemp(prefix="zops-x11-")
        cls.log = os.path.join(cls.tmp, "events.log")
        cls.target = subprocess.Popen(
            [sys.executable, str(TARGET), "--log", cls.log, "--seconds", "180"],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        ready = cls._wait(cls._read_ready, 20)
        if not ready:
            cls._cleanup_processes()
            raise unittest.SkipTest("验收目标窗口未能启动")
        cls.target_pid, cls.xid = ready
        # Ctrl+Alt+Pause 是独占抓取，每个 X 服务器只准备一次。
        cls.driver = X11Driver()
        cls.driver.tray_timeout = 0.1
        with mock.patch.dict(os.environ, cls.env):
            cls.checks = cls.driver.prepare()
        cls.backend = cls.driver._backend

    @classmethod
    def _cleanup_processes(cls):
        for name in ("target", "xvfb"):
            process = getattr(cls, name, None)
            if process is not None:
                process.terminate()
                try:
                    process.wait(5)
                except Exception:
                    process.kill()
        tmp = getattr(cls, "tmp", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.close()
        cls._cleanup_processes()

    @staticmethod
    def _wait(predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        return None

    @classmethod
    def _probe_display(cls):
        result = subprocess.run(["xdpyinfo"], env=cls.env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        return result.returncode == 0

    @classmethod
    def _read_ready(cls):
        try:
            with open(cls.log, encoding="utf-8") as stream:
                for line in stream:
                    found = re.match(r"READY pid=(\d+) xid=(\d+)", line)
                    if found:
                        return int(found.group(1)), int(found.group(2))
        except FileNotFoundError:
            return None
        return None

    def log_lines(self):
        try:
            with open(self.log, encoding="utf-8") as stream:
                return stream.read().splitlines()
        except FileNotFoundError:
            return []

    def wait_for(self, prefix, seconds=5):
        return self._wait(lambda: next((line for line in self.log_lines()
                                        if line.startswith(prefix)), None), seconds)

    def window(self):
        return self.driver.window(self.xid)

    def test_prepare_reports_every_check(self):
        self.assertEqual(set(self.checks), set(ACCEPTANCE_CHECKS_X11))
        self.assertTrue(all(value is True for value in self.checks.values()))
        self.assertEqual(self.backend.hotkey_name, "Ctrl+Alt+Pause")
        self.assertFalse(self.backend.hotkey_pending())
        probe = self.driver.uia_probe
        self.assertGreaterEqual(probe["windows"], 1)
        self.assertTrue(probe["xtest"])
        self.assertTrue(probe["xss"])
        self.assertEqual(probe["elements"], "not_supported")
        # 裸 Xvfb 没有托盘宿主：托盘必须如实为 false，而不是假装就绪。
        self.assertFalse(probe["tray"])
        self.assertFalse(self.driver.tray_ready)

    def test_window_enumeration_reads_pid_and_class(self):
        info = self.backend.info(self.xid)
        self.assertEqual(info["pid"], self.target_pid)
        self.assertEqual(info["class_name"], "ZopsTarget")
        self.assertEqual(info["title"], "Z-OPS 验收窗口")
        self.assertEqual(info["rect"], [80, 60, 560, 320])
        listed = self.backend.windows(50)
        self.assertIn(self.xid, [item["hwnd"] for item in listed])
        window = self.window()
        self.assertEqual(window["pid"], self.target_pid)
        self.assertEqual(window["process"], Path(sys.executable).name)

    def test_injection_reaches_the_application(self):
        window = self.window()
        self.driver.focus(self.xid)
        self.driver.perform("type_text", {"text": "hi"}, window)
        self.assertIsNotNone(self.wait_for("KEY 'h' h"))
        self.assertIsNotNone(self.wait_for("KEY 'i' i"))
        self.driver.perform("keys", {"keys": ["ctrl", "a"]}, window)
        self.assertIsNotNone(self.wait_for("KEY '\\x01' a"))
        self.driver.perform("click", {"x": 100, "y": 100}, window)
        self.assertIsNotNone(self.wait_for("CLICK 20 40"))

    def test_injection_resets_screensaver_idle(self):
        self.backend.idle_ms()
        time.sleep(0.3)
        self.assertGreaterEqual(self.backend.idle_ms(), 300)
        self.driver.focus(self.xid)
        self.driver.perform("type_text", {"text": "x"}, self.window())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.backend.idle_ms() > 500:
            time.sleep(0.05)
        self.assertLess(self.backend.idle_ms(), 500)

    def test_focus_lands_on_the_target(self):
        self.driver.focus(self.xid)
        self.assertEqual(self.backend.top_level(self.backend.focus()), self.xid)
        self.assertEqual(self.driver.foreground(), self.xid)

    def test_desktop_never_sees_its_own_window_as_targetable(self):
        own = [item for item in self.backend.windows(50) if item["pid"] == os.getpid()]
        for item in own:
            with self.assertRaises(Denied):
                self.driver.window(item["hwnd"])

    def test_approval_dialog_times_out_instead_of_waiting_forever(self):
        """对话框没人操作时必须自己关掉并算拒绝：否则命令过期了窗还挂着，之后点“允许”会批准作废请求。"""
        request = {"device_id": "device-123", "device_name": "测试设备", "task_id": "task-1",
                   "session_id": "session-1", "window": self.window(), "scopes": ["click"],
                   "reason": "无人操作超时", "seconds": 60, "hotkey": "Ctrl+Alt+Pause"}
        self.driver.approval_seconds = 0.5
        self.addCleanup(setattr, self.driver, "approval_seconds", 30.0)
        with mock.patch.dict(os.environ, self.env):
            started = time.monotonic()
            approved = self.driver.approve(request)
            elapsed = time.monotonic() - started
        self.assertFalse(approved)
        self.assertEqual(self.driver.last_approval, "timeout")
        self.assertLess(elapsed, 10)

    def test_acceptance_dialog_timeout_is_not_reported_as_a_refusal(self):
        """验收确认到期也不能算「有人点了不写入」：原因要能对上事实。"""
        self.driver.acceptance_seconds = 0.5
        self.addCleanup(setattr, self.driver, "acceptance_seconds", 90.0)
        with mock.patch.dict(os.environ, self.env):
            started = time.monotonic()
            accepted = self.driver.confirm_acceptance("写入验收记录")
            elapsed = time.monotonic() - started
        self.assertFalse(accepted)
        self.assertEqual(self.driver.last_approval, "timeout")
        self.assertLess(elapsed, 10)

    def test_window_survives_identity_recheck(self):
        window = self.window()
        self.assertTrue(self.driver.same_window(window))
        closed = {**window, "class_name": "Other"}
        self.assertFalse(self.driver.same_window(closed))


if __name__ == "__main__":
    unittest.main()
