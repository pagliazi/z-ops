"""P1 Streams 消费派发 — OPS-CORE 多对象只读试点

消费者认领任务后: 构造 AgentMessage → 订阅 reply 频道 → 发布给目标 agent →
progress/delta 转发进任务事件 → 最终回复落会话历史 + emit reply + 终态。
协作取消: cancel_requested 置位时尽快以 cancelled 终态闭合 (已发出的外部动作不撤销)。

SSE 侧不重复处理: 入口对 Streams 任务走事件跟随 (_replay_and_follow), 单一执行方。
"""

import asyncio
import json
import logging
import time

import config

logger = logging.getLogger("zops")


class AgentDispatch:
    """TaskConsumer 的 handler 实现: 任务 → 目标 agent 的可靠派发"""

    def __init__(self, runtime):
        self.runtime = runtime
        self.ledger = runtime.ledger

    async def __call__(self, task_id: str, stream_msg_id: str, consumer, fencing: int):
        task = await self.ledger.get_task(task_id)
        if not task:
            return
        message = task.get("message") or ""
        target = task.get("target") or "orchestrator"
        session_id = task.get("session_id") or ""
        actor = task.get("actor") or ""
        if not message:
            await self.runtime.complete(task_id, consumer.owner, fencing,
                                        "failed", {"error": "任务缺少 message"})
            return
        await self.ledger.emit(task_id, consumer.owner, fencing, "dispatch_started",
                               {"target": target})
        from agents.base import AgentMessage, AgentReply
        r = await self.ledger._redis()
        msg = AgentMessage(
            source="task_runtime", target=target, action="chat",
            text=message, channel_source="webchat",
            session_id=session_id, username=actor,
        )
        reply_ch = config.reply_channel(msg.msg_id)
        pubsub = r.pubsub()
        deadline = time.time() + config.REPLY_TIMEOUT * 3
        _steps = []  # progress 时间线 (随 assistant 消息落库, 历史重开仍可展开工具详情)
        try:
            await pubsub.subscribe(reply_ch)
            await r.publish(config.channel(target), msg.to_json())
            logger.info(f"[agent_dispatch] {task_id} 已派发 {target} (msg={msg.msg_id})")
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    await self.runtime.complete(
                        task_id, consumer.owner, fencing, "failed",
                        {"error": f"回复超时 ({config.REPLY_TIMEOUT * 3}s)"})
                    return
                # 协作取消检查
                if int((await self.ledger.get_task(task_id) or {}).get("cancel_requested") or 0) == 1:
                    await self.runtime.complete(task_id, consumer.owner, fencing,
                                                "cancelled", {"reason": "stop_requested"})
                    return
                raw = await pubsub.get_message(ignore_subscribe_messages=True,
                                               timeout=min(remaining, 5.0))
                if raw is None or raw.get("type") != "message":
                    continue
                data = raw.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                try:
                    mid = json.loads(data)
                    if mid.get("_type") in ("progress", "delta"):
                        _ev = {
                            "text": mid.get("text", ""),
                            "kind": mid.get("kind", ""),
                            "source": mid.get("source", "")}
                        # 工具调用结构化详情 (右侧调用详情卡片折叠展示)
                        _det = mid.get("detail") if isinstance(mid.get("detail"), dict) else None
                        if _det:
                            _ev["detail"] = _det
                        await self.ledger.emit(task_id, consumer.owner, fencing,
                                               mid.get("_type"), _ev)
                        # steps 采集: 一次工具调用 = 一条记录。完成事件 (detail 带 result)
                        # 合并进同 seq+tool 的开始条目, 而不是另起一条 (历史重开不再双行)
                        if mid.get("_type") == "progress" and mid.get("text") and len(_steps) < 60:
                            _st = {"t": time.strftime("%H:%M:%S"),
                                   "text": str(mid.get("text", ""))[:200],
                                   "source": str(mid.get("source", "") or "")}
                            _det2 = _det if _det else None
                            if _det2:
                                _st.update({k: _det2[k] for k in
                                            ("seq", "tool", "args", "result", "ok", "secs", "result_len") if k in _det2})
                            _merged = False
                            if _det2 and "result" in _det2:
                                for _i in range(len(_steps) - 1, -1, -1):
                                    _o = _steps[_i]
                                    if _o.get("seq") == _det2.get("seq") and _o.get("tool") == _det2.get("tool"):
                                        _o["text"] = _st["text"]
                                        _o.update({k: _det2[k] for k in
                                                   ("seq", "tool", "args", "result", "ok", "secs", "result_len") if k in _det2})
                                        _merged = True
                                        break
                            if not _merged:
                                _steps.append(_st)
                        continue
                except (json.JSONDecodeError, AttributeError):
                    pass
                try:
                    reply = AgentReply.from_json(data)
                except Exception:
                    continue
                text = ""
                if not reply.error:
                    res = reply.result or {}
                    text = res.get("text") or json.dumps(res, ensure_ascii=False)
                    await self._persist_session(session_id, actor, text, reply.source or target,
                                                steps=_steps)
                    await self.ledger.emit(task_id, consumer.owner, fencing, "reply", {
                        "text": str(text)[:2000], "source": reply.source or target,
                        "agents": (reply.result or {}).get("agents") or []})
                    dlv = await self.runtime.complete(task_id, consumer.owner, fencing,
                                                      "succeeded",
                                                      {"text": str(text)[:2000],
                                                       "source": reply.source or target})
                    if dlv:
                        await self.ledger.mark_delivery(task_id, dlv, "delivered")
                else:
                    await self.runtime.complete(task_id, consumer.owner, fencing,
                                                "failed",
                                                {"error": str(reply.error)[:500]})
                return
        finally:
            try:
                await pubsub.unsubscribe(reply_ch)
                await pubsub.aclose()
            except Exception:
                pass

    async def _persist_session(self, session_id: str, actor: str,
                               text: str, source: str, steps: list = None):
        """回复落会话历史 (Redis + transcript 双写; 与 webchat 协调路径同口径)
        steps: progress 时间线 (含工具调用详情), 前端历史重开时调用详情卡片仍有数据"""
        try:
            if not session_id:
                return
            r = await self.ledger._redis()
            assistant_msg = {
                "role": "assistant", "content": text,
                "source": source, "timestamp": time.time(),
                "session_id": session_id,
            }
            if steps:
                assistant_msg["steps"] = steps
            await r.rpush(config.key_session_msgs(session_id),
                          json.dumps(assistant_msg, ensure_ascii=False))
            if config.SESSION_TTL > 0:
                await r.expire(config.key_session_msgs(session_id), config.SESSION_TTL)
            await r.hset(config.key_session_meta(session_id), "updated_at", str(time.time()))
            await r.hincrby(config.key_session_meta(session_id), "msg_count", 1)
            from core import transcript as _transcript
            await _transcript.append_message(session_id, "assistant", str(text)[:4000],
                                             source=source)
        except Exception as e:
            logger.warning(f"[agent_dispatch] 会话落库失败 session={session_id}: {e}")
