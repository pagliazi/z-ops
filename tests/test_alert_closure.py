#!/usr/bin/env python3
"""闭环覆盖度评估测试：告警归类、分层统计与推荐顺序（纯函数，无副作用）。"""

import os
import sys
import unittest

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

from core import alert_closure as ac  # noqa: E402


def alarm(severity="major", checklist="", event_group="", summary="", **over):
    a = {"severity": severity, "checklist": checklist, "event_group": event_group,
         "summary": summary, "source": over.pop("source", "node-1"),
         "event_key": over.pop("event_key", "k1")}
    a.update(over)
    return a


def snap(*alarms):
    return {"timestamp": "2026-10-24T23:00:00+08:00", "alarms": list(alarms)}


class TestClassify(unittest.TestCase):
    def test_checklist_wins_over_content(self):
        got = ac.classify(alarm(checklist="SSL证书过期", event_group="BGP Status"))
        self.assertEqual(got["kind"], "certificate")
        self.assertEqual(got["tier"], "L1")
        self.assertEqual(got["matched_by"], "checklist")

    def test_backup_and_dns_checklists(self):
        self.assertEqual(ac.classify(alarm(checklist="VBR备份失败"))["kind"], "backup")
        self.assertEqual(ac.classify(alarm(checklist="A2C O365备份失败"))["kind"], "backup")
        self.assertEqual(ac.classify(alarm(checklist="DNS解析异常"))["kind"], "dns")

    def test_content_match_for_unmapped_alarm(self):
        got = ac.classify(alarm(event_group="BGP Status", summary="BGP neighbor down"))
        self.assertEqual(got["kind"], "network")
        self.assertEqual(got["tier"], "L2")
        self.assertEqual(got["matched_by"], "content")

    def test_auth_and_time(self):
        self.assertEqual(ac.classify(alarm(event_group="Generic Authentication"))["kind"], "auth")
        self.assertEqual(ac.classify(alarm(summary="NTP offset too large"))["kind"], "time")

    def test_unknown_falls_back_to_l3(self):
        got = ac.classify(alarm(event_group="[Generic Syslog]", summary="unclassified event"))
        self.assertEqual(got["kind"], "other")
        self.assertEqual(got["tier"], "L3")
        self.assertEqual(got["matched_by"], "default")


class TestCoverageReport(unittest.TestCase):
    def build(self):
        return snap(
            alarm(severity="critical", checklist="SSL证书过期", event_key="ssl-1"),
            alarm(severity="major", event_group="BGP Status", event_key="bgp-1"),
            alarm(severity="major", event_group="BGP Status", event_key="bgp-2"),
            alarm(severity="minor", event_group="BGP Status", event_key="bgp-2"),
            alarm(severity="minor", event_group="[Generic Syslog]", summary="misc"),
            alarm(severity="clear", event_group="BGP Status"),  # 非活跃，忽略
        )

    def test_active_filter_and_tier_split(self):
        rep = ac.coverage_report(self.build())
        self.assertEqual(rep["total_alarms"], 6)
        self.assertEqual(rep["total_active"], 5)
        self.assertEqual(rep["by_tier"]["L1"]["alarms"], 1)
        self.assertEqual(rep["by_tier"]["L2"]["alarms"], 3)
        self.assertEqual(rep["by_tier"]["L3"]["alarms"], 1)
        self.assertAlmostEqual(rep["by_tier"]["L2"]["ratio"], 60.0)
        self.assertEqual(rep["checklist_hit"], 1)
        self.assertAlmostEqual(rep["checklist_coverage"], 20.0)

    def test_sorting_uses_severity_weight(self):
        rep = ac.coverage_report(self.build())
        kinds = [r["kind"] for r in rep["by_kind"]]
        # network: 4+4+2=10，certificate: 8，other: 2 → network 排首位
        self.assertEqual(kinds[0], "network")
        self.assertEqual(rep["by_kind"][0]["alarms"], 3)
        self.assertEqual(rep["by_kind"][0]["weighted"], 10)
        self.assertEqual(rep["by_kind"][0]["event_keys"], 2)

    def test_top_groups_and_cluster_hint(self):
        rep = ac.coverage_report(self.build(), top_groups=2)
        self.assertEqual(rep["top_event_groups"][0]["group"], "BGP Status")
        self.assertEqual(rep["top_event_groups"][0]["alarms"], 3)
        self.assertLessEqual(len(rep["top_event_groups"]), 2)
        self.assertEqual(rep["cluster_hint"]["event_keys"], 4)

    def test_recommendations_skip_completed_certificate(self):
        rep = ac.coverage_report(snap(
            alarm(checklist="SSL证书过期"),
            alarm(checklist="DNS解析异常"),
            alarm(checklist="VBR备份失败"),
            alarm(event_group="BGP Status"),
        ))
        p1 = [r["kind"] for r in rep["recommendations"] if r["priority"] == "P1"]
        self.assertIn("dns", p1)
        self.assertIn("backup", p1)
        self.assertNotIn("certificate", p1)
        self.assertTrue(any(r["priority"] == "P2" for r in rep["recommendations"]))

    def test_empty_snapshot_is_safe(self):
        rep = ac.coverage_report({})
        self.assertEqual(rep["total_active"], 0)
        self.assertEqual(rep["checklist_coverage"], 0.0)
        self.assertEqual(rep["cluster_hint"]["event_keys"], 0)
        self.assertTrue(rep["recommendations"])

    def test_summary_line(self):
        line = ac.summarize_line(ac.coverage_report(self.build()))
        self.assertIn("active=5", line)
        self.assertIn("checklist_coverage=20.0%", line)
        self.assertIn("top_kind=network", line)


if __name__ == "__main__":
    unittest.main()
