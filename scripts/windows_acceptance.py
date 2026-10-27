"""Windows 实机验收清单执行器（Spec 第 82 条）。

在本机（Windows 桌面用户）运行，逐项给出可核对的结论：
- auto 步骤由脚本实测（doctor 声明自洽、全局热键、后台任务对焦点/剪贴板的影响）；
- measure 步骤由人触发、由脚本测量（取消后子进程 pid 是否真的消失）；
- manual 步骤给出判定标准，由人按提示操作后回答，并必须留下证据（任务/会话 id 或现象），
  不填证据一律视为未做；
- 缺项、未做、未通过，整体结论都是“未通过”，不允许用“没报错”当验收。

依赖：playwright（后台浏览器探针），无需 psutil（pid 检查走 Win32 OpenProcess）。
结果写入 JSON 并打印可直接贴进验收记录的 Markdown 表。
"""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
VK_PAUSE = 0x13
HOTKEY_ID = 0x5A43
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CANCEL_TIMEOUT = 60
KINDS = ("auto", "measure", "manual")
# 桌面能力必须要么声明可用, 要么在 unavailable 里给出原因, 不能沉默。
DESKTOP_CAPABILITIES = ("desktop.inspect", "desktop.request_control",
                        "desktop.act", "desktop.screen")
# 启动预检必须逐项为真; native_dialog 只能由真实批准步骤证明, 不在此列。
PREFLIGHT_CHECKS = ("uia", "input_sentinel", "focus_query", "hotkey", "tray")
BACKGROUND_STEPS = ("background_focus", "background_clipboard")

# (id, Spec 条目, 类型, 判定标准)
STEPS = (
    ("doctor", "34", "auto", "本机能力声明可复核（desktop.*、预检逐项、UIA 实测与不可用原因）"),
    ("hotkey", "53", "auto", "Ctrl+Alt+Pause 能在本机注册成功"),
    ("background_focus", "82", "auto", "无头 Chromium 后台执行前后前台窗口不变"),
    ("background_clipboard", "82", "auto", "无头 Chromium 后台执行前后剪贴板序列号不变"),
    ("cancel_child", "82", "measure", "从服务端取消正在跑的登记脚本后，脚本 pid 已不存在（实测轮询，不靠自述）"),
    ("approve", "50、82", "manual", "本机原生对话框里点“允许”能成立 60 秒租约"),
    ("reject", "50、82", "manual", "点“拒绝”立刻结束，不产生租约"),
    ("timeout", "51、82", "manual", "对话框 30 秒无人操作自动关闭并拒绝（不默认同意）"),
    ("revoke", "52、82", "manual", "紧急停止后租约作废，且再次接管要重新批准"),
    ("lock_screen", "52、82", "manual", "锁屏后采集/输入立即暂停，解锁后可恢复"),
    ("typing_interference", "82", "manual", "用户实际打字并切窗时，后台任务不抢焦点、不改剪贴板"),
    ("tray", "53", "manual", "托盘图标可见，点“停止”能中断当前任务"),
    ("uia_apps", "34", "manual", "记事本/计算器/资源管理器/登记业务应用可读控件并可 invoke"),
)


def _windows():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
    return user32


def _last_error():
    import ctypes
    return ctypes.get_last_error()


def probe_hotkey():
    user32 = _windows()
    ok = bool(user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT, VK_PAUSE))
    if ok:
        user32.UnregisterHotKey(None, HOTKEY_ID)
    return ok, "Ctrl+Alt+Pause 注册成功" if ok else f"注册失败 winerror={_last_error()}"


def probe_background(expected_focus, expected_clip):
    """真实起一个无头浏览器任务，前后对比前台窗口与剪贴板序列号。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1024, "height": 720})
            page.goto("data:text/html,<h1>Z-OPS acceptance</h1>", wait_until="load")
            page.screenshot()
        finally:
            browser.close()
    user32 = _windows()
    focus, clip = user32.GetForegroundWindow(), user32.GetClipboardSequenceNumber()
    return {"focus_same": int(focus) == int(expected_focus), "clipboard_same": clip == expected_clip,
            "focus": int(focus), "clipboard": clip}


def probe_doctor(companion):
    """--doctor 的 JSON 必须自洽：桌面能力要么声明可用，要么给出不可用原因。"""
    try:
        run = subprocess.run([companion, "--doctor"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if run.returncode != 0:
        return False, (run.stderr or run.stdout or "").strip()[-300:]
    try:
        report = json.loads(run.stdout)
    except ValueError:
        return False, "doctor 输出不是 JSON"
    capabilities = set(report.get("capabilities") or ())
    unavailable = report.get("unavailable") or {}
    desktop = report.get("desktop") or {}
    checks = desktop.get("checks") or {}
    uia = desktop.get("uia") or {}
    detail = (f"desktop_enabled={desktop.get('enabled')} reason={desktop.get('reason') or '-'} "
              f"uia_windows={uia.get('windows', '-')} "
              f"checks={sum(1 for value in checks.values() if value)}/{len(checks) or '-'} "
              f"caps={len(capabilities)} unavailable={len(unavailable)}")
    silent = [name for name in DESKTOP_CAPABILITIES
              if name not in capabilities and not unavailable.get(name)]
    if silent:
        return False, "既未声明也未说明原因：" + "、".join(silent)
    if not desktop.get("enabled"):
        # 桌面验收必须在桌面能力真正可用的实例上做, 否则后续步骤无意义。
        return False, "桌面不可用：" + str(desktop.get("reason") or "未声明") + "；" + detail
    not_ready = [name for name in PREFLIGHT_CHECKS if not checks.get(name)]
    if not_ready:
        return False, "预检未通过：" + "、".join(not_ready) + "；" + detail
    return True, detail


def probe_auto(step, companion):
    if step == "doctor":
        return probe_doctor(companion)
    if step == "hotkey":
        return probe_hotkey()
    raise KeyError(step)


def background_step(step, probe, error):
    """把一次后台执行的前后对比结果拆成焦点/剪贴板两项结论。"""
    if probe is None:
        return "skip", error
    if step == "background_focus":
        return ("pass" if probe["focus_same"] else "fail"), f"foreground=0x{probe['focus']:x}"
    return (("pass" if probe["clipboard_same"] else "fail"),
            f"sequence={probe['clipboard']}")


def pid_exists(pid):
    """Win32 检查进程是否存在，不依赖 psutil：打开失败且不是权限问题即已消失。"""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_ACCESS_DENIED 说明进程还在，只是无权查询；其余（如 87）视为已退出。
        return ctypes.get_last_error() == 5
    kernel32.CloseHandle(handle)
    return True


def measure_cancel_child():
    """取消是否真的终止子进程，必须实测：先确认 pid 在，再由人在服务端取消，最后轮询到消失。"""
    try:
        raw = input("\n[cancel_child] 让登记脚本跑起来，输入该脚本的 pid：").strip()
    except EOFError:
        return "skip", "非交互运行，未确认"
    if not raw.isdigit():
        return "skip", "未提供 pid"
    pid = int(raw)
    if pid <= 4 or pid == os.getpid():
        return "skip", f"pid={pid} 不是任务子进程"
    if not pid_exists(pid):
        return "skip", f"pid={pid} 当前不存在，需在任务运行中核实"
    try:
        input("现在从服务端取消该任务，取消完成后回车继续…")
    except EOFError:
        return "skip", "非交互运行，未取消"
    deadline = time.monotonic() + CANCEL_TIMEOUT
    while time.monotonic() < deadline:
        if not pid_exists(pid):
            return "pass", f"pid={pid} 已不存在"
        time.sleep(0.5)
    return "fail", f"pid={pid} 在 {CANCEL_TIMEOUT}s 后仍存在"


def ask(step, criteria):
    """人工步骤：既要有结论也要有证据，缺一个都按未做处理。"""
    print(f"\n[{step}] {criteria}")
    try:
        answer = input("本机实测是否满足？(y=满足 / n=不满足 / 直接回车=未做) ").strip().lower()
    except EOFError:
        # 非交互运行不能把“没人回答”当成“已通过”。
        return "skip", "非交互运行，未确认"
    if answer not in ("y", "n"):
        return "skip", "未执行"
    try:
        evidence = input("证据（任务/会话 id 或具体现象；留空视为未做）：").strip()
    except EOFError:
        evidence = ""
    if not evidence:
        return "skip", "无证据"
    return ("pass" if answer == "y" else "fail"), evidence[:200]


def verdict(results, expected=None):
    """Spec 里每一项都 pass 才算通过；缺项、重复、skip、fail 都不算。"""
    expected = [step[0] for step in STEPS] if expected is None else list(expected)
    seen = {}
    for item in results:
        seen.setdefault(item["id"], []).append(item["status"])
    missing = [step for step in expected if step not in seen]
    duplicated = sorted(step for step, statuses in seen.items() if len(statuses) > 1)
    not_passed = [item["id"] for item in results if item["status"] != "pass"]
    problems = sorted(set(missing) | set(duplicated) | set(not_passed))
    return {"accepted": bool(expected) and not problems, "failed": problems,
            "missing": missing, "duplicated": duplicated,
            "passed": [step for step in expected if seen.get(step) == ["pass"]]}


def markdown(report):
    lines = ["| 项 | Spec | 方式 | 结果 | 证据 |", "| --- | --- | --- | --- | --- |"]
    for item in report["steps"]:
        detail = str(item.get("detail", "")).replace("|", "/").replace("\n", " ")[:160]
        lines.append(f"| {item['id']} | {item['spec']} | {item['kind']} | "
                     f"{item['status']} | {detail} |")
    lines.append("")
    lines.append("结论：" + ("验收通过" if report["verdict"]["accepted"]
                            else "未通过（" + "、".join(report["verdict"]["failed"]) + "）"))
    return "\n".join(lines)


def run(companion, output):
    if os.name != "nt":
        raise SystemExit("windows_only：本清单必须在 Windows 实机上运行")
    if not Path(companion).is_file():
        raise SystemExit("companion_not_found：" + str(companion))
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    user32 = _windows()
    focus, clip = user32.GetForegroundWindow(), user32.GetClipboardSequenceNumber()
    # 后台执行只做一次, 结果同时用于焦点与剪贴板两项断言。
    try:
        probe, probe_error = probe_background(focus, clip), ""
    except Exception as exc:
        probe, probe_error = None, f"{type(exc).__name__}: {exc}"
    results = []
    for step, spec, kind, criteria in STEPS:
        if kind == "manual":
            status, detail = ask(step, criteria)
        elif kind == "measure":
            try:
                status, detail = measure_cancel_child()
            except Exception as exc:
                status, detail = "fail", f"{type(exc).__name__}: {exc}"
        elif step in BACKGROUND_STEPS:
            status, detail = background_step(step, probe, probe_error)
        else:
            try:
                ok, detail = probe_auto(step, companion)
                status = "pass" if ok else "fail"
            except Exception as exc:
                status, detail = "fail", f"{type(exc).__name__}: {exc}"
        results.append({"id": step, "spec": spec, "kind": kind,
                        "status": status, "detail": detail})
    report = {"schema": 1, "started": started, "platform": "windows",
              "steps": results, "verdict": verdict(results)}
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + markdown(report))
    print("\n已写入：" + str(path))
    return 0 if report["verdict"]["accepted"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Windows 实机验收清单（Spec 第 82 条）")
    parser.add_argument("--companion", required=True, help="companion 可执行文件路径")
    parser.add_argument("--output", default=str(Path.home() / "zops-windows-acceptance.json"))
    args = parser.parse_args(argv)
    return run(args.companion, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
