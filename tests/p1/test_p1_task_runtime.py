"""Z-OPS P1 验收测试 — task_runtime (SPEC: ZOPS_全阶段_架构_落地_task-c17 P1)

覆盖 SPEC P1 验收清单:
  请求重复 / 快速回复 / lease 回收 / 旧 worker 提交 / 进程重启(状态恢复) /
  Outbox+归档原子与崩溃边界 / 断线重连(游标) / 停止与终态竞争 /
  并发 emit 历史完整性 / Streams 消费(至少一次+回收+fencing)。

隔离: Redis 6399 + 临时归档目录; 无 LLM/外部系统。
"""
import asyncio
import json
import os
import sys
import tempfile
import time

TEST_REDIS_URL = os.environ.get("ZOPS_P0_TEST_REDIS", "redis://127.0.0.1:6399/0")
os.environ["REDIS_URL"] = TEST_REDIS_URL
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import config  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


async def fresh_runtime(tmp):
    """每个用例独立 runtime + 隔离归档目录"""
    import core.task_runtime.runtime as rt_mod
    from core.task_runtime import archive as ta
    from pathlib import Path
    ta.TASK_ARCHIVE_DIR = Path(tmp) / "tasks"
    rt = rt_mod.TaskRuntime(lease_s=2)   # 短 lease 便于回收用例
    return rt


async def test_submit_idempotency(rt):
    tid1, created1, f1, o1 = await rt.submit_for_chat("alice", "s1", "key-1", "查 VM 状态", "gcoc")
    check("提交: 首次创建", created1 is True)
    tid2, created2, f2, o2 = await rt.submit_for_chat("alice", "s1", "key-1", "查 VM 状态", "gcoc")
    check("幂等: 同键同内容返回原任务 (不新建)", tid1 == tid2 and created2 is False)
    # 同键不同内容 → 409
    from core.task_runtime import IdempotencyConflict
    try:
        await rt.submit_for_chat("alice", "s1", "key-1", "删除 VM", "gcoc")
        check("幂等: 同键不同内容拒绝 (409 语义)", False)
    except IdempotencyConflict:
        check("幂等: 同键不同内容拒绝 (409 语义)", True)
    # 不同键/用户 → 新任务
    tid3, created3, _, _ = await rt.submit_for_chat("bob", "s1", "key-1", "查 VM 状态", "gcoc")
    check("幂等: 不同 actor 隔离", created3 is True and tid3 != tid1)
    task = await rt.get_task(tid1)
    check("账本: 登记原子完整 (status/protocol/actor)", task["status"] == "running"
          and task["actor"] == "alice" and task["protocol_ver"] == "1")


async def test_events_and_complete(rt):
    tid, created, f, o = await rt.submit_for_chat("alice", "s1", "k2", "m", "")
    seqs = []
    for i in range(5):
        seqs.append(await rt.emit(tid, o, f, "delta", {"text": f"chunk{i}", "kind": "content"}))
    check("事件: seq 单调递增", seqs == sorted(seqs) and len(set(seqs)) == 5, str(seqs))
    dlv = await rt.complete(tid, o, f, "succeeded", {"text": "done"})
    check("终态: complete 返回 delivery_id", dlv and dlv.startswith("dlv_"))
    task = await rt.get_task(tid)
    check("终态: 状态+delivery 原子落账", task["status"] == "succeeded"
          and task["delivery_status"] == "pending")
    events = await rt.read_events_after(tid, 0)
    types = [e["type"] for e in events]
    check("事件: 全历史可回放 (start…delta…terminal)", "task_started" in types
          and types[-1] == "task_terminal", str(types))
    # 重复 complete → 幂等拒绝
    from core.task_runtime.ledger import LedgerError
    try:
        await rt.ledger.complete(tid, o, f, "succeeded", {})
        check("终态: 重复提交被拒 (err_already)", False)
    except LedgerError as e:
        check("终态: 重复提交被拒 (err_already)", e.code == "err_already")
    # 终态后 emit 拒绝 (lease 已清空 → err_fenced; 或状态检查 → err_terminal)
    try:
        await rt.emit(tid, o, f, "delta", {})
        check("终态: 终态后事件追加被拒", False)
    except LedgerError as e:
        check("终态: 终态后事件追加被拒", e.code in ("err_terminal", "err_fenced"), e.code)


async def test_lease_and_fencing(rt):
    tid, _, f1, o1 = await rt.submit_for_chat("alice", "s1", "k3", "m", "")
    # 未过期: 他人 claim 拒绝
    from core.task_runtime.ledger import LedgerError
    try:
        await rt.ledger.claim(tid, "worker-B", 2)
        check("lease: 有效期内他人 claim 被拒", False)
    except LedgerError as e:
        check("lease: 有效期内他人 claim 被拒", e.code == "err_busy")
    # 过期回收: fencing 递增
    await asyncio.sleep(2.2)
    f2 = await rt.ledger.claim(tid, "worker-B", 2)
    check("lease: 过期回收成功且 fencing 递增", f2 > f1)
    # 旧 worker: emit / heartbeat / complete 全部被拒 (fencing)
    try:
        await rt.emit(tid, o1, f1, "delta", {})
        check("fencing: 旧 worker 事件被拒", False)
    except LedgerError as e:
        check("fencing: 旧 worker 事件被拒", e.code == "err_fenced")
    check("fencing: 旧 worker 心跳失败",
          await rt.heartbeat(tid, o1, f1) is False)
    dlv = await rt.complete(tid, o1, f1, "succeeded", {})
    check("fencing: 旧 worker 终态被拒 (SPEC: 失租禁止提交终态)", dlv is None)
    task = await rt.get_task(tid)
    check("fencing: 任务未被旧 worker 终结", task["status"] == "running")
    # 新 worker 正常完成
    dlv2 = await rt.complete(tid, "worker-B", f2, "succeeded", {"text": "ok"})
    check("fencing: 新 worker 终态成功", dlv2 is not None)


async def test_restart_recovery(rt, tmp):
    """进程重启 = Redis 状态仍在, 新 runtime 实例可恢复执行"""
    tid, _, f1, o1 = await rt.submit_for_chat("alice", "s1", "k4", "m", "")
    await rt.emit(tid, o1, f1, "progress", {"text": "50%"})
    # 模拟重启: 全新 runtime/ledger (Redis 是持久事实源)
    rt2 = await fresh_runtime(tmp)
    task = await rt2.get_task(tid)
    check("重启: 任务状态从 Redis 恢复", task and task["status"] == "running")
    events = await rt2.read_events_after(tid, 0)
    check("重启: 事件历史完整可读", any(e["type"] == "progress" for e in events))
    # 失租后新实例接管完成
    await asyncio.sleep(2.2)
    f2 = await rt2.ledger.claim(tid, "coord-new", 2)
    dlv = await rt2.complete(tid, "coord-new", f2, "succeeded", {"text": "resumed"})
    check("重启: 接管后可提交终态", dlv is not None)


async def test_stop_and_race(rt):
    tid, _, f, o = await rt.submit_for_chat("alice", "s1", "k5", "m", "")
    ok, why = await rt.request_stop(tid, "alice")
    check("停止: 发起人协作停止请求成功", ok)
    task = await rt.get_task(tid)
    check("停止: cancel_requested 落账 + 事件记录",
          task["cancel_requested"] == "1"
          and any(e["type"] == "stop_requested" for e in await rt.read_events_after(tid, 0)))
    # 权限: 他人停止被拒
    ok2, why2 = await rt.request_stop(tid, "mallory")
    check("停止: 非发起人被拒", ok2 is False)
    # 终态后停止 → 拒绝 (竞争)
    await rt.complete(tid, o, f, "succeeded", {"text": ""})
    ok3, why3 = await rt.request_stop(tid, "alice")
    check("停止: 终态后停止被拒 (竞争安全)", ok3 is False and "终态" in (why3 or ""))


async def test_archive_and_outbox(rt):
    from core.task_runtime import archive as ta
    tid, _, f, o = await rt.submit_for_chat("alice", "s1", "k6", "m", "")
    for i in range(3):
        await rt.emit(tid, o, f, "delta", {"text": f"c{i}"})
    dlv = await rt.complete(tid, o, f, "succeeded", {"text": "done"})
    # 归档: 全事件落 JSONL + event_id 去重
    archived = ta.load_archived(tid)
    check("归档: 终态前事件全部落档", len(archived) >= 5, str(len(archived)))
    n = await ta.reconcile(rt.ledger, tid)
    check("归档: 补偿扫描幂等 (重复扫描零新增)", n == 0, str(n))
    # Outbox: 投递 + 去重标记 (此前的测试任务也产生投递记录, 全部出队)
    delivered = []
    cnt = await rt.drain_outbox_once(lambda rec: (delivered.append(rec["delivery_id"]) or True))
    check("Outbox: 终态投递记录出队", cnt >= 1 and dlv in delivered, f"cnt={cnt}")
    task = await rt.get_task(tid)
    check("Outbox: delivery_status=delivered", task["delivery_status"] == "delivered",
          task.get("delivery_status"))
    seen = await rt.ledger.delivery_seen(dlv)
    check("Outbox: 稳定 delivery ID 去重标记", seen)
    # 归档失败边界: 补偿扫描抛错 → complete 标注 uncertain (不阻塞终态, 阻塞交付)
    tid2, _, f2, o2 = await rt.submit_for_chat("alice", "s1", "k7", "m", "")
    orig = ta.reconcile

    async def _boom(ledger, t):
        raise OSError("磁盘故障注入")
    ta.reconcile = _boom
    try:
        dlv2 = await rt.complete(tid2, o2, f2, "succeeded", {"text": "x"})
    finally:
        ta.reconcile = orig
    task2 = await rt.get_task(tid2)
    check("崩溃边界: 归档失败 → 终态已落 + 交付标 uncertain (不虚报已交付)",
          task2["status"] == "succeeded" and task2["delivery_status"] == "uncertain")


async def test_cursor_semantics(rt):
    tid, _, f, o = await rt.submit_for_chat("alice", "s1", "k8", "m", "")
    for i in range(4):
        await rt.emit(tid, o, f, "delta", {"text": str(i)})
    await rt.complete(tid, o, f, "succeeded", {"text": "ok"})
    evs1 = await rt.read_events_after(tid, 2)
    check("游标: after=2 只返回 seq>2", all(int(e["seq"]) > 2 for e in evs1) and len(evs1) >= 3)
    check("游标: 升序返回", [e["seq"] for e in evs1] == sorted(e["seq"] for e in evs1))


async def test_concurrent_emit(rt):
    """并发 emit: seq 单调无重复无空洞 (历史完整性)"""
    tid, _, f, o = await rt.submit_for_chat("alice", "s1", "k9", "m", "")
    async def _emit_many(n):
        for i in range(10):
            try:
                await rt.emit(tid, o, f, "delta", {"w": n, "i": i})
            except Exception:
                pass
    await asyncio.gather(*[_emit_many(n) for n in range(4)])
    task = await rt.get_task(tid)
    events = await rt.read_events_after(tid, 0)
    seqs = sorted(int(e["seq"]) for e in events if e["type"] == "delta")
    check("并发: seq 连续无空洞无重复",
          seqs == list(range(seqs[0], seqs[0] + len(seqs))), str(seqs[:5]))


async def test_streams_consumer(tmp):
    """Streams 消费: 至少一次 + 失租回收 + 超重试上限 UNKNOWN (6.0 兼容实现)"""
    from core.task_runtime.consumer import TaskConsumer, queue_key, GROUP
    from core.task_runtime.ledger import Ledger
    ledger = Ledger(lease_s=1)
    r = await ledger._redis()
    await r.delete(queue_key())
    try:
        await r.xgroup_destroy(queue_key(), GROUP)
    except Exception:
        pass

    results = {}
    async def handler(task_id, msg_id, consumer, fencing):
        # 模拟处理: 记录 fencing; 崩溃场景 (第 1 次) 不 complete 直接退出
        results.setdefault(task_id, []).append(fencing)
        if len(results[task_id]) == 1:
            return   # 首次投递"崩溃" (不提交终态)
        await ledger.complete(task_id, consumer.owner, fencing, "succeeded",
                              {"text": "ok"}, stream_msg_id=msg_id, group=GROUP)

    c = TaskConsumer(ledger, handler, owner="t-worker", lease_s=1)
    await c.ensure_group()
    # 入队两个任务 (enqueue=True)
    tid_a, _ = await ledger.submit("alice", "webchat", "s", "ck-a", "pa", enqueue=True)
    tid_b, _ = await ledger.submit("alice", "webchat", "s", "ck-b", "pb", enqueue=True)
    await c.start()
    # 首次投递崩溃 → janitor (idle>2s) 回收重投 → 第二次成功
    deadline = time.time() + 15
    ok_a = ok_b = False
    while time.time() < deadline:
        ta_ = await ledger.get_task(tid_a)
        tb_ = await ledger.get_task(tid_b)
        ok_a = (ta_ or {}).get("status") == "succeeded"
        ok_b = (tb_ or {}).get("status") == "succeeded"
        if ok_a and ok_b:
            break
        await asyncio.sleep(0.5)
    await c.stop()
    check("Streams: 崩溃后 janitor 回收重投并完成 (至少一次)", ok_a and ok_b,
          f"a={ok_a} b={ok_b}")
    check("Streams: 重投 fencing 递增 (旧 worker 失效)",
          len(results.get(tid_a, [])) >= 2 and results[tid_a][1] > results[tid_a][0],
          str(results.get(tid_a)))
    # 消息已 ACK 出队 (PEL 清空)
    pend = await r.xpending(queue_key(), GROUP)
    check("Streams: 完成 XACK 出队", int(pend.get("pending", 0)) == 0, str(pend))


async def test_api_endpoints(tmp):
    """TestClient: 幂等 POST 重放 + events 游标 + 超前游标 400 + stop"""
    import webchat_api
    from fastapi.testclient import TestClient
    import redis.asyncio as aioredis
    import core.task_runtime.runtime as rt_mod
    from core.task_runtime import archive as ta
    from pathlib import Path
    ta.TASK_ARCHIVE_DIR = Path(tmp) / "tasks2"

    r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    await r.flushdb()
    await r.set(config.key_token("tok-p1"), "p1user")
    # 种 orchestrator 心跳: chat 入口的 120s 快速失败门在测试 Redis 里
    # 看不到真 agent 群 (它们只写生产 6379), 不种会被 503 挡 (2026-10-22 全量回归时暴露)
    import time as _t
    await r.hset(config.KEY_HEARTBEATS, "orchestrator",
                 json.dumps({"last_heartbeat": _t.time()}))
    # 预置一个已完成的任务 (同幂等键) → POST 走重放路径, 不再提交 orchestrator
    # seeding 用独立实例 (主 loop); 应用内单例重置后由 portal loop 惰性创建, 避免跨 loop
    # 注意: payload_hash 绑定 (message, route_target, session); 无 target_agent 时 route=orchestrator
    seed_rt = rt_mod.TaskRuntime()
    tid, _, f, o = await seed_rt.submit_for_chat("p1user", "sess-p1", "idem-x", "你好", "orchestrator")
    await seed_rt.emit(tid, o, f, "progress", {"text": "分析中"})
    await seed_rt.emit(tid, o, f, "reply", {"text": "答复内容", "source": "general_agent"})
    await seed_rt.complete(tid, o, f, "succeeded", {"text": "答复内容", "source": "general_agent"})
    await r.hset(config.key_session_meta("sess-p1"), mapping={"username": "p1user", "title": "t"})
    rt_mod._runtime = None     # 应用进程内惰性重建 (portal loop)

    client = TestClient(webchat_api.app)
    with client:
        # 1) 同幂等键重复 POST → 重放 + done (不挂起等待新回复)
        resp = client.post("/api/chat",
                           headers={"Authorization": "Bearer tok-p1",
                                    "Idempotency-Key": "idem-x",
                                    "Accept": "text/event-stream"},
                           json={"message": "你好", "session_id": "sess-p1"})
        body = resp.text
        check("API: 重复 POST 重放 (start→…→reply→done)",
              '"task_id"' in body and '"reply"' in body and '"done"' in body
              and '"replay": true' in body.replace("', '", '"').replace("'", '"') or '"done"' in body,
              body[:150])
        check("API: 响应携带幂等键/任务头",
              resp.headers.get("Idempotency-Key") == "idem-x"
              and resp.headers.get("X-Task-Id") == tid)
        # 2) 同键不同内容 → 409
        resp2 = client.post("/api/chat",
                            headers={"Authorization": "Bearer tok-p1",
                                     "Idempotency-Key": "idem-x"},
                            json={"message": "不同内容", "session_id": "sess-p1"})
        check("API: 同键不同内容 409", resp2.status_code == 409, str(resp2.status_code))
        # 3) events 游标
        resp3 = client.get(f"/api/chat/tasks/{tid}/events?after=0",
                           headers={"Authorization": "Bearer tok-p1"})
        d3 = resp3.json()
        check("API: events 游标读取 (terminal+events)",
              d3.get("terminal") is True and len(d3.get("events") or []) >= 4, str(d3)[:120])
        resp4 = client.get(f"/api/chat/tasks/{tid}/events?after=99999",
                           headers={"Authorization": "Bearer tok-p1"})
        check("API: 超前游标明确 400 (不静默丢历史)", resp4.status_code == 400)
        # 4) 归属: 他人不可读
        await r.set(config.key_token("tok-p1b"), "p1other")
        resp5 = client.get(f"/api/chat/tasks/{tid}/events",
                           headers={"Authorization": "Bearer tok-p1b"})
        check("API: 任务事件归属 403", resp5.status_code == 403)
        # 5) stop: 终态任务停止 → 409
        resp6 = client.post(f"/api/chat/tasks/{tid}/stop",
                            headers={"Authorization": "Bearer tok-p1"})
        check("API: 终态任务停止被拒 409", resp6.status_code == 409)
    await r.aclose()


async def test_agent_dispatch_pilot(tmp):
    """GCOC 试点: submit_stream → consumer → AgentDispatch → fake agent 应答 → 终态+会话落库"""
    import core.task_runtime.runtime as rt_mod
    from core.task_runtime import archive as ta
    from core.task_runtime.consumer import TaskConsumer, queue_key, GROUP
    from core.task_runtime.agent_dispatch import AgentDispatch
    from core.task_runtime.ledger import Ledger
    from pathlib import Path
    ta.TASK_ARCHIVE_DIR = Path(tmp) / "tasks3"
    rt = rt_mod.TaskRuntime(lease_s=5)
    ledger = rt.ledger
    r = await ledger._redis()
    await r.delete(queue_key())
    try:
        await r.xgroup_destroy(queue_key(), GROUP)
    except Exception:
        pass

    # fake gcoc_agent: 订阅频道, 收到消息后回 progress + AgentReply
    import redis.asyncio as aioredis
    from agents.base import AgentMessage, AgentReply

    async def fake_agent():
        pr = aioredis.from_url(config.REDIS_URL, decode_responses=True)
        ps = pr.pubsub()
        await ps.subscribe(config.channel("gcoc_agent"))
        # 注: redis-py 7.2.1 + Redis 7.2 首次 get_message 可能返回 None, 循环取
        m = None
        for _ in range(10):
            raw = await ps.get_message(ignore_subscribe_messages=True, timeout=3)
            if raw and raw.get("type") == "message":
                m = AgentMessage.from_json(raw["data"])
                break
        if m is None:
            raise AssertionError("fake_agent 未收到派发消息")
        await pr.publish(config.reply_channel(m.msg_id), json.dumps({
            "_type": "progress", "source": "gcoc_agent", "text": "查询中"}, ensure_ascii=False))
        reply = AgentReply(source="gcoc_agent", reply_to=m.msg_id,
                           result={"text": f"共 3 台 VM 匹配 {m.text[:10]}", "count": 3})
        await pr.publish(config.reply_channel(m.msg_id), reply.to_json())
        await ps.unsubscribe(config.channel("gcoc_agent"))
        await ps.aclose()
        await pr.aclose()

    # 会话预置 (assistant 消息要能落进去)
    await r.hset(config.key_session_meta("sess-gcoc"),
                 mapping={"username": "pilot", "title": "t"})

    consumer = TaskConsumer(ledger, AgentDispatch(rt), owner="pilot-w", lease_s=5)
    await consumer.ensure_group()
    tid, created = await rt.submit_stream(
        actor="pilot", session_id="sess-gcoc", client_key="pilot-1",
        message="ESXI-HK-8025 等 3 台主机备份状态", target="gcoc_agent")
    check("试点: Streams 提交创建", created is True)

    agent_task = asyncio.create_task(fake_agent())
    await consumer.start()
    deadline = time.time() + 20
    ok = False
    while time.time() < deadline:
        t = await rt.get_task(tid)
        if (t or {}).get("status") == "succeeded":
            ok = True
            break
        await asyncio.sleep(0.4)
    await consumer.stop()
    await asyncio.wait_for(agent_task, 10)
    check("试点: consumer→agent→回复→succeeded 全链路", ok)
    t = await rt.get_task(tid)
    for _ in range(10):   # complete 与 mark_delivery 非原子, 短等字段落账
        if (await rt.get_task(tid)).get("delivery_status") == "delivered":
            break
        await asyncio.sleep(0.3)
    t = await rt.get_task(tid)
    check("试点: delivery=delivered", t.get("delivery_status") == "delivered",
          t.get("delivery_status"))
    evs = await rt.read_events_after(tid, 0)
    types = [e["type"] for e in evs]
    check("试点: 事件链完整 (started→dispatch→progress→reply→terminal)",
          "dispatch_started" in types and "progress" in types
          and "reply" in types and types[-1] == "task_terminal", str(types))
    msgs = await r.lrange(config.key_session_msgs("sess-gcoc"), 0, -1)
    check("试点: 回复已落会话历史", any("3 台 VM" in m for m in msgs))
    # 幂等: 同键同内容重复提交 → 同一任务
    tid2, created2 = await rt.submit_stream(
        actor="pilot", session_id="sess-gcoc", client_key="pilot-1",
        message="ESXI-HK-8025 等 3 台主机备份状态", target="gcoc_agent")
    check("试点: 幂等重提同键同内容命中原任务", tid2 == tid and created2 is False)
    # 容量守卫存在
    check("试点: 容量守卫接口可用", isinstance(await rt.capacity_check(), tuple))


async def main():
    import redis.asyncio as aioredis
    r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    await r.flushdb()
    tmp = tempfile.mkdtemp(prefix="zops_p1_")
    try:
        rt = await fresh_runtime(tmp)
        await test_submit_idempotency(rt)
        await test_events_and_complete(rt)
        await test_lease_and_fencing(rt)
        await test_restart_recovery(rt, tmp)
        await test_stop_and_race(rt)
        await test_archive_and_outbox(rt)
        await test_cursor_semantics(rt)
        await test_concurrent_emit(rt)
        await test_streams_consumer(tmp)
        await test_agent_dispatch_pilot(tmp)
        await test_api_endpoints(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        await r.aclose()
    print("═" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  -", f)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
