#!/usr/bin/env python3
"""SSL 调查闭环的隔离 Redis 集成测试。

- 使用独立 DB（默认 15）与随机前缀，不读写生产 `zops:*` 告警键，不执行 FLUSHDB。
- 验证真实原子操作：CAS 单胜者、due ZSET、租约、并发槽。
- 验证多 worker 竞争：同一调查只有一个 worker 能取得租约并执行探测。
- 验证重启：新建 store 实例后调查/索引仍可读。

Redis 不可用时自动跳过（不阻断单元测试套件）。
"""

import asyncio
import json
import os
import sys
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

import config  # noqa: E402
from core import nms_ssl_workflow as w  # noqa: E402

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")

ALARM = {
    "alarm_id": "srl:900", "serial": "900", "source": "node-ssl-01",
    "severity": "critical", "summary": "ssl证书可用天数: 0",
    "event_key": "SSL_EXP_9", "event_group": "CERT",
    "first_time": "2026-10-23T10:00:00+00:00",
    "last_time": "2026-10-23T10:00:00+00:00",
    "fields": {"Summary": "ssl证书可用天数: 0 (app=a, position=ams.example.com:443)"},
}


def _healthy():
    return {
        "diagnosis": "healthy", "defect_type": "ok", "observed_at": w._iso(),
        "addresses": ["10.0.0.5"],
        "certificates": [{"ip": "10.0.0.5", "fingerprint": "aa", "verified": True,
                          "remaining_seconds": 90 * 86400}],
    }


class RedisIntegrationBase(unittest.IsolatedAsyncioTestCase):
    client = None

    @classmethod
    def setUpClass(cls):
        try:
            import redis.asyncio as aioredis
        except Exception as e:  # pragma: no cover - 环境缺依赖
            raise unittest.SkipTest(f"redis 依赖不可用: {e}")
        cls._aioredis = aioredis

        async def _ping():
            client = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
            try:
                await client.ping()
            finally:
                await client.aclose()

        try:
            asyncio.run(_ping())
        except Exception as e:  # pragma: no cover - 无 Redis 环境
            raise unittest.SkipTest(f"隔离 Redis 不可用（{TEST_REDIS_URL}）: {e}")

    async def asyncSetUp(self):
        self.prefix = f"zops_ssl_test:{uuid.uuid4().hex}"
        self.client = self._aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
        self.store = w.RedisNmsSslStore(self.client)
        self.store._p = self.prefix  # 隔离前缀（测试专用，生产固定为 zops:nms:ssl）
        w.set_store(self.store)

    async def asyncTearDown(self):
        try:
            keys = [k async for k in self.client.scan_iter(match=f"{self.prefix}:*")]
            if keys:
                await self.client.delete(*keys)
        finally:
            await self.client.aclose()
            w.set_store(w.MemoryNmsSslStore())

    async def _create(self):
        inv, created = await w.create_investigation(
            owner="alice", scope_base="zops:nms_alarms", alarm=ALARM,
            snapshot_timestamp="2026-10-23T10:00:00+00:00", snapshot_ref_value="ref1")
        self.assertTrue(created)
        return inv

    async def _force_observing(self, inv):
        raw = await self.store.get_inv(inv["id"])
        data = json.loads(raw)
        data["status"] = "observing"
        data["manual_completed_at"] = w._iso(w._now() - 600)
        data["healthy_streak"] = 0
        self.assertTrue(await self.store.cas_inv(inv["id"], raw, json.dumps(data, ensure_ascii=False)))
        return data


class TestAtomicCas(RedisIntegrationBase):
    async def test_concurrent_create_only_one_wins(self):
        payload = json.dumps({"id": "x", "status": "queued"})
        results = await asyncio.gather(*[
            self.store.cas_inv("same-id", None, payload) for _ in range(10)
        ])
        self.assertEqual(sum(1 for r in results if r), 1)
        self.assertEqual(await self.store.get_inv("same-id"), payload)

    async def test_stale_expected_rejected(self):
        await self.store.cas_inv("i1", None, "v1")
        self.assertFalse(await self.store.cas_inv("i1", "outdated", "v2"))
        self.assertTrue(await self.store.cas_inv("i1", "v1", "v2"))
        self.assertEqual(await self.store.get_inv("i1"), "v2")

    async def test_due_zset_order_and_drop(self):
        await self.store.add_due("b", 200)
        await self.store.add_due("a", 100)
        due = await self.store.due_ids(150, 10)
        self.assertEqual(due, ["a"])
        await self.store.drop_due("a")
        self.assertEqual(await self.store.due_ids(150, 10), [])

    async def test_slots_are_exclusive(self):
        tok1, tok2 = uuid.uuid4().hex, uuid.uuid4().hex
        s1 = await self.store.acquire_slot(tok1, 30, 2)
        s2 = await self.store.acquire_slot(tok2, 30, 2)
        self.assertIsNotNone(s1)
        self.assertIsNotNone(s2)
        self.assertNotEqual(s1, s2)
        self.assertIsNone(await self.store.acquire_slot(uuid.uuid4().hex, 30, 2))
        await self.store.release_slot(s1, tok1)
        self.assertIsNotNone(await self.store.acquire_slot(uuid.uuid4().hex, 30, 2))


class TestWorkerCompetition(RedisIntegrationBase):
    async def test_three_workers_probe_once(self):
        inv = await self._create()
        await self._force_observing(inv)
        await self.store.add_due(inv["id"], time.time() - 1)
        calls = {"n": 0}

        async def _probe(target, policy):
            calls["n"] += 1
            await asyncio.sleep(0.15)  # 拉长持有租约的时间窗
            return _healthy()

        with patch("agents.ssl_check.probe_cert", _probe):
            results = await asyncio.gather(
                w.run_due_once(), w.run_due_once(), w.run_due_once())

        self.assertEqual(calls["n"], 1, f"多 worker 重复探测: {calls['n']} 次")
        self.assertEqual(sum(results), 1)
        raw = await self.store.get_inv(inv["id"])
        data = json.loads(raw)
        self.assertEqual(data["status"], "observing")
        self.assertEqual(data["healthy_streak"], 1)
        self.assertTrue(data.get("last_check_at"))

    async def test_lease_blocks_second_worker_until_release(self):
        inv = await self._create()
        token = uuid.uuid4().hex
        self.assertTrue(await self.store.acquire_lease(inv["id"], token, 60))
        self.assertFalse(await self.store.acquire_lease(inv["id"], uuid.uuid4().hex, 60))
        await self.store.release_lease(inv["id"], token)
        self.assertTrue(await self.store.acquire_lease(inv["id"], uuid.uuid4().hex, 60))


class TestRestartPersistence(RedisIntegrationBase):
    async def test_store_restart_keeps_records(self):
        inv = await self._create()
        await self.store.save_index("alice", inv["id"], time.time())
        # 模拟进程重启：新客户端 + 新 store 实例，同 DB / 同前缀
        client2 = self._aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
        try:
            store2 = w.RedisNmsSslStore(client2)
            store2._p = self.prefix
            raw = await store2.get_inv(inv["id"])
            self.assertIsNotNone(raw)
            self.assertEqual(json.loads(raw)["target"]["host"], "ams.example.com")
            self.assertIn(inv["id"], await store2.list_owner_ids("alice"))
            # 重启后继续从持久索引调度（不依赖内存）
            self.assertTrue(await store2.acquire_lease(inv["id"], uuid.uuid4().hex, 60))
        finally:
            await client2.aclose()

    async def test_no_ttl_on_business_records(self):
        inv = await self._create()
        case_id = "case-" + uuid.uuid4().hex
        await self.store.save_case(case_id, json.dumps({"id": case_id}))
        for key in (f"{self.prefix}:inv", f"{self.prefix}:cases", f"{self.prefix}:case_idx"):
            self.assertEqual(await self.client.ttl(key), -1, f"{key} 不应设置 TTL")
        # 租约/槽是临时键，必须带 TTL
        token = uuid.uuid4().hex
        await self.store.acquire_lease(inv["id"], token, 60)
        self.assertGreater(await self.client.ttl(f"{self.prefix}:lease:{inv['id']}"), 0)


if __name__ == "__main__":
    unittest.main()
