"""session_recap — 会话动态观测插件 (dsh session/* 持久事件域的消费者示例)"""
import json
import logging
import time

from core.plugin_system import Plugin, PluginContext

logger = logging.getLogger("zops")


class SessionRecapPlugin(Plugin):
    id = "session_recap"
    version = "1.0.0"
    description = "会话摘要事件留痕 (日志 + Redis 最近 100 条动态)"

    async def apply(self, ctx: PluginContext):
        bus = ctx.bus
        redis_client = None

        async def on_summary_stored(payload: dict):
            nonlocal redis_client
            user = payload.get("user_id", "?")
            sid = payload.get("session_id", "?")
            topics = ", ".join((payload.get("topics") or [])[:3]) or "无话题"
            logger.info(f"[session_recap] 用户 {user} 会话 {sid[:12]} 落盘 "
                        f"(话题: {topics})")
            try:
                if redis_client is None:
                    import config
                    import redis.asyncio as aioredis
                    redis_client = aioredis.from_url(config.REDIS_URL,
                                                     decode_responses=True)
                import config
                await redis_client.lpush(
                    f"{config.REDIS_PREFIX}:plugins:recap",
                    json.dumps({"ts": time.time(), "user": user,
                                "session": sid, "topics": topics[:80]},
                               ensure_ascii=False))
                await redis_client.ltrim(f"{config.REDIS_PREFIX}:plugins:recap", 0, 99)
            except Exception as e:
                logger.debug(f"[session_recap] Redis 留痕失败: {e}")

        dispose = bus.on("session/summary_stored", on_summary_stored, self.id)
        ctx.contribute(self.id, "event_handler", "session/summary_stored",
                       "日志留痕 + zops:plugins:recap 最近动态 (100 条)")
        return dispose
