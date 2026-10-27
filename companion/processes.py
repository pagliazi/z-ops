"""只管理任务自己创建的子树；不按进程名终止用户应用。"""
import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path
import psutil
from .security import Denied, MAX_INPUT, MAX_OUTPUT, decode, encode, fields, require

JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _windows_job():
    """任务专属 Job Object: 关闭句柄即终止整棵子树, 并限制内存。"""
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Io(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job = kernel32.CreateJobObjectW(None, None)
    require(job, "job_object_unavailable")
    info = Extended()
    info.BasicLimitInformation.LimitFlags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                                              | JOB_OBJECT_LIMIT_PROCESS_MEMORY)
    info.ProcessMemoryLimit = 1024 * 1024 * 1024
    if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                            ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        raise Denied("job_object_limit_failed")
    return job


def _assign_job(job, pid):
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel32.OpenProcess(0x0100 | 0x0001 | 0x1000, False, pid)
    require(handle, "job_process_open_failed")
    try:
        require(kernel32.AssignProcessToJobObject(job, handle), "job_object_assignment_failed")
    finally:
        kernel32.CloseHandle(handle)


def _terminate_job(job):
    import ctypes
    with contextlib.suppress(Exception):
        ctypes.WinDLL("kernel32").TerminateJobObject(job, 1)
    with contextlib.suppress(Exception):
        ctypes.WinDLL("kernel32").CloseHandle(job)


class OwnedProcesses:
    def __init__(self):
        self.active = set()
        self.identities = {}
        self.jobs = {}

    async def terminate(self, proc):
        job = self.jobs.pop(proc, None)
        if job:
            _terminate_job(job)
        # 只使用创建时保存的 PID+创建时间身份；退出后不重新按 PID 查找。
        parent = self.identities.get(proc)
        if parent is not None:
            try:
                if parent.is_running():
                    children = parent.children(recursive=True)
                    for child in reversed(children):
                        with contextlib.suppress(psutil.Error):
                            child.kill()
                    # psutil 发送信号前重新检查身份，不向可能复用的进程组发信号。
                    parent.kill()
            except psutil.Error:
                pass
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 3)

    async def stop(self):
        await asyncio.gather(*(self.terminate(p) for p in tuple(self.active)))

    async def run(self, argv, *, cwd, env, payload=None, timeout=20, limit=MAX_OUTPUT, pass_fds=()):
        proc = None
        async def spawn():
            if os.name == "nt":
                child = await asyncio.create_subprocess_exec(
                    *argv, cwd=str(cwd), env=env,
                    stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=65536,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0))
                # 先入 Job 再继续: 取消/超时会终止整棵子树, 不残留孙进程。
                job = _windows_job()
                try:
                    _assign_job(job, child.pid)
                except BaseException:
                    with contextlib.suppress(Exception):
                        child.kill()
                        await child.wait()
                    _terminate_job(job)
                    raise
                with contextlib.suppress(psutil.Error):
                    self.identities[child] = psutil.Process(child.pid)
                self.jobs[child] = job
                return child
            child = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=env, start_new_session=True, pass_fds=pass_fds,
                stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=65536)
            if child.returncode is None:
                with contextlib.suppress(psutil.NoSuchProcess):
                    self.identities[child] = psutil.Process(child.pid)
            return child
        spawning = asyncio.create_task(spawn())
        try:
            # 取消发生在 create_subprocess_exec 内也必须取回句柄并终止。
            proc = await asyncio.shield(spawning)
            self.active.add(proc)
            used = 0
            async def read(stream):
                nonlocal used
                chunks = []
                while block := await stream.read(16384):
                    used += len(block)
                    require(used <= limit, "process_output_limit")
                    chunks.append(block)
                return b"".join(chunks)
            async def communicate():
                if payload is not None:
                    proc.stdin.write(payload)
                    await proc.stdin.drain()
                    proc.stdin.close()
                readers = [asyncio.create_task(read(proc.stdout)), asyncio.create_task(read(proc.stderr))]
                try:
                    out, err = await asyncio.gather(*readers)
                    await proc.wait()
                    return out, err, proc.returncode
                finally:
                    for task in readers:
                        task.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
            async with asyncio.timeout(timeout):
                return await communicate()
        finally:
            if proc is None:
                with contextlib.suppress(Exception):
                    proc = await spawning
            if proc is not None:
                await self.terminate(proc)
                self.active.discard(proc)
                self.identities.pop(proc, None)
                self.jobs.pop(proc, None)


def minimal_env(directory=None):
    if os.name == "nt":
        # 最小环境: 只保留运行解释器必需的项, 不继承用户环境变量。
        temp = str(directory or Path.home())
        return {"PATH": os.defpath, "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                "TEMP": temp, "TMP": temp, "PYTHONUTF8": "1"}
    return {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


async def local_worker(owner, config, method, params):
    env = minimal_env(config.state)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [sys.executable, "-m", "companion.worker"]
    if getattr(sys, "frozen", False):
        argv = [sys.executable, "--internal-worker"]
    timeout = 20
    if method == "script.run":
        timeout = min(63, config.scripts.get(params.get("name"), {}).get("timeout", 15) + 3)
    payload = encode({"config": str(config.path), "method": method, "params": params}, MAX_INPUT)
    try:
        out, _, code = await owner.run(argv, cwd=config.state, env=env, payload=payload, timeout=timeout)
        require(code == 0, "worker_failed")
        result = decode(out, MAX_OUTPUT)
        fields(result, ("status", "data"), ("error",))
        require(result["status"] in ("ok", "rejected", "unknown") and type(result["data"]) is dict)
    except Exception:
        # 启动后超限、崩溃、截断回执均不能证明没有副作用。
        raise RuntimeError("local_worker_outcome_unknown") from None
    if result["status"] == "unknown":
        raise RuntimeError("local_side_effects_unknown")
    if result["status"] == "rejected":
        # 子进程也只能传固定码，不能把异常路径/内容带回远端。
        raise Denied("local_operation_rejected")
    return result["data"]
