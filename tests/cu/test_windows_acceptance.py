"""Windows 验收清单的判定逻辑测试：只在本机校验“不许跳步、不许默认通过”。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "windows_acceptance.py"
spec = importlib.util.spec_from_file_location("windows_acceptance", SCRIPT)
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


def row(step_id, status, kind="auto", spec="82"):
    return {"id": step_id, "spec": spec, "kind": kind, "status": status, "detail": ""}


class FakeUser32:
    def __init__(self, focus=0x1234, clip=7):
        self.focus, self.clip = focus, clip

    def GetForegroundWindow(self):
        return self.focus

    def GetClipboardSequenceNumber(self):
        return self.clip


class VerdictTests(unittest.TestCase):
    def test_all_expected_steps_pass_is_the_only_way_to_be_accepted(self):
        verdict = acceptance.verdict([row("doctor", "pass"), row("tray", "pass", kind="manual")],
                                     expected=["doctor", "tray"])
        self.assertTrue(verdict["accepted"])
        self.assertEqual(verdict["failed"], [])

    def test_skipped_step_is_not_accepted(self):
        verdict = acceptance.verdict([row("doctor", "pass"), row("lock_screen", "skip", "manual")],
                                     expected=["doctor", "lock_screen"])
        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["failed"], ["lock_screen"])

    def test_failed_step_is_not_accepted(self):
        verdict = acceptance.verdict([row("hotkey", "fail"), row("doctor", "pass")],
                                     expected=["doctor", "hotkey"])
        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["passed"], ["doctor"])

    def test_empty_results_are_not_accepted(self):
        self.assertFalse(acceptance.verdict([])["accepted"])

    def test_missing_spec_item_blocks_acceptance(self):
        # 少了步骤也必须是“未通过”, 不能因为剩下的都 pass 就放行。
        verdict = acceptance.verdict([row("doctor", "pass")],
                                     expected=["doctor", "tray", "uia_apps"])
        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["missing"], ["tray", "uia_apps"])
        self.assertEqual(set(verdict["failed"]), {"tray", "uia_apps"})

    def test_default_expectation_is_the_whole_checklist(self):
        verdict = acceptance.verdict([row("doctor", "pass")])
        self.assertFalse(verdict["accepted"])
        self.assertEqual(len(verdict["missing"]), len(acceptance.STEPS) - 1)

    def test_duplicated_step_is_not_accepted(self):
        verdict = acceptance.verdict([row("doctor", "pass"), row("doctor", "pass")],
                                     expected=["doctor"])
        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["duplicated"], ["doctor"])


class StepRegistryTests(unittest.TestCase):
    def test_spec_82_items_are_all_covered(self):
        ids = {step[0] for step in acceptance.STEPS}
        required = {"doctor", "hotkey", "background_focus", "background_clipboard", "cancel_child",
                    "approve", "reject", "timeout", "revoke", "lock_screen",
                    "typing_interference", "tray", "uia_apps"}
        self.assertEqual(ids, required)
        self.assertEqual(len(ids), len(acceptance.STEPS))

    def test_every_step_cites_a_spec_item_and_has_criteria(self):
        for step_id, spec, kind, criteria in acceptance.STEPS:
            self.assertIn(kind, acceptance.KINDS)
            self.assertTrue(spec.strip())
            self.assertGreater(len(criteria), 6)

    def test_background_checks_are_automatic_and_interference_is_manual(self):
        kinds = {step[0]: step[2] for step in acceptance.STEPS}
        self.assertEqual(kinds["background_focus"], "auto")
        self.assertEqual(kinds["background_clipboard"], "auto")
        # 真人打字/切窗无法自动断言, 必须由人确认。
        self.assertEqual(kinds["typing_interference"], "manual")
        # 取消子进程必须实测 pid 是否消失, 不能只凭一句“已经停了”。
        self.assertEqual(kinds["cancel_child"], "measure")


class InteractivityTests(unittest.TestCase):
    def test_no_answer_means_skip_not_pass(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertEqual(acceptance.ask("tray", "托盘图标可见"), ("skip", "非交互运行，未确认"))

    def test_answer_without_evidence_is_not_done(self):
        with patch("builtins.input", side_effect=["y", "   "]):
            self.assertEqual(acceptance.ask("tray", "托盘图标可见"), ("skip", "无证据"))

    def test_affirmative_and_negative_answers_keep_evidence(self):
        with patch("builtins.input", side_effect=[" Y ", "task-9f2c"]):
            self.assertEqual(acceptance.ask("tray", "托盘图标可见"), ("pass", "task-9f2c"))
        with patch("builtins.input", side_effect=["n", "对话框无按钮"]):
            self.assertEqual(acceptance.ask("tray", "托盘图标可见"), ("fail", "对话框无按钮"))

    def test_empty_answer_is_skip(self):
        with patch("builtins.input", return_value=""):
            self.assertEqual(acceptance.ask("tray", "托盘图标可见"), ("skip", "未执行"))

    def test_non_windows_host_refuses_to_run(self):
        with patch.object(acceptance, "os", SimpleNamespace(name="posix")):
            with self.assertRaises(SystemExit):
                acceptance.run("companion", "/tmp/never.json")


class MeasureTests(unittest.TestCase):
    """取消子进程：先确认 pid 在，取消了再实测它是否真的消失。"""

    def test_no_pid_means_skip_not_pass(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertEqual(acceptance.measure_cancel_child(), ("skip", "非交互运行，未确认"))
        with patch("builtins.input", return_value="abc"):
            self.assertEqual(acceptance.measure_cancel_child(), ("skip", "未提供 pid"))

    def test_system_pid_is_refused(self):
        with patch("builtins.input", return_value="4"):
            self.assertEqual(acceptance.measure_cancel_child()[0], "skip")

    def test_pid_must_exist_before_cancel_to_prove_anything(self):
        with patch("builtins.input", return_value="4242"), \
                patch.object(acceptance, "pid_exists", lambda _p: False):
            status, detail = acceptance.measure_cancel_child()
        self.assertEqual(status, "skip")
        self.assertIn("4242", detail)

    def test_disappearing_pid_is_the_only_pass(self):
        answers = iter(["4242", ""])
        existence = iter([True, False])
        with patch("builtins.input", lambda _p="": next(answers)), \
                patch.object(acceptance, "pid_exists", lambda _p: next(existence)):
            status, detail = acceptance.measure_cancel_child()
        self.assertEqual(status, "pass")
        self.assertIn("已不存在", detail)

    def test_surviving_pid_fails_the_step(self):
        with patch("builtins.input", side_effect=["4242", ""]), \
                patch.object(acceptance, "pid_exists", lambda _p: True), \
                patch.object(acceptance, "CANCEL_TIMEOUT", 0):
            status, detail = acceptance.measure_cancel_child()
        self.assertEqual(status, "fail")
        self.assertIn("仍存在", detail)


class ProbeDoctorTests(unittest.TestCase):
    """doctor 结论必须来自真实 JSON：能力声明、启动预检、UIA 实测都要对得上。"""

    def _probe(self, payload, code=0):
        body = payload if isinstance(payload, str) else json.dumps(payload)
        result = SimpleNamespace(returncode=code, stdout=body, stderr="")
        with patch.object(acceptance.subprocess, "run", lambda *a, **k: result):
            return acceptance.probe_doctor("companion.exe")

    def _desktop(self, **overrides):
        desktop = {"enabled": True, "reason": "",
                   "checks": {name: True for name in
                              acceptance.PREFLIGHT_CHECKS + ("native_dialog",)},
                   "uia": {"windows": 7, "tree_readable": True}}
        desktop.update(overrides)
        return desktop

    def _report(self, **overrides):
        report = {"capabilities": list(acceptance.DESKTOP_CAPABILITIES) + ["system.info"],
                  "unavailable": {}, "desktop": self._desktop()}
        report.update(overrides)
        return report

    def test_clean_report_passes_and_quotes_uia_windows(self):
        ok, detail = self._probe(self._report())
        self.assertTrue(ok)
        self.assertIn("uia_windows=7", detail)

    def test_desktop_capability_without_reason_is_rejected(self):
        ok, detail = self._probe(self._report(capabilities=["system.info"], unavailable={}))
        self.assertFalse(ok)
        self.assertIn("既未声明也未说明原因", detail)

    def test_declared_reason_but_disabled_is_reported_as_unavailable(self):
        reasons = {name: "desktop_acceptance_required" for name in acceptance.DESKTOP_CAPABILITIES}
        ok, detail = self._probe(self._report(capabilities=["system.info"], unavailable=reasons,
                                              desktop=self._desktop(enabled=False, checks={},
                                                                    reason="desktop_acceptance_required")))
        self.assertFalse(ok)
        self.assertIn("桌面不可用", detail)

    def test_preflight_gap_is_rejected(self):
        checks = {name: True for name in acceptance.PREFLIGHT_CHECKS}
        checks["tray"] = False
        ok, detail = self._probe(self._report(desktop=self._desktop(checks=checks)))
        self.assertFalse(ok)
        self.assertIn("tray", detail)

    def test_non_json_and_nonzero_exit_are_rejected(self):
        self.assertFalse(self._probe("not json")[0])
        self.assertFalse(self._probe(self._report(), code=3)[0])


class MarkdownTests(unittest.TestCase):
    def test_markdown_is_paste_ready(self):
        steps = [row("doctor", "pass"), row("lock_screen", "skip", "manual")]
        report = {"steps": steps, "verdict": acceptance.verdict(
            steps, expected=["doctor", "lock_screen"])}
        text = acceptance.markdown(report)
        self.assertIn("| doctor | 82 | auto | pass |", text)
        self.assertIn("结论：未通过（lock_screen）", text)

    def test_pipe_in_detail_cannot_break_table(self):
        entry = row("doctor", "pass")
        entry["detail"] = "a|b\nc"
        text = acceptance.markdown({"steps": [entry],
                                    "verdict": acceptance.verdict([entry], expected=["doctor"])})
        self.assertIn("a/b c", text)
        self.assertEqual(text.count("| doctor |"), 1)


class ReportTests(unittest.TestCase):
    def test_report_json_keeps_status_per_step(self):
        steps = [row("doctor", "pass"), row("tray", "skip", "manual")]
        report = {"schema": 1, "platform": "windows", "steps": steps,
                  "verdict": acceptance.verdict(steps, expected=["doctor", "tray"])}
        body = json.loads(json.dumps(report, ensure_ascii=False))
        self.assertEqual([item["status"] for item in body["steps"]], ["pass", "skip"])
        self.assertFalse(body["verdict"]["accepted"])


class RunTests(unittest.TestCase):
    """在非 Windows 主机上模拟一次完整运行，只验证编排与判定，不冒充实机结果。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exe = Path(self.tmp.name) / "companion.exe"
        self.exe.write_bytes(b"MZ")
        self.output = Path(self.tmp.name) / "report.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, ask_result=("pass", "task-1"), doctor=(True, "desktop_enabled=True")):
        with patch.object(acceptance, "os", SimpleNamespace(name="nt")), \
                patch.object(acceptance, "_windows", lambda: FakeUser32()), \
                patch.object(acceptance, "probe_doctor", lambda _c: doctor), \
                patch.object(acceptance, "probe_hotkey", lambda: (True, "ok")), \
                patch.object(acceptance, "probe_background",
                             lambda focus, clip: {"focus_same": True, "clipboard_same": True,
                                                  "focus": focus, "clipboard": clip}), \
                patch.object(acceptance, "ask", lambda _s, _c: ask_result), \
                patch.object(acceptance, "measure_cancel_child",
                             lambda: ("pass", "pid=4242 已不存在")):
            return acceptance.run(str(self.exe), str(self.output))

    def test_full_pass_run_writes_every_step_in_checklist_order(self):
        self.assertEqual(self._run(), 0)
        report = json.loads(self.output.read_text())
        self.assertEqual([item["id"] for item in report["steps"]],
                         [step[0] for step in acceptance.STEPS])
        self.assertTrue(report["verdict"]["accepted"])
        self.assertEqual(report["verdict"]["missing"], [])

    def test_unconfirmed_manual_step_fails_the_whole_run(self):
        self.assertEqual(self._run(ask_result=("skip", "无证据")), 1)
        report = json.loads(self.output.read_text())
        self.assertFalse(report["verdict"]["accepted"])
        self.assertIn("tray", report["verdict"]["failed"])

    def test_desktop_unavailable_makes_doctor_fail(self):
        self.assertEqual(self._run(doctor=(False, "桌面不可用：desktop_acceptance_required")), 1)
        report = json.loads(self.output.read_text())
        self.assertEqual(report["steps"][0]["status"], "fail")

    def test_missing_companion_is_refused(self):
        with patch.object(acceptance, "os", SimpleNamespace(name="nt")):
            with self.assertRaises(SystemExit):
                acceptance.run(str(Path(self.tmp.name) / "absent.exe"), str(self.output))


if __name__ == "__main__":
    unittest.main()
