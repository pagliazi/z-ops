"""登记脚本元数据：平台/权限/目标目录/摘要/取消。"""
import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from core import script_exec as se


def digest_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ScriptRegistryMetadataTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = self.root / "reg"
        self.registry.mkdir()
        self.hello = self.registry / "hello.sh"
        self.hello.write_text('#!/bin/sh\necho "meta:$1:$2"\n')
        self.hello.chmod(0o755)
        self.old_dir, self.old_path = se.REGISTRY_DIR, se.REGISTRY_PATH
        se.REGISTRY_DIR = self.registry
        se.REGISTRY_PATH = self.registry / "registry.json"

    def tearDown(self):
        se.REGISTRY_DIR, se.REGISTRY_PATH = self.old_dir, self.old_path
        self.tmp.cleanup()

    def write(self, entries):
        (self.registry / "registry.json").write_text(json.dumps(entries))

    def entry(self, **overrides):
        body = {"name": "hello", "file": "hello.sh", "type": "shell",
                "hosts": ["localhost"], "timeout_s": 10, "risk": "read",
                "params_schema": ["--m"], "platform": [se.current_platform()],
                "permissions": ["read"], "target_dirs": [], "version": 1,
                "sha256": digest_of(self.hello), "cancel": {"grace_s": 3}}
        body.update(overrides)
        return body

    async def test_platform_permissions_digest_are_enforced_and_reported(self):
        self.write([self.entry(platform=["windows"] if se.current_platform() != "windows" else ["linux"])])
        with self.assertRaises(se.ScriptRegistryError) as raised:
            await se.run_registered("hello", {"m": "x"})
        self.assertEqual(raised.exception.code, "platform_not_supported")

        self.write([self.entry(sha256="0" * 64)])
        with self.assertRaises(se.ScriptRegistryError) as raised:
            await se.run_registered("hello", {"m": "x"})
        self.assertEqual(raised.exception.code, "registry_hash_mismatch")

        self.write([self.entry(permissions=["read"], version="2.1")])
        result = await se.run_registered("hello", {"m": "ok"})
        self.assertTrue(result["ok"], result)
        self.assertIn("meta:--m:ok", result["output"])
        self.assertEqual(result["version"], "2.1")
        self.assertEqual(result["permissions"], ["read"])
        self.assertEqual(result["platform"], [se.current_platform()])
        self.assertEqual(result["version_digest"], digest_of(self.hello))
        self.assertEqual(result["cancel"], {"grace_s": 3})

    async def test_defaults_are_added_for_legacy_entries(self):
        legacy = {"name": "hello", "file": "hello.sh", "type": "shell",
                  "hosts": ["localhost"], "timeout_s": 10, "params_schema": ["--m"]}
        self.write([legacy])
        loaded = se.get_entry("hello")
        self.assertEqual(loaded["platform"], ["linux"])
        self.assertEqual(loaded["permissions"], ["read"])
        self.assertEqual(loaded["cancel"], {"grace_s": 5})
        self.assertEqual(loaded["version"], 1)

    async def test_target_dirs_must_exist_and_not_be_symlinks(self):
        self.write([self.entry(target_dirs=[str(self.root / "missing")])])
        with self.assertRaises(se.ScriptRegistryError) as raised:
            await se.run_registered("hello", {})
        self.assertEqual(raised.exception.code, "target_dir_unavailable")

        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        self.write([self.entry(target_dirs=[str(link)])])
        with self.assertRaises(se.ScriptRegistryError) as raised:
            await se.run_registered("hello", {})
        self.assertEqual(raised.exception.code, "target_dir_unavailable")

    async def test_cancel_terminates_only_the_registered_process_group(self):
        work = self.root / "work"
        work.mkdir()
        pid_file = work / "pid.txt"
        script = self.registry / "slow.sh"
        script.write_text(f'#!/bin/sh\necho $$ > "{pid_file}"\nexec sleep 30\n')
        script.chmod(0o755)
        self.write([self.entry(name="slow", file="slow.sh",
                               permissions=["read", "write"], target_dirs=[str(work)],
                               sha256=digest_of(script), timeout_s=30)])
        cancel = asyncio.Event()
        from unittest import mock
        with mock.patch.object(se, "docker_image", return_value=""):
            task = asyncio.create_task(se.run_registered("slow", {}, cancel=cancel))
            await asyncio.sleep(0.6)
            cancel.set()
            result = await asyncio.wait_for(task, 15)
        self.assertTrue(result["cancelled"], result)
        self.assertFalse(result["ok"])
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text().strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_invalid_entries_are_dropped_without_blocking_valid_ones(self):
        self.write([
            {"name": "evil", "file": "../evil.sh", "type": "shell"},
            self.entry(),
        ])
        names = [item["name"] for item in se.load_registry()]
        self.assertEqual(names, ["hello"])
        self.assertIsNone(se.get_entry("evil"))


if __name__ == "__main__":
    unittest.main()
