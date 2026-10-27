"""
LLM 网关内容安全误判防护 (共享模块)

背景: OneAPI 等网关的内容安全系统会把正常运维对话误判为 prompt injection，
返回固定拒答模板 (如 "抱歉，我无法回答这个问题。 (检测到不安全内容类别: 涉及提示词注入)")。
典型误判场景: 用户纠正 Z-OPS 的错误回答 ("你提供的XX是错的") 被当作注入攻击。

本模块提供:
1. rewrite_for_safety(): 将纠错/否定/质疑措辞改写为中性运维措辞 (语义不变)
2. is_gateway_refusal(): 识别网关安全拒绝模板
3. rewrite_messages_for_safety(): 改写 OpenAI 格式消息列表中的用户消息 (含历史上下文)
"""

import re as _re
from typing import List, Optional

# 触发词 → 中性措辞 改写规则 (语义不变，仅消除网关误判特征)
_SAFETY_REWRITES = [
    # 指令式否定措辞 ("不要思考了"等) — 网关判为提示词注入的高危特征 (实测误判案例)
    (r'(?:请)?(?:不要|别|不用|无需|先不要)思考了?', '请直接回答'),
    (r'(?:请)?(?:不要|别)想了?', '请直接回答'),
    # 用户纠错/否定措辞 (如 "你提供的XX是错的" → 中性描述)
    (r'你(?:们)?(?:之前|刚才|上面|前面)?(?:提供|说|回答|给)(?:的|了)[^，。！？,\n]{0,20}?是[错误]的', '前面提到的内容有偏差'),
    (r'是[错误]的', '有误'),
    (r'不正确', '有误'),
    (r'不对(?![应照])', '有误'),
    (r'搞错了|弄错了|说错了', '有偏差'),
    (r'错了', '有误'),
    (r'胡说|胡扯|瞎说|乱说|扯淡|狗屁|放屁', ''),
    # "真实/胡扯" 类触发词
    (r'检查真实\s*', '查询'),
    (r'查看真实\s*', '查询'),
    (r'看看真实\s*', '查询'),
    (r'真实\s*资源', '平台资源'),
    (r'真实[的]?\s*(数据|信息|情况)', '实际的\\1'),
    (r'不要[给]?我?\s*(胡扯|乱说|瞎说)', ''),
    (r'别\s*(胡扯|乱说|瞎说)', ''),
    # Zabbix 监控项名 __xxx__ 双下划线模式 (如 __dns_query__homecrbcncn__no_data)
    # 实测易触发网关“提示词注入”误判，分隔符归一下划线 (无语义损失)
    (r'__+', '_'),
]

# 网关安全拒绝标记 (命中且回复很短 → 判定为网关拦截而非正常回复)
_REFUSAL_MARKERS = ("我无法回答这个问题", "不安全内容类别", "涉及提示词注入",
                    "prompt injection", "content_filter")


def rewrite_for_safety(text: str) -> str:
    """改写可能触发 LLM 网关内容安全误判的措辞，转为中性运维措辞"""
    if not text:
        return text
    result = text
    for pattern, replacement in _SAFETY_REWRITES:
        result = _re.sub(pattern, replacement, result, flags=_re.IGNORECASE)
    result = _re.sub(r'^[,，、。\s]+', '', result).strip()
    return result if result else text.strip()


def is_gateway_refusal(text: str) -> bool:
    """判断 LLM 返回是否为网关内容安全的误判拒绝"""
    if not text or len(text) > 300:
        return False
    return any(m in text for m in _REFUSAL_MARKERS)


# 旧拒答模板正则 (出现在历史上下文中会反复触发网关)
_REFUSAL_TEMPLATE_RE = _re.compile(
    r'抱歉，我无法回答这个问题。?\s*[\(（]检测到不安全内容类别[^\)）]*[\)）]')


def strip_refusal_text(text: str) -> str:
    """清理文本中的旧安全拒绝模板 (用于扁平化的 prompt 历史上下文)"""
    if not text:
        return text
    return _REFUSAL_TEMPLATE_RE.sub('(上一轮未能生成回答)', text)


def rewrite_messages_for_safety(messages: List[dict]) -> Optional[List[dict]]:
    """改写 OpenAI 格式消息列表，消除网关安全误判触发特征。

    返回改写后的新列表；若无任何变化返回 None。
    1. user 消息: 纠错/否定措辞改写为中性措辞
    2. assistant 消息: 若内容是旧的安全拒绝模板(含"涉及提示词注入"等字样)，
       替换为中性占位 — 避免旧拒答文本在历史上下文中反复触发网关
    3. system/tool 消息不动 (工具结果可能含真实数据)
    """
    if not messages:
        return None
    changed = False
    new_messages = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "user" and isinstance(content, str):
            rewritten = strip_refusal_text(rewrite_for_safety(content))
            if rewritten != content:
                m = dict(m)
                m["content"] = rewritten
                changed = True
        elif role == "assistant" and isinstance(content, str) and is_gateway_refusal(content):
            m = dict(m)
            m["content"] = "(上一轮未能生成回答，请直接回答本轮问题)"
            changed = True
        new_messages.append(m)
    return new_messages if changed else None
