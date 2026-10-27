"""
Z-OPS v2.0.0 — 企业微信通道插件
基于 BaseChannel 实现，支持企业微信机器人 Webhook 和应用消息

配置 (zops.env):
  WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
  WECOM_CORP_ID=your-corp-id       (可选: 应用消息)
  WECOM_AGENT_ID=your-agent-id     (可选: 应用消息)
  WECOM_SECRET=your-secret         (可选: 应用消息)
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

# 企微配置
WECOM_WEBHOOK_URL = config.get("WECOM_WEBHOOK_URL", "")
WECOM_CORP_ID = config.get("WECOM_CORP_ID", "")
WECOM_AGENT_ID = config.get("WECOM_AGENT_ID", "")
WECOM_SECRET = config.get("WECOM_SECRET", "")


class WecomChannel(BaseChannel):
    """企业微信通道 — Webhook + 应用消息"""

    channel_name = "wecom"
    description = "企业微信消息通道"

    def __init__(self):
        super().__init__()
        self._access_token = ""
        self._token_expire = 0

    async def start(self):
        """启动企业微信通道"""
        self._running = True
        await self.register_heartbeat()
        logger.info(f"[{self.channel_name}] 企业微信通道启动")

        # 心跳协程
        asyncio.create_task(self.heartbeat_loop())

        # 订阅企微通知频道
        r = await self.get_redis()
        pubsub = r.pubsub()
        notify_ch = config.channel("notify:wecom")
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
                            metadata={"topic": topic, "source": source}
                        )
                        await self.send(reply)
                except Exception as e:
                    logger.error(f"[{self.channel_name}] 处理通知失败: {e}")
        finally:
            await pubsub.unsubscribe(notify_ch)
            await self.cleanup()

    async def send(self, reply: ChannelReply) -> bool:
        """发送消息到企业微信"""
        # 优先使用 Webhook
        if WECOM_WEBHOOK_URL:
            return await self._send_webhook(reply)
        # 备选: 应用消息
        elif WECOM_CORP_ID and WECOM_SECRET:
            return await self._send_app_msg(reply)
        else:
            logger.warning(f"[{self.channel_name}] 未配置 Webhook 或应用凭证")
            return False

    async def _send_webhook(self, reply: ChannelReply) -> bool:
        """通过 Webhook 发送消息"""
        topic = reply.metadata.get("topic", "")
        source = reply.metadata.get("source", "")

        # Markdown 格式
        content = f"### Z-OPS 运维通知"
        if topic:
            content += f" [{topic}]"
        content += f"\n\n{reply.text[:2000]}"
        content += f"\n\n> 来源: {source} | {time.strftime('%Y-%m-%d %H:%M:%S')}"

        payload = {
            "msgtype": "markdown",
            "markdown": {"content": content}
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(WECOM_WEBHOOK_URL, json=payload)
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("errcode") == 0:
                        logger.info(f"[{self.channel_name}] Webhook 推送成功")
                        return True
                    else:
                        logger.error(f"[{self.channel_name}] Webhook 推送失败: {result}")
                else:
                    logger.error(f"[{self.channel_name}] HTTP 错误: {resp.status_code}")
        except Exception as e:
            logger.error(f"[{self.channel_name}] Webhook 推送异常: {e}")
        return False

    async def _get_access_token(self) -> str:
        """获取企业微信应用 access_token"""
        if self._access_token and time.time() < self._token_expire:
            return self._access_token

        try:
            url = (f"https://qyapi.weixin.qq.com/cgi-bin/gettoken"
                   f"?corpid={WECOM_CORP_ID}&corpsecret={WECOM_SECRET}")
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                data = resp.json()
                if data.get("errcode") == 0:
                    self._access_token = data["access_token"]
                    self._token_expire = time.time() + data.get("expires_in", 7200) - 300
                    return self._access_token
                else:
                    logger.error(f"[{self.channel_name}] 获取 token 失败: {data}")
        except Exception as e:
            logger.error(f"[{self.channel_name}] 获取 token 异常: {e}")
        return ""

    async def _send_app_msg(self, reply: ChannelReply) -> bool:
        """通过应用消息 API 发送"""
        token = await self._get_access_token()
        if not token:
            return False

        user_id = reply.metadata.get("user_id", "@all")

        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": int(WECOM_AGENT_ID) if WECOM_AGENT_ID else 0,
            "text": {"content": reply.text[:2048]}
        }

        try:
            url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                data = resp.json()
                if data.get("errcode") == 0:
                    logger.info(f"[{self.channel_name}] 应用消息发送成功")
                    return True
                else:
                    logger.error(f"[{self.channel_name}] 应用消息发送失败: {data}")
        except Exception as e:
            logger.error(f"[{self.channel_name}] 应用消息发送异常: {e}")
        return False

    async def stop(self):
        """停止企业微信通道"""
        self._running = False
        logger.info(f"[{self.channel_name}] 企业微信通道停止")


# ═══ 启动入口 ═══

def main():
    import signal

    channel = WecomChannel()
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
