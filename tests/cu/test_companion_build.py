"""构建产物的签名决策测试：没有验签通过就不能出现生产包名。"""
import contextlib
import io
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
        # 这些是 Windows 假产物，Linux 上跑不了；本类只验“验签→改名→回执”这条链。
        patched = patch.object(build, "run_selfcheck", return_value=(True, []))
        patched.start()
        self.addCleanup(patched.stop)

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


class SelfCheckGateTests(unittest.TestCase):
    """构建必须让包自己跑一遍 EI 自检：读不到、跑不齐、报错都不得出回执。

    这里用真可执行的文件（shell 脚本）当产物，所以验的是“真的执行并解析结果”，
    不是对 mock 的返回值做断言。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def artifact(self, stdout, rc=0, suffix=""):
        path = self.root / ("fake-companion" + suffix)
        payload = json.dumps(stdout)
        path.write_text("#!/bin/sh\nprintf '%s'\nexit %d\n" % (payload.replace("'", "'\\''"), rc))
        path.chmod(0o755)
        return path

    def good_payload(self):
        return {"passed": list(build.SELCHECK_STEPS), "failed": [],
                "window_scoped": False, "messages": 23}

    def test_passes_only_when_all_steps_and_rc_zero(self):
        ok, problems = build.run_selfcheck(self.artifact(self.good_payload()))
        self.assertEqual((ok, problems), (True, []))

    def test_missing_artifact_is_not_a_pass(self):
        ok, problems = build.run_selfcheck(self.root / "nope")
        self.assertFalse(ok)
        self.assertEqual(problems, ["selfcheck_artifact_missing"])

    def test_nonzero_rc_is_refused(self):
        ok, problems = build.run_selfcheck(self.artifact(self.good_payload(), rc=1))
        self.assertFalse(ok)
        self.assertEqual(problems, ["selfcheck_rc_1"])

    def test_unparsable_output_is_refused(self):
        path = self.root / "garbage"
        path.write_text("#!/bin/sh\necho not-json\nexit 0\n")
        path.chmod(0o755)
        ok, problems = build.run_selfcheck(path)
        self.assertFalse(ok)
        self.assertEqual(problems, ["selfcheck_unparsable_output"])

    def test_partial_steps_are_refused(self):
        payload = dict(self.good_payload(), passed=list(build.SELCHECK_STEPS[:3]))
        ok, problems = build.run_selfcheck(self.artifact(payload))
        self.assertFalse(ok)
        self.assertEqual(problems, ["selfcheck_incomplete"])

    def test_reported_failure_is_refused(self):
        # 步骤名跑齐但其中一步报了错：只能当不通过（不能用“跑了八步”掩盖失败）。
        payload = dict(self.good_payload(), failed=["gate_not_emulating: boom"])
        ok, problems = build.run_selfcheck(self.artifact(payload))
        self.assertFalse(ok)
        self.assertEqual(problems, ["selfcheck_failed"])

    def test_claimed_window_scope_is_refused(self):
        """EI 协议里没有窗口对象；自检要是报“拿到了”，只能当实现错了。"""
        payload = dict(self.good_payload(), window_scoped=True)
        ok, problems = build.run_selfcheck(self.artifact(payload))
        self.assertFalse(ok)
        self.assertEqual(problems, ["selfcheck_claims_window_scope"])

    def test_timeout_is_refused(self):
        path = self.root / "slow"
        path.write_text("#!/bin/sh\nsleep 5\n")
        path.chmod(0o755)
        ok, problems = build.run_selfcheck(path, timeout=0.2)
        self.assertFalse(ok)
        self.assertEqual(problems, ["selfcheck_timeout"])

    def test_finish_writes_no_receipt_when_selfcheck_fails(self):
        """自检不通过时：不出回执、不改名、不留下“看起来可交付”的目录。"""
        files, staged = build.staged_files(self.root / "dist", "posix")
        files["companion"].parent.mkdir(parents=True, exist_ok=True)
        for path in files.values():
            path.write_text("#!/bin/sh\nexit 1\n")
            path.chmod(0o755)
        plan = build.build_plan(True, None, "posix")
        self.assertIsNone(build.finish(self.root, plan, "2.0.2", "posix"))
        self.assertFalse((staged / "build-receipt.json").exists())
        # 显式跳过才能出产物（交付时就知道这个包没预检过）。
        target = build.finish(self.root, plan, "2.0.2", "posix", skip_selfcheck=True)
        self.assertEqual(target, staged)
        self.assertTrue((staged / "build-receipt.json").is_file())


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

    def test_test_flag_reads_receipt_only(self):
        self.assertIsNone(build.read_test_flag(self.root))
        self._receipt(signed=False, verified=False, test_only=True)
        self.assertTrue(build.read_test_flag(self.root))
        self._receipt()
        self.assertFalse(build.read_test_flag(self.root))
        (self.root / "build-receipt.json").write_text("[1,2]")
        self.assertIsNone(build.read_test_flag(self.root))


class VerifyReleaseCliWordingTests(unittest.TestCase):
    """测试包核验通过时不得对用户说“可发布”——这是会被误当正式发布物的入口。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "companion.exe").write_bytes(b"MZrelease")
        (self.root / "build-receipt.json").write_text(json.dumps({
            "schema": 1, "version": "2.0.2", "signed": False, "verified": False,
            "test_only": True, "thumbprint": "",
            "files": {"companion": {"file": "companion.exe",
                                    "sha256": build.digest(self.root / "companion.exe")}}}))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = build.main(["--verify-release", str(self.root)] + list(argv))
        return code, out.getvalue()

    def test_test_package_wording_keeps_release_forbidden(self):
        code, text = self._run("--allow-test-package")
        self.assertEqual(code, 0)
        self.assertNotIn("产物可发布", text)
        self.assertIn("未签名测试包", text)
        self.assertIn("不得发布", text)

    def test_without_ack_still_refused_with_release_wording(self):
        code, text = self._run()
        self.assertEqual(code, 1)
        self.assertIn("产物不可发布", text)
        self.assertIn("unsigned_test_package", text)

    def test_selfcheck_bypass_is_opt_in_only(self):
        """默认必须跑包内自检：“不跑”只能是一个写进 help 的显式选项，
        不能是“没装显示环境就静默跳过”（否则交付一个未预检的包没人知道）。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            build.main(["--help"])
        text = out.getvalue()
        self.assertIn("--skip-native-selfcheck", text)
        self.assertIn("不算已预检", text)


if __name__ == "__main__":
    unittest.main()
