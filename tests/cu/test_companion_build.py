"""构建产物的签名决策测试：没有验签通过就不能出现生产包名。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from companion import build

SHA1 = "a1b2c3d4e5f60718293a4b5c6d7e8f901a2b3c4d"


class PlanTests(unittest.TestCase):
    def test_unsigned_build_requires_explicit_ack(self):
        with self.assertRaises(ValueError) as caught:
            build.build_plan(False, None, "nt")
        self.assertEqual(str(caught.exception), "signing_material_required")

    def test_ack_alone_is_named_as_unsigned_test_package(self):
        plan = build.build_plan(True, None, "posix")
        self.assertEqual(plan, {"signed": False, "suffix": "unsigned-test", "thumbprint": ""})

    def test_signed_build_conflicts_with_unsigned_ack(self):
        with self.assertRaises(ValueError):
            build.build_plan(True, SHA1, "nt")

    def test_signing_outside_windows_certificate_store_is_refused(self):
        # 非 Windows 无法 signtool 验签, 拒绝而不是产出"看起来已签名"的包。
        for platform in ("posix", "darwin"):
            with self.assertRaises(ValueError) as caught:
                build.build_plan(False, SHA1, platform)
            self.assertEqual(str(caught.exception), "signing_requires_windows_certificate_store")

    def test_thumbprint_must_be_hex_and_complete(self):
        for value in ("abc", "z" * 40, SHA1[:-1]):
            with self.assertRaises(ValueError):
                build.build_plan(False, value, "nt")

    def test_thumbprint_is_normalized_before_signing(self):
        plan = build.build_plan(False, "  " + SHA1.lower() + " ", "nt")
        self.assertTrue(plan["signed"])
        self.assertEqual(plan["thumbprint"], SHA1.upper())


class CommandTests(unittest.TestCase):
    def test_sign_command_uses_store_thumbprint_and_rfc3161(self):
        argv = build.sign_command("C:/out/companion.exe", SHA1, "https://ts.example/rfc3161")
        self.assertEqual(argv[:2], ["signtool", "sign"])
        self.assertIn("/sha1", argv)
        self.assertEqual(argv[argv.index("/sha1") + 1], SHA1)
        self.assertEqual(argv[argv.index("/fd") + 1], "sha256")
        self.assertEqual(argv[argv.index("/tr") + 1], "https://ts.example/rfc3161")
        self.assertEqual(argv[argv.index("/td") + 1], "sha256")
        self.assertEqual(argv[-1], "C:/out/companion.exe")

    def test_verify_command_checks_entire_chain(self):
        argv = build.verify_command("C:/out/companion.exe")
        self.assertEqual(argv[:2], ["signtool", "verify"])
        self.assertTrue({"/pa", "/all"} <= set(argv))


class FailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.files, self.staged = build.staged_files(self.root, "nt")
        for path in self.files.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"MZ" + path.name.encode())

    def tearDown(self):
        self.tmp.cleanup()

    def test_staged_names_are_unmistakably_unsigned(self):
        self.assertEqual(self.staged.name, "companion-unsigned-test")
        self.assertEqual({p.name for p in self.files.values()},
                         {"companion-unsigned-test.exe", "companion-native-unsigned-test.exe"})

    def test_sign_failure_keeps_unsigned_names(self):
        plan = build.build_plan(False, SHA1, "nt")
        with patch.object(build, "_run", return_value=1):
            self.assertFalse(build._sign_and_verify(self.files, plan))
        self.assertTrue(all(path.exists() for path in self.files.values()))
        self.assertIn("unsigned-test", self.staged.name)

    def test_verify_failure_keeps_unsigned_names(self):
        plan = build.build_plan(False, SHA1, "nt")
        results = iter([0, 0, 1])  # 两个签名成功, 第一个验签失败
        with patch.object(build, "_run", side_effect=lambda argv: next(results)):
            self.assertFalse(build._sign_and_verify(self.files, plan))
        self.assertTrue(self.files["companion"].exists())

    def test_publish_only_after_verification_renames_to_release_names(self):
        final = build.publish(self.files, self.staged, "2.0.2")
        self.assertEqual(final.name, "companion-2.0.2")
        self.assertEqual({p.name for p in final.iterdir()},
                         {"companion.exe", "companion-native.exe"})

    def test_publish_refuses_to_overwrite_existing_release_dir(self):
        (self.root / "companion-2.0.2").mkdir()
        with self.assertRaises(FileExistsError):
            build.publish(self.files, self.staged, "2.0.2")

    def test_receipt_records_test_only_and_digests(self):
        plan = build.build_plan(True, None, "nt")
        record = build.receipt(plan, "2.0.2", self.files, False, created="2026-01-01T00:00:00Z")
        self.assertFalse(record["signed"])
        self.assertFalse(record["verified"])
        self.assertTrue(record["test_only"])
        self.assertEqual(record["thumbprint"], "")
        self.assertEqual(set(record["files"]), set(self.files))
        self.assertTrue(all(len(value["sha256"]) == 64 for value in record["files"].values()))
        self.assertEqual(json.loads(json.dumps(record))["created"], "2026-01-01T00:00:00Z")

    def test_receipt_records_the_real_file_name_not_the_logical_one(self):
        # 未签名包没有改名, 回执必须能和磁盘上的文件名对上, 否则无法核验。
        plan = build.build_plan(True, None, "nt")
        record = build.receipt(plan, "2.0.2", self.files, False)
        self.assertEqual(record["files"]["companion"]["file"], "companion-unsigned-test.exe")

    def test_signed_receipt_is_verified_only_when_both_steps_passed(self):
        plan = build.build_plan(False, SHA1, "nt")
        record = build.receipt(plan, "2.0.2", self.files, True)
        self.assertTrue(record["signed"] and record["verified"])
        self.assertFalse(record["test_only"])
        self.assertEqual(record["thumbprint"], SHA1.upper())


class FinishTests(unittest.TestCase):
    """构建收尾：测试包直接出回执；签名包必须验签通过才改成生产名字。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        files, _staged = build.staged_files(self.root / "dist", "nt")
        files["companion"].parent.mkdir(parents=True, exist_ok=True)
        for path in files.values():
            path.write_bytes(b"MZ" + path.name.encode())

    def tearDown(self):
        self.tmp.cleanup()

    def test_unsigned_finish_writes_test_only_receipt(self):
        plan = build.build_plan(True, None, "nt")
        target = build.finish(self.root, plan, "2.0.2", "nt")
        self.assertEqual(target, self.root / "dist" / "companion-unsigned-test")
        record = json.loads((target / "build-receipt.json").read_text())
        self.assertTrue(record["test_only"])
        self.assertEqual(set(record["files"]), {"companion", "companion-native"})

    def test_verified_finish_produces_release_dir_with_matching_digests(self):
        plan = build.build_plan(False, SHA1, "nt")
        with patch.object(build, "_run", return_value=0):
            target = build.finish(self.root, plan, "2.0.2", "nt")
        self.assertEqual(target, self.root / "dist" / "companion-2.0.2")
        record = json.loads((target / "build-receipt.json").read_text())
        self.assertTrue(record["signed"] and record["verified"])
        self.assertEqual(set(record["files"]), {"companion", "companion-native"})
        for name, entry in record["files"].items():
            self.assertEqual(entry["file"], name + ".exe")
            self.assertEqual(build.digest(target / entry["file"]), entry["sha256"])
        self.assertEqual(build.verify_release(target), (True, []))

    def test_failed_verification_leaves_no_release_dir(self):
        plan = build.build_plan(False, SHA1, "nt")
        with patch.object(build, "_run", return_value=1):
            self.assertIsNone(build.finish(self.root, plan, "2.0.2", "nt"))
        self.assertFalse((self.root / "dist" / "companion-2.0.2").exists())
        self.assertFalse((self.root / "dist" / "companion-unsigned-test" / "build-receipt.json").exists())


class VerifyReleaseTests(unittest.TestCase):
    """发布核验：没有验签通过的回执，就不能把这个目录当生产包发出去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "companion.exe").write_bytes(b"MZrelease")

    def tearDown(self):
        self.tmp.cleanup()

    def _receipt(self, signed=True, verified=True, test_only=False, sha=None):
        (self.root / "build-receipt.json").write_text(json.dumps({
            "schema": 1, "version": "2.0.2", "signed": signed, "verified": verified,
            "test_only": test_only, "thumbprint": SHA1 if signed else "",
            "files": {"companion": {"file": "companion.exe",
                                     "sha256": sha or build.digest(self.root / "companion.exe")}}}))

    def test_signed_and_intact_package_is_releasable(self):
        self._receipt()
        self.assertEqual(build.verify_release(self.root), (True, []))

    def test_tampered_artifact_is_refused(self):
        self._receipt()
        (self.root / "companion.exe").write_bytes(b"MZtampered")
        ok, problems = build.verify_release(self.root)
        self.assertFalse(ok)
        self.assertIn("companion:digest_mismatch", problems)

    def test_missing_artifact_is_refused(self):
        self._receipt()
        (self.root / "companion.exe").unlink()
        self.assertIn("companion:file_missing", build.verify_release(self.root)[1])

    def test_unsigned_test_package_is_not_releasable(self):
        self._receipt(signed=False, verified=False, test_only=True)
        ok, problems = build.verify_release(self.root)
        self.assertFalse(ok)
        self.assertIn("unsigned_test_package", problems)
        # 明确声明只看测试包时才放行, 且仍要核对摘要。
        self.assertTrue(build.verify_release(self.root, require_signed=False)[0])

    def test_missing_or_unreadable_receipt_is_refused(self):
        self.assertEqual(build.verify_release(self.root)[1], ["build_receipt_missing"])
        (self.root / "build-receipt.json").write_text("{not json")
        self.assertEqual(build.verify_release(self.root)[1], ["build_receipt_unreadable"])

    def test_unknown_schema_is_refused(self):
        (self.root / "build-receipt.json").write_text(json.dumps({"schema": 2, "files": {}}))
        self.assertEqual(build.verify_release(self.root)[1], ["unsupported_receipt_schema"])


if __name__ == "__main__":
    unittest.main()
