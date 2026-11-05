"""本机 CLI；所有永久授权仅由交互式本地操作写入。"""
import argparse
import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from . import __version__
from .config import Config, default_path
from .security import Denied, decode, fields, identifier, no_links, origin, private_dir, require


def confirm(message):
    require(sys.stdin.isatty() and sys.stdout.isatty(), "local_interactive_confirmation_required")
    print(message)
    require(input("仅本机授权；输入 ALLOW 确认，其他输入拒绝：") == "ALLOW", "local_user_declined")


def authorize(cfg, args):
    if args.revoke_all:
        confirm("撤销全部目录、站点、应用、脚本及扩展权限。")
        cfg.roots, cfg.origins, cfg.scripts, cfg.native_origins = [], [], {}, []
        cfg.desktop_apps = []
    for permission, paths in (("read", args.grant_read), ("write_new", args.grant_write_new),
                             ("manage", args.grant_manage)):
        for value in paths:
            path = str(no_links(Path(value).absolute()))
            require(Path(path).is_dir(), "root_not_directory")
            effect = {"manage": "包含子目录；仅该目录内允许覆盖、移动、删除，且每次仍需逐项确认",
                      "write_new": "包含子目录；只新建文件，不允许链接、覆盖、删除",
                      "read": "包含子目录；只读，不允许链接、覆盖、删除"}[permission]
            confirm(f"授权目录：{path}\n权限：{permission}；{effect}。")
            entry = next((x for x in cfg.roots if x["path"] == path), None)
            if entry is None:
                entry = {"path": path, "read": False, "write_new": False, "manage": False}
                cfg.roots.append(entry)
            entry.setdefault("manage", False)
            entry[permission] = True
    for value in args.grant_origin:
        site = origin(value)
        confirm(f"授权精确站点 origin：{site}\n不授权其他端口、协议或重定向站点。")
        if site not in cfg.origins:
            cfg.origins.append(site)
    for value in args.grant_extension:
        require(len(value) == 32 and all(c in "abcdefghijklmnop" for c in value), "invalid_extension_id")
        site = "chrome-extension://" + value + "/"
        confirm(f"授权 Native Messaging 扩展：{site}\n仅独立无头浏览器；不连接现有用户标签页。")
        if site not in cfg.native_origins:
            cfg.native_origins.append(site)
    for value in args.allow_desktop_app:
        app = str(value or "").strip().lower()
        require(re.fullmatch(r"[a-z0-9._-]{1,64}", app) is not None, "invalid_desktop_app")
        require(len(cfg.desktop_apps) < 16, "too_many_desktop_apps")
        confirm(f"授权桌面应用进程：{app}\n仅该进程的窗口在你本机逐次批准接管后可操作；"
                "未列出的应用（含安全桌面）一律不可操作。")
        if app not in cfg.desktop_apps:
            cfg.desktop_apps.append(app)
    for value in args.register_script:
        from .scripts import validate_entry, verified_bytes, verified_fd
        with no_links(Path(value).absolute()).open("rb") as stream:
            registration = decode(stream.read(262145))
        fields(registration, ("name", "entry"))
        name = identifier(registration["name"])
        entry = validate_entry(registration["entry"])
        # 目标目录元信息只能落在已授权的可写根内, 不得借登记扩张写权限。
        writable = [Path(root["path"]) for root in cfg.roots
                    if root.get("write_new") or root.get("manage")]
        for value in entry.get("target_dirs", []):
            target = Path(value).absolute()
            require(any(target == root or root in target.parents for root in writable),
                    "script_target_not_authorized")
        for p, h, limit in (("path", "sha256", 1024**2), ("executable", "executable_sha256", 64 * 1024**2)):
            if os.name == "nt":
                verified_bytes(entry[p], entry[h], limit)
            else:
                os.close(verified_fd(entry[p], entry[h], limit))
        confirm("登记可信本机 Python 脚本（不是安全沙箱，具有当前用户权限）：\n"
                + json.dumps(registration, ensure_ascii=False, indent=2))
        cfg.scripts[name] = entry
    cfg.save()


def accept_desktop(cfg):
    """本机验收：全部预检通过 + 实机可读 + 原生确认后才写本机记录。

    Windows 走 UIA，Linux 走 X11，macOS 走 AX；三个平台的验收项不同，记录不能互相冒充。
    """
    require(bool(cfg.device_id), "pairing_required")
    from .desktop import (MacDriver, X11Driver, WindowsDriver, acceptance_platform,
                          write_acceptance)

    name, expected = acceptance_platform()
    drivers = {"windows": WindowsDriver, "linux": X11Driver, "macos": MacDriver}
    driver = drivers[name]()
    report = {}
    try:
        checks = driver.prepare()
        if name == "windows":
            # UIA 不能只靠 import：必须对已打开的标准/登记应用读到真实控件树。
            report = driver.uia_app_report(cfg.desktop_apps)
            require(bool(report), "uia_app_not_open")
            require(all(item["readable"] for item in report.values()), "uia_app_unreadable")
            require(sum(item["elements"] for item in report.values()) > 0, "uia_app_unreadable")
            summary = "\n".join(f"- {app}: {item['elements']} 个可激活控件"
                                for app, item in sorted(report.items()))
            body = ("本机桌面能力预检已完成：UIA、原生对话框、输入探测、安全桌面查询、"
                    "全局热键和托盘均已就绪。\n\nUIA 实测（只读取控件数量，不读取内容）：\n"
                    + summary +
                    "\n\n选择“是”只写入本机验收记录；"
                    "不会开放远程控制，每次接管仍需你在本机原生批准。")
        elif name == "macos":
            # 权限位为真不等于能拿到像素：采集与控件树都必须实测。
            require(bool(driver.capture_probe()), "macos_capture_unavailable")
            report = driver.ax_app_report(cfg.desktop_apps)
            require(bool(report), "macos_no_target_window")
            require(all(item["readable"] for item in report.values()), "macos_ax_unreadable")
            summary = "\n".join(f"- {app}: {item['elements']} 个可激活控件"
                                for app, item in sorted(report.items()))
            body = ("本机桌面能力预检已完成：辅助功能授权、窗口枚举、焦点查询、锁屏查询、"
                    "输入探测、全局热键、原生对话框与窗口采集均已就绪。\n\n"
                    "AX 实测（只读取控件数量，不读取内容）：\n" + summary +
                    "\n\nmacOS 上没有 Pause/Break 键，停止热键是 Ctrl+Option+Cmd+S。\n\n"
                    "选“写入验收记录”只记录本机验收结果；"
                    "不会开放远程控制，每次接管仍需你在本机原生批准。")
        else:
            body = ("本机桌面能力预检已完成：X11 窗口枚举、焦点查询、屏保/锁屏查询、"
                    "XTest 注入、全局热键和原生对话框均已就绪。\n\n"
                    "Linux 不提供控件树（AT-SPI 未适配）也不提供窗口截图"
                    "（X11 上无法识别密码框），这两项不会出现在能力表里。\n\n"
                    "选“写入验收记录”只记录本机验收结果；"
                    "不会开放远程控制，每次接管仍需你在本机原生批准。")
        accepted = driver.confirm_acceptance(body)
        # 「没人回应」和「有人明确拒绝」要分开报：否则运维会把到期当成有人点了不写入。
        require(accepted, "local_user_timeout"
                if getattr(driver, "last_approval", "") == "timeout" else "local_user_declined")
    finally:
        driver.close()
    checks["native_dialog"] = True
    require(set(checks) == set(expected) and all(v is True for v in checks.values()),
            "acceptance_incomplete")
    confirm("写入本机桌面验收记录。\n"
            "记录绑定当前设备与 Companion 版本；升级后需要重新验收。")
    write_acceptance(cfg, checks, report or None)
    print("本机桌面验收记录已写入；远程仍需每次原生批准和 60 秒租约。")


async def launch(cfg, args):
    from .client import enroll, run
    from .executor import Executor
    from .protocol import Journal
    if args.pair:
        await enroll(cfg, args.pair)
        print("配对成功。默认没有目录、站点或脚本权限。")
    if args.init:
        return
    executor = Executor(cfg)
    try:
        await executor.prepare()
        if args.doctor:
            scripts = [{"name": name,
                        "platform": entry.get("platform"),
                        "permissions": entry.get("permissions"),
                        "version": entry.get("version"),
                        "timeout": entry.get("timeout"),
                        "sha256": str(entry.get("sha256") or "")[:12]}
                       for name, entry in sorted(cfg.scripts.items())[:32] if isinstance(entry, dict)]
            print(json.dumps({"version": __version__, "capabilities": executor.capabilities,
                "unavailable": executor.unavailable, "build": "unsigned_test_only",
                "enrolled": bool(cfg.device_id), "desktop_enabled": executor.desktop.enabled,
                "scripts": scripts,
                "desktop": executor.desktop.status()}, ensure_ascii=False, indent=2))
            return
        require(bool(cfg.device_id), "pairing_required")
        require(os.name != "posix" or os.getuid() != 0, "run_as_normal_desktop_user_not_root")
        journal = Journal(cfg.state / "commands.sqlite")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            if os.name == "posix":
                loop.add_signal_handler(sig, stop.set)
        try:
            await run(cfg, journal, executor, stop)
        finally:
            journal.close()
    finally:
        await executor.close()


def main():
    parser = argparse.ArgumentParser(description="Companion 用户态 WSS 客户端；未签名测试版，非生产安装包")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=default_path(), help="本机私有 JSON 配置路径")
    parser.add_argument("--server", help="HTTPS 服务 origin，不带 API 路径")
    parser.add_argument("--pair", help="一次性配对码；不写入配置")
    parser.add_argument("--name", help="设备显示名")
    parser.add_argument("--init", action="store_true", help="仅初始化无权限配置/配对后退出")
    parser.add_argument("--doctor", action="store_true", help="显示实际能力和不可用原因后退出")
    parser.add_argument("--grant-read", action="append", default=[], metavar="PATH")
    parser.add_argument("--grant-write-new", action="append", default=[], metavar="PATH")
    parser.add_argument("--grant-manage", action="append", default=[], metavar="PATH",
                        help="允许覆盖、移动、删除；仍需用户逐次确认")
    parser.add_argument("--grant-origin", action="append", default=[], metavar="ORIGIN")
    parser.add_argument("--grant-extension", action="append", default=[], metavar="EXTENSION_ID")
    parser.add_argument("--allow-desktop-app", action="append", default=[], metavar="PROCESS",
                        help="授权可接管的桌面应用进程名（如 notepad.exe）；未列入不可操作")
    parser.add_argument("--register-script", action="append", default=[], metavar="LOCAL_JSON")
    parser.add_argument("--revoke-all", action="store_true")
    parser.add_argument("--local-windows-test", "--local-desktop-test", action="store_true",
                        dest="local_desktop_test",
                        help="仅本机原生确认/热键探针；不开放桌面")
    parser.add_argument("--accept-desktop", action="store_true",
                        help="本机验收（Windows UIA / macOS AX / Linux X11）并写入绑定设备的验收记录")
    parser.add_argument("--native-manifest", metavar="HOST_EXECUTABLE", help="输出 Native Messaging 注册 JSON，不自动注册")
    parser.add_argument("--eis-selfcheck", action="store_true",
                        help="离线跑一遍 EI(libei) 协议自检（假合成器，不连 D-Bus、不接管窗口、不开放桌面能力）")
    parser.add_argument("--windows-acceptance", action="store_true",
                        help="在 Windows 实机上跑 13 项验收清单并写出报告（随包自带，不需要另装 Python）")
    parser.add_argument("--acceptance-output", metavar="PATH",
                        help="--windows-acceptance 的报告路径，默认 ~/zops-windows-acceptance.json")
    parser.add_argument("--internal-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        require(sys.version_info >= (3, 11), "python_311_required")
        if args.internal_worker:
            from .worker import main as worker_main
            worker_main()
            return 0
        if args.local_desktop_test:
            from .desktop import local_desktop_probe
            print(json.dumps(local_desktop_probe(), ensure_ascii=False))
            return 0
        if args.windows_acceptance:
            from .winaccept import run as windows_acceptance
            # 必须在 Windows 实机上跑；其他平台 winaccept.run 会直接拒绝，不产出任何报告。
            return windows_acceptance(output=args.acceptance_output)
        if args.eis_selfcheck:
            from .selfcheck import STEPS, run as eis_selfcheck
            result = eis_selfcheck()
            print(json.dumps(result, ensure_ascii=False))
            # 自检不通过就不能假装“通过”：步步跑齐且零失败才回 0，交付时可当预检用。
            return 0 if not result["failed"] and len(result["passed"]) == len(STEPS) else 1
        path = args.config.absolute()
        if path.exists():
            cfg = Config.load(path)
            require(not args.server or args.server.rstrip("/") == cfg.server, "server_change_requires_new_config")
            require(not args.name or args.name == cfg.name, "name_change_requires_new_config")
        else:
            require(bool(args.server and args.name), "server_and_name_required")
            if not path.parent.parent.exists():
                private_dir(path.parent.parent)
            private_dir(path.parent)
            cfg = Config(args.server, args.name, path=path)
            cfg.save()
        from .locking import instance_lock
        with instance_lock(cfg.state / "client.lock"):
            local_changes = (args.grant_read or args.grant_write_new or args.grant_manage
                             or args.grant_origin or args.register_script
                             or args.grant_extension or args.allow_desktop_app or args.revoke_all)
            if local_changes:
                authorize(cfg, args)
                print("本机授权已更新；未启动连接。")
                return 0
            if args.accept_desktop:
                accept_desktop(cfg)
                return 0
            if args.native_manifest:
                executable = str(no_links(Path(args.native_manifest).absolute()))
                require(bool(cfg.native_origins), "extension_grant_required")
                print(json.dumps({"name": "net.zops.companion", "description": "Companion 未签名测试 host",
                    "path": executable, "type": "stdio", "allowed_origins": cfg.native_origins}, indent=2))
                return 0
            asyncio.run(launch(cfg, args))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # 只回固定错误码或异常类型：--doctor 要能复核失败原因，又不能回显路径或凭据。
        code = str(error)
        if not (isinstance(error, Denied) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)):
            code = type(error).__name__
        print(f"Companion 已停止：{code}；请检查参数、私有配置、系统 keyring 和本机权限；不会降级授权。",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
