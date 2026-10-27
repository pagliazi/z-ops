#!/usr/bin/env python3
"""Phase 2.4 子代理 sidechain 冒烟测试 (不依赖 Redis/LLM)"""
import asyncio
import json
import os
import sys
import time
import uuid

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

from core.delegation.sidechain import (
    run_sidechain_tasks, append_sidechain_event, read_sidechain,
    summarize_for_parent, format_summaries, sidechain_path,
)

# ── 1) summarize_for_parent: 摘要限长 ──
s = summarize_for_parent({"a": "x" * 5000, "b": 123}, max_chars=300)
assert len(s) <= 320 and '"x"' not in s, len(s)  # dict 按字段截断
s2 = summarize_for_parent("x" * 5000, max_chars=300)
assert "已截断" in s2 and len(s2) <= 340, len(s2)
assert summarize_for_parent("ok") == "ok"
print("PASS 1: summarize_for_parent 摘要限长 (dict 字段截断 + 纯文本标注)")

# ── 2) run_sidechain_tasks: 并行 + sidechain 落盘 + summary-only ──
async def main():
    run_tag = uuid.uuid4().hex[:6]
    items = [
        {"task_id": f"t{run_tag}-{i}", "description": f"子任务{i}", "params": {"n": i}}
        for i in range(5)
    ]

    async def exec_fn(item):
        await asyncio.sleep(0.05 * item["params"]["n"])
        if item["params"]["n"] == 3:
            raise RuntimeError("boom")
        return {"n": item["params"]["n"], "detail": "大量原始数据" * 500}

    t0 = time.time()
    results = await run_sidechain_tasks("ops_agent", items, exec_fn, username="tester", max_concurrent=3)
    elapsed = time.time() - t0

    assert len(results) == 5
    ok = [r for r in results if r["success"]]
    bad = [r for r in results if not r["success"]]
    assert len(ok) == 4 and len(bad) == 1
    assert bad[0]["task_id"].endswith("-3") and "boom" in bad[0]["error"]
    # summary-only: 父上下文拿到的是摘要不是全文 (detail 字段全文 3000 字符)
    for r in ok:
        assert len(r["summary"]) <= 650, len(r["summary"])
        assert r["summary"].count("大量原始数据") <= 30, len(r["summary"])  # 全文有 500 个重复串
    # 并行验证: 串行 sleep 总和 0.50s + 10 次 fsync 串行落盘开销; 并发3 显著快于串行
    assert elapsed < 0.8, elapsed
    print(f"PASS 2: 并行子任务 (5 项 {elapsed:.2f}s, 4 成功 1 失败, summary-only)")

    # ── 3) sidechain transcript: append-only 事件永久保留 ──
    for r in ok:
        events = read_sidechain("ops_agent", r["task_id"])
        assert len(events) == 2, events
        assert events[0]["type"] == "task_started" and events[0]["params"] == {"n": int(r["task_id"].split("-")[1])}
        assert events[1]["type"] == "task_completed" and events[1]["success"] is True
        # 完整结果在 sidechain (未被摘要截断)
        assert "大量原始数据" * 10 in json.dumps(events[1], ensure_ascii=False)
    events = read_sidechain("ops_agent", f"t{run_tag}-3")
    assert len(events) == 2 and events[1]["type"] == "task_failed" and "boom" in events[1]["error"]
    # 路径符合审计约定
    p = sidechain_path("ops_agent", f"t{run_tag}-0")
    assert f"workspaces/ops_agent/sidechains/t{run_tag}-0.jsonl" in str(p), p
    print("PASS 3: sidechain transcript append-only 完整保留 (含完整 result)")

    # ── 4) format_summaries 渲染 ──
    block = format_summaries(results)
    assert "✅" in block and "❌" in block and f"t{run_tag}-3" in block
    print("PASS 4: format_summaries 父上下文摘要块")

asyncio.run(main())

# ── 5) SubagentManager: exec_fn 进程内执行路径 (修复 run_agent 不可 await) ──
async def main_mgr():
    run_tag2 = uuid.uuid4().hex[:6]
    from core.delegation.subagent_manager import SubagentManager, TaskSpec
    mgr = SubagentManager()

    async def my_exec(task, params):
        return {"echo": task, "params": params}

    tasks = [
        TaskSpec(task_id=f"m{run_tag2}-{i}", task_description=f"任务{i}", agent_type="ops_agent",
                 input_params={"i": i}, timeout=10, exec_fn=my_exec)
        for i in range(3)
    ]
    results = await mgr.delegate_parallel(tasks, max_concurrent=3)
    assert len(results) == 3 and all(r.success for r in results), results
    assert results[0].result == {"echo": "任务0", "params": {"i": 0}}
    # spawn 路径 sidechain 审计
    events = read_sidechain("ops_agent", f"m{run_tag2}-0")
    assert len(events) == 2 and events[0]["type"] == "task_started"
    assert events[1]["type"] == "task_completed" and events[1]["success"] is True
    print("PASS 5: SubagentManager delegate_parallel exec_fn 路径 + sidechain 审计")

asyncio.run(main_mgr())
print("\n全部通过 ✓")
