"""离线自检（`companion/selfcheck.py`）的回归：自检必须真自检，不能永远报“通过”。

这里的价值不是覆盖率，而是交付前的那一句“包里到底能不能跑”：
冻结包里没有仓库源码，也没有真合成器，自检是它在交付机器上唯一的自证手段。
"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from companion import eis, selfcheck
from companion.selfcheck import run

EXPECTED = ["handshake", "seat", "device", "gate_not_emulating", "inject",
            "window_scope_false", "gate_paused", "absolute_needs_interface_and_region"]


class SelfCheckTest(unittest.TestCase):
    def test_full_run_passes_every_step(self):
        result = run()
        self.assertEqual(result["failed"], [], "自检步骤不应失败")
        self.assertEqual(result["passed"], EXPECTED)
        self.assertFalse(result["window_scoped"], "自检必须如实报告：没有窗口作用域")
        self.assertGreaterEqual(result["messages"], 20, "假合成器应逐条收到客户端消息")

    def test_gate_failure_is_reported_not_swallowed(self):
        """拆掉统一注入闸门（`_emit` 里的 `require(self.emulating, ...)`），自检必须报失败。

        这里直接注掉一个空实现：如果自检自己不再核对闸门，“未 start_emulating 也能注入”
        就会被当成正常——那正是这条用例要拦住的假通过。
        """
        original = eis.EIS._emit
        eis.EIS._emit = lambda self, interface, request, *values: None
        try:
            result = run(["gate_not_emulating"])
        finally:
            eis.EIS._emit = original
        self.assertIn("gate_not_emulating", [item.split(":")[0] for item in result["failed"]],
                      "闸门被拆掉时自检必须报错")
        self.assertNotIn("gate_not_emulating", result["passed"])

    def test_pipeline_stops_after_a_fatal_step(self):
        """握手都起不来时不该继续跑后面的步骤（否则会刷一串无关失败）。"""
        original = eis.EIS.handshake
        eis.EIS.handshake = lambda self, *a, **k: (_ for _ in ()).throw(
            eis.Denied("eis_handshake_timeout"))
        try:
            result = run()
        finally:
            eis.EIS.handshake = original
        self.assertEqual(result["passed"], [])
        self.assertEqual([item.split(":")[0] for item in result["failed"]], ["handshake"])

    def test_cli_reports_json_and_exit_code(self):
        proc = subprocess.run([sys.executable, "-m", "companion", "--eis-selfcheck"],
                              input="", capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["failed"], [])
        self.assertEqual(len(payload["passed"]), len(EXPECTED))
        self.assertFalse(payload["window_scoped"])


if __name__ == "__main__":
    unittest.main()
