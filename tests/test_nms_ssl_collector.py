#!/usr/bin/env python3
"""SSL 调查观察钩子测试：空数据 / 哈希未变化早退之前必须先把原始行交给观察器。

不启动浏览器、不写真实 Redis：页面、登录、采集、Checklist 合并全部替换为假实现，
只验证「观察发生在早退之前」这一时序约束（NMS_SSL_调查闭环 计划第 48 条）。
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

import config  # noqa: E402
from agents.nms_agent import NmsAgent  # noqa: E402

ALARM = {
    "alarm_id": "srl:1", "serial": "1", "source": "node-a",
    "severity": "critical", "summary": "ssl证书可用天数: 0",
}


class FakePage:
    def __init__(self):
        self.closed = False

    async def set_viewport_size(self, size):
        pass

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.page = FakePage()

    async def new_page(self, **kw):
        return self.page


class FakeRedis:
    async def llen(self, key):
        return 0

    async def get(self, key):
        return None


def make_agent(seeded_hash=None):
    agent = NmsAgent.__new__(NmsAgent)
    agent._nms_last_alarm_hash = {}
    if seeded_hash is not None:
        agent._nms_last_alarm_hash[f"{config.REDIS_PREFIX}:nms_alarms"] = seeded_hash
    return agent


class TestCollectorObserverHook(unittest.IsolatedAsyncioTestCase):
    async def _run(self, alarms, agent=None, quality_error=None):
        agent = agent or make_agent()
        calls, errors = [], []

        async def observe(base, rows, observed_at):
            calls.append((base, rows, observed_at))
            return 0

        async def observe_error(base, error, observed_at):
            errors.append((base, error, observed_at))
            return 0

        async def collect(_self, page):
            agent._nms_collect_quality = {
                "status": "error" if quality_error else "ok",
                "error": quality_error or "",
            }
            return alarms, {}, 0, []

        with patch.object(NmsAgent, "get_redis",
                          new=AsyncMock(return_value=FakeRedis())), \
             patch.object(NmsAgent, "_ensure_browser",
                          new=AsyncMock(return_value=FakeBrowser())), \
             patch.object(NmsAgent, "_nms_login", new=AsyncMock(return_value=True)), \
             patch.object(NmsAgent, "_nms_get_content_frame",
                          new=AsyncMock(return_value=object())), \
             patch.object(NmsAgent, "_nms_collect_via_api", new=collect), \
             patch("agents.nms_agent._nms_merged_checklist",
                   new=AsyncMock(return_value={})), \
             patch("core.nms_ssl_workflow.observe_snapshot", observe), \
             patch("core.nms_ssl_workflow.observe_collection_error", observe_error):
            await agent._nms_run_once()
        return calls, errors

    async def test_empty_snapshot_observed_before_early_return(self):
        calls, errors = await self._run([])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], f"{config.REDIS_PREFIX}:nms_alarms")
        self.assertEqual(calls[0][1], [])
        self.assertIsInstance(calls[0][2], str)
        self.assertEqual(errors, [])

    async def test_unchanged_hash_observed_before_early_return(self):
        # 预置相同哈希 → 走「告警无变化，跳过分析」早退分支
        agent = make_agent(seeded_hash=NmsAgent._nms_hash_alarms([ALARM]))
        calls, errors = await self._run([ALARM], agent=agent)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], [ALARM])
        self.assertEqual(errors, [])
        # 早退后不得把观察结果再次写入（每轮只观察一次）
        self.assertEqual(agent._nms_last_alarm_hash[
            f"{config.REDIS_PREFIX}:nms_alarms"], NmsAgent._nms_hash_alarms([ALARM]))

    async def test_collect_error_records_anomaly_not_clear(self):
        calls, errors = await self._run([], quality_error="主集抓取失败: timeout")
        self.assertEqual(calls, [], "采集异常不得作为清除证据交给观察器")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], f"{config.REDIS_PREFIX}:nms_alarms")
        self.assertIn("timeout", errors[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
