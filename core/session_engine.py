"""
Z-OPS 会话引擎 — SessionEngine
参考 claude-code compact 系统 + claw-code QueryEnginePort 统一设计

核心能力:
1. 统一会话上下文加载 (从 Redis 读取历史, 构建上下文)
2. 自动压缩 (token 预估 → 阈值触发 → LLM 摘要 → 替换旧消息)
3. Micro-compact (截断旧工具结果, 保留最新)
4. Token 预算追踪 (per-session 用量统计)
5. 上下文构建 (为 orchestrator/agent 生成结构化历史)
"""

import json
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")


# ═══ Token 精确计数 (tiktoken) ═══

try:
    import tiktoken
    # cl100k_base 兼容 GPT-4/Qwen/DeepSeek 等主流模型，误差 <5%
    _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
    _USE_TIKTOKEN = True
    logger.info("[session_engine] tiktoken 已加载 (cl100k_base), 精确 token 计数")
except ImportError:
    _TIKTOKEN_ENC = None
    _USE_TIKTOKEN = False
    logger.warning("[session_engine] tiktoken 未安装, 使用启发式估算")


def count_tokens(text: str) -> int:
    """精确 token 计数 (tiktoken cl100k_base)
    tiktoken 可用时使用精确编码, 否则 fallback 到启发式估算
    性能: ~0.1ms/千字, 零网络开销"""
    if not text:
        return 0
    if _USE_TIKTOKEN:
        return len(_TIKTOKEN_ENC.encode(text))
    # Fallback: 启发式 (中文 ~1.5 char/tok, 英文 ~4 char/tok)
    chinese_count = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    total = len(text)
    if total == 0:
        return 0
    chinese_ratio = chinese_count / total
    chars_per_token = 1.5 * chinese_ratio + 4.0 * (1 - chinese_ratio)
    return int(total / max(chars_per_token, 1.0) * 1.33)


# 向后兼容别名
estimate_tokens = count_tokens


def count_messages_tokens(messages: List[dict]) -> int:
    """精确计算一组消息的总 token 数 (含 role/meta 开销)"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        total += count_tokens(content)
        # role + 分隔符 token 开销 (~4 tokens)
        total += 4
    # 回复启动 token
    total += 3
    return total


# 向后兼容别名
estimate_messages_tokens = count_messages_tokens


# ═══ Compact Prompt (参考 claude-code prompt.ts 的 9 节结构) ═══

COMPACT_SYSTEM_PROMPT = """你是 Z-OPS 运维智能体的对话压缩模块。
你的任务是将一段对话历史压缩为精炼的结构化摘要，保留所有关键技术细节。

要求:
- 只输出纯文本摘要，不要使用 JSON 或代码块
- 必须保留: 用户的所有请求意图、涉及的具体 GID/IP/服务器/站点名称、查询结果的关键数据
- 必须保留: agent 路由路径(哪个 agent 处理了什么)、API 查询的结果摘要
- 必须保留: 出现过的错误和修复方法、用户的反馈和纠正
- 删除冗余: 重复的问候语、格式化细节、过长的原始 API 响应数据
- 语言: 中文"""

COMPACT_USER_PROMPT_TEMPLATE = """请将以下对话历史压缩为结构化摘要:

{conversation}

输出格式:
1. 用户请求摘要: [列出用户的所有请求]
2. 关键数据: [涉及的 GID/IP/站点/服务器等具体信息]
3. 查询结果: [各 agent 返回的关键数据摘要]
4. 错误与修复: [出现的问题和解决方法]
5. 当前状态: [最后一轮对话在做什么]"""


# ═══ Micro-Compact (参考 claude-code microCompact.ts) ═══

MICRO_COMPACT_STUB = "[旧数据已压缩]"  # 参考 claude-code 的 '[Old tool result content cleared]'


def micro_compact_messages(messages: List[dict], current_turn: int = 0) -> List[dict]:
    """截断旧的工具结果/API 响应内容, 保留最近的
    
    参考 claude-code microCompact.ts:
    - 只压缩 assistant 消息中超长的 API 数据/工具结果
    - 保留最近 MICRO_COMPACT_AGE 轮的完整内容
    - 被压缩的内容替换为摘要前缀 + '[旧数据已压缩]'
    """
    if not messages:
        return messages
    
    age_threshold = config.MICRO_COMPACT_AGE
    max_chars = config.MICRO_COMPACT_MAX_CHARS
    result = []
    
    total_messages = len(messages)
    for i, msg in enumerate(messages):
        # 最近的消息保持不变
        turns_ago = total_messages - i
        if turns_ago <= age_threshold:
            result.append(msg)
            continue
        
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        # 只压缩 assistant 的长回复 (通常包含 API 数据)
        if role == "assistant" and len(content) > max_chars * 2:
            # 保留开头的摘要部分
            truncated = content[:max_chars]
            # 在合理位置截断 (句号/换行)
            for sep in ["\n\n", "\n", "。", ". "]:
                last_sep = truncated.rfind(sep)
                if last_sep > max_chars // 2:
                    truncated = truncated[:last_sep + len(sep)]
                    break
            
            result.append({
                **msg,
                "content": truncated + f"\n\n{MICRO_COMPACT_STUB}",
                "_micro_compacted": True,
                "_original_length": len(content),
            })
            logger.debug(f"[session_engine] micro-compact: {len(content)} -> {len(truncated)} chars")
        else:
            result.append(msg)
    
    return result


# ═══ SessionEngine ═══

@dataclass
class SessionTokenUsage:
    """会话 token 用量追踪 (参考 claude-code cost-tracker + claw-code UsageSummary)"""
    session_id: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    compact_count: int = 0              # 压缩次数
    micro_compact_count: int = 0        # micro-compact 次数
    consecutive_compact_failures: int = 0  # 连续压缩失败次数 (断路器)
    last_compact_time: float = 0.0
    last_estimated_tokens: int = 0      # 最近一次 token 估算
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d: dict) -> "SessionTokenUsage":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class SessionEngine:
    """
    统一会话管理引擎
    
    职责:
    1. 从 Redis 加载会话历史
    2. 执行 micro-compact (截断旧工具结果)
    3. 检查是否需要 auto-compact (token 预估超阈值)
    4. 执行 compact (调用 LLM 生成摘要, 替换旧消息)
    5. 构建适合 orchestrator/agent 的上下文
    6. 追踪 token 使用量
    
    参考:
    - claude-code: autoCompact.ts (阈值判断) + compact.ts (LLM摘要) + microCompact.ts (结果截断)
    - claw-code: QueryEnginePort (统一引擎) + TranscriptStore (compact/replay)
    """
    
    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._usage_cache: Dict[str, SessionTokenUsage] = {}
    
    # ─── Redis ───
    
    async def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                config.REDIS_URL, decode_responses=True, max_connections=5
            )
        return self._redis
    
    # ─── 会话历史加载 ───
    
    async def load_session_messages(self, session_id: str,
                                     max_count: int = 0) -> List[dict]:
        """从 Redis 加载会话消息列表
        
        Args:
            session_id: 会话ID
            max_count: 最大加载数量 (0=全部)
        
        Returns:
            消息列表 [{"role": "user"|"assistant", "content": "...", "source": "...", ...}]
        """
        r = await self._get_redis()
        key = config.key_session_msgs(session_id)
        
        if max_count > 0:
            raw_list = await r.lrange(key, -max_count, -1)
        else:
            raw_list = await r.lrange(key, 0, -1)
        
        messages = []
        for raw in raw_list:
            try:
                msg = json.loads(raw)
                messages.append(msg)
            except (json.JSONDecodeError, TypeError):
                continue
        
        return messages
    
    # ─── Token 估算与阈值判断 ───
    
    def estimate_session_tokens(self, messages: List[dict]) -> int:
        """估算会话消息总 token 数"""
        return estimate_messages_tokens(messages)
    
    def should_auto_compact(self, messages: List[dict],
                             session_id: str = "") -> bool:
        """判断是否需要自动压缩
        
        参考 claude-code autoCompact.ts:
        - effectiveContextWindow = contextWindow - reservedForSummary
        - threshold = effectiveContextWindow - AUTOCOMPACT_BUFFER_TOKENS
        - 连续失败超限 → 断路器停止
        
        Z-OPS 简化版:
        - threshold = CONTEXT_WINDOW_TOKENS * AUTO_COMPACT_THRESHOLD
        - 消息数 >= COMPACT_MIN_MESSAGES
        - 连续失败 < COMPACT_CONSECUTIVE_FAIL_MAX
        """
        # 消息数门槛
        if len(messages) < config.COMPACT_MIN_MESSAGES:
            return False
        
        # 断路器: 连续失败太多次就停止
        usage = self._get_usage(session_id)
        if usage.consecutive_compact_failures >= config.COMPACT_CONSECUTIVE_FAIL_MAX:
            logger.warning(f"[session_engine] 断路器: session={session_id}, "
                          f"连续失败{usage.consecutive_compact_failures}次, 停止 auto-compact")
            return False
        
        # Token 阈值
        estimated = self.estimate_session_tokens(messages)
        usage.last_estimated_tokens = estimated
        threshold = int(config.CONTEXT_WINDOW_TOKENS * config.AUTO_COMPACT_THRESHOLD)
        
        should_compact = estimated >= threshold
        if should_compact:
            logger.info(f"[session_engine] auto-compact 触发: "
                       f"estimated={estimated} >= threshold={threshold} "
                       f"(window={config.CONTEXT_WINDOW_TOKENS}, "
                       f"ratio={config.AUTO_COMPACT_THRESHOLD})")
        
        return should_compact
    
    # ─── Auto-Compact 执行 ───
    
    async def auto_compact_if_needed(self, session_id: str,
                                      llm_chat_fn=None) -> bool:
        """检查并执行自动压缩
        
        参考 claude-code autoCompactIfNeeded():
        1. shouldAutoCompact() → 判断是否需要
        2. micro-compact 先截断旧工具结果
        3. compactConversation() → LLM 生成摘要
        4. 替换 Redis 中的消息列表
        5. postCompactCleanup()
        
        Args:
            session_id: 会话ID
            llm_chat_fn: LLM 调用函数 async (prompt, system, temperature) -> str
        
        Returns:
            是否执行了压缩
        """
        messages = await self.load_session_messages(session_id)
        
        if not self.should_auto_compact(messages, session_id):
            return False
        
        logger.info(f"[session_engine] 开始 auto-compact: session={session_id}, "
                    f"messages={len(messages)}")
        
        usage = self._get_usage(session_id)
        
        try:
            # Step 1: Micro-compact (截断旧工具结果)
            messages = micro_compact_messages(messages)
            micro_compacted = sum(1 for m in messages if m.get("_micro_compacted"))
            if micro_compacted:
                usage.micro_compact_count += micro_compacted
                logger.info(f"[session_engine] micro-compact: {micro_compacted} 条消息被截断")
            
            # 再次检查: micro-compact 后可能已经低于阈值
            if not self.should_auto_compact(messages, session_id):
                # 仅保存 micro-compact 结果
                await self._save_messages(session_id, messages)
                logger.info("[session_engine] micro-compact 后 token 已降到阈值下, 跳过 full compact")
                return True
            
            # Step 2: Full compact — LLM 生成摘要
            if llm_chat_fn is None:
                logger.warning("[session_engine] 无 llm_chat_fn, 跳过 full compact")
                return False
            
            # 保留最近 N 条原始消息
            preserve_count = config.COMPACT_PRESERVE_RECENT
            if len(messages) <= preserve_count + 2:
                logger.info("[session_engine] 消息太少, 无法有效压缩")
                return False
            
            messages_to_compact = messages[:-preserve_count] if preserve_count > 0 else messages
            preserved_messages = messages[-preserve_count:] if preserve_count > 0 else []
            
            # 构建对话文本
            conversation_text = self._format_conversation_for_compact(messages_to_compact)
            
            # 调用 LLM 生成摘要
            compact_prompt = COMPACT_USER_PROMPT_TEMPLATE.format(
                conversation=conversation_text[:20000]  # 限制输入长度
            )
            
            summary = await llm_chat_fn(
                prompt=compact_prompt,
                system=COMPACT_SYSTEM_PROMPT,
                temperature=0.1
            )
            
            if not summary or len(summary) < 50:
                raise ValueError(f"压缩摘要过短: {len(summary) if summary else 0} chars")
            
            # Step 3: 构建新的消息列表 = [compact_boundary] + preserved_messages
            compact_boundary = {
                "role": "system",
                "content": summary,
                "source": "compact",
                "timestamp": time.time(),
                "_compact_meta": {
                    "original_count": len(messages_to_compact),
                    "preserved_count": len(preserved_messages),
                    "compact_time": time.time(),
                    "estimated_tokens_before": usage.last_estimated_tokens,
                }
            }
            
            new_messages = [compact_boundary] + preserved_messages
            
            # Step 4: 写回 Redis + transcript 边界事件 (Phase 3.3: jsonl 原始历史永不改写)
            await self._save_messages(session_id, new_messages)
            try:
                from core import transcript
                anchor = await transcript.append_compact_boundary(
                    session_id, summary, replaced_count=len(messages_to_compact))
                logger.info(f"[session_engine] compact_boundary 已写 transcript "
                            f"(anchor={anchor[:8]}, replaced={len(messages_to_compact)})")
            except Exception as te:
                logger.debug(f"[session_engine] transcript 边界写入失败 (不阻断): {te}")
            
            # 更新统计
            usage.compact_count += 1
            usage.consecutive_compact_failures = 0
            usage.last_compact_time = time.time()
            
            new_tokens = self.estimate_session_tokens(new_messages)
            logger.info(f"[session_engine] compact 完成: "
                       f"{len(messages)} -> {len(new_messages)} msgs, "
                       f"tokens: {usage.last_estimated_tokens} -> {new_tokens}, "
                       f"摘要长度: {len(summary)} chars")
            
            return True
            
        except Exception as e:
            usage.consecutive_compact_failures += 1
            logger.error(f"[session_engine] compact 失败 (第{usage.consecutive_compact_failures}次): {e}")
            return False
    
    # ─── 上下文构建 ───
    
    async def build_context_for_routing(self, session_id: str,
                                         current_text: str = "") -> List[dict]:
        """为 orchestrator 意图识别构建上下文
        
        返回最近 N 条消息 (去掉当前消息), 用于路由决策
        """
        messages = await self.load_session_messages(
            session_id, max_count=config.CONTEXT_WINDOW_SIZE + 1
        )
        
        # 去掉最后一条 (当前用户消息, 已包含在 msg.text)
        if messages and messages[-1].get("role") == "user":
            content = messages[-1].get("content", "")
            if current_text and content and (
                content.strip() == current_text.strip() or
                content.strip()[:50] == current_text.strip()[:50]
            ):
                messages = messages[:-1]
        
        # 构建标准化历史
        history = []
        for msg in messages[-config.CONTEXT_WINDOW_SIZE:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            source = msg.get("source", "")
            
            history.append({
                "role": role,
                "content": content[:500] if role == "assistant" else content[:300],
                "source": source,
            })
        
        return history
    
    async def build_context_for_agent(self, session_id: str,
                                       current_text: str = "") -> str:
        """为 agent LLM 调用构建上下文前缀
        
        返回格式化的对话历史字符串, agent 可以直接拼接到 prompt 前面
        """
        history = await self.build_context_for_routing(session_id, current_text)
        
        if not history:
            return ""
        
        lines = []
        for h in history[-6:]:  # 最多 6 条
            role = h.get("role", "user")
            content = h.get("content", "")[:300]
            source = h.get("source", "")
            if role == "user":
                lines.append(f"用户: {content}")
            elif role == "system" and source == "compact":
                lines.append(f"[对话摘要]: {content[:200]}")
            else:
                agent_hint = f" (via {source})" if source else ""
                lines.append(f"助手{agent_hint}: {content}")
        
        return "以下是之前的对话记录:\n" + "\n".join(lines) + "\n\n"
    
    # ─── 辅助方法 ───
    
    def _get_usage(self, session_id: str) -> SessionTokenUsage:
        """获取或创建 session token 用量追踪"""
        if session_id not in self._usage_cache:
            self._usage_cache[session_id] = SessionTokenUsage(session_id=session_id)
        return self._usage_cache[session_id]
    
    async def _save_messages(self, session_id: str, messages: List[dict]):
        """将消息列表写回 Redis (覆盖)"""
        r = await self._get_redis()
        key = config.key_session_msgs(session_id)
        
        pipe = r.pipeline()
        pipe.delete(key)
        for msg in messages:
            # 清除内部标记
            clean_msg = {k: v for k, v in msg.items() if not k.startswith("_")}
            pipe.rpush(key, json.dumps(clean_msg, ensure_ascii=False))
        if config.SESSION_TTL > 0:
            pipe.expire(key, config.SESSION_TTL)
        else:
            pipe.persist(key)  # 会话永久保留，清除可能残留的过期时间
        await pipe.execute()
    
    def _format_conversation_for_compact(self, messages: List[dict]) -> str:
        """格式化消息列表为可读对话文本 (用于 LLM compact)
        
        参考 claude-code compact.ts: 按 API round 分组, 去除图片等二进制内容
        """
        lines = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            source = msg.get("source", "")
            
            if role == "system" and source == "compact":
                lines.append(f"[前轮摘要]: {content}")
            elif role == "user":
                lines.append(f"用户: {content}")
            elif role == "assistant":
                # 截断过长的 API 数据
                if len(content) > 3000:
                    content = content[:3000] + "\n...[数据截断]"
                agent_hint = f" ({source})" if source else ""
                lines.append(f"助手{agent_hint}: {content}")
            else:
                lines.append(f"[{role}]: {content[:500]}")
        
        return "\n\n".join(lines)
    
    async def get_session_stats(self, session_id: str) -> dict:
        """获取会话统计信息"""
        messages = await self.load_session_messages(session_id)
        usage = self._get_usage(session_id)
        estimated_tokens = self.estimate_session_tokens(messages)
        
        return {
            "session_id": session_id,
            "message_count": len(messages),
            "estimated_tokens": estimated_tokens,
            "context_window": config.CONTEXT_WINDOW_TOKENS,
            "usage_percent": round(estimated_tokens / config.CONTEXT_WINDOW_TOKENS * 100, 1),
            "compact_count": usage.compact_count,
            "micro_compact_count": usage.micro_compact_count,
            "consecutive_failures": usage.consecutive_compact_failures,
            "auto_compact_threshold": int(config.CONTEXT_WINDOW_TOKENS * config.AUTO_COMPACT_THRESHOLD),
        }


# ═══ 全局单例 ═══

_engine: Optional[SessionEngine] = None


def get_session_engine() -> SessionEngine:
    """获取全局 SessionEngine 单例"""
    global _engine
    if _engine is None:
        _engine = SessionEngine()
    return _engine
