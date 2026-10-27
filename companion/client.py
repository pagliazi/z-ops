"""主动 HTTPS/WSS，无监听端口、无代理继承、无重定向跟随、无秘密日志。"""
import asyncio
import http.client
import os
import platform
import secrets
import ssl
import sys
from urllib.parse import quote, urlsplit
from websockets.asyncio.client import connect
from . import __version__
from .engine import Engine
from .identity import Identity
from .protocol import Guard
from .security import MAX_INPUT, decode, encode, fields, identifier, require, text


# 自编译或打包的解释器可能把默认信任库指向空目录, 此时所有证书都会被判为不可信。
DEFAULT_CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt",
                      "/etc/pki/tls/certs/ca-bundle.crt",
                      "/etc/ssl/cert.pem")


def default_context(bundles=DEFAULT_CA_BUNDLES):
    """默认信任库为空时回落到系统 CA 包; 只换信任源, 不降低校验强度。"""
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) > 0:
        return context
    for path in bundles:
        if not os.path.isfile(path):
            continue
        try:
            return ssl.create_default_context(cafile=path)
        except (ssl.SSLError, OSError):
            continue
    return context


class HTTPS:
    def __init__(self, server, context=None):
        self.server = server
        self.context = context or default_context()

    async def post(self, endpoint, data):
        def request():
            parsed = urlsplit(self.server)
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                                                     timeout=10, context=self.context)
            try:
                connection.request("POST", "/api/cu/devices/" + endpoint, encode(data),
                                   {"Content-Type": "application/json", "Accept": "application/json"})
                response = connection.getresponse()
                require(response.status == 200, "server_request_rejected")
                return decode(response.read(MAX_INPUT + 1))
            finally:
                connection.close()
        return await asyncio.to_thread(request)


async def enroll(config, code, transport=None, identity=None):
    require(not config.device_id, "already_enrolled")
    identity = identity or Identity(config)
    transport = transport or HTTPS(config.server)
    public = identity.public()
    config.save()  # key_id 先落盘，配对失败不能默默生成另一把钥匙。
    response = await transport.post("enroll", {"code": text(code, 128), "name": config.name,
                                  "platform": platform.system().lower(), "public_key": public})
    fields(response, ("device_id",))
    config.device_id = identifier(response["device_id"])
    config.save()


def grants(config):
    """只上报授权范围摘要: 目录用末级名, 不暴露完整用户路径、站点路径或脚本内容。"""
    from pathlib import Path

    def root_name(value):
        return (Path(value).name or str(Path(value).anchor) or ".")[:80]

    return {
        "roots": [{"name": root_name(root["path"]), "read": bool(root["read"]),
                   "write_new": bool(root["write_new"]), "manage": bool(root.get("manage"))}
                  for root in config.roots[:32]],
        "origins": sorted({str(site)[:200] for site in config.origins})[:64],
        "scripts": sorted(config.scripts)[:32],
        "native_origins": sorted({str(origin)[:200] for origin in config.native_origins})[:16],
        "apps": sorted(config.desktop_apps)[:16],
    }


async def ticket(config, transport, identity):
    response = await transport.post("challenge", {"device_id": config.device_id})
    fields(response, ("challenge_id", "nonce"))
    signature = identity.sign(response["challenge_id"], response["nonce"])
    response = await transport.post("ticket", {"device_id": config.device_id,
                                    "challenge_id": response["challenge_id"], "signature": signature})
    fields(response, ("ticket",))
    return text(response["ticket"], 4096)


async def session(ws, config, journal, executor):
    guard = Guard(config, journal)
    engine = Engine(guard, executor)
    tasks = []
    try:
        welcome = decode(await asyncio.wait_for(ws.recv(), 10))
        guard.bind(welcome)
        await ws.send(encode({"type": "hello", "protocol": 2, "capabilities": executor.capabilities,
                              "version": __version__, "platform": platform.system().lower(),
                              "grants": grants(config)}).decode())
        async def receive():
            async for raw in ws:
                packet = decode(raw)
                if packet.get("type") in ("ping", "pong"):
                    fields(packet, ("type",), ("protocol",))
                    require(packet.get("protocol", 2) == 2, "protocol_mismatch")
                    if packet["type"] == "ping":
                        engine.emit({"type": "pong", "protocol": 2})
                else:
                    await engine.receive(packet)
        async def send():
            while True:
                raw = await engine.outgoing.get()
                await asyncio.wait_for(ws.send(raw), 5)
        async def heartbeat():
            while True:
                await asyncio.sleep(20)
                pong = await ws.ping()
                await asyncio.wait_for(pong, 10)
                engine.emit({"type": "ping", "protocol": 2})
        tasks = [asyncio.create_task(receive()), asyncio.create_task(send()),
                 asyncio.create_task(heartbeat()), asyncio.create_task(engine.failed.wait())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        # 先停操作、释放上下文，才允许下一轮获取票据和重连。
        await engine.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(config, journal, executor, stop):
    identity = Identity(config)
    identity.key()
    transport = HTTPS(config.server)
    delay = 1
    while not stop.is_set():
        try:
            credential = await ticket(config, transport, identity)
            url = "wss" + config.server[5:] + "/api/cu/devices/connect?ticket=" + quote(credential, safe="")
            # 单次连接：禁止 websockets 隐式重连和重定向到其他 origin。
            class DirectConnect(connect):
                def process_redirect(self, exc):
                    return exc
            async with DirectConnect(url, ssl=transport.context, proxy=None, max_size=MAX_INPUT,
                    max_queue=4, write_limit=65536, ping_interval=None, open_timeout=10,
                    close_timeout=3, compression=None) as ws:
                active = asyncio.create_task(session(ws, config, journal, executor))
                stopping = asyncio.create_task(stop.wait())
                try:
                    done, _ = await asyncio.wait((active, stopping), return_when=asyncio.FIRST_COMPLETED)
                    if active in done:
                        active.result()
                finally:
                    active.cancel()
                    stopping.cancel()
                    await asyncio.gather(active, stopping, return_exceptions=True)
            delay = 1
        except asyncio.CancelledError:
            await executor.stop()
            raise
        except Exception:
            await executor.stop()
            print("连接中断或认证失败；本机任务已停止。", file=sys.stderr)
        if not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), delay + secrets.randbelow(500) / 1000)
            except TimeoutError:
                pass
            delay = min(30, delay * 2)
