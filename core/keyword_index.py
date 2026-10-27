"""
Z-OPS 关键词索引引擎 — 基于 jieba 的会话关键词提取与意图加速

核心能力:
1. jieba 分词 + TF-IDF 关键词提取  →  每条消息 Top-K 关键词
2. Redis 缓存  →  zops:kw:{session_id}  ZSet(keyword → score)
3. 意图增强  →  为 orchestrator 提供关键词上下文提示
4. Agent 亲和度  →  关键词 → Agent 映射, 加速路由

零模型依赖, 纯 CPU 运行, 单次提取 <1ms
"""

import re
import json
import time
import logging
from typing import List, Dict, Tuple, Optional, Set
from collections import Counter

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

# ═══ jieba 延迟加载 ═══

_jieba_loaded = False
_jieba_analyse = None
_jieba = None


def _ensure_jieba():
    """首次调用时加载 jieba (避免启动时阻塞 ~0.5s)"""
    global _jieba_loaded, _jieba_analyse, _jieba
    if _jieba_loaded:
        return
    try:
        import jieba
        import jieba.analyse
        # 静默 jieba 初始化日志
        jieba.setLogLevel(logging.WARNING)
        _jieba = jieba
        _jieba_analyse = jieba.analyse
        _jieba_loaded = True
        logger.info("[keyword_index] jieba 加载完成")
    except ImportError:
        _jieba_loaded = True  # 标记已尝试, 避免重复加载
        logger.warning("[keyword_index] jieba 未安装, 关键词索引降级为正则提取")


# ═══ 运维领域停用词 ═══

_STOP_WORDS: Set[str] = {
    # 通用
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有",
    "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些", "吗", "吧",
    "呢", "啊", "哦", "啦", "呀", "什么", "怎么", "哪", "哪里", "哪个", "为什么",
    "可以", "能", "请", "帮", "帮我", "查", "看看", "一下", "下", "最近",
    "所有", "全部", "多少", "几个", "怎样", "如何", "什么样", "告诉",
    "需要", "想", "想要", "知道", "麻烦", "现在",
    # 标点/符号
    ".", ",", "?", "!", ":", ";", "，", "。", "？", "！", "：", "；",
}

# ═══ Agent 关键词亲和度映射 ═══

AGENT_KEYWORDS: Dict[str, List[str]] = {
    "gcoc_agent": [
        "备份", "backup", "brr", "gid", "客户", "站点", "site",
        "快照", "snapshot", "虚拟机", "vm", "vcd", "vcloud",
        "cmdb", "主机", "服务器", "机柜", "idc", "数据中心",
        "dns", "解析", "记录", "smart-ops", "smartops",
        "aria", "m365", "office365", "ha", "高可用",
        "事件", "告警", "alert", "监控", "monitor",
        "crm", "工单", "case",
    ],
    "kb_agent": [
        "知识库", "文档", "资料", "搜索文档", "搜资料",
        "流程", "规范", "手册", "教程", "wiki",
        "kb", "knowledge", "rag",
        "故障处理", "排查", "故障排查", "解决方法",
        "vpn", "连接故障", "报错", "处理方法",
        "最佳实践", "操作指南",
    ],
    "ops_agent": [
        "巡检", "检查", "健康", "日志", "磁盘", "cpu",
        "内存", "负载", "系统", "批量执行", "批量",
    ],
    "network_agent": [
        "ping", "端口", "网络", "连通", "交换机", "路由",
        "traceroute", "tcping", "port",
    ],
    "vm_agent": [
        "开机", "关机", "重启", "迁移", "创建快照",
    ],
    "dev_agent": [
        "脚本", "shell", "python", "ansible", "自动化",
        "编写", "代码", "script",
    ],
    "browser_agent": [
        "截图", "网页", "打开", "浏览器", "screenshot",
    ],
}

# 反向索引: keyword -> agent
_KW_TO_AGENT: Dict[str, str] = {}
for agent, kws in AGENT_KEYWORDS.items():
    for kw in kws:
        _KW_TO_AGENT[kw.lower()] = agent


# ═══ 关键词提取 ═══

def extract_keywords(text: str, topk: int = 0) -> List[Tuple[str, float]]:
    """提取文本关键词 (jieba TF-IDF + 领域增强)

    Returns:
        [(keyword, weight), ...] 按权重降序, weight ∈ (0, 1]
    """
    if not text or len(text.strip()) < 2:
        return []

    topk = topk or config.KEYWORD_TOPK
    _ensure_jieba()

    keywords: List[Tuple[str, float]] = []

    if _jieba_analyse:
        # jieba TF-IDF 提取
        raw = _jieba_analyse.extract_tags(text, topK=topk * 2, withWeight=True)
        for word, weight in raw:
            if word.lower() not in _STOP_WORDS and len(word) >= 2:
                keywords.append((word, round(weight, 4)))
    else:
        # Fallback: 正则提取中文词 + 英文词
        words = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_\-]{2,}', text)
        counter = Counter(w.lower() for w in words if w.lower() not in _STOP_WORDS)
        total = sum(counter.values()) or 1
        for word, cnt in counter.most_common(topk * 2):
            keywords.append((word, round(cnt / total, 4)))

    # 领域增强: 如果文本中包含 GID 编号, 作为高权重关键词
    gid_matches = re.findall(r'\b(?:GID)?(\d{5,6})\b', text, re.IGNORECASE)
    for gid in gid_matches:
        keywords.insert(0, (gid, 1.0))

    # 领域增强: 如果文本中包含 IP 地址
    ip_matches = re.findall(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', text)
    for ip in ip_matches:
        keywords.insert(0, (ip, 0.9))

    # 去重 + 取 Top-K
    seen = set()
    result = []
    for word, weight in keywords:
        key = word.lower()
        if key not in seen:
            seen.add(key)
            result.append((word, weight))
        if len(result) >= topk:
            break

    return result


def match_agent_affinity(keywords: List[str]) -> Dict[str, float]:
    """根据关键词计算各 Agent 的亲和度分数

    Returns:
        {agent_name: score} 降序排列, score = 匹配关键词数 / 总关键词数
    """
    if not keywords:
        return {}

    agent_scores: Dict[str, int] = Counter()
    for kw in keywords:
        agent = _KW_TO_AGENT.get(kw.lower())
        if agent:
            agent_scores[agent] += 1

    total = len(keywords) or 1
    return {
        agent: round(cnt / total, 3)
        for agent, cnt in agent_scores.most_common()
    }


# ═══ Redis 缓存管理 ═══

async def update_session_keywords(redis_client, session_id: str,
                                   text: str, role: str = "user"):
    """提取消息关键词并更新 Redis 缓存 (ZSet: keyword → score)

    用户消息权重 x1.5, assistant 消息权重 x1.0
    """
    if not text or not session_id:
        return

    keywords = extract_keywords(text)
    if not keywords:
        return

    key = config.key_session_keywords(session_id)
    weight_multiplier = 1.5 if role == "user" else 1.0

    pipe = redis_client.pipeline()
    for word, score in keywords:
        # ZINCRBY: 累加分数, 多次提到的关键词分数更高
        pipe.zincrby(key, score * weight_multiplier, word)
    pipe.expire(key, config.KEYWORD_SESSION_TTL)
    await pipe.execute()

    # 修剪: 只保留 Top-N 关键词
    max_kw = config.KEYWORD_MAX_PER_SESSION
    current_count = await redis_client.zcard(key)
    if current_count > max_kw:
        # 删除分数最低的
        await redis_client.zremrangebyrank(key, 0, current_count - max_kw - 1)

    logger.debug(f"[keyword_index] session={session_id[:8]}.. "
                 f"提取 {len(keywords)} 关键词: {[k for k, _ in keywords[:5]]}")


async def get_session_keywords(redis_client, session_id: str,
                                topk: int = 10) -> List[Tuple[str, float]]:
    """获取会话的 Top-K 关键词 (按分数降序)"""
    key = config.key_session_keywords(session_id)
    # ZREVRANGEBYSCORE 获取分数最高的
    raw = await redis_client.zrevrange(key, 0, topk - 1, withscores=True)
    return [(word, round(score, 3)) for word, score in raw]


async def build_keyword_context(redis_client, session_id: str,
                                 current_text: str = "") -> str:
    """为 orchestrator 构建关键词上下文提示

    Returns:
        关键词上下文字符串, 可直接拼接到意图识别 prompt
    """
    # 提取当前消息关键词
    current_kws = extract_keywords(current_text) if current_text else []

    # 获取历史关键词
    history_kws = await get_session_keywords(redis_client, session_id, topk=10)

    # 合并
    all_kw_names = [k for k, _ in current_kws] + [k for k, _ in history_kws]
    if not all_kw_names:
        return ""

    # 去重
    seen = set()
    unique_kws = []
    for kw in all_kw_names:
        if kw.lower() not in seen:
            seen.add(kw.lower())
            unique_kws.append(kw)

    # Agent 亲和度
    affinity = match_agent_affinity(unique_kws)

    lines = []
    lines.append(f"关键词上下文: {', '.join(unique_kws[:15])}")
    if affinity:
        top_agents = [f"{a}({s:.0%})" for a, s in list(affinity.items())[:3]]
        lines.append(f"关键词亲和: {', '.join(top_agents)}")

    return "\n".join(lines)
