"""
Z-OPS — general_agent: 通用问答 / 知识库检索 / 文档生成
集成 KB RAG 增强: 对话前自动查询知识库，将相关知识注入 prompt
"""

import json
import logging
import re

from agents.base import BaseAgent, AgentMessage, run_agent, detect_data_query
from agents.kb_agent import (
    kb_rag_bundle, _simple_query_variants,
    _build_sources_section, _strip_fabricated_links,
)

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

# 纯寒暄/元消息 (整条消息仅为问候/致谢/测试词 + 标点): 跳过 KB 检索。
# 语义检索对任何查询都会返回"最像的邻居", 不过滤就会给"你好"挂上随机参考来源。
_CHITCHAT_RE = re.compile(
    r'(你好|您好|嗨|哈喽|hello|hi|hey|谢谢|多谢|感谢|辛苦|再见|拜拜|'
    r'好的|嗯|ok|okay|收到|test|测试|ping|健康检查)'
    r'[\s!！?？。.,，、~～a-zA-Z0-9^\-]*',
    re.IGNORECASE)

SYSTEM_PROMPT = """你是 Z-OPS 运维智能体中的通用问答助手。

你的能力:
1. 回答运维相关知识问题 (Linux、网络、VMware、KVM、PVE、信创环境等)
2. 生成运维文档和报告
3. 解释技术概念
4. 提供最佳实践建议

回答要求:
- 用专业的中文回答，尽量详细，提供具体的操作步骤、命令示例或配置片段
- 如果提供了内部知识库内容，请充分利用其中所有相关信息，不要遗漏
- 对于查询类问题（如“XX有什么VM”），应列出所有已知的具体资源信息
- 必要时可以用表格、列表等格式组织信息，使回答更清晰

⚠️ 链接规则（严格遵守）:
- 严禁自行编造、拼凑或猜测任何文档 URL / 链接
- 正文中不要输出知识库文档的 URL；相关文档的真实链接会由系统在回答末尾统一附上
- 如需指代某篇文档，只写文档标题，不要写链接地址
"""


class GeneralAgent(BaseAgent):
    name = "general_agent"
    description = "通用问答 / 知识库检索 / 文档生成"

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
                    "通用问答", "文档生成", "运维知识", "Linux",
                    "VMware", "KVM", "PVE", "信创", "总结",
                ],
                tools=[],
            )
        except Exception as e:
            logger.debug(f"[general_agent] ToolPool 注册异常: {e}")

    async def handle(self, msg: AgentMessage):
        action = msg.action or "chat"

        if action == "chat":
            await self._chat(msg)
        elif action == "doc_gen":
            await self._doc_gen(msg)
        elif action == "summarize":
            await self._summarize(msg)
        else:
            # 默认作为对话处理
            await self._chat(msg)

    async def _chat(self, msg: AgentMessage):
        """通用对话，自动 RAG 增强 + Wiki 知识检索 + 对话历史上下文"""
        prompt = msg.text or msg.params.get("text", "")
        if not prompt:
            await self.reply(msg, error="缺少问题内容")
            return

        # 数据查询类问题边界: 用户要查生产环境实时数据 (备份状态/清单等),
        # 通用问答 Agent 无此能力 → 转发给 gcoc_agent 数据查询助手，由它查询真实数据并回复。
        # 保持原 msg_id 转发，用户在同一个 SSE 流中直接拿到真实查询结果。
        # MemoryNudge (Hermes): 数据查询类问题时提醒用户相关历史
        try:
            nudge = await self.memory_nudge(msg.session_id, prompt, actor=msg.username or "")
            if nudge:
                await self.send_progress(msg, nudge.split("\n")[1][:80] if len(nudge.split("\n")) > 1 else "💭 有相关历史记忆")
        except Exception:
            pass

        data_hint = detect_data_query(prompt)
        if data_hint:
            if await self.forward_to_gcoc(msg, prompt):
                await self.send_progress(msg, "检测到数据查询类问题，已转交数据查询助手查询生产数据，请稍候...")
                await self.log_daily(f"general 数据查询转发 gcoc_agent: {prompt[:60]}", level="info", category="general")
                return  # 不自行 reply，避免双回复提前终止 SSE 流
            await self.send_progress(msg, "数据查询助手暂时不可用...")
            await self.reply(msg, result={"text": (
                f"这是【实时数据查询】类问题（{data_hint}），数据查询助手当前不在线，"
                "请稍后重试。通用问答助手无法查询生产环境的实时备份数据。"
            )})
            await self.log_daily(f"general 数据查询转发失败(降级边界): {prompt[:60]}", level="warn", category="general")
            return

        # 清洗情绪化用语，避免触发 LLM 安全过滤
        cleaned = self.sanitize_user_text(prompt)
        if cleaned != prompt:
            logger.info(f"[general_agent] 文本清洗: '{prompt}' → '{cleaned}'")
            prompt = cleaned

        # 改写纠错/否定措辞，避免被 OneAPI 内容安全网关误判为提示词注入
        rewritten = self.rewrite_for_safety(prompt)
        if rewritten != prompt:
            logger.info(f"[general_agent] 措辞改写(防网关误判): '{prompt}' → '{rewritten}'")
            prompt = rewritten

        # 立即通知前端：通用问答 Agent 已接管
        await self.send_progress(msg, "通用问答助手正在处理...")

        # 构建对话历史上下文 (统一使用 BaseAgent 辅助方法, 支持 SessionEngine compact 感知)
        context_prefix = await self._build_history_context(msg)

        # ─── 双路知识增强：KB RAG + LLM-Wiki ───
        kb_matched = False
        wiki_matched = False
        kb_results: list[dict] = []
        kb_valid_urls: set = set()
        
        # 1. KB RAG 增强（外部知识库）— 关键词提取 + 多路召回
        # 寒暄/元消息 (你好/谢谢/测试/ping 等) 跳过检索: 语义检索对任何查询都返回邻居,
        # 不加门槛会给自我介绍挂上毫不相关的"参考来源" (2026-10-18 用户反馈)
        if config.KB_RAG_ENABLED and not _CHITCHAT_RE.fullmatch(prompt.strip().lower()):
            try:
                # 提取查询关键词，避免完整自然语言句子搜索失败
                queries = _simple_query_variants(prompt)
                # 额外提取纯英文关键词（如 powershell、vm、dns）
                en_words = re.findall(r'[a-zA-Z][a-zA-Z0-9_]+', prompt)
                if en_words:
                    for w in en_words[:3]:
                        if w.lower() not in [q.lower() for q in queries]:
                            queries.append(w)
                # 提取纯数字关键词（如 GID 46445、端口 7789）
                # 注意: \b 在中文环境不工作，直接用 \d+
                num_words = re.findall(r'\d{3,}', prompt)
                if num_words:
                    for n in num_words[:3]:
                        if n not in queries:
                            queries.append(n)

                kb_context, kb_results = await kb_rag_bundle(
                    prompt, queries=queries if len(queries) > 1 else None,
                    strong_only=True)
                if kb_context:
                    prompt = f"{kb_context}\n用户问题: {prompt}"
                    kb_matched = True
                    kb_valid_urls = {(d.get("url") or "").strip() for d in kb_results if d.get("url")}
                    logger.info(f"[general_agent] RAG 命中知识库 (强相关, queries={len(queries)})")
                else:
                    logger.info(f"[general_agent] KB RAG 无强相关匹配 (queries={queries})")
            except Exception as e:
                logger.warning(f"[general_agent] KB RAG 查询失败: {e}")
        
        # 2. LLM-Wiki 检索增强（内部结构化记忆）
        try:
            wiki_context = await self.wiki_rag_context(prompt, top_k=5)
            if wiki_context:
                prompt = f"{wiki_context}\n用户问题: {prompt}"
                wiki_matched = True
                logger.info(f"[general_agent] Wiki 命中 {wiki_context.count('###')} 条知识")
        except Exception as e:
            logger.warning(f"[general_agent] Wiki 检索失败: {e}")

        # ─── P2 收尾 (task-c17): 已发布技能注入 (closed loop 消费端) + 用户跨会话记忆 ───
        skill_block, skill_names = await self.skill_context_for(msg)
        if skill_block:
            prompt = f"{skill_block}\n用户问题: {prompt}"
            logger.info(f"[general_agent] 技能注入: {skill_names}")
        mem_block = await self.user_memory_context(msg)
        memory_used = bool(mem_block)
        if mem_block:
            prompt = f"{mem_block}\n{prompt}"

        # Hermes MemorySystem 偏好层 (风格/禁忌; 与事实层互补)
        pref_block = await self.preference_context(msg.username or "", prompt)
        if pref_block:
            prompt = f"{pref_block}\n{prompt}"
        # 通知前端：知识检索完成，正在调用 LLM
        await self.send_progress(msg, "正在调用大模型生成回答，请稍候...")

        # 组装最终 prompt: 历史上下文 + KB RAG + Wiki + 技能 + 用户记忆 + 当前问题
        final_prompt = context_prefix + prompt if context_prefix else prompt

        # 长调用心跳: 模型慢时每 30s 向用户发进度，同时刷新后端 SSE 空闲截止时间
        async def _llm_wait(elapsed: int):
            await self.send_progress(msg, f"⏳ 模型仍在生成中 (已等待 {elapsed}s)，请稍候...")

        # 流式增量: LLM 边生成边推送到前端实时渲染 (根治"干等→超时"体验问题)
        async def _llm_delta(seg: str):
            await self.send_delta(msg, seg)

        # 思考链流式上屏 (DeepSeek thinking mode): 思考过程逐段推送，前端折叠展示
        async def _llm_thinking(seg: str):
            await self.send_delta(msg, seg, kind="thinking")

        result = await self.llm_chat(prompt=final_prompt, system=SYSTEM_PROMPT,
                                     on_wait=_llm_wait, on_delta=_llm_delta,
                                     on_reasoning=_llm_thinking,
                                     username=(msg.username or ""),
                                     session_id=(msg.session_id or ""))
        # P2 收尾: 技能使用结果回写 (verified=False — 回复成功≠业务验收)
        await self.record_skill_outcomes(skill_names, success=bool(result and result.strip()))

        # LLM 失败兜底：如果返回空或异常，给出有意义的提示而非空白
        raw_query = msg.text or msg.params.get("text", "")
        if not result or not result.strip():
            kb_hint = "\n\n**以下是从内部知识库检索到的相关信息：**\n" if kb_matched else ""
            result = (
                f"抱歉，大模型服务暂时不可用（响应超时），无法生成关于「{raw_query[:50]}」的回答。{kb_hint}"
                f"\n\n建议您：\n"
                f"1. 稍后重试该问题\n"
                f"2. 如果问题紧急，请直接联系运维团队"
            )
            await self.send_progress(msg, "⚠️ LLM 响应超时，已切换为提示信息")
        elif kb_matched and not self._is_gateway_refusal(result):
            # 网关安全误判拒答回复不拼 KB 来源 (拒答+不相关来源雪上加霜)
            # 剥离 LLM 正文中可能编造的 KB 链接，并由程序统一附上真实来源
            result = _strip_fabricated_links(result, kb_valid_urls)
            sources_section = _build_sources_section(kb_results)
            if sources_section:
                result = f"{result}\n\n{sources_section}"

        await self.reply(msg, result={**({"skills": skill_names} if skill_names else {}), **({"memory_used": True} if memory_used else {}), 
            "text": result,
            "kb_enhanced": kb_matched,
            "wiki_enhanced": wiki_matched
        })

        # 记录到聊天历史
        r = await self.get_redis()
        history_entry = json.dumps({
            "role": "assistant",
            "agent": self.name,
            "content": result,
            "query": prompt[:100],
            "channel": msg.channel_source,
            "msg_id": msg.msg_id
        }, ensure_ascii=False)
        await r.rpush(config.KEY_CHAT_HISTORY, history_entry)
        if config.CHAT_HISTORY_MAX > 0:
            await r.ltrim(config.KEY_CHAT_HISTORY, -config.CHAT_HISTORY_MAX, -1)

        kb_label = '命中' if kb_matched else '无'
        wiki_label = '命中' if wiki_matched else '无'
        # 完整问答全文由 BaseAgent.reply() 兜底收录，这里只记增强命中摘要
        await self.log_daily(
            f"问答: {prompt[:50]} → 已回复 (KB={kb_label}, Wiki={wiki_label})",
            level="success", category="chat",
            session_id=msg.session_id, username=(msg.username or "")
        )

    async def _doc_gen(self, msg: AgentMessage):
        """文档生成"""
        topic = msg.text or msg.params.get("topic", "")
        doc_type = msg.params.get("type", "runbook")

        prompt = f"请为以下主题生成运维{doc_type}文档:\n\n{topic}"
        result = await self.llm_chat(prompt=prompt, system=SYSTEM_PROMPT)
        await self.reply(msg, result={"text": result, "type": doc_type})
        await self.log_daily(f"文档生成: {topic[:50]}", level="success", category="chat")

    async def _summarize(self, msg: AgentMessage):
        """内容总结"""
        content = msg.text or msg.params.get("content", "")
        prompt = f"请总结以下运维信息的关键要点:\n\n{content}"
        result = await self.llm_chat(prompt=prompt, system=SYSTEM_PROMPT)
        await self.reply(msg, result={"text": result})
        await self.log_daily(f"内容总结: {content[:50]}", level="success", category="chat")


if __name__ == "__main__":
    run_agent(GeneralAgent())
