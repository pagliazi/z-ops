"""
Z-OPS v2.0.0 — 飞书通道插件
基于 BaseChannel 实现，监听 Redis 通知并推送到飞书 Webhook

功能:
- 监听 notify:feishu 频道，推送通知
- 可选: 接收飞书事件回调，转发到 Orchestrator
"""

import asyncio
import json
import time
import logging
from typing import Optional

import httpx

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.channel_base import BaseChannel, ChannelMessage, ChannelReply

logger = logging.getLogger("zops")


class FeishuChannel(BaseChannel):
    """飞书通道 — Webhook 推送 + 可选事件接收"""

    channel_name = "feishu"
    description = "飞书群消息通道"

    def __init__(self):
        super().__init__()
        self._webhook_url = config.FEISHU_WEBHOOK_URL

    async def start(self):
        """启动飞书通道"""
        self._running = True
        await self.register_heartbeat()
        logger.info(f"[{self.channel_name}] 飞书通道启动")

        # 心跳协程
        asyncio.create_task(self.heartbeat_loop())

        # 订阅飞书通知频道
        r = await self.get_redis()
        pubsub = r.pubsub()
        notify_ch = config.channel("notify:feishu")
        await pubsub.subscribe(notify_ch)
        logger.info(f"[{self.channel_name}] 已订阅频道: {notify_ch}")

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

                    if text:
                        reply = ChannelReply(
                            text=text,
                            reply_type="card",
                            metadata={"topic": topic, "source": source}
                        )
                        await self.send(reply)
                except Exception as e:
                    logger.error(f"[{self.channel_name}] 处理通知消息失败: {e}")
        finally:
            await pubsub.unsubscribe(notify_ch)
            await self.cleanup()

    async def send(self, reply: ChannelReply) -> bool:
        """发送消息到飞书 Webhook"""
        if not self._webhook_url or "YOUR_HOOK_ID" in self._webhook_url:
            logger.warning(f"[{self.channel_name}] Webhook URL 未配置，跳过推送")
            return False

        topic = reply.metadata.get("topic", "")
        source = reply.metadata.get("source", "")

        title = "Z-OPS 运维通知"
        if topic:
            title += f" [{topic}]"

        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"content": title, "tag": "plain_text"},
                    "template": self._topic_color(topic)
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {"content": reply.text[:2000], "tag": "lark_md"}
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {"tag": "plain_text",
                             "content": f"来源: {source} | {time.strftime('%Y-%m-%d %H:%M:%S')}"}
                        ]
                    }
                ]
            }
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._webhook_url, json=payload)
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("code") == 0:
                        logger.info(f"[{self.channel_name}] 推送成功: {reply.text[:50]}...")
                        return True
                    else:
                        logger.error(f"[{self.channel_name}] 推送失败: {result}")
                else:
                    logger.error(f"[{self.channel_name}] HTTP 错误: {resp.status_code}")
        except Exception as e:
            logger.error(f"[{self.channel_name}] 推送异常: {e}")
        return False

    async def stop(self):
        """停止飞书通道"""
        self._running = False
        logger.info(f"[{self.channel_name}] 飞书通道停止")

    @staticmethod
    def _topic_color(topic: str) -> str:
        colors = {
            "ops": "blue", "network": "green", "vm": "purple",
            "alert": "red", "report": "turquoise", "system": "grey",
        }
        return colors.get(topic, "blue")


# ═══ 启动入口 ═══

def main():
    import signal

    channel = FeishuChannel()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(sig, frame):
        logger.info("收到停止信号...")
        loop.create_task(channel.stop())

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(channel.start())
    except KeyboardInterrupt:
        loop.run_until_complete(channel.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
