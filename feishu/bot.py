"""
Z-OPS 飞书通道 — 组合根(FeishuBot + 出站推送主循环 + 进程入口)

FeishuBot 按 mixin 组合各域(方法运行时绑定,域间经 self 协作):
    FeishuProtocol     协议原语(token/发送/回复/图片上传)
    NmsDomain          监控域(告警卡片/群命令/自然语言查询)
    ApprovalDomain     审批域(审批卡片)
    ConversationDomain 对话域(帮助卡片/会话锚点)

出站推送主循环(start):订阅 Redis notify:feishu 频道,消费各 agent 的
notify() 消息,按 extra 分发:
    topic=approval          → 审批卡片(失败降级 Webhook 纯文本)
    extra.channel(webhook)  → 通道 URL 直发富卡片(多源通道)
    extra.channel(app)      → 应用消息发指定群(截图内嵌)
    extra.target            → 兼容旧订阅分发
    其余                    → 默认 Webhook

入站接收(WS 长连接 + HTTP 接收器)由 main() 启动,见 feishu.gateway。
"""

import asyncio
import json
import time
import threading
import logging
from typing import Optional

import redis.asyncio as aioredis

import config

from .protocol import FeishuProtocol, shared_bot
from .nms import NmsDomain
from .approval import ApprovalDomain
from .conversation import ConversationDomain
from .gateway import create_feishu_receiver, _start_ws_receiver

logger = logging.getLogger("zops")


class FeishuBot(FeishuProtocol, NmsDomain, ApprovalDomain, ConversationDomain):
    """飞书消息推送 + 接收(组合根:状态初始化 + 出站主循环)"""

    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None
        self._running = False
        # tenant_access_token 缓存（有效期约 2 小时，提前 5 分钟失效）
        self._ta_token = ""
        self._ta_token_expires = 0.0

    async def start(self):
        """启动飞书 Bot(出站推送主循环)"""
        self._running = True
        r = await self.get_redis()

        # 注册渠道心跳
        await r.hset(config.KEY_CHANNEL_HEARTBEATS, "feishu", json.dumps({
            "name": "feishu",
            "status": "online",
            "started": time.time()
        }))
        logger.info("飞书 Bot 启动")

        # 心跳协程
        asyncio.create_task(self._heartbeat_loop())

        # 订阅飞书通知频道
        pubsub = r.pubsub()
        notify_ch = config.channel("notify:feishu")
        await pubsub.subscribe(notify_ch)
        logger.info(f"已订阅频道: {notify_ch}")

        try:
            async for raw_msg in pubsub.listen():
                if not self._running:
                    break
                if raw_msg["type"] != "message":
                    continue
                try:
                    data = json.loads(raw_msg["data"])
                    text = data.get("text", "")
                    topic = data.get("topic", "general")
                    source = data.get("source", "unknown")
                    extra = data.get("extra")

                    if text:
                        if topic == "approval" and (extra or {}).get("approval"):
                            # 审批卡片：应用群模式发带「同意/拒绝」按钮的交互卡片；
                            # 无凭据/无应用群通道时降级 Webhook 纯文本（按钮回调仅应用卡片可达）
                            if await self._send_approval_card(extra["approval"]):
                                continue
                            await self._send_webhook(text, topic, source, extra=extra)
                            continue
                        _channel = (extra or {}).get("channel") or {}
                        if _channel.get("type") == "webhook" and _channel.get("webhook_url"):
                            # 通道分发（Webhook 型）：直发该群自定义机器人 URL
                            await self._send_webhook_card(
                                _channel["webhook_url"], text, topic, source, extra)
                        elif _channel.get("chat_id"):
                            # 通道分发（应用群型）：应用消息接口发到指定 chat（截图内嵌飞书图片）
                            await self._send_im_card(_channel["chat_id"], topic, extra)
                        elif extra and extra.get("target"):
                            # 兼容旧 target 订阅分发
                            await self._send_im_card(extra["target"], topic, extra)
                        else:
                            await self._send_webhook(text, topic, source, extra=extra)
                except Exception as e:
                    logger.error(f"处理通知消息失败: {e}")
        finally:
            await pubsub.unsubscribe(notify_ch)
            await self._cleanup()

    async def _heartbeat_loop(self):
        """心跳更新"""
        while self._running:
            try:
                r = await self.get_redis()
                await r.hset(config.KEY_CHANNEL_HEARTBEATS, "feishu", json.dumps({
                    "name": "feishu",
                    "status": "online",
                    "last_heartbeat": time.time()
                }))
            except Exception as e:
                logger.error(f"心跳更新失败: {e}")
            await asyncio.sleep(10)

    async def _cleanup(self):
        """清理"""
        r = await self.get_redis()
        await r.hset(config.KEY_CHANNEL_HEARTBEATS, "feishu", json.dumps({
            "name": "feishu", "status": "offline", "stopped": time.time()
        }))
        if self._redis:
            await self._redis.aclose()

    async def stop(self):
        self._running = False


# ═══ 进程入口 ═══

def main():
    import signal

    # 出站主循环与入站消息处理共用单例(复用 Redis 连接)
    bot = shared_bot()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(sig, frame):
        logger.info("收到停止信号...")
        loop.create_task(bot.stop())

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        # 飞书长连接事件接收（内网环境无需公网回调；凭据优先取前端管理页动态配置）
        _dyn = loop.run_until_complete(bot._dyn_cfg())
        ws_app_id = _dyn.get("feishu_app_id") or config.FEISHU_APP_ID
        ws_secret = _dyn.get("feishu_app_secret") or config.FEISHU_APP_SECRET
        if ws_app_id and ws_secret:
            threading.Thread(
                target=_start_ws_receiver,
                args=(ws_app_id, ws_secret, loop),
                daemon=True, name="feishu-ws").start()
            logger.info("飞书长连接客户端已启动（内网环境，无需公网回调地址）")
        else:
            logger.warning("飞书 APP 凭据未配置，长连接事件接收未启动")
        # 飞书事件接收 HTTP 服务（应用机器人交互，常驻启动；APP 凭据前端管理页可配置即时生效）
        receiver_app = loop.run_until_complete(create_feishu_receiver())
        import uvicorn
        _server_cfg = uvicorn.Config(
            receiver_app, host="0.0.0.0", port=config.FEISHU_EVENT_PORT,
            log_level="warning")
        _receiver = uvicorn.Server(_server_cfg)
        loop.create_task(_receiver.serve())
        logger.info(f"飞书事件接收服务已启动: 0.0.0.0:{config.FEISHU_EVENT_PORT}")
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
