"""Z-OPS /api 会话管理 域路由 (v4.0 Phase 6: 从 webchat_api.py 拆分, 行为不变)

端点逻辑原样搬迁 (@app → @router); 共享依赖经 from webchat_api import * 获取 —
webchat_api.py 在文件底部 include_router, 彼时全部共享名已定义, 无循环导入问题。
"""
from webchat_api import *  # noqa: F401,F403  (config/logger/get_redis/verify_token/...)
from webchat_api import (_emit_daily_log)  # noqa: F401  本域引用的主文件私有辅助
from fastapi import APIRouter

router = APIRouter()



# ═══ v4.0 Phase 3.3: 会话 resume 上下文 (Redis 过期后从 transcript jsonl 重建) ═══

@router.get("/api/sessions/{session_id}/resume_context")
async def session_resume_context(session_id: str, current_user: str = Depends(verify_token)):
    """重开会话完整上下文还原: Redis 优先, 过期/丢失时从 logs/sessions/{id}.jsonl 重放

    (jsonl 永久保留, 彻底规避 session meta TTL 导致会话内容丢失类事故)
    P0: 归属校验 — 无法证明归属的历史会话隔离 (此前任意用户可凭会话 ID 读全文)
    """
    from core import transcript as _transcript
    from core.session_access import ensure_session_access
    r = await get_redis()
    await ensure_session_access(r, session_id, current_user, action="restore")
    return await _transcript.resume_context(session_id, redis_client=r)

# ═══ 会话管理 ═══
from core.session_access import ensure_session_access  # P0: 共用归属校验


@router.post("/api/chat/sessions")
async def create_session(request: Request, current_user: str = Depends(verify_token)):
    """创建新会话"""
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    title = body.get("title", "新对话")
    r = await get_redis()
    session_id = str(uuid.uuid4())[:12]
    now = time.time()
    meta = {
        "session_id": session_id,
        "title": title,
        "username": current_user,
        "created_at": str(now),
        "updated_at": str(now),
        "msg_count": "0"
    }
    await r.hset(config.key_session_meta(session_id), mapping=meta)
    if config.SESSION_TTL > 0:
        await r.expire(config.key_session_meta(session_id), config.SESSION_TTL)
    await r.zadd(config.key_user_sessions(current_user), {session_id: now})
    return {"session_id": session_id, "title": title}

@router.get("/api/chat/sessions")
async def list_sessions(current_user: str = Depends(verify_token)):
    """列出当前用户的所有会话"""
    r = await get_redis()
    session_ids = await r.zrevrange(config.key_user_sessions(current_user), 0, -1)
    sessions = []
    for sid in session_ids:
        meta = await r.hgetall(config.key_session_meta(sid))
        if not meta:
            # meta 过期/丢失但消息还在 → 从消息重建 meta，避免会话被误清理 (历史丢失根因修复)
            msg_count = await r.llen(config.key_session_msgs(sid))
            if msg_count > 0:
                first_raw = await r.lindex(config.key_session_msgs(sid), 0)
                first_ts = time.time()
                first_text = "未命名"
                try:
                    first = json.loads(first_raw or "{}")
                    first_ts = float(first.get("timestamp") or first_ts)
                    first_text = (first.get("content") or "未命名")[:30]
                except Exception:
                    pass
                meta = {
                    "session_id": sid,
                    "title": first_text,
                    "username": current_user,
                    "created_at": str(first_ts),
                    "updated_at": str(time.time()),
                    "msg_count": str(msg_count),
                }
                await r.hset(config.key_session_meta(sid), mapping=meta)
                if config.SESSION_TTL > 0:
                    await r.expire(config.key_session_meta(sid), config.SESSION_TTL)
                logger.info(f"[sessions] 重建过期会话 meta: {sid[:8]} user={current_user} msgs={msg_count}")
            else:
                await r.zrem(config.key_user_sessions(current_user), sid)
                continue
        sessions.append({
            "session_id": sid,
            "title": meta.get("title", "未命名"),
            "msg_count": int(meta.get("msg_count", 0)),
            "created_at": float(meta.get("created_at", 0)),
            "updated_at": float(meta.get("updated_at", 0)),
        })
    return {"sessions": sessions, "count": len(sessions)}

@router.delete("/api/chat/sessions/{session_id}")
async def delete_session(session_id: str, current_user: str = Depends(verify_token)):
    """删除会话"""
    r = await get_redis()
    await ensure_session_access(r, session_id, current_user, action="删除")
    await r.delete(config.key_session_meta(session_id))
    await r.delete(config.key_session_msgs(session_id))
    await r.zrem(config.key_user_sessions(current_user), session_id)
    await _emit_daily_log("system", f"{current_user} 删除会话 {session_id[:8]}", level="info", category="session", session_id=session_id, username=current_user)
    return {"status": "ok"}

@router.put("/api/chat/sessions/{session_id}")
async def update_session(session_id: str, request: Request, current_user: str = Depends(verify_token)):
    """更新会话标题"""
    body = await request.json()
    title = body.get("title", "")
    if not title:
        raise HTTPException(400, "缺少 title")
    r = await get_redis()
    await ensure_session_access(r, session_id, current_user, action="修改")
    await r.hset(config.key_session_meta(session_id), "title", title)
    return {"status": "ok"}

@router.get("/api/chat/messages/{session_id}")
async def get_session_messages(session_id: str, current_user: str = Depends(verify_token)):
    """获取会话的所有消息"""
    r = await get_redis()
    meta = await r.hgetall(config.key_session_meta(session_id))
    if not meta:
        # meta 过期/丢失: 归属由 transcript 兜底判定 (P0), 无法证明则 403
        await ensure_session_access(r, session_id, current_user, action="读取")
    elif str(meta.get("username") or "").strip() != (current_user or "").strip():
        raise HTTPException(403, "无权查看此会话")
    raw_list = await r.lrange(config.key_session_msgs(session_id), 0, -1)
    messages = []
    for raw in raw_list:
        try:
            messages.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return {"session_id": session_id, "title": meta.get("title", ""), "messages": messages}


@router.get("/api/chat/sessions/{session_id}/pending")
async def get_session_pending(session_id: str, current_user: str = Depends(verify_token)):
    """会话待回复判定 (权威): 该会话最新任务运行时状态。

    前端恢复对话前先问这里: failed → 直接展示失败结论, active → 才轮询恢复,
    不再按"末条是用户消息"猜测 (10-08 事故的死消息曾致 UI 常驻"正在恢复回复")。
    """
    r = await get_redis()
    meta = await r.hgetall(config.key_session_meta(session_id))
    if not meta:
        await ensure_session_access(r, session_id, current_user, action="读取")
    elif str(meta.get("username") or "").strip() != (current_user or "").strip():
        raise HTTPException(403, "无权查看此会话")

    from core.task_runtime import get_task_runtime
    runtime = get_task_runtime()
    latest_ts, latest_id = -1.0, ""
    try:
        async for k in r.scan_iter(match=f"{config.REDIS_PREFIX}:tr:task:*", count=100):
            tid = k.split(":", 3)[-1] if isinstance(k, str) else k.decode().split(":", 3)[-1]
            t = await runtime.get_task(tid)
            if not t or (t.get("session_id") or "") != session_id:
                continue
            try:
                ts = float(t.get("created_ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts > latest_ts:
                latest_ts, latest_id = ts, tid
    except Exception as e:
        logger.warning(f"[pending] 任务扫描失败 session={session_id}: {e}")
        return {"state": "none"}
    if not latest_id:
        return {"state": "none"}

    t = await runtime.get_task(latest_id)
    status = str(t.get("status") or "")
    if status == "succeeded":
        return {"state": "succeeded", "task_id": latest_id}
    if status in ("failed", "cancelled", "unknown"):
        err = str(t.get("error") or "").strip()
        if not err:
            try:
                for ev in reversed(await runtime.read_events_after(latest_id, 0)):
                    cand = ev
                    for key in ("data", "payload"):
                        p = ev.get(key)
                        if isinstance(p, str):
                            try:
                                p = json.loads(p)
                            except Exception:
                                p = None
                        if isinstance(p, dict):
                            cand = p
                            break
                    if isinstance(cand, dict) and cand.get("error"):
                        err = str(cand["error"])
                        break
            except Exception:
                pass
        label = {"failed": "回复失败", "cancelled": "回复已取消",
                 "unknown": "任务状态未知（服务中断）"}[status]
        return {"state": "failed", "status": status, "error": err or label, "task_id": latest_id}
    # running / pending 等 → 任务仍在执行
    return {"state": "active", "status": status, "task_id": latest_id}


@router.get("/api/chat/stats/{session_id}")
async def get_session_stats(session_id: str, current_user: str = Depends(verify_token)):
    """获取会话压缩统计信息 (参考 claude-code cost-tracker)"""
    r = await get_redis()
    meta = await r.hgetall(config.key_session_meta(session_id))
    if not meta:
        await ensure_session_access(r, session_id, current_user, action="读取统计")
    elif str(meta.get("username") or "").strip() != (current_user or "").strip():
        raise HTTPException(403, "无权查看此会话")
    engine = get_session_engine()
    stats = await engine.get_session_stats(session_id)
    return stats
