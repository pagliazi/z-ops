"""
Z-OPS ExecutionRegistry — 全局工具执行追踪
参考 claw-code execution_registry.py + claude-code cost-tracker.ts

核心能力:
1. 中央化记录所有工具/Agent 执行 (tool calls, agent forwards, LLM calls)
2. 按 session/agent/tool 多维查询执行历史
3. 性能指标聚合 (P50/P95 延迟, 成功率, token 用量)
4. 自动清理过期记录 (TTL 管理)
5. 支持 Harness 失败分析 (为自动技能生成提供数据源)
"""

import json
import time
import logging
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
from enum import Enum

logger = logging.getLogger("zops")


class ExecutionType(str, Enum):
    TOOL_CALL = "tool_call"         # 工具调用
    LLM_CALL = "llm_call"          # LLM 调用
    AGENT_FORWARD = "agent_forward"  # Agent 转发
    SKILL_EXEC = "skill_exec"       # 技能执行
    KB_SEARCH = "kb_search"         # 知识库搜索
    AGENT_MESSAGE = "agent_message"  # Agent 处理一条用户消息 (消息级口径, 与工具级区分)


@dataclass
class ExecutionRecord:
    """单次执行记录"""
    exec_id: str = ""
    exec_type: str = "tool_call"
    agent_id: str = ""
    tool_name: str = ""
    session_id: str = ""
    username: str = ""
    # 输入输出
    input_summary: str = ""     # 输入摘要 (截断)
    output_summary: str = ""    # 输出摘要 (截断)
    # 状态
    success: bool = True
    error: str = ""
    # 性能
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    # 时间
    started_at: float = 0.0
    completed_at: float = 0.0
    # 上下文
    model: str = ""
    provider: str = ""
    parent_exec_id: str = ""    # 父级执行 ID (嵌套调用追踪)
    # v4.0 (Phase 6): trace_id 贯穿全链 (msg_id → 路由 → agent → tool call 一个 ID)
    trace_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.exec_id:
            self.exec_id = str(uuid.uuid4())[:16]
        if not self.started_at:
            self.started_at = time.time()

    def complete(self, success: bool = True, output: str = "", error: str = ""):
        self.success = success
        self.output_summary = output[:500]
        self.error = error
        self.completed_at = time.time()
        self.latency_ms = (self.completed_at - self.started_at) * 1000

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}


class ExecutionRegistry:
    """
    全局执行追踪注册表
    
    参考:
    - claw-code execution_registry.py: 中央化执行追踪
    - claude-code cost-tracker.ts: token 用量追踪
    
    数据存储在 Redis 中:
    - zops:executions:{date} — 按日存储执行记录 (list)
    - zops:exec_stats:{agent_id} — 按 Agent 聚合统计 (hash)
    - zops:exec_failures — 最近失败记录 (用于 Harness 自动技能生成)
    """
    
    REDIS_PREFIX = "zops"
    EXEC_TTL = 7 * 86400       # 执行记录保留 7 天
    FAILURE_MAX = 500           # 最近失败记录最多 500 条
    STATS_TTL = 30 * 86400     # 统计数据保留 30 天

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._local_buffer: List[ExecutionRecord] = []
        self._buffer_max = 100

    async def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            import config
            self._redis = aioredis.from_url(
                config.REDIS_URL, decode_responses=True, max_connections=5
            )
        return self._redis

    # ─── 记录 ───

    async def record(self, rec: ExecutionRecord):
        """记录一次执行"""
        try:
            r = await self._get_redis()
            from datetime import datetime
            date_key = f"{self.REDIS_PREFIX}:executions:{datetime.now().strftime('%Y-%m-%d')}"
            
            entry = json.dumps(rec.to_dict(), ensure_ascii=False)
            await r.rpush(date_key, entry)
            await r.expire(date_key, self.EXEC_TTL)

            # v4.0 (Phase 6): trace_id 索引 — 同一请求链 (msg_id → 路由 → agent → tool) 聚合查询
            if rec.trace_id:
                trace_key = f"{self.REDIS_PREFIX}:exec_trace:{rec.trace_id}"
                await r.rpush(trace_key, entry)
                await r.expire(trace_key, self.EXEC_TTL)
            
            # 更新 Agent 聚合统计
            stats_key = f"{self.REDIS_PREFIX}:exec_stats:{rec.agent_id}"
            await r.hincrby(stats_key, "total_calls", 1)
            await r.hincrby(stats_key, "success_calls" if rec.success else "failed_calls", 1)
            await r.hincrby(stats_key, "total_input_tokens", rec.input_tokens)
            await r.hincrby(stats_key, "total_output_tokens", rec.output_tokens)
            await r.hincrbyfloat(stats_key, "total_latency_ms", rec.latency_ms)
            await r.expire(stats_key, self.STATS_TTL)

            # 口径拆分: agent_message = 处理一条用户消息; tool_call = 真实工具执行;
            # agent_forward/llm_call 等另计 other。修复"工具池调用 vs Agent 调用数据对不上": 同源分维度
            if rec.exec_type == ExecutionType.AGENT_MESSAGE:
                await r.hincrby(stats_key, "msg_calls", 1)
            elif rec.exec_type == ExecutionType.TOOL_CALL:
                await r.hincrby(stats_key, "tool_calls", 1)
            else:
                await r.hincrby(stats_key, "other_calls", 1)

            # 工具级热度: per-tool 调用计数 (仅真实工具执行; 路由转发不算)
            if rec.exec_type == ExecutionType.TOOL_CALL and rec.tool_name:
                tool_stats_key = f"{self.REDIS_PREFIX}:exec_stats_tools:{rec.agent_id}"
                await r.hincrby(tool_stats_key, f"calls:{rec.tool_name}", 1)
                if not rec.success:
                    await r.hincrby(tool_stats_key, f"fails:{rec.tool_name}", 1)
                await r.expire(tool_stats_key, self.STATS_TTL)
            
            # 失败记录 → 用于 Harness 自动技能生成
            if not rec.success and rec.error:
                fail_key = f"{self.REDIS_PREFIX}:exec_failures"
                await r.rpush(fail_key, entry)
                await r.ltrim(fail_key, -self.FAILURE_MAX, -1)
                
                # 触发 Harness 学习: 异步触发技能改进检查
                try:
                    from core.harness.skill_improver import get_skill_improver, Feedback
                    improver = await get_skill_improver()
                    # 用 agent_id 作为 skill_name (简化映射)
                    skill_name = rec.agent_id.replace("_agent", "")
                    fb = Feedback(
                        feedback_id=f"fb_{uuid.uuid4().hex[:8]}",
                        skill_name=skill_name,
                        session_id=rec.session_id or "",
                        user_id="",
                        timestamp=datetime.now().isoformat(),
                        success=False,
                        execution_time=rec.latency_ms,
                        error_type=rec.error[:50] if rec.error else "unknown",
                        error_message=rec.error[:200] if rec.error else "",
                        context={"agent_id": rec.agent_id, "tool_name": rec.tool_name}
                    )
                    await improver.self_refine_on_failure(skill_name, fb)
                except Exception as harness_err:
                    logger.debug(f"[exec_registry] Harness 触发失败: {harness_err}")
            
        except Exception as e:
            logger.debug(f"[exec_registry] 记录失败: {e}")
            self._local_buffer.append(rec)
            if len(self._local_buffer) > self._buffer_max:
                self._local_buffer = self._local_buffer[-50:]

    async def record_tool_call(self, agent_id: str, tool_name: str,
                                input_summary: str, output_summary: str,
                                success: bool, latency_ms: float,
                                session_id: str = "", error: str = "",
                                trace_id: str = "",
                                exec_type: ExecutionType = ExecutionType.TOOL_CALL,
                                **kwargs) -> ExecutionRecord:
        """便捷方法: 记录工具调用 (exec_type 可标记为 AGENT_MESSAGE 消息级口径)"""
        rec = ExecutionRecord(
            exec_type=exec_type,
            agent_id=agent_id,
            tool_name=tool_name,
            input_summary=input_summary[:300],
            output_summary=output_summary[:500],
            success=success,
            error=error,
            latency_ms=latency_ms,
            session_id=session_id,
            trace_id=trace_id,
            **kwargs
        )
        rec.completed_at = time.time()
        await self.record(rec)
        return rec

    async def record_llm_call(self, agent_id: str, model: str, provider: str,
                               input_tokens: int, output_tokens: int,
                               latency_ms: float, success: bool = True,
                               session_id: str = "", error: str = "",
                               **kwargs) -> ExecutionRecord:
        """便捷方法: 记录 LLM 调用"""
        rec = ExecutionRecord(
            exec_type=ExecutionType.LLM_CALL,
            agent_id=agent_id,
            model=model,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            success=success,
            error=error,
            latency_ms=latency_ms,
            session_id=session_id,
            **kwargs
        )
        rec.completed_at = time.time()
        await self.record(rec)
        return rec

    # ─── 查询 ───

    async def get_recent_failures(self, limit: int = 50) -> List[dict]:
        """获取最近的失败记录 (供 Harness 分析)"""
        try:
            r = await self._get_redis()
            entries = await r.lrange(f"{self.REDIS_PREFIX}:exec_failures", -limit, -1)
            return [json.loads(e) for e in entries]
        except Exception as e:
            logger.debug(f"[exec_registry] 获取失败记录异常: {e}")
            return []

    async def get_agent_stats(self, agent_id: str) -> dict:
        """获取 Agent 聚合统计"""
        try:
            r = await self._get_redis()
            stats = await r.hgetall(f"{self.REDIS_PREFIX}:exec_stats:{agent_id}")
            if not stats:
                return {}
            total = int(stats.get("total_calls", 0))
            success = int(stats.get("success_calls", 0))
            return {
                "total_calls": total,
                "success_calls": success,
                "failed_calls": int(stats.get("failed_calls", 0)),
                "success_rate": round(success / max(total, 1) * 100, 1),
                "total_input_tokens": int(stats.get("total_input_tokens", 0)),
                "total_output_tokens": int(stats.get("total_output_tokens", 0)),
                "avg_latency_ms": round(
                    float(stats.get("total_latency_ms", 0)) / max(total, 1), 1
                ),
                # 口径拆分 (旧数据无此字段时为 0): 消息级 vs 真实工具调用
                "msg_calls": int(stats.get("msg_calls", 0)),
                "tool_calls": int(stats.get("tool_calls", 0)),
            }
        except Exception as e:
            logger.debug(f"[exec_registry] 获取统计异常: {e}")
            return {}

    async def get_agent_tool_stats(self, agent_id: str, top: int = 50) -> List[dict]:
        """获取 Agent 的 per-tool 调用统计 (工具池页真实调用热度)"""
        try:
            r = await self._get_redis()
            raw = await r.hgetall(f"{self.REDIS_PREFIX}:exec_stats_tools:{agent_id}")
            if not raw:
                return []
            merged: Dict[str, dict] = {}
            for field, val in raw.items():
                kind, _, tool = field.partition(":")
                if not tool:
                    continue
                item = merged.setdefault(tool, {"tool_name": tool, "calls": 0, "fails": 0})
                try:
                    n = int(val)
                except (TypeError, ValueError):
                    continue
                if kind == "calls":
                    item["calls"] += n
                elif kind == "fails":
                    item["fails"] += n
            result = sorted(merged.values(), key=lambda x: -x["calls"])[:top]
            for item in result:
                item["fail_rate"] = round(item["fails"] / max(item["calls"], 1) * 100, 1)
            return result
        except Exception as e:
            logger.debug(f"[exec_registry] 获取工具统计异常: {e}")
            return []

    async def get_daily_executions(self, date_str: str = None,
                                    agent_id: str = "", limit: int = 100,
                                    trace_id: str = "") -> List[dict]:
        """获取某日的执行记录 (v4.0: 支持 trace_id 过滤)"""
        try:
            if trace_id:
                return await self.get_trace_executions(trace_id, limit)
            from datetime import datetime
            r = await self._get_redis()
            date_str = date_str or datetime.now().strftime("%Y-%m-%d")
            key = f"{self.REDIS_PREFIX}:executions:{date_str}"
            entries = await r.lrange(key, -limit, -1)
            results = [json.loads(e) for e in entries]
            if agent_id:
                results = [r for r in results if r.get("agent_id") == agent_id]
            return results
        except Exception:
            return []

    async def get_trace_executions(self, trace_id: str,
                                   limit: int = 100) -> List[dict]:
        """按 trace_id 聚合全链执行记录 (msg_id → 路由 → agent → tool call)"""
        try:
            r = await self._get_redis()
            entries = await r.lrange(f"{self.REDIS_PREFIX}:exec_trace:{trace_id}",
                                     -limit, -1)
            return [json.loads(e) for e in entries]
        except Exception:
            return []


# ═══ 全局单例 ═══

_registry: Optional[ExecutionRegistry] = None

def get_execution_registry() -> ExecutionRegistry:
    global _registry
    if _registry is None:
        _registry = ExecutionRegistry()
    return _registry
