"""Z-OPS P0 验收补充: 受众隔离 (D) / NMS 服务主体 (E) / n8n 回调签名 (F)

隔离: 测试 Redis 6399 (env 覆盖) + 临时 WikiStore 目录 + TestClient;
无真实外部系统调用、无高风险操作执行。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

TEST_REDIS_URL = os.environ.get("ZOPS_P0_TEST_REDIS", "redis://127.0.0.1:6399/0")
os.environ["REDIS_URL"] = TEST_REDIS_URL
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import config  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


# ═══ P0-D: wiki 受众/归属过滤 ═══

async def test_wiki_visibility(tmp):
    from core.memory.wiki_store import WikiStore
    from core.memory.wiki_query import search_wiki, page_visible
    from core.memory import synthesizer as syn

    store = WikiStore(root=Path(tmp) / "wiki")
    syn.get_wiki_store = lambda: store   # 隔离: 不碰生产 wiki 单例

    # 私聊显式记忆 (audience 空) → private 仅本人可见
    pid_priv = syn.save_explicit_memory("GCOC 管理台我的账号是 op8888", "alice")
    # 群聊显式记忆 → group 仅该群可见
    pid_grp = syn.save_explicit_memory("本群值班电话 12345", "bob",
                                       audience="feishu:group:oc_g1")
    # 公共内部经验页 (无 visibility → public)
    page = store.get_or_create("concepts", "pub-1",
                               defaults_fm={"confidence": 0.9},
                               default_body="# 公共经验\n备份巡检先查 Veeam 作业状态")
    store.save(page)

    pages = {p.page_id: p for p in store.all_pages()}
    check("wiki: 私聊记忆落库 visibility=private",
          str(pages[pid_priv].frontmatter.get("visibility")) == "private")
    check("wiki: 群记忆落库 visibility=group+audience",
          str(pages[pid_grp].frontmatter.get("visibility")) == "group"
          and pages[pid_grp].frontmatter.get("audience") == "feishu:group:oc_g1")

    q_priv, q_grp, q_pub = "op8888 账号", "值班电话", "备份巡检 Veeam"

    hits = search_wiki(q_priv, store=store, actor="alice", audience="")
    check("wiki: 本人可检索自己的私聊记忆", any(h.page_id == pid_priv for h in hits),
          str([h.page_id for h in hits]))
    hits = search_wiki(q_priv, store=store, actor="bob", audience="feishu:group:oc_g1")
    check("wiki: 他人/群检索不到发言者私聊记忆 (核心断言)",
          not any(h.page_id == pid_priv for h in hits))
    hits = search_wiki(q_priv, store=store, actor="", audience="")
    check("wiki: 无主体上下文检索不到私聊记忆", not any(h.page_id == pid_priv for h in hits))
    hits = search_wiki(q_grp, store=store, actor="carol", audience="feishu:group:oc_g1")
    check("wiki: 同群受众可检索群记忆", any(h.page_id == pid_grp for h in hits))
    hits = search_wiki(q_grp, store=store, actor="carol", audience="feishu:group:oc_g2")
    check("wiki: 他群检索不到本群记忆", not any(h.page_id == pid_grp for h in hits))
    hits = search_wiki(q_grp, store=store, actor="bob", audience="")
    check("wiki: 私聊语境检索不到群记忆", not any(h.page_id == pid_grp for h in hits))
    ok_pub = all(
        any(h.page_id == "pub-1" for h in search_wiki(q_pub, store=store, actor=a, audience=aud))
        for a, aud in (("alice", ""), ("bob", "feishu:group:oc_g1"), ("", ""))
    )
    check("wiki: 公共经验页全场景可见 (兼容存量)", ok_pub)
    p_bad = store.get_or_create("concepts", "bad-1",
                                defaults_fm={"visibility": "weird"},
                                default_body="未知可见性页面")
    store.save(p_bad)
    check("wiki: 未知 visibility 拒绝 (fail-closed)",
          not page_visible(p_bad, actor="alice", audience=""))


# ═══ P0-D: 飞书受众标记传播 ═══

def test_feishu_audience_plumb():
    from feishu import conversation as conv
    src = open(conv.__file__, encoding="utf-8").read()
    check("feishu: _handle_feishu_message 接受 chat_type", 'chat_type: str = ""' in src)
    check("feishu: 群受众 = feishu:group:{chat_id}", 'f"feishu:group:{chat_id}"' in src)
    check("feishu: chat_type 缺失按群处理 (fail-closed)", '!= "p2p"' in src)
    check("feishu: provenance.audience 随消息传播", '"audience": audience' in src)
    from agents.base import AgentMessage
    from core.request_context import context_from_agent_message
    m = AgentMessage(source="feishu", target="orchestrator", text="hi",
                     channel_source="feishu", username="alice")
    m.provenance = {**(m.provenance or {}), "audience": "feishu:group:oc_g1"}
    ctx = context_from_agent_message(m)
    check("feishu: RequestContext.audience 接线", ctx.audience == "feishu:group:oc_g1")


# ═══ P0-E: NMS 服务主体 ═══

async def test_nms_service_principal(r):
    from agents.nms_agent import NmsAgent
    prefix = f"{config.REDIS_PREFIX}:nms_session"
    await r.flushdb()
    # 场景 1: 只有服务账号自己的会话 → 使用它
    await r.set(f"{prefix}:{config.NMS_USERNAME}", "[]", ex=600)
    pick = await NmsAgent._nms_pick_global_session(r)
    check("NMS: 服务账号会话可用 → 选服务主体", pick == config.NMS_USERNAME, str(pick))
    # 场景 2: 服务账号无会话 + 其他个人用户有会话 → 不再借用 (P0 移除跨用户回退)
    await r.flushdb()
    await r.set(f"{prefix}:op9999", "[]", ex=600)
    await r.set(f"{prefix}:op7777", "[]", ex=300)
    pick2 = await NmsAgent._nms_pick_global_session(r)
    check("NMS: 个人会话不再被周期任务借用 (跨用户回退已移除)", pick2 is None, str(pick2))
    # 场景 3: 全无 → None → 走 zops.env 显式服务凭据或暂停
    await r.flushdb()
    pick3 = await NmsAgent._nms_pick_global_session(r)
    check("NMS: 无服务会话返回 None (回退显式配置)", pick3 is None)
    # 源码断言: 暂停语义存在 (无会话且无 NMS_PASSWORD → 暂停+原因, 不借用)
    src = open("agents/nms_agent.py", encoding="utf-8").read()
    check("NMS: 监控循环含暂停+原因分支 (未授权暂停)",
          "NMS 周期采集已暂停" in src and "不再借用个人 SSO" in src)
    check("NMS: 跨用户会话扫描已删除", "scan_iter" not in src.split("_nms_pick_global_session")[1].split("async def")[0])


# ═══ P0-F: n8n 回调签名 ═══

async def test_n8n_callback_auth(r):
    import webchat_api
    from fastapi.testclient import TestClient
    from core.n8n_callback_auth import make_signature, verify_n8n_callback, claim_callback_event

    await r.flushdb()
    secret = "test-secret-c17"
    config.N8N_CALLBACK_SECRET = secret
    try:
        client = TestClient(webchat_api.app)
        with client:
            path = "/api/webhook/n8n_callback"

            def _post(body: dict, headers: dict = None):
                return client.post(path, json=body, headers=headers or {})

            # 1) 无签名头 → 401
            resp = _post({"callback_type": "workflow_result", "execution_id": "e1"})
            check("n8n: 无签名头拒绝 (401)", resp.status_code == 401, str(resp.status_code))

            # 2) 密钥未配置 → 503 暂停 (不开放匿名回退)
            config.N8N_CALLBACK_SECRET = ""
            resp = _post({"callback_type": "workflow_result", "execution_id": "e1"})
            check("n8n: 密钥未配置 → 端点暂停 (503)", resp.status_code == 503, str(resp.status_code))
            config.N8N_CALLBACK_SECRET = secret

            # 3) 伪造审批回调 → 404 绑定校验 (审批单不存在)
            body = {"callback_type": "approval_result", "approval_id": "apr_fake",
                    "approved": True, "approver": "attacker", "task_id": "t1"}
            raw = json.dumps(body)
            hdr = make_signature(secret, "POST", path, raw)
            resp = _post(body, hdr)
            check("n8n: 伪造审批单被绑定校验拒绝 (404)", resp.status_code == 404, str(resp.status_code))

            # 4) 真实审批单 + 正确签名 → 处理成功
            from core.approval import create_approval
            rec = create_approval("vm_power_off", {"n": 1}, agent="vm_agent", username="alice")
            aid = rec["approval_id"]
            body = {"callback_type": "approval_result", "approval_id": aid,
                    "approved": True, "approver": "op1", "task_id": "t1"}
            hdr = make_signature(secret, "POST", path, json.dumps(body))
            resp = _post(body, hdr)
            check("n8n: 有效签名审批回调通过 (200)",
                  resp.status_code == 200 and resp.json().get("processed") is True,
                  resp.text[:120])

            # 5) 同事件重复回调 → 幂等忽略
            hdr2 = make_signature(secret, "POST", path, json.dumps(body))
            resp = _post(body, hdr2)
            check("n8n: 重复回调幂等忽略 (duplicate)",
                  resp.status_code == 200 and resp.json().get("reason") == "duplicate",
                  resp.text[:120])

            # 6) workflow_result 缺 execution_id → 400
            body = {"callback_type": "workflow_result", "workflow_id": "w1"}
            hdr = make_signature(secret, "POST", path, json.dumps(body))
            resp = _post(body, hdr)
            check("n8n: workflow_result 缺 execution_id 拒绝 (400)", resp.status_code == 400)

            # 7) 签名被篡改 (body 改动) → 401
            body = {"callback_type": "workflow_result", "execution_id": "e9", "status": "completed"}
            hdr = make_signature(secret, "POST", path, json.dumps({"tampered": True}))
            resp = _post(body, hdr)
            check("n8n: 签名与 body 不符拒绝 (401)", resp.status_code == 401, str(resp.status_code))

            # 8) 时间戳超窗 → 401
            body = {"callback_type": "workflow_result", "execution_id": "e10"}
            hdr = make_signature(secret, "POST", path, json.dumps(body),
                                 ts=int(time.time()) - 3600)
            resp = _post(body, hdr)
            check("n8n: 时间戳超窗拒绝 (401)", resp.status_code == 401)

            # 9) nonce 重放 → 401
            body = {"callback_type": "workflow_result", "execution_id": "e11"}
            raw = json.dumps(body)
            hdr = make_signature(secret, "POST", path, raw, nonce="fixed-nonce-1")
            resp1 = _post(body, hdr)
            hdr_same = make_signature(secret, "POST", path, raw, nonce="fixed-nonce-1")
            # 同 nonce 同 ts 重放: 需要同 ts, make_signature(ts=同值)
            hdr_same = make_signature(secret, "POST", path, raw,
                                      ts=int(hdr["X-Zops-Timestamp"]), nonce="fixed-nonce-1")
            resp2 = _post(body, hdr_same)
            check("n8n: nonce 重放拒绝 (第二次 401)",
                  resp1.status_code == 200 and resp2.status_code == 401,
                  f"{resp1.status_code}/{resp2.status_code}")

            # 10) approval_result 成功回调不再携带自报 username (服务主体语义)
            src = open("api/routers/n8n.py", encoding="utf-8").read()
            check("n8n: 回调主体不再信任自报 approver 为 username",
                  "username=approver" not in src)
    finally:
        config.N8N_CALLBACK_SECRET = ""


async def main():
    import redis.asyncio as aioredis
    r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    await r.flushdb()
    tmp = tempfile.mkdtemp(prefix="zops_p0_def_")
    try:
        await test_wiki_visibility(tmp)
        test_feishu_audience_plumb()
        await test_nms_service_principal(r)
        await test_n8n_callback_auth(r)
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
