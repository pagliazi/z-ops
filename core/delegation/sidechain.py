"""
Z-OPS v4.0 Phase 2.4 — 子代理 sidechain transcript (借鉴 claude-code §8.3)

每个并行子任务写独立 sidechain:
    workspaces/{agent}/sidechains/{task_id}.jsonl — append-only, 永久保留
    (审计红线: 与对话历史同级资产, 永不删除/改写)

事件类型:
    {"type": "task_started",  "task_id", "description", "agent", "username"}
    {"type": "task_completed","task_id", "success", "result"(完整), "execution_time"}
    {"type": "task_failed",   "task_id", "error", "execution_time"}

summary-only return (借鉴 claude-code 子代理只回摘要不回全史):
    父上下文只收 summarize_for_parent() 生成的摘要文本;
    完整结果仅存 sidechain, 父代理需要时按 task_id 取回。

激活闲置的 core/delegation/: run_sidechain_tasks() 提供进程内并行子任务
执行路径 (SubagentManager._invoke_agent 依赖的 run_agent 是进程入口不可
await — 这是 delegation 此前未融入主流程的根因), 并在 ExecutionRegistry
记录 task_id 关联。

接入点:
    agents/ops_agent.py 巡检类 handler (多主机 → 并行子任务)
    core/delegation/subagent_manager.py _execute_subagent_task (spawn 路径审计)
"""

import json
import os
import time
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Callable, List, Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import config

logger = logging.getLogger("zops")

SIDECHAIN_ROOT = Path(__file__).resolve().parent.parent.parent / "workspaces"

# sidechain 父上下文摘要上限 (完整结果只留 sidechain 文件)
SIDECHAIN_SUMMARY_CHARS = getattr(config, "SIDECHAIN_SUMMARY_CHARS", 600)
# 单个子任务默认超时 (秒)
SIDECHAIN_TASK_TIMEOUT = getattr(config, "SIDECHAIN_TASK_TIMEOUT", 120)

_write_locks: dict = {}


def sidechain_path(agent_type: str, task_id: str) -> Path:
    """子任务 sidechain 文件路径 (task_id 参与文件名 — 过滤路径穿越字符)"""
    safe_agent = "".join(c for c in agent_type if c.isalnum() or c in "-_.") or "unknown"
    safe_task = "".join(c for c in task_id if c.isalnum() or c in "-_.") or uuid.uuid4().hex[:8]
    return SIDECHAIN_ROOT / safe_agent / "sidechains" / f"{safe_task}.jsonl"


def _get_lock(key: str) -> asyncio.Lock:
    if key not in _write_locks:
        _write_locks[key] = asyncio.Lock()
    return _write_locks[key]


async def append_sidechain_event(agent_type: str, task_id: str, event: dict) -> bool:
    """append-only 追加 sidechain 事件 (失败仅告警, 不阻断主流程)"""
    try:
        event = {"ts": time.time(), **event}
        line = json.dumps(event, ensure_ascii=False)
        path = sidechain_path(agent_type, task_id)
        async with _get_lock(f"{agent_type}:{task_id}"):
            def _write():
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            await asyncio.get_running_loop().run_in_executor(None, _write)
        return True
    except Exception as e:
        logger.warning(f"[sidechain] 写入失败 agent={agent_type} task={task_id}: {e}")
        return False


def read_sidechain(agent_type: str, task_id: str) -> List[dict]:
    """读取子任务完整 sidechain (审计/回放用)"""
    path = sidechain_path(agent_type, task_id)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def summarize_for_parent(full_result, max_chars: int = None) -> str:
    """生成回传父上下文的摘要 (完整结果只留 sidechain)

    dict 结果保留关键字段 + 截断文本; 长文本截断并标注 sidechain 可取回。
    """
    limit = max_chars or SIDECHAIN_SUMMARY_CHARS
    try:
        if isinstance(full_result, dict):
            # 保留结构性字段, 文本字段截断
            slim = {}
            for k, v in full_result.items():
                text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                slim[k] = text[:limit // 4] if len(text) > limit // 4 else text
            out = json.dumps(slim, ensure_ascii=False)
        else:
            out = str(full_result)
    except Exception:
        out = str(full_result)
    if len(out) > limit:
        out = out[:limit] + f"…(共 {len(out)} 字符已截断, 完整结果见 sidechain)"
    return out


async def _record_registry(parent_agent: str, task_id: str, summary: str,
                           success: bool, duration_ms: float, sidechain_file: str):
    """ExecutionRegistry 记录 task_id 关联 (msg → 父 agent → 子任务 全链可查)"""
    try:
        from core.execution_registry import get_execution_registry
        reg = get_execution_registry()
        await reg.record_tool_call(
            agent_id=parent_agent,
            tool_name=f"sidechain:{task_id}",
            input_summary=f"sidechain task {task_id}",
            output_summary=f"{('OK' if success else 'FAIL')} | sidechain={sidechain_file} | {summary[:200]}",
            success=success,
            latency_ms=duration_ms,
            error="" if success else summary[:200],
        )
    except Exception as e:
        logger.debug(f"[sidechain] ExecutionRegistry 记录失败 task={task_id}: {e}")


async def run_sidechain_tasks(
    parent_agent: str,
    items: List[dict],
    exec_fn: Callable,
    username: str = "",
    max_concurrent: int = 4,
    item_timeout: int = None,
) -> List[dict]:
    """并行执行子任务 (每个子任务独立 sidechain transcript, 只回摘要)

    Args:
        parent_agent: 父 agent 名 (sidechain 落 workspaces/{parent_agent}/sidechains/)
        items: [{"task_id": "...", "description": "...", "params": {...}}, ...]
        exec_fn: async (item) -> 完整结果 (任意可 JSON 序列化对象)
        username: 发起用户 (审计)
        max_concurrent: 并发上限 (Semaphore)
        item_timeout: 单任务超时秒数

    Returns:
        [{"task_id", "description", "success", "summary", "sidechain",
           "execution_time", "error"}] — summary-only, 完整结果在 sidechain
    """
    timeout = item_timeout or SIDECHAIN_TASK_TIMEOUT
    semaphore = asyncio.Semaphore(max(1, max_concurrent))
    batch_id = uuid.uuid4().hex[:8]
    started = time.time()
    # P3 (task-c17): task+children 共享预算预留/结算 — 父上下文预算继承,
    # 无预算时不限额但仍记账; 超限子任务 fail-fast 拒绝派发
    from core.request_context import current_request_context as _cur_ctx
    from core.delegation.budget import SidechainBudget
    _ctx = _cur_ctx()
    _cap = (int(_ctx.budget_tokens) if (_ctx and _ctx.budget_tokens) else 0) or None
    _req_key = (_ctx.request_id if _ctx else "") or batch_id
    budget = SidechainBudget(f"sc:{parent_agent}:{_req_key}", cap_tokens=_cap)
    _PER_ITEM_RESERVE = max(1, (_cap or 0) // max(1, len(items)) ) if _cap else 1000
    logger.info(f"[sidechain] 并行子任务批次 {batch_id}: {len(items)} 项 (父: {parent_agent}, "
                f"budget_cap={_cap or '∞'})")

    async def _run_one(item: dict) -> dict:
        task_id = item.get("task_id") or f"task-{uuid.uuid4().hex[:8]}"
        desc = item.get("description", "")
        t0 = time.time()
        if not await budget.reserve(task_id, _PER_ITEM_RESERVE):
            return {
                "task_id": task_id, "description": desc, "success": False,
                "summary": f"预算不足: 预留 {_PER_ITEM_RESERVE} 被拒绝 (共享预算超限)",
                "sidechain": "", "execution_time": 0.0,
                "error": "budget_exceeded",
            }
        await append_sidechain_event(parent_agent, task_id, {
            "type": "task_started", "task_id": task_id, "batch_id": batch_id,
            "description": desc, "agent": parent_agent,
            "username": username or "", "params": item.get("params", {}),
        })
        success, full, error = True, None, ""
        try:
            full = await asyncio.wait_for(exec_fn(item), timeout=timeout)
        except asyncio.TimeoutError:
            success, error = False, f"子任务超时 ({timeout}s)"
        except Exception as e:
            success, error = False, str(e)
        duration_ms = (time.time() - t0) * 1000
        # 预算结算: 实际用量 = 结果报告 usage 或摘要长度估算 (失败按预留半量计)
        usage = 0
        if isinstance(full, dict):
            u = full.get("usage") or {}
            usage = int(u.get("total_tokens") or u.get("output_tokens") or 0)
        if usage <= 0:
            usage = max(64, len(str(full if success else error)) * 2)
        await budget.settle(task_id, usage, kind="child")
        await append_sidechain_event(parent_agent, task_id, {
            "type": "task_completed" if success else "task_failed",
            "task_id": task_id, "batch_id": batch_id,
            "success": success, "execution_time": round(duration_ms / 1000, 2),
            **({"result": full} if success else {"error": error}),
        })
        summary = summarize_for_parent(full) if success else f"失败: {error}"
        sc_file = str(sidechain_path(parent_agent, task_id))
        await _record_registry(parent_agent, task_id, summary, success, duration_ms, sc_file)
        return {
            "task_id": task_id, "description": desc, "success": success,
            "summary": summary, "sidechain": sc_file,
            "execution_time": round(duration_ms / 1000, 2), "error": error,
        }

    async def _gated(item):
        async with semaphore:
            return await _run_one(item)

    results = await asyncio.gather(*[_gated(it) for it in items], return_exceptions=True)
    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append({
                "task_id": items[i].get("task_id", f"task-{i}"),
                "description": items[i].get("description", ""),
                "success": False, "summary": f"失败: {r}", "error": str(r),
                "sidechain": "", "execution_time": 0.0,
            })
        else:
            out.append(r)
    logger.info(f"[sidechain] 批次 {batch_id} 完成: {len(out)} 项, "
                f"成功 {sum(1 for x in out if x['success'])}, 耗时 {time.time() - started:.1f}s")
    # P3: 批次结束核对未结算预留 (崩溃残留回收, 宽限期内保留)
    try:
        await budget.reconcile()
    except Exception as e:
        logger.debug(f"[sidechain] 预算核对跳过: {e}")
    return out


def format_summaries(results: List[dict]) -> str:
    """把 sidechain 结果列表渲染为父上下文可读的摘要块"""
    lines = []
    for r in results:
        mark = "✅" if r.get("success") else "❌"
        lines.append(
            f"{mark} [{r.get('task_id')}] {r.get('description', '')} "
            f"({r.get('execution_time', 0)}s)\n   {r.get('summary', '')}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    print("sidechain: append_sidechain_event / read_sidechain / run_sidechain_tasks / "
          "summarize_for_parent / format_summaries")
