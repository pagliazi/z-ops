"""
Z-OPS v4.0 Phase 3.3 — 会话 transcript 持久化与 resume (借鉴 claude-code §9)

append-only JSONL transcript:
    logs/sessions/{session_id}.jsonl — 每行一个事件, 永久保留 (审计红线: 永不删除/改写)

事件类型:
    {"type": "message", "role": "user"|"assistant", "content": ..., "msg_id": ..., ...}
    {"type": "compact_boundary", "anchor": "<uuid>", "summary": "...",
     "replaced_count": n}   — auto-compact 替换 Redis 历史时写入

读时修补 (存储永不改写):
    rebuild_messages() 重放 jsonl — compact_boundary 之前的 message 折叠为一条
    summary, boundary 之后的消息原样保留; Redis 过期/丢失时从 jsonl 完整还原。

双写接入点 (Redis 保持现有实时查询用途, jsonl 为永久事实源):
    webchat_api.py 用户消息 rpush 处 / assistant 消息 rpush 处
    session_engine.auto_compact_if_needed 替换 Redis 列表处
"""

import json
import os
import uuid
import time
import asyncio
import logging
from pathlib import Path
from typing import List, Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

# 会话 transcript 目录 (与 logs/audit 同级审计资产, 永久保留)
TRANSCRIPT_DIR = Path(__file__).resolve().parent.parent / "logs" / "sessions"

_write_locks: dict = {}


def _session_path(session_id: str) -> Path:
    # 会话 ID 参与文件名 — 过滤路径穿越字符
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_.")
    return TRANSCRIPT_DIR / f"{safe}.jsonl"


def _get_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _write_locks:
        _write_locks[session_id] = asyncio.Lock()
    return _write_locks[session_id]


async def append_event(session_id: str, event: dict) -> bool:
    """append-only 追加一条事件到 transcript (失败仅告警, 不阻断主流程)"""
    try:
        event = {"ts": time.time(), **event}
        line = json.dumps(event, ensure_ascii=False)
        path = _session_path(session_id)
        async with _get_lock(session_id):
            def _write():
                TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            await asyncio.get_running_loop().run_in_executor(None, _write)
        return True
    except Exception as e:
        logger.warning(f"[transcript] 写入失败 session={session_id}: {e}")
        return False


async def append_message(session_id: str, role: str, content: str, **extra) -> bool:
    """消息事件快捷入口 (与 Redis 双写对齐)"""
    return await append_event(session_id, {
        "type": "message", "role": role, "content": content or "", **extra,
    })


async def append_compact_boundary(session_id: str, summary: str,
                                  replaced_count: int) -> str:
    """auto-compact 边界事件 (anchor UUID 供加载侧读时修补)"""
    anchor = uuid.uuid4().hex
    await append_event(session_id, {
        "type": "compact_boundary", "anchor": anchor,
        "summary": (summary or "")[:4000], "replaced_count": replaced_count,
    })
    return anchor


def load_transcript(session_id: str) -> List[dict]:
    """读取全部事件 (原始顺序; 损坏行跳过并告警)"""
    path = _session_path(session_id)
    events = []
    if not path.exists():
        return events
    try:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(f"[transcript] session={session_id} 第{i}行损坏, 跳过")
    except OSError as e:
        logger.warning(f"[transcript] 读取失败 session={session_id}: {e}")
    return events


def get_session_owner(session_id: str) -> str:
    """P0: 会话归属回查 — 取 transcript 中第一条带 username 的用户消息的归属

    Redis meta 过期/丢失时用于 ownership 校验兜底 (fail-closed):
    查不到归属返回空串, 调用方必须按不可证明归属拒绝, 不得默认放行。
    """
    try:
        for ev in load_transcript(session_id):
            if ev.get("type") == "message" and ev.get("role") == "user":
                u = str(ev.get("username") or "").strip()
                if u:
                    return u
    except Exception as e:
        logger.warning(f"[transcript] owner 回查失败 session={session_id}: {e}")
    return ""


def rebuild_messages(session_id: str, max_count: int = 0) -> List[dict]:
    """从 jsonl 重放构造消息列表 (读时修补, 存储不动)

    规则: 最后一个 compact_boundary 之前的 message 折叠为一条 summary 消息,
    boundary 之后的消息原样保留; 无 boundary 则全量。
    """
    events = load_transcript(session_id)
    last_boundary = -1
    summary = ""
    for i, ev in enumerate(events):
        if ev.get("type") == "compact_boundary":
            last_boundary = i
            summary = ev.get("summary", "")

    messages: List[dict] = []
    if last_boundary >= 0 and summary:
        messages.append({"role": "assistant", "content": summary,
                         "source": "compact_summary"})
    for ev in events[last_boundary + 1:]:
        if ev.get("type") == "message" and ev.get("content"):
            messages.append({"role": ev.get("role", "user"),
                             "content": ev.get("content", ""),
                             "source": ev.get("source", ""),
                             "timestamp": ev.get("ts")})
    if max_count > 0:
        messages = messages[-max_count:]
    return messages


def transcript_stats(session_id: str) -> dict:
    """transcript 统计 (行数/大小/边界数) — 管理面板用"""
    events = load_transcript(session_id)
    path = _session_path(session_id)
    return {
        "session_id": session_id,
        "exists": path.exists(),
        "events": len(events),
        "messages": sum(1 for e in events if e.get("type") == "message"),
        "compact_boundaries": sum(1 for e in events if e.get("type") == "compact_boundary"),
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }


async def resume_context(session_id: str, redis_client=None,
                         max_count: int = 0) -> dict:
    """resume 上下文: Redis 优先, 过期/缺失时从 jsonl 重建 (GET /api/sessions/{id}/resume_context)

    Returns:
        {session_id, source: "redis"|"jsonl"|"empty", messages: [...], stats: {...}}
    """
    source = "empty"
    messages: List[dict] = []
    try:
        if redis_client is None:
            import redis as _redis
            redis_client = _redis.from_url(config.REDIS_URL, decode_responses=True)
        raw_list = await redis_client.lrange(config.key_session_msgs(session_id), 0, -1) \
            if hasattr(redis_client, "lrange") and asyncio.iscoroutinefunction(redis_client.lrange) \
            else []
        if raw_list:
            for raw in raw_list:
                try:
                    messages.append(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    continue
            source = "redis"
    except Exception as e:
        logger.info(f"[transcript] Redis 读取失败, 降级 jsonl: {e}")

    if source == "empty":
        messages = rebuild_messages(session_id, max_count)
        if messages:
            source = "jsonl"

    return {"session_id": session_id, "source": source,
            "messages": messages, "stats": transcript_stats(session_id)}
