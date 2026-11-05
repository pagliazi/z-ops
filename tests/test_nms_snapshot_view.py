#!/usr/bin/env python3
"""NMS 快照读路径测试：个人快照过期回退 + 解析缓存。

覆盖两类线上症状的根因：
1. 个人抓取早已停摆却仍用几个月前的个人快照渲染 → 页面显示「暂无现存告警」，
   被误读为「告警又没了」；
2. 70+MB 快照每次请求都 GET + 在事件循环里 json.loads → 并发轮询拖垮后端，
   请求失败同样表现为「告警凭空消失」。
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

import config  # noqa: E402
import webchat_api  # noqa: E402,F401  必须先加载，避免 api.routers.agents 循环导入
from api.routers import agents as ar  # noqa: E402

GLOBAL_BASE = f"{config.REDIS_PREFIX}:nms_alarms"
# 服务账号是 op1855 (=config.NMS_USERNAME)，测试须用另一个 AD 账号才会走个人视图
USER = "op2141"
PERSONAL_BASE = f"{GLOBAL_BASE}:user:{USER}"


def _ts(seconds_ago: float) -> str:
    return (datetime.now().astimezone() - timedelta(seconds=seconds_ago)).isoformat()


class FakeSnapRedis:
    """只实现读路径用到的命令，并统计 latest 本体的 GET 次数。"""

    def __init__(self, kv=None):
        self.kv = dict(kv or {})
        self.latest_gets = 0
        self.sets = []

    async def get(self, key):
        if key.endswith(":latest"):
            self.latest_gets += 1
        return self.kv.get(key)

    async def set(self, key, value, *a, **kw):
        self.sets.append((key, value))
        self.kv[key] = value
        return True

    async def exists(self, key):
        return 1 if key in self.kv else 0


class TestReadBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 采集间隔固定 900s，避免被 zops.env 动态值影响
        self._iv_patch = mock.patch.object(config, "NMS_CHECK_INTERVAL", 900)
        self._iv_patch.start()
        self.addCleanup(self._iv_patch.stop)
        ar._NMS_SNAP_CACHE.clear()

    def _with_session(self, r):
        r.kv[f"{config.REDIS_PREFIX}:nms_session:{USER}"] = "[{\"name\":\"Ltpa\"}]"

    async def test_no_session_uses_global(self):
        r = FakeSnapRedis({f"{GLOBAL_BASE}:latest": "{}"})
        base, is_personal, note = await ar._nms_read_base(r, USER)
        self.assertEqual(base, GLOBAL_BASE)
        self.assertFalse(is_personal)
        self.assertEqual(note, "")

    async def test_personal_snapshot_missing_falls_back(self):
        r = FakeSnapRedis({f"{GLOBAL_BASE}:latest": "{}"})
        self._with_session(r)
        base, is_personal, note = await ar._nms_read_base(r, USER)
        self.assertEqual(base, GLOBAL_BASE)
        self.assertFalse(is_personal)
        self.assertIn("缺失", note)
        # 新鲜度判定只走旁路小键，不得 GET 70+MB 快照本体
        self.assertEqual(r.latest_gets, 0)

    async def test_fresh_personal_snapshot_is_kept(self):
        """刚用 AD 身份抓过（个人快照比全局新）→ 必须继续用个人视图。"""
        r = FakeSnapRedis({
            f"{GLOBAL_BASE}:latest": "{}", f"{GLOBAL_BASE}:latest:ts": _ts(3600),
            f"{PERSONAL_BASE}:latest": "{}", f"{PERSONAL_BASE}:latest:ts": _ts(60),
        })
        self._with_session(r)
        base, is_personal, note = await ar._nms_read_base(r, USER)
        self.assertEqual(base, PERSONAL_BASE)
        self.assertTrue(is_personal)
        self.assertEqual(note, "")

    async def test_stale_personal_snapshot_falls_back_to_global(self):
        """个人快照停更超过 3 个采集周期（全局仍在更新）→ 回退全局并说明原因。"""
        r = FakeSnapRedis({
            f"{GLOBAL_BASE}:latest": "{}", f"{GLOBAL_BASE}:latest:ts": _ts(60),
            f"{PERSONAL_BASE}:latest": "{}",
            f"{PERSONAL_BASE}:latest:ts": _ts(4 * 24 * 3600),   # 4 天前
        })
        self._with_session(r)
        base, is_personal, note = await ar._nms_read_base(r, USER)
        self.assertEqual(base, GLOBAL_BASE)
        self.assertFalse(is_personal)
        self.assertIn("已过期", note)

    async def test_legacy_snapshot_without_ts_key_keeps_personal(self):
        """旧快照没写旁路时间戳：无法判定新旧，保持原「个人优先」行为不误伤。"""
        r = FakeSnapRedis({
            f"{GLOBAL_BASE}:latest": "{}", f"{GLOBAL_BASE}:latest:ts": _ts(60),
            f"{PERSONAL_BASE}:latest": "{}",
        })
        self._with_session(r)
        base, is_personal, note = await ar._nms_read_base(r, USER)
        self.assertEqual(base, PERSONAL_BASE)
        self.assertTrue(is_personal)


class TestSnapshotCache(unittest.IsolatedAsyncioTestCase):
    SNAP = {"timestamp": _ts(30), "total": 1, "alarms": [{"alarm_id": "a1"}]}

    async def asyncSetUp(self):
        ar._NMS_SNAP_CACHE.clear()

    async def test_same_ts_served_from_cache_without_redis_get(self):
        r = FakeSnapRedis({
            f"{GLOBAL_BASE}:latest": json.dumps(self.SNAP),
            f"{GLOBAL_BASE}:latest:ts": self.SNAP["timestamp"],
        })
        first = await ar._nms_load_snapshot(r, GLOBAL_BASE)
        self.assertEqual(r.latest_gets, 1)
        self.assertIsNotNone(first)
        again = await ar._nms_load_snapshot(r, GLOBAL_BASE)
        self.assertEqual(r.latest_gets, 1, "同一快照必须复用解析结果，不再 GET 70+MB 本体")
        self.assertIs(first, again)

    async def test_new_ts_triggers_reload(self):
        snap2 = {"timestamp": _ts(1), "total": 2, "alarms": []}
        r = FakeSnapRedis({
            f"{GLOBAL_BASE}:latest": json.dumps(self.SNAP),
            f"{GLOBAL_BASE}:latest:ts": self.SNAP["timestamp"],
        })
        await ar._nms_load_snapshot(r, GLOBAL_BASE)
        r.kv[f"{GLOBAL_BASE}:latest"] = json.dumps(snap2)
        r.kv[f"{GLOBAL_BASE}:latest:ts"] = snap2["timestamp"]
        got = await ar._nms_load_snapshot(r, GLOBAL_BASE)
        self.assertEqual(r.latest_gets, 2)
        self.assertEqual(got["timestamp"], snap2["timestamp"])

    async def test_backfills_ts_sidecar_for_legacy_snapshot(self):
        """缺旁路键时补齐，后续请求即可做新鲜度判定。"""
        r = FakeSnapRedis({f"{GLOBAL_BASE}:latest": json.dumps(self.SNAP)})
        await ar._nms_load_snapshot(r, GLOBAL_BASE)
        self.assertEqual(
            r.kv.get(f"{GLOBAL_BASE}:latest:ts"), self.SNAP["timestamp"])

    async def test_cache_is_bounded(self):
        """缓存只保留有限个 base，避免多用户个人快照把进程内存吃满。"""
        r = FakeSnapRedis()
        for i in range(5):
            base = f"{GLOBAL_BASE}:user:u{i}"
            snap = {"timestamp": _ts(i), "alarms": []}
            r.kv[f"{base}:latest"] = json.dumps(snap)
            await ar._nms_load_snapshot(r, base)
        self.assertLessEqual(len(ar._NMS_SNAP_CACHE), ar._NMS_SNAP_CACHE_MAX)

    async def test_missing_snapshot_returns_none(self):
        r = FakeSnapRedis()
        self.assertIsNone(await ar._nms_load_snapshot(r, GLOBAL_BASE))


class TestAlarmsEndpointContract(unittest.IsolatedAsyncioTestCase):
    """展示类端点与 SSL 快照加载必须同一 base 口径，否则 snapshot_ref 对不上。"""

    async def asyncSetUp(self):
        ar._NMS_SNAP_CACHE.clear()
        self._iv_patch = mock.patch.object(config, "NMS_CHECK_INTERVAL", 900)
        self._iv_patch.start()
        self.addCleanup(self._iv_patch.stop)

    async def test_ssl_loader_matches_display_base(self):
        stale = _ts(7 * 24 * 3600)
        fresh = _ts(60)
        r = FakeSnapRedis({
            f"{GLOBAL_BASE}:latest": json.dumps({"timestamp": fresh, "alarms": []}),
            f"{GLOBAL_BASE}:latest:ts": fresh,
            f"{config.REDIS_PREFIX}:nms_session:{USER}": "[x]",
            f"{PERSONAL_BASE}:latest": json.dumps({"timestamp": stale, "alarms": []}),
            f"{PERSONAL_BASE}:latest:ts": stale,
        })
        display_base, _p, _n = await ar._nms_read_base(r, USER)
        ssl_base, snap = await ar._nms_ssl_load_snapshot(r, USER)
        self.assertEqual(display_base, ssl_base,
                         "页面展示的快照与创建调查读取的快照必须是同一份")
        self.assertEqual(snap["timestamp"], fresh)


if __name__ == "__main__":
    unittest.main(verbosity=2)
