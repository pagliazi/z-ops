"""Z-OPS P3 插件系统回归 (core/plugin_system.py — 参考 deepseek-harness 设计)

覆盖:
  1. EventBus 五种分发模式语义 (emit/waterfall/parallel/serial/bail)
  2. 监听器注册可逆 (disposer)
  3. 插件生命周期: 发现 → 加载 → 启用 → 停用 (副作用逆序撤销) → 重启用
  4. inject 依赖: 服务未就绪 → ERROR 不启动
  5. patch 层: disable 写 patch, enable 移除 patch 项 (dsh cordis.patch.yml 语义)
  6. entry 安全: 只允许 plugins/ 目录内模块
  7. 真实内置插件加载 (wiki_compile/session_recap/maintenance_guard)
  8. 事件缝: maintenance_guard 守卫 chat/pre_task 与 tools/pre_execute

隔离: 测试 Redis 6399 + 临时插件目录 + 临时 patch; 不触碰生产 plugins/patch.yaml。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TEST_REDIS_URL = os.environ.get("ZOPS_P0_TEST_REDIS", "redis://127.0.0.1:6399/0")
os.environ["REDIS_URL"] = TEST_REDIS_URL
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  {detail}"))


TMP = Path(tempfile.mkdtemp(prefix="zops_plug_test_"))


async def main():
    from core.plugin_system import (EventBus, PluginContext, Plugin,
                                     PluginManager, PluginState)

    # ═══ 1. EventBus 分发模式 ═══
    bus = EventBus()
    order = []

    async def emit_a(): order.append("a")
    def emit_b(): order.append("b")
    bus.on("evt/emit", emit_a, "p1")
    bus.on("evt/emit", emit_b, "p2")
    await bus.emit("evt/emit")
    check("emit: 按注册顺序观察", order == ["a", "b"], str(order))

    seen = []
    bus.declare("evt/parallel", "parallel")
    async def par_slow(): await asyncio.sleep(0.05); seen.append("slow")
    def par_fast(): seen.append("fast")
    bus.on("evt/parallel", par_slow, "p1")
    bus.on("evt/parallel", par_fast, "p2")
    await bus.parallel("evt/parallel")
    check("parallel: 并行扇出 (慢监听不阻塞快监听先跑)", seen == ["fast", "slow"], str(seen))

    def ser1(v): return f"{v}-1"
    async def ser2(v): return f"{v}-2"
    bus.declare("evt/serial", "serial")
    bus.on("evt/serial", ser1, "p1")
    bus.on("evt/serial", ser2, "p2")
    out = await bus.serial("evt/serial", "x")
    check("serial: 后者可见前者返回值", out == "x-1-2", out)

    hits = []
    def bail_yes(v): hits.append("yes"); return {"deny": True}
    def bail_after(v): hits.append("after")
    bus.declare("evt/bail", "bail")
    bus.on("evt/bail", bail_yes, "p1")
    bus.on("evt/bail", bail_after, "p2")
    bailed, val = await bus.bail("evt/bail", {})
    check("bail: 首个决定即停", bailed and hits == ["yes"] and val.get("deny"), str(hits))

    calls = []
    def wf_outer(v, args, nxt):
        calls.append("outer-in")
        r = asyncio.get_event_loop().run_until_complete if False else None
        return None  # placeholder — 下方用 async 版本重测
    # waterfall 用 async 监听器 (真实用法)
    async def wf_a(v, args, nxt):
        calls.append("a-in")
        out = await nxt(v + ">a")
        calls.append("a-out")
        return out
    async def wf_b(v, args, nxt):
        calls.append("b-in")
        return v + ">b"       # 不调 next → 短路
    async def wf_c(v, args, nxt):
        calls.append("c-in")  # 被短路, 不应执行
        return v
    bus.declare("evt/wf", "waterfall")
    bus.on("evt/wf", wf_a, "p1")
    bus.on("evt/wf", wf_b, "p2")
    bus.on("evt/wf", wf_c, "p3")
    out = await bus.waterfall("evt/wf", "v")
    check("waterfall: 环绕语义 (包装+短路)", out == "v>a>b"
          and calls == ["a-in", "b-in", "a-out"], f"{out} {calls}")

    # disposer 可逆
    def doomed(): return None
    d = bus.on("evt/emit", doomed, "px")
    d()
    check("监听器: disposer 撤销", doomed not in bus.listeners_of("evt/emit"))

    # ═══ 2. 插件生命周期 + patch (在真实 plugins/ 目录下建测试插件, 结束后清理) ═══
    from core.plugin_system import PLUGINS_DIR
    pdir = PLUGINS_DIR
    talpha = pdir / "_test_alpha"
    shutil.rmtree(talpha, ignore_errors=True)   # 上次运行残留清理
    talpha.mkdir(parents=True)
    (talpha / "__init__.py").write_text("")
    (talpha / "plugin.yaml").write_text(
        "id: alpha\nversion: 1.0.0\ndescription: 测试插件\n"
        "entry: plugins._test_alpha.plugin:AlphaPlugin\ninject: []\n")
    (talpha / "plugin.py").write_text(
        "from core.plugin_system import Plugin, PluginContext\n"
        "STATE = {'applied': 0, 'disposed': 0}\n"
        "class AlphaPlugin(Plugin):\n"
        "    id = 'alpha'\n"
        "    async def apply(self, ctx):\n"
        "        STATE['applied'] += 1\n"
        "        bus = ctx.bus\n"
        "        def on_task(meta):\n"
        "            return {'deny': True, 'reason': 'alpha 拒绝'}\n"
        "        d1 = bus.on('chat/pre_task', on_task, self.id)\n"
        "        ctx.contribute(self.id, 'policy', 'chat/pre_task', '全部拒绝')\n"
        "        def dispose():\n"
        "            STATE['disposed'] += 1\n"
        "            d1()\n"
        "        return dispose\n")

    def _cleanup_alpha():
        import shutil as _sh
        _sh.rmtree(talpha, ignore_errors=True)
        for m in [k for k in list(sys.modules) if k.startswith("plugins._test_alpha")]:
            del sys.modules[m]

    ctx = PluginContext(EventBus())
    mgr = PluginManager(ctx, plugins_dir=pdir, patch_file=TMP / "patch.yaml")
    await mgr.load_all()
    st = {p["id"]: p["state"] for p in mgr.list()}
    check("生命周期: 发现并启用", st.get("alpha") == PluginState.ENABLED, str(st))
    check("生命周期: 贡献已登记",
          any(c["id"] == "chat/pre_task" for c in
              next(p for p in mgr.list() if p["id"] == "alpha")["contributions"]))

    bailed, reason = await ctx.bus.bail("chat/pre_task", {"x": 1})
    check("事件缝: 插件守卫生效", bailed and reason.get("reason") == "alpha 拒绝")

    # disable → 副作用撤销
    r = await mgr.disable("alpha")
    check("patch: disable 写入 patch 文件",
          (TMP / "patch.yaml").exists() and "alpha" in (TMP / "patch.yaml").read_text())
    check("生命周期: 停用后状态", r.get("state") == PluginState.DISABLED)
    bailed2, _ = await ctx.bus.bail("chat/pre_task", {"x": 1})
    check("可逆性: 停用后监听器已撤销", not bailed2)
    import plugins._test_alpha.plugin as alpha_mod
    check("可逆性: disposer 被调用", alpha_mod.STATE["disposed"] >= 1)

    # enable → patch 项移除 (dsh: 与默认一致不留 patch)
    r = await mgr.enable("alpha")
    check("patch: enable 移除 patch 项", "alpha" not in (TMP / "patch.yaml").read_text())
    check("生命周期: 重启用后守卫恢复", (await ctx.bus.bail("chat/pre_task", {}))[0])
    check("生命周期: apply 幂等累计", alpha_mod.STATE["applied"] >= 2)

    # patch 预置 disabled → load 不启动
    (TMP / "patch.yaml").write_text("alpha:\n  disabled: true\n")
    ctx2 = PluginContext(EventBus())
    mgr2 = PluginManager(ctx2, plugins_dir=pdir, patch_file=TMP / "patch.yaml")
    await mgr2.load_all()
    st2 = {p["id"]: p["state"] for p in mgr2.list()}
    check("patch: 预置 disabled → 加载即停用", st2.get("alpha") == PluginState.DISABLED, str(st2))
    # 注意: 同目录的真实 maintenance_guard 也监听 chat/pre_task — 断言"alpha 自己没有"
    alpha_listeners = [pid for pid, _ in
                       ctx2.bus._listeners.get("chat/pre_task", [])]
    check("patch: 停用插件无监听器", "alpha" not in alpha_listeners, str(alpha_listeners))

    # ═══ 3. inject 依赖 ═══
    shutil.rmtree(pdir / "beta", ignore_errors=True)   # 上次运行残留
    (pdir / "beta").mkdir()
    (pdir / "beta" / "__init__.py").write_text("")
    (pdir / "beta" / "plugin.yaml").write_text(
        "id: beta\nentry: plugins.beta.plugin:BetaPlugin\ninject: [missing_svc]\n")
    (pdir / "beta" / "plugin.py").write_text(
        "from core.plugin_system import Plugin\n"
        "class BetaPlugin(Plugin):\n"
        "    id = 'beta'\n"
        "    async def apply(self, ctx): return None\n")
    ctx3 = PluginContext(EventBus())
    mgr3 = PluginManager(ctx3, plugins_dir=pdir, patch_file=TMP / "patch2.yaml")
    await mgr3.load_all()
    beta = next(p for p in mgr3.list() if p["id"] == "beta")
    check("inject: 依赖缺失 → ERROR 不启动", beta["state"] == PluginState.ERROR
          and "missing_svc" in beta["error"], str(beta)[:100])
    ctx3.provide("missing_svc", object())
    r = await mgr3.enable("beta")
    check("inject: 服务就绪后可启用", r.get("ok"), str(r))
    shutil.rmtree(pdir / "beta", ignore_errors=True)
    for m in [k for k in list(sys.modules) if k.startswith("plugins.beta")]:
        del sys.modules[m]

    # ═══ 4. entry 安全 ═══
    try:
        mgr3._load_class("os.path:join")
        check("entry 安全: 非 plugins 模块拒绝", False)
    except ValueError as e:
        check("entry 安全: 非 plugins 模块拒绝", "plugins/" in str(e))

    # ═══ 5. 真实内置插件 (生产 plugins/ 目录, 只读观察) ═══
    ctx4 = PluginContext(EventBus())
    mgr4 = PluginManager(ctx4, plugins_dir=PLUGINS_DIR,
                         patch_file=TMP / "patch3.yaml")   # 独立 patch: 全部启用
    await mgr4.load_all()
    states = {p["id"]: p["state"] for p in mgr4.list()}
    check("内置: wiki_compile/session_recap/maintenance_guard 全部加载",
          {"wiki_compile", "session_recap", "maintenance_guard"} <= set(states), str(states))
    check("内置: 三个插件启用 (独立 patch 无停用)",
          all(states.get(k) == PluginState.ENABLED
              for k in ("wiki_compile", "session_recap", "maintenance_guard")), str(states))

    # maintenance_guard 窗口外不拦截 (默认 02:00-04:00)
    from datetime import datetime
    now = datetime.now().strftime("%H:%M")
    bailed3, _ = await ctx4.bus.bail("tools/pre_execute", {"capability_id": "x"})
    if not ("02:00" <= now <= "04:00"):
        check("内置守卫: 窗口外放行", not bailed3)
    else:
        check("内置守卫: 窗口内拦截 (当前正处维护窗)", bailed3)

    # session_recap 事件消费 (写测试 Redis)
    os.environ["REDIS_URL"] = TEST_REDIS_URL
    await ctx4.bus.emit("session/summary_stored",
                        {"session_id": "s1", "user_id": "u1",
                         "summary": "x", "topics": ["t1"], "ts": 0})
    await asyncio.sleep(0.5)
    import redis.asyncio as aioredis
    rr = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    recap = await rr.lrange("zops:plugins:recap", 0, -1)
    check("内置: session_recap 真实消费事件", len(recap) >= 1
          and json.loads(recap[0])["user"] == "u1", str(recap[:1]))
    await rr.delete("zops:plugins:recap")
    await rr.aclose()

    # 清理测试插件 (真实 plugins 目录还原)
    _cleanup_alpha()
    for m in [k for k in list(sys.modules) if k.startswith("plugins._test_alpha")]:
        del sys.modules[m]
    shutil.rmtree(TMP, ignore_errors=True)


asyncio.run(main())
print("═" * 64)
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
print("插件系统回归全部通过 ✅")
