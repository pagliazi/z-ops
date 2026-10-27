"""单设备 WS 通道：独立接收、优先控制队列、有限发送等待和代际看门狗。"""

import asyncio
import itertools

from . import protocol as p


class DeviceConnection:
    def __init__(self, service, websocket, device):
        self.service, self.ws, self.device = service, websocket, device
        self.device_id = device["device_id"]
        self.actor = device["actor_id"]
        self.generation = device["connection_generation"]
        self.queue = asyncio.PriorityQueue(maxsize=16)
        self.order = itertools.count()
        self.hello = False
        self.stopped = asyncio.Event()
        self.sub = None

    def enqueue(self, message, priority=1):
        # 队列满即失败关闭，不无界占用内存，不等待慢客户端。
        self.queue.put_nowait((priority, next(self.order), message))

    async def receive(self):
        while not self.stopped.is_set():
            raw = await asyncio.wait_for(self.ws.receive_text(), timeout=30 if self.hello else 10)
            if len(raw) > p.MAX_MESSAGE or len(raw.encode("utf-8")) > p.MAX_MESSAGE:
                raise ValueError("消息过大")
            msg = p.loads(raw)
            if not isinstance(msg, dict):
                raise ValueError("消息格式错误")
            if not await self.service.rate_limit("ws", (self.device_id, self.generation), 120, 10):
                raise ValueError("设备消息过于频繁")
            kind = msg.get("type")
            if not self.hello:
                if kind != "hello" or not await self.service.heartbeat(self.device_id, self.generation, msg):
                    raise ValueError("设备握手失败")
                self.hello = True
            elif kind == "ping":
                if set(msg) - {"type", "protocol"} or msg.get("protocol", 2) != 2:
                    raise ValueError("心跳格式错误")
                if not await self.service.heartbeat(self.device_id, self.generation):
                    raise ValueError("连接已失效")
                self.enqueue({"type": "pong", "protocol": 2}, 0)
            elif kind == "result":
                if not await self.service.accept_result(self.device_id, self.generation, msg):
                    # 无效/重复/旧任务响应丢弃，不允许迟到响应断开新任务连接。
                    continue
                if not await self.service.heartbeat(self.device_id, self.generation):
                    self.stopped.set()
            else:
                raise ValueError("不支持的协议消息")

    async def bus(self):
        secret = await self.service.secret()
        command_channel = self.service.channel(self.device_id, self.generation)
        control_channel = self.service.channel(self.device_id, self.generation, True)
        while not self.stopped.is_set():
            message = await self.sub.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if not message:
                await asyncio.sleep(0)
                continue
            try:
                obj = p.verified(secret, message["data"])
                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()
                if not isinstance(obj, dict) or obj.get("connection_generation") != self.generation:
                    continue
                if obj.get("type") == "disconnect" and channel == control_channel:
                    self.stopped.set()
                    return
                if obj.get("device_id") != self.device_id or obj.get("actor_id") != self.actor:
                    continue
                kind = obj.get("type")
                if kind == "command" and channel == command_channel:
                    self.enqueue(obj, 1)
                elif kind in {"cancel", "pause"} and channel == control_channel:
                    self.enqueue(obj, 0)
            except (ValueError, TypeError, KeyError):
                # 不记录载荷、签名、nonce、截图或远端错误。
                continue

    async def write(self):
        while not self.stopped.is_set():
            _, _, message = await self.queue.get()
            if message.get("type") != "pong":
                await self.service.check_session(self.actor, message["session_id"])
                if not await self.service.claim_command(message):
                    continue
            # 无独立帧 WS；截图只由设备 result 上行，服务器绝不在控制 WS 下行大帧。
            await asyncio.wait_for(self.ws.send_text(p.dumps(message)), timeout=p.SEND_TIMEOUT)

    async def watch(self):
        while not self.stopped.is_set():
            await asyncio.sleep(1)
            device = await self.service.json(self.service.device_key(self.device_id))
            if not device or device["revoked"] or device["connection_generation"] != self.generation or device["online_until"] <= self.service.clock():
                self.stopped.set()
                return
            if device.get("active_task"):
                key = device["active_task"]
                task = await self.service.json(key)
                if task:
                    await self.service.reconcile(key, task)

    async def run(self):
        jobs = []
        self.sub = self.service.r.pubsub()
        try:
            await self.sub.subscribe(self.service.channel(self.device_id, self.generation),
                                     self.service.channel(self.device_id, self.generation, True))
            await self.ws.accept()
            welcome = {"type": "welcome", "protocol": 2, "actor_id": self.actor,
                       "device_id": self.device_id, "connection_generation": self.generation}
            await asyncio.wait_for(self.ws.send_text(p.dumps(welcome)), timeout=p.SEND_TIMEOUT)
            jobs = [asyncio.create_task(fn()) for fn in (self.receive, self.bus, self.write, self.watch, self.stopped.wait)]
            await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.stopped.set()
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await self.sub.aclose()
            await self.service.detach(self.device_id, self.generation)
            try:
                await asyncio.wait_for(self.ws.close(code=1008), timeout=p.SEND_TIMEOUT)
            except Exception:
                pass
