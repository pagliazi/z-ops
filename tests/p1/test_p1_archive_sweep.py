"""Z-OPS P1 验收测试 — 永久归档补偿扫描 + UNKNOWN 闭合归档 (上线门实测驱动)

生产门 4 实测发现: 3 个 UNKNOWN (孤儿回收闭合) 任务的 82 条事件只在 Redis, JSONL 无文件。
根因: reclaim 调 ledger.complete_unknown 后未归档确认; 且 60s 回收循环无归档补偿扫描。

本用例锁定修复:
  1. 孤儿闭合为 UNKNOWN 时同步完成归档确认 (崩溃类任务最需审计);
  2. archive_sweep_once 把仅在 Redis 的事件补写 JSONL (只补写不删除, 幂等);
  3. 补写后诊断端点不再报归档 blocker;
  4. 未终态 (非 terminal) 任务不被 sweep;
  5. 重复 sweep 不重复写入 (幂等, 按 event_id 去重)。

隔离: Redis 6399 + 临时归档目录; 无真实外部系统。
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


async def make_terminal(rt, ta, key, n_events=6):
    """构造一个已终态但归档缺失的任务 (Redis 有完整事件)"""
    tid, created, fenc, own = await rt.submit_for_chat(
        "sweeper", f"s:{RUN}", f"{RUN}-{key}", "归档补扫样本", "diag", channel="webchat")
    for i in range(n_events):
        await rt.emit(tid, own, fenc, "delta", {"text": f"c{i}", "kind": "content"})
    await rt.complete(tid, own, fenc, "succeeded", {"text": "done"})
    p = Path(ta.TASK_ARCHIVE_DIR) / f"{tid}.jsonl"
    if p.exists():
        p.unlink()   # 模拟历史遗留: 文件从未落盘 (Redis 抖动/进程 SIGKILL 前)
    return tid, await (await rt.ledger._redis()).llen(f"{config.REDIS_PREFIX}:tr:events:{tid}")


async def case_orphan_closure_archives(rt, ta):
    """修复 1: 孤儿回收闭合 UNKNOWN 时同口径完成归档确认"""
    tid, created, fenc, own = await rt.submit_for_chat(
        "crashy", f"s:{RUN}", f"{RUN}-orphan", "孤儿回收", "diag", channel="webchat")
    await rt.emit(tid, own, fenc, "delta", {"text": "before crash"})
    r = await rt.ledger._redis()
    evk = f"{config.REDIS_PREFIX}:tr:events:{tid}"
    await r.zadd(f"{config.REDIS_PREFIX}:tr:active", {tid: time.time()})
    await r.hset(f"{config.REDIS_PREFIX}:tr:task:{tid}", "status", "running")
    await r.hset(f"{config.REDIS_PREFIX}:tr:task:{tid}", "lease_owner", own)
    await r.hset(f"{config.REDIS_PREFIX}:tr:task:{tid}", "lease_expires_ts", str(time.time() - rt.lease_s * 4 - 5))
    closed = await rt.reclaim_orphans_once()
    check("孤儿: 回收闭合 UNKNOWN", closed >= 1
          and (await rt.get_task(tid))["status"] == "unknown", str(await rt.get_task(tid)))
    p = Path(ta.TASK_ARCHIVE_DIR) / f"{tid}.jsonl"
    check("孤儿: 闭合即归档确认 (旧实现此处未归档 → UNKNOWN 无 JSONL)", p.exists(), str(p))
    arch_ids = ta.archived_event_ids(tid)
    check("孤儿: 终态事件可审计且不漏 (Redis↔归档集合一致)", arch_ids >= {
        str(json.loads(x).get("event_id")) for x in await r.lrange(evk, 0, -1)}, str(arch_ids))


async def case_sweep_backfills_gaps(rt, ta):
    """修复 2: archive_sweep_once 幂等补写, 只增不删"""
    for tid, _ in [await make_terminal(rt, ta, f"gap{i}") for i in range(2)]:
        pass
    tid1, n1 = await make_terminal(rt, ta, "g10", n_events=5)
    tid2, n2 = await make_terminal(rt, ta, "g11", n_events=4)
    p1, p2 = Path(ta.TASK_ARCHIVE_DIR) / f"{tid1}.jsonl", Path(ta.TASK_ARCHIVE_DIR) / f"{tid2}.jsonl"
    check("补扫: 构造两个无归档的终态任务", not p1.exists() and not p2.exists())
    res = await rt.archive_sweep_once()
    check("补扫: repaired≥9 (覆盖两任务全部事件)", res["repaired"] >= 9, str(res))
    check("补扫: JSONL 文件已生成", p1.exists() and p2.exists())
    ids1 = ta.archived_event_ids(tid1)
    r = await rt.ledger._redis()
    redis_ids = {str(json.loads(x).get("event_id")) for x in await r.lrange(f"{config.REDIS_PREFIX}:tr:events:{tid1}", 0, -1)}
    check("补扫: Redis 事件与归档集合一致 (可回放不缺口)", redis_ids <= ids1, str(len(redis_ids)))
    res2 = await rt.archive_sweep_once()
    check("补扫: 第二次幂等不重复写入", res2["repaired"] == 0, str(res2))


async def case_sweep_skips_inflight(rt, ta):
    """修复 4: 未终态 (queued/running) 不被 sweep 误写 (只有 terminal 才归档闭合)"""
    tid, created, fenc, own = await rt.submit_for_chat(
        "still", f"s:{RUN}", f"{RUN}-running", "未终态", "diag", channel="webchat")
    res = await rt.archive_sweep_once()
    p = Path(ta.TASK_ARCHIVE_DIR) / f"{tid}.jsonl"
    check("补扫: 跳过未终态任务", not p.exists() and (await rt.get_task(tid))["status"] in ("running", "queued"))


async def case_diag_no_blindspot(rt, ta):
    """修复 3: 补扫后诊断不再出现归档盲区 blocker"""
    from core.task_runtime import diagnostics as dg
    rep = await dg.collect(await rt.ledger._redis())
    check("诊断: 补扫后归档盲区 blocker 清除",
          not any("归档" in b for b in rep["blockers"]), str([b for b in rep["blockers"] if "归档" in b]))


async def main():
    tmp = tempfile.mkdtemp(prefix="zops_p1_sweep_")
    import core.task_runtime.runtime as rt_mod
    from core.task_runtime import archive as ta
    ta.TASK_ARCHIVE_DIR = Path(tmp) / "tasks"
    rt_mod._runtime = rt_mod.TaskRuntime(lease_s=2)
    rt = rt_mod._runtime
    r = await rt.ledger._redis()
    await r.flushdb()
    try:
        await case_orphan_closure_archives(rt, ta)
        await case_sweep_backfills_gaps(rt, ta)
        await case_sweep_skips_inflight(rt, ta)
        await case_diag_no_blindspot(rt, ta)
    finally:
        try:
            await r.aclose()
        except Exception:
            await r.close()
        rt_mod._runtime = None
        shutil.rmtree(tmp, ignore_errors=True)
    print("═" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.new_event_loop().run_until_complete(main())
