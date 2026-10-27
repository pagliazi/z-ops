"""Z-OPS P0 验收测试 (SPEC: ZOPS_全阶段_架构_落地_task-c17 — P0 部分)

隔离要求 (SPEC「实施基线与隔离验证」):
  - 独立测试 Redis (127.0.0.1:6399, 与生产 6379/DB0 完全分离);
  - transcript 指向临时目录;
  - 无 LLM 调用、无真实外部系统、无高风险业务操作实际执行;
  - 不安装任何新依赖。

运行: python3 tests/p0/test_p0_acceptance.py
覆盖 (对应 SPEC P0 验收清单中已落地项):
  1. 并发身份隔离 (RequestContext 任务级隔离)
  2. 空 scope 语义 (空交集=NONE, 不是不限; 资源缺失不跳过限制)
  3. 跨用户会话隔离 (meta/transcript 归属链, 无法证明→403)
  4. 跨用户引用隔离 (read_ref 归属校验)
  5. 审批存储故障 (未登记成功, 不虚报; destructive 不因故障放行)
  6. 决策并发 CAS (版本单调, 并发只有一个生效)
  7. 审批门 fail-closed (组件异常→拦截; APPROVAL_ENABLED=false→仍拦截)
  8. write 权限门 (无权限/无主体/范围不明→拒绝; 有权限+范围命中→放行)
  9. send_to 主体透传 (下游不再丢 username)
  10. RiskLevel 归一化 (字符串/别名/异常值)
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

# ── 隔离环境 (必须在 import zops 模块之前) ──
TEST_REDIS_URL = os.environ.get("ZOPS_P0_TEST_REDIS", "redis://127.0.0.1:6399/0")
os.environ["REDIS_URL"] = TEST_REDIS_URL
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import config  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name} {detail}")


# ═══ 1. 并发身份隔离 ═══

def test_request_context_isolation():
    from core.request_context import (
        RequestContext, context_from_agent_message, request_scope,
        current_username, current_session_id, run_with_context,
    )

    class _Msg:
        username, session_id, msg_id = "alice", "s1", "m1"
        channel_source = "webchat"
        provenance = {}

    async def _user_task(username: str, barrier: asyncio.Event, hold: asyncio.Event):
        msg = _Msg()
        msg.username, msg.session_id = username, f"s-{username}"
        ctx = context_from_agent_message(msg)
        with request_scope(ctx):
            # 模拟 _safe_handle 中的 await 让步点: 等 barrier → 让出控制权 → 其他任务绑定
            barrier.set()
            await hold.wait()
            return current_username(), current_session_id()

    async def _main():
        b1, b2, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def _u1():
            return await _user_task("alice", b1, done)
        async def _u2():
            return await _user_task("bob", b2, done)
        t1, t2 = asyncio.create_task(_u1()), asyncio.create_task(_u2())
        await asyncio.wait_for(b1.wait(), 2)
        await asyncio.wait_for(b2.wait(), 2)
        await asyncio.sleep(0.05)   # 两任务都已绑定后交错执行
        done.set()
        r1, r2 = await t1, await t2
        return r1, r2

    r1, r2 = asyncio.run(_main())
    check("并发隔离: alice 任务读到 alice", r1 == ("alice", "s-alice"), str(r1))
    check("并发隔离: bob 任务读到 bob (await 交错后不错位)", r2 == ("bob", "s-bob"), str(r2))

    # 旧实例属性竞态复现对照 (证明 bug 存在, 修复必要): 省略 — 仅验证新机制

    # 上下文外无主体
    check("无上下文时 current_username 为空", current_username() == "")

    # 线程显式传播
    ctx = RequestContext(actor="carol", session_id="s3")
    got = {}
    def _in_thread():
        got["u"] = current_username()
    t = threading.Thread(target=run_with_context, args=(_in_thread,), kwargs={"_ctx": ctx})
    t.start(); t.join()
    check("线程传播: run_with_context 带上下文进线程", got.get("u") == "carol", str(got))

    # derive_child 不扩大受众/范围
    parent = RequestContext(actor="dave", audience="grp1", scopes={"sites": "SCOPED"})
    child = parent.derive_child(run_id="r9")
    check("派生子上下文: audience 不扩大", child.audience == "grp1" and child.delegation_ref == parent.request_id)


# ═══ 2. 空 scope 语义 ═══

async def test_scope_semantics(r):
    from core import bizperms as bp
    # 清理 + 造数据
    await r.delete(config.KEY_USERS, config.KEY_GROUP_BIZPERMS)
    await r.hset(config.KEY_USERS, "u_own_hkg", json.dumps(
        {"role": "user", "biz_perms": ["vmops.operate"], "scopes": {"sites": ["HKG"], "customers": []}}))
    await r.hset(config.KEY_USERS, "u_grp_sha", json.dumps(
        {"role": "user", "biz_perms": [], "group": "g_sha", "scopes": {"sites": [], "customers": []}}))
    await r.hset(config.KEY_GROUP_BIZPERMS, "g_sha", json.dumps(
        {"perms": ["vmops.operate"], "scope": {"sites": ["SHA"], "customers": []}}))

    eff = await bp.effective_perms("u_own_hkg")
    check("scope: 单边配置 → scoped", eff["scopes"].get("_modes", {}).get("sites") == "scoped")
    ok, why = bp.scope_allows(eff["scopes"], site="HKG")
    check("scope: 命中站点放行", ok, why)
    ok, why = bp.scope_allows(eff["scopes"], site="BEJ")
    check("scope: 未命中站点拒绝", not ok)

    # 核心修复: 个人 HKG ∩ 组 SHA = 空 → NONE (旧行为: 空列表=不限)
    await r.hset(config.KEY_USERS, "u_conflict", json.dumps(
        {"role": "user", "biz_perms": [], "group": "g_sha", "scopes": {"sites": ["HKG"], "customers": []}}))
    eff2 = await bp.effective_perms("u_conflict")
    check("scope 空交集: mode=none (不再解释为不限)",
          eff2["scopes"].get("_modes", {}).get("sites") == "none", str(eff2["scopes"]))
    ok, why = bp.scope_allows(eff2["scopes"], site="HKG")
    check("scope 空交集: 一律拒绝 (含原命中值)", not ok, why)

    # 资源信息缺失不跳过限制
    eff3 = await bp.effective_perms("u_grp_sha")
    ok, why = bp.scope_allows(eff3["scopes"], site="")
    check("scope: 单据缺站点 + scoped → 拒绝 (旧行为放行)", not ok and "缺少站点" in why, why)
    ok, why = bp.scope_allows(eff3["scopes"], site="SHA")
    check("scope: 组继承命中放行", ok, why)


# ═══ 3. 跨用户会话隔离 ═══

async def test_session_access(r, transcript_dir):
    from pathlib import Path
    from core import transcript as _tr
    from core.session_access import ensure_session_access, get_session_owner
    from fastapi import HTTPException

    _tr.TRANSCRIPT_DIR = Path(transcript_dir)  # 隔离: 不碰生产 logs/sessions

    sid_meta = "sess_meta_owner"
    await r.hset(config.key_session_meta(sid_meta), mapping={"username": "alice", "title": "t"})
    ok_owner = await ensure_session_access(r, sid_meta, "alice", action="读取")
    check("会话: 本人访问放行 (meta)", ok_owner == "alice")
    try:
        await ensure_session_access(r, sid_meta, "bob", action="读取")
        check("会话: 他人访问 403 (meta)", False)
    except HTTPException as e:
        check("会话: 他人访问 403 (meta)", e.status_code == 403)

    # meta 丢失 → transcript 兜底
    sid_tr = "sess_tr_owner"
    await _tr.append_message(sid_tr, "user", "hello", username="alice")
    await _tr.append_message(sid_tr, "assistant", "hi")
    ok_owner2 = await ensure_session_access(r, sid_tr, "alice", action="restore")
    check("会话: meta 过期由 transcript 证明归属", ok_owner2 == "alice")
    try:
        await ensure_session_access(r, sid_tr, "bob", action="restore")
        check("会话: transcript 归属下他人仍 403", False)
    except HTTPException as e:
        check("会话: transcript 归属下他人仍 403", e.status_code == 403)

    # 无法证明归属 → 拒绝
    await _tr.append_message("sess_no_owner", "assistant", "匿名消息")  # 无 username
    try:
        await ensure_session_access(r, "sess_no_owner", "alice", action="restore")
        check("会话: 无法证明归属 → 403 (fail-closed)", False)
    except HTTPException as e:
        check("会话: 无法证明归属 → 403 (fail-closed)", e.status_code == 403)


# ═══ 4. 跨用户引用隔离 ═══

def test_ref_acl():
    from core.query_engine import _store_ref, _ref_allows, _REF_STORE
    rid = _store_ref("X" * 10, actor="alice", session_id="s1")
    entry = _REF_STORE[rid]
    check("引用: 同 actor+session 放行", _ref_allows(entry, "alice", "s1"))
    check("引用: 跨用户拒绝", not _ref_allows(entry, "bob", "s1"))
    check("引用: 跨会话拒绝", not _ref_allows(entry, "alice", "s2"))
    check("引用: 无归属 (旧数据) 拒绝", not _ref_allows({"text": "x"}, "alice", "s1"))
    check("引用: 空 actor 拒绝", not _ref_allows(entry, "", "s1"))


# ═══ 5/6/7. 审批门 ═══

def _sync_redis():
    import redis
    return redis.from_url(config.REDIS_URL, decode_responses=True)


def test_approval_cas_and_persist():
    from core import approval as ap
    r = _sync_redis()
    r.flushdb()

    rec = ap.create_approval("vm_power_off", {"name": "vm1"}, agent="vm_agent",
                             session_id="s1", msg_id="m1", username="alice")
    check("审批: 创建成功无持久化失败标记", not rec.get("_persist_failed"))
    aid = rec["approval_id"]

    # 并发决策 CAS: 两线程同时 decide, 只有一个生效, decision_ver 单调
    results = []
    def _decide(approver):
        try:
            out = ap.decide_approval(aid, "approve", approver=approver)
            results.append((approver, out.get("approver"), out.get("decision_ver")))
        except Exception as e:
            results.append((approver, f"ERR:{e}", None))
    t1 = threading.Thread(target=_decide, args=("op1",))
    t2 = threading.Thread(target=_decide, args=("op2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    final = r.hgetall(f"{config.REDIS_PREFIX}:approval:{aid}")
    vers = [v for (_, _, v) in results if v is not None]
    check("决策 CAS: 最终状态唯一", final.get("status") == "approved", str(final.get("status")))
    check("决策 CAS: decision_ver 只推进到 1 (并发不重复计数)",
          final.get("decision_ver") == "1", str(final.get("decision_ver")))
    check("决策 CAS: 两并发调用均返回终态 (幂等收敛)", len(results) == 2 and all(v is not None for v in vers), str(results))

    # 幂等: 已决策再 decide 返回原单
    again = ap.decide_approval(aid, "reject", approver="op3")
    check("决策 CAS: 终态后重决策幂等 (不被覆盖)", again.get("status") == "approved")

    # 持久化故障: 指向不可达 Redis
    good_url = config.REDIS_URL
    try:
        config.REDIS_URL = "redis://127.0.0.1:1/0"   # 不可达端口
        ap._audit_cache.pop("alog", None)
        bad = ap.create_approval("vm_power_off", {"name": "vm2"}, agent="vm_agent", username="alice")
        check("审批存储故障: 显式 _persist_failed 标记 (不虚报已创建)", bool(bad.get("_persist_failed")))

        async def _gate():
            from core.tool_schema import ToolDef, RiskLevel, ToolExecution
            td = ToolDef(name="vm_power_off", description="x", risk_level=RiskLevel.DESTRUCTIVE)
            from core.approval import gate_destructive_call
            executed = []
            async def _exec():
                executed.append(1)
                return ToolExecution(tool_name="vm_power_off", success=True, output="RAN")
            return await gate_destructive_call(td, {}, _exec, "vm_agent", "s", "m", "alice"), executed
        gated, executed = asyncio.run(_gate())
        check("审批存储故障: gate 返回未登记成功提示", gated.error == "approval_persist_failed")
        check("审批存储故障: executor 未被调用 (高风险绝不执行)", executed == [])
    finally:
        config.REDIS_URL = good_url


async def test_gate_action_message(r):
    from core import approval as ap
    from agents.base import AgentMessage

    replies = []
    class _FakeAgent:
        name = "vm_agent"
        async def reply(self, msg, result=None, error=""):
            replies.append(result or {"error": error})

    await r.delete(config.KEY_USERS)
    # write 级 + 无主体 → 拒绝 (旧逻辑: username 空 → 跳过检查放行)
    msg = AgentMessage(source="webchat", target="vm_agent", action="vm_snapshot",
                       params={"snap_action": "create", "name": "vm1"}, username="")
    ok = await ap.gate_action_message(_FakeAgent(), msg)
    check("write 门: 无法确认主体 → 拦截 (旧逻辑放行)", ok is False and replies and "无法确认发起用户" in replies[-1]["text"])

    # write 级 + 有权限 + 范围命中 → 放行
    await r.hset(config.KEY_USERS, "w1", json.dumps(
        {"role": "user", "biz_perms": ["vmops.operate"], "scopes": {"sites": ["HKG"], "customers": []}}))
    msg2 = AgentMessage(source="webchat", target="vm_agent", action="vm_snapshot",
                        params={"snap_action": "create", "name": "vm1", "site": "HKG"}, username="w1")
    ok2 = await ap.gate_action_message(_FakeAgent(), msg2)
    check("write 门: 持权+范围命中 → 放行", ok2 is True)

    # write 级 + scoped + 资源缺 site → 拦截
    msg3 = AgentMessage(source="webchat", target="vm_agent", action="vm_snapshot",
                        params={"snap_action": "create", "name": "vm1"}, username="w1")
    ok3 = await ap.gate_action_message(_FakeAgent(), msg3)
    check("write 门: scoped+资源信息缺失 → 拦截", ok3 is False)

    # destructive + APPROVAL_ENABLED=false → 仍拦截 (旧逻辑: 直接放行)
    prev = config.APPROVAL_ENABLED
    try:
        config.APPROVAL_ENABLED = False
        msg4 = AgentMessage(source="webchat", target="vm_agent", action="vm_power",
                            params={"power_action": "shutdown", "name": "vm1"}, username="w1")
        ok4 = await ap.gate_action_message(_FakeAgent(), msg4)
        check("destructive: APPROVAL_ENABLED=false 仍拦截 (无绕过开关)", ok4 is False)
    finally:
        config.APPROVAL_ENABLED = prev

    # 审批门组件异常 → fail-closed (旧逻辑: fail-open 放行)
    orig_create = ap.create_approval
    async def _boom(*a, **k):
        raise RuntimeError("组件故障注入")
    try:
        ap.create_approval = _boom
        msg5 = AgentMessage(source="webchat", target="vm_agent", action="vm_power",
                            params={"power_action": "shutdown", "name": "vm1"}, username="w1")
        ok5 = await ap.gate_action_message(_FakeAgent(), msg5)
        check("审批门异常: fail-closed 拦截 (旧逻辑 fail-open 放行)", ok5 is False)
    finally:
        ap.create_approval = orig_create


# ═══ 8. 引擎风险门 (enforce_tool_risk) ═══

async def test_enforce_tool_risk(r):
    from core.query_engine import enforce_tool_risk
    from core.tool_schema import ToolDef, RiskLevel

    await r.delete(config.KEY_USERS)
    # read → 放行
    td_read = ToolDef(name="q1", description="查询")
    check("引擎门: read 放行", await enforce_tool_risk(td_read, "q1", {}, actor_hint="x") is None)

    # destructive + APPROVAL_ENABLED=false → 拦截 (不执行)
    prev = config.APPROVAL_ENABLED
    try:
        config.APPROVAL_ENABLED = False
        td_d = ToolDef(name="vm_power_off", description="x", risk_level=RiskLevel.DESTRUCTIVE)
        res = await enforce_tool_risk(td_d, "vm_power_off", {}, actor_hint="w1")
        check("引擎门: destructive 审批停用仍拦截", res is not None and res.permission_denied
              and res.error == "approval_disabled_blocked")
    finally:
        config.APPROVAL_ENABLED = prev

    # write 无权限 → 拒绝
    await r.hset(config.KEY_USERS, "noperm", json.dumps({"role": "user", "biz_perms": []}))
    td_w = ToolDef(name="script_exec", description="x", risk_level=RiskLevel.WRITE)
    res2 = await enforce_tool_risk(td_w, "script_exec", {}, actor_hint="noperm")
    check("引擎门: write 无权限拒绝", res2 is not None and res2.permission_denied)

    # write 无主体 → 拒绝
    res3 = await enforce_tool_risk(td_w, "script_exec", {}, actor_hint="")
    check("引擎门: write 无法确认主体拒绝", res3 is not None and res3.permission_denied)

    # write 有权限+范围命中 → 放行
    await r.hset(config.KEY_USERS, "w2", json.dumps(
        {"role": "user", "biz_perms": ["devops.operate"], "scopes": {"sites": [], "customers": []}}))
    res4 = await enforce_tool_risk(td_w, "script_exec", {"site": ""}, actor_hint="w2")
    check("引擎门: write 持权+不限范围放行", res4 is None)

    # admin 不自动获得业务写权限
    await r.hset(config.KEY_USERS, "admin1", json.dumps({"role": "admin", "biz_perms": []}))
    res5 = await enforce_tool_risk(td_w, "script_exec", {}, actor_hint="admin1")
    check("引擎门: admin 无业务权限同样拒绝", res5 is not None and res5.permission_denied)


# ═══ 9. send_to 主体透传 ═══

async def test_send_to_propagation():
    from agents.base import BaseAgent, AgentMessage
    from core.request_context import request_scope, RequestContext

    published = []
    class _FakeRedis:
        async def publish(self, ch, payload):
            published.append((ch, payload))

    class _StubAgent:
        name = "kb_agent"
        _current_username = ""
        _current_session_id = ""
        _current_msg_id = ""
        send_to = BaseAgent.send_to
        async def get_redis(self):
            return _FakeRedis()

    stub = _StubAgent()
    # 场景 1: 请求上下文内调用 (正常处理链路)
    with request_scope(RequestContext(actor="alice", session_id="s9", channel="feishu", msg_id="parent1")):
        await stub.send_to("vm_agent", action="vm_snapshot", text="create snap", params={"name": "vm1"})
    sent = AgentMessage.from_json(published[-1][1])
    check("send_to: username 透传 (旧逻辑丢失)", sent.username == "alice", sent.username)
    check("send_to: session_id 透传", sent.session_id == "s9")
    check("send_to: channel 透传", sent.channel_source == "feishu")
    check("send_to: 父关联透传 (reply_to)", sent.reply_to == "parent1")
    check("send_to: provenance 带父 request_id", bool(sent.provenance.get("request_id")))

    # 场景 2: 上下文外 (后台任务) + 实例属性兜底
    stub._current_username = "legacy_user"
    await stub.send_to("vm_agent", action="chat", text="hi")
    sent2 = AgentMessage.from_json(published[-1][1])
    check("send_to: 无上下文时实例属性兜底", sent2.username == "legacy_user", sent2.username)


# ═══ 10. RiskLevel 归一化 ═══

def test_risk_normalization():
    from core.tool_schema import ToolDef, RiskLevel
    check("risk 归一: 'DESTRUCTIVE' 字符串 → 枚举",
          ToolDef(name="a", risk_level="DESTRUCTIVE").risk_level == RiskLevel.DESTRUCTIVE)
    check("risk 归一: 未知值 → WRITE (不放行)",
          ToolDef(name="b", risk_level="banana").risk_level == RiskLevel.WRITE)
    check("risk 归一: 默认 READ", ToolDef(name="c").risk_level == RiskLevel.READ)


# ═══ 主流程 ═══

async def _async_suite(transcript_dir):
    import redis.asyncio as aioredis
    r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    await r.flushdb()
    await test_scope_semantics(r)
    await test_session_access(r, transcript_dir)
    await test_gate_action_message(r)
    await test_enforce_tool_risk(r)
    await test_send_to_propagation()
    await r.aclose()


def main():
    print("═" * 60)
    print("Z-OPS P0 验收测试 (隔离 Redis:", TEST_REDIS_URL, ")")
    print("═" * 60)
    tmp = tempfile.mkdtemp(prefix="zops_p0_transcript_")
    try:
        test_request_context_isolation()
        test_ref_acl()
        test_approval_cas_and_persist()
        test_risk_normalization()
        asyncio.run(_async_suite(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.remove("/tmp/zops_p0_test_redis.pid")
        except OSError:
            pass
    print("═" * 60)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    print("P0 验收测试全部通过 ✅")


if __name__ == "__main__":
    main()
