"""Native Messaging stdio host；没有 TCP/IPC 监听，不附加用户标签页。

扩展启动参数必须是本机授权的 chrome-extension origin。
hello 后返回本 host 的绑定字段；浏览器请求使用完整 v2 command 信封。
这是独立无头路由，不伪称已连 WSS，也不与 WSS 任务共享浏览器。
"""
import argparse
import asyncio
import contextlib
import hashlib
import os
import secrets
import struct
import sys
from . import __version__
from . import login_state
from .config import Config, default_path
from .engine import Engine
from .executor import Executor
from .protocol import Guard, Journal
from .security import Denied, MAX_INPUT, MAX_OUTPUT, decode, encode, fields, require


def read_frame(stream):
    def exact(size):
        out = bytearray()
        while len(out) < size:
            block = stream.read(size - len(out))
            if not block:
                if not out:
                    return None
                raise ValueError("truncated_native_frame")
            out.extend(block)
        return bytes(out)
    header = exact(4)
    if header is None:
        return None
    size = struct.unpack("<I", header)[0]
    require(0 < size <= MAX_INPUT, "native_frame_limit")
    payload = exact(size)
    require(payload is not None, "truncated_native_frame")
    return decode(payload)


def write_frame(stream, message):
    payload = encode(message, MAX_OUTPUT)
    stream.write(struct.pack("<I", len(payload)) + payload)
    stream.flush()


async def receive_frame(reader):
    if not isinstance(reader, asyncio.StreamReader):
        return await asyncio.to_thread(read_frame, reader)
    try:
        async with asyncio.timeout(90):
            header = await reader.readexactly(4)
            size = struct.unpack("<I", header)[0]
            require(0 < size <= MAX_INPUT, "native_frame_limit")
            return decode(await reader.readexactly(size))
    except asyncio.IncompleteReadError as exc:
        require(not exc.partial, "truncated_native_frame")
        return None


async def send_frame(writer, message):
    if not isinstance(writer, asyncio.StreamWriter):
        return await asyncio.to_thread(write_frame, writer, message)
    payload = encode(message, MAX_OUTPUT)
    writer.write(struct.pack("<I", len(payload)) + payload)
    await asyncio.wait_for(writer.drain(), 5)


def login_handoff(config, packet):
    """扩展显式授权的站点登录态，只落本机私有状态目录；回执不含任何 Cookie。"""
    try:
        fields(packet, ("type", "protocol", "origin", "cookies", "expiry"))
        require(packet["protocol"] == 2, "protocol_mismatch")
        count = login_state.store(config, packet["origin"], packet["cookies"], packet["expiry"])
        return {"type": "login_handoff_result", "protocol": 2, "ok": True, "count": count}
    except (Denied, ValueError, TypeError, RecursionError):
        return {"type": "login_handoff_result", "protocol": 2, "ok": False,
                "error": "login_handoff_denied"}


async def serve_host(config, extension_origin, reader, writer):
    executor = Executor(config, browser_only=True)
    try:
        return await _serve_host(config, extension_origin, reader, writer, executor)
    finally:
        await executor.close()


async def _serve_host(config, extension_origin, reader, writer, executor):
    require(extension_origin in config.native_origins, "extension_not_locally_authorized")
    require(bool(config.actor_id and config.device_id), "enrolled_welcome_required")
    hello = await receive_frame(reader)
    # 2.x 扩展会附带 version/capabilities/screenshot; 这些只作展示, 不扩大本机能力。
    fields(hello, ("type", "protocol"), ("version", "capabilities", "screenshot"))
    require(hello["type"] == "hello" and hello["protocol"] == 2, "native_hello_required")
    await executor.prepare()
    # 本扩展独占 journal；不影响运行中的 WSS journal。
    suffix = hashlib.sha256(extension_origin.encode()).hexdigest()[:16]
    from .locking import instance_lock
    with instance_lock(config.state / ("native-" + suffix + ".lock")):
        journal = Journal(config.state / ("native-" + suffix + ".sqlite"))
        guard = Guard(config, journal)
        generation = "native-" + secrets.token_hex(16)
        guard.bind({"type": "welcome", "protocol": 2, "actor_id": config.actor_id,
                    "device_id": config.device_id, "connection_generation": generation})
        engine = Engine(guard, executor)
        await send_frame(writer, {"type": "hello", "protocol": 2, "version": __version__,
            "capabilities": executor.capabilities, "actor_id": config.actor_id,
            "device_id": config.device_id, "connection_generation": generation,
            "next_fencing_token": journal.get("fence", 0) + 1,
            "route": "isolated_headless", "server_connected": False,
            "shared_server_task": False})
        async def send():
            while True:
                raw = await engine.outgoing.get()
                await send_frame(writer, decode(raw, MAX_OUTPUT))
        sending = asyncio.create_task(send())
        try:
            while not engine.failed.is_set():
                receiving = asyncio.create_task(receive_frame(reader))
                failure = asyncio.create_task(engine.failed.wait())
                try:
                    done, _ = await asyncio.wait((receiving, sending, failure), return_when=asyncio.FIRST_COMPLETED)
                    if receiving not in done:
                        break
                    packet = receiving.result()
                finally:
                    receiving.cancel()
                    failure.cancel()
                    await asyncio.gather(receiving, failure, return_exceptions=True)
                if packet is None:
                    break
                if packet.get("type") == "login_handoff":
                    engine.emit(login_handoff(config, packet))
                elif packet.get("type") == "ping":
                    fields(packet, ("type",), ("protocol",))
                    engine.emit({"type": "pong", "protocol": 2})
                else:
                    await engine.receive(packet)
        finally:
            await engine.close()
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
            await executor.close()
            journal.close()


async def stdio(config, extension_origin):
    if os.name == "nt":
        # Windows 的事件循环不支持 connect_read_pipe(stdin);
        # serve_host 对同步流走线程读取, 保持同一协议与超时。
        await serve_host(config, extension_origin, sys.stdin.buffer, sys.stdout.buffer)
        return
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_INPUT + 4)
    read_transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    write_transport, protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop), sys.stdout.buffer)
    writer = asyncio.StreamWriter(write_transport, protocol, None, loop)
    try:
        await serve_host(config, extension_origin, reader, writer)
    finally:
        read_transport.close()
        writer.close()


def main():
    parser = argparse.ArgumentParser(description="Companion Native Messaging：本机独立无头浏览器路由")
    parser.add_argument("origin", help="浏览器传入的扩展 origin，必须先在本机登记")
    parser.add_argument("--config", default=str(default_path()))
    args = parser.parse_args()
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    try:
        asyncio.run(stdio(Config.load(args.config), args.origin))
    except Exception:
        print("Native host 已安全关闭；请检查本机授权和协议。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
