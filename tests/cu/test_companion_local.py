"""只使用临时目录、合成内容，不加载服务端或 Redis。"""
import asyncio
import hashlib
import io
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from companion.config import Config
from companion.client import grants
from companion import login_state
from companion.files import Files, extract
from companion.processes import OwnedProcesses, local_worker
from companion.scripts import arguments, run_script
from companion.security import Denied, decode, encode, origin


class ExecutorCapabilityTests(unittest.IsolatedAsyncioTestCase):
    """浏览器能力必须按真实原因声明; 不能把运行身份问题报成沙箱不可用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "private"
        self.home.mkdir(mode=0o700)
        self.cfg = Config("https://example.invalid", "test", path=self.home / "config.json")
        self.cfg.origins = ["https://example.test:443"]
        self.cfg.save()

    def tearDown(self):
        self.tmp.cleanup()

    async def test_browser_unavailable_reason_reflects_run_identity(self):
        from companion.executor import Executor
        executor = Executor(self.cfg)
        with patch("companion.executor.Browser.prepare", AsyncMock(side_effect=RuntimeError("x"))), \
                patch("companion.executor.os.getuid", return_value=0, create=True):
            await executor.prepare()
        self.assertEqual(executor.unavailable["browser"], "run_as_normal_desktop_user_not_root")
        self.assertFalse([c for c in executor.capabilities if c.startswith("browser.")])

        executor = Executor(self.cfg)
        with patch("companion.executor.Browser.prepare", AsyncMock(side_effect=RuntimeError("x"))), \
                patch("companion.executor.os.getuid", return_value=1000, create=True):
            await executor.prepare()
        self.assertEqual(executor.unavailable["browser"], "sandboxed_chromium_unavailable")

    async def test_no_origin_grant_reports_missing_grant_not_browser_failure(self):
        from companion.executor import Executor
        self.cfg.origins = []
        self.cfg.save()
        executor = Executor(self.cfg)
        with patch("companion.executor.Browser.prepare", AsyncMock()) as prepare:
            await executor.prepare()
        self.assertEqual(executor.unavailable["browser"], "no_local_origin_grant")
        prepare.assert_not_called()
        self.assertNotIn("browser.goto", executor.capabilities)


class LocalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "private"
        self.home.mkdir(mode=0o700)
        self.data = self.base / "data"
        self.data.mkdir()
        self.cfg = Config("https://example.invalid", "test", path=self.home / "config.json")
        self.cfg.save()
        self.files = Files(self.cfg)
        self.owner = OwnedProcesses()

    async def asyncTearDown(self):
        await self.owner.stop()
        self.tmp.cleanup()

    def grant(self):
        self.cfg.roots = [{"path": str(self.data), "read": True, "write_new": True}]
        self.cfg.save()

    def test_default_deny_and_protected_config(self):
        p = self.data / "a.txt"
        p.write_text("synthetic")
        with self.assertRaises(Denied):
            self.files.read_bytes(str(p))
        self.cfg.roots = [{"path": str(self.base), "read": True, "write_new": True}]
        with self.assertRaises(Denied):
            self.files.read_bytes(str(self.cfg.path))

    def test_no_links_no_overwrite_and_bounded(self):
        self.grant()
        p = self.data / "new.txt"
        receipt = self.files.write_bytes(str(p), b"original")
        self.assertTrue(receipt["created"])
        self.assertEqual(receipt["size"], 8)
        self.assertEqual(receipt["sha256"], hashlib.sha256(b"original").hexdigest())
        with self.assertRaises(FileExistsError):
            self.files.write_bytes(str(p), b"replacement")
        self.assertEqual(p.read_bytes(), b"original")
        link = self.data / "link"
        link.symlink_to(p)
        with self.assertRaises(Denied):
            self.files.read_bytes(str(link))
        hard = self.data / "hard"
        os.link(p, hard)
        with self.assertRaises(Denied):
            self.files.read_bytes(str(hard))
        with self.assertRaises(Denied):
            self.files.read_bytes(str(self.data / ".." / "private" / "config.json"))
        outside = self.base / "outside"
        outside.mkdir()
        (self.data / "junction").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(Denied):
            self.files.write_bytes(str(self.data / "junction" / "bad"), b"bad")

    def test_file_find_is_bounded_and_skips_links(self):
        self.grant()
        nested = self.data / "a" / "b"
        nested.mkdir(parents=True)
        (self.data / "top.xlsx").write_text("synthetic")
        (self.data / "a" / "report.xlsx").write_text("synthetic")
        (nested / "deep.csv").write_text("synthetic")
        deep = self.data
        for _ in range(7):
            deep = deep / "d"
        deep.mkdir(parents=True)
        (deep / "too-deep.xlsx").write_text("synthetic")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "outside.xlsx").write_text("synthetic")
        (self.data / "dir-link").symlink_to(outside, target_is_directory=True)
        (self.data / "file-link.xlsx").symlink_to(outside / "outside.xlsx")
        hard = self.data / "hard.xlsx"
        (self.data / "hl-source.xlsx").write_text("synthetic")
        os.link(self.data / "hl-source.xlsx", hard)
        # 硬链接本身及其源文件都会被跳过, 避免用链接绕过授权根。
        result = self.files.dispatch("file.find", {"path": str(self.data), "pattern": "*.xlsx"})
        names = sorted(item["name"] for item in result["entries"])
        self.assertEqual(names, ["report.xlsx", "top.xlsx"])
        self.assertFalse(result["truncated"])
        self.assertTrue(all(item["path"].startswith(str(self.data)) for item in result["entries"]))
        self.assertNotIn("too-deep.xlsx", names)
        for pattern in ("../*.xlsx", "a/*.xlsx", "..", ""):
            with self.assertRaises((Denied, ValueError)):
                self.files.dispatch("file.find", {"path": str(self.data), "pattern": pattern})
        for _ in range(5):
            target = self.data / f"log-{_}.log"
            target.write_text("synthetic")
        limited = self.files.dispatch("file.find", {"path": str(self.data), "pattern": "*.log", "max_results": 2})
        self.assertEqual(len(limited["entries"]), 2)
        self.assertTrue(limited["truncated"])
        with self.assertRaises(Denied):
            self.files.dispatch("file.find", {"path": str(outside), "pattern": "*.xlsx"})
        with self.assertRaises(Denied):
            self.files.dispatch("file.find", {"path": str(self.data), "pattern": "*.log", "command": "find"})

    async def test_service_list_is_read_only_and_rejects_parameters(self):
        data = await local_worker(self.owner, self.cfg, "service.list", {})
        self.assertIsInstance(data.get("available"), bool)
        self.assertIsInstance(data.get("services"), list)
        self.assertLessEqual(len(data["services"]), 300)
        for item in data["services"]:
            self.assertLessEqual(len(item["name"]), 120)
            self.assertIsInstance(item["state"], str)
        with self.assertRaises(Denied):
            await local_worker(self.owner, self.cfg, "service.list", {"name": "sshd"})

    def test_log_collect_redacts_and_stays_in_read_root(self):
        self.grant()
        log = self.data / "app.log"
        secret = "SYNTHETIC_LOG_SECRET"
        digest = "b" * 64
        log.write_text(
            "2026-10-23 10:00:00 INFO started\n"
            f"password={secret} token: {secret}\n"
            f"Authorization: Bearer {secret}value\n"
            f"url=https://alice:{secret}@example.com/path\n"
            f"mail=alice@example.com digest={digest}\n"
            f"-----BEGIN RSA PRIVATE KEY-----\n{secret}\n-----END RSA PRIVATE KEY-----\n")
        result = self.files.dispatch("log.collect", {"path": str(log), "max_bytes": 4096})
        text = result["text"]
        for marker in (secret, digest, "alice@example.com"):
            self.assertNotIn(marker, text)
        self.assertIn("started", text)
        self.assertIn("[REDACTED]", text)
        self.assertGreaterEqual(sum(result["redacted"].values()), 5)
        self.assertEqual(len(result["sha256"]), 64)
        self.assertEqual(result["bytes"], log.stat().st_size)
        with self.assertRaises(Denied):
            self.files.dispatch("log.collect", {"path": str(self.cfg.path)})
        with self.assertRaises((Denied, ValueError)):
            self.files.dispatch("log.collect", {"path": str(log), "max_bytes": 10})
        with self.assertRaises(Denied):
            self.files.dispatch("log.collect", {"path": str(log), "command": "tail"})

    def test_login_handoff_is_origin_scoped_single_use_and_private(self):
        self.cfg.origins = ["https://example.test:443"]
        self.cfg.save()
        now = time.time()
        cookie = {"name": "sid", "value": "SYNTHETIC_COOKIE", "domain": ".example.test",
                  "path": "/", "secure": True, "httpOnly": True, "sameSite": "lax"}
        with self.assertRaises(Denied):
            login_state.store(self.cfg, "https://other.test", [cookie], now + 60)
        with self.assertRaises(Denied):
            login_state.store(self.cfg, "https://example.test",
                              [{**cookie, "domain": ".other.test"}], now + 60)
        with self.assertRaises(Denied):
            login_state.store(self.cfg, "https://example.test", [cookie], now + 3600)
        with self.assertRaises(Denied):
            login_state.store(self.cfg, "https://example.test", [], now + 60)
        self.assertEqual(login_state.store(self.cfg, "https://example.test", [cookie], now + 60), 1)
        path = self.cfg.state / "login-handoff.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        data = login_state.claim(self.cfg)
        self.assertEqual(data["origin"], "https://example.test:443")
        self.assertEqual(data["cookies"][0]["value"], "SYNTHETIC_COOKIE")
        self.assertFalse(path.exists())
        self.assertIsNone(login_state.claim(self.cfg))

    def test_login_handoff_expired_tampered_or_public_is_discarded(self):
        self.cfg.origins = ["https://example.test:443"]
        self.cfg.save()
        now = time.time()
        cookie = {"name": "sid", "value": "SYNTHETIC_COOKIE"}
        login_state.store(self.cfg, "https://example.test", [cookie], now + 60)
        path = self.cfg.state / "login-handoff.json"
        self.assertIsNone(login_state.claim(self.cfg, clock=lambda: now + 400))
        self.assertFalse(path.exists())
        path.write_text('{"origin":"https://example.test:443","cookies":[],"expiry":0,"extra":1}')
        os.chmod(path, 0o600)
        self.assertIsNone(login_state.claim(self.cfg))
        self.assertFalse(path.exists())
        login_state.store(self.cfg, "https://example.test", [cookie], now + 60)
        os.chmod(path, 0o644)
        self.assertIsNone(login_state.claim(self.cfg))
        self.assertFalse(path.exists())
        login_state.store(self.cfg, "https://example.test", [cookie], now + 60)
        os.chmod(path, 0o600)
        path.write_bytes(b'{"origin":"https://example.test:443","cookies":[{"name":"sid","value":"X","domain":".evil.test"}],"expiry":' + str(int(now + 60)).encode() + b'}')
        self.assertIsNone(login_state.claim(self.cfg))
        self.assertFalse(path.exists())

    def test_manage_operations_need_permission_and_never_overwrite(self):
        self.grant()
        source = self.data / "a.txt"
        source.write_text("synthetic")
        with self.assertRaises(Denied):
            self.files.dispatch("file.move", {"source": str(source), "target": str(self.data / "b.txt")})
        with self.assertRaises(Denied):
            self.files.dispatch("file.delete", {"path": str(source)})
        with self.assertRaises(Denied):
            self.files.dispatch("file.replace", {"path": str(source), "content": "x"})
        self.cfg.roots = [{"path": str(self.data), "read": True, "write_new": True, "manage": True}]
        moved = self.files.dispatch("file.move", {"source": str(source),
                                                 "target": str(self.data / "b.txt")})
        self.assertTrue(moved["moved"])
        self.assertFalse(source.exists())
        existing = self.data / "c.txt"
        existing.write_text("keep")
        with self.assertRaises((Denied, FileExistsError)):
            self.files.dispatch("file.move", {"source": str(self.data / "b.txt"),
                                              "target": str(existing)})
        self.assertEqual(existing.read_text(), "keep")
        receipt = self.files.dispatch("file.replace", {"path": str(existing), "content": "replaced"})
        self.assertTrue(receipt["replaced"])
        self.assertEqual(len(receipt["sha256"]), 64)
        self.assertEqual(existing.read_text(), "replaced")
        outside = self.base / "outside.txt"
        outside.write_text("outside")
        (self.data / "link.txt").symlink_to(outside)
        with self.assertRaises(Denied):
            self.files.dispatch("file.delete", {"path": str(self.data / "link.txt")})
        with self.assertRaises(Denied):
            self.files.dispatch("file.delete", {"path": str(self.data / "missing.txt")})
        self.assertTrue(outside.exists())
        deleted = self.files.dispatch("file.delete", {"path": str(self.data / "b.txt")})
        self.assertTrue(deleted["deleted"])
        self.assertFalse((self.data / "b.txt").exists())
        (self.data / "nested").mkdir()
        with self.assertRaises((Denied, IsADirectoryError)):
            self.files.dispatch("file.delete", {"path": str(self.data / "nested")})

    def test_grants_summary_hides_full_paths_and_bounds(self):
        self.cfg.roots = [{"path": "/home/alice/Documents", "read": True, "write_new": True}]
        self.cfg.origins = ["https://example.invalid"]
        self.cfg.scripts = {"disk_report": {}}
        self.cfg.native_origins = ["chrome-extension://" + "a" * 32 + "/"]
        self.cfg.desktop_apps = ["notepad.exe", "calc.exe"]
        summary = grants(self.cfg)
        self.assertEqual(summary["roots"], [{"name": "Documents", "read": True,
                                             "write_new": True, "manage": False}])
        self.assertNotIn("alice", str(summary))
        self.assertEqual(summary["scripts"], ["disk_report"])
        self.assertEqual(summary["origins"], ["https://example.invalid"])
        self.assertEqual(summary["apps"], ["calc.exe", "notepad.exe"])

    def test_config_without_desktop_apps_defaults_to_none_authorized(self):
        self.cfg.save()
        raw = decode(self.cfg.path.read_bytes())
        raw.pop("desktop_apps")
        self.cfg.path.write_bytes(encode(raw, 262144))
        # 旧版本配置无应用字段: 升级后默认不授权任何应用接管。
        self.assertEqual(Config.load(self.cfg.path).desktop_apps, [])

    async def test_worker_documents_and_formula_as_text(self):
        self.grant()
        for suffix, params in [(".docx", {"content": "中文 synthetic"}),
                               (".xlsx", {"rows": [["synthetic", "=1+1"], [42, True]]})]:
            p = str(self.data / ("doc" + suffix))
            result = await local_worker(self.owner, self.cfg, "document.write_new", {"path": p, **params})
            self.assertTrue(result["created"])
            result = await local_worker(self.owner, self.cfg, "document.extract", {"path": p})
            self.assertIn("synthetic", result["text"])
        from openpyxl import load_workbook
        book = load_workbook(self.data / "doc.xlsx")
        self.assertEqual(book.active["B1"].data_type, "s")
        book.close()
        result = await local_worker(self.owner, self.cfg, "file.list", {"path": str(self.data)})
        self.assertEqual(len(result["entries"]), 2)

    def test_zip_bomb_and_json_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", "x" * 1000000)
        with self.assertRaises(Denied):
            extract(".docx", buf.getvalue())
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '['):
            with self.assertRaises(Denied):
                decode(raw)
        for url in ("file:///etc/passwd", "https://user:pass@example.org", "https://a\\@b"):
            with self.assertRaises(Denied):
                origin(url)

    def script(self, source, timeout=3):
        scripts = self.base / "scripts"
        scripts.mkdir(exist_ok=True)
        p = scripts / "fixed.py"
        p.write_text(source)
        executable = Path(sys.executable).resolve()
        entry = {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                 "executable": str(executable), "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                 "timeout": timeout, "schema": {"type": "object", "properties": {"msg": {"type": "string", "maxLength": 100}},
                                              "required": [], "additionalProperties": False}}
        self.cfg.scripts = {"test": entry}
        return p, entry

    async def test_script_real_run_hash_and_schema(self):
        p, entry = self.script("import sys,os\nprint(sys.argv[1:]); print(os.getenv('SYNTHETIC_SECRET', 'absent'))\n")
        os.environ["SYNTHETIC_SECRET"] = "NOT_FOR_CHILD"
        try:
            result = await run_script(self.owner, self.cfg, {"name": "test", "args": {"msg": "x;$(echo y)"}})
        finally:
            os.environ.pop("SYNTHETIC_SECRET")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("absent", result["stdout"])
        self.assertIn("x;$(echo y)", result["stdout"])
        with self.assertRaises(Denied):
            arguments(entry["schema"], {"unknown": "x"})
        p.write_text("print('changed')")
        with self.assertRaises(Denied):
            await run_script(self.owner, self.cfg, {"name": "test", "args": {}})
        with self.assertRaises(Denied):
            await run_script(self.owner, self.cfg, {"name": "unknown", "args": {}})

    async def test_script_metadata_is_validated_and_platform_gated(self):
        from companion.scripts import validate_entry
        p, entry = self.script("print('ok')\n")
        accepted = {**entry, "platform": ["linux"], "permissions": ["read"],
                    "target_dirs": [str(self.data)], "version": 2, "cancel": {"grace_s": 5}}
        validate_entry(accepted)
        # 平台/权限/目录/版本/取消元信息必须受限, 不得借登记扩张权限。
        for bad in ({"platform": []}, {"platform": ["plan9"]}, {"permissions": ["execute"]},
                    {"permissions": []}, {"target_dirs": ["relative"]}, {"version": 0},
                    {"version": ""}, {"cancel": {"grace_s": 0}},
                    {"cancel": {"grace_s": 60, "kill": True}}, {"unknown_meta": 1}):
            with self.assertRaises(Denied):
                validate_entry({**accepted, **bad})

    async def test_script_platform_mismatch_is_not_executed(self):
        marker = self.base / "ran.txt"
        p, entry = self.script("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('ran')\n")
        entry["platform"] = ["windows"] if os.name != "nt" else ["linux"]
        self.cfg.scripts = {"test": entry}
        with self.assertRaises(Denied) as caught:
            await run_script(self.owner, self.cfg, {"name": "test", "args": {}})
        self.assertIn("script_platform_unsupported", str(caught.exception))
        self.assertFalse(marker.exists())

    async def test_cancel_output_limit_and_timeout(self):
        self.script("import time\ntime.sleep(30)\n", timeout=1)
        with self.assertRaises(RuntimeError):
            await run_script(self.owner, self.cfg, {"name": "test", "args": {}})
        self.assertFalse(self.owner.active)
        self.script("import time\ntime.sleep(30)\n")
        job = asyncio.create_task(run_script(self.owner, self.cfg, {"name": "test", "args": {}}))
        await asyncio.sleep(0.2)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertFalse(self.owner.active)
        self.script("print('x' * 200000)\n")
        with self.assertRaises(RuntimeError):
            await run_script(self.owner, self.cfg, {"name": "test", "args": {}})

    async def test_worker_crash_after_write_and_invalid_receipts_are_unknown(self):
        marker = self.data / "committed.txt"
        real_run = self.owner.run

        async def crash_after_write(argv, **options):
            options.pop("payload")
            source = "from pathlib import Path; import os; Path(" + repr(str(marker)) + ").write_text('committed'); os._exit(7)"
            return await real_run([sys.executable, "-I", "-B", "-c", source], **options)

        with patch.object(self.owner, "run", side_effect=crash_after_write):
            with self.assertRaisesRegex(RuntimeError, "local_worker_outcome_unknown"):
                await local_worker(self.owner, self.cfg, "file.write_new", {"path": str(marker), "content": "committed"})
        self.assertEqual(marker.read_text(), "committed")
        for output in (b"", b'{"status":"ok"', b'{"status":"ok","data":null}',
                       b'{"status":"unexpected","data":{}}'):
            with patch.object(self.owner, "run", new=AsyncMock(return_value=(output, b"", 0))):
                with self.assertRaises(RuntimeError):
                    await local_worker(self.owner, self.cfg, "file.write_new", {})
        with patch.object(self.owner, "run", new=AsyncMock(side_effect=Denied("process_output_limit"))):
            with self.assertRaises(RuntimeError):
                await local_worker(self.owner, self.cfg, "file.write_new", {})
        self.grant()
        with self.assertRaises(Denied):
            await local_worker(self.owner, self.cfg, "file.write_new", {"path": str(marker), "content": "replacement"})
        self.assertEqual(marker.read_text(), "committed")

    async def test_termination_never_rediscovers_reused_pid(self):
        child = Mock(pid=12345, returncode=0)
        child.wait = AsyncMock(return_value=0)
        identity = Mock()
        identity.is_running.return_value = False
        self.owner.identities[child] = identity
        with patch("companion.processes.psutil.Process") as lookup:
            await self.owner.terminate(child)
            lookup.assert_not_called()
        identity.children.assert_not_called()
        identity.kill.assert_not_called()
        child.kill.assert_not_called()
        self.owner.identities.clear()

    async def test_worker_reaps_detached_descendant_not_unrelated_process(self):
        import psutil
        pidfile = self.base / "owned.pid"
        self.script("import os,time\nfrom pathlib import Path\np=Path(" + repr(str(pidfile)) + ")\n"
                    "pid=os.fork()\nif pid:\n while not p.exists(): time.sleep(.01)\n os._exit(0)\n"
                    "os.setsid()\nos.close(1)\nos.close(2)\np.write_text(str(os.getpid()))\ntime.sleep(30)\n")
        self.cfg.save()
        unrelated = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
        try:
            result = await local_worker(self.owner, self.cfg, "script.run", {"name": "test", "args": {}})
            self.assertEqual(result["exit_code"], 0)
            pid = int(pidfile.read_text())
            self.assertFalse(psutil.pid_exists(pid))
            self.assertIsNone(unrelated.returncode)
        finally:
            unrelated.kill()
            await unrelated.wait()


class TransportTrustTests(unittest.TestCase):
    """打包/自编译解释器的默认信任库可能为空; 此时必须回落到系统 CA 包。"""

    class Context:
        check_hostname = True
        verify_mode = 2

        def __init__(self, ca):
            self.ca = ca

        def cert_store_stats(self):
            return {"x509_ca": self.ca}

    def test_empty_default_store_falls_back_to_system_bundle(self):
        from companion import client
        with tempfile.NamedTemporaryFile(suffix=".pem") as bundle:
            calls = []

            def fake(cafile=None):
                calls.append(cafile)
                return self.Context(120 if cafile else 0)

            with patch("companion.client.ssl.create_default_context", side_effect=fake):
                context = client.default_context((bundle.name,))
            self.assertEqual(calls, [None, bundle.name])
            self.assertEqual(context.cert_store_stats()["x509_ca"], 120)

    def test_non_empty_default_store_is_used_as_is(self):
        from companion import client
        with patch("companion.client.ssl.create_default_context", return_value=self.Context(140)) as make:
            context = client.default_context(())
        self.assertEqual(context.cert_store_stats()["x509_ca"], 140)
        make.assert_called_once_with()

    def test_missing_bundles_keep_default_context_instead_of_disabling_verification(self):
        from companion import client
        with patch("companion.client.ssl.create_default_context", return_value=self.Context(0)):
            context = client.default_context(("/nonexistent/ca.pem",))
        self.assertEqual(context.cert_store_stats()["x509_ca"], 0)
        self.assertFalse(context.check_hostname is False)


if __name__ == "__main__":
    unittest.main()
