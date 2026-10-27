#!/usr/bin/env python3
"""通用告警闭环引擎测试：单元聚合、状态机、恢复判据、人工动作与案例。"""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

import config  # noqa: E402
from core import alert_closure_engine as engine  # noqa: E402
from core import alert_probes  # noqa: E402

T0 = 1_700_000_000.0
_CONFIG_KEYS = ("NMS_CLOSURE_QUIET_S", "NMS_CLOSURE_L1_HEALTHY",
                "NMS_CLOSURE_L1_MIN_SPACING_S", "NMS_CLOSURE_PROBE_INTERVAL",
                "NMS_CLOSURE_PROBE_UNKNOWN_LIMIT",
                "NMS_CLOSURE_TRACK_WINDOW_S", "NMS_CLOSURE_MAX_UNITS",
                "NMS_CLOSURE_FORGET_S")


def alarm(source, summary="BGP peer down", severity="critical", group="BGP Status",
          checklist="", first_time="", ack="0", **over):
    a = {"alarm_id": f"srl:{source}", "source": source, "severity": severity,
         "summary": summary, "event_group": group, "checklist": checklist,
         "first_time": first_time, "last_time": "10/25/26, 1:00 AM",
         "acknowledged": ack, "node": ""}
    a.update(over)
    return a


def backup_alarm(source="10.1.1.5"):
    return alarm(source, summary=f"备份作业失败 {source}", group="VBR",
                 checklist="VBR备份失败")


class FakePipeline:
    def __init__(self, redis):
        self.redis, self.ops = redis, []

    def hset(self, *args, **kwargs):
        self.ops.append(("hset", args, kwargs))
        return self

    async def execute(self):
        for name, args, kwargs in self.ops:
            await getattr(self.redis, name)(*args, **kwargs)
        self.ops = []
        return [1]


class FakeRedis:
    def __init__(self):
        self.hashes, self.lists, self.kv, self.published = {}, {}, {}, []

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, field=None, value=None, mapping=None):
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update({str(k): str(v) for k, v in mapping.items()})
        elif field is not None:
            bucket[str(field)] = str(value)
        return 1

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hdel(self, key, *fields):
        bucket = self.hashes.get(key, {})
        removed = sum(1 for f in fields if bucket.pop(f, None) is not None)
        return removed

    async def hincrby(self, key, field, amount=1):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = str(int(bucket.get(field) or 0) + int(amount))
        return int(bucket[field])

    async def hlen(self, key):
        return len(self.hashes.get(key, {}))

    async def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def ltrim(self, key, start, end):
        return True

    async def lrange(self, key, start, end):
        return list(self.lists.get(key, []))

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0

    async def publish(self, channel, message):
        self.published.append((channel, message))
        return 1

    def pipeline(self):
        return FakePipeline(self)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig = {k: getattr(config, k) for k in _CONFIG_KEYS}
        config.NMS_CLOSURE_QUIET_S = 600
        config.NMS_CLOSURE_L1_HEALTHY = 2
        config.NMS_CLOSURE_L1_MIN_SPACING_S = 120
        config.NMS_CLOSURE_PROBE_UNKNOWN_LIMIT = 3
        config.NMS_CLOSURE_PROBE_INTERVAL = 300
        config.NMS_CLOSURE_TRACK_WINDOW_S = 86400
        config.NMS_CLOSURE_MAX_UNITS = 3000
        config.NMS_CLOSURE_FORGET_S = 604800
        self.r = FakeRedis()
        engine.set_redis(self.r)
        # 确认动作会异步触发探针；测试里统一护栏，避免真实网络调用
        self._probe_patch = patch.object(engine, "_safe_probe", _noop)
        self._probe_patch.start()

    async def asyncTearDown(self):
        self._probe_patch.stop()
        for key, value in self._orig.items():
            setattr(config, key, value)
        engine.set_redis(None)


def confirmed_unit(key="network:sw-01", **over):
    unit = {"key": key, "kind": "network", "label": "网络协议/链路", "tier": "L2",
            "source": "sw-01", "status": "confirmed", "alarm_count": 3,
            "weighted": 24, "active_count": 3, "probe_kind": "", "targets": []}
    unit.update(over)
    return unit


class TestBuildUnits(unittest.TestCase):
    def test_groups_by_source_and_kind(self):
        units = engine.build_units([
            alarm("sw-01", severity="critical"),
            alarm("sw-01", severity="major", summary="BFD session down"),
            alarm("sw-02"),
            backup_alarm("10.1.1.5"),
        ])
        self.assertEqual(len(units), 3)
        net = units["network:sw-01"]
        self.assertEqual(net["alarm_count"], 2)
        self.assertEqual(net["weighted"], 12)
        self.assertEqual(net["tier"], "L2")
        self.assertEqual(net["severities"], {"critical": 1, "major": 1})

    def test_certificate_handed_off(self):
        units = engine.build_units([alarm("node-1", checklist="SSL证书过期",
                                         summary="ssl证书可用天数: 0")])
        unit = units["certificate:node-1"]
        self.assertEqual(unit["handoff"], "nms_ssl_workflow")
        self.assertEqual(unit["probe_kind"], "")
        self.assertEqual(unit["targets"], [])

    def test_targets_extraction(self):
        self.assertEqual(engine.build_units([backup_alarm("10.1.1.5")])
                         ["backup:10.1.1.5"]["targets"], ["10.1.1.5"])
        tcp = alarm("10.2.2.2", checklist="PDNS outage故障",
                    summary="PDNS 10.2.2.2:53 unreachable")
        self.assertEqual(engine.build_units([tcp])["tcp:10.2.2.2"]["targets"],
                         ["10.2.2.2:53"])

    def test_ignores_unknown_severity(self):
        self.assertEqual(engine.build_units([alarm("sw-01", severity="clear")]), {})

    def test_first_seen_parsed(self):
        units = engine.build_units([alarm("sw-01", first_time="10/10/26, 6:47 PM")])
        self.assertTrue(units["network:sw-01"]["first_seen"].startswith("2026-10-10"))


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self._quiet = config.NMS_CLOSURE_QUIET_S
        self._window = config.NMS_CLOSURE_TRACK_WINDOW_S
        config.NMS_CLOSURE_QUIET_S = 600
        config.NMS_CLOSURE_TRACK_WINDOW_S = 86400

    def tearDown(self):
        config.NMS_CLOSURE_QUIET_S = self._quiet
        config.NMS_CLOSURE_TRACK_WINDOW_S = self._window

    def test_observed_untouched(self):
        unit = {"key": "network:sw-01", "status": "observed"}
        self.assertEqual(engine.apply_tick(unit, 5, T0), [])
        self.assertNotIn("clear_since", unit)

    def test_active_alarms_keep_waiting(self):
        unit = confirmed_unit()
        unit["clear_since_ts"] = T0 - 10000
        events = engine.apply_tick(unit, 2, T0)
        self.assertIn("clear_reset", events)
        self.assertIsNone(unit["clear_since_ts"])
        self.assertEqual(unit["status"], "confirmed")

    def test_clear_starts_then_quiet_recovers(self):
        unit = confirmed_unit()
        events = engine.apply_tick(unit, 0, T0)
        self.assertIn("clear_started", events)
        self.assertEqual(unit["status"], "waiting_clear")
        self.assertEqual(engine.apply_tick(unit, 0, T0 + 599), [])
        self.assertEqual(unit["status"], "waiting_clear")
        events = engine.apply_tick(unit, 0, T0 + 601)
        self.assertIn("recovered", events)
        self.assertEqual(unit["status"], "recovered")
        self.assertIn("cluster_clear", unit["recovery_evidence"])

    def test_empty_snapshot_blocks_clear(self):
        unit = confirmed_unit()
        unit["clear_since_ts"] = T0 - 10000
        events = engine.apply_tick(unit, 0, T0, empty_snapshot=True)
        self.assertEqual(events, [])
        self.assertIsNone(unit["clear_since_ts"])
        self.assertEqual(unit["status"], "confirmed")

    def test_maintenance_blocks_recovery(self):
        unit = confirmed_unit()
        unit["clear_since_ts"] = T0 - 10000
        events = engine.apply_tick(unit, 0, T0, maint_until=T0 + 3600)
        self.assertEqual(events, [])
        self.assertTrue(unit["in_maintenance"])
        self.assertIsNone(unit["clear_since_ts"])
        self.assertEqual(unit["status"], "confirmed")

    def test_l1_requires_streak_and_span(self):
        unit = confirmed_unit(key="backup:10.1.1.5", kind="backup", tier="L1",
                              source="10.1.1.5", probe_kind="backup",
                              probe_ok_streak=1, probe_first_ok_at_ts=T0 - 10,
                              probe_last_ok_at_ts=T0 - 10,
                              clear_since_ts=T0 - 5)
        self.assertFalse(engine.recovery_check(unit, T0)[0])
        unit["probe_ok_streak"] = 2
        self.assertFalse(engine.recovery_check(unit, T0)[0])   # 跨度不足
        unit["probe_first_ok_at_ts"] = T0 - 300
        self.assertTrue(engine.recovery_check(unit, T0)[0])
        unit["probe_last_ok_at_ts"] = T0 - 1000                 # 证据过期
        self.assertFalse(engine.recovery_check(unit, T0)[0])

    def test_l1_without_clear_never_recovers(self):
        unit = confirmed_unit(key="backup:10.1.1.5", tier="L1", probe_kind="backup",
                              probe_ok_streak=9, probe_last_ok_at_ts=T0)
        self.assertFalse(engine.recovery_check(unit, T0)[0])

    def test_l1_probe_unavailable_falls_back_to_quiet(self):
        unit = confirmed_unit(key="backup:10.1.1.5", tier="L1", probe_kind="backup",
                              probe_unknown_streak=3, clear_since_ts=T0 - 100)
        self.assertFalse(engine.recovery_check(unit, T0)[0])
        unit["clear_since_ts"] = T0 - 601
        ok, reason = engine.recovery_check(unit, T0)
        self.assertTrue(ok)
        self.assertIn("probe_unavailable", reason)

    def test_track_window_expires(self):
        unit = confirmed_unit()
        unit["confirmed_at_ts"] = T0 - 86401
        events = engine.apply_tick(unit, 0, T0)
        self.assertIn("expired", events)
        self.assertEqual(unit["status"], "expired")


class TestEngineFlows(Base):
    async def test_refresh_creates_observed_units(self):
        result = await engine.refresh_units(alarms=[alarm("sw-01"), alarm("sw-02")],
                                            now=T0, maint={})
        self.assertEqual(result["units_total"], 2)
        self.assertEqual(result["new_units"], 2)
        self.assertEqual(result["by_status"], {"observed": 2})
        units = await engine.list_units()
        self.assertEqual(units["total"], 2)

    async def test_confirm_then_recover(self):
        await engine.refresh_units(alarms=[alarm("sw-01")], now=T0, maint={})
        unit = await engine.confirm_unit("network:sw-01", "root", "光模块故障", now=T0)
        self.assertEqual(unit["status"], "confirmed")
        self.assertEqual(unit["confirmed_by"], "root")
        # 仍有告警：保持跟踪
        await engine.refresh_units(alarms=[alarm("sw-01")], now=T0 + 60, maint={})
        self.assertEqual((await engine.get_unit("network:sw-01"))["status"], "confirmed")
        # 告警消失（快照非空）：开始静默计时
        await engine.refresh_units(alarms=[alarm("sw-02")], now=T0 + 120, maint={})
        self.assertEqual((await engine.get_unit("network:sw-01"))["status"], "waiting_clear")
        # 静默未满：不恢复
        await engine.refresh_units(alarms=[alarm("sw-02")], now=T0 + 400, maint={})
        self.assertEqual((await engine.get_unit("network:sw-01"))["status"], "waiting_clear")
        result = await engine.refresh_units(alarms=[alarm("sw-02")],
                                            now=T0 + 800, maint={})
        self.assertEqual(result["recovered"], ["network:sw-01"])
        unit = await engine.get_unit("network:sw-01")
        self.assertEqual(unit["status"], "recovered")
        self.assertTrue(unit["recovery_evidence"])
        self.assertTrue(self.r.published)   # 飞书通知已入队

    async def test_empty_snapshot_never_recovers(self):
        await engine.refresh_units(alarms=[alarm("sw-01")], now=T0, maint={})
        await engine.confirm_unit("network:sw-01", "root", "", now=T0)
        await engine.refresh_units(alarms=[], now=T0 + 10000, maint={})
        self.assertEqual((await engine.get_unit("network:sw-01"))["status"], "confirmed")

    async def test_maintenance_window_never_recovers(self):
        await engine.refresh_units(alarms=[alarm("sw-01")], now=T0, maint={})
        await engine.confirm_unit("network:sw-01", "root", "", now=T0)
        await engine.refresh_units(alarms=[alarm("sw-02")], now=T0 + 120,
                                   maint={"sw-01": T0 + 7200})
        unit = await engine.get_unit("network:sw-01")
        self.assertTrue(unit["in_maintenance"])
        self.assertEqual(unit["status"], "confirmed")

    async def test_confirm_errors(self):
        with self.assertRaises(engine.ClosureError) as ctx:
            await engine.confirm_unit("network:ghost", "root")
        self.assertEqual(ctx.exception.status, 404)
        await engine.refresh_units(alarms=[alarm("sw-01")], now=T0, maint={})
        await engine.confirm_unit("network:sw-01", "root", "", now=T0)
        unit = await engine.get_unit("network:sw-01")
        unit["status"] = "recovered"
        await engine._save_unit(unit)
        with self.assertRaises(engine.ClosureError) as ctx:
            await engine.confirm_unit("network:sw-01", "root", "")
        self.assertEqual(ctx.exception.status, 409)

    async def test_pause_resume(self):
        await engine.refresh_units(alarms=[alarm("sw-01")], now=T0, maint={})
        await engine.pause_unit("network:sw-01", "root", "等待变更")
        await engine.refresh_units(alarms=[alarm("sw-02")], now=T0 + 3000, maint={})
        unit = await engine.get_unit("network:sw-01")
        self.assertEqual(unit["status"], "paused")
        await engine.resume_unit("network:sw-01", "root")
        self.assertEqual((await engine.get_unit("network:sw-01"))["status"], "confirmed")

    async def test_l1_probe_recovery(self):
        await engine.refresh_units(alarms=[backup_alarm()], now=T0, maint={})
        with patch.object(engine, "_safe_probe", _noop):
            await engine.confirm_unit("backup:10.1.1.5", "root", "备份作业失败", now=T0)
        await engine.refresh_units(alarms=[alarm("sw-01")], now=T0 + 60, maint={})
        ok = {"ok": True, "summary": "作业全部成功", "results": [{"target": "10.1.1.5"}]}
        with patch.object(alert_probes, "probe", _fake_probe(ok)):
            await engine.recheck_unit("backup:10.1.1.5", now=T0 + 70)
            unit = await engine.get_unit("backup:10.1.1.5")
            self.assertEqual(unit["probe_status"], "ok")
            self.assertEqual(unit["status"], "waiting_clear")
            await engine.recheck_unit("backup:10.1.1.5", now=T0 + 300)
            unit = await engine.get_unit("backup:10.1.1.5")
            self.assertEqual(unit["status"], "recovered")
            self.assertIn("probe_healthy", unit["recovery_evidence"])

    async def test_l1_unknown_probe_is_not_healthy(self):
        await engine.refresh_units(alarms=[backup_alarm()], now=T0, maint={})
        unit = await engine.get_unit("backup:10.1.1.5")
        unit["status"] = "waiting_clear"
        unit["active_count"] = 0
        unit["clear_since_ts"] = T0 - 100
        await engine._save_unit(unit)
        unknown = {"ok": None, "summary": "未查询到备份作业", "results": []}
        with patch.object(alert_probes, "probe", _fake_probe(unknown)):
            await engine.recheck_unit("backup:10.1.1.5", now=T0)
        unit = await engine.get_unit("backup:10.1.1.5")
        self.assertEqual(unit["probe_status"], "unknown")
        self.assertEqual(unit["probe_ok_streak"], 0)
        self.assertEqual(unit["status"], "waiting_clear")

    async def test_case_requires_recovered(self):
        await engine.refresh_units(alarms=[alarm("sw-01")], now=T0, maint={})
        with self.assertRaises(engine.ClosureError):
            await engine.record_case("network:sw-01", "root", "已处理")
        unit = await engine.get_unit("network:sw-01")
        unit.update(status="recovered", recovery_evidence="cluster_clear=601s")
        await engine._save_unit(unit)
        case = await engine.record_case("network:sw-01", "root", "更换光模块",
                                        "增加光衰监控")
        self.assertTrue(case["case_id"])
        detail = await engine.get_unit_detail("network:sw-01")
        self.assertEqual(detail["case"]["resolution"], "更换光模块")
        self.assertEqual((await engine.list_cases())["total"], 1)

    async def test_reopen_closed_unit(self):
        await engine.refresh_units(alarms=[alarm("sw-01")], now=T0, maint={})
        unit = await engine.get_unit("network:sw-01")
        unit.update(status="recovered", recovery_evidence="x")
        await engine._save_unit(unit)
        reopened = await engine.reopen_unit("network:sw-01", "root", "再次确认")
        self.assertEqual(reopened["status"], "confirmed")
        self.assertNotIn("recovery_evidence", reopened)

    async def test_list_filters_and_stats(self):
        await engine.refresh_units(alarms=[alarm("sw-01"), alarm("sw-02"),
                                           backup_alarm()], now=T0, maint={})
        await engine.confirm_unit("backup:10.1.1.5", "root", "", now=T0)
        rows = await engine.list_units(tier="L1")
        self.assertEqual(rows["total"], 1)
        self.assertEqual(rows["items"][0]["kind"], "backup")
        rows = await engine.list_units(status="confirmed")
        self.assertEqual(rows["total"], 1)
        rows = await engine.list_units(q="sw-0")
        self.assertEqual(rows["total"], 2)
        stats = await engine.stats()
        self.assertEqual(stats["units"], 3)
        self.assertEqual(stats["tracked"], 1)
        self.assertEqual(stats["thresholds"]["quiet_s"], 600)

    async def test_probe_due_respects_interval(self):
        await engine.refresh_units(alarms=[backup_alarm()], now=T0, maint={})
        await engine.confirm_unit("backup:10.1.1.5", "root", "", now=T0)
        unit = await engine.get_unit("backup:10.1.1.5")
        unit["probe_last_at_ts"] = T0 - 10          # 距上次探测不足间隔
        await engine._save_unit(unit)
        ok = {"ok": True, "summary": "作业全部成功"}
        with patch.object(alert_probes, "probe", _fake_probe(ok)):
            self.assertEqual((await engine._probe_due(T0))["probed"], 0)
            unit["probe_last_at_ts"] = T0 - 1000
            await engine._save_unit(unit)
            self.assertEqual((await engine._probe_due(T0))["probed"], 1)
        self.assertEqual((await engine.get_unit("backup:10.1.1.5"))["probe_ok_streak"], 1)

    async def test_run_once_reads_snapshot(self):
        self.r.kv[f"{config.REDIS_PREFIX}:nms_alarms:latest"] = json.dumps(
            {"timestamp": "x", "alarms": [alarm("sw-01")]})
        result = await engine.run_once(now=T0)
        self.assertEqual(result["units_total"], 1)
        self.assertEqual(self.r.hashes[engine.KEY_METRICS]["last_units"], "1")
        with patch.object(config, "NMS_CLOSURE_ENABLED", False):
            self.assertEqual(await engine.run_once(now=T0), {"skipped": "disabled"})


async def _noop(*args, **kwargs):
    return None


def _fake_probe(result):
    async def _probe(kind, targets, timeout=8):
        return dict(result, kind=kind)
    return _probe


if __name__ == "__main__":
    unittest.main(verbosity=2)
