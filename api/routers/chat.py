"""Z-OPS /api 对话主链 域路由 (v4.0 Phase 6: 从 webchat_api.py 拆分, 行为不变)

端点逻辑原样搬迁 (@app → @router); 共享依赖经 from webchat_api import * 获取 —
webchat_api.py 在文件底部 include_router, 彼时全部共享名已定义, 无循环导入问题。
"""
from webchat_api import *  # noqa: F401,F403  (config/logger/get_redis/verify_token/...)
from webchat_api import (_emit_daily_log, _log_user_op)  # noqa: F401  本域引用的主文件私有辅助
from fastapi import APIRouter

router = APIRouter()



# ═══ AI 对话 (SSE 流式) ═══
@router.post("/api/chat")
async def chat(request: Request, current_user: str = Depends(verify_token)):
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "")
    client_session_id = session_id   # P1: 幂等键绑定客户端提供的会话标识 (自动建会话的随机 id 不参与)
    target = body.get("target", "manager")

    if not message:
        raise HTTPException(400, "缺少 message")

    r = await get_redis()

    # 自动创建会话
    if not session_id:
        session_id = str(uuid.uuid4())[:12]
        now = time.time()
        meta = {
            "session_id": session_id,
            "title": message[:30],
            "username": current_user,
            "created_at": str(now),
            "updated_at": str(now),
            "msg_count": "0"
        }
        await r.hset(config.key_session_meta(session_id), mapping=meta)
        if config.SESSION_TTL > 0:
            await r.expire(config.key_session_meta(session_id), config.SESSION_TTL)
        await r.zadd(config.key_user_sessions(current_user), {session_id: now})
    else:
        # P0: 既有会话归属校验 — 任意用户向他人 session_id 追加消息会污染他人历史
        # (此前无校验; meta 过期时由 transcript 首条用户消息兜底, 均无法证明则 403)
        from core.session_access import ensure_session_access
        await ensure_session_access(r, session_id, current_user, action="追加消息")

    # 保存用户消息
    user_msg = {
        "role": "user",
        "content": message,
        "timestamp": time.time(),
        "session_id": session_id
    }
    await r.rpush(config.key_session_msgs(session_id), json.dumps(user_msg, ensure_ascii=False))
    # SessionSearch FTS5 索引 (Hermes): 用户消息同步索引
    try:
        from core.memory.session_search import get_session_search, Message as SSMsg
        _ss = await get_session_search()
        await _ss.index_session(
            session_id=session_id, user_id=current_user, agent_id="webchat",
            messages=[SSMsg(role="user", content=message, timestamp=str(int(time.time())))],
            summary=message[:80])
    except Exception as _fts_err:
        logger.debug(f"[chat] FTS5 索引失败 (不阻断): {_fts_err}")
    # v4.0 (Phase 3.3): transcript jsonl 双写 — 永久保留, Redis 过期后可完整还原
    try:
        from core import transcript as _transcript
        await _transcript.append_message(session_id, "user", message,
                                         username=current_user)
    except Exception:
        pass
    if config.SESSION_MSG_MAX > 0:
        await r.ltrim(config.key_session_msgs(session_id), -config.SESSION_MSG_MAX, -1)
    if config.SESSION_TTL > 0:
        await r.expire(config.key_session_msgs(session_id), config.SESSION_TTL)
    await r.hset(config.key_session_meta(session_id), "updated_at", str(time.time()))
    await r.hincrby(config.key_session_meta(session_id), "msg_count", 1)
    # 自动生成会话标题: 前端先 createSession 再带 session_id 发消息, 后端需在标题仍为默认值时
    # 用首条消息命名 (否则标题永远是 "新对话")
    try:
        _meta = await r.hgetall(config.key_session_meta(session_id))
        if not _meta or not _meta.get("title") or _meta.get("title") in ("新对话", "未命名"):
            clean_title = " ".join(message.split())[:30]
            if clean_title:
                await r.hset(config.key_session_meta(session_id), "title", clean_title)
    except Exception as _e:
        logger.debug(f"[会话标题] 自动命名失败: {_e}")
    # 会话永久保留 (SESSION_TTL=0)；若配置了 TTL 则随活跃度刷新，防止会话从列表消失
    if config.SESSION_TTL > 0:
        await r.expire(config.key_session_meta(session_id), config.SESSION_TTL)

    # jieba 关键词索引: 提取用户消息关键词并缓存到 Redis
    try:
        from core.keyword_index import update_session_keywords
        await update_session_keywords(r, session_id, message, role="user")
    except Exception as e:
        logger.debug(f"[关键词索引] 提取失败: {e}")

    # 加载会话历史 (via SessionEngine, 参考 claude-code compact 系统)
    session_engine = get_session_engine()
    context_history = []
    try:
        context_history = await session_engine.build_context_for_routing(
            session_id, current_text=message
        )
    except Exception as e:
        logger.warning(f"[加载会话历史失败] {e}")

    # 构造消息 (支持指定 agent)
    target_agent = body.get("target_agent", "")  # 前端指定的 agent
    route_target = target_agent if target_agent else "orchestrator"
    # 续跑路由 (chat harness): 本会话存在挂起的结构化问询 → 用户回复直达发起问询的 agent。
    # zops:ask:pending (通用, 带 agent 归属) 优先; 兼容旧 zops:cu:pending (归属 computer_agent)。
    # 否则像 "svc-gcoc" 这类裸回复会被关键词路由到无关 agent (10-21 "收到指令编号"事故)
    if not target_agent:
        try:
            _pend_raw = await r.get(f"{config.REDIS_PREFIX}:ask:pending:{session_id}")
            if _pend_raw:
                _pend = json.loads(_pend_raw)
                route_target = _pend.get("agent") or "orchestrator"
            elif await r.exists(f"{config.REDIS_PREFIX}:cu:pending:{session_id}"):
                route_target = "computer_agent"
            if route_target not in ("orchestrator",):
                await _emit_daily_log("system", f"ask 续跑路由: 会话 {session_id[:8]} → {route_target}",
                                      level="info", category="chat", session_id=session_id, username=current_user)
        except Exception:
            pass
    # 快速失败: 目标 agent 心跳陈旧 (agent 群死亡/重启窗口) 时立即拒绝,
    # 不让前端空等 REPLY_TIMEOUT (10-08 事故期间消息石沉大海且 UI 常驻"思考中")。
    # 阈值 120s 远大于心跳间隔 (~30s), 正常抖动不会误伤; 心跳由 BaseAgent 持续续写。
    try:
        _hb_raw = await r.hget(config.KEY_HEARTBEATS, route_target)
        _hb_ts = float(json.loads(_hb_raw).get("last_heartbeat", 0)) if _hb_raw else 0.0
    except Exception:
        _hb_ts = 0.0
    if time.time() - _hb_ts > 120:
        await _emit_daily_log("system", f"chat 快速失败: {route_target} 心跳超时 {int(time.time() - _hb_ts)}s",
                              level="error", category="chat", session_id=session_id, username=current_user)
        raise HTTPException(status_code=503,
                            detail=f"目标服务 {route_target} 当前不在线（心跳超时），消息未被处理，请稍后重试")
    # 插件缝 (dsh agent/pre-step 语义): 策略插件可拒绝任务入队 (bail: {"deny": true, "reason": "..."})
    try:
        from core.plugin_system import get_event_bus
        _bailed, _reason = await get_event_bus().bail(
            "chat/pre_task", {"actor": current_user, "route_target": route_target,
                              "message_len": len(message or ""), "session_id": session_id})
        if _bailed and isinstance(_reason, dict) and _reason.get("deny"):
            raise HTTPException(status_code=403,
                                detail=_reason.get("reason") or "插件策略拒绝该请求")
    except HTTPException:
        raise
    except Exception as _pe:
        logger.debug(f"[chat] pre_task 插件缝异常 (放行): {_pe}")
    await _emit_daily_log("system", f"{current_user} 提问: {message[:80]}", level="info", category="chat", session_id=session_id, username=current_user)
    # 用户查询审计 (登录/查询/配置变更统一在 user_ops:{date}; 管理后台审计页可查询)
    try:
        client_ip = request.client.host if request.client else "unknown"
        # 消息体已由上游存入会话, 这里只留痕摘要 (≤120 字), 避免长文本膨胀审计文件
        await _log_user_op(current_user, "query", f"提问 ({len(message)}字): {message[:120]}", ip=client_ip, session_id=session_id)
    except Exception as _e:
        logger.debug(f"[chat] 查询审计写入失败: {_e}")
    msg = AgentMessage(
        source="webchat",
        target=route_target,
        text=message,
        channel_source="webchat",
        session_id=session_id,
        username=current_user,
        action=body.get("action", ""),
        params={"history": context_history} if context_history else {},
    )

    # 全局聊天历史 (向后兼容)
    history_entry = json.dumps({
        "role": "user", "content": message, "channel": "webchat",
        "user": current_user, "msg_id": msg.msg_id,
        "session_id": session_id, "timestamp": time.time()
    }, ensure_ascii=False)
    await r.rpush(config.KEY_CHAT_HISTORY, history_entry)
    if config.CHAT_HISTORY_MAX > 0:
        await r.ltrim(config.KEY_CHAT_HISTORY, -config.CHAT_HISTORY_MAX, -1)

    reply_ch = config.reply_channel(msg.msg_id)

    # ═══ P1: 任务登记 (幂等) — 可靠登记成功后才向流式层报告接收 ═══
    from core.task_runtime import get_task_runtime, IdempotencyConflict
    runtime = get_task_runtime()
    # 旧客户端未提供幂等键则生成并返回 (不承诺识别该类未知重复 POST)
    idem_key = (request.headers.get("Idempotency-Key") or "").strip() or str(uuid.uuid4())
    # P1 GCOC 试点: readiness 达标 + 开关开启时, gcoc_agent (多对象只读) 走 Streams 队列消费
    use_streams = (route_target == "gcoc_agent"
                   and await runtime.streams_enabled())
    try:
        if use_streams:
            task_id, created = await runtime.submit_stream(
                actor=current_user, session_id=(client_session_id or session_id),
                client_key=idem_key, message=message, target=route_target)
            task_fencing, task_owner = 0, ""   # 执行方为消费者, 协调者不持租约
        else:
            task_id, created, task_fencing, task_owner = await runtime.submit_for_chat(
                actor=current_user, session_id=(client_session_id or session_id), client_key=idem_key,
                message=message, target=route_target,
                meta={"msg_id": msg.msg_id})
    except IdempotencyConflict as e:
        raise HTTPException(409, f"Idempotency-Key 已绑定不同内容: {e}")
    if created and not use_streams:
        await runtime.ledger.link_msg(task_id, msg.msg_id)

    async def _task_emit(ev_type: str, data: dict, archive_now: bool = False) -> int:
        """P1: 记录任务事件 (单一 writer = _persist_reply; 失租时静默降级)。
        Returns: 事件 seq (失败返回 -1)"""
        try:
            return await runtime.emit(task_id, task_owner, task_fencing, ev_type, data,
                                      archive_now=archive_now)
        except Exception as e:
            logger.debug(f"[chat] 任务事件记录失败 {task_id}/{ev_type}: {e}")
            return -1

    async def _task_heartbeat():
        """P1: lease 心跳 (12s < 默认 15s); 失租后退出 (旧 worker 禁止提交终态)。
        由 _persist_reply 持有 — 客户端断流 (SSE 结束) 不影响心跳与终态记录。"""
        while True:
            await asyncio.sleep(12)
            if not await runtime.heartbeat(task_id, task_owner, task_fencing):
                return

    async def _replay_and_follow():
        """P1 幂等重放: 同键同内容重复 POST → 重放已记录事件并跟随至终态, 不重新提交任务"""
        import time as _time
        cursor = 0
        idle = 0
        while True:
            try:
                task = await runtime.get_task(task_id)
                events = await runtime.read_events_after(task_id, cursor)
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'text': f'任务事件读取失败: {e}'}, ensure_ascii=False)}\n\n"
                break
            for ev in events:
                cursor = max(cursor, int(ev.get("seq") or 0))
                t = ev.get("type")
                d = ev.get("data") or {}
                if t == "task_started":
                    yield f"data: {json.dumps({'type': 'start', 'msg_id': d.get('msg_id', ''), 'session_id': session_id, 'task_id': task_id, 'idempotency_key': idem_key, 'replay': True}, ensure_ascii=False)}\n\n"
                elif t == "progress":
                    yield f"data: {json.dumps({'type': 'progress', 'text': d.get('text', ''), 'source': d.get('source', '')}, ensure_ascii=False)}\n\n"
                elif t == "delta":
                    yield f"data: {json.dumps({'type': 'delta', 'text': d.get('text', ''), 'source': d.get('source', ''), 'kind': d.get('kind', 'content'), 'event_id': ev.get('event_id')}, ensure_ascii=False)}\n\n"
                elif t == "reply":
                    yield f"data: {json.dumps({'type': 'reply', 'text': d.get('text', ''), 'source': d.get('source', ''), 'session_id': session_id, 'task_id': task_id, 'event_id': ev.get('event_id')}, ensure_ascii=False)}\n\n"
                elif t == "task_terminal":
                    if d.get("error"):
                        yield f"data: {json.dumps({'type': 'error', 'text': d.get('error')}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'task_id': task_id, 'replay': True}, ensure_ascii=False)}\n\n"
                    return
            status = (task or {}).get("status")
            if status in ("succeeded", "failed", "cancelled", "unknown"):
                yield f"data: {json.dumps({'type': 'done', 'task_id': task_id, 'replay': True}, ensure_ascii=False)}\n\n"
                return
            idle += 1
            if idle % 8 == 0:
                yield ": ping\n\n"
            await asyncio.sleep(min(1.0 * (1 + idle // 30), 3.0))

    async def event_stream():
        # P1: Streams 任务 (消费者执行) 与重复 POST 同走事件跟随 — 单一执行方, 不重复提交
        if use_streams or not created:
            async for chunk in _replay_and_follow():
                yield chunk
            return
        pubsub = r.pubsub()
        await pubsub.subscribe(reply_ch)
        orch_ch = config.channel("orchestrator")
        await r.publish(orch_ch, msg.to_json())
        yield f"data: {json.dumps({'type': 'start', 'msg_id': msg.msg_id, 'session_id': session_id, 'task_id': task_id, 'idempotency_key': idem_key, 'protocol_ver': 1}, ensure_ascii=False)}\n\n"

        # ─── 回复落库任务 (独立于 SSE 连接生命周期) ───
        # 用户对话中途切页/刷新/网络闪断会终止 SSE 生成器，但 agent 仍在处理；
        # 本任务用独立 pubsub 连接持续等待最终回复并落库，保证用户切回页面时
        # 能从会话历史加载到结果 (否则回复会永久丢失)。
        async def _persist_reply():
            p = r.pubsub()
            # P1: 心跳与事件记录的单一 owner — 独立于 SSE 连接生命周期 (断线不取消)
            hb_task = asyncio.create_task(_task_heartbeat())

            async def _timeout_terminal():
                # P1: 超时终态 (failed; 断线不算取消, 但账本需要闭合)
                try:
                    await runtime.complete(task_id, task_owner, task_fencing, "failed",
                                           {"error": f"回复超时 ({config.REPLY_TIMEOUT * 3}s)"})
                except Exception as tr_err:
                    logger.debug(f"[chat] 超时终态提交异常 {task_id}: {tr_err}")
            try:
                await p.subscribe(reply_ch)
                deadline = time.time() + config.REPLY_TIMEOUT * 3
                # 过程事件采集 (progress 时间线) — 随 assistant 消息落库,
                # 前端历史消息重开时 工具/子代理 tab 仍有数据 (10-19)
                _steps_log: list = []
                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        await _emit_daily_log("system", f"回复落库超时 (user={current_user}, {config.REPLY_TIMEOUT * 3}s 未收到最终回复)", level="error", category="chat", session_id=session_id, username=current_user)
                        await _timeout_terminal()
                        return
                    try:
                        raw = await asyncio.wait_for(
                            p.get_message(ignore_subscribe_messages=True, timeout=remaining),
                            timeout=remaining)
                    except asyncio.TimeoutError:
                        await _emit_daily_log("system", f"回复落库超时 (user={current_user}, {config.REPLY_TIMEOUT * 3}s 未收到最终回复)", level="error", category="chat", session_id=session_id, username=current_user)
                        await _timeout_terminal()
                        return
                    if not raw or raw.get("type") != "message":
                        continue
                    data = raw.get("data")
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    try:
                        maybe = json.loads(data)
                        if maybe.get("_type") in ("progress", "delta"):
                            if maybe.get("_type") == "progress" and maybe.get("text") and len(_steps_log) < 60:
                                _steps_log.append({
                                    "t": time.strftime("%H:%M:%S"),
                                    "text": str(maybe.get("text", ""))[:200],
                                    "source": str(maybe.get("source", "") or ""),
                                })
                            # P1: 中间事件持久化 (单一 writer = persist 任务; 客户端断流仍记录)
                            await _task_emit(maybe.get("_type"), {
                                "text": maybe.get("text", ""),
                                "kind": maybe.get("kind", ""),
                                "source": maybe.get("source", "")})
                            continue  # 继续等待最终回复
                    except (json.JSONDecodeError, AttributeError):
                        pass
                    try:
                        reply = AgentReply.from_json(data)
                    except Exception:
                        continue
                    if reply.error:
                        await _emit_daily_log(reply.source or "system", f"Agent 错误: {reply.error[:100]}", level="error", category="chat", session_id=session_id, username=current_user)
                        # P1: 错误终态 (原子: 终态+事件+Outbox; 归档确认后交付)
                        await runtime.complete(task_id, task_owner, task_fencing,
                                               "failed", {"error": str(reply.error)[:500]})
                        return
                    # 落库: 会话历史 + 关键词索引 + auto-compact + 每日日志
                    text = reply.result.get("text", json.dumps(reply.result, ensure_ascii=False))
                    assistant_msg = {
                        "role": "assistant",
                        "content": text,
                        "source": reply.source,
                        "timestamp": time.time(),
                        "session_id": session_id
                    }
                    if reply.result.get("agents"):
                        assistant_msg['agents'] = reply.result['agents']
                    if _steps_log:
                        assistant_msg['steps'] = _steps_log
                    if reply.result.get("kb_matched") or reply.result.get("kb_enhanced"):
                        assistant_msg['kb_source'] = True
                    if reply.result.get("file_url"):
                        assistant_msg['file_url'] = reply.result['file_url']
                        assistant_msg['file_name'] = reply.result.get('file_name', 'export.csv')
                    # CU 页面快照全集落库 (前端在右侧调用详情卡片展示, 不进对话正文)
                    if reply.result.get("all_shots"):
                        assistant_msg['all_shots'] = reply.result['all_shots']
                    # 结构化问询落库 (chat harness 通用; 兼容旧 cu_ask)
                    if reply.result.get("ask"):
                        assistant_msg['ask'] = reply.result['ask']
                    elif reply.result.get("cu_ask"):
                        assistant_msg['ask'] = reply.result['cu_ask']
                    # Computer Use: 本次使用的浏览器后端 + "需要用户本机浏览器"引导标记
                    if reply.result.get("cu_backend"):
                        assistant_msg['cu_backend'] = reply.result['cu_backend']
                    if reply.result.get("need_extension"):
                        assistant_msg['need_extension'] = True
                    if reply.result.get("login_handoff"):
                        assistant_msg['login_handoff'] = True
                    await r.rpush(config.key_session_msgs(session_id), json.dumps(assistant_msg, ensure_ascii=False))
                    # SessionSearch FTS5 索引: AI 回复同步索引
                    try:
                        from core.memory.session_search import get_session_search, Message as SSMsg
                        _ss = await get_session_search()
                        await _ss.index_session(
                            session_id=session_id, user_id=current_user, agent_id="webchat",
                            messages=[SSMsg(role="assistant", content=str(text)[:2000],
                                           timestamp=str(int(time.time())))],
                            summary=str(text)[:80])
                    except Exception as _fts2:
                        logger.debug(f"[chat] FTS5 AI 索引失败: {_fts2}")
                    # v4.0 (Phase 3.3): transcript jsonl 双写
                    try:
                        from core import transcript as _transcript
                        await _transcript.append_message(
                            session_id, "assistant", text, source=reply.source,
                            agents=reply.result.get("agents"))
                    except Exception:
                        pass
                    if config.SESSION_TTL > 0:
                        await r.expire(config.key_session_msgs(session_id), config.SESSION_TTL)
                    await r.hset(config.key_session_meta(session_id), "updated_at", str(time.time()))
                    await r.hincrby(config.key_session_meta(session_id), "msg_count", 1)
                    if config.SESSION_TTL > 0:
                        await r.expire(config.key_session_meta(session_id), config.SESSION_TTL)
                    try:
                        from core.keyword_index import update_session_keywords
                        await update_session_keywords(r, session_id, text[:500], role="assistant")
                    except Exception:
                        pass
                    try:
                        asyncio.create_task(session_engine.auto_compact_if_needed(
                            session_id, llm_chat_fn=_session_llm_fn))
                    except Exception as compact_err:
                        logger.debug(f"[auto-compact 调度失败] {compact_err}")
                    # P1: 成功终态 — reply 事件 + 终态 + Outbox 同原子, 归档确认后交付
                    try:
                        await runtime.emit(task_id, task_owner, task_fencing, "reply", {
                            "text": str(text)[:2000], "source": reply.source or "",
                            "agents": reply.result.get("agents") or [],
                        })
                        dlv_id = await runtime.complete(task_id, task_owner, task_fencing,
                                                        "succeeded", {"text": str(text)[:2000],
                                                                       "source": reply.source or ""})
                        # 回复已落会话历史 (上方 rpush+transcript 双写) → 渠道交付确认
                        if dlv_id:
                            await runtime.ledger.mark_delivery(task_id, dlv_id, "delivered")
                    except Exception as tr_err:
                        logger.warning(f"[chat] 任务终态提交异常 {task_id}: {tr_err}")
                    today = datetime.now().strftime("%Y-%m-%d")
                    log_key = config.daily_log_key(today)
                    model_info = reply.result.get('model', '')
                    usage_info = reply.result.get('usage', {})
                    token_str = ""
                    if usage_info:
                        token_str = f" [tokens: {usage_info.get('input_tokens', 0)}→{usage_info.get('output_tokens', 0)}]"
                    log_entry = json.dumps({
                        "agent": reply.source,
                        "content": f"路由→{reply.source} 回复 {current_user}: {text[:80]}{token_str}",
                        "level": "success", "category": "chat",
                        "session_id": session_id,
                        "username": current_user,
                        "timestamp": time.time()
                    }, ensure_ascii=False)
                    await r.rpush(log_key, log_entry)
                    if config.DAILY_LOG_TTL > 0:
                        await r.expire(log_key, config.DAILY_LOG_TTL)

                    # P2 收尾 (task-c17): 跨会话记忆存储 (Hermes 构件接线; owner=用户,
                    # 检索侧仅本人可见 — BaseAgent.user_memory_context)
                    try:
                        _u_raw = await r.hget(config.KEY_USERS, current_user)
                        _mem_on = (not _u_raw) or (json.loads(_u_raw).get("memory_enabled", True) is not False)
                        if _mem_on:
                            from core.memory.cross_session_memory import get_cross_session_memory
                            _q = str(msg.text or "")[:200]
                            _a = str(text)[:300]
                            await (await get_cross_session_memory()).store_session_summary(
                                session_id=session_id, user_id=current_user,
                                agent_id=reply.source or "agent",
                                summary=f"Q: {_q} → A: {_a}",
                                topics=[w for w in str(msg.text or "").split() if len(w) > 2][:5])
                    except Exception as _mem_err:
                        logger.debug(f"[chat] 跨会话记忆存储失败 (不阻断): {_mem_err}")
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[persist_reply] 回复落库异常: {e}")
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass
                try:
                    if hasattr(p, "aclose"):
                        await p.aclose()
                    else:
                        await p.close()
                except Exception:
                    pass

        # llm_chat_fn: auto-compact 用 (full compact 时生成摘要)
        async def _session_llm_fn(prompt, system="", temperature=0.3):
            from agents.base import BaseAgent
            _stub = BaseAgent.__new__(BaseAgent)
            _stub.name = "session_engine"
            return await _stub.llm_chat(prompt=prompt, system=system, temperature=temperature)

        persist_task = asyncio.create_task(_persist_reply())

        _pump_task = None  # 供 finally 安全引用
        try:
            # 滑动超时: 收到 progress 即重置空闲窗口 (证明流水线存活)，
            # 绝对上限 3×REPLY_TIMEOUT 防止无限挂起；每 25s 发 SSE 注释心跳保活
            deadline = time.time() + config.REPLY_TIMEOUT
            abs_deadline = time.time() + config.REPLY_TIMEOUT * 3
            _STOP = object()  # 哨兵: 频道已关闭
            _queue: asyncio.Queue = asyncio.Queue()

            async def _pump():
                # 后台 pump: 持续阻塞读取 pubsub，消息转发进队列。
                # 不能用 asyncio.wait_for 取消 listen() 的阻塞读取——取消 redis 读取
                # 协程会破坏连接 (实测 redis-py 7.2.1: 第 2 次取消后迭代器永久死亡)，
                # 导致长请求思考期 >25s 时 SSE 提前 done、最终 reply 丢失。
                try:
                    async for raw in pubsub.listen():
                        await _queue.put(raw)
                finally:
                    await _queue.put(_STOP)

            _pump_task = asyncio.create_task(_pump())

            async def _next(timeout_s):
                try:
                    # 超时取消的是队列等待，不触碰 redis 连接
                    return await asyncio.wait_for(_queue.get(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    return None  # 空闲超时 → 触发心跳/超时检查

            while True:
                if time.time() > abs_deadline:
                    yield f"data: {json.dumps({'type': 'error', 'text': '回复超时'})}\n\n"
                    await _emit_daily_log("system", f"回复超时 (user={current_user}, timeout={config.REPLY_TIMEOUT * 3}s)", level="error", category="chat", session_id=session_id, username=current_user)
                    break
                raw_msg = await _next(25)  # 心跳间隔 25s
                if raw_msg is _STOP:
                    break  # pubsub 已关闭，优雅退出
                if raw_msg is None:
                    if time.time() > deadline:
                        yield f"data: {json.dumps({'type': 'error', 'text': '回复超时'})}\n\n"
                        await _emit_daily_log("system", f"回复超时 (user={current_user}, timeout={config.REPLY_TIMEOUT}s)", level="error", category="chat", session_id=session_id, username=current_user)
                        break
                    yield ": ping\n\n"  # SSE 注释心跳，保持连接活性
                    continue
                if raw_msg["type"] != "message":
                    continue
                try:
                    # 检查是否为进度消息 (JSON 中带 _type=progress)
                    raw_data = raw_msg["data"]
                    if isinstance(raw_data, bytes):
                        raw_data = raw_data.decode('utf-8')
                    try:
                        maybe_progress = json.loads(raw_msg["data"])
                        if maybe_progress.get("_type") == "progress":
                            # 收到进度 → 流水线存活，刷新空闲截止时间 (滑动窗口)
                            deadline = time.time() + config.REPLY_TIMEOUT
                            yield f"data: {json.dumps({'type': 'progress', 'text': maybe_progress.get('text', '处理中...'), 'source': maybe_progress.get('source', '')}, ensure_ascii=False)}\n\n"
                            continue  # 不 break，继续等待最终回复
                        if maybe_progress.get("_type") == "delta":
                            # 流式增量 → 纯转发 (事件记录由 persist 任务统一负责, 单一 writer)
                            deadline = time.time() + config.REPLY_TIMEOUT
                            yield f"data: {json.dumps({'type': 'delta', 'text': maybe_progress.get('text', ''), 'source': maybe_progress.get('source', ''), 'kind': maybe_progress.get('kind', 'content')}, ensure_ascii=False)}\n\n"
                            continue  # 不 break，继续等待最终回复
                    except (json.JSONDecodeError, AttributeError):
                        pass

                    reply = AgentReply.from_json(raw_msg["data"])
                    if reply.error:
                        yield f"data: {json.dumps({'type': 'error', 'text': reply.error}, ensure_ascii=False)}\n\n"
                        # 错误日志由 _persist_reply 落库任务统一写入 (独立于 SSE 连接)
                    else:
                        text = reply.result.get("text", json.dumps(reply.result, ensure_ascii=False))
                        # 增强 SSE 元数据 (参考 claude-code cost-tracker)
                        reply_meta = {
                            'type': 'reply',
                            'text': text,
                            'source': reply.source,
                            'session_id': session_id,
                        }
                        # 注入 agent 路由信息
                        if reply.result.get("source"):
                            reply_meta['data_source'] = reply.result['source']
                        if reply.result.get("count") is not None:
                            reply_meta['data_count'] = reply.result['count']
                        # 注入参与链 (多 Agent 协作时前端全部展示来源徽章)
                        if reply.result.get("agents"):
                            reply_meta['agents'] = reply.result['agents']
                        # 注入知识来源标记 (kb_matched/kb_enhanced → 前端追加 KB 徽章)
                        kb_source = bool(reply.result.get("kb_matched") or reply.result.get("kb_enhanced"))
                        if kb_source:
                            reply_meta['kb_source'] = True
                        # 注入 token 用量 (如果 agent 返回了)
                        if reply.result.get("usage"):
                            reply_meta['usage'] = reply.result['usage']
                        if reply.result.get("model"):
                            reply_meta['model'] = reply.result['model']
                        # 注入文件下载信息 (CSV 导出)
                        if reply.result.get("file_url"):
                            reply_meta['file_url'] = reply.result['file_url']
                            reply_meta['file_name'] = reply.result.get('file_name', 'export.csv')
                        # CU 页面快照全集 (右侧调用详情卡片展示)
                        if reply.result.get("all_shots"):
                            reply_meta['all_shots'] = reply.result['all_shots']
                        # 结构化问询 (chat harness 通用: BaseAgent.send_ask; 兼容旧 cu_ask)
                        if reply.result.get("ask"):
                            reply_meta['ask'] = reply.result['ask']
                        elif reply.result.get("cu_ask"):
                            reply_meta['ask'] = reply.result['cu_ask']
                        # Computer Use: 浏览器后端 / 需连接本机浏览器的引导标记
                        if reply.result.get("cu_backend"):
                            reply_meta['cu_backend'] = reply.result['cu_backend']
                        if reply.result.get("need_extension"):
                            reply_meta['need_extension'] = True
                        if reply.result.get("login_handoff"):
                            reply_meta['login_handoff'] = True
                        # 凸显信号: 本轮使用的已发布技能 / 是否参考了用户记忆 (前端徽章)
                        if reply.result.get("skills"):
                            reply_meta['skills'] = reply.result['skills']
                        if reply.result.get("memory_used"):
                            reply_meta['memory_used'] = True
                        yield f"data: {json.dumps(reply_meta, ensure_ascii=False)}\n\n"

                        # 会话历史落库/关键词索引/auto-compact/每日日志已统一由
                        # _persist_reply 后台任务负责 (独立于 SSE 连接，切页不断流)
                    break
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
                    break
        finally:
            if _pump_task is not None:
                _pump_task.cancel()
                try:
                    await _pump_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.warning(f"[SSE] pump 清理异常 (忽略): {e}")
            try:
                await pubsub.unsubscribe(reply_ch)
            except Exception as e:
                logger.debug(f"[SSE] unsubscribe 失败 (忽略): {e}")
            try:
                if hasattr(pubsub, "aclose"):
                    await pubsub.aclose()
                else:
                    await pubsub.close()
            except Exception as e:
                logger.debug(f"[SSE] pubsub close 失败 (忽略): {e}")
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    # P1: 幂等键随响应返回 (旧客户端下次重试可携带), 任务 id 供断线补读
    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Idempotency-Key": idem_key, "X-Task-Id": task_id,
                 "Cache-Control": "no-cache"})


# ═══ P1: 任务事件读取 (断线补读/游标/SSE) + 协作停止 ═══

_TERMINAL = ("succeeded", "failed", "cancelled", "unknown")


def _task_ev_to_sse(ev: dict, session_id: str) -> dict:
    """任务事件 → 前端 SSE 事件形状 (与 POST 流事件格式对齐)"""
    t, d = ev.get("type"), ev.get("data") or {}
    if t == "task_started":
        return {"type": "start", "msg_id": d.get("msg_id", ""),
                "session_id": d.get("session_id", session_id)}
    if t == "progress":
        return {"type": "progress", "text": d.get("text", ""), "source": d.get("source", "")}
    if t == "delta":
        return {"type": "delta", "text": d.get("text", ""), "source": d.get("source", ""),
                "kind": d.get("kind", "content"), "event_id": ev.get("event_id")}
    if t == "reply":
        return {"type": "reply", "text": d.get("text", ""), "source": d.get("source", ""),
                "agents": d.get("agents") or [], "event_id": ev.get("event_id")}
    if t == "task_terminal":
        out = {"type": "done", "event_id": ev.get("event_id")}
        if d.get("error"):
            out = {"type": "error", "text": d.get("error"), "event_id": ev.get("event_id")}
        return out
    return {"type": t or "event", "event_id": ev.get("event_id"), **(d if isinstance(d, dict) else {})}


async def _task_owned(runtime, task_id: str, current_user: str) -> dict:
    task = await runtime.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if (task.get("actor") or "") != (current_user or ""):
        raise HTTPException(403, "无权访问此任务")
    return task


@router.get("/api/chat/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request,
                      after: int = 0, current_user: str = Depends(verify_token)):
    """任务事件读取 (P1 断线补读): ?after=游标 → JSON; Accept: text/event-stream → SSE。

    - 游标 = 事件 seq; 支持 Last-Event-ID header (SSE 标准回连);
    - 游标超前于账本历史 → 400 明确报错 (不静默丢历史);
    - 归属校验: 仅任务发起人可读。
    """
    from core.task_runtime import get_task_runtime
    runtime = get_task_runtime()
    task = await _task_owned(runtime, task_id, current_user)
    last_seq = int(task.get("seq") or 0)
    cursor = after
    lei = request.headers.get("Last-Event-ID") or ""
    if lei and lei.isdigit():
        cursor = int(lei)
    if cursor > last_seq:
        raise HTTPException(400, f"游标超前 (after={cursor} > last_seq={last_seq}); 事件历史不会静默丢弃")
    want_sse = "text/event-stream" in (request.headers.get("accept") or "")

    if not want_sse:
        events = await runtime.read_events_after(task_id, cursor)
        return {"task_id": task_id, "status": task.get("status"),
                "terminal": task.get("status") in _TERMINAL,
                "last_seq": last_seq, "events": events}

    async def sse():
        nonlocal cursor
        yield "retry: 3000\n\n"
        idle = 0
        while True:
            t = await runtime.get_task(task_id)
            if not t:
                yield f"data: {json.dumps({'type': 'error', 'text': '任务不存在'})}\n\n"
                return
            events = await runtime.read_events_after(task_id, cursor)
            for ev in events:
                cursor = max(cursor, int(ev.get("seq") or 0))
                payload = _task_ev_to_sse(ev, t.get("session_id") or "")
                yield f"id: {ev.get('seq')}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if payload.get("type") in ("done", "error") and ev.get("type") == "task_terminal":
                    return
            if t.get("status") in _TERMINAL:
                yield f"id: {cursor}\ndata: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                return
            idle += 1
            if idle % 10 == 0:
                yield ": ping\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@router.post("/api/chat/tasks/{task_id}/stop")
async def task_stop(task_id: str, current_user: str = Depends(verify_token)):
    """协作停止 (P1): 校验 actor 归属; 断线不取消; 不宣称撤销已发出的外部动作"""
    from core.task_runtime import get_task_runtime
    runtime = get_task_runtime()
    task = await _task_owned(runtime, task_id, current_user)
    is_admin = False
    try:
        r = await get_redis()
        raw = await r.hget(config.KEY_USERS, current_user)
        is_admin = bool(raw and json.loads(raw).get("role") == "admin")
    except Exception:
        pass
    ok, why = await runtime.request_stop(task_id, current_user, is_admin=is_admin)
    if not ok:
        raise HTTPException(409, why)
    # 协作通知: 处理链可见 cancel_requested 事件; 已发出的外部动作不自动撤销
    try:
        r = await get_redis()
        await r.publish(config.channel("orchestrator"), json.dumps({
            "_type": "cancel_requested", "task_id": task_id,
            "session_id": task.get("session_id") or "",
            "msg_id": task.get("msg_id") or "",
            "actor": current_user,
        }, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"[chat] 停止通知发布失败: {e}")
    # 停止必须贯通本机执行端: 运行中的 Computer Use 任务直接下发 control cancel。
    # 只有设备 result 把任务置为 cancelled 才算确认; 失联/超时保持未知, 不宣称已停止。
    cu = await _cu_cancel(task, task_id, current_user)
    if cu and cu.get("confirmed"):
        note = "已请求协作取消; 本机执行端已确认停止; 已发出的外部操作不自动撤销"
    elif cu and cu.get("state") == "unknown":
        note = "已请求协作取消; 本机设备结果未知 (可能失联), 请先在设备上核对, 不会自动重发"
    elif cu:
        note = "已请求协作取消; 本机执行端尚未确认停止, 请在任务小窗核对"
    else:
        note = "已请求协作取消; 已发出的外部操作不自动撤销, 以审计为准"
    return {"status": "ok", "task_id": task_id, "cu": cu, "note": note}


async def _cu_cancel(task: dict, task_id: str, actor: str):
    """把运行时停止请求转发给本机设备; 返回 None 表示该任务没有本机执行记录。"""
    try:
        from core.cu import devices as cudev
        svc = await cudev.get_service()
    except Exception as e:
        logger.debug(f"[chat] 本机设备服务不可用: {e}")
        return None
    # Agent 以触发消息 id 作为设备任务 id; 兼容直接使用运行时任务 id 的情况。
    candidates = [str(task.get("msg_id") or ""), task_id]
    entry = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            # 归属已由 _task_owned 校验; 此处不再依赖会话访问, 避免停止键被会话校验挡住。
            _, entry = await svc.task_record(actor, candidate, check=False)
            break
        except (ValueError, TypeError):
            entry = None
    if not entry:
        return None
    state = str(entry.get("state") or "")
    if state not in ("cancelled", "failed", "succeeded", "unknown"):
        try:
            view = await svc.cancel_task(actor, entry["task_id"])
            state = str((view or {}).get("state") or state)
        except (ValueError, TypeError):
            # 控制未受理也不能改写状态; 保持未知/未确认, 由小窗继续核对。
            state = ""
    return {"task_id": entry["task_id"], "state": state, "requested": True,
            "confirmed": state == "cancelled",
            "unknown": state == "unknown"}

# ═══ 执行命令 ═══
@router.post("/api/command")
async def command(request: Request, current_user: str = Depends(verify_token)):
    body = await request.json()
    cmd = body.get("cmd", "")
    args = body.get("args", "")
    if not cmd:
        raise HTTPException(400, "缺少 cmd")
    r = await get_redis()
    msg = AgentMessage(
        source="webchat", target="orchestrator", action=cmd, text=args,
        params={"args": args}, channel_source="webchat", username=current_user
    )
    reply_ch = config.reply_channel(msg.msg_id)
    pubsub = r.pubsub()
    await pubsub.subscribe(reply_ch)
    orch_ch = config.channel("orchestrator")
    await r.publish(orch_ch, msg.to_json())
    try:
        deadline = time.time() + config.REPLY_TIMEOUT
        async for raw_msg in pubsub.listen():
            if time.time() > deadline:
                return {"error": "回复超时", "msg_id": msg.msg_id}
            if raw_msg["type"] != "message":
                continue
            reply = AgentReply.from_json(raw_msg["data"])
            return {"msg_id": msg.msg_id, "source": reply.source, "result": reply.result, "error": reply.error}
    finally:
        await pubsub.unsubscribe(reply_ch)
        await pubsub.close()
    return {"error": "未收到回复"}

async def _verify_oneapi_token(token: str) -> str | None:
    """验证 API Key，返回 created_by (创建者用户名) 或 None"""
    r = await get_redis()
    raw = await r.hgetall(config.KEY_ONEAPI_KEYS)
    for _hkey, data in raw.items():
        try:
            info = json.loads(data)
            if secrets.compare_digest(info.get("key", ""), token):
                return info.get("created_by", "oneapi_user")
        except json.JSONDecodeError:
            continue
    return None

# ═══ OpenAI 兼容接口 (/v1/chat/completions) ═══

@router.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    """OpenAI 兼容端点，支持 oneapi:* token 鉴权。
    外部 Chatbox / OpenWebUI 等可直接使用此接口。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, {"error": {"message": "Missing Authorization header", "type": "invalid_request_error"}})
    token = auth[7:]

    # 验证 token: 先查平台 API Key，再查用户登录 token
    username = await _verify_oneapi_token(token)
    if not username:
        r = await get_redis()
        username = await r.get(config.key_token(token))
    if not username:
        raise HTTPException(401, {"error": {"message": "Invalid API key", "type": "invalid_request_error"}})

    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", "")
    stream = body.get("stream", False)

    # 提取最后一条 user message 作为 prompt
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = m.get("content", "")
            break
    if not user_text:
        raise HTTPException(400, {"error": {"message": "No user message found", "type": "invalid_request_error"}})

    r = await get_redis()
    msg = AgentMessage(
        source="oneapi", target="orchestrator", text=user_text,
        channel_source="oneapi", username=username,
    )
    reply_ch = config.reply_channel(msg.msg_id)
    completion_id = f"chatcmpl-{msg.msg_id}"
    ts = int(time.time())

    if stream:
        async def sse_stream():
            pubsub = r.pubsub()
            await pubsub.subscribe(reply_ch)
            orch_ch = config.channel("orchestrator")
            await r.publish(orch_ch, msg.to_json())
            try:
                deadline = time.time() + config.REPLY_TIMEOUT
                async for raw_msg in pubsub.listen():
                    if time.time() > deadline:
                        break
                    if raw_msg["type"] != "message":
                        continue
                    try:
                        raw_data = raw_msg["data"]
                        if isinstance(raw_data, bytes):
                            raw_data = raw_data.decode("utf-8")
                        # 跳过 progress 消息
                        try:
                            maybe = json.loads(raw_data)
                            if maybe.get("_type") == "progress":
                                continue
                        except (json.JSONDecodeError, AttributeError):
                            pass
                        reply = AgentReply.from_json(raw_msg["data"])
                        text = reply.result.get("text", "") if not reply.error else reply.error
                        # 发送 SSE chunk
                        chunk = {
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": ts, "model": model or "zops-agent",
                            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                        # 结束 chunk
                        done_chunk = {
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": ts, "model": model or "zops-agent",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                        }
                        yield f"data: {json.dumps(done_chunk)}\n\n"
                        yield "data: [DONE]\n\n"
                        break
                    except Exception:
                        break
            finally:
                await pubsub.unsubscribe(reply_ch)
                await pubsub.close()
        return StreamingResponse(sse_stream(), media_type="text/event-stream")
    else:
        # 同步模式: 等待回复
        pubsub = r.pubsub()
        await pubsub.subscribe(reply_ch)
        orch_ch = config.channel("orchestrator")
        await r.publish(orch_ch, msg.to_json())
        text = ""
        try:
            deadline = time.time() + config.REPLY_TIMEOUT
            async for raw_msg in pubsub.listen():
                if time.time() > deadline:
                    text = "回复超时"
                    break
                if raw_msg["type"] != "message":
                    continue
                try:
                    raw_data = raw_msg["data"]
                    if isinstance(raw_data, bytes):
                        raw_data = raw_data.decode("utf-8")
                    try:
                        maybe = json.loads(raw_data)
                        if maybe.get("_type") == "progress":
                            continue
                    except (json.JSONDecodeError, AttributeError):
                        pass
                    reply = AgentReply.from_json(raw_msg["data"])
                    text = reply.result.get("text", "") if not reply.error else reply.error
                    break
                except Exception as e:
                    text = str(e)
                    break
        finally:
            await pubsub.unsubscribe(reply_ch)
            await pubsub.close()

        return JSONResponse({
            "id": completion_id, "object": "chat.completion",
            "created": ts, "model": model or "zops-agent",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": len(user_text), "completion_tokens": len(text), "total_tokens": len(user_text) + len(text)}
        })

# 兼容 /v1/models 端点 (Chatbox/OpenWebUI 探测用)
@router.get("/v1/models")
async def openai_models_list(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        user = await _verify_oneapi_token(token)
        if not user:
            r = await get_redis()
            user = await r.get(config.key_token(token))
        if not user:
            raise HTTPException(401, {"error": {"message": "Invalid API key", "type": "invalid_request_error"}})
    return {
        "object": "list",
        "data": [
            {"id": "zops-agent", "object": "model", "owned_by": "zops", "created": int(time.time())},
        ]
    }
