#!/usr/bin/env python3
"""历史告警服务端关键字搜索 + Agent 优雅退出测试。

覆盖灰度清单第十二/十三节两项遗留：
1. 「历史告警」页搜索原本只在前端已加载的本页 200 条里过滤，命中不到早期事件
   → 现改到服务端 history_z 全库扫描（多词 AND），且必须命中才解码，不能整库 json.loads；
2. run_agent 旧实现用 signal.signal + loop.create_task(agent.stop())，循环阻塞在
   epoll 时新任务不被调度 → SIGTERM 后要等 systemd TimeoutStopSec 到点 SIGKILL
   （实测 ~45s，重启窗口内 /api/nms/* 全失败 → 前端「告警又没了」）。
   现改为 add_signal_handler + _graceful_shutdown（取消残留任务 + 限时清理 + 停循环）。
"""

import asyncio
import json
import os
import signal
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

import config  # noqa: E402
import webchat_api  # noqa: E402,F401  避免 api.routers.agents / agents.base 循环导入
from agents.nms_agent import nms_history_read  # noqa: E402
from agents import base as agent_base  # noqa: E402

BASE = f"{config.REDIS_PREFIX}:nms_alarms"


class FakeHistoryRedis:
    """只实现 nms_history_read 用到的命令，并统计 hmget 解码次数（防整库解析）。"""

    def __init__(self, entries):
        # entries: [(content_key, score, alarm_dict)]
        self._z = {}
        self._data = {}
        for ck, score, obj in entries:
            self._z[ck] = float(score)
            self._data[ck] = json.dumps(obj, ensure_ascii=False)
        self.decode_count = 0

    async def zcard(self, key):
        return len(self._z)

    async def zrevrangebyscore(self, key, hi, lo, start=0, num=0):
        hiv = float('inf') if hi == '+inf' else float(hi)
        lov = float('-inf') if lo == '-inf' else float(lo)
        ordered = sorted(
            [ck for ck, s in self._z.items() if lov <= s <= hiv],
            key=lambda ck: self._z[ck], reverse=True)
        if num == 0:
            return ordered[start:]
        return ordered[start:start + num]

    async def hmget(self, key, cks):
        out = []
        for ck in cks:
            v = self._data.get(ck)
            if v is not None:
                self.decode_count += 1
            out.append(v)
        return out

    async def lrange(self, key, a, b):
        return []


def _ev(ck, score, **kw):
    obj = {"_ck": ck, "summary": kw.get("summary", ""),
           "source": kw.get("source", ""), "group": kw.get("group", ""),
           "groups": kw.get("groups", []), "alarm_type": kw.get("alarm_type", ""),
           "first_time": time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(score))}
    obj.update(kw.get("extra", {}))
    return ck, score, obj


class TestHistoryServerSearch(unittest.IsolatedAsyncioTestCase):
    NOW = time.time()

    def _entries(self):
        return [
            _ev("a", self.NOW - 3600, summary="disk capacity 90% on esx1",
                groups=["Storage"], source="esx1"),
            _ev("b", self.NOW - 7200, summary="cpu usage high", groups=["Compute"],
                source="esx2"),
            _ev("c", self.NOW - 10800, summary="memory leak detected in VMdb",
                groups=["Compute"], source="vmdb"),
            _ev("d", self.NOW - 20 * 3600, summary="esx1 link down",
                groups=["Network"], source="esx1"),
        ]

    async def test_substring_match_whole_library(self):
        """命中项跨多页也应返回（旧前端只在已加载本页 200 条里筛会漏早期 c/d）。"""
        r = FakeHistoryRedis(self._entries())
        alarms, src, has_more = await nms_history_read(
            r, BASE, limit=2, page=1, q="esx1")
        self.assertEqual(src, 'zset')
        # esx1 命中 a + d = 2 条
        summaries = {a['summary'] for a in alarms}
        self.assertIn("disk capacity 90% on esx1", summaries)
        self.assertIn("esx1 link down", summaries)
        self.assertEqual(len(alarms), 2)

    async def test_multi_term_and(self):
        """空格分词按 AND：'esx1 capacity' 只应命中同时含两者的 a。"""
        r = FakeHistoryRedis(self._entries())
        alarms, _, _ = await nms_history_read(r, BASE, limit=10, page=1,
                                              q="esx1 capacity")
        self.assertEqual(len(alarms), 1)
        self.assertIn("disk capacity", alarms[0]['summary'])

    async def test_no_match_returns_empty(self):
        r = FakeHistoryRedis(self._entries())
        alarms, _, has_more = await nms_history_read(
            r, BASE, limit=10, page=1, q="nonexistent-token-xyz")
        self.assertEqual(alarms, [])
        self.assertFalse(has_more)

    async def test_case_insensitive(self):
        r = FakeHistoryRedis(self._entries())
        alarms, _, _ = await nms_history_read(r, BASE, limit=10, page=1, q="ESX1")
        self.assertEqual(len(alarms), 2)

    async def test_groups_plus_query_combined(self):
        """分组 + 关键字叠加：Network 组里含 esx1 → 只有 d。"""
        r = FakeHistoryRedis(self._entries())
        alarms, _, _ = await nms_history_read(
            r, BASE, limit=10, page=1, groups=["Network"], q="esx1")
        self.assertEqual(len(alarms), 1)
        self.assertIn("link down", alarms[0]['summary'])

    async def test_query_uses_byte_prefilter_no_full_decode(self):
        """关键字扫描必须先在原始字节上筛选，命中才解析，不能整库 json.loads。"""
        from agents import nms_agent as na
        entries = [_ev(f"k{i}", self.NOW - i * 100,
                        summary=("MATCHME " if i == 0 else "noise") + f" item {i}")
                   for i in range(1200)]
        r = FakeHistoryRedis(entries)
        real_loads = json.loads
        loads_calls = {"n": 0}

        def counting_loads(s, *a, **k):
            loads_calls["n"] += 1
            return real_loads(s, *a, **k)
        with mock.patch.object(na.json, "loads", counting_loads):
            alarms, _, _ = await nms_history_read(
                r, BASE, limit=500, page=1, q="MATCHME")
        self.assertEqual(len(alarms), 1)
        # 命中稀疏：全库 1200 条只做字节筛选，json.loads 只发生在命中的少数条目上
        self.assertLessEqual(
            loads_calls["n"], 5,
            f"只应解析命中的条目，实际解析 {loads_calls['n']} 次")

    async def test_empty_query_keeps_rank_fast_path(self):
        """无关键字仍走 rank 分页快路径（一次定位，不整库扫描）。"""
        r = FakeHistoryRedis(self._entries())
        alarms, src, _ = await nms_history_read(r, BASE, limit=2, page=1)
        self.assertEqual(src, 'zset')
        self.assertEqual(len(alarms), 2)


def _done():
    """RecordingLoop.run_until_complete 不会消费参数（非真实循环），
    start() 返回 None 即可（不能用 asyncio.Future()，3.10 下无当前循环会报错）。"""
    return None


class _RecordingLoop:
    """记录信号安装方式与退出编排调用，不真正跑事件循环。"""

    def __init__(self):
        self.signal_handlers = {}
        self.old_style_signals = []
        self.created = []
        self.stopped = False

    def add_signal_handler(self, sig, cb, *args):
        self.signal_handlers[sig] = (cb, args)

    def remove_signal_handler(self, sig):
        return True

    def create_task(self, coro):
        self.created.append(coro)
        coro.close()   # 不调度，避免 "never awaited" 警告

    def run_until_complete(self, fut):
        return None

    def stop(self):
        self.stopped = True

    def shutdown_asyncgens(self):
        return None

    def close(self):
        pass


class TestRunAgentSignalWiring(unittest.TestCase):
    def test_uses_add_signal_handler_not_signal_signal(self):
        """必须走 loop.add_signal_handler（能唤醒阻塞在 epoll 的循环），
        而非旧式 signal.signal（循环阻塞时 create_task 不被调度）。"""
        loop = _RecordingLoop()
        agent = mock.Mock()
        agent.name = "stub"
        agent.start = mock.Mock(side_effect=lambda: _done())
        with mock.patch.object(asyncio, "new_event_loop", return_value=loop), \
                mock.patch.object(asyncio, "set_event_loop"):
            agent_base.run_agent(agent)
        self.assertIn(signal.SIGTERM, loop.signal_handlers)
        self.assertIn(signal.SIGINT, loop.signal_handlers)

    def test_signal_callback_triggers_graceful_shutdown(self):
        loop = _RecordingLoop()
        agent = mock.Mock()
        agent.name = "stub"
        agent.start = mock.Mock(side_effect=lambda: _done())
        with mock.patch.object(asyncio, "new_event_loop", return_value=loop), \
                mock.patch.object(asyncio, "set_event_loop"):
            agent_base.run_agent(agent)
        cb, args = loop.signal_handlers[signal.SIGTERM]
        before = len(loop.created)
        cb(*args)
        self.assertEqual(len(loop.created), before + 1,
                         "信号回调应编排一次 _graceful_shutdown 任务")


class TestGracefulShutdown(unittest.TestCase):
    def test_cancels_stuck_task_and_cleans_up(self):
        """宽限期到后取消残留任务（如卡住的采集轮次），并跑一次 _cleanup。

        用独立新循环（非 IsolatedAsyncioTestCase 的运行循环）：_graceful_shutdown
        结尾会 loop.stop()，若在测试自带的循环里跑会连带打断测试 runner。"""
        loop = asyncio.new_event_loop()
        try:
            stub = mock.Mock()
            stub.name = "stub"
            stub._running = True
            calls = {"cleanup": 0}

            async def _cleanup():
                calls["cleanup"] += 1
            stub._cleanup = _cleanup

            async def _blocker():
                await asyncio.sleep(30)

            async def _drive():
                b = asyncio.ensure_future(_blocker())
                await asyncio.sleep(0)   # 让 blocker 真正开始调度
                await agent_base.BaseAgent._graceful_shutdown(
                    stub, loop, grace=0.1)
                return b

            fut = asyncio.ensure_future(_drive(), loop=loop)
            try:
                loop.run_until_complete(fut)
            except RuntimeError:
                pass   # loop.stop() 触发「Event loop stopped before Future
                # completed」属预期，此时清理已完成
            self.assertEqual(calls["cleanup"], 1, "退出必须执行一次资源清理")
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
