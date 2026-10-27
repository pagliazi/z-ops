"""仅本地合成 HTML。root 测试专用无沙箱启动，不改变生产安全选项。"""
import asyncio
import base64
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from aiohttp import web
from playwright.async_api import async_playwright
from companion.browser import Browser
from companion.config import Config
from companion.desktop import Desktop
from companion.native_host import read_frame, write_frame, login_handoff
from companion.security import Denied, Paused, decode, encode


class SyntheticBrowser(Browser):
    async def prepare(self):
        if self.browser:
            return
        self.playwright = await async_playwright().start()
        options = {"headless": True, "chromium_sandbox": False}
        if os.environ.get("COMPANION_TEST_CHROMIUM"):
            options["executable_path"] = os.environ["COMPANION_TEST_CHROMIUM"]
        try:
            self.browser = await self.playwright.chromium.launch(**options)
        except Exception as exc:
            await self.playwright.stop()
            self.playwright = None
            raise unittest.SkipTest("测试 Chromium 不可用：" + type(exc).__name__) from None


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config("https://example.invalid", "test", path=self.root / "config.json")
        self.cfg.save()
        self.hits = 0
        async def forbidden(request):
            self.hits += 1
            return web.Response(text="NOT_AUTHORIZED")
        other = web.Application()
        other.router.add_route("*", "/{path:.*}", forbidden)
        self.other = web.AppRunner(other)
        await self.other.setup()
        site = web.TCPSite(self.other, "127.0.0.1", 0)
        await site.start()
        self.forbidden_url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        async def page(request):
            path = request.path
            if path == "/redirect":
                raise web.HTTPFound(self.forbidden_url + "/secret")
            if path == "/chain":
                raise web.HTTPFound("/redirect")
            if path == "/slow":
                await asyncio.sleep(2)
            if path == "/password":
                html = '<title>Form</title><input type="password" value="SYNTHETIC_PASSWORD">'
            elif path == "/token":
                html = '<title>Form</title><input type="hidden" name="access_token" value="SYNTHETIC_TOKEN">'
            elif path == "/otp":
                html = '<title>Form</title><input autocomplete="one-time-code" value="SYNTHETIC_OTP">'
            elif path == "/login":
                html = '<title>Login</title><div>Sign in here</div>'
            elif path == "/download":
                html = '<title>Reports</title><a id="dl" download href="/file">下载报表</a>'
            elif path == "/file":
                return web.Response(body=b"name,value\nalpha,1\n", content_type="text/csv",
                                    headers={"Content-Disposition": 'attachment; filename="report.csv"'})
            elif path == "/cookie":
                return web.Response(text="cookie:" + request.cookies.get("sid", "none"), content_type="text/plain")
            elif path == "/frame":
                html = '<iframe src="' + self.forbidden_url + '/secret"></iframe>'
            else:
                html = '''<title>Public synthetic page</title><h1>Hello local</h1>
                    <input id="field" value="SYNTHETIC_INPUT_SECRET">
                    <textarea>SYNTHETIC_TEXTAREA_SECRET</textarea>
                    <input type="hidden" name="internal" value="SYNTHETIC_HIDDEN_SECRET">
                    <script>const privateValue = 'SYNTHETIC_SCRIPT_SECRET';</script>
                    <button onclick="document.querySelector('h1').textContent='Clicked'">Go</button>
                    <select><option value="a">Alpha</option><option value="b">Beta</option></select>
                    <div style="height:1800px">Page content</div>'''
            return web.Response(text=html, content_type="text/html")
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", page)
        self.server = web.AppRunner(app)
        await self.server.setup()
        site = web.TCPSite(self.server, "127.0.0.1", 0)
        await site.start()
        self.base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.cfg.origins = [self.base]
        self.browser = SyntheticBrowser(self.cfg)
        await self.browser.prepare()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.server.cleanup()
        await self.other.cleanup()
        self.tmp.cleanup()

    async def goto(self, path="/", task="t1"):
        return await self.browser.execute("browser.goto", {"url": self.base + path}, ("session", task))

    async def test_snapshot_excludes_values_and_query(self):
        snap = await self.goto("/?token=SYNTHETIC_QUERY_SECRET")
        serialized = json.dumps(snap)
        self.assertIn("Hello local", snap["text"])
        for marker in ("INPUT", "TEXTAREA", "HIDDEN", "SCRIPT", "QUERY"):
            self.assertNotIn("SYNTHETIC_" + marker + "_SECRET", serialized)
        self.assertTrue(all(item["pw"] is False for item in snap["items"]))
        button = next(item for item in snap["items"] if item["tag"] == "button")
        await self.browser.execute("browser.act", {"ref": button["ref"], "verb": "click"}, ("session", "t1"))
        self.assertIn("Clicked", (await self.browser.snapshot())["text"])
        with self.assertRaises(Denied):
            await self.browser.execute("browser.act", {"ref": button["ref"], "verb": "click"}, ("session", "t1"))

    async def test_fill_select_scroll_screenshot_and_task_isolation(self):
        snap = await self.goto()
        ref = next(x["ref"] for x in snap["items"] if x["tag"] == "input")
        await self.browser.execute("browser.act", {"ref": ref, "verb": "fill", "value": "synthetic-new"}, ("session", "t1"))
        self.assertEqual(await self.browser.page.locator("#field").input_value(), "synthetic-new")
        snap = await self.browser.snapshot()
        ref = next(x["ref"] for x in snap["items"] if x["tag"] == "select")
        await self.browser.execute("browser.act", {"ref": ref, "verb": "select", "value": "b"}, ("session", "t1"))
        self.assertEqual(await self.browser.page.locator("select").input_value(), "b")
        await self.browser.execute("browser.scroll", {"direction": "down", "amount": 300}, ("session", "t1"))
        shot = await self.browser.screenshot()
        self.assertEqual((shot["w"], shot["h"]), (1024, 720))
        self.assertLess(len(base64.b64decode(shot["jpeg"])), 400 * 1024)
        with self.assertRaises(Denied):
            await self.browser.execute("browser.screenshot", {}, ("session", "other"))
        old_context = self.browser.context
        await self.goto(task="t2")
        self.assertIsNot(old_context, self.browser.context)
        self.assertEqual(await self.browser.page.locator("#field").input_value(), "SYNTHETIC_INPUT_SECRET")

    async def test_login_handoff_imports_once_into_task_context(self):
        import time
        result = login_handoff(self.cfg, {"type": "login_handoff", "protocol": 2, "origin": self.base,
            "cookies": [{"name": "sid", "value": "SYNTHETIC_COOKIE", "domain": "127.0.0.1",
                         "path": "/", "secure": False, "httpOnly": True}],
            "expiry": time.time() + 60})
        self.assertTrue(result["ok"])
        snap = await self.goto("/cookie", task="login")
        self.assertIn("cookie:SYNTHETIC_COOKIE", snap["text"])
        self.assertFalse((self.cfg.state / "login-handoff.json").exists())
        fresh = await self.goto("/cookie", task="login-2")
        self.assertIn("cookie:none", fresh["text"])

    async def test_sensitive_pages_pause_before_snapshot_or_screenshot(self):
        for path in ("/password", "/token", "/otp", "/login"):
            with self.assertRaises(Paused):
                await self.goto(path, task=path[1:])
            with self.assertRaises(Paused):
                await self.browser.screenshot()

    async def test_strict_origin_and_redirect_chain(self):
        with self.assertRaises(Denied):
            await self.browser.execute("browser.goto", {"url": self.forbidden_url}, ("s", "t"))
        for path in ("/redirect", "/chain", "/frame"):
            with self.assertRaises(Paused):
                await self.goto(path, task=path[1:])
        self.assertEqual(self.hits, 0)

    async def test_cancel_closes_only_task_browser(self):
        task = asyncio.create_task(self.goto("/slow"))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.browser.stop_task()
        self.assertIsNone(self.browser.context)
        self.assertIsNone(self.browser.page)
        self.assertIsNotNone(self.browser.browser)

    async def test_download_only_publishes_new_file_in_write_root(self):
        link = next(x["ref"] for x in (await self.goto("/download", task="dl"))["items"]
                    if x["tag"] == "a")
        with self.assertRaises(Denied):
            await self.browser.execute("browser.download", {"ref": link}, ("session", "dl"))
        with tempfile.TemporaryDirectory() as data:
            files = Path(data) / "files"
            files.mkdir()
            self.cfg.roots = [{"path": str(files), "read": True, "write_new": True}]
            first = await self.browser.execute("browser.download", {"ref": link, "name": "../报表.csv"},
                                               ("session", "dl"))
            target = Path(first["path"])
            self.assertEqual(target.parent, files / "Z-OPS下载")
            self.assertEqual(target.read_bytes(), b"name,value\nalpha,1\n")
            self.assertEqual(first["size"], len(b"name,value\nalpha,1\n"))
            self.assertEqual(len(first["sha256"]), 64)
            self.assertNotIn("..", target.name)
            link = next(x["ref"] for x in (await self.goto("/download", task="dl"))["items"]
                        if x["tag"] == "a")
            second = await self.browser.execute("browser.download", {"ref": link, "name": target.name},
                                                ("session", "dl"))
            self.assertNotEqual(second["path"], first["path"])
            self.assertEqual(target.read_bytes(), b"name,value\nalpha,1\n")


class RouteGenerationTests(unittest.IsolatedAsyncioTestCase):
    """不依赖真实 Chromium：直接验证任务切换时网络代际的行为。"""

    class Page:
        def __init__(self, url):
            self.url = url

        async def evaluate(self, script):
            return False

    class Route:
        def __init__(self):
            self.aborted = []

        async def abort(self, reason):
            self.aborted.append(reason)

    class Request:
        method = "GET"
        url = "https://example.test/late"
        post_data_buffer = None

        async def all_headers(self):
            return {}

    class Http:
        """要么在释放后报错（模拟连接被关闭），要么立即报错。"""

        def __init__(self, entered=None, release=None, immediate=False):
            self.entered, self.release, self.immediate = entered, release, immediate

        def request(self, *args, **kwargs):
            http = self

            class Stalled:
                async def __aenter__(self):
                    if http.entered is not None:
                        http.entered.set()
                    if http.release is not None:
                        await http.release.wait()
                    raise ConnectionError("任务已被停止")

                async def __aexit__(self, *exc):
                    return False

            return Stalled()

        async def close(self):
            pass

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config("https://example.invalid", "test", path=Path(self.tmp.name) / "config.json",
                          origins=["https://example.test:443"])
        self.cfg.save()
        self.browser = Browser(self.cfg)
        self.browser.page = self.Page("https://example.test/")

    def tearDown(self):
        self.tmp.cleanup()

    async def test_late_route_from_previous_task_cannot_block_new_task(self):
        """停止时仍在途的请求结束后, 不得把下一个任务标记成 origin_blocked。"""
        entered, release = asyncio.Event(), asyncio.Event()
        generation = self.browser.generation
        self.browser.http = self.Http(entered, release)
        route = self.Route()
        pending = asyncio.create_task(self.browser.route(route, self.Request()))
        await asyncio.wait_for(entered.wait(), 5)
        # 旧请求还在路上时切换任务。
        await self.browser.stop_task()
        self.assertGreater(self.browser.generation, generation)
        self.assertFalse(self.browser.blocked)
        release.set()
        await asyncio.wait_for(pending, 5)
        self.assertEqual(route.aborted, ["blockedbyclient"])
        self.assertFalse(self.browser.blocked)

        # 新任务建立后 guard 必须正常通过, 不因上一个任务的迟到失败而暂停。
        self.browser.page = self.Page("https://example.test/")
        self.browser.http = self.Http()
        await self.browser.guard()

        # 正对照: 当前代际的失败仍然要拦截后续动作。
        self.browser.http = self.Http(immediate=True)
        await self.browser.route(self.Route(), self.Request())
        self.assertTrue(self.browser.blocked)
        with self.assertRaises(Paused):
            await self.browser.guard()

    async def test_route_after_stop_uses_no_previous_session(self):
        """停止后 http 已释放; 旧请求不得复用已关闭的会话。"""
        self.browser.http = None
        route = self.Route()
        await self.browser.route(route, self.Request())
        self.assertEqual(route.aborted, ["blockedbyclient"])
        self.assertFalse(self.browser.blocked)


class NativeTests(unittest.IsolatedAsyncioTestCase):
    def test_login_handoff_handler_validates_origin_and_hides_values(self):
        import json as _json
        import time
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config("https://example.invalid", "test", path=Path(tmp) / "config.json",
                         origins=["https://example.test:443"])
            cfg.save()
            packet = {"type": "login_handoff", "protocol": 2, "origin": "https://example.test",
                      "cookies": [{"name": "sid", "value": "SYNTHETIC_COOKIE"}],
                      "expiry": time.time() + 60}
            result = login_handoff(cfg, packet)
            self.assertTrue(result["ok"])
            self.assertEqual(result["count"], 1)
            self.assertNotIn("SYNTHETIC", _json.dumps(result))
            self.assertTrue((cfg.state / "login-handoff.json").exists())
            for bad in ({**packet, "origin": "https://other.test"},
                        {**packet, "protocol": 1},
                        {**packet, "expiry": time.time() + 3600},
                        {**packet, "extra": "x"},
                        {**packet, "cookies": [{"name": "sid", "value": "X", "domain": ".other.test"}]}):
                answer = login_handoff(cfg, bad)
                self.assertFalse(answer["ok"])
                self.assertNotIn("SYNTHETIC", _json.dumps(answer))

    def test_frames_and_desktop_disabled(self):
        stream = io.BytesIO()
        write_frame(stream, {"type": "hello", "protocol": 2})
        stream.seek(0)
        self.assertEqual(read_frame(stream), {"type": "hello", "protocol": 2})
        self.assertIsNone(read_frame(stream))
        for raw in (b"\x01", struct.pack("<I", 300000), struct.pack("<I", 4) + b"{}"):
            with self.assertRaises((Denied, ValueError)):
                read_frame(io.BytesIO(raw))
        self.assertEqual(Desktop.capabilities, [])

    async def test_real_stdio_host_hello_rejects_nonbrowser_and_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config("https://example.invalid", "test", device_id="device", actor_id="actor",
                         path=Path(tmp) / "config.json",
                         native_origins=["chrome-extension://" + "a" * 32 + "/"])
            cfg.save()
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "companion.native_host",
                cfg.native_origins[0], "--config", str(cfg.path), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            async def send(data):
                payload = encode(data)
                process.stdin.write(struct.pack("<I", len(payload)) + payload)
                await process.stdin.drain()
            async def receive():
                size = struct.unpack("<I", await asyncio.wait_for(process.stdout.readexactly(4), 10))[0]
                return decode(await process.stdout.readexactly(size))
            try:
                await send({"type": "hello", "protocol": 2})
                hello = await receive()
                self.assertFalse(hello["server_connected"])
                self.assertEqual(hello["capabilities"], [])
                import time
                await send({"type": "command", "protocol": 2, "actor_id": "actor", "device_id": "device",
                    "session_id": "s", "task_id": "t", "command_id": "c", "seq": 1, "expiry": time.time() + 60,
                    "connection_generation": hello["connection_generation"], "fencing_token": hello["next_fencing_token"],
                    "method": "system.info", "params": {}})
                self.assertEqual((await receive())["status"], "rejected")
                process.stdin.close()
                await asyncio.wait_for(process.wait(), 5)
                self.assertEqual(process.returncode, 0, (await process.stderr.read()).decode())
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()


if __name__ == "__main__":
    unittest.main()
