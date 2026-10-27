"""真实对话框 + 真实注入的端到端验证（opt-in）。

用 XTest 充当“坐在机器前的人”：按下按钮的是机器替身，所以这里**不是人工验收结论**，
只证明 attended 执行器与真实对话框、60 秒租约、XTest 注入这条链路能跑通——这条链路
曾被 async 替身掩盖成 TypeError（真实 `Desktop.act` 是同步的）。五个用例分别是批准、
拒绝、到期（不按键，必须报 `desktop_approval_timeout` 而不是拒绝）、紧急停止（`Ctrl+Alt+Pause`）
和切窗打字（在第二窗口真实输入 → `user_input` 暂停且不抢焦点）。锁屏不在内：必须有真实锁屏器。

默认跳过；设 `ZOPS_LINUX_DESKTOP_LIVE=1` 才运行，需要 Xvfb 与 tkinter。
"""
import asyncio  # noqa: F401  (IsolatedAsyncioTestCase 依赖事件循环)
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "linux_desktop_attended_live", ROOT / "scripts" / "linux_desktop_attended.py")
attended_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(attended_module)

LIVE = os.environ.get("ZOPS_LINUX_DESKTOP_LIVE") == "1"
DIALOG_TITLE = "本机桌面接管"


def start_xvfb():
    """起一个独立显示器；返回 (进程, DISPLAY)。已占用的号码直接跳过。"""
    for number in range(90, 100):
        if Path("/tmp/.X%d-lock" % number).exists():
            continue
        display = ":%d" % number
        process = subprocess.Popen(["Xvfb", display, "-screen", "0", "1280x800x24"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        if process.poll() is None:
            return process, display
    raise unittest.SkipTest("Xvfb 不可用")


def press_in_dialog(display, keys, seconds=40):
    """机器替身：等原生对话框出现，再按脚本完成选择与后续动作。

    脚本项：`tab`/`space`；`sleep:秒`；`focus:窗口标题`；`type:文本`；`hotkey:ctrl+alt+pause`。
    """
    from companion.x11 import X11

    backend = X11()
    try:
        deadline = time.monotonic() + seconds
        window = None
        while time.monotonic() < deadline:
            for item in backend.windows(50):
                if DIALOG_TITLE in (item.get("title") or ""):
                    window = item
                    break
            if window:
                break
            time.sleep(0.2)
        if not window:
            return False
        backend.set_focus(window["hwnd"])
        time.sleep(0.4)
        for key in keys:
            if key.startswith("sleep:"):
                time.sleep(float(key.split(":", 1)[1]))
            elif key.startswith("focus:"):
                title = key.split(":", 1)[1]
                target = next((item for item in backend.windows(50)
                               if title in (item.get("title") or "")), None)
                if target is None:
                    return False
                backend.set_focus(target["hwnd"])
            elif key.startswith("type:"):
                backend.type_text(key.split(":", 1)[1])
            elif key.startswith("hotkey:"):
                backend.press(key.split(":", 1)[1].split("+"))
            elif key == "space":
                backend.type_text(" ")
            else:
                backend.press([key])
            time.sleep(0.3)
        return True
    finally:
        backend.close()


def start_second_window(title, seconds=120):
    """第二窗口只用来把焦点从目标窗口移走（用户切窗场景）。"""
    directory = tempfile.mkdtemp(prefix="zops-live2-")
    log_path = os.path.join(directory, "target.log")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "x11_target_window.py"),
         "--log", log_path, "--title", title, "--seconds", str(seconds)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if attended_module._read_ready(log_path):
            return process
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise unittest.SkipTest("第二窗口没有启动，无法验证切窗干扰")


@unittest.skipUnless(LIVE, "设置 ZOPS_LINUX_DESKTOP_LIVE=1 才跑真实对话框链路")
class LiveDialogTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(5)
            except Exception:
                process.kill()
                process.wait(5)

    def setUp(self):
        from companion.config import Config
        from companion.desktop import ACCEPTANCE_CHECKS_X11, write_acceptance

        self.xvfb, self.display = start_xvfb()
        self.addCleanup(self.stop, self.xvfb)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE")}
        self.env.update({"DISPLAY": self.display, "XDG_SESSION_TYPE": "x11",
                         "WAYLAND_DISPLAY": ""})
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.target, self.xid, self.log_path = attended_module.start_target()
        self.addCleanup(self.stop, self.target)
        self.tmp = tempfile.mkdtemp(prefix="zops-live-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        app = attended_module._window_process(self.xid)
        cfg = Config("https://example.invalid", "live-check", device_id="device-live",
                     desktop_apps=[app], path=Path(self.tmp) / "config.json")
        cfg.validate()
        cfg.path.parent.chmod(0o700)
        cfg.save()
        write_acceptance(cfg, {name: True for name in ACCEPTANCE_CHECKS_X11})
        self.cfg = cfg

    async def make_attended(self):
        """每个用例都用真实 Desktop/真实窗口，收尾由清理钩子释放。"""
        from companion.desktop import Desktop, load_acceptance

        desktop = Desktop(self.cfg, acceptance=load_acceptance(self.cfg), platform="linux")
        await desktop.prepare()
        self.assertTrue(desktop.enabled, desktop.reason)
        self.addAsyncCleanup(desktop.close)
        window = desktop.driver.window(self.xid)
        attended = attended_module.Attended(desktop, window, log_path=self.log_path,
                                            ask=lambda _prompt: None, poll_seconds=30)
        return desktop, attended

    def press(self, keys):
        bot = threading.Thread(target=press_in_dialog, args=(self.display, keys), daemon=True)
        bot.start()
        return bot

    async def test_approve_through_the_real_dialog_reaches_the_app(self):
        desktop, attended = await self.make_attended()
        self.press(["tab", "tab", "space"])
        result = await attended.run("approve")
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["evidence"]["expires_in"], 60)
        self.assertIn("CLICK", result["evidence"]["click_log"])

    async def test_reject_through_the_real_dialog_leaves_no_lease(self):
        desktop, attended = await self.make_attended()
        self.press(["tab", "space"])
        result = await attended.run("reject")
        self.assertEqual(result["status"], "pass", result)
        # 拒绝必须报成拒绝，不能被当成到期（两者在真机上很容易混）。
        self.assertEqual(result["evidence"]["pause_reason"], "desktop_approval_declined")
        self.assertIsNone(desktop.lease)

    async def test_timeout_is_reported_as_expiry_not_as_a_refusal(self):
        desktop, attended = await self.make_attended()
        # 不按任何键：对话框自身 30 秒到期，绝不能默认同意。
        result = await attended.run("timeout")
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["evidence"]["pause_reason"], "desktop_approval_timeout")
        self.assertGreaterEqual(result["evidence"]["elapsed_s"], 25)
        self.assertIsNone(desktop.lease)

    async def test_emergency_stop_hotkey_voids_the_lease(self):
        desktop, attended = await self.make_attended()
        # 先批准拿到租约，再由“人”按 Ctrl+Alt+Pause。
        self.press(["tab", "tab", "space", "sleep:3", "hotkey:ctrl+alt+pause"])
        result = await attended.run("revoke")
        self.assertEqual(result["status"], "pass", result)
        self.assertTrue(result["evidence"]["revoked"])
        self.assertEqual(result["evidence"]["suspended"], "emergency_stop")
        self.assertIsNone(desktop.lease)

    async def test_user_typing_in_another_window_pauses_without_stealing_focus(self):
        desktop, attended = await self.make_attended()
        second = start_second_window("Z-OPS 第二窗口")
        self.addCleanup(self.stop, second)
        self.press(["tab", "tab", "space", "sleep:3", "focus:Z-OPS 第二窗口", "type:abc"])
        result = await attended.run("typing_interference")
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["evidence"]["suspended"], "user_input")
        self.assertTrue(result["evidence"]["foreground_unchanged"])
        # 收尾时租约必须自己释放，否则下一步只会看到 desktop_lease_active。
        self.assertIsNone(desktop.lease)


if __name__ == "__main__":
    unittest.main()
