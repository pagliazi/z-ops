"""Redis 是唯一权威状态；WATCH/MULTI 提供跨进程原子性，可注入异步 Redis fake。"""

import asyncio
import secrets
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from redis.exceptions import WatchError

from . import protocol as p


class DeviceStore:
    def __init__(self, redis, prefix="zops:cu2", clock=time.time):
        self.r, self.prefix, self.clock = redis, prefix, clock

    def key(self, kind, *parts):
        # 长度编码由 JSON 提供，不允许拼接产生 actor/session 分隔符碰撞。
        return f"{self.prefix}:{kind}:{p.digest(p.dumps(parts))}"

    def device_key(self, device_id):
        return self.key("device", p.identifier(device_id))

    def index_key(self, actor):
        return self.key("tasks", p.text(actor, 128))

    def task_key(self, actor, device, session, task):
        return self.key("task", actor, device, session, task)

    async def json(self, key, pipe=None):
        raw = await (pipe or self.r).get(key)
        return p.loads(raw) if raw else None

    async def atomic(self, keys, operation):
        for _ in range(64):
            async with self.r.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(*keys)
                    return await operation(pipe)
                except WatchError:
                    await asyncio.sleep(0)
        raise ValueError("状态繁忙，请稍后查询；命令不会自动重发")

    async def secret(self):
        key = self.key("bus-secret")
        await self.r.set(key, secrets.token_hex(32), nx=True)
        value = await self.r.get(key)
        return value.encode() if isinstance(value, str) else value

    async def publish(self, channel, payload):
        return await self.r.publish(channel, p.signed(await self.secret(), payload))

    def channel(self, device_id, generation, control=False):
        return self.key("control" if control else "commands", device_id, generation)

    def response_channel(self, command_id):
        return self.key("responses", command_id)

    async def rate_limit(self, category, identity, limit=20, period=60):
        key = self.key("rate", category, identity, int(self.clock() // period))

        async def op(pipe):
            count = int(await pipe.get(key) or 0)
            if count >= limit:
                return False
            pipe.multi()
            pipe.set(key, count + 1, ex=period + 1)
            await pipe.execute()
            return True

        return await self.atomic([key], op)

    @staticmethod
    def owned(device, actor):
        if not device or device["actor_id"] != actor:
            raise ValueError("设备不存在或无权访问")
        return device

    def online(self, device):
        return bool(device and not device["revoked"] and device.get("ready") and device.get("online_until", 0) > self.clock())

    async def create_pair_code(self, actor):
        actor = p.text(actor, 128)
        if not await self.rate_limit("pair", actor, 10):
            raise ValueError("生成配对码过于频繁")
        for _ in range(4):
            code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(10))
            if await self.r.set(self.key("pair", code), actor, nx=True, ex=p.PAIR_TTL):
                return {"code": code, "expires_in": p.PAIR_TTL}
        raise ValueError("配对码生成失败")

    async def enroll(self, code, name, platform, public_key):
        code, name, platform = p.text(code, 32).upper(), p.text(name, 120), p.text(platform, 64)
        Ed25519PublicKey.from_public_bytes(p.b64(public_key, 32))
        code_key = self.key("pair", code)
        device_id = "dev_" + secrets.token_hex(16)

        async def op(pipe):
            actor = await pipe.get(code_key)
            if not actor:
                raise ValueError("配对码无效或已过期")
            if isinstance(actor, bytes):
                actor = actor.decode()
            device = {
                "actor_id": actor, "device_id": device_id, "name": name, "platform": platform,
                "public_key": public_key, "version": "", "capabilities": [], "revoked": False,
                "epoch": 0, "connection_generation": "", "fencing_token": 0, "seq": 0,
                "last_sent_seq": 0, "ready": False, "online_until": 0, "active_task": "",
            }
            pipe.multi()
            pipe.delete(code_key)
            pipe.set(self.device_key(device_id), p.dumps(device))
            pipe.sadd(self.key("devices", actor), device_id)
            await pipe.execute()
            return {"device_id": device_id}

        return await self.atomic([code_key], op)

    async def challenge(self, device_id):
        key = self.device_key(device_id)
        cid, nonce = "ch_" + secrets.token_hex(16), secrets.token_urlsafe(32)

        async def op(pipe):
            device = await self.json(key, pipe)
            if not device or device["revoked"]:
                raise ValueError("设备凭据无效")
            record = {"device_id": device_id, "actor_id": device["actor_id"], "epoch": device["epoch"], "nonce": nonce}
            pipe.multi()
            pipe.set(self.key("challenge", cid), p.dumps(record), ex=p.CHALLENGE_TTL)
            await pipe.execute()
            return {"challenge_id": cid, "nonce": nonce}

        return await self.atomic([key], op)

    async def ticket(self, device_id, challenge_id, signature):
        p.identifier(challenge_id)
        signature = p.b64(signature, 64)
        dkey, ckey = self.device_key(device_id), self.key("challenge", challenge_id)
        ticket = secrets.token_urlsafe(32)

        async def op(pipe):
            device, challenge = await self.json(dkey, pipe), await self.json(ckey, pipe)
            if not device or device["revoked"] or not challenge or challenge["device_id"] != device_id or challenge["epoch"] != device["epoch"]:
                raise ValueError("挑战无效或已过期")
            try:
                Ed25519PublicKey.from_public_bytes(p.b64(device["public_key"], 32)).verify(
                    signature, f"{challenge_id}:{challenge['nonce']}".encode("utf-8"))
            except InvalidSignature as exc:
                raise ValueError("设备签名无效") from exc
            record = {"actor_id": device["actor_id"], "device_id": device_id, "epoch": device["epoch"]}
            pipe.multi()
            pipe.delete(ckey)
            pipe.set(self.key("ticket", ticket), p.dumps(record), ex=p.TICKET_TTL)
            await pipe.execute()
            return {"ticket": ticket}

        return await self.atomic([dkey, ckey], op)

    def audit(self, pipe, task_key, task, event, reason="", envelope=None):
        # 永久、只追加；reason 必须是服务端固定码，不能传入参数、结果或客户端错误。
        task["audit_seq"] = task.get("audit_seq", 0) + 1
        record = {"event_id": "evt_" + secrets.token_hex(12), "seq": task["audit_seq"],
                  "type": event, "ts": self.clock(), "data": {
                      k: task[k] for k in ("actor_id", "device_id", "session_id", "task_id", "state")}}
        record["data"]["reason"] = reason
        for field in ("connection_generation", "fencing_token", "command_id", "control_id", "seq"):
            source = envelope or task
            if field in source:
                record["data"][field] = source[field]
        pipe.rpush(task_key + ":events", p.dumps(record))

    def close_task(self, pipe, key, task, state, reason):
        task.update(state=state, reason=reason, updated_at=self.clock(), lease_expires=0)
        self.audit(pipe, key, task, "task_state", reason)
        pipe.set(key, p.dumps(task))
        if state in p.TERMINAL:
            pipe.delete(key + ":frame")

    async def old_task(self, pipe, device):
        key = device.get("active_task")
        if not key:
            return None
        await pipe.watch(key)
        task = await self.json(key, pipe)
        return (key, task) if task else None

    async def connect_ticket(self, ticket):
        p.text(ticket, 128)
        tkey = self.key("ticket", ticket)
        generation = secrets.token_hex(16)

        async def op(pipe):
            record = await self.json(tkey, pipe)
            if not record:
                raise ValueError("短票据无效或已消费")
            dkey = self.device_key(record["device_id"])
            await pipe.watch(dkey)
            device = await self.json(dkey, pipe)
            if not device or device["revoked"] or device["epoch"] != record["epoch"] or device["actor_id"] != record["actor_id"]:
                raise ValueError("设备已撤销")
            old = await self.old_task(pipe, device)
            previous = device["connection_generation"]
            device.update(connection_generation=generation, online_until=self.clock() + p.CONNECTION_TTL,
                          ready=False, capabilities=[], active_task="", last_sent_seq=0)
            # 所有同代签发但尚未使用的票据/挑战也失效。
            device["epoch"] += 1
            device["fencing_token"] += 1
            pipe.multi()
            pipe.delete(tkey)
            if old and old[1]["state"] not in p.TERMINAL:
                self.close_task(pipe, *old, "unknown", "connection_replaced")
            pipe.set(dkey, p.dumps(device))
            await pipe.execute()
            return device, previous

        device, previous = await self.atomic([tkey], op)
        if previous:
            await self.publish(self.channel(device["device_id"], previous, True), {"type": "disconnect", "connection_generation": previous})
        return device

    async def heartbeat(self, device_id, generation, hello=None):
        key = self.device_key(device_id)

        async def op(pipe):
            device = await self.json(key, pipe)
            if not device or device["revoked"] or device["connection_generation"] != generation or device["online_until"] <= self.clock():
                return False
            if hello is not None:
                if hello.get("type") != "hello" or type(hello.get("protocol")) is not int or hello["protocol"] != 2 or device["ready"]:
                    raise ValueError("不兼容的设备协议")
                caps = hello.get("capabilities")
                if not isinstance(caps, list) or len(caps) > 64 or any(not isinstance(c, str) or len(c) > 80 for c in caps):
                    raise ValueError("能力列表格式错误")
                device.update(ready=True, version=p.text(hello.get("version"), 80),
                              platform=p.text(hello.get("platform"), 64), capabilities=sorted(set(caps) & p.METHODS))
                if "grants" in hello:
                    # 授权摘要只用于设置页展示; 真实授权仍只在本机 config 生效。
                    device["grants"] = p.grants(hello["grants"])
            device["online_until"] = self.clock() + p.CONNECTION_TTL
            pipe.multi()
            pipe.set(key, p.dumps(device))
            await pipe.execute()
            return True

        return await self.atomic([key], op)

    async def detach(self, device_id, generation, revoked_by=None):
        key = self.device_key(device_id)

        async def op(pipe):
            device = await self.json(key, pipe)
            if revoked_by is not None:
                self.owned(device, revoked_by)
            elif not device or device["connection_generation"] != generation:
                return None
            old = await self.old_task(pipe, device)
            previous = device["connection_generation"]
            device.update(ready=False, online_until=0, active_task="", connection_generation="")
            device["fencing_token"] += 1
            device["epoch"] += 1
            if revoked_by is not None:
                device["revoked"] = True
                selection = self.key("selection", revoked_by)
                await pipe.watch(selection)
                selected = await pipe.get(selection)
            else:
                selected = None
            pipe.multi()
            if old and old[1]["state"] not in p.TERMINAL:
                self.close_task(pipe, *old, "unknown", "device_revoked" if revoked_by is not None else "disconnected")
            if selected in (device_id, device_id.encode()):
                pipe.delete(selection)
            pipe.set(key, p.dumps(device))
            await pipe.execute()
            return previous

        previous = await self.atomic([key], op)
        if previous and revoked_by is not None:
            await self.publish(self.channel(device_id, previous, True), {"type": "disconnect", "connection_generation": previous})

    async def get_selected_device(self, actor):
        device_id = await self.r.get(self.key("selection", actor))
        if isinstance(device_id, bytes):
            device_id = device_id.decode()
        device = await self.json(self.device_key(device_id)) if device_id else None
        return device if device and device["actor_id"] == actor and not device["revoked"] else None

    async def select(self, actor, device_id):
        key = self.device_key(device_id)

        async def op(pipe):
            device = self.owned(await self.json(key, pipe), actor)
            if device["revoked"]:
                raise ValueError("设备已撤销")
            pipe.multi()
            pipe.set(self.key("selection", actor), device_id)
            await pipe.execute()
            return {"selected_device_id": device_id}

        return await self.atomic([key], op)

    async def list_devices(self, actor):
        ids = await self.r.smembers(self.key("devices", actor))
        devices = []
        for device_id in sorted(ids):
            if isinstance(device_id, bytes):
                device_id = device_id.decode()
            device = await self.json(self.device_key(device_id))
            if device and device["actor_id"] == actor:
                record = {k: device[k] for k in ("device_id", "name", "platform", "version", "capabilities", "revoked")}
                if "grants" in device:
                    record["grants"] = device["grants"]
                record["online"] = self.online(device)
                devices.append(record)
        selected = await self.get_selected_device(actor)
        return {"devices": devices, "selected_device_id": selected["device_id"] if selected else None}
