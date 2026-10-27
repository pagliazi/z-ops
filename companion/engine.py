"""单执行槽、无待执行缓存；控制消息不排在耗时命令之后。"""
import asyncio
import contextlib
import time
from .executor import execution_mode
from .protocol import READ_ONLY, result
from .security import Denied, Paused, encode, pause_code, require


class Engine:
    def __init__(self, guard, executor):
        self.guard = guard
        self.executor = executor
        self.job = None
        self.outgoing = asyncio.Queue(maxsize=8)
        self.failed = asyncio.Event()
        self.closing = False

    def emit(self, reply):
        if self.closing:
            return
        try:
            raw = encode(reply).decode()
            self.outgoing.put_nowait(raw)
        except Exception:
            self.failed.set()

    async def interrupt(self):
        if self.job and not self.job.done():
            self.job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.job
        self.job = None
        await self.executor.stop()

    async def close(self):
        self.closing = True
        await self.interrupt()
        self.guard.journal.stop_tasks()
        while not self.outgoing.empty():
            self.outgoing.get_nowait()

    async def receive(self, command):
        try:
            replay, switch = self.guard.admit(command)
        except Exception:
            # 不回显异常、params、URL 或文件内容。
            self.emit(result(command, "rejected", error="invalid_or_stale_command"))
            return
        if replay:
            self.emit(replay)
            return
        if switch:
            await self.interrupt()
        if command["type"] in ("cancel", "pause"):
            await self.interrupt()
            status = "cancelled" if command["type"] == "cancel" else "paused"
            self.guard.journal.finish(command, status)
            self.emit(result(command, status))
            return
        if self.job and not self.job.done():
            self.guard.journal.finish(command, "rejected")
            self.emit(result(command, "rejected", error="device_busy_no_queue"))
            return
        self.job = asyncio.create_task(self.execute(command))

    async def execute(self, command):
        status, data, error = "unknown", {}, "operation_incomplete"
        try:
            self.guard.validate(command)
            require(command["task_id"] == self.guard.active, "task_not_active")
            async with asyncio.timeout(min(60, command["expiry"] - time.time())):
                data = await self.executor.execute(command)
            encode(data)
            status, error = "ok", ""
        except asyncio.CancelledError:
            status = "cancelled" if command["method"] in READ_ONLY else "unknown"
            data, error = {}, "interrupted_verify_side_effects"
        except Paused as pause:
            status, error = "paused", "local_authorization_required"
            # 具体原因只传自定义静态码, 供小窗展示暂停原因; 不泄露页面内容。
            data = {"pause_reason": pause_code(pause)}
            self.guard.journal.stop_tasks()
            await self.executor.stop()
        except Denied:
            status, data, error = "rejected", {}, "operation_not_authorized_or_invalid"
        except Exception:
            status = "rejected" if command["method"] in READ_ONLY else "unknown"
            data, error = {}, "operation_failed_verify_side_effects"
            await self.executor.stop()
        finally:
            self.guard.journal.finish(command, status)
            mode = execution_mode(command["method"])
            if mode and type(data) is dict:
                data = {**data, "execution_mode": mode}
            self.emit(result(command, status, data, error))
