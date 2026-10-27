"""
Z-OPS v4.0 Query Checkpoint — Tool-call Loop 在途状态快照
借鉴 LangGraph durable execution / claude-code session transcript 思想:
- QueryEngine 每轮循环结束把在途状态写入 Redis (TTL 保护)
- 进程崩溃/重启后，Agent 启动时扫描残留 checkpoint → 通知用户执行中断
- Phase 4 审批门复用同一底座: destructive 工具挂起时保存现场，批准后恢复

Redis 结构: {REDIS_PREFIX}:qe:ckpt:{msg_id} -> hash {
    status, turn, agent, session_id, pid, ts,
    messages_tail (JSON, 最近4条), usage (JSON)
}
"""

import os
import json
import time
import logging
from typing import Dict, List, Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops.checkpoint")

_sync_redis = None


def _get_redis():
    """惰性同步 Redis 客户端 (checkpoint 读写均为小对象，毫秒级)"""
    global _sync_redis
    if _sync_redis is None:
        import redis
        _sync_redis = redis.from_url(config.REDIS_URL, decode_responses=True)
    return _sync_redis


def _key(msg_id: str) -> str:
    return f"{config.REDIS_PREFIX}:qe:ckpt:{msg_id}"


def save_query_checkpoint(msg_id: str, turn: int, messages_tail: list,
                          usage: dict, agent: str = "", session_id: str = "") -> None:
    """保存/续写一轮 checkpoint (同 msg_id 覆盖更新)"""
    try:
        r = _get_redis()
        r.hset(_key(msg_id), mapping={
            "status": "in_progress",
            "turn": turn,
            "agent": agent,
            "session_id": session_id,
            "pid": os.getpid(),
            "ts": time.time(),
            "messages_tail": json.dumps(messages_tail, ensure_ascii=False)[:20000],
            "usage": json.dumps(usage, ensure_ascii=False)[:2000],
        })
        r.expire(_key(msg_id), config.QE_CHECKPOINT_TTL)
    except Exception as e:
        logger.debug(f"[checkpoint] 保存失败 {msg_id}: {e}")


def mark_query_done(msg_id: str) -> None:
    """查询正常/终态结束 → 清除 checkpoint (避免被误报为中断)"""
    try:
        _get_redis().delete(_key(msg_id))
    except Exception as e:
        logger.debug(f"[checkpoint] 清除失败 {msg_id}: {e}")


def get_checkpoint(msg_id: str) -> Optional[dict]:
    """读取 checkpoint (审批恢复用)"""
    try:
        data = _get_redis().hgetall(_key(msg_id))
        return data if data else None
    except Exception as e:
        logger.debug(f"[checkpoint] 读取失败 {msg_id}: {e}")
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def scan_orphan_checkpoints() -> List[dict]:
    """扫描孤儿 checkpoint: 属主进程已死且仍 in_progress 的在途任务

    供 Agent 启动时调用 → 通知用户「执行因进程重启中断」并清理。
    """
    orphans: List[dict] = []
    try:
        r = _get_redis()
        pattern = f"{config.REDIS_PREFIX}:qe:ckpt:*"
        for key in r.scan_iter(match=pattern, count=100):
            try:
                data = r.hgetall(key)
                if not data or data.get("status") != "in_progress":
                    continue
                pid = int(data.get("pid", 0) or 0)
                if pid and pid != os.getpid() and _pid_alive(pid):
                    continue  # 属主进程仍存活，非孤儿
                msg_id = key.rsplit(":", 1)[-1]
                data["msg_id"] = msg_id
                orphans.append(data)
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"[checkpoint] 扫描失败: {e}")
    return orphans


def clear_checkpoint(msg_id: str) -> None:
    mark_query_done(msg_id)


def report_and_clear_orphans(agent_name: str, notify_fn=None) -> List[dict]:
    """报告并清理属于指定 agent 的孤儿 checkpoint

    Args:
        agent_name: 仅处理 agent 字段匹配 (或为空) 的 checkpoint
        notify_fn: 可选异步通知函数 async (text) -> None
    Returns:
        已处理的孤儿列表 (同步调用方用)
    """
    handled = []
    for ck in scan_orphan_checkpoints():
        ck_agent = ck.get("agent", "")
        if ck_agent and ck_agent != agent_name:
            continue
        turn = ck.get("turn", "?")
        msg_id = ck.get("msg_id", "?")
        text = (f"[{agent_name}] 检测到中断的任务: 消息 {msg_id} 在第 {turn} 轮 "
                f"因进程重启中断 (session={ck.get('session_id') or '-'})。"
                f"历史工具结果已保留在会话记录中，可重新发起请求。")
        logger.warning(f"[checkpoint] {text}")
        if notify_fn is not None:
            try:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(notify_fn(text))
                except RuntimeError:
                    asyncio.run(notify_fn(text))
            except Exception as e:
                logger.debug(f"[checkpoint] 通知发送失败: {e}")
        clear_checkpoint(msg_id)
        handled.append(ck)
    return handled
