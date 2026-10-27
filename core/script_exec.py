"""Z-OPS P0/P2 收尾 — 脚本登记注册表 + 受限执行 (SPEC P0「统一执行与审批」)

SPEC: 脚本必须显式登记且在支持的受限环境运行；隔离不可用则暂停。
替代 dev_agent 旧路径 (任意脚本内容 + root@host ssh 直跑)。

登记: scripts/registered/registry.json + 同目录脚本文件 (管理员审核后放置)
  [{"name":"backup_check","file":"backup_check.sh","type":"shell",
    "hosts":["localhost"],"timeout_s":120,"risk":"read","params_schema":["--gid"]}]
调用: script_exec(script_name, params, host) — 仅登记项; 未登记一律拒绝 (fail-closed)。

受限环境 (按可用性降级, 均不可用则暂停):
  1) Docker: ZOPS_SCRIPT_DOCKER_IMAGE 配置时 — --rm --memory 512m --cpus 1
     --pids-limit 128 --cap-drop ALL --read-only + tmpfs, 非 root 用户, 网络默认 bridge
  2) 本地沙箱: prlimit (AS 512MB / NPROC 64 / CPU 120s) + 干净 env + cwd 临时目录
     + 进程组超时击杀 + 输出截断
  3) 远程: 仅登记 hosts, SSHBackend (登记的 KeyFile/User, 不再裸 root)
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger("zops")

REGISTRY_DIR = Path(__file__).resolve().parent.parent / "scripts" / "registered"
REGISTRY_PATH = REGISTRY_DIR / "registry.json"
OUTPUT_CAP = 200 * 1024
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
PLATFORMS = frozenset({"linux", "windows", "darwin"})
PERMISSIONS = frozenset({"read", "write"})


class ScriptRegistryError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def current_platform() -> str:
    if os.name == "nt":
        return "windows"
    return "darwin" if sys.platform == "darwin" else "linux"


def validate_entry(entry: dict) -> dict:
    """登记项：平台/权限/目标目录/版本摘要/取消元信息；非法项一律拒绝。"""
    if not isinstance(entry, dict):
        raise ScriptRegistryError("invalid_entry", "登记项必须是对象")
    name = entry.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ScriptRegistryError("invalid_entry", "非法脚本名")
    file = entry.get("file")
    if (not isinstance(file, str) or not file or Path(file).name != file
            or ".." in Path(file).parts):
        raise ScriptRegistryError("invalid_entry", f"{name}: 脚本文件必须是登记目录内的文件名")
    if entry.get("type") not in ("shell", "python"):
        raise ScriptRegistryError("invalid_entry", f"{name}: 不支持的类型")
    hosts = entry.get("hosts", ["localhost"])
    if (not isinstance(hosts, list) or not 1 <= len(hosts) <= 16
            or any(not isinstance(h, str) or not h for h in hosts)):
        raise ScriptRegistryError("invalid_entry", f"{name}: hosts 非法")
    timeout = entry.get("timeout_s", 120)
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ScriptRegistryError("invalid_entry", f"{name}: timeout_s 非法")
    risk = entry.get("risk", "read")
    if risk not in PERMISSIONS:
        raise ScriptRegistryError("invalid_entry", f"{name}: risk 非法")
    schema = entry.get("params_schema", [])
    if (not isinstance(schema, list) or len(schema) > 32
            or any(not isinstance(p, str) or not re.fullmatch(r"--[A-Za-z0-9_-]{1,64}", p) for p in schema)):
        raise ScriptRegistryError("invalid_entry", f"{name}: params_schema 非法")
    platforms = entry.get("platform", ["linux"])
    if (not isinstance(platforms, list) or not platforms or len(platforms) > len(PLATFORMS)
            or set(platforms) - PLATFORMS):
        raise ScriptRegistryError("invalid_entry", f"{name}: platform 非法")
    permissions = entry.get("permissions", [risk])
    if (not isinstance(permissions, list) or not permissions
            or set(permissions) - PERMISSIONS):
        raise ScriptRegistryError("invalid_entry", f"{name}: permissions 非法")
    target_dirs = entry.get("target_dirs", [])
    if not isinstance(target_dirs, list) or len(target_dirs) > 16:
        raise ScriptRegistryError("invalid_entry", f"{name}: target_dirs 非法")
    for value in target_dirs:
        if not isinstance(value, str) or not os.path.isabs(value) or ".." in Path(value).parts:
            raise ScriptRegistryError("invalid_entry", f"{name}: target_dirs 必须是绝对路径")
    version = entry.get("version", 1)
    if not (type(version) is int or isinstance(version, str) and 0 < len(version) <= 64):
        raise ScriptRegistryError("invalid_entry", f"{name}: version 非法")
    digest = entry.get("sha256")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ScriptRegistryError("invalid_entry", f"{name}: sha256 非法")
    cancel = entry.get("cancel", {"grace_s": 5})
    if (not isinstance(cancel, dict) or set(cancel) - {"grace_s"}
            or type(cancel.get("grace_s", 5)) is not int
            or not 1 <= cancel.get("grace_s", 5) <= 30):
        raise ScriptRegistryError("invalid_entry", f"{name}: cancel 元信息非法")
    return {**entry, "hosts": hosts, "timeout_s": timeout, "risk": risk,
            "platform": platforms, "permissions": permissions, "target_dirs": target_dirs,
            "version": version, "cancel": {"grace_s": cancel.get("grace_s", 5)}}


def load_registry() -> list:
    try:
        if REGISTRY_PATH.exists():
            data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            result = []
            for item in data:
                try:
                    result.append(validate_entry(item))
                except ScriptRegistryError as e:
                    logger.error(f"[script_registry] 跳过非法登记项 (fail-closed): {e}")
            return result
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[script_registry] 注册表读取失败 (视为空, fail-closed): {e}")
    return []


def get_entry(name: str) -> Optional[dict]:
    for e in load_registry():
        if e.get("name") == name:
            return e
    return None


def _sanitize_param(v) -> Optional[str]:
    """参数只允许安全标量 (防注入: 拒绝 shell 元字符)"""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if any(ch in s for ch in ";|&$`><\n\r\\"):
            return None
        return s
    return None


def build_argv(entry: dict, params: dict) -> list:
    """params → argv (--key value 形式, 只接受登记过的参数名)"""
    allowed = set(entry.get("params_schema") or [])
    argv = []
    for k, v in (params or {}).items():
        key = str(k).lstrip("-").replace("-", "_")
        if allowed and key not in {a.lstrip("-").replace("-", "_") for a in allowed}:
            raise ScriptRegistryError("param_not_registered", f"参数 {k} 未登记")
        sv = _sanitize_param(v)
        if sv is None:
            raise ScriptRegistryError("param_invalid", f"参数 {k} 值非法 (含元字符或非标量)")
        argv += [f"--{key}", sv]
    return argv


def docker_image() -> str:
    return (getattr(config, "SCRIPT_DOCKER_IMAGE", "") or "").strip()


def _truncate(b: bytes) -> str:
    text = b.decode("utf-8", errors="replace")
    return text if len(text) <= OUTPUT_CAP else text[:OUTPUT_CAP] + "\n...[截断]"


async def _terminate_process_group(proc, grace: int):
    """先 TERM，宽限期后 KILL；只作用于本任务 setsid 的进程组。"""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), grace)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


async def _collect(comm, seconds=5):
    try:
        return await asyncio.wait_for(comm, seconds)
    except asyncio.TimeoutError:
        comm.cancel()
        await asyncio.gather(comm, return_exceptions=True)
        return b"", b""


async def _run_local_restricted(script_path: str, entry: dict, argv: list, cancel=None) -> dict:
    """本地沙箱: prlimit + 干净 env + 临时 cwd + 进程组超时/取消"""
    timeout = int(entry.get("timeout_s") or 120)
    if cancel is not None and cancel.is_set():
        return {"ok": False, "exit_code": -15, "timed_out": False, "cancelled": True,
                "env": "local-sandbox", "duration_s": 0, "output": "执行前已收到取消请求"}
    base = None
    if entry.get("target_dirs") and "write" in entry.get("permissions", []):
        base = entry["target_dirs"][0]
    workdir = tempfile.mkdtemp(prefix="zops_script_", dir=base)
    is_py = entry.get("type") == "python"
    interp = "python3" if is_py else "bash"
    cmd = [
        "prlimit", f"--as={512 * 1024 * 1024}", "--nproc=64", "--cpu=120",
        "--", interp, script_path, *argv,
    ]
    t0 = time.time()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workdir,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                 "HOME": workdir, "LC_ALL": "C.UTF-8", "ZOPS_SANDBOX": "1"},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid)
        comm = asyncio.create_task(proc.communicate())
        canceller = asyncio.create_task(cancel.wait()) if cancel is not None else None
        waiters = {comm} | ({canceller} if canceller is not None else set())
        done, _ = await asyncio.wait(waiters, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        timed_out = cancelled = False
        if comm in done:
            out, err = comm.result()
        elif canceller is not None and canceller in done:
            cancelled = True
            await _terminate_process_group(proc, int(entry["cancel"]["grace_s"]))
            out, err = await _collect(comm)
        else:
            timed_out = True
            await _terminate_process_group(proc, 1)
            out, err = await _collect(comm)
        if canceller is not None:
            canceller.cancel()
            await asyncio.gather(canceller, return_exceptions=True)
        return {
            "ok": (proc.returncode == 0) and not timed_out and not cancelled,
            "exit_code": proc.returncode if not timed_out else -9,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "env": "local-sandbox",
            "duration_s": round(time.time() - t0, 2),
            "output": (_truncate(out) + ("\n[STDERR] " + _truncate(err) if err else "")),
        }
    finally:
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(workdir, ignore_errors=True)


async def _run_docker(script_path: str, entry: dict, argv: list, cancel=None) -> dict:
    """Docker 隔离 (经 Hermes DockerBackend — 资源/能力/文件系统全限制, 非 root)

    复用 core/backends/docker_backend.py (Hermes 容器隔离执行):
    脚本以只读挂载进入受限容器, 限时执行, 输出截断。
    """
    from core.backends.docker_backend import DockerBackend
    be = DockerBackend({
        "image": docker_image(),
        "memory": "512m", "cpu_quota": 100000,
        "network": "none",             # 禁网 (SPEC: 最小权限)
        "timeout": int(entry.get("timeout_s") or 120),
        # docker -v 格式: 宿主路径:容器路径:权限
        "volumes": [f"{script_path}:/script:ro"],
        "auto_remove": True,
        # P0 安全加固
        "user": "65534:65534",
        "cap_drop": True,
        "read_only": True,
        "pids_limit": 128,
    })
    timeout = int(entry.get("timeout_s") or 120)
    is_py = entry.get("type") == "python"
    interp = "python3" if is_py else "sh"
    # argv 转义拼入命令 (DockerBackend.execute 接收单命令字符串)
    import shlex as _shlex
    cmd = f"{interp} /script {' '.join(_shlex.quote(a) for a in argv)}".strip()
    t0 = time.time()
    if cancel is not None and cancel.is_set():
        return {"ok": False, "exit_code": -15, "timed_out": False, "cancelled": True,
                "env": f"docker:{docker_image()}", "duration_s": 0, "output": "执行前已收到取消请求"}
    work = None
    canceller = None
    try:
        work = asyncio.create_task(be.execute(cmd))
        canceller = asyncio.create_task(cancel.wait()) if cancel is not None else None
        waiters = {work} | ({canceller} if canceller is not None else set())
        done, _ = await asyncio.wait(waiters, timeout=timeout + 15,
                                     return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            result = work.result()
            out = (getattr(result, "stdout", "") or "") + \
                  (("\n[STDERR] " + getattr(result, "stderr", "")) if getattr(result, "stderr", "") else "")
            ok = getattr(result, "success", False) and getattr(result, "return_code", 0) == 0
            return {
                "ok": ok,
                "exit_code": getattr(result, "return_code", 0),
                "timed_out": False,
                "cancelled": False,
                "env": f"docker:{docker_image()}",
                "duration_s": round(time.time() - t0, 2),
                "output": _truncate(out.encode("utf-8", "replace")),
            }
        if canceller is not None and canceller in done:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            cleaned = False
            try:
                await be.cleanup()
                cleaned = True
            except Exception:
                pass
            return {"ok": False, "exit_code": -15, "timed_out": False,
                    "cancelled": cleaned, "outcome": "cancelled" if cleaned else "unknown",
                    "env": f"docker:{docker_image()}", "duration_s": round(time.time() - t0, 2),
                    "output": "已请求取消; 容器清理" + ("完成" if cleaned else "失败, 需人工核查")}
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        return {"ok": False, "exit_code": -9, "timed_out": True, "cancelled": False,
                "env": f"docker:{docker_image()}", "duration_s": round(time.time() - t0, 2),
                "output": f"docker 执行超时 ({timeout}s)"}
    except Exception as e:
        return {"ok": False, "exit_code": -1, "timed_out": False,
                "env": f"docker:{docker_image()}", "duration_s": 0,
                "output": f"docker 执行异常: {e}"}
    finally:
        if canceller is not None:
            canceller.cancel()
            await asyncio.gather(canceller, return_exceptions=True)
        try:
            await be.cleanup()
        except Exception:
            pass


async def _run_remote(script_path: str, entry: dict, argv: list, host: str, cancel=None) -> dict:
    """远程执行: 仅登记 host; SSHBackend (登记 KeyFile/User, 非裸 root)"""
    from core.backends.ssh_backend import SSHBackend
    cfg = dict(entry.get("ssh") or {})
    cfg.setdefault("host", host)
    cfg.setdefault("user", "zopsrunner")            # 非 root 专用执行账号
    cfg.setdefault("key_file", "/opt/zops/.ssh/zopsrunner_key")
    cfg.setdefault("connect_timeout", 10)
    be = SSHBackend(cfg)
    timeout = int(entry.get("timeout_s") or 120)
    t0 = time.time()
    if cancel is not None and cancel.is_set():
        return {"ok": False, "exit_code": -15, "timed_out": False, "cancelled": True,
                "env": f"ssh:{host}", "duration_s": 0, "output": "执行前已收到取消请求"}
    work = None
    canceller = None
    try:
        work = asyncio.create_task(be.execute_script(script_path, timeout=timeout))
        canceller = asyncio.create_task(cancel.wait()) if cancel is not None else None
        waiters = {work} | ({canceller} if canceller is not None else set())
        done, _ = await asyncio.wait(waiters, timeout=timeout + 20,
                                     return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            out = work.result()
            return {"ok": True, "exit_code": 0, "timed_out": False, "cancelled": False,
                    "env": f"ssh:{host}", "duration_s": round(time.time() - t0, 2),
                    "output": _truncate(out.encode() if isinstance(out, str) else out)}
        if canceller is not None and canceller in done:
            # SSH 断开不能证明远端进程已停止: 明确标记结果未知, 不宣称已取消。
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            return {"ok": False, "exit_code": -15, "timed_out": False, "cancelled": False,
                    "cancel_requested": True, "outcome": "unknown", "env": f"ssh:{host}",
                    "duration_s": round(time.time() - t0, 2),
                    "output": "已请求取消并断开连接; 远端执行状态未知, 需人工核查"}
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        return {"ok": False, "exit_code": -9, "timed_out": True, "cancelled": False,
                "env": f"ssh:{host}", "duration_s": round(time.time() - t0, 2),
                "output": "远程执行超时 (已终止等待; 远端进程状态需人工核查)"}
    except asyncio.TimeoutError:
        return {"ok": False, "exit_code": -9, "timed_out": True, "cancelled": False,
                "env": f"ssh:{host}", "duration_s": round(time.time() - t0, 2),
                "output": "远程执行超时 (已终止等待; 远端进程状态需人工核查)"}
    except FileNotFoundError as e:
        # 密钥/工具缺失 → 暂停 (fail-closed), 不回退 root ssh
        return {"ok": False, "exit_code": -1, "timed_out": False, "env": f"ssh:{host}",
                "duration_s": 0, "output": f"远程隔离环境不可用, 已暂停: {e}"}
    finally:
        if canceller is not None:
            canceller.cancel()
            await asyncio.gather(canceller, return_exceptions=True)
        try:
            await be.cleanup()
        except Exception:
            pass


def _script_digest(path: str) -> str:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            if size > MAX_SCRIPT_BYTES:
                raise ScriptRegistryError("script_too_large", path)
            digest.update(chunk)
    return digest.hexdigest()


def _metadata(result: dict, entry: dict, digest: str) -> dict:
    return {**result, "risk": entry["risk"], "permissions": entry["permissions"],
            "platform": entry["platform"], "version": entry["version"],
            "version_digest": digest, "cancel": entry["cancel"]}


async def run_registered(name: str, params: dict = None, host: str = "localhost",
                         cancel=None) -> dict:
    """执行登记脚本 (受限环境; 未登记/参数非法/平台不符/隔离不可用 → 拒绝或暂停)"""
    entry = get_entry(name)
    if not entry:
        raise ScriptRegistryError(
            "not_registered",
            f"脚本 '{name}' 未登记 (skills/../scripts/registered/registry.json); "
            "任意脚本内容执行已被 P0 安全策略禁止")
    entry = validate_entry(entry)
    if current_platform() not in entry["platform"]:
        raise ScriptRegistryError("platform_not_supported",
                                  f"脚本 '{name}' 不支持 {current_platform()}")
    hosts = entry["hosts"]
    if host not in hosts:
        raise ScriptRegistryError("host_not_registered", f"host '{host}' 不在该脚本登记范围")
    for value in entry["target_dirs"]:
        path = Path(value)
        if (not path.is_dir() or path.is_symlink()
                or os.path.realpath(value) != os.path.abspath(value)):
            raise ScriptRegistryError("target_dir_unavailable", value)
    script_path = str(REGISTRY_DIR / str(entry.get("file") or ""))
    if not os.path.isfile(script_path):
        raise ScriptRegistryError("script_file_missing", script_path)
    digest = _script_digest(script_path)
    if entry.get("sha256") and digest != entry["sha256"]:
        raise ScriptRegistryError("registry_hash_mismatch", f"脚本 '{name}' 与登记摘要不符")
    argv = build_argv(entry, params or {})
    os.chmod(script_path, 0o755)

    if host in ("localhost", "127.0.0.1", "local"):
        if docker_image() and await _docker_available():
            result = await _run_docker(script_path, entry, argv, cancel)
        elif await _prlimit_available():
            result = await _run_local_restricted(script_path, entry, argv, cancel)
        else:
            # SPEC: 隔离不可用则暂停 (不裸跑)
            result = {"ok": False, "exit_code": -1, "timed_out": False, "cancelled": False,
                      "env": "none", "duration_s": 0,
                      "output": ("受限执行环境不可用 (无 prlimit / 未配置 docker 镜像), "
                                 "脚本执行已暂停; 请配置 ZOPS_SCRIPT_DOCKER_IMAGE 或安装 prlimit")}
    else:
        result = await _run_remote(script_path, entry, argv, host, cancel)
    return _metadata(result, entry, digest)


async def _docker_available() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info", stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        return (await proc.wait()) == 0
    except FileNotFoundError:
        return False


async def _prlimit_available() -> bool:
    return os.path.isfile("/usr/bin/prlimit")
