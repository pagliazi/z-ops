"""
Z-OPS v2.0.0 — Telegram 通道插件
基于 BaseChannel 实现，使用 python-telegram-bot 库

配置 (zops.env):
  TELEGRAM_BOT_TOKEN=your-bot-token
  TELEGRAM_ALLOWED_CHAT_IDS=123456,789012  (可选，限制接受消息的 chat)
"""

import asyncio
import json
import time
import logging
from typing import Optional, Set

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.channel_base import BaseChannel, ChannelMessage, ChannelReply

logger = logging.getLogger("zops")

# Telegram 配置
TELEGRAM_BOT_TOKEN = config.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_CHATS = config.get("TELEGRAM_ALLOWED_CHAT_IDS", "")


class TelegramChannel(BaseChannel):
    """Telegram Bot 通道"""

    channel_name = "telegram"
    description = "Telegram Bot 消息通道"

    def __init__(self):
        super().__init__()
        self._bot = None
        self._allowed_chats: Set[int] = set()
        self._pending_replies = {}  # msg_id -> chat_id 映射

        # 解析允许的 chat IDs
        if TELEGRAM_ALLOWED_CHATS:
            for cid in TELEGRAM_ALLOWED_CHATS.split(","):
                try:
                    self._allowed_chats.add(int(cid.strip()))
                except ValueError:
                    pass

    async def start(self):
        """启动 Telegram Bot"""
        if not TELEGRAM_BOT_TOKEN:
            logger.warning(f"[{self.channel_name}] TELEGRAM_BOT_TOKEN 未配置，跳过启动")
            return

        self._running = True
        await self.register_heartbeat()
        logger.info(f"[{self.channel_name}] Telegram 通道启动")

        try:
            from telegram import Update
            from telegram.ext import Application, CommandHandler, MessageHandler, filters

            app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

            # 命令处理
            async def start_cmd(update: Update, context):
                await update.message.reply_text(
                    "Z-OPS 运维智能体 已连接。\n"
                    "直接发送消息即可进行运维操作。\n"
                    "例如: 检查主机状态 / 查询备份 / 帮我写脚本"
                )

            async def help_cmd(update: Update, context):
                await update.message.reply_text(
                    "Z-OPS 运维智能体命令:\n"
                    "/start - 开始\n"
                    "/help - 帮助\n"
                    "/status - 系统状态\n"
                    "直接发消息 - AI 运维助手"
                )

            async def status_cmd(update: Update, context):
                r = await self.get_redis()
                hb_data = await r.hgetall(config.KEY_HEARTBEATS)
                online = sum(1 for v in hb_data.values()
                            if json.loads(v).get("status") == "online")
                await update.message.reply_text(
                    f"Z-OPS 系统状态:\n"
                    f"Agent 在线: {online}/{len(hb_data)}"
                )

            # 消息处理
            async def handle_message(update: Update, context):
                chat_id = update.effective_chat.id
                user = update.effective_user

                # 权限检查
                if self._allowed_chats and chat_id not in self._allowed_chats:
                    await update.message.reply_text("未授权的 Chat，请联系管理员。")
                    return

                text = update.message.text
                if not text:
                    return

                sender = user.username or user.first_name or str(user.id)
                msg = ChannelMessage(
                    text=text,
                    sender=sender,
                    channel_name=self.channel_name,
                    channel_msg_id=str(update.message.message_id),
                    metadata={"chat_id": chat_id}
                )

                # 转发到 Orchestrator
                msg_id = await self.forward_to_orchestrator(msg)
                self._pending_replies[msg_id] = chat_id

                # 发送处理中提示
                await update.message.reply_text("处理中...")

            app.add_handler(CommandHandler("start", start_cmd))
            app.add_handler(CommandHandler("help", help_cmd))
            app.add_handler(CommandHandler("status", status_cmd))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

            # 心跳
            asyncio.create_task(self.heartbeat_loop())

            # 启动 polling
            await app.initialize()
            await app.start()
            await app.updater.start_polling()

            logger.info(f"[{self.channel_name}] Telegram Bot 已启动 polling")

            # 等待停止信号
            while self._running:
                await asyncio.sleep(1)

            await app.updater.stop()
            await app.stop()
            await app.shutdown()

        except ImportError:
            logger.error(f"[{self.channel_name}] 请安装: pip install python-telegram-bot")
        except Exception as e:
            logger.error(f"[{self.channel_name}] 启动失败: {e}")
        finally:
            await self.cleanup()

    async def send(self, reply: ChannelReply) -> bool:
        """发送消息到 Telegram"""
        if not TELEGRAM_BOT_TOKEN:
            return False

        try:
            import httpx
            chat_id = reply.metadata.get("chat_id")
            if not chat_id:
                return False

            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json={
                    "chat_id": chat_id,
                    "text": reply.text[:4096],  # Telegram 限制
                    "parse_mode": "Markdown"
                })
                if resp.status_code == 200:
                    logger.info(f"[{self.channel_name}] 消息已发送到 chat {chat_id}")
                    return True
                else:
                    logger.error(f"[{self.channel_name}] 发送失败: {resp.status_code}")
        except Exception as e:
            logger.error(f"[{self.channel_name}] 发送异常: {e}")
        return False

    async def stop(self):
        """停止 Telegram 通道"""
        self._running = False
        logger.info(f"[{self.channel_name}] Telegram 通道停止")


# ═══ 启动入口 ═══

def main():
    import signal

    channel = TelegramChannel()
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
