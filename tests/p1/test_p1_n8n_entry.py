"""Z-OPS P1 验收测试 — n8n 回调可靠登记与去重 (entry_guard + /api/n8n/webhook)

覆盖:
  1. 首次回调: 登记任务 (channel=n8n) + received 事件 + succeeded 终态 + 永久 JSONL 归档;
  2. 重复回调且前次 succeeded → 判重复, 副作用 (process_webhook) 不再执行;
  3. 前次 failed / unknown / 在途 → 另记一次新尝试 (保留历史终态, 处理照常执行);
  4. 幂等键语义: execution_id / dedupe_key 优先, 否则 payload 摘要 (键顺序无关);
  5. 开关关闭 / 账本异常 → 降级为不登记, 处理链路完全不受影响 (可用性优先);
  6. router 端点: 鉴权失败不登记不处理; 下游异常留 failed (绝不虚报 succeeded);
     unknown_action 不虚报成功, 因此重发不会被误判重复。

隔离: Redis 6399 + 临时归档目录 + 假 n8n integration; 无真实外部系统。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

TEST_REDIS_URL = os.environ.get("ZOPS_P0_TEST_REDIS", "redis://127.0.0.1:6399/0")
os.environ["REDIS_URL"] = TEST_REDIS_URL
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import config  # noqa: E402

RUN = os.environ.get("ZOPS_TEST_RUN") or str(int(time.time() * 1000))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


def body(action="alert", ex=None, **extra):
    b = {"action": action, "message": "cpu high", "level": "warning"}
    b["execution_id"] = ex if ex is not None else f"{RUN}-ex-{len(PASS)}"
    b.update(extra)
    return b


class FakeN8n:
    """替代 core.n8n_integration.get_n8n_integration (记录副作用次数)"""

    def __init__(self):
        self.calls = []
        self.boom = False

    async def process_webhook(self, req):
        self.calls.append(dict(req))
        if self.boom:
            raise RuntimeError("downstream boom")
        action = req.get("action")
        if action in ("alert", "health_report", "backup_result", "workflow_result"):
            return {"processed": True, "action": action}
        return {"processed": False, "action": action, "reason": "unknown_action"}


# ═══ entry_guard 单元语义 ═══

async def case_first_register(rt, eg, tmp):
    b = body("alert", ex=f"{RUN}-u1")
    g = await eg.register(b)
    check("guard: 首次回调登记成功 (registered/未重复)",
          g["registered"] is True and g["duplicate"] is False and g["task_id"], str(g))
    await eg.finish(g, "succeeded", {"processed": True})
    t = await eg.lookup(g)
    check("guard: 任务归属 channel=n8n + 会话/actor 可追溯",
          t.get("channel") == "n8n" and t.get("session_id") == "n8n:alert"
          and t.get("actor") == "n8n", str(t))
    check("guard: succeeded 终态与 delivery 原子落账",
          t.get("status") == "succeeded" and t.get("delivery_status") == "pending", str(t))
    evs = await rt.read_events_after(g["task_id"], 0)
    types = [e["type"] for e in evs]
    check("guard: 永久事件完整 (start→received→terminal)",
          types[0] == "task_started" and "received" in types
          and types[-1] == "task_terminal", str(types))
    arch = Path(tmp) / "tasks" / f"{g['task_id']}.jsonl"
    check("guard: JSONL 永久归档落盘 (回调审计不丢失)", arch.exists() and arch.read_text().strip() != "",
          str(arch))


async def case_duplicate(rt, eg):
    b = body("alert", ex=f"{RUN}-u2")
    g1 = await eg.register(b)
    await eg.finish(g1, "succeeded", {"processed": True})
    ev_n = len(await rt.read_events_after(g1["task_id"], 0))
    g2 = await eg.register(b)
    check("guard: 前次 succeeded 的重发判为重复", g2["duplicate"] is True, str(g2))
    check("guard: 重复回调复用同一任务 (不新建)", g2["task_id"] == g1["task_id"])
    await eg.finish(g2, "succeeded", {})
    check("guard: 重复回调不追加终态 (历史不被覆盖)",
          len(await rt.read_events_after(g1["task_id"], 0)) == ev_n
          and (await eg.lookup(g2))["status"] == "succeeded")


async def case_failed_then_retry(rt, eg):
    b = body("health_report", ex=f"{RUN}-u3")
    g1 = await eg.register(b)
    await eg.finish(g1, "failed", {"error": "downstream"})
    g2 = await eg.register(b)
    check("guard: 前次 failed 的重发不被幂等卡死", g2["duplicate"] is False)
    check("guard: 重试另记新尝试 (保留原失败审计)",
          g2["task_id"] and g2["task_id"] != g1["task_id"]
          and (await eg.lookup(g1))["status"] == "failed", str(g2))
    evs = await rt.read_events_after(g2["task_id"], 0)
    check("guard: 重试任务带 retry_of_status 溯源",
          any(e["type"] == "task_started" and (e.get("data") or {}).get("retry_of_status") == "failed"
              for e in evs), str([e["type"] for e in evs]))


async def case_unknown_then_retry(eg):
    b = body("backup_result", ex=f"{RUN}-u4")
    g1 = await eg.register(b)
    await g1["runtime"].ledger.complete_unknown(g1["task_id"], {"reason": "write_crash"})
    g2 = await eg.register(b)
    check("guard: UNKNOWN 终态不自动重做也不误判重复 (人工核查语义)",
          (await eg.lookup(g1))["status"] == "unknown" and g2["duplicate"] is False
          and g2["task_id"] != g1["task_id"], str(g2))


async def case_inflight(eg):
    b = body("alert", ex=f"{RUN}-u5")
    g1 = await eg.register(b)
    g2 = await eg.register(b)
    check("guard: 在途重复回调仍照常处理 (仅 succeeded 才短路)",
          g2["duplicate"] is False and (await eg.lookup(g1))["status"] == "running")
    await eg.finish(g1, "succeeded", {})
    await eg.finish(g2, "succeeded", {})


async def case_key_semantics(eg):
    k1 = eg.client_key({"execution_id": "e1"})
    k2 = eg.client_key({"dedupe_key": "d9", "action": "alert"})
    k3 = eg.client_key({"action": "alert", "message": "x"})
    k4 = eg.client_key({"message": "x", "action": "alert"})
    check("键: execution_id 优先 / dedupe_key 次之",
          k1 == "n8n:e1" and k2 == "n8n:d9", f"{k1}/{k2}")
    check("键: 无稳定 ID 时用 payload 摘要且键顺序无关", k3 == k4 and k3.startswith("n8n:sha:"), k3)
    check("键: 内容变化产生不同键 (不会误合并不同回调)",
          k3 != eg.client_key({"action": "alert", "message": "y"}))
    g = await eg.register({"action": "alert"})
    check("guard: 缺 execution_id 仍可登记 (摘要键)", g["registered"] is True, str(g))


async def case_degrade(rt, eg):
    orig_flag = config.TR_N8N_ENABLED
    try:
        config.TR_N8N_ENABLED = False
        g = await eg.register(body("alert", ex=f"{RUN}-u6"))
        check("降级: 开关关闭 → 不登记且照常放行处理",
              g["registered"] is False and g["duplicate"] is False and not g["task_id"])
        await eg.finish(g, "succeeded", {})
        check("降级: 未登记时 finish 静默不抛", True)
    finally:
        config.TR_N8N_ENABLED = orig_flag

    import core.task_runtime as tr_pkg
    orig_get = tr_pkg.get_task_runtime

    def raiser():
        raise OSError("redis down")
    try:
        tr_pkg.get_task_runtime = raiser
        g = await eg.register(body("alert", ex=f"{RUN}-u7"))
        check("降级: 账本不可用 → 不登记、不判重复、不抛异常",
              g["registered"] is False and g["duplicate"] is False, str(g))
        await eg.finish(g, "failed", {})
        check("降级: 账本异常后 finish 仍安全 (处理不被二次打断)", True)
    finally:
        tr_pkg.get_task_runtime = orig_get


# ═══ /api/n8n/webhook 端点 (薄接入) ═══

class FakeReq:
    def __init__(self, payload, key):
        self._p = payload
        self.headers = {"X-Webhook-Key": key}

    async def json(self):
        return self._p


async def case_router(rt, eg, fake):
    from fastapi import HTTPException
    import webchat_api  # noqa: F401  (按生产装载顺序先导 app, 避开路由模块拆分的循环导入)
    from api.routers import n8n as n8n_mod
    import core.n8n_integration as ni

    key = "unit-webhook-key-" + RUN
    orig_key = getattr(config, "N8N_WEBHOOK_KEY", "")
    orig_get = ni.get_n8n_integration
    ni.get_n8n_integration = lambda: asyncio.sleep(0, result=fake)
    try:
        config.N8N_WEBHOOK_KEY = key
        b = body("alert", ex=f"{RUN}-r1")
        try:
            await n8n_mod.n8n_webhook(FakeReq(b, "wrong-key"))
            check("端点: Webhook key 不符拒绝 (401)", False)
        except HTTPException as e:
            check("端点: Webhook key 不符拒绝 (401)", e.status_code == 401, str(e.status_code))
        check("端点: 鉴权失败不处理也不登记 (无副作用)", fake.calls == [], str(fake.calls))

        config.N8N_WEBHOOK_KEY = ""
        try:
            await n8n_mod.n8n_webhook(FakeReq(b, ""))
            check("端点: 密钥未配置暂停 (503, fail-closed)", False)
        except HTTPException as e:
            check("端点: 密钥未配置暂停 (503, fail-closed)", e.status_code == 503, str(e.status_code))
        config.N8N_WEBHOOK_KEY = key

        out = await n8n_mod.n8n_webhook(FakeReq(b, key))
        check("端点: 首次回调处理成功并返回任务号",
              out.get("success") is True and out.get("task_id") and fake.calls == [b], str(out)[:160])
        t1 = await rt.get_task(out["task_id"])
        check("端点: 成功处理留 succeeded 终态", t1["status"] == "succeeded", str(t1))

        out2 = await n8n_mod.n8n_webhook(FakeReq(b, key))
        check("端点: 重发同回调判重复 (副作用不重复执行)",
              out2.get("duplicate") is True and len(fake.calls) == 1
              and out2["task_id"] == out["task_id"], str(out2)[:160])

        b3 = body("alert", ex=f"{RUN}-r2")
        out3 = await n8n_mod.n8n_webhook(FakeReq(b3, key))
        check("端点: 不同 execution_id 正常处理 (不被幂等误伤)",
              len(fake.calls) == 2 and out3["task_id"] != out["task_id"], str(out3)[:160])

        fake.boom = True
        b4 = body("alert", ex=f"{RUN}-r3")
        try:
            await n8n_mod.n8n_webhook(FakeReq(b4, key))
            check("端点: 下游异常仍按 500 暴露 (不吞错)", False)
        except HTTPException as e:
            check("端点: 下游异常仍按 500 暴露 (不吞错)", e.status_code == 500, str(e.status_code))
        tid4 = await _tid_for(rt, b4)
        t4 = (await rt.get_task(tid4)) if tid4 else None
        check("端点: 下游异常留 failed 终态 (绝不虚报 succeeded)",
              bool(t4) and t4["status"] == "failed", str(t4))
        fake.boom = False

        out5 = await n8n_mod.n8n_webhook(FakeReq(b4, key))
        check("端点: 失败后重发再处理 (at-least-once 重试不被卡死)",
              len(fake.calls) == 4 and out5.get("duplicate") is not True, str(out5)[:160])

        b6 = body("weird_action", ex=f"{RUN}-r4")
        out6 = await n8n_mod.n8n_webhook(FakeReq(b6, key))
        check("端点: unknown_action 不虚报已处理",
              out6["result"]["processed"] is False)
        tid6 = out6["task_id"]
        check("端点: unknown_action 记 failed (重发不会被误判重复)",
              (await rt.get_task(tid6))["status"] == "failed")
        out7 = await n8n_mod.n8n_webhook(FakeReq(b6, key))
        check("端点: 未处理成功的回调允许再次处理", out7.get("duplicate") is not True)
    finally:
        config.N8N_WEBHOOK_KEY = orig_key
        ni.get_n8n_integration = orig_get


async def _tid_for(rt, b):
    """按幂等键反查首次登记的任务号 (端点异常路径不外露 task_id)"""
    from core.task_runtime.ledger import idem_hash
    h = idem_hash("n8n", "n8n", f"n8n:{b['action']}", eg_client_key(b))
    r = await rt.ledger._redis()
    return await r.hget(f"{config.REDIS_PREFIX}:tr:idem:{h}", "task_id")


def eg_client_key(b):
    from core.task_runtime import entry_guard as _eg
    return _eg.client_key(b)


async def main():
    tmp = tempfile.mkdtemp(prefix="zops_p1_n8n_")
    import core.task_runtime.runtime as rt_mod
    from core.task_runtime import archive as ta
    from core.task_runtime import entry_guard as eg
    ta.TASK_ARCHIVE_DIR = Path(tmp) / "tasks"
    rt_mod._runtime = rt_mod.TaskRuntime(lease_s=60)
    rt = rt_mod._runtime
    _r = await rt.ledger._redis()
    await _r.flushdb()
    fake = FakeN8n()
    try:
        await case_first_register(rt, eg, tmp)
        await case_duplicate(rt, eg)
        await case_failed_then_retry(rt, eg)
        await case_unknown_then_retry(eg)
        await case_inflight(eg)
        await case_key_semantics(eg)
        await case_degrade(rt, eg)
        await case_router(rt, eg, fake)
    finally:
        try:
            await _r.aclose()
        except Exception:
            await _r.close()
        rt_mod._runtime = None
        shutil.rmtree(tmp, ignore_errors=True)
    print("═" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.new_event_loop().run_until_complete(main())
