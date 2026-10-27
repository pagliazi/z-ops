"""
Z-OPS v2.0.0 — Channel 抽象层
统一通道接口，所有消息通道 (webchat/feishu/telegram/wecom) 实现此基类

Channel 生命周期:
  start() → 监听消息 → receive() → 转发到 Orchestrator → send() 回复 → stop()
"""

import asyncio
import json
import time
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from dataclasses import dataclass, field

import redis.asyncio as aioredis

logger = logging.getLogger("zops")


@dataclass
class ChannelMessage:
    """通道消息 — 从外部通道接收的原始消息"""
    text: str = ""                  # 消息文本
    sender: str = ""                # 发送者标识
    channel_name: str = ""          # 通道名称
    channel_msg_id: str = ""        # 通道内的消息ID
    metadata: Dict[str, Any] = field(default_factory=dict)  # 通道特有元数据
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class ChannelReply:
    """通道回复 — 发送回外部通道的消息"""
    text: str = ""
    channel_msg_id: str = ""        # 对应的原始消息ID
    reply_type: str = "text"        # text / card / image / file
    metadata: Dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str = ""


class BaseChannel(ABC):
    """
    通道基类 — 所有消息通道的统一接口

    子类需要:
    1. 设置 channel_name
    2. 实现 start() — 启动通道监听
    3. 实现 send() — 发送消息到通道
    4. 实现 stop() — 停止通道
    """

    channel_name: str = "base"
    description: str = ""

    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None
        self._running = False
        self._config: Dict[str, Any] = {}

    async def get_redis(self) -> aioredis.Redis:
        """获取 Redis 连接"""
        if self._redis is None:
            import sys
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import config
            self._redis = aioredis.from_url(config.REDIS_URL, decode_responses=True)
        return self._redis

    # ─── 抽象方法 (子类必须实现) ───

    @abstractmethod
    async def start(self):
        """启动通道 — 开始监听外部消息"""
        pass

    @abstractmethod
    async def send(self, reply: ChannelReply) -> bool:
        """发送消息到外部通道"""
        pass

    @abstractmethod
    async def stop(self):
        """停止通道"""
        pass

    # ─── 通用方法 ───

    async def forward_to_orchestrator(self, msg: ChannelMessage):
        """将通道消息转发到 Orchestrator"""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import config
        from agents.base import AgentMessage

        r = await self.get_redis()
        agent_msg = AgentMessage(
            source=self.channel_name,
            target="orchestrator",
            text=msg.text,
            channel_source=self.channel_name,
            username=msg.sender,
            params=msg.metadata
        )

        orch_ch = config.channel("orchestrator")
        await r.publish(orch_ch, agent_msg.to_json())
        logger.info(f"[{self.channel_name}] 转发到 orchestrator: {msg.text[:50]}")
        return agent_msg.msg_id

    async def register_heartbeat(self):
        """注册通道心跳"""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import config

        r = await self.get_redis()
        await r.hset(config.KEY_CHANNEL_HEARTBEATS, self.channel_name, json.dumps({
            "name": self.channel_name,
            "status": "online",
            "started": time.time(),
            "last_heartbeat": time.time()
        }))

    async def heartbeat_loop(self):
        """心跳循环"""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import config

        while self._running:
            try:
                r = await self.get_redis()
                await r.hset(config.KEY_CHANNEL_HEARTBEATS, self.channel_name, json.dumps({
                    "name": self.channel_name,
                    "status": "online",
                    "last_heartbeat": time.time()
                }))
            except Exception as e:
                logger.error(f"[{self.channel_name}] 心跳更新失败: {e}")
            await asyncio.sleep(10)

    async def cleanup(self):
        """清理资源"""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import config

        try:
            r = await self.get_redis()
            await r.hset(config.KEY_CHANNEL_HEARTBEATS, self.channel_name, json.dumps({
                "name": self.channel_name,
                "status": "offline",
                "stopped": time.time()
            }))
        except Exception:
            pass
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    def __repr__(self):
        return f"<Channel:{self.channel_name}>"


# ═══ Channel 注册表 ═══

_channels: Dict[str, BaseChannel] = {}


def register_channel(channel: BaseChannel):
    """注册一个通道"""
    _channels[channel.channel_name] = channel
    logger.info(f"[channels] 注册通道: {channel.channel_name}")


def get_channel(name: str) -> Optional[BaseChannel]:
    """获取指定通道"""
    return _channels.get(name)


def get_all_channels() -> Dict[str, BaseChannel]:
    """获取所有注册的通道"""
    return dict(_channels)
