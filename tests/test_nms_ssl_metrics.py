#!/usr/bin/env python3
"""SSL 调查闭环运行时指标测试：租约续租失败、采集异常率、waiting_nms 停留、恢复耗时。

只操作进程内指标与纯函数，不触真实 Redis / NMS / 网络。
"""

import asyncio
import contextlib
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

import config  # noqa: E402
from core import nms_ssl_workflow as w  # noqa: E402

SB = "zops:nms_alarms"


class FakeLeaseStore:
    def __init__(self, renew_ok: bool):
        self.renew_ok = renew_ok
        self.renews = 0

    async def renew_lease(self, inv_id, token, ttl):
        self.renews += 1
        return self.renew_ok


class MetricsBase(unittest.TestCase):
    def setUp(self):
        w.reset_metrics()


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def hincrby(self, key, field, amount=1):
        self.ops.append(("hincrby", key, field, amount))
        return self

    def hset(self, key, mapping=None, **kw):
        self.ops.append(("hset", key, dict(mapping or {})))
        return self

    async def execute(self):
        for op in self.ops:
            if op[0] == "hincrby":
                _, key, field, amount = op
                h = self.redis.hashes.setdefault(key, {})
                h[field] = str(int(h.get(field) or 0) + amount)
            else:
                _, key, mapping = op
                self.redis.hashes.setdefault(key, {}).update(
                    {k: str(v) for k, v in mapping.items()})
        self.ops = []
        return [1]


class FakeMetricsRedis:
    """只实现指标聚合需要的 pipeline/hincrby/hset/hgetall 子集。"""

    def __init__(self):
        self.hashes = {}

    def pipeline(self, transaction=False):
        return _FakePipeline(self)

    async def hgetall(self, key):
        return dict(self.hashes.get(key) or {})


class FailingRedis:
    def pipeline(self, transaction=False):
        raise RuntimeError("redis down")

    async def hgetall(self, key):
        raise RuntimeError("redis down")


class TestLeaseMetrics(MetricsBase):
    def test_renew_failure_counted_and_lease_lost(self):
        lost = asyncio.Event()
        store = FakeLeaseStore(False)
        with patch.object(w, "LEASE_RENEW_SECONDS", 0.01):
            asyncio.run(w._lease_heartbeat(store, "inv-1", "tok", lost))
        snap = w.metrics_snapshot()
        self.assertEqual(snap["lease_renew_attempts"], 1)
        self.assertEqual(snap["lease_renew_failures"], 1)
        self.assertTrue(lost.is_set())

    def test_renew_success_keeps_failure_zero(self):
        async def run():
            lost = asyncio.Event()
            store = FakeLeaseStore(True)
            with patch.object(w, "LEASE_RENEW_SECONDS", 0.01):
                task = asyncio.create_task(w._lease_heartbeat(store, "inv-1", "tok", lost))
                await asyncio.sleep(0.05)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            return lost, store

        lost, store = asyncio.run(run())
        snap = w.metrics_snapshot()
        self.assertGreaterEqual(snap["lease_renew_attempts"], 2)
        self.assertEqual(snap["lease_renew_failures"], 0)
        self.assertFalse(lost.is_set())


class TestCollectionMetrics(MetricsBase):
    def test_error_rate_and_scope_counters(self):
        for _ in range(3):
            w.record_collection(SB, True, empty=True)
        line = w.record_collection(SB, False, error="主集抓取失败: timeout")
        snap = w.metrics_snapshot()
        self.assertEqual(snap["collection_rounds"], 4)
        self.assertEqual(snap["collection_errors"], 1)
        self.assertEqual(snap["collection_empty"], 3)
        self.assertAlmostEqual(snap["collection_error_rate"], 25.0)
        self.assertAlmostEqual(snap["recent_error_rate"], 25.0)
        self.assertEqual(snap["scopes"][SB]["rounds"], 4)
        self.assertEqual(snap["scopes"][SB]["errors"], 1)
        self.assertIn("scope_error_rate=25.0%", line)
        self.assertIn("last_error=主集抓取失败: timeout", line)

    def test_recent_window_limits_rate(self):
        # 累计 69 轮含 9 次异常；最近 50 轮窗口保留 9 错 + 41 对
        for _ in range(50):
            w.record_collection(SB, True)
        for _ in range(9):
            w.record_collection(SB, False, error="x")
        for _ in range(10):
            w.record_collection(SB, True)
        snap = w.metrics_snapshot()
        self.assertEqual(snap["collection_rounds"], 69)
        self.assertAlmostEqual(snap["collection_error_rate"], 13.0)
        self.assertEqual(snap["collection_window"], 50)
        self.assertAlmostEqual(snap["recent_error_rate"], 18.0)


def _healthy(now):
    return {"diagnosis": "healthy", "defect_type": "ok", "observed_at": w._iso(now),
            "addresses": ["10.0.0.5"], "certificates": []}


def _unhealthy(now):
    return {"diagnosis": "unhealthy", "defect_type": "expired", "observed_at": w._iso(now),
            "addresses": [], "certificates": [], "error": "expired"}


def _waiting_inv(now, wait_ago=180, manual_ago=600, clear_ago=30):
    return {
        "id": "inv-metrics", "status": "waiting_nms",
        "waiting_since": w._iso(now - wait_ago),
        "manual_completed_at": w._iso(now - manual_ago),
        "healthy_streak": 3,
        "clear_evidence": {"observed_at": w._iso(now - clear_ago), "serial": "555"},
    }


class TestTimingMetrics(MetricsBase):
    def test_waiting_dwell_and_recovery_duration(self):
        now = w._now()
        out = w.apply_probe_result(_waiting_inv(now), _healthy(now), now)
        self.assertEqual(out["status"], "recovered")
        snap = w.metrics_snapshot()
        self.assertEqual(snap["waiting_nms_exited"], 1)
        self.assertAlmostEqual(snap["waiting_nms_dwell_last"], 180, delta=2)
        self.assertEqual(snap["recovered_total"], 1)
        self.assertAlmostEqual(snap["recovery_last"], 600, delta=2)
        self.assertAlmostEqual(snap["recovery_avg"], 600, delta=2)

    def test_regression_records_dwell_without_recovery(self):
        now = w._now()
        out = w.apply_probe_result(_waiting_inv(now, wait_ago=120), _unhealthy(now), now)
        self.assertEqual(out["status"], "observing")
        snap = w.metrics_snapshot()
        self.assertAlmostEqual(snap["waiting_nms_dwell_last"], 120, delta=2)
        self.assertEqual(snap["recovered_total"], 0)

    def test_still_waiting_records_pending_max(self):
        now = w._now()
        # 清除证据年龄 400s > 300s：仍停留 waiting_nms
        out = w.apply_probe_result(_waiting_inv(now, wait_ago=240, clear_ago=400),
                                   _healthy(now), now)
        self.assertEqual(out["status"], "waiting_nms")
        snap = w.metrics_snapshot()
        self.assertAlmostEqual(snap["waiting_nms_pending_max"], 240, delta=2)
        self.assertEqual(snap["waiting_nms_exited"], 0)

    def test_waiting_entry_counted(self):
        now = w._now()
        inv = {
            "id": "inv-enter", "status": "observing",
            "manual_completed_at": w._iso(now - 600),
            "healthy_streak": 2,
            "streak_signature": w._cert_signature(_healthy(now)),
            "streak_first_at": w._iso(now - 150),
            "streak_last_at": w._iso(now - 60),
            "last_check_at": w._iso(now - 10),
        }
        out = w.apply_probe_result(inv, _healthy(now), now)
        self.assertEqual(out["status"], "waiting_nms")
        self.assertEqual(w.metrics_snapshot()["waiting_nms_entered"], 1)


class TestProbeAndWorkerMetrics(MetricsBase):
    def test_probe_records_latency_and_unknown(self):
        w._note_probe({"diagnosis": "healthy"}, 0.5)
        w._note_probe({"diagnosis": "unknown"}, 12.0, "inv-1")
        snap = w.metrics_snapshot()
        self.assertEqual(snap["probe_rounds"], 2)
        self.assertEqual(snap["probe_unknown"], 1)
        self.assertAlmostEqual(snap["probe_unknown_rate"], 50.0)
        self.assertAlmostEqual(snap["probe_max"], 12.0)
        self.assertAlmostEqual(snap["probe_last"], 12.0)
        self.assertAlmostEqual(snap["probe_avg"], 6.25)

    def test_worker_cycle_and_saturation_counters(self):
        w.record_worker_cycle(3)
        w.record_worker_cycle(2)
        w.record_lease_busy()
        w.record_slot_full()
        snap = w.metrics_snapshot()
        self.assertEqual(snap["worker_cycles"], 2)
        self.assertEqual(snap["worker_due_total"], 5)
        self.assertEqual(snap["lease_busy"], 1)
        self.assertEqual(snap["slot_full"], 1)
        self.assertIn("worker_cycles=2", w.metrics_line(snap))
        self.assertIn("lease_busy=1", w.metrics_line(snap))


class TestRemoteCollectionMetrics(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        w.reset_metrics()

    async def test_remote_counters_and_roundtrip(self):
        r = FakeMetricsRedis()
        await w.record_collection_remote(r, SB, True)
        line = await w.record_collection_remote(r, SB, False, error="timeout")
        self.assertIn("errors=1", line)
        remote = await w.read_collection_metrics(r)
        self.assertEqual(remote["collection_rounds"], 2)
        self.assertEqual(remote["collection_errors"], 1)
        self.assertAlmostEqual(remote["error_rate"], 50.0)
        self.assertEqual(remote["scopes"][SB], {"rounds": 2, "errors": 1})
        self.assertEqual(remote["last_scope"], SB)
        self.assertEqual(remote["last_error"], "timeout")
        self.assertEqual(remote["source"], "redis")
        # 本地进程同样计数（本进程日志仍可独立阅读）
        self.assertEqual(w.metrics_snapshot()["collection_rounds"], 2)

    async def test_empty_flag_only_counted_on_success(self):
        r = FakeMetricsRedis()
        await w.record_collection_remote(r, SB, True, empty=True)
        await w.record_collection_remote(r, SB, False, error="x", empty=True)
        remote = await w.read_collection_metrics(r)
        self.assertEqual(remote["collection_empty"], 1)
        self.assertEqual(remote["collection_errors"], 1)

    async def test_redis_failure_never_blocks_collection(self):
        line = await w.record_collection_remote(FailingRedis(), SB, True)
        self.assertIn("scope=" + SB, line)
        self.assertIsNone(await w.read_collection_metrics(FailingRedis()))
        self.assertIsNone(await w.read_collection_metrics(None))

    async def test_metrics_report_contains_remote_and_config(self):
        r = FakeMetricsRedis()
        await w.record_collection_remote(r, SB, False, error="boom")
        report = await w.metrics_report(r)
        self.assertTrue(report["enabled"])
        self.assertGreater(report["process"]["pid"], 0)
        self.assertEqual(report["collection_remote"]["collection_rounds"], 1)
        self.assertEqual(report["metrics"]["collection_rounds"], 1)
        self.assertEqual(report["config"]["check_interval"],
                         int(config.NMS_SSL_CHECK_INTERVAL))

    async def test_summary_merges_remote_collection(self):
        r = FakeMetricsRedis()
        await w.record_collection_remote(r, SB, False, error="boom")
        w.reset_metrics()  # 模拟 backend：本进程不采集
        collection = await w.read_collection_metrics(r)
        with patch.object(w.logger, "info") as info:
            self.assertTrue(w.log_metrics_summary(force=True, collection=collection))
        line = info.call_args[0][0]
        self.assertIn("collection_rounds=1", line)
        self.assertIn("error_rate=100.0%", line)
        self.assertIn("recent_error_rate=100.0%", line)

    async def test_async_summary_reads_redis_from_store(self):
        r = FakeMetricsRedis()
        await w.record_collection_remote(r, SB, True, empty=True)
        w.reset_metrics()
        store = type("S", (), {"_r": r})()
        with patch.object(config, "NMS_SSL_METRICS_LOG_INTERVAL_S", 300), \
                patch.object(w.logger, "info") as info:
            self.assertTrue(await w.log_metrics_summary_async(store))
        self.assertIn("collection_empty=1", info.call_args[0][0])


class TestSummaryLog(MetricsBase):
    def test_interval_gate_and_disable(self):
        w.record_collection(SB, True)
        with patch.object(config, "NMS_SSL_METRICS_LOG_INTERVAL_S", 300):
            self.assertTrue(w.log_metrics_summary())
            self.assertFalse(w.log_metrics_summary())
        with patch.object(config, "NMS_SSL_METRICS_LOG_INTERVAL_S", 0):
            self.assertFalse(w.log_metrics_summary())
            self.assertTrue(w.log_metrics_summary(force=True))

    def test_idle_process_stays_quiet(self):
        with patch.object(config, "NMS_SSL_METRICS_LOG_INTERVAL_S", 300):
            self.assertFalse(w.log_metrics_summary())

    def test_due_investigations_keep_summary_alive(self):
        # 有到期调查（worker_due>0）即使本轮探测未完成也要输出汇总：
        # 避免"一直在排队但汇总静默"看不到积压。
        w.record_worker_cycle(0)
        w.record_worker_cycle(2)
        with patch.object(config, "NMS_SSL_METRICS_LOG_INTERVAL_S", 300), \
                patch.object(w.logger, "info") as info:
            self.assertTrue(w.log_metrics_summary())
        self.assertIn("worker_due=2", info.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
