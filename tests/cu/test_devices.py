"""隔离验收：标准库 unittest + 可注入 Redis fake，不建立任何真实 Redis 连接。"""

import asyncio
import base64
import io
import logging
import tempfile
import time
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException
from redis.exceptions import WatchError

import config
from api.routers import cu_devices as api
from core.cu import devices as module
from core.cu import protocol as p
from core.cu.connection import DeviceConnection
from core.cu.devices import DeviceService
from core.session_access import ensure_session_access
from core.task_runtime.ledger import k_task


class FakeServer:
    """仅实现本模块使用的 Redis 命令；事务执行不让出事件循环，WATCH 可重试。"""
    def __init__(self):
        self.data = {}
        self.expiry = {}
        self.versions = defaultdict(int)
        self.subscribers = defaultdict(set)
        self.offset = 0
        self.published = []

    def clock(self):
        return time.time() + self.offset

    def version(self, key):
        if key in self.expiry and self.expiry[key] <= self.clock():
            self.data.pop(key, None)
            self.expiry.pop(key, None)
            self.versions[key] += 1
        return self.versions[key]

    def call(self, op, *args, **kwargs):
        key = args[0] if args else ""
        self.version(key)
        old = self.data.get(key)
        if op == "get":
            return old
        if op == "set":
            if kwargs.get("nx") and old is not None:
                return None
            self.data[key] = str(args[1])
            if not kwargs.get("keepttl"):
                self.expiry.pop(key, None)
            if kwargs.get("ex"):
                self.expiry[key] = self.clock() + kwargs["ex"]
            self.versions[key] += 1
            return True
        if op == "delete":
            count = 0
            for key in args:
                count += key in self.data
                self.data.pop(key, None)
                self.expiry.pop(key, None)
                self.versions[key] += 1
            return count
        if op == "expire":
            if old is None:
                return False
            self.expiry[key] = self.clock() + args[1]
            self.versions[key] += 1
            return True
        if op == "ttl":
            return max(0, int(self.expiry[key] - self.clock())) if key in self.expiry else (-1 if old is not None else -2)
        if op == "hget":
            return (old or {}).get(args[1])
        if op == "hgetall":
            return dict(old or {})
        if op == "hset":
            target = self.data.setdefault(key, {})
            target.update(kwargs.get("mapping") or {args[1]: args[2]})
            self.versions[key] += 1
            return 1
        if op == "sadd":
            target = self.data.setdefault(key, set())
            target.update(args[1:])
            self.versions[key] += 1
            return 1
        if op == "smembers":
            return set(old or set())
        if op in {"rpush", "lpush"}:
            target = self.data.setdefault(key, [])
            if op == "rpush":
                target.extend(args[1:])
            else:
                target[0:0] = list(reversed(args[1:]))
            self.versions[key] += 1
            return len(target)
        if op == "lrange":
            return list((old or [])[args[1]:None if args[2] == -1 else args[2] + 1])
        if op == "llen":
            return len(old or [])
        raise AssertionError(f"未实现的 fake 命令：{op}")


class FakeRedis:
    def __init__(self, server):
        self.server = server

    def __getattr__(self, op):
        async def call(*args, **kwargs):
            await asyncio.sleep(0)
            return self.server.call(op, *args, **kwargs)
        return call

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def pubsub(self):
        return FakePubSub(self.server)

    async def publish(self, channel, data):
        self.server.published.append((channel, data))
        subscribers = self.server.subscribers[channel].copy()
        for subscriber in subscribers:
            subscriber.queue.put_nowait({"type": "message", "channel": channel, "data": data})
        return len(subscribers)

    async def hscan_iter(self, key):
        for entry in (await self.hgetall(key)).items():
            yield entry


class FakePipeline:
    def __init__(self, redis):
        self.r = redis
        self.watched = {}
        self.ops = []
        self.in_multi = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def watch(self, *keys):
        for key in keys:
            self.watched.setdefault(key, self.r.server.version(key))
        await asyncio.sleep(0)

    def multi(self):
        self.in_multi = True

    def __getattr__(self, op):
        def call(*args, **kwargs):
            if self.in_multi:
                self.ops.append((op, args, kwargs))
                return self
            return getattr(self.r, op)(*args, **kwargs)
        return call

    async def execute(self):
        await asyncio.sleep(0)
        if any(self.r.server.version(key) != version for key, version in self.watched.items()):
            raise WatchError()
        return [self.r.server.call(op, *args, **kwargs) for op, args, kwargs in self.ops]


class FakePubSub:
    def __init__(self, server):
        self.server, self.channels = server, set()
        self.queue = asyncio.Queue()

    async def subscribe(self, *channels):
        for channel in channels:
            self.server.subscribers[channel].add(self)
            self.channels.add(channel)

    async def get_message(self, ignore_subscribe_messages=True, timeout=0):
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=max(timeout, 0.001))
        except asyncio.TimeoutError:
            return None

    async def aclose(self):
        for channel in self.channels:
            self.server.subscribers[channel].discard(self)


class MemoryWS:
    def __init__(self):
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        self.closed = False

    async def accept(self):
        pass

    async def receive_text(self):
        return await self.incoming.get()

    async def send_text(self, raw):
        self.outgoing.put_nowait(p.loads(raw))

    async def close(self, code=1000):
        self.closed = True

    async def send(self, data):
        await self.incoming.put(p.dumps(data))

    async def next(self):
        return await asyncio.wait_for(self.outgoing.get(), timeout=2)


class DevicesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.production_guard = patch("redis.asyncio.from_url", side_effect=AssertionError("禁止真实 Redis 连接"))
        self.production_guard.start()
        self.addCleanup(self.production_guard.stop)
        self.server = FakeServer()
        self.r = FakeRedis(self.server)
        self.backend = DeviceService(self.r, prefix="isolated:cu2", clock=self.server.clock, session_check=ensure_session_access)
        self.agent = DeviceService(FakeRedis(self.server), prefix="isolated:cu2", clock=self.server.clock, session_check=ensure_session_access)
        for sid, actor in (("s1", "alice"), ("s2", "alice"), ("sb", "bob")):
            await self.r.hset(config.key_session_meta(sid), mapping={"username": actor})
        self.jobs = []
        self.connections = []

    async def asyncTearDown(self):
        for connection in self.connections:
            connection.stopped.set()
        for job in self.jobs:
            job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)

    def job(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.jobs.append(task)
        return task

    async def enrolled(self, actor="alice", name="workstation"):
        private = Ed25519PrivateKey.generate()
        public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        code = (await self.backend.create_pair_code(actor))["code"]
        device_id = (await self.backend.enroll(code, name, "linux", public))["device_id"]
        return device_id, private

    async def ticket(self, device_id, private):
        challenge = await self.backend.challenge(device_id)
        signature = base64.b64encode(private.sign(f"{challenge['challenge_id']}:{challenge['nonce']}".encode())).decode()
        return (await self.backend.ticket(device_id, challenge["challenge_id"], signature))["ticket"]

    async def connected(self, actor="alice", real_ws=False):
        device_id, private = await self.enrolled(actor)
        device = await self.backend.connect_ticket(await self.ticket(device_id, private))
        if real_ws:
            ws = MemoryWS()
            connection = DeviceConnection(self.backend, ws, device)
            self.connections.append(connection)
            self.job(connection.run())
            welcome = await ws.next()
            self.assertEqual(set(welcome), {"type", "protocol", "actor_id", "device_id", "connection_generation"})
            self.assertEqual(welcome["protocol"], 2)
            await ws.send(self.hello())
            for _ in range(200):
                if self.backend.online(await self.backend.json(self.backend.device_key(device_id))):
                    break
                await asyncio.sleep(0.001)
            return device_id, private, device, ws
        await self.backend.heartbeat(device_id, device["connection_generation"], self.hello())
        return device_id, private, device

    @staticmethod
    def hello():
        return {"type": "hello", "protocol": 2, "capabilities": sorted(p.METHODS), "version": "2.0", "platform": "linux"}

    @staticmethod
    def reply(envelope, status="ok", data=None, error=""):
        return {**{k: envelope[k] for k in p.BINDINGS}, "type": "result", "status": status, "data": data or {}, "error": error}

    async def prepared(self, method="system.info", params=None):
        did, private, device = await self.connected()
        task = await self.agent.start_task("alice", "s1", "t1", did)
        env = await self.agent.prepare("alice", did, "s1", "t1", method, params or {}, 30)
        self.assertTrue(await self.backend.claim_command(env))
        return did, private, device, task, env

    async def test_pair_atomic_once_public_key_and_ttl(self):
        private = Ed25519PrivateKey.generate()
        pub = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        code = await self.backend.create_pair_code("alice")
        self.assertEqual(code["expires_in"], 180)
        with self.assertRaises(ValueError):
            await self.backend.enroll(code["code"], "n", "linux", "invalid")
        outcomes = await asyncio.gather(*(self.backend.enroll(code["code"], "n", "linux", pub) for _ in range(8)), return_exceptions=True)
        self.assertEqual(sum(isinstance(x, dict) for x in outcomes), 1)
        expired = await self.backend.create_pair_code("alice")
        self.server.offset += p.PAIR_TTL + 1
        with self.assertRaises(ValueError):
            await self.backend.enroll(expired["code"], "n", "linux", pub)

    async def test_challenge_signature_consumption_and_expiry(self):
        did, private = await self.enrolled()
        challenge = await self.backend.challenge(did)
        invalid = base64.b64encode(b"x" * 64).decode()
        with self.assertRaises(ValueError):
            await self.backend.ticket(did, challenge["challenge_id"], invalid)
        signature = base64.b64encode(private.sign(f"{challenge['challenge_id']}:{challenge['nonce']}".encode())).decode()
        outcomes = await asyncio.gather(*(self.backend.ticket(did, challenge["challenge_id"], signature) for _ in range(6)), return_exceptions=True)
        self.assertEqual(sum(isinstance(x, dict) for x in outcomes), 1)
        fresh = await self.backend.challenge(did)
        self.server.offset += p.CHALLENGE_TTL + 1
        with self.assertRaises(ValueError):
            await self.backend.ticket(did, fresh["challenge_id"], invalid)

    async def test_ticket_single_use_expiry_and_revocation(self):
        did, private = await self.enrolled()
        ticket = await self.ticket(did, private)
        self.assertLessEqual(await self.r.ttl(self.backend.key("ticket", ticket)), 20)
        outcomes = await asyncio.gather(*(self.backend.connect_ticket(ticket) for _ in range(6)), return_exceptions=True)
        self.assertEqual(sum(isinstance(x, dict) for x in outcomes), 1)
        ticket = await self.ticket(did, private)
        self.server.offset += 21
        with self.assertRaises(ValueError):
            await self.backend.connect_ticket(ticket)
        ticket = await self.ticket(did, private)
        await self.backend.detach(did, "", revoked_by="alice")
        with self.assertRaises(ValueError):
            await self.backend.connect_ticket(ticket)
        with self.assertRaises(ValueError):
            await self.backend.challenge(did)

    async def test_no_default_selection_no_fallback_and_multi_device(self):
        a, _, da = await self.connected()
        with self.assertRaises(ValueError):
            await self.agent.start_task("alice", "s1", "t1")
        await self.backend.select("alice", a)
        t1 = await self.agent.start_task("alice", "s1", "t1")
        b, _, db = await self.connected()
        self.assertEqual((await self.backend.get_selected_device("alice"))["device_id"], a)
        await self.backend.select("alice", b)
        t2 = await self.agent.start_task("alice", "s2", "t2")
        self.assertEqual(t1["device_id"], a)
        self.assertEqual(t2["device_id"], b)
        self.assertTrue(await self.backend.heartbeat(a, da["connection_generation"]))
        with self.assertRaises(ValueError):
            await self.agent.start_task("alice", "s1", "t1", b)
        await self.backend.detach(b, db["connection_generation"])
        with self.assertRaises(ValueError):
            await self.agent.start_task("alice", "s1", "t3")
        self.assertEqual((await self.backend.get_task("alice", "t1"))["state"], "running")

    async def test_task_idempotency_serial_lease_and_parent_ownership(self):
        did, _, device = await self.connected()
        results = await asyncio.gather(*(self.agent.start_task("alice", "s1", "t1", did) for _ in range(6)))
        self.assertEqual(len({r["fencing_token"] for r in results}), 1)
        with self.assertRaises(ValueError):
            await self.agent.start_task("alice", "s2", "t2", did)
        with self.assertRaises(HTTPException):
            await self.agent.start_task("bob", "s1", "tb", did)
        self.server.offset += p.TASK_LEASE + 1
        self.assertEqual((await self.backend.get_task("alice", "t1"))["state"], "unknown")
        self.assertFalse(await self.backend.heartbeat(did, device["connection_generation"]))
        other, _, _ = await self.connected()
        await self.r.hset(k_task("parent"), mapping={"actor": "bob", "session_id": "sb", "status": "running"})
        with self.assertRaises(ValueError):
            await self.agent.start_task("alice", "s1", "parent", other)
        await self.r.hset(k_task("parent"), mapping={"actor": "alice", "session_id": "s1", "status": "running", "lease_owner": "coordinator"})
        task = await self.agent.start_task("alice", "s1", "parent", other)
        self.assertEqual(task["runtime_task_id"], "parent")
        await self.agent.finish_task("alice", "parent", "succeeded")
        self.assertEqual(await self.r.hget(k_task("parent"), "lease_owner"), "coordinator")

    async def test_progress_reports_step_and_pending_approval_only(self):
        did, _, _ = await self.connected()
        task_key = self.backend.task_key("alice", did, "s1", "t1")
        await self.agent.start_task("alice", "s1", "t1", did)
        view = await self.agent.progress("alice", "t1", step=3, approval=True)
        self.assertEqual((view["step"], view["approval"]), (3, True))
        status = await self.agent.get_task("alice", "t1")
        self.assertEqual((status["step"], status["approval"], status["state"]), (3, True, "running"))
        await self.agent.progress("alice", "t1", step=4, approval=False)
        status = await self.agent.get_task("alice", "t1")
        self.assertEqual((status["step"], status["approval"]), (4, False))
        with self.assertRaises(ValueError):
            await self.agent.progress("bob", "t1", step=9)
        events = [p.loads(x) for x in await self.r.lrange(task_key + ":events", 0, -1)]
        self.assertEqual([e["data"]["reason"] for e in events if e["type"] == "task_approval"],
                         ["approval_required", "approval_cleared"])
        self.assertNotIn("password", p.dumps(events))
        await self.agent.finish_task("alice", "t1", "succeeded")
        frozen = await self.agent.progress("alice", "t1", step=99, approval=True)
        self.assertEqual((frozen["step"], frozen["approval"]), (4, False))

    async def test_cross_instance_pubsub_command_result_and_idempotence(self):
        did, _, device, ws = await self.connected(real_ws=True)
        await self.agent.start_task("alice", "s1", "t1", did)
        pending = self.job(self.agent.send_command("alice", did, "s1", "t1", "system.info", {}, timeout=2))
        command = await ws.next()
        self.assertEqual(command["type"], "command")
        self.assertEqual(command["method"], "system.info")
        self.assertIs(type(command["seq"]), int)
        self.assertFalse(await self.backend.claim_command(command))
        await ws.send(self.reply(command, data={"os": "linux"}))
        self.assertEqual(await pending, {"status": "ok", "data": {"os": "linux"}, "error": ""})
        self.assertFalse(await self.backend.accept_result(did, device["connection_generation"], self.reply(command)))
        self.assertGreaterEqual(len(self.server.published), 2)

    async def test_every_binding_and_digest_fails_closed(self):
        did, _, device, task, env = await self.prepared()
        for field in p.BINDINGS:
            wrong = self.reply(env)
            wrong[field] = "forged"
            self.assertFalse(await self.backend.accept_result(did, device["connection_generation"], wrong), field)
        for field in ("seq", "fencing_token", "protocol"):
            wrong = self.reply(env)
            wrong[field] = True
            self.assertFalse(await self.backend.accept_result(did, device["connection_generation"], wrong))
        tampered = {**env, "params": {"shell": "bad"}}
        self.assertFalse(await self.backend.claim_command(tampered))
        self.assertTrue(await self.backend.accept_result(did, device["connection_generation"], self.reply(env)))

    async def test_reported_execution_mode_is_whitelisted_and_kept(self):
        did, _, device, _, env = await self.prepared()
        self.assertNotIn("execution_mode", await self.backend.get_task("alice", "t1"))
        # 缺失或非白名单时不推断、不透传任意字符串。
        for value in (None, "server_fallback", ["background"], 1):
            data = {"os": "linux"}
            if value is not None:
                data["execution_mode"] = value
            self.assertTrue(await self.backend.accept_result(
                did, device["connection_generation"], self.reply(env, data=data)))
            self.assertNotIn("execution_mode", await self.backend.get_task("alice", "t1"))
            env = await self.agent.prepare("alice", did, "s1", "t1", "system.info", {}, 30)
            self.assertTrue(await self.backend.claim_command(env))
        self.assertTrue(await self.backend.accept_result(
            did, device["connection_generation"],
            self.reply(env, data={"os": "linux", "execution_mode": "background"})))
        view = await self.backend.get_task("alice", "t1")
        self.assertEqual(view["execution_mode"], "background")

    async def test_reconnect_fences_old_generation_and_stale_disconnect(self):
        did, private, device, _, env = await self.prepared()
        new = await self.backend.connect_ticket(await self.ticket(did, private))
        await self.backend.heartbeat(did, new["connection_generation"], self.hello())
        await self.backend.detach(did, device["connection_generation"])
        self.assertTrue(self.backend.online(await self.backend.json(self.backend.device_key(did))))
        self.assertFalse(await self.backend.accept_result(did, device["connection_generation"], self.reply(env)))
        self.assertEqual((await self.backend.get_task("alice", "t1"))["state"], "unknown")
        task = await self.agent.start_task("alice", "s1", "t2", did)
        self.assertGreater(task["fencing_token"], env["fencing_token"])
        new_env = await self.agent.prepare("alice", did, "s1", "t2", "system.info", {}, 30)
        self.assertGreater(new_env["seq"], env["seq"])

    async def test_revoke_stops_socket_and_rejects_inflight_result(self):
        did, _, device, ws = await self.connected(real_ws=True)
        await self.backend.select("alice", did)
        await self.agent.start_task("alice", "s1", "t1", did)
        pending = self.job(self.agent.send_command("alice", did, "s1", "t1", "system.info", {}, 2))
        command = await ws.next()
        with self.assertRaises(ValueError):
            await self.backend.detach(did, "", revoked_by="bob")
        await self.backend.detach(did, "", revoked_by="alice")
        self.assertFalse(await self.backend.accept_result(did, device["connection_generation"], self.reply(command)))
        self.assertEqual((await pending)["status"], "unknown")
        self.assertEqual((await self.backend.get_task("alice", "t1"))["state"], "unknown")
        self.assertIsNone(await self.backend.get_selected_device("alice"))
        for _ in range(100):
            if ws.closed:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(ws.closed)

    async def test_timeout_unknown_never_resend_and_no_secret_audit(self):
        did, _, _, ws = await self.connected(real_ws=True)
        await self.agent.start_task("alice", "s1", "t1", did)
        job = self.job(self.agent.send_command("alice", did, "s1", "t1", "file.write_new", {"content": "TOP_SECRET", "path": "new.txt"}, .08))
        await ws.next()
        self.assertEqual((await job)["status"], "unknown")
        result = await self.agent.send_command("alice", did, "s1", "t1", "file.write_new", {"content": "TOP_SECRET"}, .05)
        self.assertEqual(result["status"], "rejected")
        commands = [p.loads(raw)["payload"] for _, raw in self.server.published if p.loads(raw)["payload"].get("type") == "command"]
        self.assertEqual(len(commands), 1)
        key, _ = await self.backend.task_record("alice", "t1")
        self.assertNotIn("TOP_SECRET", str(self.server.data))
        self.assertEqual(await self.r.ttl(key), -1)
        self.assertEqual(await self.r.ttl(key + ":events"), -1)

    async def test_control_priority_cancel_requires_matching_confirmation(self):
        did, _, _, ws = await self.connected(real_ws=True)
        await self.agent.start_task("alice", "s1", "t1", did)
        job = self.job(self.agent.send_command("alice", did, "s1", "t1", "system.info", {}, 2))
        command = await ws.next()
        cancel = self.job(self.agent.cancel_task("alice", "t1"))
        control = await ws.next()
        self.assertEqual(control["type"], "cancel")
        self.assertEqual(control["method"], "cancel")
        self.assertGreater(control["seq"], command["seq"])
        self.assertEqual((await self.backend.get_task("alice", "t1"))["state"], "cancel_requested")
        with self.assertRaises(ValueError):
            await self.agent.finish_task("alice", "t1", "cancelled")
        await ws.send(self.reply(control, "cancelled"))
        self.assertEqual((await cancel)["state"], "cancelled")
        self.assertEqual((await job)["status"], "cancelled")

    async def test_interrupted_command_does_not_hide_control_receipt(self):
        did, _, _, ws = await self.connected(real_ws=True)
        await self.agent.start_task("alice", "s1", "t1", did)
        job = self.job(self.agent.send_command("alice", did, "s1", "t1", "file.write_new",
                                               {"content": "synthetic", "path": "a.txt"}, 2))
        command = await ws.next()
        cancel = self.job(self.agent.cancel_task("alice", "t1"))
        control = await ws.next()
        # 写命令被中断时设备先回 unknown; 不能因此提前终结任务, 否则停止回执被丢弃。
        await ws.send(self.reply(command, "unknown", error="interrupted_verify_side_effects"))
        self.assertEqual((await self.backend.get_task("alice", "t1"))["state"], "cancel_requested")
        self.assertTrue(self.backend.online(await self.backend.json(self.backend.device_key(did))))
        await ws.send(self.reply(control, "cancelled"))
        self.assertEqual((await cancel)["state"], "cancelled")
        self.assertEqual((await job)["status"], "unknown")

    async def test_pause_interrupting_read_command_stays_paused_not_cancelled(self):
        did, _, _, ws = await self.connected(real_ws=True)
        await self.agent.start_task("alice", "s1", "t1", did)
        job = self.job(self.agent.send_command("alice", did, "s1", "t1", "system.info", {}, 2))
        command = await ws.next()
        pause = self.job(self.agent.control_task("alice", "t1", "pause"))
        control = await ws.next()
        # 只读命令被暂停打断会回 cancelled; 不能把整个任务当作已停止。
        await ws.send(self.reply(command, "cancelled"))
        self.assertEqual((await self.backend.get_task("alice", "t1"))["state"], "pause_requested")
        await ws.send(self.reply(control, "paused"))
        self.assertEqual((await pause)["state"], "paused")
        self.assertEqual((await job)["status"], "cancelled")

    async def test_paused_result_carries_static_pause_reason(self):
        did, _, device = await self.connected()
        await self.agent.start_task("alice", "s1", "t1", did)
        env = await self.agent.prepare("alice", did, "s1", "t1", "browser.goto",
                                     {"url": "https://example.test/login"}, 30)
        self.assertTrue(await self.backend.claim_command(env))
        self.assertTrue(await self.backend.accept_result(
            did, device["connection_generation"],
            self.reply(env, "paused", data={"pause_reason": "sensitive_or_login_page"})))
        view = await self.agent.get_task("alice", "t1")
        self.assertEqual(view["state"], "paused")
        self.assertEqual(view["reason"], "device_confirmed")
        self.assertEqual(view["pause_reason"], "sensitive_or_login_page")

    async def test_paused_result_drops_non_static_pause_reason(self):
        did, _, device = await self.connected()
        await self.agent.start_task("alice", "s1", "t1", did)
        env = await self.agent.prepare("alice", did, "s1", "t1", "browser.goto",
                                     {"url": "https://example.test/login"}, 30)
        self.assertTrue(await self.backend.claim_command(env))
        # 页面内容/参数/凭据不得借暂停原因进入任务视图或审计。
        self.assertTrue(await self.backend.accept_result(
            did, device["connection_generation"],
            self.reply(env, "paused", data={"pause_reason": "password=hunter2&token=xyz"})))
        view = await self.agent.get_task("alice", "t1")
        self.assertEqual(view["state"], "paused")
        self.assertNotIn("pause_reason", view)
        self.assertNotIn("hunter2", str(view))

    def test_pause_reason_accepts_only_static_codes(self):
        self.assertEqual(p.pause_reason("sensitive_or_login_page"), "sensitive_or_login_page")
        for bad in ("", None, 1, ["x"], "页面：请输入密码", "A" * 12, "x" * 65, "two words"):
            self.assertEqual(p.pause_reason(bad), "")

    async def test_pause_is_not_cancel_and_holds_device_lock(self):
        did, _, _, ws = await self.connected(real_ws=True)
        await self.agent.start_task("alice", "s1", "t1", did)
        control = self.job(self.agent.control_task("alice", "t1", "pause"))
        envelope = await ws.next()
        self.assertEqual(envelope["type"], "pause")
        await ws.send(self.reply(envelope, "paused"))
        self.assertEqual((await control)["state"], "paused")
        with self.assertRaises(ValueError):
            await self.agent.start_task("alice", "s2", "t2", did)
        self.assertEqual((await self.agent.send_command("alice", did, "s1", "t1", "system.info", {}))["status"], "rejected")

    async def test_rejected_pause_does_not_claim_remote_paused(self):
        did, _, device = await self.connected()
        await self.agent.start_task("alice", "s1", "t1", did)
        env = await self.agent.prepare("alice", did, "s1", "t1", "pause", {}, 30, "pause")
        self.assertTrue(await self.backend.claim_command(env))
        self.assertTrue(await self.backend.accept_result(did, device["connection_generation"], self.reply(env, "rejected")))
        self.assertNotEqual((await self.backend.get_task("alice", "t1"))["state"], "paused")

    async def test_screenshot_task_only_ttl_size_and_redaction(self):
        from PIL import Image
        image = io.BytesIO()
        Image.new("RGB", (8, 8)).save(image, format="JPEG")
        jpeg = base64.b64encode(image.getvalue()).decode()
        did, _, device, task, env = await self.prepared("browser.screenshot")
        self.assertTrue(await self.backend.accept_result(did, device["connection_generation"], self.reply(env, data={"jpeg": jpeg})))
        view = await self.backend.get_task("alice", "t1")
        self.assertEqual(view["frame"]["jpeg"], jpeg)
        self.assertEqual(view["frame"]["task_id"], "t1")
        self.assertEqual(view["frame"]["width"], 8)
        self.assertNotIn(jpeg, str(self.server.published))
        with self.assertRaises(ValueError):
            await self.backend.get_task("bob", "t1")
        other, _, _ = await self.connected()
        await self.agent.start_task("alice", "s2", "t2", other)
        self.assertNotIn("frame", await self.backend.get_task("alice", "t2"))
        self.server.offset += p.FRAME_TTL + 1
        self.assertNotIn("frame", await self.backend.get_task("alice", "t1"))
        with self.assertRaises(ValueError):
            p.frame_from({"jpeg": "A" * (p.MAX_MESSAGE + 1)})
        with self.assertRaises(ValueError):
            p.frame_from({"jpeg": base64.b64encode(b"not jpeg").decode()})

    async def test_desktop_screen_frame_only_for_authorized_desktop_commands(self):
        from PIL import Image
        image = io.BytesIO()
        Image.new("RGB", (8, 8)).save(image, format="JPEG")
        jpeg = base64.b64encode(image.getvalue()).decode()
        service = DeviceService(FakeRedis(self.server), prefix="isolated:cu2", clock=self.server.clock,
                                session_check=ensure_session_access,
                                ceiling=p.admin_ceiling(desktop=True))
        did, _, device = await self.connected()
        await service.start_task("alice", "s1", "t1", did)
        env = await service.prepare("alice", did, "s1", "t1", "desktop.screen", {}, 30)
        self.assertTrue(await service.claim_command(env))
        self.assertTrue(await service.accept_result(
            did, device["connection_generation"],
            self.reply(env, data={"jpeg": jpeg, "execution_mode": "desktop"})))
        view = await service.get_task("alice", "t1")
        self.assertEqual(view["frame"]["jpeg"], jpeg)
        self.assertEqual(view["execution_mode"], "desktop")

        # 非画面方法即使携带 jpeg 也不能写进任务画面缓存。
        env = await service.prepare("alice", did, "s1", "t1", "system.info", {}, 30)
        self.assertTrue(await service.claim_command(env))
        self.assertFalse(await service.accept_result(
            did, device["connection_generation"], self.reply(env, data={"jpeg": jpeg})))

    async def test_screenshot_evidence_needs_separate_optin_and_is_bounded(self):
        from PIL import Image
        image = io.BytesIO()
        Image.new("RGB", (8, 8)).save(image, format="JPEG")
        jpeg = base64.b64encode(image.getvalue()).decode()
        pref = f"{config.REDIS_PREFIX}:cu_pref:alice"
        owner_key = f"{config.REDIS_PREFIX}:cu:shot_owner"
        with tempfile.TemporaryDirectory() as tmp:
            service = DeviceService(FakeRedis(self.server), prefix="isolated:cu2", clock=self.server.clock,
                                    session_check=ensure_session_access, evidence_dir=tmp)
            did, _, device, _, env = await self.prepared("browser.screenshot")

            async def shot(envelope=None):
                if envelope is None:
                    envelope = await self.agent.prepare("alice", did, "s1", "t1", "browser.screenshot", {}, 30)
                    self.assertTrue(await service.claim_command(envelope))
                self.assertTrue(await service.accept_result(did, device["connection_generation"],
                                                            self.reply(envelope, data={"jpeg": jpeg})))

            folder = Path(tmp) / "computer_use"
            # 默认未授权: 结果照常返回, 但不落盘、不写归属、不暴露 evidence。
            await shot(env)
            self.assertFalse(folder.exists() and any(folder.iterdir()))
            self.assertNotIn("evidence", await service.get_task("alice", "t1"))
            self.assertEqual(await self.r.hgetall(owner_key), {})

            # 用户另行授权后才能落盘, 且归属到任务用户。
            await self.r.hset(pref, mapping={"persist_shots": "1"})
            await shot()
            files = sorted(folder.glob("*.jpg"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), image.getvalue())
            view = await service.get_task("alice", "t1")
            self.assertEqual(view["evidence"], [f"computer_use/{files[0].name}"])
            self.assertEqual(await self.r.hget(owner_key, view["evidence"][0]), "alice")
            self.assertEqual((await self.backend.get_task("alice", "t1"))["evidence"], view["evidence"])

            # 同一任务限频: 间隔内的后续帧不落盘。
            await shot()
            self.assertEqual(len(list(folder.glob("*.jpg"))), 1)

            # 超出上限后不再追加, 但历史证据不删除; 关闭授权也不删除。
            with patch.object(module, "EVIDENCE_MAX", 2):
                self.server.offset += module.EVIDENCE_INTERVAL_S + 1
                await shot()
                self.assertEqual(len(list(folder.glob("*.jpg"))), 2)
                self.server.offset += module.EVIDENCE_INTERVAL_S + 1
                await shot()
                self.assertEqual(len(list(folder.glob("*.jpg"))), 2)
            await self.r.hset(pref, mapping={"persist_shots": "0"})
            self.server.offset += module.EVIDENCE_INTERVAL_S + 1
            await shot()
            self.assertEqual(len(list(folder.glob("*.jpg"))), 2)

    async def test_hello_grants_summary_is_bounded_and_display_only(self):
        device_id, private = await self.enrolled()
        device = await self.backend.connect_ticket(await self.ticket(device_id, private))
        hello = self.hello()
        hello["grants"] = {"roots": [{"name": "Documents", "read": True, "write_new": False,
                                       "manage": False}],
                           "origins": ["https://example.invalid"], "scripts": ["disk_report"],
                           "native_origins": ["chrome-extension://" + "a" * 32 + "/"],
                           "apps": ["notepad.exe"]}
        self.assertTrue(await self.backend.heartbeat(device_id, device["connection_generation"], hello))
        grants = (await self.backend.list_devices("alice"))["devices"][0]["grants"]
        self.assertEqual(grants["roots"][0], {"name": "Documents", "read": True,
                                              "write_new": False, "manage": False})
        self.assertEqual(grants["scripts"], ["disk_report"])
        self.assertEqual(grants["apps"], ["notepad.exe"])
        # 2.0.1 及更早的 Companion 不带 apps: 仍然接受, 按未授权任何应用处理。
        self.assertEqual(p.grants({k: v for k, v in hello["grants"].items() if k != "apps"})["apps"], [])
        for bad in ({}, {"roots": "x"},
                    {"roots": [{"name": "x", "read": True, "write_new": False, "manage": 1}],
                     "origins": [], "scripts": [], "native_origins": []},
                    {"roots": [{"name": "x", "read": True, "write_new": False}],
                     "origins": [], "scripts": [], "native_origins": []},
                    {"roots": [], "origins": ["x" * 300], "scripts": [], "native_origins": []},
                    {"roots": [], "origins": [], "scripts": ["../evil"], "native_origins": []},
                    {"roots": [], "origins": [], "scripts": [], "native_origins": [],
                     "apps": ["Notepad.exe"]},
                    {"roots": [], "origins": [], "scripts": [], "native_origins": [],
                     "apps": [f"app{i}.exe" for i in range(17)]}):
            with self.assertRaises(ValueError):
                p.grants(bad)

    def test_admin_ceiling_desktop_requires_explicit_server_enable(self):
        caps = {"desktop.inspect", "desktop.request_control", "desktop.act", "desktop.screen"}
        params = {"desktop.inspect": {},
                  "desktop.request_control": {"window_id": 1, "scopes": ["click"], "reason": "x"},
                  "desktop.act": {"action": "click", "x": 10, "y": 10},
                  "desktop.screen": {}}
        for method in caps:
            self.assertFalse(p.allowed(method, params[method], caps))
            self.assertTrue(p.allowed(method, params[method], caps,
                                      p.admin_ceiling(desktop=True)))
        self.assertFalse(p.allowed("desktop.act", {"approval": True}, caps,
                                   p.admin_ceiling(desktop=True)))
        self.assertFalse(p.allowed("desktop.request_control",
                                   {"window_id": 1, "scopes": ["shell"], "reason": "x"},
                                   caps, p.admin_ceiling(desktop=True)))
        # UIA 元素级动作: 允许 invoke 租约, 但参数必须完整且限定在可激活控件类型内。
        ceiling = p.admin_ceiling(desktop=True)
        self.assertTrue(p.allowed("desktop.request_control",
                                  {"window_id": 1, "scopes": ["invoke", "keys"], "reason": "x"},
                                  caps, ceiling))
        # view 是采集真实桌面画面的显式授权范围; 画面命令本身只接受空参数。
        self.assertTrue(p.allowed("desktop.request_control",
                                  {"window_id": 1, "scopes": ["view"], "reason": "x"},
                                  caps, ceiling))
        self.assertTrue(p.allowed("desktop.screen", {}, caps, ceiling))
        self.assertFalse(p.allowed("desktop.screen", {"window_id": 1}, caps, ceiling))
        self.assertTrue(p.allowed("desktop.inspect", {"window_id": 101}, caps, ceiling))
        self.assertTrue(p.allowed("desktop.act",
                                  {"action": "invoke", "element": 0, "role": "button", "name": "保存"},
                                  caps, ceiling))
        for bad in ({"action": "invoke", "element": 0, "role": "button"},
                    {"action": "invoke", "element": 80, "role": "button", "name": "x"},
                    {"action": "invoke", "element": 0, "role": "edit", "name": "x"},
                    {"action": "invoke", "element": "0", "role": "button", "name": "x"},
                    {"action": "invoke", "element": 0, "role": "button", "name": "a\nb"},
                    {"action": "invoke", "element": 0, "role": "button", "name": "x",
                     "x": 10}):
            self.assertFalse(p.allowed("desktop.act", bad, caps, ceiling), bad)
        self.assertNotIn("desktop.act", p.admin_ceiling())
        self.assertIn("desktop.act", p.admin_ceiling(desktop=True))
        self.assertNotIn("script.run", p.admin_ceiling(desktop=True))
        self.assertNotIn("script.run", p.admin_ceiling())
        self.assertIn("script.run", p.admin_ceiling(scripts=True))
        self.assertIn("service.list", p.admin_ceiling())
        self.assertFalse(p.allowed("service.list", {"name": "sshd"}, {"service.list"}))
        self.assertTrue(p.allowed("service.list", {}, {"service.list"}))

    async def test_unknown_method_shell_approval_and_capabilities(self):
        did, _, _ = await self.connected()
        await self.agent.start_task("alice", "s1", "t1", did)
        for method, params in [("shell", {}), ("script.run", {"shell": "id"}), ("desktop.act", {"approval": True}), ("system.info", {"nested": {"cmd": "id"}}), ("browser.goto", {"url": "javascript:alert(1)"}), ("browser.act", {"action": "press", "key": "CTRL+L"}), ("browser.download", {"ref": "a" * 24, "path": "C:/x"}), ("browser.download", {"ref": "a" * 24, "name": "../x"}), ("log.collect", {"path": "x", "max_bytes": 10}), ("log.collect", {"path": "x", "tail": "yes"}), ("log.collect", {"path": "x", "command": "cat"})]:
            answer = await self.agent.send_command("alice", did, "s1", "t1", method, params)
            self.assertEqual(answer["status"], "rejected", method)
        key = self.backend.device_key(did)
        record = await self.backend.json(key)
        record["capabilities"] = []
        await self.r.set(key, p.dumps(record))
        answer = await self.agent.send_command("alice", did, "s1", "t1", "system.info", {})
        self.assertEqual(answer["status"], "rejected")
        # 能力不足单独区分, 界面据此提示而不是误报设备离线。
        self.assertEqual(answer["error"], "capability_unavailable")

    async def test_forged_bus_and_response_not_trusted(self):
        did, _, _, ws = await self.connected(real_ws=True)
        await self.agent.start_task("alice", "s1", "t1", did)
        job = self.job(self.agent.send_command("alice", did, "s1", "t1", "system.info", {}, .1))
        envelope = await ws.next()
        channel = self.backend.response_channel(envelope["command_id"])
        forged = {"envelope": {k: envelope[k] for k in p.BINDINGS}, "result": p.result("ok", {"forged": True})}
        await self.r.publish(channel, p.signed(b"wrong-key", forged))
        await self.backend.publish(channel, forged)
        self.assertEqual((await job)["status"], "unknown")
        with self.assertRaises(ValueError):
            p.verified(await self.backend.secret(), p.signed(b"wrong", forged))

    async def test_slow_writer_timeout_detaches_without_blocking_reader(self):
        did, _, device = await self.connected()
        ws = MemoryWS()
        connection = DeviceConnection(self.backend, ws, device)
        # 单测写协程：慢 send 有硬上限，接收/控制协程不共享该 await。
        async def slow_send(_):
            await asyncio.sleep(10)

        ws.send_text = slow_send
        connection.enqueue({"type": "pong", "protocol": 2}, 0)
        with patch.object(p, "SEND_TIMEOUT", .02):
            with self.assertRaises(asyncio.TimeoutError):
                await connection.write()

    async def test_unsent_replay_lower_sequence_and_revocation_claim(self):
        did, _, device = await self.connected()
        await self.agent.start_task("alice", "s1", "t1", did)
        command = await self.agent.prepare("alice", did, "s1", "t1", "system.info", {}, 30)
        control = await self.agent.prepare("alice", did, "s1", "t1", "pause", {}, 30, "pause")
        self.assertTrue(await self.backend.claim_command(control))
        self.assertFalse(await self.backend.claim_command(command))
        self.assertFalse(await self.backend.claim_command(control))
        await self.backend.detach(did, "", revoked_by="alice")
        self.assertFalse(await self.backend.claim_command(command))
        self.assertFalse(await self.backend.accept_result(did, device["connection_generation"], self.reply(control, "paused")))

    async def test_command_expiry_and_invalid_size_no_execution(self):
        did, _, device, _, env = await self.prepared()
        self.server.offset += 31
        self.assertFalse(await self.backend.accept_result(did, device["connection_generation"], self.reply(env)))
        self.assertEqual((await self.backend.expire_command(env))["status"], "unknown")
        other, _, _ = await self.connected()
        await self.agent.start_task("alice", "s2", "t2", other)
        for value in (0, -1, float("nan"), float("inf"), 121, True):
            self.assertEqual((await self.agent.send_command("alice", other, "s2", "t2", "system.info", {}, value))["status"], "rejected")
        self.assertEqual((await self.agent.send_command("alice", other, "s2", "t2", "file.write_new", {"content": "x" * (p.MAX_PARAMS + 1)}))["status"], "rejected")
        self.assertFalse(await self.backend.accept_result(did, device["connection_generation"], {"type": "result", "status": []}))

    async def test_ws_large_message_disconnects_and_task_unknown(self):
        did, _, _, ws = await self.connected(real_ws=True)
        await self.agent.start_task("alice", "s1", "t1", did)
        await ws.incoming.put("x" * (p.MAX_MESSAGE + 1))
        for _ in range(100):
            if ws.closed:
                break
            await asyncio.sleep(.01)
        self.assertTrue(ws.closed)
        self.assertEqual((await self.backend.get_task("alice", "t1"))["state"], "unknown")

    async def test_paused_lease_expiry_and_terminal_audit_survive(self):
        did, _, device = await self.connected()
        await self.agent.start_task("alice", "s1", "t1", did)
        env = await self.agent.prepare("alice", did, "s1", "t1", "pause", {}, 30, "pause")
        await self.backend.claim_command(env)
        await self.backend.accept_result(did, device["connection_generation"], self.reply(env, "paused"))
        self.server.offset += p.TASK_LEASE + 1
        self.assertEqual((await self.backend.get_task("alice", "t1"))["state"], "unknown")
        key, _ = await self.backend.task_record("alice", "t1")
        events = [p.loads(e) for e in await self.r.lrange(key + ":events", 0, -1)]
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertIn(env["command_id"], str(events))
        self.server.offset += 365 * 86400
        self.assertEqual(await self.r.ttl(key + ":events"), -1)
        self.assertEqual((await self.backend.get_task("alice", "t1"))["state"], "unknown")

    def test_access_log_ticket_is_redacted(self):
        from core.cu.logging import TicketRedaction
        record = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, '%s - "WebSocket %s" [accepted]', ("client", "/api/cu/devices/connect?ticket=VERY_SECRET"), None)
        self.assertTrue(TicketRedaction().filter(record))
        self.assertNotIn("VERY_SECRET", record.getMessage())
        self.assertIn("[redacted]", record.getMessage())
        from uvicorn.logging import AccessFormatter
        access = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("client", "GET", "/api/cu/devices/connect?ticket=VERY_SECRET", "1.1", 101), None)
        TicketRedaction().filter(access)
        self.assertNotIn("VERY_SECRET", AccessFormatter().format(access))
        self.assertEqual(len(access.args), 5)

    async def test_late_old_result_does_not_disconnect_next_task(self):
        did, _, _, ws = await self.connected(real_ws=True)
        await self.agent.start_task("alice", "s1", "t1", did)
        first = self.job(self.agent.send_command("alice", did, "s1", "t1", "system.info", {}, 2))
        command = await ws.next()
        await ws.send(self.reply(command))
        self.assertEqual((await first)["status"], "ok")
        await self.agent.finish_task("alice", "t1", "succeeded")
        await self.agent.start_task("alice", "s2", "t2", did)
        await ws.send(self.reply(command))
        await ws.send({"type": "ping"})
        self.assertEqual((await ws.next())["type"], "pong")
        self.assertFalse(ws.closed)
        self.assertEqual((await self.backend.get_task("alice", "t2"))["state"], "running")

    async def test_ticket_generation_invalidates_sibling_credentials(self):
        did, private = await self.enrolled()
        one, two = await self.ticket(did, private), await self.ticket(did, private)
        challenge = await self.backend.challenge(did)
        await self.backend.connect_ticket(one)
        with self.assertRaises(ValueError):
            await self.backend.connect_ticket(two)
        signature = base64.b64encode(private.sign(f"{challenge['challenge_id']}:{challenge['nonce']}".encode())).decode()
        with self.assertRaises(ValueError):
            await self.backend.ticket(did, challenge["challenge_id"], signature)

    async def test_cross_actor_identical_task_ids_remain_isolated(self):
        a, _, _ = await self.connected("alice")
        b, _, _ = await self.connected("bob")
        await self.agent.start_task("alice", "s1", "t1", a)
        await self.agent.start_task("bob", "sb", "t1", b)
        self.assertEqual((await self.backend.get_task("alice", "t1"))["device_id"], a)
        self.assertEqual((await self.backend.get_task("bob", "t1"))["device_id"], b)
        self.assertEqual((await self.agent.send_command("alice", b, "s1", "t1", "system.info", {}))["status"], "rejected")

    async def test_http_size_and_anonymous_rate_before_body(self):
        app = self.make_app()
        with patch.object(api, "service", AsyncMock(return_value=self.backend)), patch.object(api, "verify_token", self.fake_auth):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                response = await client.put("/api/cu/devices/selection", content="x" * (p.MAX_HTTP + 1), headers={"Authorization": "Bearer alice", "Content-Type": "application/json"})
                self.assertEqual(response.status_code, 413)
                for _ in range(20):
                    await self.backend.rate_limit("enroll", "127.0.0.1", 20)

                async def unread():
                    raise AssertionError("限流后仍读取请求体")
                    yield b"unreachable"

                response = await client.post("/api/cu/devices/enroll", content=unread(), headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 429)

    def make_app(self):
        app = FastAPI()
        app.include_router(api.router)
        return app

    async def fake_auth(self, request):
        actor = request.headers.get("authorization", "").removeprefix("Bearer ")
        if actor not in {"alice", "bob"}:
            raise HTTPException(401, "认证失败")
        return actor

    async def test_http_contract_permissions_and_no_body_before_auth(self):
        did, _, _ = await self.connected()
        await self.agent.start_task("alice", "s1", "t1", did)
        app = self.make_app()
        with patch.object(api, "service", AsyncMock(return_value=self.backend)), patch.object(api, "verify_token", self.fake_auth):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                response = await client.get("/api/cu/devices", headers={"Authorization": "Bearer alice"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(set(response.json()), {"devices", "selected_device_id"})
                self.assertEqual(set(response.json()["devices"][0]), {"device_id", "name", "platform", "online", "version", "capabilities", "revoked"})
                response = await client.put("/api/cu/devices/selection", json={"device_id": did}, headers={"Authorization": "Bearer alice"})
                self.assertEqual(response.json(), {"selected_device_id": did})
                response = await client.get("/api/cu/tasks?session_id=s1", headers={"Authorization": "Bearer alice"})
                self.assertEqual(response.json()["tasks"][0]["task_id"], "t1")
                for path in ("/api/cu/tasks?session_id=s1", "/api/cu/tasks/t1/status"):
                    response = await client.get(path, headers={"Authorization": "Bearer bob"})
                    self.assertIn(response.status_code, (403, 404))

                async def unread_body():
                    raise AssertionError("未授权时读取了 body")
                    yield b"unreachable"

                response = await client.put("/api/cu/devices/selection", content=unread_body(), headers={"Content-Type": "application/json", "Content-Length": "999999"})
                self.assertEqual(response.status_code, 401)
                response = await client.post("/api/cu/tasks/t1/control", content=unread_body(), headers={"Authorization": "Bearer bob", "Content-Type": "application/json"})
                self.assertEqual(response.status_code, 404)
                await self.r.hset(config.key_session_meta("s1"), mapping={"username": "bob"})
                response = await client.get("/api/cu/tasks/t1/status", headers={"Authorization": "Bearer alice"})
                self.assertEqual(response.status_code, 403)

    async def test_http_enrollment_contract_limits_and_tls_origin(self):
        app = self.make_app()
        with patch.object(api, "service", AsyncMock(return_value=self.backend)), patch.object(api, "verify_token", self.fake_auth):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                pair = await client.post("/api/cu/devices/pair-code", headers={"Authorization": "Bearer alice"})
                self.assertEqual(set(pair.json()), {"code", "expires_in"})
                private = Ed25519PrivateKey.generate()
                public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
                enrolled = await client.post("/api/cu/devices/enroll", json={"code": pair.json()["code"], "name": "linux", "platform": "linux", "public_key": public})
                self.assertEqual(set(enrolled.json()), {"device_id"})
                did = enrolled.json()["device_id"]
                challenge = (await client.post("/api/cu/devices/challenge", json={"device_id": did})).json()
                signature = base64.b64encode(private.sign(f"{challenge['challenge_id']}:{challenge['nonce']}".encode())).decode()
                ticket = await client.post("/api/cu/devices/ticket", json={"device_id": did, "challenge_id": challenge["challenge_id"], "signature": signature})
                self.assertEqual(set(ticket.json()), {"ticket"})
                for _ in range(20):
                    response = await client.post("/api/cu/devices/enroll", json={})
                self.assertEqual(response.status_code, 429)
                response = await client.get("/api/cu/devices", headers={"Authorization": "Bearer alice", "Origin": "https://evil.invalid"})
                self.assertEqual(response.status_code, 403)
            remote = httpx.ASGITransport(app=app, client=("192.0.2.1", 1000))
            async with httpx.AsyncClient(transport=remote, base_url="http://service.invalid") as client:
                self.assertEqual((await client.post("/api/cu/devices/challenge", json={})).status_code, 403)
        self.assertFalse(p.transport_ok({"scheme": "wss", "client": ("192.0.2.1", 1)}, {}, (), True))
        self.assertTrue(p.transport_ok({"scheme": "wss", "client": ("192.0.2.1", 1)}, {"origin": "https://trusted.invalid"}, ("https://trusted.invalid",), True))

    async def test_actual_ws_route_short_ticket_only_and_legacy_rejection(self):
        app = self.make_app()
        did, private = await self.enrolled()
        short = await self.ticket(did, private)

        async def open_ws(query):
            incoming, outgoing = asyncio.Queue(), asyncio.Queue()
            await incoming.put({"type": "websocket.connect"})
            scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": "/api/cu/devices/connect", "raw_path": b"/api/cu/devices/connect", "query_string": query.encode(), "headers": [], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80), "root_path": "", "subprotocols": []}
            job = self.job(app(scope, incoming.get, outgoing.put))
            return incoming, outgoing, job

        with patch.object(api, "service", AsyncMock(return_value=self.backend)):
            _, output, job = await open_ws("token=old-user-token")
            self.assertEqual((await asyncio.wait_for(output.get(), 1))["type"], "websocket.close")
            await job
            incoming, output, job = await open_ws("ticket=" + short)
            self.assertEqual((await asyncio.wait_for(output.get(), 1))["type"], "websocket.accept")
            welcome = p.loads((await asyncio.wait_for(output.get(), 1))["text"])
            self.assertEqual(welcome["device_id"], did)
            await incoming.put({"type": "websocket.receive", "text": p.dumps({"type": "hello", "client": "old-extension"})})
            self.assertEqual((await asyncio.wait_for(output.get(), 1))["type"], "websocket.close")
            await job

    async def test_ws_route_accepts_native_client_over_tls_and_rejects_bad_origin(self):
        app = self.make_app()
        did, private = await self.enrolled()

        async def open_ws(query, client, scheme, headers):
            incoming, outgoing = asyncio.Queue(), asyncio.Queue()
            await incoming.put({"type": "websocket.connect"})
            scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": scheme,
                     "path": "/api/cu/devices/connect", "raw_path": b"/api/cu/devices/connect",
                     "query_string": query.encode(), "headers": headers, "client": client,
                     "server": ("127.0.0.1", 80), "root_path": "", "subprotocols": []}
            return incoming, outgoing, self.job(app(scope, incoming.get, outgoing.put))

        with patch.object(api, "service", AsyncMock(return_value=self.backend)):
            # 真实网络经 nginx 转发后客户端 IP 不是 127.0.0.1; Companion 不是浏览器, 不带 Origin。
            # 鉴权完全由一次性票据 + 设备签名承担, 不得因缺 Origin 拒绝。
            incoming, output, job = await open_ws("ticket=" + await self.ticket(did, private),
                                                 ("192.0.2.1", 1000), "wss", [])
            self.assertEqual((await asyncio.wait_for(output.get(), 1))["type"], "websocket.accept")
            await incoming.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(job, 2)
            # 自带非法 Origin 的浏览器请求仍按白名单拒绝（未配置任何允许来源）。
            _, output, job = await open_ws("ticket=" + await self.ticket(did, private),
                                           ("192.0.2.1", 1000), "wss",
                                           [(b"origin", b"https://evil.invalid")])
            self.assertEqual((await asyncio.wait_for(output.get(), 1))["type"], "websocket.close")
            await asyncio.wait_for(job, 2)

    async def test_module_agent_facade_uses_injected_service(self):
        did, _, _ = await self.connected()
        await self.backend.select("alice", did)
        with patch.object(module, "_service", self.agent):
            self.assertEqual((await module.get_selected_device("alice"))["device_id"], did)
            self.assertEqual((await module.start_task("alice", "s1", "t1"))["task_id"], "t1")
            self.assertEqual((await module.send_command("alice", did, "s1", "t1", "shell", {}))["status"], "rejected")
            self.assertEqual((await module.finish_task("alice", "t1", "succeeded", "secret value"))["state"], "succeeded")
        self.assertNotIn("secret value", str(self.server.data))


if __name__ == "__main__":
    unittest.main(verbosity=2)
