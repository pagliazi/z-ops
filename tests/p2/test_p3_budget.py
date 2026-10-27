"""Z-OPS P3 验收 — 子任务共享预算预留/结算 (隔离 Redis 6399)"""
import asyncio, os, sys, time
TEST_REDIS = os.environ.get("ZOPS_P0_TEST_REDIS", "redis://127.0.0.1:6399/0")
os.environ["REDIS_URL"] = TEST_REDIS
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import config
PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))

async def test_budget():
    from core.delegation.budget import SidechainBudget
    b = SidechainBudget("test-b1", cap_tokens=700)
    r = await b._r()
    await r.delete(b.key); await r.aclose()
    check("预留: 上限内通过", await b.reserve("t1", 400) is True)
    check("预留: 幂等重预留", await b.reserve("t1", 400) is True)
    check("预留: 超上限拒绝", await b.reserve("t2", 400) is False)
    await b.settle("t1", 350, kind="child")
    snap = await b.snapshot()
    check("结算: 预留释放+实际入账", snap["reserved"] == 0 and snap["used"] == 350, str(snap))
    check("结算: 释放后额度可再预留", await b.reserve("t3", 300) is True)
    # 崩溃核对: 伪造 stale 预留
    r = await b._r()
    import json as _j
    entries = _j.loads((await r.hget(b.key, "entries")) or "{}")
    entries["t-crash"] = {"amount": 250, "ts": time.time() - 99999}
    await r.hset(b.key, "entries", _j.dumps(entries))
    await r.hset(b.key, "reserved", str(int(await r.hget(b.key, "reserved")) + 250))
    await r.aclose()
    rec = await b.reconcile()
    check("核对: 崩溃残留回收 (used 不清零)", rec["reclaimed"] == 1 and rec["amount"] == 250)
    snap2 = await b.snapshot()
    check("核对: 回收后 reserved 只剩活跃项", snap2["reserved"] == 300 and snap2["used"] == 350, str(snap2))

async def test_sidechain_budget_gate():
    from core.delegation.sidechain import run_sidechain_tasks
    from core.request_context import RequestContext, request_scope
    import tempfile, shutil
    from pathlib import Path
    tmp = tempfile.mkdtemp()
    os.chdir("/opt/zops")   # sidechain 落 workspaces/ 相对路径
    async def exec_fn(item):
        await asyncio.sleep(0.05)
        return {"out": "x" * 500}
    items = [{"task_id": f"bt-{i}", "description": f"d{i}"} for i in range(3)]
    with request_scope(RequestContext(actor="alice", budget_tokens=5000)):
        results = await run_sidechain_tasks("ops_agent", items, exec_fn,
                                            username="t", max_concurrent=2)
    check("sidechain: 预算内全部执行", all(r["success"] for r in results),
          str([r.get("error") for r in results]))
    # 极小预算 → 后续子任务 budget_exceeded
    with request_scope(RequestContext(actor="alice", budget_tokens=10)):
        results2 = await run_sidechain_tasks("ops_agent", items, exec_fn,
                                             username="t", max_concurrent=1)
    denied = [r for r in results2 if r.get("error") == "budget_exceeded"]
    check("sidechain: 超预算 fail-fast 拒绝派发", len(denied) >= 2, str(len(denied)))
    shutil.rmtree(tmp, ignore_errors=True)

async def main():
    await test_budget()
    await test_sidechain_budget_gate()
    print("═" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL: sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
