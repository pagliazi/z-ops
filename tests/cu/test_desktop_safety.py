"""桌面接管安全链的纯逻辑测试：Windows 实机验收仍单独阻塞。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from companion import __version__
from companion.config import Config
from companion.desktop import (ACCEPTANCE_CHECKS, ACCEPTANCE_CHECKS_X11, APPROVAL_SECONDS,
                               APPROVAL_TIMEOUT, ELEMENT_TYPES, Desktop, Lease, Paused,
                               WindowsDriver, _validate_keys, validate_acceptance)
from companion.security import Denied

WINDOW = {"hwnd": 101, "title": "无标题 - 记事本", "class_name": "Notepad",
          "pid": 4242, "process": "notepad.exe", "rect": [0, 0, 800, 600]}
SECURITY_WINDOW = {"hwnd": 102, "title": "Windows 安全", "class_name": "Credential Dialog Xaml Host",
                   "pid": 4, "process": "LogonUI.exe", "rect": [0, 0, 800, 600]}
OWN_WINDOW = {"hwnd": 103, "title": "Z-OPS Companion", "class_name": "#32770",
              "pid": os.getpid(), "process": "python.exe", "rect": [0, 0, 400, 300]}
SHELL_WINDOW = {"hwnd": 104, "title": "Program Manager", "class_name": "Progman",
                "pid": 999, "process": "explorer.exe", "rect": [0, 0, 800, 600]}


class FakeDriver:
    def __init__(self):
        self.windows = [dict(WINDOW), dict(SECURITY_WINDOW), dict(OWN_WINDOW), dict(SHELL_WINDOW)]
        self.approve_result = True
        self.on_approve = None
        self.locked = False
        self.tick = 1000
        self.prepared = False
        self.preflight_error = None
        self.requests = []
        self.actions = []
        self.foreground_hwnd = WINDOW["hwnd"]
        self.focus_ok = True
        self.end_lease_count = 0
        self.status = ""
        self.closed = False
        # 模拟 UIA 树: 包含非可激活控件、敏感名称、禁用项和空名称项。
        self.elements = [
            {"role": "button", "name": "保存", "enabled": True, "offscreen": False},
            {"role": "tabitem", "name": "文件", "enabled": True, "offscreen": False},
            {"role": "button", "name": "Reset password", "enabled": True, "offscreen": False},
            {"role": "button", "name": "删除", "enabled": False, "offscreen": False},
            {"role": "edit", "name": "内容", "enabled": True, "offscreen": False},
            {"role": "button", "name": "离屏", "enabled": True, "offscreen": True},
        ]
        self.invocations = []
        self.captures = 0
        self.sensitive = False
        self.last_approval = ""

    def prepare(self):
        if self.preflight_error:
            raise Denied(self.preflight_error)
        self.prepared = True

    def list_windows(self, limit):
        if self.locked:
            return True, []
        return False, [dict(w) for w in self.windows[:limit] if w["pid"] != os.getpid()
                       and w["process"].lower() not in {"logonui.exe", "consent.exe"}]

    def window(self, hwnd):
        for item in self.windows:
            if item["hwnd"] == hwnd:
                return dict(item)
        raise Denied("desktop_window_missing")

    def approve(self, request):
        self.requests.append(request)
        if self.on_approve:
            self.on_approve()
        return self.approve_result

    def secure(self):
        return self.locked

    def last_input(self):
        return self.tick

    def external_input_since(self, baseline):
        return (self.tick - baseline) & 0xFFFFFFFF > 100

    def mark_baseline(self):
        # 模拟 Companion 自己注入的输入推进系统“最后输入时间”。
        self.tick += 25
        return self.tick

    def same_window(self, identity):
        for item in self.windows:
            if item["hwnd"] == identity["hwnd"]:
                return item["pid"] == identity["pid"] and item["class_name"] == identity["class_name"]
        return False

    def foreground(self):
        return self.foreground_hwnd

    def focus(self, hwnd):
        if not self.focus_ok:
            raise Paused("desktop_focus_denied")
        self.foreground_hwnd = hwnd

    def begin_lease(self, window):
        self.status = "桌面接管中"

    def set_status(self, value):
        self.status = value

    def perform(self, action, params, window):
        assert self.foreground_hwnd == window["hwnd"]
        self.actions.append((action, dict(params)))

    def list_elements(self, hwnd, limit):
        out = []
        for item in self.elements:
            if item["role"] not in ELEMENT_TYPES or not item["name"].strip() or item["offscreen"]:
                continue
            out.append({"index": len(out), "role": item["role"], "name": item["name"],
                        "enabled": item["enabled"]})
        return out[:limit]

    def invoke_element(self, hwnd, element, role, name):
        items = self.list_elements(hwnd, 80)
        target = next((item for item in items if item["index"] == element), None)
        assert target is not None and target["role"] == role and target["name"] == name
        assert target["enabled"]
        assert self.foreground_hwnd == hwnd
        self.invocations.append((hwnd, element))

    def sensitive_focus(self, hwnd):
        return self.sensitive

    def capture_window(self, hwnd):
        assert hwnd == WINDOW["hwnd"]
        self.captures += 1
        return "ZmFrZS1qcGVn"

    def end_lease(self):
        self.end_lease_count += 1

    def close(self):
        self.closed = True


class DesktopSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "config.json"
        self.cfg = Config("https://example.test", "测试设备", device_id="device-123",
                          desktop_apps=["notepad.exe"], path=path).validate()
        self.clock = {"now": 1000.0}
        self.driver = FakeDriver()
        self.acceptance = {
            "version": 1, "device_id": "device-123", "platform": "windows",
            "accepted_version": __version__, "accepted_at": int(time.time()),
            "checks": {name: True for name in ACCEPTANCE_CHECKS},
        }
        self.desktop = Desktop(self.cfg, driver=self.driver, acceptance=self.acceptance,
                               platform="nt", clock=lambda: self.clock["now"])
        await self.desktop.prepare()
        self.task = {"device_id": "device-123", "task_id": "task-1", "session_id": "session-1"}

    async def asyncTearDown(self):
        await self.desktop.close()
        self.tmp.cleanup()

    async def approve(self, scopes=("click",)):
        return await self.desktop.request_control(
            {"window_id": 101, "scopes": list(scopes), "reason": "处理本机报表"}, self.task)

    async def test_disabled_without_local_acceptance(self):
        """没有本机验收记录时桌面能力必须为空（Linux 同样如此，不因平台而放宽）。"""
        desktop = Desktop(self.cfg, driver=FakeDriver(), acceptance=None, platform="linux")
        await desktop.prepare()
        self.assertFalse(desktop.enabled)
        self.assertEqual(desktop.capabilities, [])
        self.assertEqual(desktop.reason, "desktop_acceptance_required")
        with self.assertRaises(Denied):
            await desktop.execute("desktop.inspect", {}, {"task_id": "t", "session_id": "s"})

    async def test_approval_timeout_is_reported_apart_from_a_user_decline(self):
        """无人操作到期自动拒绝，要和用户点“拒绝”区分开，并且都不能产生租约。"""
        self.driver.approve_result, self.driver.last_approval = False, "timeout"
        with self.assertRaises(Paused) as caught:
            await self.approve()
        self.assertEqual(str(caught.exception), APPROVAL_TIMEOUT)
        self.assertEqual(self.desktop.suspended, "approval_timeout")
        self.assertIsNone(self.desktop.lease)

    async def test_user_decline_keeps_its_own_reason(self):
        self.driver.approve_result, self.driver.last_approval = False, "declined"
        with self.assertRaises(Paused) as caught:
            await self.approve()
        self.assertEqual(str(caught.exception), "desktop_approval_declined")
        self.assertEqual(self.desktop.suspended, "approval_declined")
        self.assertIsNone(self.desktop.lease)

    async def test_approval_window_ends_before_the_command_cap(self):
        """引擎按 min(60, expiry) 中止命令；对话框必须在它之前自动关闭。"""
        self.assertGreater(APPROVAL_SECONDS, 0)
        self.assertLess(APPROVAL_SECONDS, 60)

    async def test_linux_acceptance_cannot_impersonate_windows(self):
        """两个平台的验收项不同：Linux 记录不能当 Windows 用，反之亦然。"""
        linux = {"version": 1, "device_id": "device-123", "platform": "linux",
                 "accepted_version": __version__, "accepted_at": int(time.time()),
                 "checks": {name: True for name in ACCEPTANCE_CHECKS_X11}}
        validate_acceptance(linux, self.cfg, "posix")
        for mutant in ({**linux, "platform": "windows"},
                       {**linux, "checks": {**linux["checks"], "uia": True}},
                       dict(self.acceptance)):
            with self.assertRaises(Denied):
                validate_acceptance(mutant, self.cfg, "posix")

    async def test_linux_does_not_claim_window_capture(self):
        """X11 识别不了密码框，所以不声明 desktop.screen；直接调用也必须拒绝。"""
        acceptance = {"version": 1, "device_id": "device-123", "platform": "linux",
                      "accepted_version": __version__, "accepted_at": int(time.time()),
                      "checks": {name: True for name in ACCEPTANCE_CHECKS_X11}}
        desktop = Desktop(self.cfg, driver=FakeDriver(), acceptance=acceptance, platform="linux")
        await desktop.prepare()
        self.assertTrue(desktop.enabled)
        self.assertEqual(sorted(desktop.capabilities),
                         ["desktop.act", "desktop.inspect", "desktop.request_control"])
        # 启用了也要说清 desktop.screen 为什么没有：平台做不到，而不是尚未验收。
        self.assertEqual(desktop.unavailable["desktop.screen"],
                         "desktop_capture_not_accepted_on_platform")
        with self.assertRaises(Denied):
            await desktop.execute("desktop.screen", {}, {"task_id": "t", "session_id": "s"})

    async def test_wayland_display_without_session_type_is_still_refused(self):
        """有些会话不设 XDG_SESSION_TYPE，只留 WAYLAND_DISPLAY；DISPLAY 指向 XWayland，
        注入对用户真正在用的原生窗口无效，所以同样必须拒绝。"""
        acceptance = {"version": 1, "device_id": "device-123", "platform": "linux",
                      "accepted_version": __version__, "accepted_at": int(time.time()),
                      "checks": {name: True for name in ACCEPTANCE_CHECKS_X11}}
        env = {key: value for key, value in os.environ.items()
               if key not in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY")}
        env.update({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"})
        with mock.patch.dict(os.environ, env, clear=True):
            desktop = Desktop(self.cfg, driver=FakeDriver(), acceptance=acceptance,
                              platform="linux")
            await desktop.prepare()
        self.assertFalse(desktop.enabled)
        self.assertEqual(desktop.reason, "wayland_portal_not_implemented")
        self.assertEqual(desktop.capabilities, [])

    async def test_wayland_session_is_disabled_with_its_own_reason(self):
        """Wayland（含 XWayland）下 XTest 无效、Portal 未适配：必须给出可区分的原因。"""
        acceptance = {"version": 1, "device_id": "device-123", "platform": "linux",
                      "accepted_version": __version__, "accepted_at": int(time.time()),
                      "checks": {name: True for name in ACCEPTANCE_CHECKS_X11}}
        with mock.patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}):
            desktop = Desktop(self.cfg, driver=FakeDriver(), acceptance=acceptance,
                              platform="linux")
            await desktop.prepare()
        self.assertFalse(desktop.enabled)
        self.assertEqual(desktop.reason, "wayland_portal_not_implemented")
        self.assertEqual(desktop.capabilities, [])
        for capability in ("desktop.inspect", "desktop.request_control", "desktop.act",
                           "desktop.screen"):
            self.assertEqual(desktop.unavailable[capability], "wayland_portal_not_implemented")

    async def test_acceptance_must_match_device_version_and_all_checks(self):
        for mutant in (
            {**self.acceptance, "device_id": "other"},
            {**self.acceptance, "accepted_version": "0.0.0"},
            {**self.acceptance, "checks": {**self.acceptance["checks"], "hotkey": False}},
            {**self.acceptance, "platform": "windows", "version": 2},
        ):
            with self.assertRaises(Denied):
                validate_acceptance(mutant, self.cfg, "nt")
        desktop = Desktop(self.cfg, driver=FakeDriver(), acceptance={**self.acceptance, "device_id": "other"},
                          platform="nt")
        await desktop.prepare()
        self.assertFalse(desktop.enabled)
        self.assertEqual(desktop.reason, "desktop_acceptance_required")

    async def test_preflight_failure_disables_desktop(self):
        """预检失败要回具体固定码，不能只说一句 desktop_preflight_failed。"""
        driver = FakeDriver()
        driver.preflight_error = "emergency_hotkey_unavailable"
        desktop = Desktop(self.cfg, driver=driver, acceptance=self.acceptance, platform="nt")
        await desktop.prepare()
        self.assertFalse(desktop.enabled)
        self.assertEqual(desktop.reason, "emergency_hotkey_unavailable")
        self.assertTrue(driver.closed)

    async def test_status_exposes_preflight_checks_and_uia_summary(self):
        """--doctor 要能复核预检逐项与 UIA 实测，不能只回一句 enabled。"""
        self.assertEqual(self.desktop.status()["checks"], {})
        self.assertEqual(self.desktop.status()["uia"], {})
        self.driver.checks = {"uia": True, "hotkey": True}
        self.driver.uia_probe = {"windows": 5, "tree_readable": True}
        status = self.desktop.status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["checks"], {"uia": True, "hotkey": True})
        self.assertEqual(status["uia"]["windows"], 5)

    async def test_remote_approval_fields_cannot_bypass_native_dialog(self):
        with self.assertRaises(Denied):
            await self.desktop.execute(
                "desktop.request_control",
                {"window_id": 101, "scopes": ["click"], "reason": "x", "approved": True},
                self.task)
        self.driver.approve_result = False
        with self.assertRaises(Paused):
            await self.approve()
        self.assertIsNone(self.desktop.lease)
        self.assertEqual(self.desktop.suspended, "approval_declined")

    async def test_lease_binds_device_task_session_window_scope_and_60_seconds(self):
        result = await self.approve(("click", "type_text"))
        self.assertTrue(result["approved"])
        self.assertEqual(result["lease"]["expires_in"], 60)
        self.assertEqual(self.driver.requests[0]["device_id"], "device-123")
        self.assertEqual(self.driver.requests[0]["task_id"], "task-1")
        self.assertEqual(self.driver.requests[0]["session_id"], "session-1")
        self.assertEqual(self.driver.requests[0]["window"]["hwnd"], 101)
        self.assertEqual(self.driver.requests[0]["scopes"], ["click", "type_text"])
        self.assertEqual(self.driver.requests[0]["seconds"], 60)

        with self.assertRaises(Paused):
            self.desktop.act({"action": "click", "x": 10, "y": 10},
                             {**self.task, "task_id": "other"})
        self.assertIsNone(self.desktop.lease)
        await self.approve(("click",))
        with self.assertRaises(Paused):
            self.desktop.act({"action": "type_text", "text": "x"}, self.task)
        self.assertIsNone(self.desktop.lease)
        await self.approve(("click",))
        self.clock["now"] += 60
        with self.assertRaises(Paused):
            self.desktop.act({"action": "click", "x": 10, "y": 10}, self.task)
        self.assertIsNone(self.desktop.lease)
        self.assertEqual(self.desktop.suspended, "lease_expired")
        self.assertFalse(hasattr(Lease, "renew"))

    async def test_user_input_window_change_and_lock_suspend(self):
        await self.approve(("click",))
        self.driver.tick += 500
        with self.assertRaises(Paused):
            self.desktop.act({"action": "click", "x": 10, "y": 10}, self.task)
        self.assertEqual(self.desktop.suspended, "user_input")

        self.driver.tick = 1000
        await self.approve(("click",))
        self.driver.windows[0]["class_name"] = "Replaced"
        with self.assertRaises(Paused):
            self.desktop.act({"action": "click", "x": 10, "y": 10}, self.task)
        self.assertEqual(self.desktop.suspended, "target_window_changed")

        self.driver.windows[0]["class_name"] = "Notepad"
        self.driver.tick = 1000
        await self.approve(("click",))
        self.driver.locked = True
        with self.assertRaises(Paused):
            self.desktop.act({"action": "click", "x": 10, "y": 10}, self.task)
        self.assertEqual(self.desktop.suspended, "secure_desktop")

    async def test_disconnect_and_emergency_stop_release_lease(self):
        await self.approve(("click",))
        await self.desktop.release()
        self.assertIsNone(self.desktop.lease)
        with self.assertRaises(Paused):
            self.desktop.act({"action": "click", "x": 10, "y": 10}, self.task)

        self.driver.tick = 1000
        await self.approve(("click",))
        before = self.driver.end_lease_count
        self.desktop.emergency_stop()
        self.assertTrue(self.desktop.revoked)
        self.assertIsNone(self.desktop.lease)
        self.assertGreater(self.driver.end_lease_count, before)
        with self.assertRaises(Paused):
            self.desktop.act({"action": "click", "x": 10, "y": 10}, self.task)

    async def test_own_injected_input_does_not_suspend_but_user_input_does(self):
        await self.approve(("click",))
        for _ in range(8):
            self.desktop.act({"action": "click", "x": 10, "y": 10}, self.task)
        # Companion 自己注入的输入会推进系统最后输入时间，不能把自己当成用户输入。
        self.assertIsNotNone(self.desktop.lease)
        self.assertEqual(self.desktop.suspended, "")
        self.assertEqual(len(self.driver.actions), 8)
        self.driver.tick += 500
        with self.assertRaises(Paused):
            self.desktop.act({"action": "click", "x": 10, "y": 10}, self.task)
        self.assertEqual(self.desktop.suspended, "user_input")

    async def test_emergency_stop_ends_lease_but_new_native_approval_still_works(self):
        await self.approve(("click",))
        self.desktop.emergency_stop()
        self.assertTrue(self.desktop.revoked)
        with self.assertRaises(Paused):
            self.desktop.act({"action": "click", "x": 10, "y": 10}, self.task)
        # 紧急停止不保留永久同意；再次接管必须重新本机批准，而不是要求重启 Companion。
        self.driver.tick = 1000
        result = await self.approve(("click",))
        self.assertTrue(result["approved"])
        self.assertFalse(self.desktop.revoked)
        self.assertIsNotNone(self.desktop.lease)
        self.desktop.act({"action": "click", "x": 10, "y": 10}, self.task)

    async def test_uia_elements_are_read_only_filtered_and_allowlisted(self):
        view = self.desktop.inspect({"window_id": 101})
        self.assertEqual(view["window"]["hwnd"], 101)
        self.assertEqual([(e["index"], e["role"]) for e in view["elements"]],
                         [(0, "button"), (1, "tabitem"), (2, "button"), (3, "button")])
        # 敏感名称以占位符返回, 文本编辑框与离屏控件不出现。
        self.assertEqual([e["name"] for e in view["elements"]],
                         ["保存", "文件", "[敏感控件]", "删除"])
        self.assertFalse(view["elements"][3]["enabled"])
        self.assertNotIn("内容", str(view))
        extra = {"hwnd": 105, "title": "计算器", "class_name": "ApplicationFrameWindow",
                 "pid": 5252, "process": "CalculatorApp.exe", "rect": [0, 0, 400, 300]}
        self.driver.windows.append(extra)
        with self.assertRaises(Denied):
            self.desktop.inspect({"window_id": 105})

    async def test_uia_invoke_requires_lease_scope_and_exact_element(self):
        with self.assertRaises(Paused):
            self.desktop.act({"action": "invoke", "element": 0, "role": "button", "name": "保存"},
                             self.task)
        await self.approve(("click",))
        with self.assertRaises(Paused):
            self.desktop.act({"action": "invoke", "element": 0, "role": "button", "name": "保存"},
                             self.task)
        self.assertEqual(self.desktop.suspended, "action_not_approved")
        self.assertEqual(self.driver.invocations, [])

        await self.approve(("invoke",))
        result = self.desktop.act(
            {"action": "invoke", "element": 0, "role": "button", "name": "保存"}, self.task)
        self.assertEqual(result["element"], {"index": 0, "role": "button"})
        self.assertNotIn("保存", str(result["element"]))
        self.assertEqual(self.driver.invocations, [(101, 0)])
        # 元素被替换/改名、禁用、敏感或不存在的下标都拒绝, 且不会回退成坐标点击。
        for bad in ({"element": 0, "role": "button", "name": "另存为"},
                    {"element": 2, "role": "button", "name": "[敏感控件]"},
                    {"element": 3, "role": "button", "name": "删除"},
                    {"element": 9, "role": "button", "name": "保存"},
                    {"element": 1, "role": "edit", "name": "文件"}):
            with self.assertRaises(Denied):
                self.desktop.act({"action": "invoke", **bad}, self.task)
        self.assertEqual(self.driver.invocations, [(101, 0)])
        self.assertEqual(self.driver.actions, [])

    async def test_desktop_frame_requires_view_scope_and_pauses_on_sensitive_focus(self):
        with self.assertRaises(Paused):
            self.desktop.screen({}, self.task)
        await self.approve(("click",))
        with self.assertRaises(Paused):
            self.desktop.screen({}, self.task)
        self.assertEqual(self.desktop.suspended, "action_not_approved")
        self.assertEqual(self.driver.captures, 0)

        self.driver.tick = 1000
        await self.approve(("view",))
        frame = self.desktop.screen({}, self.task)
        self.assertEqual(frame["jpeg"], "ZmFrZS1qcGVn")
        self.assertEqual(frame["execution_mode"], "desktop")
        self.assertNotIn("hwnd", frame)
        self.assertEqual(self.driver.captures, 1)

        # 密码框/锁屏期间不采集, 但租约仍可用; 非空参数一律拒绝。
        self.driver.sensitive = True
        self.assertEqual(self.desktop.screen({}, self.task),
                         {"unavailable": "sensitive_input", "window": "无标题 - 记事本"})
        self.assertEqual(self.driver.captures, 1)
        self.assertIsNotNone(self.desktop.lease)
        self.driver.sensitive = False
        with self.assertRaises(Denied):
            self.desktop.screen({"jpeg": "x"}, self.task)

        self.clock["now"] += 60
        with self.assertRaises(Paused):
            self.desktop.screen({}, self.task)
        self.assertEqual(self.desktop.suspended, "lease_expired")

    async def test_security_and_shell_windows_are_never_targets(self):
        for hwnd in (102, 103, 104):
            with self.assertRaises(Denied):
                await self.desktop.request_control(
                    {"window_id": hwnd, "scopes": ["click"], "reason": "x"}, self.task)
        view = self.desktop.inspect({})
        self.assertFalse(view["locked"])
        self.assertEqual([w["hwnd"] for w in view["windows"]], [101])
        self.driver.locked = True
        view = self.desktop.inspect({})
        self.assertTrue(view["locked"])
        self.assertEqual(view["windows"], [])

    async def test_desktop_apps_allowlist_gates_inspect_and_control(self):
        extra = {"hwnd": 105, "title": "计算器", "class_name": "ApplicationFrameWindow",
                 "pid": 5252, "process": "CalculatorApp.exe", "rect": [0, 0, 400, 300]}
        self.driver.windows.append(extra)
        view = self.desktop.inspect({})
        self.assertEqual([w["hwnd"] for w in view["windows"]], [101])
        with self.assertRaises(Denied):
            await self.desktop.request_control(
                {"window_id": 105, "scopes": ["click"], "reason": "x"}, self.task)
        self.assertEqual(self.driver.requests, [])
        # 验收通过但未授权任何应用: 既看不到窗口, 也不能接管。
        empty_cfg = Config("https://example.test", "测试设备", device_id="device-123")
        other = FakeDriver()
        desktop = Desktop(empty_cfg, driver=other, acceptance=self.acceptance, platform="nt")
        await desktop.prepare()
        self.assertTrue(desktop.enabled)
        self.assertEqual(desktop.inspect({})["windows"], [])
        with self.assertRaises(Denied):
            await desktop.request_control(
                {"window_id": 101, "scopes": ["click"], "reason": "x"}, self.task)
        self.assertEqual(other.requests, [])
        await desktop.close()

    def test_desktop_app_names_are_canonical_and_bounded(self):
        Config("https://example.test", "设备", desktop_apps=["notepad.exe", "calc.exe"]).validate()
        for bad in (["Notepad.exe"], ["../notepad.exe"], ["notepad.exe "], [""], ["a" * 65],
                    ["calc.exe", "calc.exe"], [f"app{i}.exe" for i in range(17)]):
            with self.assertRaises(Denied):
                Config("https://example.test", "设备", desktop_apps=bad).validate()

    async def test_target_replaced_during_native_dialog_is_rejected(self):
        def replace_window():
            self.driver.windows[0]["class_name"] = "OtherApp"
        self.driver.on_approve = replace_window
        with self.assertRaises(Denied):
            await self.approve()

    async def test_click_outside_window_and_focus_denied_pause(self):
        await self.approve(("click",))
        with self.assertRaises(Denied):
            self.desktop.act({"action": "click", "x": 9000, "y": 10}, self.task)
        self.assertIsNotNone(self.desktop.lease)
        with self.assertRaises(Paused):
            self.desktop.act({"action": "type_text", "text": "x"}, self.task)
        self.assertIsNone(self.desktop.lease)

        self.driver.tick = 1000
        self.driver.focus_ok = False
        await self.approve(("click",))
        with self.assertRaises(Paused):
            self.desktop.act({"action": "click", "x": 10, "y": 10}, self.task)
        self.assertIsNone(self.desktop.lease)

    async def test_keys_are_limited_and_system_shortcuts_blocked(self):
        _validate_keys(["ctrl", "c"])
        _validate_keys(["shift", "f5"])
        for value in (["win", "r"], ["ctrl", "alt", "delete"], ["ctrl", "shift", "escape"],
                      ["alt", "tab"], ["ctrl"], ["ctrl", "c", "v"]):
            with self.assertRaises(Denied):
                _validate_keys(value)

    async def test_approval_dialog_request_has_no_secret_or_full_path(self):
        await self.approve(("click",))
        body = str(self.driver.requests[0])
        self.assertNotIn("/home/", body)
        self.assertNotIn(str(self.cfg.path), body)
        self.assertNotIn("password", body.lower())

    def test_acceptance_uia_apps_must_be_readable_and_bounded(self):
        # 验收记录只能携带真实可读的 UIA 实测结果, 不含控件名称或内容。
        data = {**self.acceptance,
                "uia_apps": {"notepad.exe": {"readable": True, "elements": 12}}}
        self.assertEqual(validate_acceptance(data, self.cfg, "nt")["uia_apps"],
                         {"notepad.exe": {"readable": True, "elements": 12}})
        for bad in ({"notepad.exe": {"readable": False, "elements": 3}},
                    {"notepad.exe": {"readable": True, "elements": 999}},
                    {"notepad.exe": {"readable": True}},
                    {}, {"": {"readable": True, "elements": 1}}):
            with self.assertRaises(Denied):
                validate_acceptance({**self.acceptance, "uia_apps": bad}, self.cfg, "nt")

    def test_uia_app_report_only_counts_standard_and_granted_apps(self):
        windows = [{"hwnd": 1, "process": "notepad.exe"}, {"hwnd": 2, "process": "other.exe"},
                   {"hwnd": 3, "process": "Calc.exe"}, {"hwnd": 4, "process": "crm.exe"}]

        class ReportDriver(WindowsDriver):
            def list_windows(self, limit):
                return False, [dict(window) for window in windows[:limit]]

            def list_elements(self, hwnd, limit):
                if hwnd == 3:
                    raise Denied("uia_unavailable")
                return [{"index": index} for index in range(4)]

        report = ReportDriver().uia_app_report(["crm.exe"])
        self.assertEqual(report, {"notepad.exe": {"readable": True, "elements": 4},
                                  "calc.exe": {"readable": False, "elements": 0},
                                  "crm.exe": {"readable": True, "elements": 4}})
        # 未授权且非标准的应用不入报告。
        self.assertNotIn("other.exe", report)


if __name__ == "__main__":
    unittest.main()
