"""能力只包含本机授权且后端自检可用的方法。"""
import os
from pathlib import Path
from .browser import Browser, METHODS
from .desktop import Desktop, UNAVAILABLE
from .processes import OwnedProcesses, local_worker
from .scripts import arguments, validate_entry
from .security import Denied, require

# 执行方式由实际后端声明; 服务端只接受白名单值。
BACKGROUND_PREFIXES = ("browser.", "file.", "document.", "system.",
                       "process.", "service.", "log.", "script.")


def execution_mode(method):
    if isinstance(method, str) and method.startswith("desktop."):
        return "desktop"
    if isinstance(method, str) and method.startswith(BACKGROUND_PREFIXES):
        return "background"
    return ""


class Executor:
    def __init__(self, config, browser_only=False):
        self.config = config
        self.browser_only = browser_only
        self.browser = Browser(config)
        self.desktop = Desktop(config)
        self.processes = OwnedProcesses()
        self.capabilities = []
        self.unavailable = dict(UNAVAILABLE)

    async def prepare(self):
        caps = []
        if not self.browser_only:
            await self.desktop.prepare()
            self.unavailable.update(self.desktop.unavailable)
            caps += self.desktop.capabilities
            # Windows 与 POSIX 都开放文件/信息能力; 桌面与脚本封印单独判定。
            caps += ["system.info", "process.list", "service.list"]
            if any(root["read"] for root in self.config.roots):
                caps += ["file.list", "file.find", "file.read", "document.extract", "log.collect"]
            if any(root["write_new"] for root in self.config.roots):
                caps += ["file.write_new", "document.write_new"]
            if any(root.get("manage") for root in self.config.roots):
                # 破坏性操作只在用户显式 manage 授权根内声明; 服务端仍逐次审批。
                caps += ["file.move", "file.delete", "file.replace"]
            if self.config.scripts and (os.name == "nt" or (
                    os.name == "posix" and Path("/proc/self/fd").exists())):
                caps += ["script.run"]
            elif self.config.scripts:
                self.unavailable["script.run"] = "script_sealing_unavailable"
        if self.config.origins:
            try:
                await self.browser.prepare()
                # 下载需要本机写授权根; 未授权就不声明该能力。
                writable = any(root["write_new"] for root in self.config.roots)
                caps += [m for m in METHODS if m != "browser.download" or writable]
            except Exception:
                # root 下 Chromium 沙箱必然失败, 真正原因是运行身份; 不要报成“沙箱不可用”。
                self.unavailable["browser"] = (
                    "run_as_normal_desktop_user_not_root"
                    if os.name == "posix" and os.getuid() == 0
                    else "sandboxed_chromium_unavailable")
        else:
            self.unavailable["browser"] = "no_local_origin_grant"
        self.capabilities = sorted(caps)
        # 同一个能力不能既声明可用又给出不可用原因: doctor 是远端唯一的判断依据。
        self.unavailable = {name: reason for name, reason in self.unavailable.items()
                            if name not in self.capabilities}

    async def execute(self, command):
        method, params = command["method"], command["params"]
        require(method in self.capabilities, "capability_unavailable")
        if method.startswith("browser."):
            return await self.browser.execute(method, params, (command["session_id"], command["task_id"]))
        if method.startswith("desktop."):
            return await self.desktop.execute(method, params, command)
        if method == "script.run":
            from .security import fields, identifier
            fields(params, ("name", "args"))
            name = identifier(params["name"])
            require(name in self.config.scripts, "script_not_registered")
            entry = validate_entry(self.config.scripts[name])
            arguments(entry["schema"], params["args"])
            if os.name == "nt":
                # Windows 子进程由 Job Object 约束; 不经过再一层 worker, 避免嵌套 Job。
                from .scripts import run_script
                return await run_script(self.processes, self.config, params)
        return await local_worker(self.processes, self.config, method, params)

    async def stop(self):
        await self.processes.stop()
        await self.browser.stop_task()
        await self.desktop.release()

    async def close(self):
        await self.stop()
        await self.browser.close()
        await self.desktop.close()
