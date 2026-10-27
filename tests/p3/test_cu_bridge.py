"""Z-OPS P3 Computer Use 桥回归 (cu_bridge)

覆盖 2026-10-22 新增的 Chrome 扩展桥核心语义:
  1. 进程内注册表: 扩展接入/替换/断开, 状态查询, 命令 future 往返
  2. 断线时未决命令全部终结 (agent 不悬挂)
  3. 命令白名单 (未知方法拒绝 — 扩展不能被驱动执行任意动作)
  4. Redis 跨进程总线: 在线状态表 + agent_send_command 经 pubsub 往返
     (bus_loop 与 agent 侧在同一进程的不同 task 中模拟两进程拓扑)
  5. 查看者 backlog 回放 (新查看者立即拿到缓存帧+状态)

隔离: 测试 Redis 6399 (与生产 6379 完全分离); 无真实浏览器/WS/LLM。
无导入副作用: 导入本模块不执行任何检查, 由 unittest 驱动。
"""
import asyncio
import json
import os
import sys
import time
import unittest
from urllib.parse import urlparse

TEST_REDIS_URL = os.environ.get("ZOPS_P0_TEST_REDIS", "redis://127.0.0.1:6399/0")


def guard_test_redis(url):
    """强制隔离: 拒绝在疑似生产 Redis 上跑会 flushdb 的测试。"""
    parsed = urlparse(url)
    db = (parsed.path or "/0").strip("/") or "0"
    if parsed.port == 6379 and db == "0":
        raise RuntimeError(f"拒绝在疑似生产 Redis 上运行: {url}")


os.environ["REDIS_URL"] = TEST_REDIS_URL
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  {detail}"))


class FakeWs:
    """进程内假扩展/查看者 WS: send_text 收集, 收到 cmd 自动回结果 (可编程)"""

    def __init__(self, actor: str, auto_reply: bool = True):
        self.actor = actor
        self.sent: list = []
        self.closed = False
        self.auto_reply = auto_reply

    async def send_text(self, text: str):
        self.sent.append(text)
        if self.auto_reply:
            msg = json.loads(text)
            if msg.get("type") == "cmd":
                from core import cu_bridge
                asyncio.get_event_loop().create_task(cu_bridge.handle_ext_message(
                    self.actor, json.dumps(
                        {"type": "cmd_result", "id": msg["id"], "ok": True,
                         "data": {"echo": msg.get("method")}})))

    async def close(self):
        self.closed = True


async def main():
    import config
    # 导入顺序会决定 config 读到的地址: 若其他测试先导入 config, 本模块必须显式改回测试实例,
    # 否则总线和心跳断言会打到生产 Redis (违反 Spec 第 79 条的强制隔离)。
    guard_test_redis(TEST_REDIS_URL)
    config.REDIS_URL = TEST_REDIS_URL
    from core import cu_bridge

    # 测试 Redis 干净
    import redis.asyncio as aioredis
    r = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    await r.flushdb()
    check("隔离: 强制使用测试 Redis 实例", config.REDIS_URL == TEST_REDIS_URL, config.REDIS_URL)

    ACTOR = "cu_bridge_tester"

    # ═══ 1. 进程内注册表 ═══
    ws1 = FakeWs(ACTOR)
    await cu_bridge.attach_ext(ACTOR, ws1, meta={"client": "test"})
    st = cu_bridge.get_status(ACTOR)
    check("桥: 接入后 status.connected", st["connected"], str(st))

    res = await cu_bridge.send_command(ACTOR, "tabs", {}, timeout=5)
    check("桥: 命令 future 往返 (cmd_result 解析)", res.get("ok")
          and res.get("data", {}).get("echo") == "tabs", str(res)[:80])

    # 旧连接被新连接顶替
    ws2 = FakeWs(ACTOR)
    await cu_bridge.attach_ext(ACTOR, ws2, meta={"client": "test2"})
    check("桥: 新接入顶替旧连接 (旧 ws 被关闭)", ws1.closed)

    res_bad = await cu_bridge.send_command(ACTOR, "eval_main", {}, timeout=3)
    check("桥: 白名单外方法拒绝", not res_bad.get("ok")
          and "未知命令" in res_bad.get("error", ""), str(res_bad)[:80])

    # ═══ 2. 未决命令随断线终结 ═══
    ws3 = FakeWs(ACTOR, auto_reply=False)   # 不回 → future 挂起
    await cu_bridge.attach_ext(ACTOR, ws3, meta={})
    t_fut = asyncio.get_event_loop().create_task(
        cu_bridge.send_command(ACTOR, "screenshot", {}, timeout=10))
    await asyncio.sleep(0.3)                 # 命令已入 pending
    ch = cu_bridge._channels[ACTOR]
    check("桥: 命令挂起在 pending", len(ch.pending) == 1, str(ch.pending.keys()))
    await cu_bridge.detach_ext(ACTOR, ws3)
    res_dead = await t_fut
    check("桥: 断线终结未决命令 (不悬挂)", not res_dead.get("ok")
          and "断开" in res_dead.get("error", ""), str(res_dead)[:80])

    res_none = await cu_bridge.send_command(ACTOR, "goto", {"url": "https://x.com"}, timeout=3)
    check("桥: 无扩展时命令明确失败", not res_none.get("ok")
          and "未连接" in res_none.get("error", ""), str(res_none)[:80])

    # ═══ 3. Redis 总线 (跨进程拓扑) ═══
    await cu_bridge.bus_init()
    await asyncio.sleep(1.0)                 # bus_loop 订阅就绪
    # 清掉进程内注册表, 模拟 "backend 进程" 只经 Redis 与 agent 交互
    ws4 = FakeWs(ACTOR)
    await cu_bridge.attach_ext(ACTOR, ws4, meta={"client": "bus-test"})

    check("总线: 接入写入 Redis 状态表", await cu_bridge.agent_is_connected(ACTOR),
          "ext_status hash 缺失")

    res_bus = await cu_bridge.agent_send_command(ACTOR, "snapshot", {"k": 1}, timeout=8)
    check("总线: agent_send_command 经 pubsub 完整往返", res_bus.get("ok")
          and res_bus.get("data", {}).get("echo") == "snapshot", str(res_bus)[:100])

    res_off = await cu_bridge.agent_send_command("nobody_online", "goto", {}, timeout=4)
    check("总线: 无扩展 actor 命令失败", not res_off.get("ok"), str(res_off)[:80])

    await cu_bridge.detach_ext(ACTOR, ws4)
    await asyncio.sleep(0.3)
    check("总线: 断开后状态表清除", not await cu_bridge.agent_is_connected(ACTOR))

    # ═══ 4. 查看者 backlog 回放 ═══
    await cu_bridge.handle_ext_message(
        ACTOR, json.dumps({"type": "frame", "jpeg": "FAKEFRAME", "w": 1, "h": 1}))
    await cu_bridge.handle_ext_message(
        ACTOR, json.dumps({"type": "state", "url": "https://a.com", "title": "A"}))
    viewer = FakeWs(ACTOR, auto_reply=False)
    backlog = await cu_bridge.add_viewer(ACTOR, viewer)
    check("查看者: backlog 含缓存帧", backlog.get("frame", {}).get("jpeg") == "FAKEFRAME")
    check("查看者: backlog 含状态", backlog.get("state", {}).get("url") == "https://a.com")
    # 查看者接入触发 stream:on 下发到扩展
    if ws4.closed or True:
        # 重新接入一个扩展来验证 stream 同步
        ws5 = FakeWs(ACTOR, auto_reply=False)
        await cu_bridge.attach_ext(ACTOR, ws5, meta={})
        v2 = FakeWs(ACTOR, auto_reply=False)
        await cu_bridge.add_viewer(ACTOR, v2)
        await asyncio.sleep(0.5)
        stream_msgs = [json.loads(s) for s in ws5.sent if json.loads(s).get("type") == "stream"]
        check("查看者: 扩展收到 stream:on", any(m["on"] for m in stream_msgs),
              str(ws5.sent)[-100:])
        await cu_bridge.remove_viewer(ACTOR, v2)
        await cu_bridge.remove_viewer(ACTOR, viewer)   # 第一个查看者也退出 → 真正清空
        await asyncio.sleep(0.5)
        off = [json.loads(s) for s in ws5.sent if json.loads(s).get("type") == "stream"]
        check("查看者: 全部退出后 stream:off", any(not m["on"] for m in off))
        await cu_bridge.detach_ext(ACTOR, ws5)
    await cu_bridge.remove_viewer(ACTOR, viewer)

    # ═══ 5. 心跳续期: 扩展长时间在线不得被误判离线 ═══
    ws6 = FakeWs(ACTOR, auto_reply=False)
    await cu_bridge.attach_ext(ACTOR, ws6, meta={})
    ch6 = cu_bridge._channels[ACTOR]
    ch6.last_hb = time.time() - 120
    await r.hset(cu_bridge._KEY_STATUS, ACTOR,
                 json.dumps({"connected": True, "ts": time.time() - 120}))
    check("心跳: 状态超过 90s 未刷新 → 视为离线",
          not await cu_bridge.agent_is_connected(ACTOR))
    await cu_bridge.handle_ext_message(ACTOR, json.dumps({"type": "ping"}))
    check("心跳: 扩展 ping 后在线状态续期 (不再误回退服务端)",
          await cu_bridge.agent_is_connected(ACTOR))
    await cu_bridge.detach_ext(ACTOR, ws6)

    # ═══ 6. 连接码配对 (扩展免输 AD 密码) ═══
    pair = await cu_bridge.create_pair_code("pair_actor")
    code = pair.get("code", "")
    check("配对: 连接码为 8 位", len(code) == 8, code)
    check("配对: 连接码兑换账号 (大小写不敏感)",
          await cu_bridge.claim_pair_code(code.lower()) == "pair_actor")
    try:
        await cu_bridge.claim_pair_code(code)
        reused = False
    except ValueError:
        reused = True
    check("配对: 连接码一次性 (重兑失败)", reused)
    try:
        await cu_bridge.claim_pair_code("ZZZZZZZZ")
        bad_ok = False
    except ValueError:
        bad_ok = True
    check("配对: 无效连接码拒绝", bad_ok)

    await r.flushdb()
    await r.aclose()

    # 总线任务清场 (避免事件循环告警)
    if cu_bridge._bus_task:
        cu_bridge._bus_task.cancel()
        try:
            await cu_bridge._bus_task
        except (asyncio.CancelledError, Exception):
            pass


class CuBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        PASS.clear()
        FAIL.clear()
        import config
        guard_test_redis(TEST_REDIS_URL)
        config.REDIS_URL = TEST_REDIS_URL
        from core import cu_bridge
        cu_bridge._channels.clear()

    async def test_bridge_flow(self):
        await main()
        self.assertEqual(FAIL, [], f"失败项: {FAIL}")


if __name__ == "__main__":
    unittest.main()
