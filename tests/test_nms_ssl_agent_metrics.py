#!/usr/bin/env python3
"""nms_agent 侧采集指标：确认计数写入 Redis（供 backend 跨进程汇总），

且 Redis 异常不阻断采集观察流程。只替换 observe_* 与 Redis 客户端，不触真实 NMS。
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

from agents import nms_agent as agent_mod  # noqa: E402
from core import nms_ssl_workflow as w  # noqa: E402

SCOPE = "zops:nms_alarms"


class _Pipe:
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
                h = self.redis.data.setdefault(key, {})
                h[field] = str(int(h.get(field) or 0) + amount)
            else:
                _, key, mapping = op
                self.redis.data.setdefault(key, {}).update(
                    {k: str(v) for k, v in mapping.items()})
        self.ops = []
        return [1]


class FakeRedis:
    def __init__(self):
        self.data = {}

    def pipeline(self, transaction=False):
        return _Pipe(self)

    async def hgetall(self, key):
        return dict(self.data.get(key) or {})


class BrokenRedis:
    def pipeline(self, transaction=False):
        raise RuntimeError("redis down")


class TestAgentCollectionMetrics(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        w.reset_metrics()
        self.agent = agent_mod.NmsAgent.__new__(agent_mod.NmsAgent)
        self.observed = []

        async def fake_observe(scope, alarms, at):
            self.observed.append(("ok", scope, len(alarms)))
            return 0

        async def fake_observe_error(scope, error, at):
            self.observed.append(("error", scope, error))
            return 0

        self._p1 = patch.object(w, "observe_snapshot", fake_observe)
        self._p2 = patch.object(w, "observe_collection_error", fake_observe_error)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    async def test_observe_writes_redis_and_local_counters(self):
        r = FakeRedis()
        await self.agent._nms_ssl_observe(r, SCOPE, [{"alarm_id": "a"}])
        self.assertEqual(self.observed, [("ok", SCOPE, 1)])
        remote = await w.read_collection_metrics(r)
        self.assertEqual(remote["collection_rounds"], 1)
        self.assertEqual(remote["collection_errors"], 0)
        self.assertEqual(remote["last_scope"], SCOPE)
        # 本进程（nms_agent）同样计数，便于看自身日志
        self.assertEqual(w.metrics_snapshot()["collection_rounds"], 1)

    async def test_empty_snapshot_counted_as_empty(self):
        r = FakeRedis()
        await self.agent._nms_ssl_observe(r, SCOPE, [])
        remote = await w.read_collection_metrics(r)
        self.assertEqual(remote["collection_empty"], 1)
        self.assertEqual(remote["collection_errors"], 0)

    async def test_observe_error_marks_error_and_last_error(self):
        r = FakeRedis()
        await self.agent._nms_ssl_observe_error(r, SCOPE, "登录失败")
        remote = await w.read_collection_metrics(r)
        self.assertEqual(remote["collection_rounds"], 1)
        self.assertEqual(remote["collection_errors"], 1)
        self.assertEqual(remote["last_error"], "登录失败")
        self.assertEqual(self.observed, [("error", SCOPE, "登录失败")])

    async def test_redis_failure_does_not_block_observer(self):
        await self.agent._nms_ssl_observe(BrokenRedis(), SCOPE, [])
        self.assertEqual(self.observed, [("ok", SCOPE, 0)])
        self.assertEqual(w.metrics_snapshot()["collection_rounds"], 1)


if __name__ == "__main__":
    unittest.main()
