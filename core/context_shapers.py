"""
Z-OPS v4.0 Context Shapers — 五级上下文整形管线
借鉴 claude-code query.ts 的 Pre-Model Context Shapers 设计:
  1. budget reduction — 单工具结果超预算转 [REF] 引用 (query_engine._clip_result 已实现)
  2. snip             — 轻量裁剪: 超压时移除最旧完整轮次
  3. micro-compact    — 截断旧工具结果 (session_engine.micro_compact_messages 已实现)
  4. context collapse — 读时投影: 旧工具结果轮折叠为单行摘要 (不改存储)
  5. auto-compact     — LLM 全量摘要 (session_engine 已实现, 仅前四级无效后触发)

关键约束 (审计红线): 所有整形仅作用于「发给 LLM 的投影视图」(messagesForQuery)，
永不改写 Redis/JSONL 中的原始会话历史。
"""

import logging
from typing import List, Optional, Tuple

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

# 折叠行模板 (借鉴 claude-code collapse store 摘要行)
_COLLAPSED_LINE = "[已折叠: {n} 条历史工具结果，涉及 {tools}，共 {chars} 字符 — 原文完整保留在会话记录中]"


def estimate_chars(messages: List[dict]) -> int:
    """粗估消息列表字符量 (整形决策用，无需 tiktoken 精度)"""
    total = 0
    for m in messages:
        total += len(str(m.get("content", "") or ""))
        for tc in m.get("tool_calls") or []:
            total += len(str(tc.get("function", {}).get("arguments", "")))
    return total


# ─── Shaper 2: Snip — 移除最旧完整轮次 (借鉴 claude-code snipCompactIfNeeded) ───

def snip_messages(messages: List[dict], keep_recent: int = 6) -> Tuple[List[dict], int]:
    """超压时移除最旧的完整对话轮次 (user+assistant+tool 成组)，保留 system 与最近 N 条。

    Returns:
        (snipped_messages, freed_chars)
    """
    if len(messages) <= keep_recent + 1:
        return messages, 0

    before = estimate_chars(messages)

    # system 消息置顶保留
    system_msgs = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    # 保留最近 keep_recent 条，其余裁掉
    removed = rest[:-keep_recent] if keep_recent > 0 else rest
    kept = rest[-keep_recent:] if keep_recent > 0 else []

    if not removed:
        return messages, 0

    freed = before - estimate_chars(kept) - estimate_chars(system_msgs)
    # 加一行替代说明，让模型知道有历史被裁剪
    marker = {
        "role": "user",
        "content": f"[系统提示: 已省略更早的 {len(removed)} 条对话消息以控制上下文长度，完整历史见会话记录]",
    }
    result = system_msgs + [marker] + kept
    logger.info(f"[context_shapers] snip: {len(messages)} -> {len(result)} msgs, 释放 ~{freed} chars")
    return result, freed


# ─── Shaper 4: Context Collapse — 读时投影折叠 (借鉴 claude-code applyCollapsesIfNeeded) ───

def collapse_messages(messages: List[dict], age: int = None) -> List[dict]:
    """把「连续多轮之前的工具结果」折叠为单行摘要 — 只改投影视图，不改存储。

    规则:
    - 最近 age 轮 (默认 config.CONTEXT_COLLAPSE_AGE=3) 的 tool 消息保持原样
    - 更早的 tool 消息: 内容超阈值的替换为折叠行 (短内容保留，避免信息损失大于收益)
    - 永不动 system/user/assistant 消息 (工具调用参数属于 assistant，保留)
    """
    if age is None:
        try:
            age = config.CONTEXT_COLLAPSE_AGE
        except Exception:
            age = 3

    # 从尾部数起，找最近 age 条 tool 消息的位置边界
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= age:
        return messages
    keep_from = tool_indices[-age] if age > 0 else len(messages)

    result = []
    collapsed_count = 0
    collapsed_chars = 0
    collapsed_tools = set()
    for i, m in enumerate(messages):
        if m.get("role") == "tool" and i < keep_from:
            content = str(m.get("content", "") or "")
            if len(content) > 200:  # 短结果保留原文
                collapsed_count += 1
                collapsed_chars += len(content)
                collapsed_tools.add(m.get("tool_call_id", "?")[:24])
                result.append({
                    **m,
                    "content": _COLLAPSED_LINE.format(
                        n=1, tools="多条调用", chars=len(content)
                    ),
                    "_collapsed": True,
                })
            else:
                result.append(m)
        else:
            result.append(m)

    if collapsed_count:
        logger.info(
            f"[context_shapers] collapse: 折叠 {collapsed_count} 条旧工具结果, "
            f"释放 ~{collapsed_chars} chars (读时投影, 原始历史未改动)"
        )
    return result


# ─── 组合入口: 按 token 压力逐级整形 (轻 -> 重) ───

def apply_context_shapers(
    messages: List[dict],
    token_counter=None,
    window_tokens: int = None,
    auto_compact_fn=None,
) -> List[dict]:
    """按压力顺序执行五级整形，前级释放不足才触发后级。

    Args:
        messages: 当前 messagesForQuery 视图
        token_counter: 可选精确计数函数 (str -> tokens)；缺省用字符量近似
        window_tokens: 上下文窗口大小 (默认 config.CONTEXT_WINDOW_TOKENS)
        auto_compact_fn: auto-compact 回调 (session_engine 提供)，最后一级

    说明:
        1. budget reduction 已在 query_engine._clip_result 工具结果入口执行
        2. 本函数处理 2-5 级; 全部为纯函数式投影，不修改传入列表
    """
    if window_tokens is None:
        try:
            window_tokens = config.CONTEXT_WINDOW_TOKENS
        except Exception:
            window_tokens = 120000

    def _tokens(msgs: List[dict]) -> int:
        if token_counter:
            total = 0
            for m in msgs:
                total += token_counter(str(m.get("content", "") or ""))
            return total
        # 近似: 1 token ≈ 2 chars (中英混合)
        return estimate_chars(msgs) // 2

    current = list(messages)
    try:
        snip_threshold = config.CONTEXT_SNIP_THRESHOLD
        auto_threshold = config.AUTO_COMPACT_THRESHOLD
    except Exception:
        snip_threshold, auto_threshold = 0.70, 0.80

    pressure = _tokens(current) / max(window_tokens, 1)

    # Shaper 2: snip (70% 压力)
    if pressure >= snip_threshold:
        current, freed = snip_messages(current)
        pressure = _tokens(current) / max(window_tokens, 1)

    # Shaper 3: micro-compact (沿用 session_engine 实现)
    if pressure >= snip_threshold:
        try:
            from core.session_engine import micro_compact_messages
            current = micro_compact_messages(current)
            pressure = _tokens(current) / max(window_tokens, 1)
        except Exception as e:
            logger.debug(f"[context_shapers] micro-compact 跳过: {e}")

    # Shaper 4: context collapse (读时投影)
    if pressure >= snip_threshold:
        current = collapse_messages(current)
        pressure = _tokens(current) / max(window_tokens, 1)

    # Shaper 5: auto-compact (仅前四级无效且超过 80% 时, 由 session_engine 执行)
    if pressure >= auto_threshold and auto_compact_fn is not None:
        try:
            current = auto_compact_fn(current)
            logger.info("[context_shapers] auto-compact 已触发 (前四级释放不足)")
        except Exception as e:
            logger.warning(f"[context_shapers] auto-compact 失败: {e}")

    return current
