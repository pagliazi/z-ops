"""隔离协议、防重、真实 TLS/WSS 测试；所有身份均为合成。"""
import asyncio
import base64
import datetime
import json
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
from cryptography.x509.oid import NameOID
from websockets.asyncio.server import serve
from websockets.asyncio.client import connect
from companion.config import Config
from companion.client import enroll, ticket, session
from companion.engine import Engine
from companion.executor import execution_mode
from companion.identity import Identity
from companion.protocol import BINDINGS, Guard, Journal
from companion.security import Denied, Paused, decode, encode


def command(**overrides):
    return {"type": "command", "protocol": 2, "actor_id": "actor", "device_id": "device",
            "session_id": "session", "task_id": "task", "command_id": "cmd1", "seq": 1,
            "expiry": time.time() + 60, "connection_generation": "generation-1", "fencing_token": 1,
            "method": "system.info", "params": {}, **overrides}


def welcome(gen="generation-1"):
    return {"type": "welcome", "protocol": 2, "actor_id": "actor", "device_id": "device",
            "connection_generation": gen}


class Executor:
    capabilities = ["system.info", "file.write_new"]
    def __init__(self):
        self.calls = 0
        self.stopped = 0
        self.block = False
        self.pause = False
        self.started = asyncio.Event()

    async def execute(self, cmd):
        self.calls += 1
        self.started.set()
        if self.pause:
            raise Paused("sensitive_or_login_page")
        if self.block:
            await asyncio.sleep(60)
        return {"synthetic": True}

    async def stop(self):
        self.stopped += 1


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config("https://example.invalid", "test", device_id="device", key_id="key", path=self.root / "config.json")
        self.cfg.save()
        self.journal = Journal(self.cfg.state / "commands.sqlite")
        self.guard = Guard(self.cfg, self.journal)
        self.guard.bind(welcome())
        self.executor = Executor()
        self.engine = Engine(self.guard, self.executor)

    async def asyncTearDown(self):
        await self.engine.close()
        self.journal.close()
        self.tmp.cleanup()

    async def reply(self):
        return decode(await asyncio.wait_for(self.engine.outgoing.get(), 3), 786432)

    async def test_repair_new_device_resets_connection_meta(self):
        """吊销后重新配对会换设备号: 不能因旧 journal 身份与 fencing 永久连不上。"""
        self.journal.set("fence", 7)
        with self.journal.db:
            self.journal.db.execute("INSERT INTO tasks VALUES ('old-task','s',7,1)")
        self.cfg.device_id = "device2"
        self.cfg.save()
        guard = Guard(self.cfg, self.journal)
        guard.bind({"type": "welcome", "protocol": 2, "actor_id": "actor",
                    "device_id": "device2", "connection_generation": "generation-2"})
        self.assertEqual(guard.welcome["device_id"], "device2")
        self.assertIsNone(self.journal.get("fence"))
        self.assertEqual(self.journal.get("generation"), "generation-2")
        # 旧设备号的停止标记不得阻断新设备的任务。
        self.assertIsNone(self.journal.db.execute("SELECT id FROM tasks WHERE id='old-task'").fetchone())
        # 旧代际标记必须清空, 否则新设备首次连接就会被当作重放。
        self.assertTrue(self.journal.get("generation_seen:generation-2"))
        self.assertIsNone(self.journal.get("generation_seen:generation-1"))

    async def test_success_mirror_and_lost_receipt_no_reexecution(self):
        c = command()
        await self.engine.receive(c)
        r = await self.reply()
        self.assertEqual(r["status"], "ok")
        # 执行方式由本机执行器上报, 只允许白名单值。
        self.assertEqual(r["data"]["execution_mode"], "background")
        for key in BINDINGS:
            self.assertEqual(r[key], c[key])
        await self.engine.receive(c)
        r = await self.reply()
        self.assertEqual(r["status"], "unknown")
        self.assertTrue(r["data"]["replayed"])
        self.assertEqual(self.executor.calls, 1)
        await self.engine.receive(command(params={"changed": True}))
        self.assertEqual((await self.reply())["status"], "rejected")

    async def test_binding_fence_expiry_approval_and_sequence(self):
        invalid = [{"actor_id": "other"}, {"device_id": "other"}, {"protocol": True},
                   {"connection_generation": "old"}, {"expiry": time.time() - 1},
                   {"expiry": float("inf")}, {"fencing_token": True}, {"seq": -1},
                   {"approval": "permanent"}]
        for value in invalid:
            await self.engine.receive(command(**value))
            self.assertEqual((await self.reply())["status"], "rejected", value)
        self.assertEqual(self.executor.calls, 0)
        await self.engine.receive(command())
        await self.reply()
        for value in ({"session_id": "other", "seq": 2}, {"task_id": "other", "seq": 2},
                      {"seq": 1}, {"fencing_token": 0, "seq": 2}):
            await self.engine.receive(command(command_id="cmd2", **value))
            self.assertEqual((await self.reply())["status"], "rejected")
        self.assertEqual(self.executor.calls, 1)

    async def test_pause_reports_reason_and_task_still_stoppable(self):
        """暂停后必须仍能停止, 且暂停原因只上报静态码。"""
        self.executor.pause = True
        await self.engine.receive(command(method="browser.goto", params={"url": "http://127.0.0.1:8099/"}))
        r = await self.reply()
        self.assertEqual(r["status"], "paused")
        self.assertEqual(r["data"]["pause_reason"], "sensitive_or_login_page")
        self.assertGreater(self.executor.stopped, 0)
        await self.engine.receive(command(type="cancel", command_id="cancel-after-pause", seq=2))
        r = await self.reply()
        self.assertEqual(r["status"], "cancelled")

    async def test_execution_mode_only_for_known_backends(self):
        self.assertEqual(execution_mode("browser.goto"), "background")
        self.assertEqual(execution_mode("file.read"), "background")
        self.assertEqual(execution_mode("desktop.act"), "desktop")
        self.assertEqual(execution_mode("server.shell"), "")

    async def test_cancel_priority_unknown_write_and_pause_terminal(self):
        self.executor.block = True
        await self.engine.receive(command(method="file.write_new"))
        await self.executor.started.wait()
        await asyncio.wait_for(self.engine.receive(command(type="cancel", command_id="cancel1", seq=2)), 1)
        replies = [await self.reply(), await self.reply()]
        self.assertEqual({r["status"] for r in replies}, {"unknown", "cancelled"})
        self.assertGreater(self.executor.stopped, 0)
        await self.engine.receive(command(command_id="cmd3", seq=3))
        self.assertEqual((await self.reply())["status"], "rejected")

    async def test_busy_has_no_pending_execution_and_fence_preempts(self):
        self.executor.block = True
        await self.engine.receive(command())
        await self.executor.started.wait()
        await self.engine.receive(command(command_id="cmd2", seq=2))
        self.assertEqual((await self.reply())["error"], "device_busy_no_queue")
        self.executor.block = False
        await self.engine.receive(command(task_id="task2", command_id="cmd3", seq=3, fencing_token=2))
        statuses = {(await self.reply())["status"], (await self.reply())["status"]}
        self.assertEqual(statuses, {"cancelled", "ok"})
        self.assertEqual(self.executor.calls, 2)

    async def test_crash_unknown_and_generation_reset(self):
        c = command(method="file.write_new")
        self.guard.admit(c)
        self.journal.close()
        self.journal = Journal(self.cfg.state / "commands.sqlite")
        guard = Guard(self.cfg, self.journal)
        guard.bind(welcome("generation-2"))
        self.engine.guard = guard
        replay, _ = guard.admit({**c, "connection_generation": "generation-2"})
        self.assertEqual(replay["status"], "unknown")
        with self.assertRaises(Denied):
            guard.admit(command(command_id="cmd2", seq=2, connection_generation="generation-2"))
        with self.assertRaises(Denied):
            guard.bind(welcome("generation-2"))

    def tls_contexts(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1))
                .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                .sign(key, hashes.SHA256()))
        pem, keyfile = self.root / "cert.pem", self.root / "test-key.pem"
        pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ssl.load_cert_chain(pem, keyfile)
        client_ssl = ssl.create_default_context(cafile=str(pem))
        return server_ssl, client_ssl

    async def test_tls_websocket_welcome_hello_and_disconnect_stop(self):
        server_ssl, client_ssl = self.tls_contexts()
        got = []
        async def handler(ws):
            await ws.send(encode(welcome("generation-wss")).decode())
            got.append(decode(await ws.recv()))
            await ws.send(encode(command(connection_generation="generation-wss")).decode())
            got.append(decode(await ws.recv()))
            await ws.send('{"type":"ping","protocol":2}')
            got.append(decode(await ws.recv()))
            await ws.close()
        async with serve(handler, "127.0.0.1", 0, ssl=server_ssl) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"wss://localhost:{port}", ssl=client_ssl, proxy=None) as ws:
                await session(ws, self.cfg, self.journal, self.executor)
        self.assertEqual(got[0]["type"], "hello")
        self.assertEqual(got[0]["protocol"], 2)
        self.assertEqual(got[1]["status"], "ok")
        self.assertEqual(got[2]["type"], "pong")
        self.assertGreater(self.executor.stopped, 0)

    async def test_actual_https_contract_and_redirect_denied(self):
        from aiohttp import web
        from companion.client import HTTPS
        server_ssl, client_ssl = self.tls_contexts()
        key = ed25519.Ed25519PrivateKey.generate()
        identity = object.__new__(Identity)
        identity.key = lambda create=False: key
        calls = []
        async def handler(request):
            body = await request.json()
            calls.append((request.path, body))
            if request.path.endswith("/enroll"):
                self.assertEqual(set(body), {"code", "name", "platform", "public_key"})
                self.assertEqual(len(base64.b64decode(body["public_key"])), 32)
                return web.json_response({"device_id": "device"})
            if request.path.endswith("/challenge"):
                self.assertEqual(body, {"device_id": "device"})
                return web.json_response({"challenge_id": "cid", "nonce": "nonce"})
            if request.path.endswith("/ticket"):
                key.public_key().verify(base64.b64decode(body["signature"]), b"cid:nonce")
                return web.json_response({"ticket": "synthetic-ticket"})
            raise web.HTTPFound("https://example.invalid/never-follow")
        app = web.Application()
        app.router.add_post("/api/cu/devices/{endpoint}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl)
        await site.start()
        try:
            port = site._server.sockets[0].getsockname()[1]
            transport = HTTPS(f"https://localhost:{port}", context=client_ssl)
            self.cfg.device_id = ""
            await enroll(self.cfg, "LOCALCODE", transport, identity)
            self.assertEqual(await ticket(self.cfg, transport, identity), "synthetic-ticket")
            self.assertEqual([p for p, _ in calls], ["/api/cu/devices/enroll", "/api/cu/devices/challenge", "/api/cu/devices/ticket"])
            with self.assertRaises(Denied):
                await transport.post("redirect", {})
        finally:
            await runner.cleanup()

    async def test_queue_overflow_stops_instead_of_buffering_commands(self):
        self.executor.block = True
        c = command()
        await self.engine.receive(c)
        await self.executor.started.wait()
        for _ in range(9):
            await self.engine.receive(c)
        self.assertTrue(self.engine.failed.is_set())
        await self.engine.close()
        self.assertEqual(self.executor.calls, 1)
        self.assertTrue(self.engine.outgoing.empty())

    async def test_enrollment_contract_real_ed25519_synthetic_keyring(self):
        class MemoryBackend:
            def __init__(self):
                self.values = {}
            def get_password(self, service, key):
                return self.values.get((service, key))
            def set_password(self, service, key, value):
                self.values[service, key] = value
        identity = object.__new__(Identity)
        identity.backend = MemoryBackend()
        identity.service = "test-only"
        self.cfg.device_id = ""
        identity.config = self.cfg
        calls = []
        class Transport:
            async def post(_, endpoint, data):
                calls.append((endpoint, data))
                if endpoint == "enroll":
                    public = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(data["public_key"]))
                    identity.test_public = public
                    return {"device_id": "device"}
                if endpoint == "challenge":
                    return {"challenge_id": "cid", "nonce": "synthetic-nonce"}
                identity.test_public.verify(base64.b64decode(data["signature"]), b"cid:synthetic-nonce")
                return {"ticket": "synthetic-ticket"}
        transport = Transport()
        await enroll(self.cfg, "TESTCODE", transport, identity)
        self.assertEqual(await ticket(self.cfg, transport, identity), "synthetic-ticket")
        self.assertEqual(set(calls[0][1]), {"code", "name", "platform", "public_key"})
        self.assertEqual(set(calls[2][1]), {"device_id", "challenge_id", "signature"})
        config_raw = self.cfg.path.read_text()
        for secret in ("TESTCODE", "synthetic-ticket", next(iter(identity.backend.values.values()))):
            self.assertNotIn(secret, config_raw)
        with patch("keyring.get_keyring", return_value=MemoryBackend()):
            with self.assertRaises(Denied):
                Identity(self.cfg)


if __name__ == "__main__":
    unittest.main()
