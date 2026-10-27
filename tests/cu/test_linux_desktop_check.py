"""Linux 验收清单的判定逻辑与实测探针测试：不许跳步、不许默认通过。"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "linux_desktop_check.py"
spec = importlib.util.spec_from_file_location("linux_desktop_check", SCRIPT)
checklist = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checklist)


def row(step_id, status, kind="auto", spec="76"):
    return {"id": step_id, "spec": spec, "kind": kind, "status": status, "detail": ""}


def doctor_report(**overrides):
    body = {
        "version": "0.0.0", "capabilities": ["desktop.act", "desktop.inspect",
                                             "desktop.request_control", "file.read"],
        "unavailable": {"desktop.screen": "linux_x11_wayland_desktop_not_accepted",
                        "headed_login": "linux_headed_login_not_implemented"},
        "desktop_enabled": True,
        "desktop": {"enabled": True, "reason": "",
                    "checks": {name: True for name in checklist.LINUX_CHECKS},
                    "uia": {"windows": 3, "tray": False, "xtest": True, "xss": True}},
    }
    body.update(overrides)
    return body


class VerdictTests(unittest.TestCase):
    def test_all_expected_steps_pass_is_the_only_way_to_be_accepted(self):
        verdict = checklist.verdict([row("doctor", "pass"), row("tray", "pass")],
                                    expected=["doctor", "tray"])
        self.assertTrue(verdict["accepted"])
        self.assertEqual(verdict["failed"], [])

    def test_skipped_step_is_not_accepted(self):
        verdict = checklist.verdict([row("doctor", "pass"), row("lock_screen", "skip", "manual")],
                                    expected=["doctor", "lock_screen"])
        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["failed"], ["lock_screen"])

    def test_missing_and_duplicated_steps_are_not_accepted(self):
        verdict = checklist.verdict([row("doctor", "pass"), row("doctor", "pass")],
                                    expected=["doctor", "hotkey"])
        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["duplicated"], ["doctor"])
        self.assertEqual(verdict["missing"], ["hotkey"])

    def test_default_expects_every_spec_step(self):
        verdict = checklist.verdict([])
        self.assertFalse(verdict["accepted"])
        self.assertEqual(verdict["missing"], [step[0] for step in checklist.STEPS])


class MarkdownTests(unittest.TestCase):
    def test_report_states_failure_steps(self):
        report = {"steps": [{"id": "hotkey", "spec": "53", "kind": "auto",
                             "status": "fail", "detail": "抓取失败"}],
                  "verdict": checklist.verdict([])}
        text = checklist.markdown(report)
        self.assertIn("| hotkey | 53 | auto | fail | 抓取失败 |", text)
        self.assertIn("未通过", text)


class DoctorTests(unittest.TestCase):
    def run_doctor(self, report):
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr="")
        with mock.patch.object(checklist.subprocess, "run", return_value=completed):
            return checklist.probe_doctor(["companion"])

    def test_consistent_linux_report_passes(self):
        ok, detail = self.run_doctor(doctor_report())
        self.assertTrue(ok, detail)
        self.assertIn("checks=6/6", detail)

    def test_declaring_window_capture_is_refused(self):
        report = doctor_report()
        report["capabilities"] = report["capabilities"] + ["desktop.screen"]
        ok, detail = self.run_doctor(report)
        self.assertFalse(ok)
        self.assertIn("desktop.screen", detail)

    def test_undeclared_and_unexplained_capability_is_refused(self):
        report = doctor_report()
        report["capabilities"] = ["desktop.inspect"]
        ok, detail = self.run_doctor(report)
        self.assertFalse(ok)
        self.assertIn("desktop.act", detail)

    def test_declared_capability_with_unavailable_reason_is_refused(self):
        """同一个能力不能既声明可用又说明不可用：doctor 是远端唯一的判断依据。"""
        report = doctor_report()
        report["unavailable"] = {**report["unavailable"],
                                 "desktop.act": "linux_x11_wayland_desktop_not_accepted"}
        ok, detail = self.run_doctor(report)
        self.assertFalse(ok)
        self.assertIn("desktop.act", detail)

    def test_disabled_desktop_is_refused(self):
        report = doctor_report()
        report["desktop"] = {"enabled": False, "reason": "desktop_acceptance_required",
                            "checks": {}, "uia": {}}
        ok, detail = self.run_doctor(report)
        self.assertFalse(ok)
        self.assertIn("desktop_acceptance_required", detail)

    def test_incomplete_preflight_is_refused(self):
        report = doctor_report()
        report["desktop"]["checks"]["injection"] = False
        ok, detail = self.run_doctor(report)
        self.assertFalse(ok)
        self.assertIn("injection", detail)

    def test_failed_doctor_process_is_refused(self):
        completed = SimpleNamespace(returncode=1, stdout="", stderr="Companion 已停止：x")
        with mock.patch.object(checklist.subprocess, "run", return_value=completed):
            ok, detail = checklist.probe_doctor(["companion"])
        self.assertFalse(ok)
        self.assertIn("Companion 已停止", detail)


class AskTests(unittest.TestCase):
    def test_no_answer_is_not_a_pass(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(checklist.ask("approve", "标准")[0], "skip")

    def test_answer_without_evidence_is_not_a_pass(self):
        answers = iter(["y", ""])
        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(answers)):
            self.assertEqual(checklist.ask("approve", "标准"), ("skip", "无证据"))

    def test_answer_with_evidence_keeps_verdict(self):
        answers = iter(["y", "task-1 60 秒租约"])
        with mock.patch("builtins.input", side_effect=lambda _prompt="": next(answers)):
            self.assertEqual(checklist.ask("approve", "标准"),
                             ("pass", "task-1 60 秒租约"))


class AttendedTests(unittest.TestCase):
    """人工步骤证据：只接受 Linux、同一显示器、且只覆盖 manual 步骤的记录。"""

    def write(self, body):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(body, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def record(self, **overrides):
        body = {"schema": 1, "platform": "linux", "display": os.environ.get("DISPLAY", ":0"),
                "steps": [{"id": "approve", "status": "pass", "detail": "租约 60 秒"}]}
        body.update(overrides)
        return body

    def test_linux_record_is_accepted(self):
        path = self.write(self.record())
        with mock.patch.dict(os.environ, {"DISPLAY": os.environ.get("DISPLAY", ":0")}):
            recorded = checklist.attended_steps(path)
        self.assertEqual(sorted(recorded), ["approve"])
        self.assertEqual(recorded["approve"]["status"], "pass")

    def test_windows_record_is_refused(self):
        path = self.write(self.record(platform="windows"))
        with self.assertRaises(SystemExit):
            checklist.attended_steps(path)

    def test_other_display_is_refused(self):
        path = self.write(self.record(display=":77"))
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
            with self.assertRaises(SystemExit):
                checklist.attended_steps(path)

    def test_auto_step_cannot_be_smuggled_in(self):
        path = self.write(self.record(steps=[{"id": "doctor", "status": "pass"}]))
        with self.assertRaises(SystemExit):
            checklist.attended_steps(path)

    def test_duplicated_and_unknown_status_are_handled(self):
        path = self.write(self.record(steps=[{"id": "approve", "status": "pass"},
                                             {"id": "approve", "status": "pass"}]))
        with self.assertRaises(SystemExit):
            checklist.attended_steps(path)
        skipped = self.write(self.record(steps=[{"id": "approve", "status": "skip"}]))
        self.assertEqual(checklist.attended_steps(skipped), {})

    def test_fail_is_kept_and_missing_falls_back_to_asking(self):
        attended = {"approve": {"id": "approve", "status": "fail", "detail": "点了允许却没租约"}}
        self.assertEqual(checklist.manual_result("approve", "50", attended)["status"], "fail")
        self.assertIsNone(checklist.manual_result("reject", "50", attended))


class CompanionCommandTests(unittest.TestCase):
    def test_missing_executable_is_refused(self):
        with self.assertRaises(SystemExit):
            checklist.companion_command("/nonexistent/companion")

    def test_python_interpreter_gets_module_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "python"
            python.write_text("", encoding="utf-8")
            command = checklist.companion_command(str(python), config="/tmp/cfg.json")
        self.assertEqual(command, [str(python), "-m", "companion", "--config", "/tmp/cfg.json"])


def _free_display():
    for number in range(90, 160):
        if not os.path.exists("/tmp/.X%d-lock" % number) \
                and not os.path.exists("/tmp/.X11-unix/X%d" % number):
            return number
    return None


def _tk_ready():
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(shutil.which("Xvfb") and _tk_ready()
                     and (ROOT / "scripts" / "x11_target_window.py").exists(),
                     "需要 Xvfb、tkinter 与验收目标窗口")
class RealProbeTest(unittest.TestCase):
    """在真实 Xvfb 上跑 auto 探针：探针本身必须能分辨通过与不通过。"""

    @classmethod
    def setUpClass(cls):
        number = _free_display()
        if number is None:
            raise unittest.SkipTest("no free X display")
        cls.display = ":%d" % number
        cls.env = {**os.environ, "DISPLAY": cls.display, "XDG_SESSION_TYPE": "x11",
                   "WAYLAND_DISPLAY": ""}
        cls.xvfb = subprocess.Popen(
            ["Xvfb", cls.display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        if not cls._wait(lambda: subprocess.run(["xdpyinfo"], env=cls.env,
                                                stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL).returncode == 0, 15):
            cls.tearDownClass()
            raise unittest.SkipTest("Xvfb 未能就绪")
        cls.tmp = tempfile.mkdtemp(prefix="zops-linux-check-")
        cls.log = os.path.join(cls.tmp, "events.log")
        cls.target = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "x11_target_window.py"),
             "--log", cls.log, "--seconds", "120"],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        cls._wait(cls._ready, 20)

    @classmethod
    def _wait(cls, predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    @classmethod
    def _ready(cls):
        try:
            with open(cls.log, encoding="utf-8") as stream:
                return "READY" in stream.read()
        except FileNotFoundError:
            return False

    @classmethod
    def tearDownClass(cls):
        for name in ("target", "xvfb"):
            process = getattr(cls, name, None)
            if process is not None:
                process.terminate()
                try:
                    process.wait(5)
                except Exception:
                    process.kill()
        tmp = getattr(cls, "tmp", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_auto_probe_passes_on_a_real_x_server(self):
        for step, spec_row, kind, _criteria in checklist.STEPS:
            if kind != "auto" or step == "doctor":
                continue
            ok, detail = checklist.PROBES[step]()
            self.assertTrue(ok, f"{step}: {detail}")
            self.assertTrue(detail)

    def test_session_probe_rejects_wayland(self):
        with mock.patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}):
            ok, detail = checklist.probe_session()
        self.assertFalse(ok)
        self.assertIn("Wayland", detail)

    def test_session_probe_rejects_wayland_display_without_session_type(self):
        env = {key: value for key, value in os.environ.items()
               if key not in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY")}
        env.update({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"})
        with mock.patch.dict(os.environ, env, clear=True):
            ok, detail = checklist.probe_session()
        self.assertFalse(ok)
        self.assertIn("Wayland", detail)

    def test_session_probe_rejects_missing_display(self):
        env = {key: value for key, value in os.environ.items() if key != "DISPLAY"}
        with mock.patch.dict(os.environ, env, clear=True):
            ok, detail = checklist.probe_session()
        self.assertFalse(ok)
        self.assertIn("DISPLAY", detail)

    def test_tray_probe_reports_absent_host_honestly(self):
        ok, detail = checklist.probe_tray()
        self.assertTrue(ok, detail)
        self.assertIn("tray_host=False", detail)

    def test_doctor_fails_without_local_acceptance(self):
        """没写本机验收记录时 doctor 必须报“桌面不可用”，而不是提前通过。"""
        config = str(Path(self.tmp) / "config.json")
        command = checklist.companion_command(sys.executable, config=config)
        created = subprocess.run(command[:3] + ["--server", "https://example.test",
                                                "--name", "验收设备", "--init"] + command[3:],
                                 env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(created.returncode, 0, created.stderr[-300:])
        ok, detail = checklist.probe_doctor(command)
        self.assertFalse(ok)
        self.assertIn("desktop_acceptance_required", detail)


if __name__ == "__main__":
    unittest.main()
