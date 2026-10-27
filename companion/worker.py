"""可被立即终止的文件/文档解析进程；不在网络循环解析不可信文档。"""
import os
import platform
import shutil
import subprocess
import sys
from .config import Config
from .files import Files
from .security import MAX_INPUT, Denied, decode, encode, fields, require

SERVICE_LIMIT = 300


def _service_env():
    names = ("PATH", "HOME", "XDG_RUNTIME_DIR", "USER", "LOGNAME", "LANG", "LC_ALL", "SYSTEMROOT")
    return {name: os.environ[name] for name in names if os.environ.get(name)}


def services():
    """只读列出当前用户可见的服务/单元, 不修改状态、不接受参数。"""
    if os.name == "nt":
        import psutil
        out = []
        for item in psutil.win_service_iter():
            try:
                info = item.as_dict()
            except psutil.Error:
                continue
            out.append({"name": str(info.get("name") or "")[:120],
                        "display": str(info.get("display_name") or "")[:200],
                        "state": str(info.get("status") or "")[:32]})
            if len(out) >= SERVICE_LIMIT:
                break
        return {"available": True, "services": out}
    if sys.platform == "darwin":
        tool = "/bin/launchctl"
        argv = [tool, "list"]
    else:
        tool = shutil.which("systemctl")
        argv = [tool, "--user", "list-units", "--type=service", "--all",
                "--no-pager", "--no-legend", "--plain"]
    if not tool or not os.access(tool, os.X_OK):
        return {"available": False, "services": [], "reason": "service_query_unavailable"}
    try:
        run = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=5, env=_service_env(), check=False)
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "services": [], "reason": "service_query_unavailable"}
    if run.returncode != 0:
        return {"available": False, "services": [], "reason": "service_query_unavailable"}
    out = []
    for line in run.stdout.splitlines():
        if sys.platform == "darwin":
            parts = line.split(None, 2)
            if len(parts) < 3 or parts[0].upper() == "PID":
                continue
            out.append({"name": parts[2].strip()[:120], "display": parts[2].strip()[:200],
                        "state": "running" if parts[0].isdigit() else str(parts[1])[:32]})
        else:
            parts = line.split(None, 4)
            if len(parts) < 4 or not parts[0].endswith(".service"):
                continue
            out.append({"name": parts[0][:120], "display": (parts[4] if len(parts) > 4 else "")[:200],
                        "state": (parts[2] + "/" + parts[3])[:32]})
        if len(out) >= SERVICE_LIMIT:
            break
    return {"available": True, "services": out}


def dispatch(config, method, params):
    if method == "script.run":
        import asyncio
        import ctypes
        from .scripts import run_script
        from .processes import OwnedProcesses
        # Linux subreaper 保持任务双重 fork/setsid 后代在本 worker 子树内。
        require(os.name == "posix" and ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0,
                "script_subreaper_unavailable")
        return asyncio.run(run_script(OwnedProcesses(), config, params))
    if method.startswith(("file.", "document.")) or method == "log.collect":
        return Files(config).dispatch(method, params)
    fields(params)
    import psutil
    if method == "system.info":
        return {"platform": platform.system(), "release": platform.release(),
                "cpu_count": psutil.cpu_count(), "memory_total": psutil.virtual_memory().total}
    if method == "process.list":
        entries = []
        for p in psutil.process_iter(["pid", "name", "username"]):
            try:
                if os.name == "posix" and p.uids().real != os.getuid():
                    continue
                entries.append({"pid": p.pid, "name": (p.info["name"] or "")[:100]})
                if len(entries) >= 300:
                    break
            except psutil.Error:
                continue
        return {"processes": entries}
    if method == "service.list":
        return services()
    raise ValueError("unsupported_method")


def main():
    method = ""
    try:
        if os.name == "posix":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
            resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
            resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2, 16 * 1024**2))
            resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        request = decode(sys.stdin.buffer.read(MAX_INPUT + 1))
        fields(request, ("config", "method", "params"))
        method = request["method"]
        data = dispatch(Config.load(request["config"]), method, request["params"])
        raw = encode({"status": "ok", "data": data})
    except (Denied, FileExistsError):
        raw = encode({"status": "rejected", "data": {}, "error": "local_operation_rejected"})
    except Exception:
        status = "unknown" if method in ("file.write_new", "document.write_new", "file.move",
                                         "file.delete", "file.replace", "script.run") else "rejected"
        raw = encode({"status": status, "data": {}, "error": "local_operation_failed"})
    finally:
        import psutil
        children = psutil.Process().children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(children, timeout=2)
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
