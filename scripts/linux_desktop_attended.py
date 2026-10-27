"""Linux X11 桌面人工验收步骤执行器（Spec 第 51、52、76、82 条）。

`linux_desktop_check.py` 的 6 项 manual 步骤原先只是“你做了吗”的问答：没有真实任务
就没有批准对话框，人在机旁也无法完成。这个脚本把同一批步骤接到真实 Companion 代码路径
（`Desktop` → 原生对话框 → 60 秒租约 → XTest 注入 → 热键/锁屏/输入探测），逐步观察状态
并留下证据；输出可直接交给 `linux_desktop_check.py --attended`。

它不代替人：每一步都要人在机旁点对话框、按热键、锁屏或真的打字。没人回答、拒绝配合、
或观察不到约定状态，都按未通过处理，绝不默认通过。
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# (id, 人工动作, 判定标准)
STEPS = (
    ("approve", "在原生对话框点“允许”", "批准后得到 60 秒租约，且注入的点击真的到达目标应用"),
    ("reject", "在原生对话框点“拒绝”", "立刻结束、不产生租约，原因与“到期”区分"),
    ("timeout", "不要碰对话框，等它自己关闭", "到期自动拒绝（不默认同意），且原因码是到期"),
    ("typing_interference", "切到别的窗口用真实键盘打字",
     "下一次动作前就暂停在用户输入上，且不抢回焦点"),
    ("lock_screen", "锁屏（例如 Super+L），脚本确认后再解锁", "锁屏期间动作被拒绝，暂停原因是安全桌面"),
    ("revoke", "按 Ctrl+Alt+Pause", "租约立即作废，后续动作被拒绝，再接管要重新批准"),
)
STEP_IDS = tuple(step[0] for step in STEPS)
# revoke 会终止当前热键线程，放在最后；其它步骤互不依赖。
DEFAULT_ORDER = ("approve", "reject", "timeout", "typing_interference", "lock_screen", "revoke")
# 对话框类步骤的对话框在按回车**之后**才弹出；提示不能写成“完成后按回车”，
# 否则人会在还没出现窗口时就开始找按钮，到期步骤也会被误以为已经开始了。
DIALOG_INTRO = {
    "approve": "按回车后会弹出原生对话框；请在对话框里点“允许”，完成后按回车：",
    "reject": "按回车后会弹出原生对话框；请在对话框里点“拒绝”，完成后按回车：",
    "timeout": "按回车后会弹出原生对话框；不要碰它，等它自己关闭后按回车：",
}
POLL_SECONDS = 120


class StepFailed(Exception):
    """步骤没达到判定标准：只记原因，不重试、不推断。"""


class Attended:
    """把人工动作接进真实 `Desktop`；每次判定都基于观察到的状态，不基于用户口述。"""

    def __init__(self, desktop, window, log_path=None, ask=input, poll_seconds=POLL_SECONDS):
        self.desktop = desktop
        self.window = dict(window)
        self.log_path = log_path
        self.ask = ask
        self.poll_seconds = poll_seconds

    @property
    def task(self):
        return {"device_id": self.desktop.config.device_id,
                "task_id": "attend-task", "session_id": "attend-session"}

    def prompt(self, text):
        # 非交互运行视为“没人在机旁”，不能当成做过。
        try:
            self.ask(text)
        except EOFError:
            raise StepFailed("非交互运行：没有人在机旁，步骤未执行")

    async def request(self, scopes=("click",)):
        return await self.desktop.request_control(
            {"window_id": self.window["hwnd"], "scopes": list(scopes),
             "reason": "Linux 本机桌面验收"}, self.task)

    async def act_click(self):
        left, top, right, bottom = self.window["rect"]
        # `Desktop.act` 是同步方法（只做一次 X11 注入，不经过网络）；写成 await 会在真机上
        # 直接 TypeError，而替身如果写成 async 就把这个错误掩掉了。
        return self.desktop.act({"action": "click",
                                 "x": (left + right) // 2,
                                 "y": (top + bottom) // 2}, self.task)

    def wait(self, predicate, seconds=None):
        deadline = time.monotonic() + (self.poll_seconds if seconds is None else float(seconds))
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def log_lines(self):
        if not self.log_path:
            return []
        try:
            with open(self.log_path, encoding="utf-8") as stream:
                return stream.read().splitlines()
        except FileNotFoundError:
            return []

    def wait_log(self, prefix, since=0):
        """只认本次动作之后新增的日志行，避免把上一轮留下的记录当成这次的结果。"""
        found = {}

        def seen():
            for line in self.log_lines()[since:]:
                if line.startswith(prefix):
                    found["line"] = line
                    return True
            return False

        if not self.wait(seen):
            raise StepFailed(f"目标应用没有收到 {prefix!r}：注入没有到达应用")
        return found["line"]

    async def run(self, step):
        action, criteria = {step_id: (action, criteria) for step_id, action, criteria in STEPS}[step]
        started = time.monotonic()
        try:
            self.prompt(f"{DIALOG_INTRO[step]}（判定：{criteria}）" if step in DIALOG_INTRO
                        else f"{action}（判定：{criteria}）完成后按回车：")
            evidence = await getattr(self, "step_" + step)()
            return {"id": step, "status": "pass", "detail": evidence.pop("detail", ""),
                    "evidence": evidence, "seconds": round(time.monotonic() - started, 1)}
        except StepFailed as error:
            return {"id": step, "status": "fail", "detail": str(error), "evidence": {},
                    "seconds": round(time.monotonic() - started, 1)}
        except Exception as error:  # 意外异常同样是未通过，不能当成功
            return {"id": step, "status": "fail",
                    "detail": f"{type(error).__name__}: {error}", "evidence": {},
                    "seconds": round(time.monotonic() - started, 1)}
        finally:
            await self.settle()

    async def settle(self):
        """每一步都自己收尾。

        `Desktop.request_control` 在已有租约时直接拒绝（`desktop_lease_active`）；某步中途
        失败却把租约留着，下一步就只会看到这个拒绝，真实原因被盖掉，人会以为后面几步也坏了。
        """
        if self.desktop.lease is None:
            return
        try:
            await self.desktop.release()
        except Exception:
            pass

    async def step_approve(self):
        from companion.desktop import LEASE_SECONDS

        before = len(self.log_lines())
        result = await self.request(("click",))
        lease = (result or {}).get("lease") or {}
        if not result.get("approved") or not lease:
            raise StepFailed("点了允许却没有建立租约")
        if lease.get("expires_in", 0) > LEASE_SECONDS:
            raise StepFailed(f"租约超过 {LEASE_SECONDS} 秒：{lease.get('expires_in')}")
        action = await self.act_click()
        line = self.wait_log("CLICK", since=before)
        await self.desktop.release()
        # 批准后目标窗口的日志必须多出一条新点击，才算“注入到达应用”。
        return {"detail": f"租约 {lease.get('expires_in')} 秒内，点击到达应用",
                "approval_id": lease.get("approval_id"), "expires_in": lease.get("expires_in"),
                "action": action.get("action"), "click_log": line,
                "log_lines_before": before, "log_lines_after": len(self.log_lines())}

    async def step_reject(self):
        from companion.security import Paused

        try:
            await self.request()
        except Paused as error:
            reason = str(error)
            if reason != "desktop_approval_declined":
                raise StepFailed(f"拒绝后被报成 {reason}，应与用户拒绝一致")
            if self.desktop.lease is not None:
                raise StepFailed("拒绝后仍有租约")
            return {"detail": "拒绝即结束，无租约", "pause_reason": reason,
                    "suspended": self.desktop.suspended}
        raise StepFailed("点了拒绝却建立了租约")

    async def step_timeout(self):
        from companion.desktop import APPROVAL_SECONDS, APPROVAL_TIMEOUT
        from companion.security import Paused

        started = time.monotonic()
        try:
            await self.request()
        except Paused as error:
            elapsed = time.monotonic() - started
            reason = str(error)
            if reason != APPROVAL_TIMEOUT:
                raise StepFailed(f"到期被报成 {reason}，应与用户拒绝区分")
            if elapsed < APPROVAL_SECONDS - 5:
                raise StepFailed(f"只用 {elapsed:.0f}s 就结束，早于 {APPROVAL_SECONDS}s 上限")
            if self.desktop.lease is not None:
                raise StepFailed("到期自动拒绝后仍有租约")
            return {"detail": f"{elapsed:.0f}s 无人操作后自动拒绝",
                    "pause_reason": reason, "elapsed_s": round(elapsed, 1),
                    "limit_s": APPROVAL_SECONDS}
        raise StepFailed("对话框无人操作却建立了租约（绝不能默认同意）")

    async def step_typing_interference(self):
        from companion.security import Paused

        await self.request()
        lease = self.desktop.lease
        if lease is None:
            raise StepFailed("没有租约，无法验证用户输入干扰")
        changed = self.wait(lambda: self.desktop.driver.external_input_since(lease.baseline))
        if not changed:
            raise StepFailed(f"{self.poll_seconds}s 内没有检测到用户真实输入")
        before = self.desktop.driver.foreground()
        try:
            await self.act_click()
        except Paused as error:
            reason, suspended = str(error), self.desktop.suspended
            after = self.desktop.driver.foreground()
            if suspended != "user_input":
                raise StepFailed(f"暂停原因不是用户输入：{suspended}")
            if before != after:
                raise StepFailed("接管暂停时抢走了焦点")
            return {"detail": "用户输入后动作被拒绝，焦点未被抢回", "pause_reason": reason,
                    "suspended": suspended, "foreground_unchanged": True}
        raise StepFailed("用户真实输入后动作仍然执行")

    async def step_lock_screen(self):
        from companion.security import Paused

        await self.request()
        if not self.wait(self.desktop.driver.secure):
            raise StepFailed(f"{self.poll_seconds}s 内没有观察到锁屏/安全桌面状态")
        try:
            await self.act_click()
        except Paused as error:
            reason, suspended = str(error), self.desktop.suspended
            self.prompt("请解锁并回到桌面")
            await self.desktop.release()
            if suspended != "secure_desktop":
                raise StepFailed(f"锁屏暂停原因不对：{suspended}")
            return {"detail": "锁屏期间动作被拒绝", "pause_reason": reason,
                    "suspended": suspended}
        raise StepFailed("锁屏时动作仍然执行")

    async def step_revoke(self):
        from companion.security import Paused

        await self.request()
        if not self.wait(lambda: self.desktop.revoked):
            raise StepFailed(f"{self.poll_seconds}s 内没有观察到热键触发的紧急停止")
        if self.desktop.lease is not None:
            raise StepFailed("紧急停止后租约仍然存在")
        try:
            await self.act_click()
        except Paused as error:
            return {"detail": "紧急停止后动作被拒绝，再次接管需重新批准",
                    "pause_reason": str(error), "suspended": self.desktop.suspended,
                    "revoked": True}
        raise StepFailed("紧急停止后动作仍然执行")


def _read_ready(log_path):
    try:
        with open(log_path, encoding="utf-8") as stream:
            for line in stream:
                found = re.match(r"READY pid=(\d+) xid=(\d+)", line)
                if found:
                    return int(found.group(1)), int(found.group(2))
    except FileNotFoundError:
        return None
    return None


def start_target(seconds=1800):
    """真实 GUI 目标窗口；验收只看它自己的事件日志，不看屏幕。"""
    tmp = tempfile.mkdtemp(prefix="zops-attended-")
    log_path = os.path.join(tmp, "target.log")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "x11_target_window.py"),
         "--log", log_path, "--seconds", str(seconds)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ready = _read_ready(log_path)
        if ready:
            return process, ready[1], log_path
        if process.poll() is not None:
            break
        time.sleep(0.1)
    process.terminate()
    raise SystemExit("target_window_not_ready：验收目标窗口没有启动，检查 DISPLAY 与 tkinter")


def _window_process(window_id):
    import psutil

    from companion.x11 import X11

    backend = X11()
    try:
        info = backend.info(int(window_id))
    finally:
        backend.close()
    if info["pid"] <= 0:
        raise SystemExit("window_pid_unavailable：目标窗口没有 _NET_WM_PID，无法按进程授权")
    app = psutil.Process(info["pid"]).name().lower()[:64]
    if not re.fullmatch(r"[a-z0-9._-]{1,64}", app):
        # 配置只接受进程名, 带空格/路径的名字无法表达授权; 不在这里放宽规则。
        raise SystemExit("desktop_app_name_not_supported：" + app)
    return app


def markdown(report):
    lines = ["| 步骤 | 结果 | 说明 |", "| --- | --- | --- |"]
    for item in report["steps"]:
        detail = str(item.get("detail") or "").replace("|", "/").replace("\n", " ")[:160]
        lines.append(f"| {item['id']} | {item['status']} | {detail} |")
    lines.append("")
    lines.append("结论：" + ("人工步骤全部通过" if report["accepted"]
                            else "未通过（" + "、".join(item["id"] for item in report["steps"]
                                                      if item["status"] != "pass") + "）"))
    return "\n".join(lines)


async def run(args):
    from companion.config import Config
    from companion.desktop import Desktop, load_acceptance

    cfg = Config.load(Path(args.config).absolute())
    acceptance = load_acceptance(cfg)
    target = None
    window_id = args.window_id
    log_path = args.target_log
    try:
        if not window_id:
            target, window_id, log_path = start_target()
        app = _window_process(window_id)
        # 只把验收目标进程加进本次运行的应用白名单：不改用户配置。
        scoped = Config(cfg.server, cfg.name, device_id=cfg.device_id,
                        desktop_apps=[app], path=cfg.path).validate()
        desktop = Desktop(scoped, acceptance=acceptance, platform="linux")
        await desktop.prepare()
        if not desktop.enabled:
            print("桌面不可用：" + (desktop.reason or "unknown")
                  + "；先运行 companion --accept-desktop 写入本机验收记录。", file=sys.stderr)
            return 2
        window = desktop.driver.window(window_id)
        attended = Attended(desktop, window, log_path=log_path)
        order = args.step or list(DEFAULT_ORDER)
        steps = []
        try:
            for step in order:
                item = await attended.run(step)
                steps.append(item)
                print(f"[{step}] {item['status']}：{item['detail']}")
                if item["status"] != "pass" and args.stop_on_fail:
                    break
        finally:
            await desktop.close()
        report = {"schema": 1, "platform": "linux", "started": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session": os.environ.get("XDG_SESSION_TYPE", ""),
            "display": os.environ.get("DISPLAY", ""), "target": app, "steps": steps,
            "accepted": all(item["status"] == "pass" for item in steps) and bool(steps)}
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n" + markdown(report))
        print("\n已写入：" + str(path))
        return 0 if report["accepted"] else 1
    finally:
        if target is not None:
            target.terminate()
            try:
                target.wait(5)
            except Exception:
                target.kill()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Linux X11 桌面人工验收步骤（真实对话框/租约/热键/锁屏/输入干扰）")
    parser.add_argument("--config", required=True, help="已配对并写入桌面验收记录的 Companion 配置")
    parser.add_argument("--step", action="append", choices=STEP_IDS,
                        help="只跑指定步骤；可重复。默认按 approve→…→revoke 顺序全跑")
    parser.add_argument("--window-id", type=int, help="已授权的现有窗口；默认启动内置验收窗口")
    parser.add_argument("--target-log", help="配合 --window-id 时目标应用的事件日志（用于核对注入）")
    parser.add_argument("--output", default=str(Path.home() / "zops-linux-attended.json"))
    parser.add_argument("--stop-on-fail", action="store_true", help="某步失败后不再继续")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
