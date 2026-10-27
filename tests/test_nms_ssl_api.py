#!/usr/bin/env python3
"""SSL 调查 API 测试（httpx ASGI，内存 Redis/存储替换，不触真实 NMS）。"""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

import config  # noqa: E402
import webchat_api  # noqa: E402
from api.routers import agents as agents_router  # noqa: E402
from core import nms_ssl_workflow as w  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

NOW = "2026-10-23T10:00:00+00:00"


def ssl_alarm(severity="critical", **over):
    a = {
        "alarm_id": "srl:100", "serial": "100", "source": "node-ams-01",
        "severity": severity, "summary": "ssl证书可用天数: 0",
        "event_key": "SSL_EXP_1", "event_group": "CERT", "checklist": "SSL证书过期",
        "first_time": NOW, "last_time": NOW,
        "fields": {"Summary": "ssl证书可用天数: 0 (app=a, position=ams.example.com:443)"},
    }
    a.update(over)
    return a


SNAPSHOT = {"timestamp": NOW, "total": 2,
            "alarms": [ssl_alarm(), ssl_alarm(alarm_id="srl:200", serial="200",
                                               source="node-db-02", severity="major",
                                               summary="磁盘使用率 95%",
                                               event_key="DISK_1", checklist="",
                                               fields={"Summary": "磁盘使用率 95%"})]}


class FakeRedis:
    def __init__(self):
        self.kv = {f"{config.REDIS_PREFIX}:nms_alarms:latest": json.dumps(SNAPSHOT),
                   config.KEY_USERS: {}}

    async def get(self, key):
        return self.kv.get(key)

    async def hget(self, key, field):
        val = self.kv.get(key, {})
        if isinstance(val, dict):
            return val.get(field)
        return None

    async def hset(self, key, field=None, value=None, mapping=None):
        self.kv.setdefault(key, {})
        if not isinstance(self.kv[key], dict):
            self.kv[key] = {}
        if mapping:
            self.kv[key].update({str(k): str(v) for k, v in mapping.items()})
        elif field is not None:
            self.kv[key][field] = value
        return 1

    async def hgetall(self, key):
        val = self.kv.get(key, {})
        return dict(val) if isinstance(val, dict) else {}

    async def hdel(self, key, *fields):
        bucket = self.kv.get(key)
        if not isinstance(bucket, dict):
            return 0
        return sum(1 for f in fields if bucket.pop(f, None) is not None)

    async def hincrby(self, key, field, amount=1):
        bucket = self.kv.setdefault(key, {})
        bucket[field] = int(bucket.get(field) or 0) + int(amount)
        return bucket[field]

    async def hlen(self, key):
        bucket = self.kv.get(key)
        return len(bucket) if isinstance(bucket, dict) else 0

    async def rpush(self, key, *values):
        bucket = self.kv.setdefault(key, [])
        bucket.extend(values)
        return len(bucket)

    async def ltrim(self, key, start, end):
        return True

    async def lrange(self, key, start, end):
        bucket = self.kv.get(key) or []
        return list(bucket)[start:] if end == -1 else list(bucket)[start:end]

    async def publish(self, channel, message):
        return 1

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def delete(self, key):
        self.kv.pop(key, None)
        return 1

    def pipeline(self):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis):
        self.redis, self.ops = redis, []

    def hset(self, *args, **kwargs):
        self.ops.append((args, kwargs))
        return self

    async def execute(self):
        for args, kwargs in self.ops:
            await self.redis.hset(*args, **kwargs)
        self.ops = []
        return [1]


async def _fake_get_redis():
    return _FAKE.r


_FAKE = type("F", (), {})()


class SslApiBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = w.MemoryNmsSslStore()
        w.set_store(self.store)
        self.r = FakeRedis()
        _FAKE.r = self.r
        self.r.kv[config.KEY_USERS] = {
            "alice": json.dumps({"role": "user"}),
            "bob": json.dumps({"role": "user"}),
            "root": json.dumps({"role": "admin"}),
        }
        self.current = {"user": "alice"}
        self._orig_get_redis = (webchat_api.get_redis, agents_router.get_redis)
        webchat_api.get_redis = _fake_get_redis
        agents_router.get_redis = _fake_get_redis
        self._orig_verify = webchat_api.app.dependency_overrides.get(webchat_api.verify_token)

        async def _verify():
            return self.current["user"]
        webchat_api.app.dependency_overrides[webchat_api.verify_token] = _verify
        self.client = AsyncClient(transport=ASGITransport(app=webchat_api.app),
                                  base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        webchat_api.get_redis, agents_router.get_redis = self._orig_get_redis
        if self._orig_verify is None:
            webchat_api.app.dependency_overrides.pop(webchat_api.verify_token, None)
        else:
            webchat_api.app.dependency_overrides[webchat_api.verify_token] = self._orig_verify

    def body(self, **over):
        base = {"alarm_id": "srl:100", "snapshot_timestamp": NOW,
                "snapshot_ref": w.snapshot_ref("alice", f"{config.REDIS_PREFIX}:nms_alarms",
                                               SNAPSHOT)}
        base.update(over)
        return base

    async def create(self, **over):
        r = await self.client.post("/api/nms/ssl-investigations", json=self.body(**over))
        return r

    async def mark_recovered(self, inv_id):
        raw = await self.store.get_inv(inv_id)
        inv = json.loads(raw)
        inv["status"] = "recovered"
        inv["manual_completed_at"] = NOW
        inv["recovered_at"] = NOW
        inv["clear_evidence"] = {"observed_at": NOW, "serial": "555"}
        await self.store.cas_inv(inv_id, raw, json.dumps(inv))


class TestCreateApi(SslApiBase):
    async def test_create_202_then_reuse_200(self):
        r = await self.create()
        self.assertEqual(r.status_code, 202, r.text)
        inv = r.json()["investigation"]
        self.assertEqual(inv["target"], {"host": "ams.example.com", "port": 443})
        r2 = await self.create()
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["investigation"]["id"], inv["id"])

    async def test_stale_snapshot_409(self):
        r = await self.create(snapshot_timestamp="2026-10-23T09:00:00+00:00")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "snapshot_stale")

    async def test_snapshot_ref_mismatch_409(self):
        r = await self.create(snapshot_ref="deadbeef")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "snapshot_mismatch")

    async def test_non_ssl_alarm_422(self):
        r = await self.create(alarm_id="srl:200")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["code"], "not_ssl_alarm")

    async def test_missing_alarm_404(self):
        r = await self.create(alarm_id="srl:999")
        self.assertEqual(r.status_code, 404)

    async def test_non_whitelisted_port_creates_manual(self):
        """非 443 端口（如 1280）不再 422，而是转人工核验调查并能看到恢复口径。"""
        snap = {"timestamp": NOW, "total": 1, "alarms": [
            ssl_alarm(alarm_id="srl:300", serial="300",
                      fields={"Summary": "ssl证书可用天数: 0 (position=ams.example.com:1280)"})]}
        self.r.kv[f"{config.REDIS_PREFIX}:nms_alarms:latest"] = json.dumps(snap)
        body = {"alarm_id": "srl:300", "snapshot_timestamp": NOW,
                "snapshot_ref": w.snapshot_ref("alice", f"{config.REDIS_PREFIX}:nms_alarms", snap)}
        with patch.object(config, "NMS_SSL_ALLOWED_PORTS", [443]):
            r = await self.client.post("/api/nms/ssl-investigations", json=body)
        self.assertEqual(r.status_code, 202, r.text)
        inv = r.json()["investigation"]
        self.assertEqual(inv["verification"], "manual")
        self.assertEqual(inv["status"], "awaiting_action")
        self.assertEqual(inv["target"], {"host": "ams.example.com", "port": 1280})
        detail = (await self.client.get(f"/api/nms/ssl-investigations/{inv['id']}")).json()
        self.assertEqual(detail["recovery"]["mode"], "manual")
        self.assertEqual(detail["recovery"]["needs"], ["manual_action", "nms_clear_quiet"])

    async def test_other_user_cannot_read(self):
        inv = (await self.create()).json()["investigation"]
        self.current["user"] = "bob"
        r = await self.client.get(f"/api/nms/ssl-investigations/{inv['id']}")
        self.assertEqual(r.status_code, 404)
        listing = await self.client.get("/api/nms/ssl-investigations")
        self.assertEqual(listing.json()["items"], [])

    async def test_list_and_detail(self):
        inv = (await self.create()).json()["investigation"]
        listing = await self.client.get("/api/nms/ssl-investigations")
        self.assertEqual([i["id"] for i in listing.json()["items"]], [inv["id"]])
        detail = await self.client.get(f"/api/nms/ssl-investigations/{inv['id']}")
        self.assertEqual(detail.json()["status"], "queued")
        self.assertIn("recovery", detail.json())


class TestActionsApi(SslApiBase):
    async def test_manual_completed_validation_and_transition(self):
        inv = (await self.create()).json()["investigation"]
        bad = await self.client.post(f"/api/nms/ssl-investigations/{inv['id']}/manual-completed",
                                     json={"category": "renewed", "note": ""})
        self.assertEqual(bad.status_code, 422)
        good = await self.client.post(f"/api/nms/ssl-investigations/{inv['id']}/manual-completed",
                                      json={"category": "renewed", "note": "已续期",
                                            "expected_version": inv["version"]})
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(good.json()["status"], "observing")

    async def test_pause_and_resume(self):
        inv = (await self.create()).json()["investigation"]
        p = await self.client.post(f"/api/nms/ssl-investigations/{inv['id']}/pause",
                                   json={"reason": "wait"})
        self.assertEqual(p.json()["status"], "paused")
        r = await self.client.post(f"/api/nms/ssl-investigations/{inv['id']}/resume")
        self.assertIn(r.json()["status"], ("awaiting_action", "observing"))

    async def test_case_flow_and_self_review_guard(self):
        inv = (await self.create()).json()["investigation"]
        not_ready = await self.client.post(f"/api/nms/ssl-investigations/{inv['id']}/case",
                                           json={"root_cause": "x", "action": "y"})
        self.assertEqual(not_ready.status_code, 409)
        await self.mark_recovered(inv["id"])
        made = await self.client.post(f"/api/nms/ssl-investigations/{inv['id']}/case",
                                      json={"root_cause": "证书未续期", "action": "更新证书",
                                            "conditions": "仅 443"})
        self.assertEqual(made.status_code, 200, made.text)
        case_id = made.json()["id"]
        self.current["user"] = "root"
        approved = await self.client.post(f"/api/nms/ssl-cases/{case_id}/review",
                                          json={"action": "approve", "note": "已核对"})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "approved")
        revoke = await self.client.post(f"/api/nms/ssl-cases/{case_id}/revoke",
                                        json={"note": "证据撤回"})
        self.assertEqual(revoke.json()["status"], "revoked")

    async def test_non_admin_review_forbidden(self):
        inv = (await self.create()).json()["investigation"]
        await self.mark_recovered(inv["id"])
        case = (await self.client.post(f"/api/nms/ssl-investigations/{inv['id']}/case",
                                       json={"root_cause": "x", "action": "y"})).json()
        r = await self.client.post(f"/api/nms/ssl-cases/{case['id']}/review",
                                   json={"action": "approve"})
        self.assertEqual(r.status_code, 403)

    async def test_feedback(self):
        inv = (await self.create()).json()["investigation"]
        r = await self.client.post(f"/api/nms/ssl-investigations/{inv['id']}/feedback",
                                   json={"helpful": False, "note": "建议有误"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["feedback"][-1]["helpful"], False)


class TestCoverageApi(SslApiBase):
    """闭环覆盖度接口：只读、仅管理员。"""

    async def test_admin_reads_coverage(self):
        self.current["user"] = "root"
        r = await self.client.get("/api/nms/closure-coverage")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total_active"], 2)
        self.assertEqual(body["checklist_hit"], 1)
        self.assertEqual({row["kind"] for row in body["by_kind"]},
                         {"certificate", "storage"})
        self.assertTrue(body["recommendations"])

    async def test_non_admin_forbidden(self):
        r = await self.client.get("/api/nms/closure-coverage")
        self.assertEqual(r.status_code, 403)


async def _closure_noop(*args, **kwargs):
    return None


class TestClosureApi(SslApiBase):
    """通用告警闭环接口：写操作限管理员，确认后进入自动恢复核验。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from unittest.mock import patch
        from core import alert_closure_engine as engine
        self.engine = engine
        engine.set_redis(self.r)
        self._probe_patch = patch.object(engine, "_safe_probe", _closure_noop)
        self._probe_patch.start()

    async def asyncTearDown(self):
        self._probe_patch.stop()
        self.engine.set_redis(None)
        await super().asyncTearDown()

    async def test_summary_admin_only(self):
        r = await self.client.get("/api/nms/closure/summary")
        self.assertEqual(r.status_code, 403)
        self.current["user"] = "root"
        r = await self.client.get("/api/nms/closure/summary")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["units"], 0)
        self.assertEqual(r.json()["thresholds"]["quiet_s"], config.NMS_CLOSURE_QUIET_S)

    async def test_refresh_confirm_detail_and_case_guard(self):
        self.current["user"] = "root"
        r = await self.client.post("/api/nms/closure/refresh")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["units_total"], 2)
        self.assertEqual(r.json()["by_status"], {"observed": 2})
        r = await self.client.get("/api/nms/closure/units?tier=L2")
        keys = {row["key"] for row in r.json()["items"]}
        self.assertIn("storage:node-db-02", keys)
        key = "storage:node-db-02"
        r = await self.client.post(f"/api/nms/closure/units/{key}/confirm",
                                   json={"root_cause": "容量不足", "note": "扩容中"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "confirmed")
        r = await self.client.get(f"/api/nms/closure/units/{key}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["unit"]["root_cause"], "容量不足")
        self.assertTrue(r.json()["events"])
        r = await self.client.post(f"/api/nms/closure/units/{key}/case",
                                   json={"resolution": "扩容"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "unit_not_recovered")

    async def test_pause_resume_and_missing_unit(self):
        self.current["user"] = "root"
        await self.client.post("/api/nms/closure/refresh")
        key = "storage:node-db-02"
        r = await self.client.post(f"/api/nms/closure/units/{key}/pause",
                                   json={"note": "变更窗口"})
        self.assertEqual(r.json()["status"], "paused")
        r = await self.client.post(f"/api/nms/closure/units/{key}/resume")
        self.assertEqual(r.json()["status"], "confirmed")
        r = await self.client.post("/api/nms/closure/units/storage:ghost/confirm", json={})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"]["code"], "unit_not_found")

    async def test_cases_endpoint_and_non_admin_write(self):
        r = await self.client.post("/api/nms/closure/refresh")
        self.assertEqual(r.status_code, 403)
        self.current["user"] = "root"
        r = await self.client.get("/api/nms/closure/cases")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total"], 0)


class TestDisabled(SslApiBase):
    async def test_disabled_returns_503(self):
        from unittest.mock import patch
        with patch.object(config, "NMS_SSL_WORKFLOW_ENABLED", False):
            r = await self.create()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["detail"]["code"], "workflow_disabled")


class TestMetricsApi(SslApiBase):
    """只读指标接口：仅管理员可读，采集计数来自 Redis（agent 进程写入）。"""

    async def test_admin_reads_metrics(self):
        self.current["user"] = "root"
        r = await self.client.get("/api/nms/ssl-metrics")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["enabled"])
        self.assertIn("lease_renew_failures", body["metrics"])
        self.assertIn("metrics_log_interval_s", body["config"])
        self.assertIsNone(body["collection_remote"])

    async def test_metrics_merges_remote_collection(self):
        self.current["user"] = "root"
        self.r.kv[f"{config.REDIS_PREFIX}:nms:ssl:metrics"] = {
            "collection_rounds": "4", "collection_errors": "1",
            f"scope:{config.REDIS_PREFIX}:nms_alarms:rounds": "4",
            f"scope:{config.REDIS_PREFIX}:nms_alarms:errors": "1",
            "last_scope": f"{config.REDIS_PREFIX}:nms_alarms",
            "last_error": "timeout", "last_at": NOW,
        }
        r = await self.client.get("/api/nms/ssl-metrics")
        body = r.json()
        remote = body["collection_remote"]
        self.assertEqual(remote["collection_rounds"], 4)
        self.assertAlmostEqual(remote["error_rate"], 25.0)
        self.assertEqual(remote["scopes"][f"{config.REDIS_PREFIX}:nms_alarms"],
                         {"rounds": 4, "errors": 1})
        # metrics 中的采集段也合并远程计数（backend 本进程不采集）
        self.assertEqual(body["metrics"]["collection_rounds"], 4)
        self.assertAlmostEqual(body["metrics"]["collection_error_rate"], 25.0)

    async def test_non_admin_forbidden(self):
        r = await self.client.get("/api/nms/ssl-metrics")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
