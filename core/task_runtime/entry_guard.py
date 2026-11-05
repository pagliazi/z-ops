"""P1 入口可靠登记 — 非对话型入口 (n8n 回调等) 的幂等守卫

背景: n8n webhook 是 at-least-once (工作流重试/网络重发都会再打一次), 而处理动作
会产生副作用 (发布通知 → 飞书推送)。旧路径无去重 → 重复告警/重复通知。

语义 (保守, 不改变原有处理行为):
  - 首次回调        → 登记任务 + 处理 + succeeded 终态 (永久事件留痕);
  - 重复回调且前次 succeeded → 判重复, 跳过副作用 (去重生效);
  - 重复回调但前次 failed/unknown/在途 → 另记一次尝试任务 (保留原尝试审计,
    绝不覆盖已终态任务), 处理照常执行 — 失败后重试不会被幂等键卡死;
  - 账本不可用/开关关闭 → 降级为不登记, 处理链路完全不受影响 (可用性优先)。
"""

import hashlib
import json
import logging
import time
from typing import Optional

import config

logger = logging.getLogger("zops")

CHANNEL = "n8n"
TARGET = "n8n_webhook"


def client_key(body: dict) -> str:
    """回调幂等键: 优先 execution_id / dedupe_key, 否则 action+规范化 payload 摘要"""
    ex = str(body.get("execution_id") or body.get("dedupe_key") or "").strip()
    if ex:
        return f"{CHANNEL}:{ex}"
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)[:4000]
    return f"{CHANNEL}:sha:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _action(body: dict) -> str:
    return str(body.get("action") or body.get("callback_type") or "unknown")


def _message(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False, default=str)[:2000]


async def register(body: dict) -> dict:
    """登记一次回调; 返回 guard dict (含 duplicate 标志)。永不抛异常 (降级优先)。"""
    guard = {"runtime": None, "task_id": "", "owner": "", "fencing": 0,
             "action": _action(body), "duplicate": False, "registered": False}
    if not getattr(config, "TR_N8N_ENABLED", True):
        return guard
    try:
        from core.task_runtime import get_task_runtime
        rt = get_task_runtime()
        actor = str(body.get("actor") or CHANNEL)
        session_id = f"{CHANNEL}:{guard['action']}"
        key = client_key(body)
        tid, created, fenc, own = await rt.submit_for_chat(
            actor=actor, session_id=session_id, client_key=key,
            message=_message(body), target=TARGET,
            meta={"action": guard["action"]}, channel=CHANNEL)
        prior = await rt.get_task(tid) or {}
        if not created and str(prior.get("status") or "") == "succeeded":
            guard.update({"runtime": rt, "task_id": tid, "duplicate": True,
                          "registered": True})
            logger.info(f"[entry_guard] 回调重复 (前次已成功), 跳过副作用: {tid}")
            return guard
        if not created:
            # 失败/UNKNOWN/在途 → 作为新一次尝试登记 (不覆盖历史终态)
            tid, created, fenc, own = await rt.submit_for_chat(
                actor=actor, session_id=session_id,
                client_key=f"{key}#retry-{int(time.time())}",
                message=_message(body), target=TARGET,
                meta={"action": guard["action"], "retry_of_status": prior.get("status")},
                channel=CHANNEL)
        guard.update({"runtime": rt, "task_id": tid, "owner": own,
                      "fencing": fenc, "registered": True})
        await rt.emit(tid, own, fenc, "received", {"action": guard["action"]})
    except Exception as e:
        logger.warning(f"[entry_guard] 回调登记失败 (降级不登记, 不阻断处理): {e}")
        guard.update({"runtime": None, "task_id": "", "duplicate": False,
                      "registered": False})
    return guard


async def finish(guard: dict, terminal: str, result: Optional[dict] = None) -> None:
    """提交回调处理终态 (重复判定/未登记时静默跳过)"""
    if not guard or not guard.get("task_id") or guard.get("duplicate"):
        return
    try:
        await guard["runtime"].complete(guard["task_id"], guard["owner"],
                                        guard["fencing"], terminal, result or {})
    except Exception as e:
        logger.debug(f"[entry_guard] 终态提交异常 {guard['task_id']}: {e}")


async def lookup(guard: dict) -> dict:
    """读取当前任务账 (测试/排障用; 未登记返回空)"""
    if not guard or not guard.get("task_id"):
        return {}
    return await guard["runtime"].get_task(guard["task_id"]) or {}
