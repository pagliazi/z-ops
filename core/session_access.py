"""Z-OPS P0 — 会话归属校验 (ownership)

SPEC P0「请求隔离与会话访问」:
  chat.py / sessions.py 的读取、追加、restore、events、stop 共用 ownership 校验;
  无法证明归属的历史会话隔离 (fail-closed, 不默认放行)。

判定链 (按序):
  1. Redis session meta 的 username (权威, 会话创建时写入);
  2. transcript jsonl 第一条用户消息的 username (meta 过期/丢失时的永久事实兜底);
  3. 都拿不到 → 拒绝 (403)。隐藏/撤销会话的墓碑语义由调用方叠加, 原始审计不删除。
"""

import logging

from fastapi import HTTPException

import config

logger = logging.getLogger("zops")


async def get_session_owner(r, session_id: str) -> str:
    """会话归属: Redis meta 优先, transcript 兜底; 无法证明返回空串"""
    owner = ""
    try:
        meta = await r.hgetall(config.key_session_meta(session_id))
        owner = str(meta.get("username") or "").strip() if meta else ""
    except Exception as e:
        logger.warning(f"[session_access] meta 读取失败 session={session_id}: {e}")
    if owner:
        return owner
    try:
        from core import transcript as _transcript
        return _transcript.get_session_owner(session_id)
    except Exception as e:
        logger.warning(f"[session_access] transcript owner 回查失败 session={session_id}: {e}")
        return ""


async def ensure_session_access(r, session_id: str, current_user: str,
                                action: str = "读取") -> str:
    """校验 current_user 对 session_id 的归属, 不通过抛 403

    Returns: 会话归属 (owner username)
    """
    if not session_id:
        raise HTTPException(400, "缺少 session_id")
    current_user = (current_user or "").strip()
    if not current_user:
        # 认证依赖已保证有主体; 防御性兜底
        raise HTTPException(403, "无法确认当前用户, 拒绝访问会话")
    owner = await get_session_owner(r, session_id)
    if owner != current_user:
        logger.warning(
            f"[session_access] 会话归属校验拒绝: user={current_user} session={session_id[:8]} "
            f"owner={owner or '(无法证明归属)'} action={action}")
        raise HTTPException(403, "无权访问此会话" if owner else "会话不存在或归属无法证明")
    return owner
