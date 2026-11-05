"""
智OC — kb_agent: 知识库查询 Agent
对接 KB API 实现:
  - 关键词搜索知识库
  - 作为 RAG 上下文注入 (供其他 Agent 调用)
  - 独立的知识查询问答

KB API:
  主端点: POST {KB_API_URL}/api/kb/search
    Body: {"query": "关键词", "limit": 5}
    返回: {"documents": [{"title": "...", "context": "...", "url": "...", "score": 0.85}], "success": true}
  混合搜索: POST {KB_API_URL}/api/kb/hybrid-search
    Body: {"query": "关键词", "mode": "hybrid"}
"""

import json
import re
import time
import logging
import httpx

from agents.base import BaseAgent, AgentMessage, run_agent, detect_data_query

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.guardrail import bump_guardrail  # 护栏埋点 (永不抛异常)

logger = logging.getLogger("zops")

SYSTEM_PROMPT = """你是智OC运维平台的知识库助手。

你可以查询内部知识库，回答运维相关问题。
回答时请:
1. 优先引用知识库中的内容，标注来源
2. 如果知识库没有匹配结果，基于通用运维知识尽力回答，并标注“此内容来自通用知识，非内部 KB”
3. 用简洁专业的中文回答
4. 重要操作步骤用编号列表，关键命令用代码块
5. 给出实质性建议，不要只说“未找到”就结束

⚠️ 链接规则（严格遵守）:
- 严禁自行编造、拼凑或猜测任何文档 URL / 链接
- 正文中不要输出知识库文档的 URL；相关文档的真实链接会由系统在回答末尾统一附上
- 如需指代某篇文档，只写文档标题，不要写链接地址"""


# ═══ 查询清洗 ═══

_NOISE_PATTERNS = [
    r'^请?查(一下|询)?\s*KB[，,\s]*',
    r'^KB[，,\s]*',
    r'^请?(告诉|帮)我[，,\s]*',
    r'^(查一下|搜索|搜一下|查找|请问|问一下)[，,\s]*',
    r'^(知识库|文档|资料)[，,\s]*',
]

def clean_query(text: str) -> str:
    """去除查询中的噪音前缀，提取真正的问题"""
    cleaned = text.strip()
    for pat in _NOISE_PATTERNS:
        cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE).strip()
    return cleaned or text.strip()  # 如果清洗后为空，返回原文

# ═══ 数据查询类问题边界识别 ═══
# 检测逻辑已上移到 agents/base.py 的 detect_data_query (kb_agent/general_agent 共用)


# ═══ LLM Query Rewriting (参考 claude-code 多路召回策略) ═══

_REWRITE_SYSTEM = """你是搜索查询优化器。将用户的自然语言问题转换为更适合知识库向量搜索的查询。

规则:
1. 提取核心关键词和运维术语，去除口语化表达
2. 如果问题包含缩写，同时输出全称 (如 HA → High Availability 高可用)
3. 生成 2-3 个不同角度的搜索查询，用 | 分隔
4. 保持简洁，每个查询不超过 20 个字
5. 只输出查询，不要解释

示例:
输入: "VMware 快照太多了怎么清理"
输出: VMware 快照清理|虚拟机快照删除最佳实践|snapshot consolidation

输入: "VPN 连不上怎么办"
输出: VPN 连接故障排查|VPN 无法连接解决方案|SSL VPN troubleshooting

输入: "怎么看备份有没有成功"
输出: 备份作业状态检查|Veeam 备份成功失败监控|backup job verification"""


async def rewrite_query(original: str, llm_chat_fn) -> list[str]:
    """LLM 驱动的查询改写 — 返回多个搜索查询变体
    
    参考 claude-code 的 multi-query retrieval 模式:
    用 LLM 将用户自然语言问题拆解为多个搜索查询，提升召回率。
    """
    try:
        result = await llm_chat_fn(
            prompt=f"将以下问题转换为知识库搜索查询:\n{original}",
            system=_REWRITE_SYSTEM,
            temperature=0.3,
        )
        # 解析 LLM 返回的 | 分隔查询
        variants = [q.strip() for q in result.split("|") if q.strip()]
        # 确保原始查询也在列表中
        if original not in variants:
            variants.insert(0, original)
        return variants[:4]  # 最多 4 个变体
    except Exception as e:
        logger.warning(f"[kb] query rewrite 失败: {e}")
        return [original]


def _simple_query_variants(query: str) -> list[str]:
    """无需 LLM 的简单查询变体生成，提取关键词做搜索"""
    variants = [query]
    # 提取英文单词和中文词组
    words = re.findall(r'[a-zA-Z0-9_]+|[\u4e00-\u9fff]+', query)
    # 如果关键词足够多，加一个精简版
    if len(words) >= 3:
        short = ' '.join(words[:3])
        if short != query:
            variants.append(short)
    return variants


# ═══ KB API 客户端 ═══

async def _kb_search_single(query: str, limit: int = None, mode: str = "search") -> list[dict]:
    """单次知识库查询 (支持 search / hybrid-search 两种模式)"""
    endpoint = "hybrid-search" if mode == "hybrid" else "search"
    url = f"{config.KB_API_URL}/api/kb/{endpoint}"
    token = config.KB_API_TOKEN
    limit = limit or config.KB_SEARCH_LIMIT

    body = {"query": query, "limit": limit}
    if mode == "hybrid":
        body["mode"] = "hybrid"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), verify=False) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                },
                json=body
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("success", False):
                logger.warning(f"[kb] API({mode}) 返回失败: {data}")
                return []

            docs = data.get("documents", [])
            return [d for d in docs if d.get("context") or d.get("title")]
    except httpx.TimeoutException:
        logger.warning(f"[kb] 查询超时({mode}): {query[:30]}")
        return []
    except Exception as e:
        logger.warning(f"[kb] 查询失败({mode}): {e}")
        return []


async def kb_search(query: str, limit: int = None) -> list[dict]:
    """基础搜索 (向后兼容)"""
    return await _kb_search_single(query, limit, mode="search")


async def kb_multi_recall(queries: list[str], limit: int = None) -> list[dict]:
    """多路召回 — 并发执行多个查询变体 + hybrid 模式，去重合并
    
    参考 claude-code 的 multi-source retrieval 模式:
    同一问题的不同表述同时检索，合并去重取 top-K。
    """
    import asyncio
    limit = limit or config.KB_SEARCH_LIMIT
    per_query_limit = max(3, limit)  # 每路至少取 3 条

    tasks = []
    for q in queries[:4]:  # 最多 4 路
        tasks.append(_kb_search_single(q, per_query_limit, mode="search"))
    # 对第一个查询额外跑 hybrid 模式
    if queries:
        tasks.append(_kb_search_single(queries[0], per_query_limit, mode="hybrid"))

    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    # 去重合并 (按 title+url 去重，保留最高 score)
    seen = {}  # key -> doc
    for batch in all_results:
        if isinstance(batch, Exception):
            continue
        for doc in batch:
            key = (doc.get("title", ""), doc.get("url", ""))
            existing = seen.get(key)
            if existing is None:
                seen[key] = doc
            else:
                # 保留更高 score
                old_score = existing.get("score", 0) or 0
                new_score = doc.get("score", 0) or 0
                if new_score > old_score:
                    seen[key] = doc

    # 按 score 降序排列，取 top limit
    merged = sorted(seen.values(), key=lambda d: d.get("score", 0) or 0, reverse=True)
    return merged[:limit]


def format_kb_results(results: list[dict]) -> str:
    """将 KB 搜索结果格式化为可读文本"""
    if not results:
        return ""

    parts = []
    for i, item in enumerate(results, 1):
        title = item.get("title", "").strip() or f"文档{i}"
        context = item.get("context", item.get("content", item.get("text", ""))).strip()
        url = item.get("url", "")
        score = item.get("score", item.get("semantic_score", ""))
        collection = item.get("collection", "")

        # 清理 HTML 标记 (API 返回带<b>高亮)
        if context:
            context = re.sub(r'</?b>', '', context)
        # 截断过长内容
        if len(context) > 800:
            context = context[:800] + "..."

        score_str = f" (相似度: {score:.2f})" if isinstance(score, (int, float)) else ""
        col_str = f" [{collection}]" if collection else ""
        entry = f"[{i}] **{title}**{score_str}{col_str}"
        if context:
            entry += f"\n{context}"
        if url:
            entry += f"\n🔗 {url}"
        parts.append(entry)

    return "\n\n".join(parts)


# ═══ 规范文档链接解析 (修复 KB 包装器返回的截断短链接) ═══
#
# KB 包装器(198.18.18.166:8089)返回的 url 为 /doc/{urlId} 短链接，
# 而本 Outline 实例前端需要完整的 /doc/{slug}-{urlId} 才能正确打开，
# 否则用户点击会 404。这里通过 Outline 原生 API(documents.info)
# 用文档 id / urlId 解析出规范 url。

_URL_CACHE: dict[str, str] = {}  # ident(id 或 urlId) -> 规范绝对 url


def _outline_base() -> str:
    """Outline 站点根地址 (去掉尾部 /api)"""
    api = (getattr(config, "OUTLINE_API_URL", "") or "").rstrip("/")
    if api.endswith("/api"):
        return api[:-4]
    return api


def _url_id_of(doc: dict) -> str:
    """从包装器 url 尾部提取 urlId (/doc/xxxx -> xxxx)"""
    url = (doc.get("url") or "").rstrip("/")
    if "/doc/" in url:
        return url.rsplit("/doc/", 1)[-1]
    return ""


def _looks_canonical(doc: dict) -> bool:
    """判断 url 是否已是规范链接 (/doc/{slug}-{urlId})，即末段含连字符。

    KB-API 从源头修好后，返回的 url 已带 slug，此处直接跳过解析，
    避免多余的 Outline API 调用 —— Z-OPS 兜底层自动静默。
    """
    seg = _url_id_of(doc)
    return bool(seg) and "-" in seg


async def _resolve_one(client: httpx.AsyncClient, doc: dict) -> None:
    """解析单个文档的规范 url，就地写回 doc['url']；失败则保留原值。"""
    # 已是规范链接 (KB-API 已修复) → 直接跳过，不再调 Outline
    if _looks_canonical(doc):
        return
    ident = (doc.get("id") or "").strip() or _url_id_of(doc)
    if not ident:
        return
    base = _outline_base()
    # 命中缓存
    if ident in _URL_CACHE:
        doc["url"] = _URL_CACHE[ident]
        return
    try:
        r = await client.post(
            f"{config.OUTLINE_API_URL}/documents.info",
            headers={
                "Authorization": f"Bearer {config.OUTLINE_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"id": ident},
        )
        if r.status_code == 200:
            path = (r.json().get("data", {}) or {}).get("url") or ""
            if path:
                full = path if path.startswith("http") else f"{base}{path}"
                _URL_CACHE[ident] = full
                doc["url"] = full
    except Exception as e:
        logger.debug(f"[kb] url 解析失败 ident={ident}: {e}")


async def enrich_source_urls(results: list[dict]) -> list[dict]:
    """将结果中的截断短链接批量解析为规范 slug 链接 (并发 + 缓存 + 降级)。

    无 token 或开关关闭时直接返回原结果，不影响主流程。
    """
    if not results:
        return results
    if not getattr(config, "KB_URL_RESOLVE", False):
        return results
    if not getattr(config, "OUTLINE_API_TOKEN", ""):
        return results
    import asyncio
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6, connect=3), verify=False) as client:
            await asyncio.gather(*[_resolve_one(client, d) for d in results],
                                 return_exceptions=True)
    except Exception as e:
        logger.debug(f"[kb] enrich_source_urls 异常: {e}")
    return results


# ═══ 来源链接处理 (防 LLM 链接幻觉) ═══

_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\s)]+)\)')


def _build_sources_section(results: list[dict]) -> str:
    """从真实 KB 搜索结果生成“参考来源”区块，保证链接真实可访问"""
    lines = []
    seen = set()
    for item in results:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip() or "知识库文档"
        if not url or url in seen:
            continue
        seen.add(url)
        lines.append(f"- [{title}]({url})")
    if not lines:
        return ""
    return "**📚 参考来源：**\n" + "\n".join(lines)


def _strip_fabricated_links(text: str, valid_urls: set) -> str:
    """剔除 LLM 正文中指向 KB 文档域名但非真实的编造链接，仅保留链接文字。

    只处理与真实结果同域名的链接，避免误伤合法的外部链接。
    """
    if not valid_urls:
        return text
    valid_hosts = set()
    for u in valid_urls:
        m = re.match(r'(https?://[^/]+)', u)
        if m:
            valid_hosts.add(m.group(1))
    if not valid_hosts:
        return text

    def _repl(m):
        label, url = m.group(1), m.group(2)
        if url in valid_urls:
            return m.group(0)  # 真实链接，保留
        host_m = re.match(r'(https?://[^/]+)', url)
        host = host_m.group(1) if host_m else ""
        if host in valid_hosts:
            # 指向 KB 文档域名但 URL 非真实 → 判定为编造，剔除链接仅留文字
            return label
        return m.group(0)  # 其他外部链接不动

    return _MD_LINK_RE.sub(_repl, text)


async def kb_rag_bundle(query: str, limit: int = None,
                        queries: list[str] = None,
                        strong_only: bool = False) -> tuple[str, list[dict]]:
    """RAG 检索: 返回 (格式化上下文, 原始结果列表)。

    检索策略: search (关键词全字段匹配) + hybrid-search (向量语义检索) 并发多路召回。
    KB API 的 search 端点对自然语言长句几乎全返回 0 条 (全字段匹配语义差)，
    hybrid 语义检索才是主路径，search 仅作缩写/编号等精确词兑底。

    strong_only=True: 环境性 RAG (聊天自动挂引用) 用 — 只保留强相关结果
    (semantic_score ≥ config.KB_RAG_SEM_MIN 或词法精确命中 title_match/text_match)。
    hybrid 融合分是名次归一化的, 对任何查询 top1 都 ≈1.0, 不能作相关性依据;
    显式 KB 问答 (kb_agent 直查) 语义弱相关也有价值, 默认 False 不过滤。
    """
    if queries and len(queries) > 1:
        results = await kb_multi_recall(queries, limit)
    else:
        # 单查询也走双路召回 (search + hybrid)，避免纯 search 长句 0 命中
        results = await kb_multi_recall([query], limit)
    if not results:
        return "", []

    if strong_only:
        def _strong(d: dict) -> bool:
            if d.get("title_match") or d.get("text_match"):
                return True
            try:
                return float(d.get("semantic_score") or 0) >= config.KB_RAG_SEM_MIN
            except (TypeError, ValueError):
                return False
        results = [d for d in results if _strong(d)]
        if not results:
            return "", []

    # 将截断短链接解析为规范 slug 链接，避免用户点击 404
    await enrich_source_urls(results)

    formatted = format_kb_results(results)
    context = (
        "以下是从内部知识库中检索到的相关信息，请参考回答:\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{formatted}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    return context, results


async def kb_rag_context(query: str, limit: int = None,
                        queries: list[str] = None) -> str:
    """
    RAG 上下文生成: 查询 KB 并格式化为 prompt 上下文。
    返回空字符串表示没有匹配的知识。
    供 base.py llm_chat 或其他 Agent 调用。
    
    Args:
        query: 原始查询
        limit: 最大结果数
        queries: 多路召回查询列表 (如 LLM rewrite 的变体)
    """
    context, _ = await kb_rag_bundle(query, limit, queries)
    return context


# ═══ KB Agent ═══

class KbAgent(BaseAgent):
    name = "kb_agent"
    description = "知识库查询 / RAG 知识增强"

    def __init__(self):
        super().__init__()
        self._register_to_pool()

    def _register_to_pool(self):
        """注册到全局 ToolPool"""
        try:
            from core.tool_pool import get_tool_pool
            pool = get_tool_pool()
            pool.register_agent(
                agent_id=self.name,
                description=self.description,
                capabilities=[
                    "知识库", "KB", "RAG", "文档搜索", "运维知识",
                    "流程规范", "故障处理", "最佳实践",
                ],
                tools=[],
            )
        except Exception as e:
            logger.debug(f"[kb_agent] ToolPool 注册异常: {e}")

    async def handle(self, msg: AgentMessage):
        action = msg.action or "chat"

        if action == "search":
            await self._search(msg)
        elif action == "chat":
            await self._chat_with_kb(msg)
        else:
            await self._chat_with_kb(msg)

    async def _search(self, msg: AgentMessage):
        """搜索知识库 — LLM query rewriting + 多路召回"""
        raw_query = msg.text or msg.params.get("query", "")
        query = clean_query(raw_query)
        limit = msg.params.get("limit", config.KB_SEARCH_LIMIT)

        if not query:
            await self.reply(msg, error="缺少查询关键词")
            return

        await self.send_progress(msg, f"正在搜索知识库: {query}")
        # 主路径: search (关键词) + hybrid (语义) 双路并发召回。
        # KB API 的 search 端点对自然语言长句几乎全返回 0 条，hybrid 才是主检索能力
        _t0 = time.time()
        _via = "direct"
        results = await kb_multi_recall([query], limit)

        # 无结果时 → LLM query rewriting + 多路召回
        if not results:
            await self.send_progress(msg, "直接搜索无结果，正在优化搜索查询...")
            variants = await rewrite_query(query, self.llm_chat)
            logger.info(f"[kb] query rewrite: {query[:30]} → {variants}")

            if len(variants) > 1:
                results = await kb_multi_recall(variants, limit)
                if results:
                    _via = "llm_rewrite"
        _via = _via if results else "miss"
        # 检索阶段入账 (含 rewrite 路径; 降级 RAG 问答的检索由 _chat_with_kb 自行入账)
        await self._record_kb_step(
            msg, "kb_retrieval", _t0,
            summary=f"{_via} 命中 {len(results)} 条" if results else "直接搜索与 LLM 变体均无结果")

        if not results:
            # 所有搜索路径均无结果 → 降级 RAG 问答
            logger.info(f"[kb] 多路搜索仍无结果，降级到 RAG 问答: {query[:40]}")
            msg.text = query
            await self._chat_with_kb(msg)
            return

        # 将截断短链接解析为规范 slug 链接，避免用户点击 404
        await enrich_source_urls(results)
        formatted = format_kb_results(results)
        await self.reply(msg, result={
            "text": f"知识库搜索「{query}」找到 {len(results)} 条结果:\n\n{formatted}",
            "results": results,
            "count": len(results)
        })
        await self.log_daily(f"KB 搜索: {query[:50]} → {len(results)} 条", level="success", category="kb")

    async def _record_kb_step(self, msg: AgentMessage, tool: str, t0: float,
                              success: bool = True, summary: str = "") -> None:
        """KB 问答链路步骤入账 (检索/LLM 生成各一条, trace_id=msg_id,
        时间线可按一次问答聚合展开: 路由→消息→检索→生成)"""
        try:
            from core.execution_registry import get_execution_registry, ExecutionType
            cost = time.time() - t0
            await get_execution_registry().record_tool_call(
                agent_id=self.name, tool_name=tool,
                input_summary=(msg.text or "")[:60],
                output_summary=summary or f"耗时 {round(cost, 1)}s",
                success=success, latency_ms=round(cost * 1000, 1),
                session_id=msg.session_id, trace_id=msg.msg_id,
                exec_type=ExecutionType.TOOL_CALL,
                username=(msg.username or ""),  # 问答归户 (feishu 绑定后全线贯通)
            )
        except Exception as e:
            logger.debug(f"[kb_agent] 步骤入账失败 {tool}: {e}")

    async def _chat_with_kb(self, msg: AgentMessage):
        """结合知识库回答问题 (直接搜索 + LLM 回答, 跳过 query rewriting 省 15s)"""
        raw_query = msg.text or msg.params.get("text", "")
        query = clean_query(raw_query)
        if not query:
            await self.reply(msg, error="缺少问题内容")
            return

        # 数据查询类问题边界: 用户要查生产环境实时数据 (备份状态/清单等),
        # 知识库无此能力 → 直接转发给 opscore_agent 数据查询助手，由它查询真实数据并回复。
        # 关键点: 转发消息保持原 msg_id，opscore_agent 的进度/流式增量/最终回复都会
        # 发到原 reply 频道，用户在同一个 SSE 流中直接拿到真实查询结果，
        # 而不是"请用户自行登录第三方系统核查"的踢皮球回答。
        data_hint = detect_data_query(raw_query)
        if data_hint:
            if await self.forward_to_opscore(msg, raw_query):
                await self.send_progress(msg, "检测到数据查询类问题，已转交数据查询助手查询生产数据，请稍候...")
                await self.log_daily(f"KB 数据查询转发 opscore_agent: {raw_query[:60]}", level="info", category="kb")
                return  # 不自行 reply，避免双回复提前终止 SSE 流
            # opscore_agent 离线/转发失败 → 降级为边界说明，绝不编造数据
            await self.send_progress(msg, "数据查询助手暂时不可用...")
            await self.reply(msg, result={"text": (
                f"这是【实时数据查询】类问题（{data_hint}），数据查询助手当前不在线，"
                "请稍后重试。知识库仅收录文档/流程/规范，无法查询生产环境的实时备份数据。"
            )})
            await self.log_daily(f"KB 数据查询转发失败(降级边界): {raw_query[:60]}", level="warn", category="kb")
            return
    
        # Step 1: 主搜索 — hybrid 语义检索 (自然语言问题的正确检索方式)。
        # KB API 的 search 端点对长句做全字段匹配几乎全返回 0 条，效果差；
        # hybrid-search 是向量语义检索，才是知识库真正的检索能力
        await self.send_progress(msg, f"知识库助手正在搜索: {query[:40]}...")
        _t0 = time.time()
        _via = ""
        results = await _kb_search_single(query, mode="hybrid")
        if results:
            _via = "hybrid"
        # 护栏埋点: 主检索命中率 (优化闭环用它做召回回归核算)
        await bump_guardrail(self.name, "kb_hybrid_hit" if results else "kb_hybrid_miss")
        
        # hybrid 无结果 → search 关键词精确匹配兑底 (缩写/编号/命令等精确词场景)
        if not results:
            results = await kb_search(query)
            if results:
                _via = "keyword"
        
        # 仍无结果 → 用简单关键词变体多路召回再搜一次 (不用 LLM, 几乎无延迟)
        if not results:
            # 护栏埋点: 兑底路径触发率 (触发越多 = 召回越依赖兑底)
            await bump_guardrail(self.name, "kb_variant_fallback")
            fallback_queries = _simple_query_variants(query)
            if len(fallback_queries) > 1:
                results = await kb_multi_recall(fallback_queries, config.KB_SEARCH_LIMIT)
                if results:
                    _via = "variants"
        if not results:
            # 护栏埋点: 三路检索全空 (最终未命中)
            await bump_guardrail(self.name, "kb_final_no_hit")
            _via = "miss"
        # 检索阶段入账: 路径 + 命中数 (bot/webchat 问答的详细记录)
        await self._record_kb_step(
            msg, "kb_retrieval", _t0,
            summary=f"{_via} 命中 {len(results)} 条" if results else "hybrid/关键词/变体三路全空")
    
        # 构建 KB 上下文 + 真实 URL 集合
        kb_context = ""
        valid_urls = set()
        if results:
            # 将截断短链接解析为规范 slug 链接，避免用户点击 404
            await enrich_source_urls(results)
            valid_urls = {(d.get("url") or "").strip() for d in results if d.get("url")}
            kb_context = (
                "以下是从内部知识库中检索到的相关信息，请参考回答:\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{format_kb_results(results)}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            )
    
        # Step 2: 构建对话历史上下文 (参考 claude-code 会话上下文模式)
        history_prefix = await self._build_history_context(msg, max_turns=4, max_content_len=300)

        # 用户问题改写: 纠错/否定措辞会被 OneAPI 内容安全网关误判为提示词注入，
        # KB 检索仍用原句，仅送入 LLM 的版本改为中性措辞
        safe_query = self.rewrite_for_safety(self.sanitize_user_text(query))
        if safe_query != query:
            logger.info(f"[kb_agent] 问题措辞改写(防网关误判): '{query}' → '{safe_query}'")

        if kb_context:
            await self.send_progress(msg, f"知识库命中，正在生成回答，请稍候...")
            prompt = f"{history_prefix}{kb_context}\n用户问题: {safe_query}"
        else:
            await self.send_progress(msg, "知识库无匹配，正在生成回答，请稍候...")
            prompt = (
                f"{history_prefix}"
                f"知识库中没有找到与「{safe_query}」直接匹配的文档。\n"
                f"请基于你的运维专业知识，详细回答以下问题。\n"
                f"回答要求: 简洁实用，给出具体的操作步骤和关键命令。\n\n"
                f"用户问题: {safe_query}"
            )
    
        # 长调用心跳: 模型慢时每 30s 向用户发进度，同时刷新后端 SSE 空闲截止时间
        async def _llm_wait(elapsed: int):
            await self.send_progress(msg, f"⏳ 模型仍在生成中 (已等待 {elapsed}s)，请稍候...")

        # 流式增量: LLM 边生成边推送到前端实时渲染
        async def _llm_delta(seg: str):
            await self.send_delta(msg, seg)

        # 思考链流式上屏 (DeepSeek thinking mode): 思考过程逐段推送，前端折叠展示
        async def _llm_thinking(seg: str):
            await self.send_delta(msg, seg, kind="thinking")

        _t1 = time.time()
        result = await self.llm_chat(prompt=prompt, system=SYSTEM_PROMPT,
                                     on_wait=_llm_wait, on_delta=_llm_delta,
                                     on_reasoning=_llm_thinking,
                                     username=(msg.username or ""),
                                     session_id=(msg.session_id or ""))
        # LLM 生成阶段入账: 字数 + KB 命中上下文 (问答耗时大头在这里)
        _kb_tag = "命中" if kb_context else "无"
        await self._record_kb_step(
            msg, "kb_llm_answer", _t1, success=bool(result and result.strip()),
            summary=f"生成 {len(result or '')} 字, KB={_kb_tag}上下文")
    
        # LLM 失败兑底：如果返回空或异常，给出有意义的提示
        if not result or not result.strip():
            result = (
                f"抱歉，当前无法生成关于「{query}」的回答（LLM 服务暂时不可用）。\n\n"
                f"建议您：\n"
                f"1. 稍后重试该问题\n"
                f"2. 如果是 Z-OPS 系统相关问题，请尝试更具体的描述\n"
                f"3. 联系管理员检查 LLM 服务状态"
            )
            source_tag = "(来源: 系统提示)"
            final_text = f"{result}\n\n---\n*{source_tag}*"
        else:
            source_tag = "(来源: 内部知识库)" if kb_context else "(来源: 通用运维知识)"
            # 剔除 LLM 正文中可能编造的 KB 链接，避免用户点到无效地址
            result = _strip_fabricated_links(result, valid_urls)
            # 由程序统一附上真实来源链接（仅 KB 命中时）
            sources_section = _build_sources_section(results) if results else ""
            if sources_section:
                final_text = f"{result}\n\n{sources_section}\n\n---\n*{source_tag}*"
            else:
                final_text = f"{result}\n\n---\n*{source_tag}*"
    
        await self.reply(msg, result={
            "text": final_text,
            "kb_matched": bool(kb_context),
            "query": query,
        })
        kb_label = '命中' if kb_context else '无'
        # 完整问答全文由 BaseAgent.reply() 兜底收录，这里只记 KB 命中摘要
        await self.log_daily(
            f"KB 问答: {query[:50]} → 已回复 (KB={kb_label})",
            level="success", category="kb",
            session_id=msg.session_id, username=(msg.username or "")
        )


if __name__ == "__main__":
    run_agent(KbAgent())
