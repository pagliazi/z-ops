"""Linux X11 桌面验收清单执行器（Spec 第 76、82 条）。

与 `windows_acceptance.py` 逐项对齐，但把 UIA/安全桌面查询换成 X11 原生能力：
- auto 步骤由脚本实测（doctor 自洽、会话类型、窗口枚举与 _NET_WM_PID、屏保查询、
  XTest 与键盘映射、全局热键、控件树/采集必须被拒绝）；
- manual 步骤给出判定标准，由人按提示操作后回答，并必须留下证据（任务/会话 id 或现象），
  不填证据一律视为未做；
- 缺项、未做、未通过，整体结论都是“未通过”，不允许用“没报错”当验收。

Linux 不声明 desktop.screen（X11 识别不了密码框），因此没有窗口采集步骤；
托盘只是第二停止通道，热键才是硬门槛，故托盘项只核对“如实上报”。

manual 步骤可以由 `linux_desktop_attended.py` 观察真实状态后写入证据，再用 `--attended`
交给本清单；没有证据时仍然逐项问人，人不在机旁就只能是未通过。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 允许从任意工作目录运行：companion 包在仓库根下。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KINDS = ("auto", "measure", "manual")
DESKTOP_CAPABILITIES = ("desktop.inspect", "desktop.request_control", "desktop.act",
                        "desktop.screen")
# Linux 预检项必须逐项为真; native_dialog 只能由真实批准步骤证明, 不在此列。
LINUX_CHECKS = ("x11", "input_sentinel", "lock_query", "focus_query", "hotkey", "injection")

# (id, Spec 条目, 类型, 判定标准)
STEPS = (
    ("doctor", "34", "auto", "本机能力声明可复核：不声明 desktop.screen 且给出原因，其余 desktop.* 要么声明要么说明原因"),
    ("session", "76", "auto", "当前会话是 X11（Wayland/XWayland 一律拒绝）且 DISPLAY 可用"),
    ("x11_windows", "76", "auto", "能枚举真实顶层窗口，且至少一个窗口带 _NET_WM_PID 可核对进程授权"),
    ("lock_query", "76", "auto", "XScreenSaver 的空闲时间与屏保状态可查询"),
    ("injection", "76", "auto", "XTest 扩展可用且键盘映射可读（能算出注入用 keycode）"),
    ("hotkey", "53", "auto", "Ctrl+Alt+Pause 能在本机独占抓取成功"),
    ("no_elements", "76", "auto", "不声明控件树与窗口采集：对任意窗口调用都必须以固定错误码拒绝"),
    ("tray", "53", "auto", "托盘宿主是否存在都如实上报（不作为硬门槛；热键才是停止硬门槛）"),
    ("approve", "50、82", "manual", "本机原生对话框里点“允许”能成立 60 秒租约"),
    ("reject", "50、82", "manual", "点“拒绝”立刻结束，不产生租约"),
    ("timeout", "51、82", "manual", "对话框 30 秒无人操作自动关闭并拒绝（不默认同意）"),
    ("revoke", "52、82", "manual", "紧急停止（Ctrl+Alt+Pause）后租约作废，且再次接管要重新批准"),
    ("lock_screen", "52、82", "manual", "锁屏/屏保后输入与焦点立即暂停，解锁后可恢复"),
    ("typing_interference", "82", "manual", "用户实际打字并切窗时，接管立即暂停且不抢回焦点"),
)


def probe_doctor(command):
    """--doctor 的 JSON 必须自洽：能力要么声明，要么给出不可用原因。"""
    try:
        run = subprocess.run(command + ["--doctor"], capture_output=True, text=True, timeout=180)
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
              f"x11_windows={uia.get('windows', '-')} tray={uia.get('tray', '-')} "
              f"checks={sum(1 for value in checks.values() if value)}/{len(checks) or '-'} "
              f"caps={len(capabilities)}")
    if "desktop.screen" in capabilities:
        # 声明了做不到的能力比不声明更危险: 远端会以为可以看屏幕。
        return False, "X11 不应声明 desktop.screen（无法识别密码框）；" + detail
    contradictory = sorted(name for name in capabilities if name in unavailable)
    if contradictory:
        return False, "既声明可用又给出不可用原因：" + "、".join(contradictory) + "；" + detail
    if not unavailable.get("desktop.screen"):
        return False, "desktop.screen 既未声明也未说明原因；" + detail
    silent = [name for name in DESKTOP_CAPABILITIES[:3]
              if name not in capabilities and not unavailable.get(name)]
    if silent:
        return False, "既未声明也未说明原因：" + "、".join(silent) + "；" + detail
    if not desktop.get("enabled"):
        return False, "桌面不可用：" + str(desktop.get("reason") or "未声明") + "；" + detail
    not_ready = [name for name in LINUX_CHECKS if not checks.get(name)]
    if not_ready:
        return False, "X11 预检未通过：" + "、".join(not_ready) + "；" + detail
    return True, detail


def probe_session():
    from companion.desktop import wayland_session

    session = str(os.environ.get("XDG_SESSION_TYPE") or "").lower()
    wayland = str(os.environ.get("WAYLAND_DISPLAY") or "")
    # 只看 XDG_SESSION_TYPE 会漏掉只有 WAYLAND_DISPLAY 的会话（DISPLAY 指向 XWayland）。
    if wayland_session():
        return False, (f"会话是 Wayland（XDG_SESSION_TYPE={session or 'unset'} "
                       f"WAYLAND_DISPLAY={wayland or 'unset'}）：XTest 对原生 Wayland 客户端"
                       "无效，整段会话不可信")
    display = str(os.environ.get("DISPLAY") or "")
    if not display:
        return False, "DISPLAY 未设置：没有可操作的 X 会话"
    return True, f"session={session or 'unset'} DISPLAY={display} wayland_display=unset"


def _backend():
    from companion.x11 import X11
    return X11()


def probe_x11_windows():
    backend = _backend()
    try:
        windows = backend.windows(30)
        if not windows:
            return False, "没有可见顶层窗口：无法核对枚举与授权"
        with_pid = [item for item in windows if item["pid"] > 0]
        named = 0
        if with_pid:
            try:
                import psutil
            except Exception:
                psutil = None
            for item in with_pid:
                try:
                    psutil.Process(item["pid"]).name()
                    named += 1
                except Exception:
                    continue
        detail = f"windows={len(windows)} with_pid={len(with_pid)} named={named}"
        if not with_pid:
            return False, "可见窗口都没有 _NET_WM_PID：无法按进程授权；" + detail
        if not named:
            return False, "有 _NET_WM_PID 但读不到进程名（psutil 不可用或权限不足）；" + detail
        return True, detail
    finally:
        backend.close()


def probe_lock_query():
    backend = _backend()
    try:
        if not backend.xss:
            return False, "XScreenSaver 扩展不可用：无法判断用户是否在场"
        idle, state = backend.idle_ms(), backend.screensaver_state()
        if idle < 0 or state < 0:
            return False, "空闲时间/屏保状态查询失败"
        return True, f"idle_ms={idle} state={state} locked={backend.locked()}"
    finally:
        backend.close()


def probe_injection():
    backend = _backend()
    try:
        if not backend.xtest:
            return False, "XTest 扩展不可用：无法注入键鼠"
        code, shifted = backend.keycode_for("a")
        enter = backend.keycode(0xFF0D)
        detail = f"xtest=true key_a={code} shift={shifted} enter={enter}"
        return (bool(code) and bool(enter)), detail
    finally:
        backend.close()


def probe_hotkey():
    backend = _backend()
    try:
        name = backend.grab_hotkey()
        return bool(name), (name or "抓取失败：Ctrl+Alt+Pause 已被其它程序占用或无对应键码")
    finally:
        backend.close()


def probe_no_elements():
    from companion.desktop import X11Driver
    from companion.security import Denied

    driver = X11Driver()
    if driver.list_elements(0, 80):
        return False, "X11 列出了控件：AT-SPI 未适配，不应声明控件树"
    expected = {"desktop_element_not_supported_on_platform": False,
                "desktop_capture_not_accepted_on_platform": False}
    for code, call in (("desktop_element_not_supported_on_platform",
                        lambda: driver.invoke_element(0, 0, "button", "保存")),
                       ("desktop_capture_not_accepted_on_platform",
                        lambda: driver.capture_window(0))):
        try:
            call()
            return False, "未拒绝：" + code
        except Denied as error:
            if str(error) != code:
                return False, f"错误码不符：期望 {code}，实际 {error}"
            expected[code] = True
    return all(expected.values()), "元素列表为空；invoke/capture 均以固定错误码拒绝"


def probe_tray():
    backend = _backend()
    try:
        host = backend.tray_available()
        return True, (f"tray_host={bool(host)}；"
                      + ("托盘可用，作为热键之外的第二停止通道" if host
                         else "无托盘宿主：只靠 Ctrl+Alt+Pause 停止，已如实上报"))
    finally:
        backend.close()


PROBES = {"doctor": probe_doctor, "session": probe_session,
          "x11_windows": probe_x11_windows, "lock_query": probe_lock_query,
          "injection": probe_injection, "hotkey": probe_hotkey,
          "no_elements": probe_no_elements, "tray": probe_tray}


MANUAL = tuple(step[0] for step in STEPS if step[2] == "manual")


def attended_steps(path):
    """读取人工步骤证据：只认 Linux、同一 DISPLAY、且只覆盖 manual 步骤。

    `linux_desktop_attended.py` 观察真实状态后写入这些结论；这里不做任何放宽：
    非 pass 一律照原样保留，未知步骤 id 直接拒绝，防止把 auto 步骤夹带成“已通过”。
    """
    try:
        with Path(path).open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"attended_unreadable：{type(exc).__name__}")
    if not isinstance(data, dict) or data.get("platform") != "linux":
        raise SystemExit("attended_platform_mismatch：需要 Linux 人工步骤记录")
    display = str(data.get("display") or "")
    current = str(os.environ.get("DISPLAY") or "")
    if display and current and display != current:
        raise SystemExit(f"attended_display_mismatch：记录为 {display}，当前为 {current}")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise SystemExit("attended_invalid：缺少 steps")
    out = {}
    for item in steps:
        if not isinstance(item, dict):
            raise SystemExit("attended_invalid：步骤格式错误")
        step, status = item.get("id"), item.get("status")
        if step not in MANUAL:
            raise SystemExit(f"attended_unknown_step：{step}")
        if status not in ("pass", "fail"):
            continue
        if step in out:
            raise SystemExit(f"attended_duplicated：{step}")
        out[step] = item
    return out


def manual_result(step, spec, attended):
    """有证据就用证据；没有证据返回 None，交给调用方继续问人。"""
    item = (attended or {}).get(step)
    if item is None:
        return None
    detail = str(item.get("detail") or item.get("evidence") or "")[:200]
    return {"id": step, "spec": spec, "kind": "manual", "status": item["status"],
            "detail": "attended: " + (detail or "-")}


def companion_command(value, config=None):
    """--companion 可以是可执行文件，也可以是 python 解释器（自动加 -m companion）。"""
    if value is None:
        python = ROOT / "companion" / ".venv" / "bin" / "python"
        if not python.is_file():
            raise SystemExit("companion_not_found：" + str(python))
        command = [str(python), "-m", "companion"]
    else:
        path = Path(value).absolute()
        if not path.is_file():
            raise SystemExit("companion_not_found：" + str(path))
        command = ([str(path), "-m", "companion"] if path.name in ("python", "python3")
                   else [str(path)])
    if config:
        command += ["--config", str(Path(config).absolute())]
    return command


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


def run(companion, output, config=None, attended=None):
    if not sys.platform.startswith("linux"):
        raise SystemExit("linux_only：本清单必须在 Linux X11 桌面会话中运行")
    recorded = attended_steps(attended) if attended else {}
    command = companion_command(companion, config)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results = []
    for step, spec, kind, criteria in STEPS:
        if kind == "manual":
            evidence = manual_result(step, spec, recorded)
            if evidence is not None:
                status, detail = evidence["status"], evidence["detail"]
            else:
                status, detail = ask(step, criteria)
        else:
            probe = PROBES[step]
            try:
                ok, detail = probe(command) if step == "doctor" else probe()
                status = "pass" if ok else "fail"
            except Exception as exc:
                status, detail = "fail", f"{type(exc).__name__}: {exc}"
        results.append({"id": step, "spec": spec, "kind": kind,
                        "status": status, "detail": detail})
    report = {"schema": 1, "started": started, "platform": "linux",
              "session": str(os.environ.get("XDG_SESSION_TYPE") or ""),
              "display": str(os.environ.get("DISPLAY") or ""),
              "attended": str(attended) if attended else "",
              "steps": results, "verdict": verdict(results)}
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + markdown(report))
    print("\n已写入：" + str(path))
    return 0 if report["verdict"]["accepted"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Linux X11 桌面验收清单（Spec 第 76、82 条）")
    parser.add_argument("--companion", help="companion 可执行文件或 python 解释器；默认用仓库内 venv")
    parser.add_argument("--config", help="Companion 私有配置路径（可选）")
    parser.add_argument("--output", default=str(Path.home() / "zops-linux-acceptance.json"))
    parser.add_argument("--attended",
                        help="linux_desktop_attended.py 的 JSON；人工步骤改用其中的实测结论")
    args = parser.parse_args(argv)
    return run(args.companion, args.output, args.config, args.attended)


if __name__ == "__main__":
    raise SystemExit(main())
