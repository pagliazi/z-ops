# [架构说明 2026-10-19] 本模块的 auto-compact 功能已被 core/session_engine.py 的
# auto_compact_if_needed() 取代 (自带 micro-compact + LLM compact + transcript 双写)。
# compress_context() 作为独立压缩工具保留 (可被自定义 pipeline 使用), 但主链路不走此模块。
# Layer 4 Auto-Compact 已实现 LLM 调用 (保留为工具方法)。
"""
Z-OPS v4 — Context Compression (四层压缩管线)

参考 Claude Code 的 Context Compression 设计:
- 每次 LLM 调用前执行，确保上下文不超过窗口限制
- 4 层压缩 (从低到高成本):
  0. Tool Result Budget — 限制单条工具输出大小
  1. Snip Compact — 物理移除旧消息
  2. Microcompact — 丢弃不需要的工具结果
  3. Context Collapse — 对话片段摘要
  4. Auto-Compact — fork 整个对话摘要 (最重)

预算阈值 (Claude Code 参考):
- Auto-compact: effective_context_window - 13,000 tokens
- Hard blocking: effective_context_window - 3,000 tokens
- 10K gap: reactive compact 运行窗口
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("zops")


# ═══ 配置 ═══

@dataclass
class CompressionConfig:
    """压缩配置"""
    max_context_tokens: int = 128000       # 最大上下文窗口 (Claude 3.5)
    auto_compact_threshold: int = 115000   # 触发 auto-compact (窗口 - 13K)
    hard_block_threshold: int = 125000     # 硬阻塞 (窗口 - 3K)
    reactive_compact_threshold: int = 120000  # reactive compact (gap 中间)
    
    max_tool_result_chars: int = 30000     # 工具结果最大字符数 (Bash)
    max_grep_result_chars: int = 100000    # grep 结果最大字符数
    max_edit_result_chars: int = 100000    # edit 结果最大字符数
    
    snip_keep_recent: int = 10             # Snip 时保留最近 N 条消息
    collapse_min_span: int = 5             # Collapse 至少跨越 N 条消息


@dataclass
class CompressionStats:
    """压缩统计"""
    layer: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_freed: int = 0
    messages_removed: int = 0
    duration_ms: float = 0.0


# ═══ Token 估算 ═══

def estimate_tokens(text: str) -> int:
    """
    粗略估算 token 数 (中英文混合)
    
    规则:
    - 英文: 1 token ≈ 4 chars
    - 中文: 1 token ≈ 1.5 chars
    """
    if not text:
        return 0
    
    # 简单估算 (Phase 5 后续接入 tiktoken)
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    
    return chinese_chars + other_chars // 4


def estimate_message_tokens(msg: Dict) -> int:
    """估算单条消息的 token 数"""
    content = msg.get("content", "")
    tokens = estimate_tokens(content)
    
    # tool_call 额外开销
    if "tool_calls" in msg:
        tokens += 50  # 固定开销
    
    # tool_result 额外开销
    if "tool_call_id" in msg:
        tokens += 20
    
    return tokens


def estimate_messages_tokens(messages: List[Dict]) -> int:
    """估算消息列表的总 token 数"""
    return sum(estimate_message_tokens(m) for m in messages)


# ═══ 四层压缩管线 ═══

class ContextCompressor:
    """
    Context Compression — 四层压缩管线
    
    使用方式:
        compressor = ContextCompressor()
        messages, stats = await compressor.compress(messages, current_tokens)
    """
    
    def __init__(self, config: CompressionConfig = None):
        self.config = config or CompressionConfig()
    
    async def compress(
        self,
        messages: List[Dict],
        current_tokens: int = 0,
    ) -> Tuple[List[Dict], CompressionStats]:
        """
        执行压缩管线
        
        返回: (压缩后的消息, 统计信息)
        """
        start_time = time.time()
        tokens_before = current_tokens or estimate_messages_tokens(messages)
        
        # 确定需要哪层压缩
        layer = self._select_layer(tokens_before)
        if layer == 0:
            # 不需要压缩
            return messages, CompressionStats(
                layer=0,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                duration_ms=(time.time() - start_time) * 1000,
            )
        
        logger.info(f"[compression] 触发第 {layer} 层压缩 (tokens={tokens_before})")
        
        stats = CompressionStats(
            layer=layer,
            tokens_before=tokens_before,
        )
        
        # 逐层执行 (从低到高)
        for l in range(1, layer + 1):
            if l == 1:
                messages, removed = self._layer1_snip(messages)
                stats.messages_removed += removed
            elif l == 2:
                messages, removed = self._layer2_microcompact(messages)
                stats.messages_removed += removed
            elif l == 3:
                messages, removed = self._layer3_collapse(messages)
                stats.messages_removed += removed
            elif l == 4:
                messages = await self._layer4_auto_compact(messages)
        
        tokens_after = estimate_messages_tokens(messages)
        stats.tokens_after = tokens_after
        stats.tokens_freed = tokens_before - tokens_after
        stats.duration_ms = (time.time() - start_time) * 1000
        
        logger.info(
            f"[compression] 完成: layer={layer}, "
            f"freed={stats.tokens_freed} tokens, "
            f"removed={stats.messages_removed} messages, "
            f"duration={stats.duration_ms:.0f}ms"
        )
        
        return messages, stats
    
    def _select_layer(self, current_tokens: int) -> int:
        """选择压缩层级"""
        if current_tokens < self.config.auto_compact_threshold:
            return 0  # 不需要压缩
        elif current_tokens < self.config.reactive_compact_threshold:
            return 1  # Layer 1: Snip
        elif current_tokens < self.config.hard_block_threshold:
            return 2  # Layer 2: Microcompact
        else:
            return 3  # Layer 3: Context Collapse
            # Layer 4 (Auto-Compact) 需要 LLM 调用，暂不自动触发
    
    def _layer1_snip(self, messages: List[Dict]) -> Tuple[List[Dict], int]:
        """
        Layer 1: Snip Compact
        
        物理移除旧消息，保留最近 N 条
        """
        if len(messages) <= self.config.snip_keep_recent + 1:  # +1 for system
            return messages, 0
        
        # 保留 system message 和最近 N 条
        system_msg = messages[0] if messages[0].get("role") == "system" else None
        recent = messages[-self.config.snip_keep_recent:]
        
        if system_msg:
            new_messages = [system_msg] + recent
        else:
            new_messages = recent
        
        removed = len(messages) - len(new_messages)
        return new_messages, removed
    
    def _layer2_microcompact(self, messages: List[Dict]) -> Tuple[List[Dict], int]:
        """
        Layer 2: Microcompact
        
        丢弃不需要的工具结果 (保留 tool_use 但截断 tool_result)
        """
        new_messages = []
        removed = 0
        
        for msg in messages:
            if msg.get("role") == "tool":
                # 工具结果截断
                content = msg.get("content", "")
                if len(content) > 1000:
                    # 保留前 500 + 后 200
                    msg = dict(msg)
                    msg["content"] = content[:500] + "\n...(truncated)\n" + content[-200:]
                    removed += 1
            new_messages.append(msg)
        
        return new_messages, removed
    
    def _layer3_collapse(self, messages: List[Dict]) -> Tuple[List[Dict], int]:
        """
        Layer 3: Context Collapse
        
        将对话片段替换为摘要
        """
        if len(messages) < self.config.collapse_min_span + 2:
            return messages, 0
        
        # 找到可压缩的区间 (跳过 system 和最近消息)
        system_msg = messages[0] if messages[0].get("role") == "system" else None
        recent_count = 5  # 保留最近 5 条
        
        if system_msg:
            compressible = messages[1:-recent_count]
            prefix = [system_msg]
        else:
            compressible = messages[:-recent_count]
            prefix = []
        
        if len(compressible) < self.config.collapse_min_span:
            return messages, 0
        
        # 生成摘要 (简化版: 取前 200 chars)
        summary_text = " ".join(m.get("content", "")[:50] for m in compressible[:5])
        summary_msg = {
            "role": "system",
            "content": f"[历史对话摘要] {summary_text[:200]}...(省略 {len(compressible)} 条消息)",
        }
        
        new_messages = prefix + [summary_msg] + messages[-recent_count:]
        removed = len(compressible) - 1  # -1 for summary
        return new_messages, removed
    
    async def _layer4_auto_compact(self, messages: List[Dict]) -> List[Dict]:
        """
        Layer 4: Auto-Compact

        把整个对话历史交给 LLM 生成结构化摘要, 压缩为单条消息保留关键信息。
        LLM 不可用时降级到 Layer 3 (滑窗), 不阻断对话。
        """
        if len(messages) < 6:
            return messages   # 消息太少不需要 compact

        # 构建摘要 prompt (Hermes 风格: 结构化摘要保留关键决策/数据/上下文)
        lines = []
        for m in messages:
            role = m.get("role", "user")
            content = str(m.get("content", ""))[:300]
            lines.append(f"[{role}] {content}")
        dialog = "\n".join(lines)

        prompt = (
            "请把以下运维对话压缩为一条结构化摘要 (保留: 关键决策/数据/结论/未完成事项)。"
            "输出格式:\n"
            "## 对话摘要\n- **主题**: ...\n- **关键数据**: ...\n"
            "- **已确定**: ...\n- **待跟进**: ...\n\n对话内容:\n" + dialog
        )

        try:
            # 使用共享的 llm_chat (走模型网关: 限流/退避/熔断)
            import config as _config
            import httpx as _httpx
            _pref_raw = None
            try:
                import redis.asyncio as _aioredis
                _r = _aioredis.from_url(_config.REDIS_URL, decode_responses=True)
                _pref_raw = await _r.get(_config.KEY_LLM_PREF)
                await _r.aclose()
            except Exception:
                pass

            if not _pref_raw:
                logger.info("[compression] Layer 4 无 LLM 配置, 跳过")
                return messages

            import json as _json
            pref = _json.loads(_pref_raw)
            api_key = pref.get("oneapi_key") or pref.get("api_key", "")
            base_url = (pref.get("oneapi_base_url") or pref.get("base_url") or
                        "https://api.deepseek.com/v1").rstrip("/")
            model = pref.get("model") or "deepseek-chat"
            if not api_key:
                return messages

            async with _httpx.AsyncClient(timeout=60, verify=False) as _client:
                _resp = await _client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": 800, "temperature": 0.2})
                _resp.raise_for_status()
                _data = _resp.json()
                summary = (_data.get("choices") or [{}])[0].get("message", {}).get("content", "")

            if not summary or len(summary) < 50:
                logger.warning("[compression] Layer 4 摘要过短, 降级到滑窗")
                return messages

            # 保留 system 消息 + 摘要 + 最近 2 条 (确保衔接)
            system_msgs = [m for m in messages if m.get("role") == "system"]
            compacted = system_msgs + [
                {"role": "user", "content": "[上下文摘要]\n" + summary},
            ] + messages[-2:]
            logger.info(f"[compression] Layer 4 Auto-Compact: {len(messages)} → {len(compacted)} 条")
            return compacted

        except Exception as e:
            logger.warning(f"[compression] Layer 4 LLM 调用失败 (降级到滑窗): {e}")
            return messages
    
    def stats(self, messages: List[Dict]) -> Dict:
        """当前上下文统计"""
        total_tokens = estimate_messages_tokens(messages)
        
        by_role = {}
        for msg in messages:
            role = msg.get("role", "unknown")
            by_role[role] = by_role.get(role, 0) + 1
        
        return {
            "total_messages": len(messages),
            "total_tokens": total_tokens,
            "by_role": by_role,
            "window_usage_pct": round(total_tokens / self.config.max_context_tokens * 100, 1),
            "compression_needed": total_tokens >= self.config.auto_compact_threshold,
        }


# ═══ 便捷函数 ═══

async def compress_context(messages, current_tokens=0, config=None):
    """便捷函数: 执行压缩"""
    compressor = ContextCompressor(config)
    return await compressor.compress(messages, current_tokens)


# ═══ 测试 ═══

if __name__ == "__main__":
    import asyncio
    
    async def test():
        compressor = ContextCompressor()
        
        # 生成测试消息
        messages = [{"role": "system", "content": "你是助手"}]
        for i in range(50):
            messages.append({"role": "user", "content": f"问题 {i}"})
            messages.append({"role": "assistant", "content": f"回答 {i}"})
        
        print(f"=== Context Compression 测试 ===\n")
        print(f"原始消息: {len(messages)} 条")
        
        stats = compressor.stats(messages)
        print(f"估算 tokens: {stats['total_tokens']}")
        print(f"窗口使用率: {stats['window_usage_pct']}%")
        
        # 测试压缩
        compressed, result = await compressor.compress(messages, stats['total_tokens'])
        print(f"\n压缩后: {len(compressed)} 条消息")
        print(f"压缩层: {result.layer}")
        print(f"释放 tokens: {result.tokens_freed}")
        print(f"移除消息: {result.messages_removed}")
        print(f"耗时: {result.duration_ms:.0f}ms")
    
    asyncio.run(test())
