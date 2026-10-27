"""
Z-OPS v2.0.0 — 操作审计日志
记录谁/何时/调用了什么 Skill/结果如何

持久化策略:
- 内存: 最近 N 条 (快速查询)
- Redis: 按日存储 (zops:audit:YYYY-MM-DD)
- 文件: 可选持久化到 logs/audit/
"""

import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("zops")


@dataclass
class AuditEntry:
    """审计日志条目"""
    audit_id: str = ""           # 唯一ID
    timestamp: float = 0.0       # 时间戳
    agent_id: str = ""           # 执行 Agent
    action: str = ""             # 执行的 action/skill
    tool_name: str = ""          # 使用的工具
    username: str = ""           # 发起用户
    channel: str = ""            # 来源通道 (webchat/feishu/telegram)
    session_id: str = ""         # 会话ID
    params: Dict[str, Any] = field(default_factory=dict)   # 输入参数
    result_summary: str = ""     # 结果摘要
    success: bool = True         # 是否成功
    error: str = ""              # 错误信息
    duration_ms: float = 0.0     # 执行时长(毫秒)
    msg_id: str = ""             # 消息ID
    model: str = ""              # 调用的 LLM 模型 (provider/model)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.audit_id:
            import uuid
            self.audit_id = str(uuid.uuid4())[:12]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @property
    def time_str(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")


class AuditLogger:
    """
    操作审计日志管理器

    功能:
    1. 记录所有 Skill 调用的审计日志
    2. 持久化到 Redis (按日分片)
    3. 可选写入文件
    4. 提供查询接口
    """

    def __init__(self, redis_client=None, log_dir: str = None):
        self._redis = redis_client
        self._memory_log: List[AuditEntry] = []
        self._max_memory = 2000
        self._log_dir = Path(log_dir) if log_dir else None
        self._file_logging = False

        if self._log_dir:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            self._file_logging = True

    def set_redis(self, redis_client):
        """设置 Redis 客户端 (延迟初始化)"""
        self._redis = redis_client

    async def log(self,
                  agent_id: str,
                  action: str,
                  username: str = "",
                  channel: str = "",
                  session_id: str = "",
                  params: dict = None,
                  result_summary: str = "",
                  success: bool = True,
                  error: str = "",
                  duration_ms: float = 0.0,
                  msg_id: str = "",
                  tool_name: str = "",
                  model: str = "") -> AuditEntry:
        """
        记录审计日志

        Args:
            agent_id: 执行 Agent
            action: 执行的动作
            username: 发起用户
            channel: 来源通道
            session_id: 会话ID
            params: 输入参数 (自动脱敏)
            result_summary: 结果摘要
            success: 是否成功
            error: 错误信息
            duration_ms: 执行时长
            msg_id: 消息ID
            tool_name: 使用的工具

        Returns:
            AuditEntry
        """
        # 参数脱敏 (移除敏感字段)
        safe_params = self._sanitize_params(params or {})

        entry = AuditEntry(
            agent_id=agent_id,
            action=action,
            tool_name=tool_name,
            username=username or "anonymous",
            channel=channel or "unknown",
            session_id=session_id,
            params=safe_params,
            result_summary=result_summary[:500] if result_summary else "",
            success=success,
            error=error[:200] if error else "",
            duration_ms=duration_ms,
            msg_id=msg_id,
            model=model
        )

        # 内存记录
        self._memory_log.append(entry)
        if len(self._memory_log) > self._max_memory:
            self._memory_log = self._memory_log[-self._max_memory:]

        # Redis 持久化
        await self._persist_to_redis(entry)

        # 文件持久化
        self._persist_to_file(entry)

        # 日志输出
        status = "OK" if success else f"FAIL: {error}"
        logger.info(f"[audit] {entry.agent_id}.{entry.action} by {entry.username} "
                     f"via {entry.channel} — {status} ({entry.duration_ms:.0f}ms)")

        return entry

    async def _persist_to_redis(self, entry: AuditEntry):
        """持久化到 Redis"""
        if not self._redis:
            return
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import config

            date_str = datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d")
            key = f"{config.REDIS_PREFIX}:audit:{date_str}"
            await self._redis.rpush(key, entry.to_json())
            await self._redis.expire(key, 90 * 86400)  # 保留90天
        except Exception as e:
            logger.debug(f"[audit] Redis 持久化失败: {e}")

    def _persist_to_file(self, entry: AuditEntry):
        """持久化到文件"""
        if not self._file_logging or not self._log_dir:
            return
        try:
            date_str = datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d")
            log_file = self._log_dir / f"audit-{date_str}.jsonl"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(entry.to_json() + "\n")
        except Exception as e:
            logger.debug(f"[audit] 文件写入失败: {e}")

    @staticmethod
    def _sanitize_params(params: dict) -> dict:
        """参数脱敏 — 移除/遮盖敏感字段"""
        sensitive_keys = {"password", "pass", "secret", "token", "api_key", "key"}
        safe = {}
        for k, v in params.items():
            if any(s in k.lower() for s in sensitive_keys):
                safe[k] = "***"
            elif isinstance(v, str) and len(v) > 200:
                safe[k] = v[:200] + "...(truncated)"
            else:
                safe[k] = v
        return safe

    # ─── 查询接口 ───

    def get_recent(self, count: int = 50) -> List[AuditEntry]:
        """获取最近的审计记录"""
        return self._memory_log[-count:]

    def get_by_agent(self, agent_id: str, count: int = 50) -> List[AuditEntry]:
        """按 Agent 过滤"""
        return [e for e in self._memory_log if e.agent_id == agent_id][-count:]

    def get_by_user(self, username: str, count: int = 50) -> List[AuditEntry]:
        """按用户过滤"""
        return [e for e in self._memory_log if e.username == username][-count:]

    def get_by_channel(self, channel: str, count: int = 50) -> List[AuditEntry]:
        """按通道过滤"""
        return [e for e in self._memory_log if e.channel == channel][-count:]

    def get_errors(self, count: int = 50) -> List[AuditEntry]:
        """获取失败的操作"""
        return [e for e in self._memory_log if not e.success][-count:]

    def get_stats(self) -> dict:
        """获取审计统计"""
        total = len(self._memory_log)
        errors = sum(1 for e in self._memory_log if not e.success)
        agents = {}
        for e in self._memory_log:
            agents[e.agent_id] = agents.get(e.agent_id, 0) + 1
        return {
            "total": total,
            "errors": errors,
            "success_rate": round((total - errors) / max(total, 1) * 100, 1),
            "by_agent": agents
        }

    async def query_redis(self, date_str: str = None, count: int = 100) -> List[dict]:
        """从 Redis 查询历史审计记录"""
        if not self._redis:
            return []
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import config

            if not date_str:
                date_str = datetime.now().strftime("%Y-%m-%d")
            key = f"{config.REDIS_PREFIX}:audit:{date_str}"
            raw_list = await self._redis.lrange(key, -count, -1)
            return [json.loads(r) for r in raw_list]
        except Exception as e:
            logger.debug(f"[audit] Redis 查询失败: {e}")
            return []


# ═══ 全局单例 ═══

_audit_logger: Optional[AuditLogger] = None


def get_audit_logger(log_dir: str = None) -> AuditLogger:
    """获取全局 AuditLogger 单例"""
    global _audit_logger
    if _audit_logger is None:
        if log_dir is None:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import config
            log_dir = str(config.BASE_DIR / "logs" / "audit")
        _audit_logger = AuditLogger(log_dir=log_dir)
    return _audit_logger
