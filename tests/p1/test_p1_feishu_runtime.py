"""Z-OPS P1 验收测试 — 飞书入口接入 task_runtime (SPEC: 可靠任务全入口覆盖 + 永久归档)

覆盖:
  1. 门面 channel 透传 (submit_for_chat channel="feishu" → 账本/Outbox 归属, 默认 webchat 兼容);
  2. 飞书 happy path: message_id 幂等登记 → dispatched/progress/reply 事件 →
     succeeded 终态 → 发送成功即 delivered → JSONL 永久归档 (闭合飞书仅 Redis 截断缺口);
  3. 超时路径: complete failed, 不虚报交付;
  4. 登记失败降级: 不阻断对话回复 (可用性优先);
  5. 受众标记: 群聊 audience 随任务 meta 留痕 (P0 语义不回退)。

隔离: Redis 6399 + 临时归档目录 + fake bot/模块注入; 无 LLM/外部系统/真飞书。
注: 重复回调 (同 message_id) 由网关层 message_id 去重拦截; 账本幂等键同样约束,
    双保险不重复建任务 (见 ledger SUBMIT_LUA)。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

TEST_REDIS_URL = os.environ.get("ZOPS_P0_TEST_REDIS", "redis://127.0.0.1:6399/0")
os.environ["REDIS_URL"] = TEST_REDIS_URL
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# 跨运行隔离: 幂等键绑定测试进程唯一令牌 (持久测试 Redis 不留残留任务干扰)
RUN = os.environ.get("ZOPS_TEST_RUN") or str(int(time.time() * 1000))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


class FakeBot:
    def __init__(self):
        self.md_sent, self.im_sent = [], []

    async def _reply_md(self, message_id, text, chat_id, title=""):
        self.md_sent.append(text)
        return True

    async def _reply_im(self, message_id, text, chat_id):
        self.im_sent.append(text)
        return True

    async def _reply_card(self, *a, **k):
        return True

    async def _handle_nms_command(self, text, message_id, chat_id):
        return ""      # ① NMS 命令不命中

    async def _nms_nl_query(self, text, message_id, chat_id):
        return ""      # ①.2 自然语言 NMS 不命中


def install_fakes(tmp, deadline_s=25):
    """最小模块注入: 避免 feishu.protocol→bot 组合根与真实生产 Redis 客户端"""
    import redis.asyncio as aioredis

    fake_protocol = types.ModuleType("feishu.protocol")
    bot = FakeBot()
    fake_protocol.shared_bot = lambda: bot

    root = Path(__file__).resolve().parents[2]
    pkg = types.ModuleType("feishu")
    pkg.__path__ = [str(root / "feishu")]
    sys.modules["feishu"] = pkg
    sys.modules["feishu.protocol"] = fake_protocol

    import feishu.conversation as conv
    import config
    config.TR_FEISHU_ENABLED = True
    # conv.shared_bot 仅在首次 import 时绑定→多用例必须逐个重绑,
    # 否则 handler 向首个用例的 bot 回复, 本用例断言永远落空
    conv.shared_bot = lambda: bot

    # 隔离 Redis: conv 内 aioredis.from_url 强制指向测试实例
    _orig_from_url = aioredis.from_url
    conv.aioredis = types.SimpleNamespace(
        from_url=lambda url, **kw: _orig_from_url(TEST_REDIS_URL, **kw))

    # 身份/会话/引导打桩 (无真实用户表/会话引擎依赖)
    async def _resolve(sender_id):
        return ("alice", "Alice", False)

    async def _noop(*a, **k):
        return False

    conv._resolve_feishu_user = _resolve
    conv._handle_binding = _noop
    conv._save_session_msg = _noop
    conv._BINDING_GUIDE = "bind"
    conv._HELP_TEXT = "help"
    conv._HELP_CARD = None

    # 测试可控 deadline (源码默认 180s, 逐字符替换注入)
    _orig_src = conv._handle_feishu_message.__code__
    # 不改动函数对象; 用源码级替换的重放函数 (见 run_patched)
    del _orig_src
    conv.__test_deadline__ = deadline_s

    # 归档目录隔离 + 清全局单例 (避免污染/被污染)
    from core.task_runtime import archive as ta
    ta.TASK_ARCHIVE_DIR = Path(tmp) / "tasks"
    import core.task_runtime.runtime as rt_mod
    rt_mod._runtime = None
    return conv, bot


def run_patched(conv, text, message_id, chat_id="oc_g1", chat_type="group"):
    """以测试 deadline 重放 _handle_feishu_message (源码级替换 180→N, 不改生产文件)"""
    import inspect
    src = inspect.getsource(conv._handle_feishu_message)
    assert "time.time() + 180" in src, "deadline 锚点漂移"
    src = src.replace("time.time() + 180",
                      f"time.time() + {conv.__test_deadline__}")
    ns = conv.__dict__.copy()
    ns["_handle_feishu_message"] = None
    exec(compile(src, "<feishu-test-deadline>", "exec"), ns)
    fn = ns["_handle_feishu_message"]
    return fn(text, message_id, chat_id, sender_id="ou_x1", chat_type=chat_type)


async def feed_agent(reply_text, progress="", hang=False, probe=None,
                     gate=None, total_deadline=30, after_probe_wait=0.3):
    """模拟 agent: 监听 orchestrator 频道, 捕获 msg_id 后回 progress+最终 AgentReply。
    hang=True 时不回包 (触发超时); probe 回调收到转发消息 dict;
    gate: pubsub 激活确认后回调 (消除发布/订阅竞态)。"""
    import redis.asyncio as aioredis
    import config
    from agents.base import AgentReply
    r = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    ps = r.pubsub()
    await ps.subscribe(config.channel("orchestrator"))
    if gate:
        # 等 subscribe 确认消息后才放行发布方
        while True:
            m = await ps.get_message(timeout=1.0)
            if m and m["type"] == "subscribe":
                gate.set()
                break
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            m = await ps.get_message(timeout=1.0)
            if not m or m["type"] != "message":
                continue
            d = json.loads(m["data"])
            if d.get("target") != "orchestrator":
                continue
            if probe:
                probe(d)
            if hang:
                return False
            # 等 handler 完成 reply 频道订阅 (真实 agent 固有处秒级延迟的测试等价)
            await asyncio.sleep(after_probe_wait)
            if progress:
                await r.publish(config.reply_channel(d["msg_id"]), json.dumps(
                    {"_type": "progress", "text": progress}, ensure_ascii=False))
                await asyncio.sleep(0.3)
            reply = AgentReply(reply_to=d["msg_id"], source="kb_agent",
                               result={"text": reply_text})
            await r.publish(config.reply_channel(d["msg_id"]), reply.to_json())
            return True
        return False
    finally:
        await ps.unsubscribe()
        await r.aclose()


async def settle(seconds=1.0):
    await asyncio.sleep(seconds)


async def test_channel_passthrough():
    """1. 门面 channel 透传"""
    from core.task_runtime import runtime as rt_mod
    rt = rt_mod.TaskRuntime(lease_s=2)
    tid, created, f, o = await rt.submit_for_chat(
        "alice", "feishu:oc_x", f"om_pt1-{RUN}", "查告警", "orchestrator",
        channel="feishu")
    task = await rt.get_task(tid)
    check("门面: channel='feishu' 透传入账本", task.get("channel") == "feishu")
    tid2, created2, f2, o2 = await rt.submit_for_chat(
        "alice", "s2", f"pt-web1-{RUN}", "查VM", "opscore")
    task2 = await rt.get_task(tid2)
    check("门面: 缺省渠道向后兼容 (webchat)", task2.get("channel") == "webchat")
    dlv = await rt.complete(tid, o, f, "succeeded", {"text": "ok"})
    rec = await rt.ledger.pop_delivery()
    check("门面: feishu 终态 Outbox 记录归属",
          bool(dlv) and rec and rec.get("task_id") == tid)
    await rt.ledger.mark_delivery(tid, dlv, "delivered")
    # 重复 submit 同键同内容 → 复用原任务
    tid3, created3, _, _ = await rt.submit_for_chat(
        "alice", "feishu:oc_x", f"om_pt1-{RUN}", "查告警", "orchestrator",
        channel="feishu")
    check("门面: 幂等复用不新建", tid3 == tid and created3 is False)
    await rt.complete(tid2, o2, f2, "succeeded", {})


async def test_feishu_happy_path(tmp):
    """2. 飞书全链路: 登记→事件→succeeded→delivered→归档"""
    conv, bot = install_fakes(tmp)
    from core.task_runtime import get_task_runtime
    from core.task_runtime import archive as ta
    captured = {}

    def probe(d):
        captured["msg_id"] = d.get("msg_id")
        captured["provenance"] = d.get("provenance") or {}

    gate = asyncio.Event()
    f = asyncio.create_task(feed_agent(
        "告警共 3 条: A/B/C", progress="正在查询 NMS…", probe=probe, gate=gate))
    await asyncio.wait_for(gate.wait(), 10)   # 先订阅就绪, 再发消息
    h = asyncio.create_task(run_patched(
        conv, "当前有哪些告警？请详细分析", f"om_happy1-{RUN}"))
    fed = await asyncio.wait_for(f, 30)
    res = await asyncio.wait_for(h, 30)
    check("链路: agent 喂入成功且 handler 正常返回", fed and res is None)
    check("链路: 飞书卡片收到完整回复", bool(bot.md_sent) and "告警共 3 条" in bot.md_sent[0])

    rt = get_task_runtime()
    from core.task_runtime import ledger as led
    r = await rt.ledger._redis()
    # 幂等键 = idem_hash(actor, channel, session, client_key) → Hash.task_id
    tid = await r.hget(led.k_idem(led.idem_hash(
        "alice", "feishu", "feishu:oc_g1", f"om_happy1-{RUN}")), "task_id") or ""
    check("登记: message_id 幂等键建任务", bool(tid))
    task = await rt.get_task(tid) if tid else {}
    check("登记: 账本 channel/actor/session 正确",
          task.get("channel") == "feishu" and task.get("actor") == "alice"
          and task.get("session_id") == "feishu:oc_g1"
          and task.get("status") == "succeeded")
    events = await rt.read_events_after(tid, 0) if tid else []
    types_ = [e.get("type") for e in events]
    check("事件: started/dispatched/progress/reply/terminal 全落",
          "task_started" in types_ and "dispatched" in types_
          and "progress" in types_ and "reply" in types_
          and types_[-1] == "task_terminal", str(types_))
    ev_reply = next((e for e in events if e.get("type") == "reply"), {})
    content = json.dumps(ev_reply.get("data") or {}, ensure_ascii=False)
    check("归档预备: reply 事件含全量文本 (≤2000 非 1000 截断)",
          "告警共 3 条" in content)
    check("交付: 发送成功即 delivered", (task or {}).get("delivery_status") == "delivered")
    af = ta._archive_path(tid) if tid else Path("/nonexistent")
    atext = af.read_text(encoding="utf-8") if af.exists() else ""
    check("归档: JSONL 永久保留回复原文 (闭合飞书截断缺口)",
          "告警共 3 条" in atext, str(af))
    check("受众: 群 audience 随任务 meta 留痕",
          captured.get("provenance", {}).get("audience") == "feishu:group:oc_g1")
    ts_ev = next((e for e in events if e.get("type") == "task_started"), {})
    check("受众: task_started meta 含 chat_id/audience",
          (ts_ev.get("data") or {}).get("chat_id") == "oc_g1"
          and (ts_ev.get("data") or {}).get("audience") == "feishu:group:oc_g1")


async def test_feishu_timeout(tmp):
    """3. 超时: handler 闭合 failed 终态, 不虚报交付"""
    conv, bot = install_fakes(tmp, deadline_s=3)
    from core.task_runtime import get_task_runtime
    h = asyncio.create_task(run_patched(
        conv, "这个会不会超时呀", f"om_timeout1-{RUN}"))
    await asyncio.wait_for(h, 20)
    rt = get_task_runtime()
    from core.task_runtime import ledger as led
    r = await rt.ledger._redis()
    tid = await r.hget(led.k_idem(led.idem_hash(
        "alice", "feishu", "feishu:oc_g1", f"om_timeout1-{RUN}")), "task_id") or ""
    task = await rt.get_task(tid) if tid else {}
    check("超时: 终态闭合为 failed", (task or {}).get("status") == "failed")
    check("超时: 用户收到超时提示", any("超时" in x for x in bot.im_sent))
    check("超时: 交付状态不虚报 (非 delivered)",
          (task or {}).get("delivery_status") != "delivered")


async def test_feishu_degrade(tmp):
    """4. 登记失败 → 降级纯对话, 不阻断回复 (可用性优先)"""
    conv, bot = install_fakes(tmp)
    import config
    config.TR_FEISHU_ENABLED = True
    # 断链方案: 改坏类层 submit_for_chat 模拟账本故障 (测后恢复)
    import core.task_runtime.runtime as rt_mod
    orig = rt_mod.TaskRuntime.submit_for_chat

    async def boom(*a, **k):
        raise RuntimeError("simulated ledger outage")
    rt_mod.TaskRuntime.submit_for_chat = boom
    gate = asyncio.Event()
    try:
        f = asyncio.create_task(feed_agent("降级也应回答", gate=gate))
        await asyncio.wait_for(gate.wait(), 10)
        h = asyncio.create_task(run_patched(conv, "降级测试", f"om_degrade1-{RUN}"))
        fed = await asyncio.wait_for(f, 25)
        await asyncio.wait_for(h, 25)
    finally:
        rt_mod.TaskRuntime.submit_for_chat = orig
    check("降级: 登记失败仍完成回复", fed and any("降级也应回答" in x for x in bot.md_sent))
    # 降级路径不应误建任务
    from core.task_runtime import get_task_runtime
    from core.task_runtime import ledger as led
    rt = get_task_runtime()
    r = await rt.ledger._redis()
    tid = await r.hget(led.k_idem(led.idem_hash(
        "alice", "feishu", "feishu:oc_g1", f"om_degrade1-{RUN}")), "task_id")
    check("降级: 故障时确实未建任务 (降级非静默假登记)", not tid)


async def main():
    tmp = tempfile.mkdtemp(prefix="zops_p1_feishu_")
    # 归档目录必须在任何用例之前隔离: test_channel_passthrough 不经过 install_fakes,
    # 旧顺序会把测试任务事件写进生产 logs/tasks (永久归档污染, 11-01 实测 20 个脏文件)
    from pathlib import Path as _P
    from core.task_runtime import archive as _ta
    _ta.TASK_ARCHIVE_DIR = _P(tmp) / "tasks"
    try:
        await test_channel_passthrough()
        await test_feishu_happy_path(tmp)
        await test_feishu_degrade(tmp)
        await test_feishu_timeout(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n== P1 飞书接入汇总: PASS={len(PASS)} FAIL={len(FAIL)} ==")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
