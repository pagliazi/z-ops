"""CLI、配置和构建入口隔离测试；不连接真实服务器/keyring。"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from companion.config import Config
from companion.locking import instance_lock
from companion.security import Denied


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, *args):
        return subprocess.run([sys.executable, "-m", "companion", "--config", str(self.path), *args],
            input="", capture_output=True, text=True, timeout=15,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))

    def test_init_doctor_no_defaults_no_secret(self):
        result = self.invoke("--server", "https://example.invalid", "--name", "Synthetic", "--init")
        self.assertEqual(result.returncode, 0, result.stderr)
        cfg = Config.load(self.path)
        self.assertEqual(cfg.roots, [])
        self.assertEqual(cfg.origins, [])
        self.assertEqual(cfg.scripts, {})
        self.assertEqual(cfg.desktop_apps, [])
        self.assertEqual(cfg.device_id, "")
        result = self.invoke("--doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertFalse(status["desktop_enabled"])
        self.assertEqual(status["capabilities"], ["process.list", "service.list", "system.info"])
        self.assertEqual(status["build"], "unsigned_test_only")
        # 实机验收要能复核预检逐项与 UIA 实测; 未启用桌面时为空而不是缺字段。
        self.assertEqual(status["desktop"]["checks"], {})
        self.assertEqual(status["desktop"]["uia"], {})
        self.assertNotIn("private_key", self.path.read_text())

    def test_failure_message_carries_fixed_code_only(self):
        # 私有目录权限不对时报固定错误码，既能让验收人复核，又不回显路径。
        self.root.chmod(0o755)
        result = self.invoke("--server", "https://example.invalid", "--name", "Synthetic", "--init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("state_directory_not_private", result.stderr)
        self.assertNotIn(str(self.root), result.stderr)

    def test_no_automatic_local_approval_and_no_server_change(self):
        self.assertEqual(self.invoke("--server", "https://example.invalid", "--name", "Synthetic", "--init").returncode, 0)
        result = self.invoke("--grant-read", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(Config.load(self.path).roots, [])
        result = self.invoke("--allow-desktop-app", "notepad.exe")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(Config.load(self.path).desktop_apps, [])
        result = self.invoke("--server", "https://another.invalid", "--doctor")
        self.assertNotEqual(result.returncode, 0)

    def test_script_registration_requires_target_dirs_inside_granted_roots(self):
        from companion import __main__ as cli
        cfg = Config("https://example.invalid", "test", path=self.path)
        data = self.root / "data"
        outside = self.root / "outside"
        data.mkdir()
        outside.mkdir()
        cfg.roots = [{"path": str(data), "read": True, "write_new": True, "manage": False}]
        cfg.save()
        script = self.root / "probe.py"
        script.write_text("print('ok')\n", encoding="utf-8")
        executable = Path(sys.executable).resolve()
        entry = {"path": str(script), "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                 "executable": str(executable),
                 "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                 "schema": {"type": "object", "properties": {}, "required": [],
                            "additionalProperties": False},
                 "timeout": 5, "platform": ["linux"], "permissions": ["read"],
                 "target_dirs": [str(outside)]}
        registration = self.root / "registration.json"
        registration.write_text(json.dumps({"name": "probe", "entry": entry}), encoding="utf-8")
        args = Namespace(revoke_all=False, grant_read=[], grant_write_new=[], grant_manage=[],
                         grant_origin=[], grant_extension=[], allow_desktop_app=[],
                         register_script=[str(registration)])
        with patch.object(cli, "confirm", lambda message: None):
            with self.assertRaises(Denied):
                cli.authorize(cfg, args)
            self.assertEqual(cfg.scripts, {})
            # 目标目录在授权可写根内时登记成功, 元信息随本机配置保存。
            entry["target_dirs"] = [str(data)]
            registration.write_text(json.dumps({"name": "probe", "entry": entry}), encoding="utf-8")
            cli.authorize(cfg, args)
        self.assertIn("probe", cfg.scripts)
        self.assertEqual(cfg.scripts["probe"]["target_dirs"], [str(data)])
        self.assertEqual(cfg.scripts["probe"]["version"], 1)
        status = json.loads(self.invoke("--doctor").stdout)
        self.assertEqual(status["scripts"][0]["name"], "probe")
        self.assertEqual(status["scripts"][0]["version"], 1)
        self.assertEqual(status["scripts"][0]["platform"], ["linux"])
        self.assertNotIn("print('ok')", json.dumps(status, ensure_ascii=False))

    def test_native_manifest_requires_granted_extension_and_is_stdio_only(self):
        self.assertEqual(self.invoke("--server", "https://example.invalid", "--name", "Synthetic",
                                     "--init").returncode, 0)
        host = str(Path(sys.executable).resolve())
        # 未授权扩展时拒绝输出 manifest。
        self.assertNotEqual(self.invoke("--native-manifest", host).returncode, 0)
        cfg = Config.load(self.path)
        cfg.native_origins = ["chrome-extension://" + "a" * 32 + "/"]
        cfg.save()
        result = self.invoke("--native-manifest", host)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest["type"], "stdio")
        self.assertEqual(manifest["allowed_origins"], cfg.native_origins)
        self.assertEqual(manifest["path"], host)

    def test_private_mode_unknown_secret_key_and_instance_lock(self):
        cfg = Config("https://example.invalid", "test", path=self.path)
        cfg.save()
        with instance_lock(cfg.state / "lock"):
            with self.assertRaises(Denied):
                with instance_lock(cfg.state / "lock"):
                    self.fail("不应获得第二把锁")
        self.path.chmod(0o644)
        with self.assertRaises(Denied):
            Config.load(self.path)
        self.path.chmod(0o600)
        data = json.loads(self.path.read_text())
        data["private_key"] = "SYNTHETIC_SECRET"
        self.path.write_text(json.dumps(data))
        with self.assertRaises(Denied):
            Config.load(self.path)

    def test_frozen_cli_and_real_document_worker(self):
        import companion
        binary = Path(companion.__file__).parent / "dist" / "companion-unsigned-test" / "companion-unsigned-test"
        if not binary.exists():
            self.skipTest("未生成可选测试构建")
        result = subprocess.run([str(binary), "--version"], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        private = self.root / "private"
        private.mkdir(mode=0o700)
        data = self.root / "data"
        data.mkdir()
        cfg = Config("https://example.invalid", "test", path=private / "config.json",
                     roots=[{"path": str(data), "read": True, "write_new": True}])
        cfg.save()
        for method, params in (("document.write_new", {"path": str(data / "test.docx"), "content": "Frozen synthetic"}),
                               ("document.extract", {"path": str(data / "test.docx")})):
            result = subprocess.run([str(binary), "--internal-worker"],
                input=json.dumps({"config": str(cfg.path), "method": method, "params": params}),
                capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "ok", result.stdout)
        self.assertIn("Frozen synthetic", json.loads(result.stdout)["data"]["text"])

    def test_unsigned_build_requires_explicit_ack(self):
        result = subprocess.run([sys.executable, "-m", "companion.build"], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"--ack-unsigned-test", result.stderr)


if __name__ == "__main__":
    unittest.main()
