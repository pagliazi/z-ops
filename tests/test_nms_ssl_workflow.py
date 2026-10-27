#!/usr/bin/env python3
"""SSL 调查闭环状态机 / 持久化 / 案例审核测试（内存存储，不触真实 Redis/NMS/LLM）。"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

import config  # noqa: E402
from core import nms_ssl_workflow as w  # noqa: E402

SB = "zops:nms_alarms"
ALARM = {
    "alarm_id": "srl:100", "serial": "100", "source": "node-ams-01",
    "severity": "critical", "summary": "ssl证书可用天数: 0",
    "event_key": "SSL_EXP_1", "event_group": "CERT",
    "fields": {"Summary": "ssl证书可用天数: 0 (app=a, ip=10.0.0.5, "
                          "position=ams.example.com:443)"},
}
ADMIN = {"role": "admin"}


def healthy(now, fp="aa", ip="10.0.0.5"):
    return {"diagnosis": "healthy", "defect_type": "ok", "observed_at": w._iso(now),
            "addresses": [ip], "certificates": [
                {"ip": ip, "fingerprint": fp, "verified": True, "remaining_seconds": 90 * 86400}]}


def unhealthy(now, defect="expired"):
    return {"diagnosis": "unhealthy", "defect_type": defect, "observed_at": w._iso(now),
            "addresses": [], "certificates": [], "error": defect}


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = w.MemoryNmsSslStore()
        w.set_store(self.store)

    async def _create(self, owner="alice", base=SB, alarm=None):
        inv, created = await w.create_investigation(
            owner=owner, scope_base=base, alarm=alarm or ALARM,
            snapshot_timestamp="2026-10-23T10:00:00+00:00", snapshot_ref_value="ref1")
        self.assertTrue(created)
        return inv

    def _put(self, inv):
        self.store.invs[inv["id"]] = json.dumps(inv, ensure_ascii=False)

    async def _seed(self, status="observing", manual_ago=600, healthy_now=True, **over):
        inv = await self._create()
        now = w._now()
        inv["status"] = status
        inv["manual_completed_at"] = w._iso(now - manual_ago) if manual_ago is not None else None
        if healthy_now:
            inv["health"] = {"diagnosis": "healthy", "defect_type": "ok",
                             "observed_at": w._iso(now - 10)}
            inv["last_check_at"] = w._iso(now - 10)
        inv.update(over)
        self._put(inv)
        return inv


class TestCreate(Base):
    async def test_idempotent_by_fingerprint(self):
        inv1 = await self._create()
        inv2, created = await w.create_investigation(
            owner="alice", scope_base=SB, alarm=ALARM,
            snapshot_timestamp="2026-10-23T10:05:00+00:00", snapshot_ref_value="ref2")
        self.assertFalse(created)
        self.assertEqual(inv1["id"], inv2["id"])
        self.assertEqual(len(inv2["source_refs"]), 2)

    async def test_idempotency_key_returns_cached(self):
        inv1 = await self._create()
        store = await w.get_store()
        await store.set_idem("alice", "create:k1", json.dumps(inv1))
        inv2, created = await w.create_investigation(
            owner="alice", scope_base=SB, alarm=ALARM, snapshot_timestamp="t",
            snapshot_ref_value="r", idempotency_key="k1")
        self.assertFalse(created)
        self.assertEqual(inv1["id"], inv2["id"])

    async def test_quota(self):
        with patch.object(config, "NMS_SSL_MAX_ACTIVE_PER_USER", 1):
            await self._create()
            alarm2 = dict(ALARM, event_key="SSL_EXP_2")
            with self.assertRaises(w.WorkflowError) as ctx:
                await w.create_investigation(owner="alice", scope_base=SB, alarm=alarm2,
                                             snapshot_timestamp="t", snapshot_ref_value="r")
            self.assertEqual(ctx.exception.status, 429)

    async def test_unstable_identity_rejected(self):
        bad = dict(ALARM, event_key="", event_group="")
        with self.assertRaises(w.WorkflowError) as ctx:
            await w.create_investigation(owner="alice", scope_base=SB, alarm=bad,
                                         snapshot_timestamp="t", snapshot_ref_value="r")
        self.assertEqual(ctx.exception.status, 422)

    async def test_invalid_target_rejected(self):
        bad = dict(ALARM, fields={"Summary": "ssl证书 position=10.0.0.9:443"})
        with self.assertRaises(w.WorkflowError) as ctx:
            await w.create_investigation(owner="alice", scope_base=SB, alarm=bad,
                                         snapshot_timestamp="t", snapshot_ref_value="r")
        self.assertEqual(ctx.exception.status, 422)

    async def test_other_users_cannot_read(self):
        inv = await self._create()
        with self.assertRaises(w.WorkflowError) as ctx:
            await w.get_investigation("bob", inv["id"])
        self.assertEqual(ctx.exception.status, 404)


class TestTransitions(Base):
    async def test_first_probe_moves_to_awaiting_action(self):
        inv = await self._create()
        out = w.apply_probe_result(inv, unhealthy(w._now()), w._now())
        self.assertEqual(out["status"], "awaiting_action")
        self.assertEqual(out["health"]["defect_type"], "expired")

    async def test_manual_completed_starts_observing(self):
        inv = await self._create()
        inv["status"] = "awaiting_action"
        self._put(inv)
        out = await w.manual_completed("alice", inv["id"], "renewed", "已更换证书")
        self.assertEqual(out["status"], "observing")
        self.assertTrue(out["manual_completed_at"])
        with self.assertRaises(w.WorkflowError):
            await w.manual_completed("alice", inv["id"], "bad_category", "x")
        with self.assertRaises(w.WorkflowError):
            await w.manual_completed("alice", inv["id"], "renewed", "  ")

    async def test_three_healthy_with_spacing_then_waiting_nms(self):
        inv = await self._seed(status="observing", manual_ago=600)
        now = w._now()
        steps = [0, 60.0, 121.0, 181.0]
        for i, delta in enumerate(steps):
            out = w.apply_probe_result(inv, healthy(now + delta), now + delta)
            inv = out
            if i < 2:
                self.assertEqual(out["healthy_streak"], i + 1, f"step {i}")
        self.assertEqual(inv["status"], "waiting_nms")
        self.assertEqual(inv["healthy_streak"], 3)

    async def test_spacing_below_60s_not_counted(self):
        inv = await self._seed(status="observing", manual_ago=600)
        now = w._now()
        inv = w.apply_probe_result(inv, healthy(now), now)
        inv = w.apply_probe_result(inv, healthy(now + 10), now + 10)
        self.assertEqual(inv["healthy_streak"], 1)

    async def test_signature_change_resets_streak(self):
        inv = await self._seed(status="observing", manual_ago=600)
        now = w._now()
        inv = w.apply_probe_result(inv, healthy(now), now)
        inv = w.apply_probe_result(inv, healthy(now + 60), now + 60)
        self.assertEqual(inv["healthy_streak"], 2, msg=json.dumps({
            k: inv.get(k) for k in ("status", "healthy_streak", "last_check_at",
                                    "manual_completed_at", "streak_last_at")}, default=str))
        inv = w.apply_probe_result(inv, healthy(now + 121, fp="bb"), now + 121)
        self.assertEqual(inv["healthy_streak"], 1)

    async def test_unhealthy_resets_streak_and_regresses_from_waiting(self):
        inv = await self._seed(status="waiting_nms", manual_ago=600, healthy_streak=3)
        now = w._now()
        out = w.apply_probe_result(inv, unhealthy(now, "untrusted"), now)
        self.assertEqual(out["status"], "observing")
        self.assertEqual(out["healthy_streak"], 0)

    async def test_unknown_does_not_promote(self):
        inv = await self._seed(status="observing", manual_ago=600)
        now = w._now()
        out = w.apply_probe_result(
            inv, {"diagnosis": "unknown", "defect_type": "timeout",
                  "observed_at": w._iso(now), "addresses": [], "certificates": []}, now)
        self.assertEqual(out["healthy_streak"], 0)
        self.assertEqual(out["status"], "observing")

    async def test_stale_probe_evidence_not_counted(self):
        """超过 5 分钟的 TLS 实测属于过期证据，不计入连续健康。"""
        inv = await self._seed(status="observing", manual_ago=600)
        now = w._now()
        out = w.apply_probe_result(inv, healthy(now - 400), now)
        self.assertEqual(out["status"], "observing")
        self.assertEqual(int(out.get("healthy_streak") or 0), 0)

    async def test_expired_clear_evidence_blocks_recovery(self):
        """清除证据年龄超过 max(2×间隔, 300s) 时不得恢复。"""
        inv = await self._seed(status="waiting_nms", manual_ago=600, healthy_streak=3)
        inv["clear_evidence"] = {"observed_at": w._iso(w._now() - 400), "serial": "555"}
        self._put(inv)
        now = w._now()
        out = w.apply_probe_result(inv, healthy(now), now)
        self.assertEqual(out["status"], "waiting_nms")

    async def test_24h_window_pauses(self):
        inv = await self._seed(status="observing", manual_ago=86401)
        now = w._now()
        out = w.apply_probe_result(inv, healthy(now), now)
        self.assertEqual(out["status"], "paused")
        self.assertEqual(out["pause_reason"], "track_window_expired")

    async def test_pause_and_resume_restart_window(self):
        inv = await self._seed(status="observing", manual_ago=600)
        paused = await w.pause_investigation("alice", inv["id"], "user_paused")
        self.assertEqual(paused["status"], "paused")
        self.assertNotIn(inv["id"], self.store.due)
        resumed = await w.resume_investigation("alice", inv["id"])
        self.assertEqual(resumed["status"], "observing")
        self.assertGreater(w._parse_iso(resumed["manual_completed_at"]),
                           w._parse_iso(inv["manual_completed_at"]))


class TestObservations(Base):
    def _clear_row(self, **over):
        row = {
            "serial": "555", "source": "node-ams-01", "severity": "cleared",
            "event_key": "SSL_EXP_1", "event_group": "CERT", "last_time": "2026-10-23T11:00:00Z",
            "fields": {"Summary": "ssl证书可用天数: 0 (position=ams.example.com:443)"},
        }
        row.update(over)
        return row

    async def test_clear_row_recorded(self):
        inv = await self._seed(status="observing", manual_ago=600)
        touched = await w.observe_snapshot(SB, [self._clear_row()])
        self.assertEqual(touched, 1)
        out = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(out["clear_evidence"]["serial"], "555")
        self.assertEqual(out["nms_last_result"], "rows")

    async def test_waiting_nms_plus_clear_becomes_recovered(self):
        inv = await self._seed(status="waiting_nms", manual_ago=600, healthy_streak=3)
        await w.observe_snapshot(SB, [self._clear_row()])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(out["status"], "recovered")
        self.assertNotIn(inv["id"], self.store.due)

    async def test_missing_serial_is_not_clear(self):
        inv = await self._seed(status="observing", manual_ago=600)
        await w.observe_snapshot(SB, [self._clear_row(serial="")])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertIsNone(out.get("clear_evidence"))

    async def test_active_replacement_blocks_clear(self):
        inv = await self._seed(status="waiting_nms", manual_ago=600, healthy_streak=3)
        active = self._clear_row(severity="critical", serial="777")
        await w.observe_snapshot(SB, [self._clear_row(), active])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertIsNone(out.get("clear_evidence"))
        self.assertEqual(out["status"], "waiting_nms")

    async def test_different_target_is_not_clear(self):
        inv = await self._seed(status="observing", manual_ago=600)
        other = self._clear_row(fields={"Summary": "position=other.example.com:443"})
        await w.observe_snapshot(SB, [other])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertIsNone(out.get("clear_evidence"))

    async def test_empty_snapshot_does_not_clear(self):
        inv = await self._seed(status="waiting_nms", manual_ago=600, healthy_streak=3)
        await w.observe_snapshot(SB, [])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertIsNone(out.get("clear_evidence"))
        self.assertEqual(out["nms_last_result"], "empty")
        self.assertEqual(out["status"], "waiting_nms")

    async def test_clear_before_manual_completion_not_trusted(self):
        inv = await self._seed(status="waiting_nms", manual_ago=-100, healthy_streak=3)
        await w.observe_snapshot(SB, [self._clear_row()])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(out["status"], "waiting_nms")

    async def test_identifier_list_serial_is_not_clear(self):
        """行内 Serial 缺失（退回 identifierList）的身份不可靠行不能作为清除证据。"""
        inv = await self._seed(status="waiting_nms", manual_ago=600, healthy_streak=3)
        await w.observe_snapshot(
            SB, [self._clear_row(serial_source="identifier_list")])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertIsNone(out.get("clear_evidence"))
        self.assertEqual(out["status"], "waiting_nms")

    async def test_collection_error_recorded_but_never_recovers(self):
        inv = await self._seed(status="waiting_nms", manual_ago=600, healthy_streak=3)
        touched = await w.observe_collection_error(SB, "主集抓取失败: timeout")
        self.assertEqual(touched, 1)
        out = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(out["status"], "waiting_nms")
        self.assertEqual(out["nms_last_result"], "error")
        self.assertEqual(out["nms_last_error"], "主集抓取失败: timeout")
        self.assertEqual(out["nms_clear_note"], "采集异常，不能作为清除证据")
        self.assertIsNone(out.get("clear_evidence"))
        # 连续同类异常不重复写时间线
        await w.observe_collection_error(SB, "again")
        out2 = json.loads(self.store.invs[inv["id"]])
        events = [t for t in out2.get("timeline") or [] if t.get("event") == "nms_collect_error"]
        self.assertEqual(len(events), 1)


class TestCases(Base):
    async def _recovered(self, owner="alice", base=SB, alarm=None):
        inv = await self._create(owner=owner, base=base, alarm=alarm)
        made = await w.manual_completed(owner, inv["id"], "renewed", "已续期")
        now = w._now()
        inv2 = w.apply_probe_result(made, healthy(now), now)
        inv2["status"] = "recovered"
        inv2["recovered_at"] = w._iso(now)
        inv2["clear_evidence"] = {"observed_at": w._iso(now), "serial": "555"}
        self._put(inv2)
        return inv2

    async def test_only_recovered_can_submit(self):
        inv = await self._create()
        with self.assertRaises(w.WorkflowError) as ctx:
            await w.submit_case("alice", inv["id"], "根因", "操作", "条件")
        self.assertEqual(ctx.exception.status, 409)

    async def test_review_flow_and_self_review_guard(self):
        inv = await self._recovered()
        case = await w.submit_case("alice", inv["id"], "证书未续期", "更新证书", "仅 443")
        self.assertEqual(case["status"], "pending")
        with self.assertRaises(w.WorkflowError) as ctx:
            await w.review_case("alice", {"role": "admin"}, case["id"], "approve")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(w.WorkflowError):
            await w.review_case("root", ADMIN, case["id"], "return", "")
        approved = await w.review_case("root", ADMIN, case["id"], "approve", "已核对证据")
        self.assertEqual(approved["status"], "approved")
        with self.assertRaises(w.WorkflowError):
            await w.review_case("root", ADMIN, case["id"], "approve")
        with self.assertRaises(w.WorkflowError) as ctx:
            await w.review_case("alice", {"role": "user"}, case["id"], "approve")
        self.assertEqual(ctx.exception.status, 403)

    async def test_recurrence_links_previous_investigation(self):
        inv = await self._recovered()
        recurring = await self._create()
        self.assertNotEqual(recurring["id"], inv["id"])
        self.assertEqual(recurring.get("previous_investigation_id"), inv["id"])
        events = [t.get("event") for t in recurring.get("timeline") or []]
        self.assertIn("recurrence", events)

    async def test_citation_visible_and_personal_isolated(self):
        inv = await self._recovered()
        case = await w.submit_case("alice", inv["id"], "证书未续期", "更新证书", "")
        await w.review_case("root", ADMIN, case["id"], "approve")
        recurring = await self._create()  # 已恢复 → 同指纹重新建调查
        self.assertNotEqual(recurring["id"], inv["id"])
        detail = await w.get_investigation("alice", recurring["id"])
        self.assertEqual([c["id"] for c in detail["cases"]], [case["id"]])
        bob = await self._create(owner="bob", base="zops:nms_alarms:user:bob")
        bob_detail = await w.get_investigation("bob", bob["id"])
        self.assertEqual(bob_detail["cases"], [])

    async def test_revoke_requires_note_and_hides_citation(self):
        inv = await self._recovered()
        case = await w.submit_case("alice", inv["id"], "根因", "操作", "")
        await w.review_case("root", ADMIN, case["id"], "approve")
        with self.assertRaises(w.WorkflowError):
            await w.revoke_case("alice", {"role": "user"}, case["id"], "")
        await w.revoke_case("alice", {"role": "user"}, case["id"], "证据不足")
        recurring = await self._create()
        detail = await w.get_investigation("alice", recurring["id"])
        self.assertEqual(detail["cases"], [])

    async def test_feedback_is_permanent(self):
        inv = await self._create()
        await w.add_feedback("alice", inv["id"], False, "建议有误")
        raw = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(raw["feedback"][0]["helpful"], False)


class TestWorker(Base):
    async def test_worker_probes_and_never_calls_llm(self):
        inv = await self._create()
        called = {}

        async def fake_probe(target, policy):
            called["target"] = target
            return healthy(w._now())

        with patch("agents.ssl_check.probe_cert", fake_probe), \
             patch.object(config, "NMS_SSL_GLOBAL_CONCURRENCY", 1):
            ok = await w._run_one(self.store, inv["id"])
        self.assertTrue(ok)
        self.assertEqual(called["target"], {"host": "ams.example.com", "port": 443})
        out = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(out["status"], "awaiting_action")
        self.assertNotIn(inv["id"], self.store.due)
        # 闭环不触碰任何 LLM 客户端
        self.assertFalse(hasattr(w, "llm") or hasattr(w, "ask_llm"))

    async def test_worker_respects_lease(self):
        inv = await self._create()
        await self.store.acquire_lease(inv["id"], "other", 60)
        with patch("agents.ssl_check.probe_cert") as probe:
            ok = await w._run_one(self.store, inv["id"])
        self.assertFalse(ok)
        probe.assert_not_called()

    async def test_long_probe_renews_lease_periodically(self):
        """60 秒租约探测期间必须持续续租，而不是只在探测结束补一次。"""
        inv = await self._create()
        renewals = []
        orig = self.store.renew_lease

        async def counting(inv_id, token, ttl):
            renewals.append(ttl)
            return await orig(inv_id, token, ttl)

        async def slow_probe(target, policy):
            await asyncio.sleep(0.35)
            return healthy(w._now())

        with patch("agents.ssl_check.probe_cert", slow_probe), \
             patch.object(self.store, "renew_lease", counting), \
             patch.object(config, "NMS_SSL_GLOBAL_CONCURRENCY", 1), \
             patch.object(w, "LEASE_RENEW_SECONDS", 0.1):
            ok = await w._run_one(self.store, inv["id"])
        self.assertTrue(ok)
        # 0.35s 探测在 0.1s 心跳下至少 3 次心跳，再加提交前确认
        self.assertGreaterEqual(len(renewals), 4)
        self.assertTrue(all(ttl == w.LEASE_TTL_SECONDS for ttl in renewals))
        self.assertEqual(json.loads(self.store.invs[inv["id"]])["status"], "awaiting_action")

    async def test_lost_lease_aborts_probe_and_discards_result(self):
        inv = await self._create()
        before = json.loads(self.store.invs[inv["id"]])
        cancelled = {"probe": False}

        async def slow_probe(target, policy):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled["probe"] = True
                raise
            return healthy(w._now())

        async def renew_fails(inv_id, token, ttl):
            return False  # 围栏令牌已被他人接管

        with patch("agents.ssl_check.probe_cert", slow_probe), \
             patch.object(self.store, "renew_lease", renew_fails), \
             patch.object(config, "NMS_SSL_GLOBAL_CONCURRENCY", 1), \
             patch.object(w, "LEASE_RENEW_SECONDS", 0.05):
            ok = await w._run_one(self.store, inv["id"])
        self.assertFalse(ok)
        self.assertTrue(cancelled["probe"], "失去租约必须终止探测")
        # 旧响应不得提交：状态、版本、证据均保持原样
        out = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out.get("version"), before.get("version"))
        self.assertEqual(len(out.get("timeline") or []), len(before.get("timeline") or []))
        self.assertNotIn("probes", out)


class TestNoSideEffects(Base):
    """闭环不调用 LLM、不 ACK/屏蔽告警、不写生产配置。"""

    async def test_full_cycle_has_no_llm_ack_or_production_calls(self):
        from unittest.mock import MagicMock
        import pathlib

        inv = await self._seed(status="waiting_nms", manual_ago=60, healthy_streak=3)
        clear_row = {
            "serial": "555", "source": "node-ams-01", "severity": "cleared",
            "event_key": "SSL_EXP_1", "event_group": "CERT",
            "last_time": "2026-10-23T11:00:00Z",
            "fields": {"Summary": "ssl证书可用天数: 0 (position=ams.example.com:443)"},
        }
        forbidden = MagicMock(side_effect=AssertionError("闭环不应调用该方法"))
        with patch("core.model_gateway.ModelGateway.request_chat", forbidden), \
             patch("agents.nms_agent.NmsAgent._nms_apply_ack_quickfilter", forbidden), \
             patch("agents.nms_agent.NmsAgent._nms_clear_quickfilters", forbidden), \
             patch("agents.nms_agent.NmsAgent._nms_run_once", forbidden):
            touched = await w.observe_snapshot(SB, [clear_row])
            recovered = json.loads(self.store.invs[inv["id"]])
            case = await w.submit_case("alice", inv["id"], "证书未续期", "更新证书", "仅 443")
            await w.review_case("root", ADMIN, case["id"], "approve", "已核对")
            await w._run_one(self.store, inv["id"])
        self.assertEqual(touched, 1)
        self.assertEqual(recovered["status"], "recovered")
        forbidden.assert_not_called()
        # 结构约束：工作流模块不得引入 LLM / ACK / 采集执行入口
        src = pathlib.Path(w.__file__).read_text(encoding="utf-8")
        for banned in ("model_gateway", "request_chat", "NmsAgent", "_nms_apply_ack"):
            self.assertNotIn(banned, src, f"工作流模块不应引用 {banned}")


ALARM_1280 = dict(
    ALARM, alarm_id="srl:1280", serial="1280",
    fields={"Summary": "ssl证书可用天数: 0 (app=a, ip=10.0.0.5, "
                      "position=ams.example.com:1280)"},
)


class TestManualVerification(Base):
    """端口不在自动取证白名单：转人工核验模式，仍需 NMS 清除持续静默才闭环。"""

    def _clear_row(self, severity="cleared", serial="666", **over):
        row = {
            "serial": serial, "source": "node-ams-01", "severity": severity,
            "event_key": "SSL_EXP_1", "event_group": "CERT",
            "last_time": "2026-10-25T11:00:00Z",
            "fields": {"Summary": "ssl证书可用天数: 0 (position=ams.example.com:1280)"},
        }
        row.update(over)
        return row

    async def _create_manual(self):
        # 固定白名单为 443，避免本机 zops.env 扩展端口后测试结果随环境变化
        with patch.object(config, "NMS_SSL_ALLOWED_PORTS", [443]):
            inv, created = await w.create_investigation(
                owner="alice", scope_base=SB, alarm=ALARM_1280,
                snapshot_timestamp="2026-10-25T10:00:00+00:00", snapshot_ref_value="ref1")
        self.assertTrue(created)
        return inv

    def _edit(self, inv_id, **fields):
        inv = json.loads(self.store.invs[inv_id])
        inv.update(fields)
        self.store.invs[inv_id] = json.dumps(inv, ensure_ascii=False)
        return inv

    def _age(self, inv_id, clear_ago):
        """把人工处置时间推到 1 小时前，并设置静默已持续 clear_ago 秒。"""
        now = w._now()
        return self._edit(inv_id, manual_completed_at=w._iso(now - 3600),
                          manual_clear_first_at=w._iso(now - clear_ago))

    async def test_non_whitelisted_port_becomes_manual(self):
        inv = await self._create_manual()
        self.assertEqual(inv["verification"], "manual")
        self.assertEqual(inv["status"], "awaiting_action")
        self.assertEqual(inv["target"], {"host": "ams.example.com", "port": 1280})
        self.assertIn("1280", inv["verification_note"])
        self.assertNotIn(inv["id"], self.store.due)  # 不进入探测调度

    async def test_other_invalid_targets_still_rejected(self):
        bad = dict(ALARM_1280, fields={
            "Summary": "position=a.example.com:1280 position=b.example.com:1280"})
        with patch.object(config, "NMS_SSL_ALLOWED_PORTS", [443]), \
                self.assertRaises(w.WorkflowError) as ctx:
            await w.create_investigation(owner="alice", scope_base=SB, alarm=bad,
                                         snapshot_timestamp="t", snapshot_ref_value="r")
        self.assertEqual(ctx.exception.status, 422)

    async def test_manual_completed_waits_nms_without_due(self):
        inv = await self._create_manual()
        out = await w.manual_completed("alice", inv["id"], "renewed", "已更换证书")
        self.assertEqual(out["status"], "waiting_nms")
        self.assertIsNone(out.get("next_check_at"))
        self.assertNotIn(inv["id"], self.store.due)

    async def test_probe_result_cannot_touch_manual(self):
        inv = await self._create_manual()
        out = w.apply_probe_result(inv, healthy(w._now()), w._now())
        self.assertEqual(out["status"], inv["status"])
        self.assertNotIn("health", out)

    async def test_worker_skips_manual(self):
        from unittest.mock import MagicMock

        inv = await self._create_manual()
        await self.store.add_due(inv["id"], w._now())
        probe = MagicMock(side_effect=AssertionError("人工核验模式不应探测"))
        with patch("agents.ssl_check.probe_cert", probe):
            n = await w.run_due_once()
        self.assertEqual(n, 0)
        self.assertNotIn(inv["id"], self.store.due)
        probe.assert_not_called()

    async def test_clear_before_action_does_not_start_quiet(self):
        inv = await self._create_manual()
        await w.observe_snapshot(SB, [self._clear_row()])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertIsNone(out.get("manual_clear_first_at"))
        self.assertEqual(out["status"], "awaiting_action")

    async def test_quiet_window_required_then_recovers(self):
        inv = await self._create_manual()
        await w.manual_completed("alice", inv["id"], "renewed", "已更换证书")
        await w.observe_snapshot(SB, [self._clear_row()])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(out["status"], "waiting_nms")
        self.assertIsNotNone(out.get("manual_clear_first_at"))
        # 静默未满 1800s：不恢复
        self._age(inv["id"], 60)
        await w.observe_snapshot(SB, [self._clear_row()])
        self.assertEqual(json.loads(self.store.invs[inv["id"]])["status"], "waiting_nms")
        # 静默满 1800s：恢复
        self._age(inv["id"], 1801)
        await w.observe_snapshot(SB, [self._clear_row()])
        done = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(done["status"], "recovered")
        self.assertEqual(done["verification_result"], "manual_clear_quiet")

    async def test_active_row_resets_quiet(self):
        inv = await self._create_manual()
        await w.manual_completed("alice", inv["id"], "renewed", "已更换证书")
        self._age(inv["id"], 1801)
        await w.observe_snapshot(SB, [self._clear_row(severity="critical", serial="777")])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(out["status"], "waiting_nms")
        self.assertIsNone(out.get("manual_clear_first_at"))

    async def test_row_disappearance_keeps_quiet_and_recovers(self):
        """清除行自然消失（无活跃替代）不应重置静默：否则短保留期快照永远无法闭环。"""
        inv = await self._create_manual()
        await w.manual_completed("alice", inv["id"], "renewed", "已更换证书")
        self._age(inv["id"], 1801)
        other = self._clear_row(serial="999", event_key="OTHER", event_group="OTHER",
                                fields={"Summary": "position=other.example.com:443"})
        await w.observe_snapshot(SB, [other])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(out["status"], "recovered")
        self.assertEqual(out["verification_result"], "manual_clear_quiet")

    async def test_empty_snapshot_freezes_quiet(self):
        inv = await self._create_manual()
        await w.manual_completed("alice", inv["id"], "renewed", "已更换证书")
        self._age(inv["id"], 1801)
        await w.observe_snapshot(SB, [])
        out = json.loads(self.store.invs[inv["id"]])
        self.assertEqual(out["status"], "waiting_nms")
        self.assertIsNotNone(out.get("manual_clear_first_at"))

    async def test_recheck_rejected_for_manual(self):
        inv = await self._create_manual()
        with self.assertRaises(w.WorkflowError) as ctx:
            await w.recheck("alice", inv["id"])
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "manual_verification")

    async def test_pause_resume_restarts_quiet(self):
        inv = await self._create_manual()
        await w.manual_completed("alice", inv["id"], "renewed", "已更换证书")
        self._age(inv["id"], 1801)
        await w.pause_investigation("alice", inv["id"], "灰度验证")
        out = await w.resume_investigation("alice", inv["id"])
        self.assertEqual(out["status"], "waiting_nms")
        self.assertIsNone(out.get("manual_clear_first_at"))
        self.assertNotIn(inv["id"], self.store.due)

    async def test_case_records_manual_verification(self):
        inv = await self._create_manual()
        await w.manual_completed("alice", inv["id"], "renewed", "已更换证书")
        self._age(inv["id"], 1801)
        await w.observe_snapshot(SB, [self._clear_row()])
        case = await w.submit_case("alice", inv["id"], "证书到期", "更新证书", "仅 1280 端口")
        self.assertEqual(case["evidence"]["verification"], "manual")
        self.assertEqual(case["evidence"]["verification_result"], "manual_clear_quiet")


if __name__ == "__main__":
    unittest.main()
