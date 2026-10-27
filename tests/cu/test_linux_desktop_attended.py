"""Linux 人工验收执行器的判定测试：只认观察到的状态，不认“应该做了”。

真实对话框/热键/锁屏需要人在机旁，这里用替身验证判定逻辑本身：
批准无租约、拒绝不结束、到期被当拒绝、用户输入后仍执行、锁屏未识别、热键未生效，
任一情况都必须判未通过。
"""
import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "linux_desktop_attended.py"
spec = importlib.util.spec_from_file_location("linux_desktop_attended", SCRIPT)
attended_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(attended_module)

import psutil  # noqa: E402

from companion.security import Denied, Paused  # noqa: E402


class FakeLease:
    approval_id = "a1"
    baseline = 100.0


class FakeDriver:
    def __init__(self):
        self.secure_state = False
        self.external = False
        self.foreground_id = 777
        self.deny = ""

    def secure(self):
        return self.secure_state

    def external_input_since(self, baseline):
        return self.external

    def foreground(self):
        return self.foreground_id


class FakeDesktop:
    """按 mode 模拟 request_control 的结果；act 只在 driver.deny 为空时执行。"""

    def __init__(self, mode="approve", log_path=None):
        self.mode = mode
        self.config = SimpleNamespace(device_id="device-123")
        self.driver = FakeDriver()
        self.log_path = log_path
        self.lease = None
        self.revoked = False
        self.suspended = ""
        self.requests = 0
        self.acts = 0
        self.released = 0

    async def request_control(self, params, task):
        self.requests += 1
        if self.lease is not None:
            # 与真实 Desktop 一致：已有租约时不再弹对话框，直接拒绝。
            raise Denied("desktop_lease_active")
        if self.mode == "approve":
            self.lease = FakeLease()
            return {"approved": True,
                    "lease": {"approval_id": "a1", "expires_in": 60, "scopes": ["click"]}}
        if self.mode == "decline":
            self.suspended = "approval_declined"
            raise Paused("desktop_approval_declined")
        if self.mode == "timeout":
            self.suspended = "approval_timeout"
            raise Paused("desktop_approval_timeout")
        if self.mode == "expired_with_lease":
            self.lease = FakeLease()
            raise Paused("desktop_approval_timeout")
        if self.mode == "revoked":
            # 批准后立刻按了停止热键：租约终结、revoked 置位。
            self.revoked = True
            return {"approved": True,
                    "lease": {"approval_id": "a1", "expires_in": 60, "scopes": ["click"]}}
        return {"approved": False, "lease": {}}

    def act(self, params, task):
        # 与真实 `Desktop.act` 一致：同步方法。写成 async 会把执行器里的多余 await 掩掉。
        self.acts += 1
        if self.driver.deny:
            raise Paused(self.driver.deny)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as stream:
                stream.write("CLICK 1 2\n")
        return {"action": params["action"]}

    async def release(self):
        self.released += 1
        self.lease = None

    async def close(self):
        pass


def window():
    return {"hwnd": 501, "title": "Z-OPS 验收窗口", "class_name": "ZopsTarget", "pid": 4242,
            "process": "python3", "rect": [80, 60, 560, 320]}


class StepTests(unittest.IsolatedAsyncioTestCase):
    def build(self, desktop, log_path=None, ask=None):
        return attended_module.Attended(desktop, window(), log_path=log_path,
                                        ask=ask or (lambda _prompt: None), poll_seconds=0.3)

    async def test_approve_requires_lease_and_real_injection(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "events.log")
        desktop = FakeDesktop("approve", log_path=log)
        result = await self.build(desktop, log).run("approve")
        self.assertEqual(result["status"], "pass", result)
        self.assertIn("CLICK 1 2", result["evidence"]["click_log"])
        self.assertEqual(desktop.released, 1)

    async def test_approve_without_lease_fails(self):
        desktop = FakeDesktop("silent")
        result = await self.build(desktop, None).run("approve")
        self.assertEqual(result["status"], "fail")
        self.assertIn("没有建立租约", result["detail"])

    async def test_approve_without_reaching_the_app_fails(self):
        desktop = FakeDesktop("approve", log_path=None)
        result = await self.build(desktop, None).run("approve")
        self.assertEqual(result["status"], "fail")
        self.assertIn("注入没有到达应用", result["detail"])

    async def test_reject_must_end_without_lease(self):
        result = await self.build(FakeDesktop("decline")).run("reject")
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["evidence"]["pause_reason"], "desktop_approval_declined")

    async def test_reject_reported_as_timeout_fails(self):
        desktop = FakeDesktop("timeout")
        result = await self.build(desktop).run("reject")
        self.assertEqual(result["status"], "fail")
        self.assertIn("应与用户拒绝一致", result["detail"])

    async def test_timeout_requires_the_timeout_code_and_no_lease(self):
        with mock.patch("companion.desktop.APPROVAL_SECONDS", 1):
            result = await self.build(FakeDesktop("timeout")).run("timeout")
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["evidence"]["pause_reason"], "desktop_approval_timeout")

    async def test_lease_created_by_an_unattended_dialog_fails(self):
        with mock.patch("companion.desktop.APPROVAL_SECONDS", 1):
            result = await self.build(FakeDesktop("expired_with_lease")).run("timeout")
        self.assertEqual(result["status"], "fail")
        self.assertIn("仍有租约", result["detail"])

    async def test_timeout_that_returns_too_early_fails(self):
        with mock.patch("companion.desktop.APPROVAL_SECONDS", 30):
            result = await self.build(FakeDesktop("timeout")).run("timeout")
        self.assertEqual(result["status"], "fail")
        self.assertIn("早于", result["detail"])

    async def test_typing_interference_requires_user_input_and_no_focus_theft(self):
        desktop = FakeDesktop("approve")
        desktop.driver.external = True
        desktop.driver.deny = "desktop_control_suspended"
        desktop.suspended = "user_input"
        result = await self.build(desktop).run("typing_interference")
        self.assertEqual(result["status"], "pass", result)
        self.assertTrue(result["evidence"]["foreground_unchanged"])

    async def test_typing_interference_without_real_input_fails(self):
        desktop = FakeDesktop("approve")
        desktop.driver.deny = "desktop_control_suspended"
        desktop.suspended = "user_input"
        result = await self.build(desktop).run("typing_interference")
        self.assertEqual(result["status"], "fail")
        self.assertIn("没有检测到用户真实输入", result["detail"])

    async def test_typing_interference_with_focus_steal_fails(self):
        desktop = FakeDesktop("approve")
        desktop.driver.external = True
        desktop.driver.deny = "desktop_control_suspended"
        desktop.suspended = "user_input"
        original = attended_module.Attended.act_click

        async def steal(self_):
            desktop.driver.foreground_id = 999
            return await original(self_)

        with mock.patch.object(attended_module.Attended, "act_click", steal):
            result = await self.build(desktop).run("typing_interference")
        self.assertEqual(result["status"], "fail")
        self.assertIn("抢走了焦点", result["detail"])

    async def test_typing_interference_that_still_acts_fails(self):
        desktop = FakeDesktop("approve")
        desktop.driver.external = True
        result = await self.build(desktop).run("typing_interference")
        self.assertEqual(result["status"], "fail")
        self.assertIn("仍然执行", result["detail"])

    async def test_lock_screen_requires_the_secure_state_to_be_observed(self):
        desktop = FakeDesktop("approve")
        result = await self.build(desktop).run("lock_screen")
        self.assertEqual(result["status"], "fail")
        self.assertIn("没有观察到锁屏", result["detail"])

    async def test_lock_screen_pass_needs_the_secure_pause_reason(self):
        desktop = FakeDesktop("approve")
        desktop.driver.secure_state = True
        desktop.driver.deny = "desktop_control_suspended"
        desktop.suspended = "secure_desktop"
        result = await self.build(desktop).run("lock_screen")
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["evidence"]["suspended"], "secure_desktop")

    async def test_lock_screen_with_actions_still_running_fails(self):
        desktop = FakeDesktop("approve")
        desktop.driver.secure_state = True
        result = await self.build(desktop).run("lock_screen")
        self.assertEqual(result["status"], "fail")
        self.assertIn("仍然执行", result["detail"])

    async def test_revoke_requires_the_hotkey_to_take_effect(self):
        desktop = FakeDesktop("approve")
        result = await self.build(desktop).run("revoke")
        self.assertEqual(result["status"], "fail")
        self.assertIn("没有观察到热键", result["detail"])

    async def test_revoke_pass_after_emergency_stop(self):
        desktop = FakeDesktop("revoked")
        desktop.driver.deny = "desktop_control_not_approved"
        result = await self.build(desktop).run("revoke")
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["evidence"]["pause_reason"], "desktop_control_not_approved")

    async def test_revoke_with_surviving_lease_fails(self):
        desktop = FakeDesktop("approve")
        desktop.revoked = True
        result = await self.build(desktop).run("revoke")
        self.assertEqual(result["status"], "fail")
        self.assertIn("租约仍然存在", result["detail"])

    async def test_no_one_at_the_keyboard_is_not_a_pass(self):
        def no_input(_prompt):
            raise EOFError

        result = await self.build(FakeDesktop("approve"), ask=no_input).run("approve")
        self.assertEqual(result["status"], "fail")
        self.assertIn("没有人在机旁", result["detail"])

    async def test_unexpected_error_is_a_failure_not_a_pass(self):
        class Broken(FakeDesktop):
            async def request_control(self, params, task):
                raise RuntimeError("boom")

        result = await self.build(Broken()).run("approve")
        self.assertEqual(result["status"], "fail")
        self.assertIn("boom", result["detail"])

    async def test_failed_step_does_not_poison_the_next_step(self):
        """中途失败留下的租约必须自己收尾，否则下一步只会看到 desktop_lease_active。"""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "events.log")
        desktop = FakeDesktop("approve", log_path=log)
        attended = self.build(desktop, log)
        first = await attended.run("typing_interference")  # 没有真实输入 → fail，且留下租约
        self.assertEqual(first["status"], "fail")
        self.assertIn("没有检测到用户真实输入", first["detail"])
        self.assertIsNone(desktop.lease)
        second = await attended.run("approve")
        self.assertEqual(second["status"], "pass", second)
        self.assertEqual(desktop.requests, 2)

    async def test_settle_survives_a_release_error(self):
        desktop = FakeDesktop("approve")

        async def broken_release():
            raise RuntimeError("driver_gone")

        desktop.release = broken_release
        attended = self.build(desktop)
        await attended.request()
        await attended.settle()  # 不抛异常


class ReportTests(unittest.TestCase):
    def test_markdown_lists_every_step_and_the_verdict(self):
        report = {"steps": [{"id": "approve", "status": "pass", "detail": "ok"},
                            {"id": "reject", "status": "fail", "detail": "没结束"}],
                  "accepted": False}
        text = attended_module.markdown(report)
        self.assertIn("| approve | pass | ok |", text)
        self.assertIn("未通过（reject）", text)


def _free_display():
    for number in range(90, 160):
        if not os.path.exists("/tmp/.X%d-lock" % number) \
                and not os.path.exists("/tmp/.X11-unix/X%d" % number):
            return number
    return None


@unittest.skipUnless(shutil.which("Xvfb") and (ROOT / "scripts" / "x11_target_window.py").exists(),
                     "需要 Xvfb 与验收目标窗口")
class RealTargetTest(unittest.TestCase):
    """真实 X 服务器上确认执行器能启动目标、读到它的 xid 与进程名。"""

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
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if subprocess.run(["xdpyinfo"], env=cls.env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                return
            time.sleep(0.1)
        cls.tearDownClass()
        raise unittest.SkipTest("Xvfb 未能就绪")

    @classmethod
    def tearDownClass(cls):
        xvfb = getattr(cls, "xvfb", None)
        if xvfb is not None:
            xvfb.terminate()
            try:
                xvfb.wait(5)
            except Exception:
                xvfb.kill()

    def test_target_is_started_and_labelled(self):
        with mock.patch.dict(os.environ, self.env):
            process, xid, log_path = attended_module.start_target(seconds=30)
            try:
                self.assertGreater(xid, 0)
                self.assertEqual(attended_module._window_process(xid),
                                 psutil.Process(process.pid).name().lower())
                with open(log_path, encoding="utf-8") as stream:
                    self.assertIn("READY pid=", stream.read())
            finally:
                process.terminate()
                try:
                    process.wait(5)
                except Exception:
                    process.kill()


if __name__ == "__main__":
    unittest.main()
