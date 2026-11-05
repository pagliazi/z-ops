"""
Z-OPS QueryEngine — 统一 LLM 调用引擎
参考 claude-code QueryEngine.ts + claw-code QueryEnginePort

核心能力:
1. 统一 LLM 调用入口 (OpenAI/DeepSeek/Anthropic/OneAPI)
2. Tool-call 循环 (LLM → 选择工具 → 执行 → 结果返回 LLM → 生成回复)
3. 流式输出 (SSE 兼容)
4. 自动重试 + token 追踪
5. 多轮 turn 管理 (max_turns / max_budget_tokens)

参考:
- claude-code QueryEngine.ts: streaming, tool-call loops, thinking mode, retry, token counting
- claw-code QueryEnginePort: max_turns, max_budget_tokens, submit_message, stream_submit_message
"""

import json
import re
import time
import logging
import asyncio
import itertools
from dataclasses import dataclass, field, asdict
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

import httpx

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from utils.safety import is_gateway_refusal as _is_gateway_refusal, \
    rewrite_messages_for_safety as _rewrite_messages_for_safety

logger = logging.getLogger("zops")

# v4.0: 进程内请求序号 (未显式传 context_id 时作 checkpoint 关联)
_ID_SEQ = itertools.count(1)


def _id_seq() -> int:
    return next(_ID_SEQ)


def _parse_xml_tool_calls(text):
    """Parse XML-format tool calls from LLM output."""
    import re as _re
    import json as _json
    tool_calls = []
    tc_pat = _re.compile(r'<tool_call>(.*?)</tool_call>', _re.DOTALL)
    arg_pat = _re.compile(r'<arg_key>([^<]+)</arg_key>\s*<arg_value>([^<]*)</arg_value>', _re.DOTALL)
    for idx, tc_match in enumerate(tc_pat.finditer(text)):
        tc_text = tc_match.group(1)
        name_match = _re.match(r'\s*(\w+)', tc_text)
        if not name_match:
            continue
        fn_name = name_match.group(1)
        args = {}
        for arg_match in arg_pat.finditer(tc_text):
            args[arg_match.group(1).strip()] = arg_match.group(2).strip()
        tool_calls.append({
            'id': 'call_xml_' + str(idx) + '_' + fn_name,
            'type': 'function',
            'function': {
                'name': fn_name,
                'arguments': _json.dumps(args) if args else '{}'
            }
        })
    cleaned = tc_pat.sub('', text).strip()
    return cleaned, tool_calls


# ═══ v4.0 工具结果预算 (借鉴 claude-code applyToolResultBudget: 超限转内容引用) ═══

_REF_STORE: Dict[str, dict] = {}  # ref_id -> {text, actor, session, ts} (进程内，随会话存活)
# P0: 引用绑定归属 (actor+session) — read_ref 校验, 不能通过引用 ID 跨任务/跨用户取回原文
_REF_STORE_MAX = 500              # 引用存储上限 (超出时淘汰最旧 100 条)
_REF_SEQ = 0

# read_ref 元工具: LLM 可用引用号取回被预算裁剪的完整工具结果
_READ_REF_SPEC = {
    "type": "function",
    "function": {
        "name": "read_ref",
        "description": "取回工具结果的完整原文。当工具结果中出现 [REF:tN] 引用且确需完整内容时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "引用编号，如 t12"}
            },
            "required": ["ref"]
        }
    }
}


class _RefExecution:
    """read_ref 结果适配器 (仅需 output 属性)"""
    def __init__(self, output: str):
        self.output = output


# ── v4.0 (Phase 2.3) 工具 schema 延迟加载 (借鉴 claude-code ToolSearch) ──
# 初始只注入 {name: 一句话描述} 精简清单 + search_tool_detail 元工具;
# LLM 需要完整参数 schema 时先调元工具, 引擎将完整 spec 懒注入后续轮次。

_SEARCH_TOOL_DETAIL_SPEC = {
    "type": "function",
    "function": {
        "name": "search_tool_detail",
        "description": (
            "获取工具的完整参数定义。当前工具列表只展示名称与一句话说明; "
            "当你需要调用某工具但不确定其参数格式时, 先调用本工具并传 tool_name 查看完整 schema, "
            "随后即可正常调用该工具。不传 tool_name 则返回全部工具清单。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "description": "工具名 (可选; 缺省返回全部工具清单)"}
            },
        },
    },
}


def _one_line(desc: str, limit: int = 40) -> str:
    """从工具描述提取一句话摘要 (延迟加载的精简清单用)"""
    if not desc:
        return ""
    first = re.split(r"[。;\n]", desc, maxsplit=1)[0].strip() or desc.strip()
    if len(first) > limit:
        first = first[:limit].rstrip() + "…"
    return first


def _store_ref(text: str, actor: str = "", session_id: str = "") -> str:
    """存储超限工具结果全文，返回引用号 (P0: 绑定 actor+session 归属)"""
    global _REF_SEQ
    _REF_SEQ += 1
    ref_id = f"t{_REF_SEQ}"
    _REF_STORE[ref_id] = {
        "text": text,
        "actor": (actor or "").strip(),
        "session": (session_id or "").strip(),
        "ts": time.time(),
    }
    if len(_REF_STORE) > _REF_STORE_MAX:
        for k in list(_REF_STORE.keys())[:100]:
            _REF_STORE.pop(k, None)
    return ref_id


def _ref_allows(ref_entry: dict, actor: str, session_id: str) -> bool:
    """引用归属校验: 同 actor 且同 session 才可取回 (fail-closed)

    引用无归属信息 (旧数据/未知来源) → 拒绝, 不能通过引用 ID 绕过权限。
    """
    if not isinstance(ref_entry, dict):
        return False
    if not ref_entry.get("actor") or not ref_entry.get("session"):
        return False
    return ref_entry["actor"] == (actor or "").strip() and ref_entry["session"] == (session_id or "").strip()


async def enforce_tool_risk(t_def, tool_name: str, params: dict,
                            agent_hint: str = "", session_hint: str = "",
                            msg_context_id="", actor_hint: str = ""):
    """统一执行 gate (SPEC P0: 覆盖 QueryEngine 工具循环的风险判定)

    - destructive: 所有配置组合下均不调用 executor (APPROVAL_ENABLED 仅控制是否留审批单);
    - write: 业务权限点 + 资源范围校验 (fail-closed; 无映射/无主体/范围不明 → 拒绝);
    - read / 未知工具 (t_def None): 放行交由 executor 处理 (未知工具在 executor 层拒绝);
    - risk 值归一: 字符串/别名/大小写统一; 异常值按 write 处理 (不放行)。

    Returns: ToolExecution (permission_denied) 或 None (放行)。
    """
    _risk_raw = getattr(t_def, "risk_level", None) if t_def else None
    _risk = str(getattr(_risk_raw, "value", _risk_raw) or "").strip().lower()
    if _risk not in ("read", "write", "destructive") and _risk_raw is not None:
        logger.warning(f"[query_engine] 工具 {tool_name} risk_level 异常值 "
                       f"'{_risk_raw}', 按 write 处理 (fail-closed)")
        _risk = "write"
    if _risk == "destructive":
        import config as _cfg
        if _cfg.APPROVAL_ENABLED:
            from core.approval import gate_destructive_call
            return await gate_destructive_call(
                tool_def=t_def, params=params,
                exec_fn=None,  # 审批只走流程, 不执行 (P0: exec_fn 不再提供)
                agent=agent_hint, session_id=session_hint,
                msg_id=str(msg_context_id), username=actor_hint,
            )
        logger.warning(f"[query_engine] destructive 工具 {tool_name} 在 APPROVAL_ENABLED=false 下仍拦截 "
                       "(P0: 高风险无绕过开关, 审批服务停用不降级为执行)")
        from core.tool_schema import ToolExecution
        return ToolExecution(
            tool_name=tool_name, success=False,
            output=json.dumps({
                "permission_denied": True,
                "tool": tool_name,
                "reason": "审批服务未启用, 高风险操作已拦截 (系统在所有配置下均不自动执行)",
                "advice": "请联系管理员启用审批服务后重试; 紧急变更走线下变更流程",
            }, ensure_ascii=False),
            error="approval_disabled_blocked",
            permission_denied=True,
        )
    if _risk == "write":
        from core.bizperms import check_write_allowed
        _ok_w, _why_w, _req_w = await check_write_allowed(actor_hint, tool_name, params)
        if not _ok_w:
            logger.warning(f"[query_engine] write 工具拦截 {tool_name}: {_why_w} (actor={actor_hint or '-'})")
            from core.tool_schema import ToolExecution
            return ToolExecution(
                tool_name=tool_name, success=False,
                output=json.dumps({
                    "permission_denied": True,
                    "tool": tool_name,
                    "reason": _why_w,
                }, ensure_ascii=False),
                error="permission_denied",
                permission_denied=True,
            )
        logger.info(f"[query_engine] write 级工具 {tool_name} 放行 (权限+范围已校验, 已记入审计链)")
    return None



# ═══ QueryEngine 配置 (参考 claw-code QueryEngineConfig) ═══

@dataclass
class QueryConfig:
    """LLM 查询引擎配置"""
    max_turns: int = 8                     # 单次查询最大工具调用轮数
    max_budget_tokens: int = 30000         # 单次查询 token 预算
    max_retry: int = 2                     # LLM 调用失败重试次数
    retry_delay: float = 1.0               # 重试间隔 (秒)
    temperature: float = 0.7
    max_output_tokens: int = 8192
    enable_tool_calls: bool = True         # 启用 tool calling
    tool_choice: str = "auto"              # auto / none / required
    stream: bool = False                   # 是否流式输出


# ═══ Turn 结果 (参考 claw-code TurnResult) ═══

@dataclass
class TurnResult:
    """单轮 LLM 交互结果"""
    prompt: str = ""
    output: str = ""
    tool_calls: List[dict] = field(default_factory=list)   # LLM 请求的工具调用
    tool_results: List[dict] = field(default_factory=list)  # 工具执行结果
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    stop_reason: str = ""    # completed / max_turns / max_budget / error
    error: str = ""


@dataclass
class QueryUsage:
    """查询用量追踪 (参考 claw-code UsageSummary + claude-code cost-tracker)"""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tool_calls: int = 0
    total_turns: int = 0
    total_latency_ms: float = 0.0

    def add_turn(self, turn: TurnResult):
        self.total_input_tokens += turn.input_tokens
        self.total_output_tokens += turn.output_tokens
        self.total_tool_calls += len(turn.tool_calls)
        self.total_turns += 1
        self.total_latency_ms += turn.latency_ms

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    def to_dict(self) -> dict:
        return asdict(self)


# ═══ QueryEngine ═══

class QueryEngine:
    """
    统一 LLM 调用引擎
    
    参考:
    - claude-code QueryEngine.ts: 核心 LLM 调用 + tool-call 循环 + 流式
    - claw-code QueryEnginePort: submit_message + stream_submit_message + compact
    
    使用方式:
        engine = QueryEngine(llm_config)
        result = await engine.query(
            messages=[...],
            tools=[...],   # ToolDef 列表
            system="...",
        )
    """

    def __init__(self, llm_config: dict = None):
        """
        Args:
            llm_config: LLM 配置 (provider/model/api_key/base_url 等)
                        如果为 None，从 Redis/config 动态读取
        """
        self._llm_config = llm_config or {}
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=180,  # LLM 多轮工具调用可能耗时较长
                verify=False,
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            )
        return self._http_client

    def _resolve_config(self) -> dict:
        """解析 LLM 配置 (provider/model/api_key/base_url)"""
        cfg = dict(self._llm_config)
        if not cfg.get("provider"):
            cfg["provider"] = config.LLM_PROVIDER
        if not cfg.get("model"):
            cfg["model"] = config.LLM_MODEL

        provider = cfg["provider"]
        # base_url 解析 (通用查找表，不再硬编码 provider)
        if not cfg.get("base_url"):
            if provider == "oneapi":
                cfg["base_url"] = config.ONEAPI_BASE_URL.rstrip("/")
            else:
                # 从 PROVIDER_URL_MAP 查找, fallback 到 OpenAI
                try:
                    from webchat_api import PROVIDER_URL_MAP
                    cfg["base_url"] = PROVIDER_URL_MAP.get(provider, "https://api.openai.com/v1").rstrip("/")
                except ImportError:
                    _fallback_urls = {
                        "deepseek": "https://api.deepseek.com/v1",
                        "openai": "https://api.openai.com/v1",
                        "ollama": "http://127.0.0.1:11434/v1",
                    }
                    cfg["base_url"] = _fallback_urls.get(provider, "https://api.openai.com/v1")

        # api_key 解析
        if not cfg.get("api_key"):
            if provider == "oneapi":
                cfg["api_key"] = config.ONEAPI_API_KEY
            elif provider == "deepseek":
                cfg["api_key"] = config.DEEPSEEK_API_KEY or config.OPENAI_API_KEY
            elif provider == "anthropic":
                cfg["api_key"] = config.ANTHROPIC_API_KEY
            else:
                cfg["api_key"] = config.OPENAI_API_KEY or config.DEEPSEEK_API_KEY

        return cfg

    def _clip_result(self, text: str, actor: str = "", session_id: str = "") -> str:
        """v4.0 工具结果预算裁剪 (借鉴 claude-code applyToolResultBudget)

        超过 TOOL_RESULT_MAX_CHARS 的结果: 保留前半 + [REF:tN] 引用号，
        原文存进程内引用库，LLM 可用 read_ref 元工具按需取回。
        P0: 引用绑定 actor+session 归属, read_ref 校验后才可取回。
        """
        if not text:
            return text
        try:
            max_chars = config.TOOL_RESULT_MAX_CHARS
        except Exception:
            max_chars = 6000
        if len(text) <= max_chars:
            return text
        ref_id = _store_ref(text, actor=actor, session_id=session_id)
        head = text[:max_chars // 2].rsplit("\n", 1)[0] if "\n" in text[:max_chars // 2] else text[:max_chars // 2]
        # 懒注入 read_ref 元工具
        specs = getattr(self, "_active_tool_specs", None)
        if specs is not None and all(
            s.get("function", {}).get("name") != "read_ref" for s in specs
        ):
            specs.append(_READ_REF_SPEC)
            logger.info("[query_engine] 已注入 read_ref 元工具 (工具结果预算裁剪生效)")
        logger.info(f"[query_engine] 工具结果超预算: {len(text)} -> {len(head)} chars + [REF:{ref_id}]")
        return (
            f"{head}\n\n[REF:{ref_id}] (原始内容共 {len(text)} 字符已省略，"
            f"如需完整原文请调用 read_ref 工具，传 ref=\"{ref_id}\")"
        )

    def _expand_lazy_spec(self, name: str) -> None:
        """v4.0 (Phase 2.3): 将某工具的完整 spec 替换进当前活跃工具列表 (后续轮次全量注入)"""
        full = (getattr(self, "_full_tool_spec_map", None) or {}).get(name)
        specs = getattr(self, "_active_tool_specs", None)
        if not full or specs is None:
            return
        for i, s in enumerate(specs):
            if s.get("function", {}).get("name") == name:
                if s is full or s == full:
                    return
                specs[i] = full
                logger.info(f"[query_engine] 惰性展开工具 schema: {name}")
                return

    # ─── 核心查询 ───

    async def query(
        self,
        messages: List[dict],
        system: str = "",
        tools: List = None,
        query_config: QueryConfig = None,
        tool_executor: Callable = None,
        context_id: str = "",
        agent_hint: str = "",
        session_hint: str = "",
        lazy_tool_schemas: bool = False,
        actor_hint: str = "",
    ) -> TurnResult:
        """
        执行 LLM 查询 (支持 tool-call 循环)

        参考 claude-code QueryEngine 的 tool-call loop:
        1. 发送消息到 LLM
        2. LLM 返回 tool_calls → 执行工具 → 结果追加到消息
        3. 重复直到 LLM 不再请求工具调用 或 达到 max_turns

        Args:
            messages: 消息列表 [{role, content}]
            system: system prompt
            tools: ToolDef 列表 (传入时启用 tool calling)
            query_config: 查询配置
            tool_executor: 工具执行函数 async (name, params) -> ToolExecution
            context_id: 请求标识 (msg_id，checkpoint 关联用)
            agent_hint: 发起 agent 名 (审计/恢复通知用)
            session_hint: 会话 ID (审批挂起恢复用)
            lazy_tool_schemas: v4.0 (Phase 2.3) 工具 schema 延迟加载 (opscore_agent 试点)
            actor_hint: 发起用户 (P0: write 权限门/引用归属; 缺省取 RequestContext)

        Returns:
            TurnResult 包含最终输出和用量统计
        """
        qc = query_config or QueryConfig()
        cfg = self._resolve_config()
        usage = QueryUsage()
        # v4.0: checkpoint 上下文 (未提供时退化为进程内自增 id)
        msg_context_id = context_id or f"q{_id_seq()}"
        # P0: 发起主体 — 显式传入优先, 否则取 RequestContext (服务端认证产生的 actor)
        if not actor_hint:
            try:
                from core.request_context import current_username as _rc_user
                actor_hint = _rc_user()
            except Exception:
                actor_hint = ""

        # v4.0: 工具名 → ToolDef 映射 (per-tool 超时查询用)
        tool_map = {}
        if tools:
            for t in tools:
                if hasattr(t, "name"):
                    tool_map[t.name] = t
        # v4.0: prompt_too_long 恢复状态 (每轮至多 reactive collapse 一次)
        _reactive_collapse_done = False

        # 构建初始消息
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        # 构建 tools 参数 (LLM function calling 格式)
        tool_specs = None
        if tools and qc.enable_tool_calls:
            tool_specs = [t.to_function_spec() for t in tools if hasattr(t, 'to_function_spec')]
        # v4.0 (Phase 2.3): 工具 schema 延迟加载 — 精简 spec + search_tool_detail 元工具,
        # 完整 schema 按需展开 (借鉴 claude-code ToolSearch)
        self._full_tool_spec_map = None
        self._lazy_tool_lines = None
        if tool_specs and lazy_tool_schemas and len(tool_specs) >= max(
            1, getattr(config, "TOOL_SCHEMA_LAZY_MIN_TOOLS", 10)
        ):
            full_map = {s.get("function", {}).get("name", ""): s for s in tool_specs}
            self._full_tool_spec_map = full_map
            self._lazy_tool_lines = {
                n: _one_line((s.get("function", {}) or {}).get("description", ""))
                for n, s in full_map.items()
            }
            saved = sum(len(json.dumps(s, ensure_ascii=False)) for s in full_map.values())
            # 精简 spec: 仅 name + 一句话描述 (parameters 省略 — OpenAI 规范中可选,
            # 完整参数经 search_tool_detail 按需展开)
            tool_specs = [
                {"type": "function", "function": {"name": n, "description": d}}
                for n, d in self._lazy_tool_lines.items()
            ] + [_SEARCH_TOOL_DETAIL_SPEC]
            slim = sum(len(json.dumps(s, ensure_ascii=False)) for s in tool_specs)
            logger.info(
                f"[query_engine] 工具 schema 延迟加载: {len(full_map)} 个工具精简注入 "
                f"({saved} -> {slim} chars, -{(1 - slim / saved) * 100:.0f}%"
                + (f", 阈值 {config.TOOL_SCHEMA_LAZY_MIN_TOOLS}" if hasattr(config, 'TOOL_SCHEMA_LAZY_MIN_TOOLS') else "")
                + ")"
            )
        # v4.0: read_ref 元工具懒注入 (首次裁剪发生时追加到工具列表)
        self._active_tool_specs = tool_specs

        final_output = ""
        all_tool_calls = []
        all_tool_results = []
        total_input = 0
        total_output = 0
        start_time = time.time()

        import asyncio as _asyncio

        for turn in range(qc.max_turns):
            # 检查 token 预算
            if usage.total_tokens > qc.max_budget_tokens:
                logger.warning(f"[query_engine] token 预算耗尽: {usage.total_tokens}/{qc.max_budget_tokens}")
                # 尝试用已有数据生成回复
                if not final_output and all_tool_results:
                    try:
                        sr = await self._call_llm(messages=full_messages, tools=None, config=cfg, query_config=qc, turn_number=999)
                        final_output = sr.get("content", "")
                    except Exception as budget_err:
                        logger.warning(f"[query_engine] 预算耗尽兑底总结失败: {budget_err}")
                try:
                    from core.checkpoint import mark_query_done
                    mark_query_done(msg_context_id)
                except Exception:
                    pass
                return TurnResult(
                    output=final_output or "token 预算已耗尽",
                    stop_reason="max_budget",
                    input_tokens=total_input,
                    output_tokens=total_output,
                    tool_calls=all_tool_calls,
                    tool_results=all_tool_results,
                    model=cfg.get("model", ""),
                    provider=cfg.get("provider", ""),
                    latency_ms=(time.time() - start_time) * 1000,
                )

            # v4.0: 每次调 LLM 前执行五级上下文整形 (只改投影，不改存储)
            try:
                from core.context_shapers import apply_context_shapers
                shaped = apply_context_shapers(full_messages)
                if shaped is not full_messages:
                    full_messages = shaped
            except Exception as _shaper_err:
                logger.debug(f"[query_engine] context shapers 跳过: {_shaper_err}")

            # 调用 LLM (第一轮强制调用工具，后续轮次自动)
            turn_result = await self._call_llm(
                messages=full_messages,
                tools=tool_specs,
                config=cfg,
                query_config=qc,
                turn_number=turn,
            )

            total_input += turn_result.get("input_tokens", 0)
            total_output += turn_result.get("output_tokens", 0)
            usage.total_input_tokens += turn_result.get("input_tokens", 0)
            usage.total_output_tokens += turn_result.get("output_tokens", 0)

            if turn_result.get("error"):
                err_text = str(turn_result["error"])
                # v4.0: prompt_too_long 恢复 (借鉴 claude-code reactive compaction):
                # 先触发一次 reactive collapse (读时投影压缩 full_messages)，再重试本曾
                if "prompt_too_long" in err_text or "context_length_exceeded" in err_text \
                        or "maximum context length" in err_text.lower():
                    if not _reactive_collapse_done:
                        _reactive_collapse_done = True
                        try:
                            from core.context_shapers import collapse_messages
                            before = len(json.dumps(full_messages, ensure_ascii=False))
                            full_messages = collapse_messages(full_messages)
                            after = len(json.dumps(full_messages, ensure_ascii=False))
                            logger.warning(f"[query_engine] reactive collapse: {before} -> {after} chars，重试本曾")
                            continue
                        except Exception as collapse_err:
                            logger.warning(f"[query_engine] reactive collapse 失败: {collapse_err}")
                try:
                    from core.checkpoint import mark_query_done
                    mark_query_done(msg_context_id)
                except Exception:
                    pass
                return TurnResult(
                    output=final_output,
                    error=turn_result["error"],
                    stop_reason="error",
                    input_tokens=total_input,
                    output_tokens=total_output,
                    model=cfg.get("model", ""),
                    provider=cfg.get("provider", ""),
                    latency_ms=(time.time() - start_time) * 1000,
                )

            content = turn_result.get("content", "") or ""
            tool_calls = turn_result.get("tool_calls") or []
            finish_reason = turn_result.get("finish_reason", "")
            
            # Debug: log message structure sent to LLM
            msg_summary = [f"{m['role']}:{len(str(m.get('content','')))}" for m in full_messages[-5:]]
            logger.info(f"[query_engine] turn {turn+1} messages: total={len(full_messages)}, last5={msg_summary}")
            
            # Debug: log LLM response details
            logger.info(f"[query_engine] turn {turn+1}: finish_reason={finish_reason}, "
                       f"tool_calls={len(tool_calls) if tool_calls else 0}, content_len={len(content) if content else 0}, "
                       f"content_preview={content[:100] if content else 'None'}")
            
            # 没有工具调用 → 直接返回文本结果
            if not tool_calls or not tool_executor:
                final_output = content
                try:
                    from core.checkpoint import mark_query_done
                    mark_query_done(msg_context_id)
                except Exception:
                    pass
                return TurnResult(
                    output=final_output,
                    stop_reason="completed",
                    input_tokens=total_input,
                    output_tokens=total_output,
                    tool_calls=all_tool_calls,
                    tool_results=all_tool_results,
                    model=cfg.get("model", ""),
                    provider=cfg.get("provider", ""),
                    latency_ms=(time.time() - start_time) * 1000,
                )

            # 有工具调用 → 并行执行独立工具 → 追加结果 → 继续循环
            # 参考 claude-code Parallel Prefetch 模式: 多个 tool_calls 并发执行
            assistant_msg = {"role": "assistant", "content": content or ""}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            full_messages.append(assistant_msg)

            # 解析所有 tool_calls
            parsed_calls = []
            for tc in tool_calls:
                fn_name = tc.get("function", {}).get("name", "")
                fn_args_str = tc.get("function", {}).get("arguments", "{}")
                tc_id = tc.get("id", f"call_{turn}_{fn_name}")
                try:
                    fn_args = json.loads(fn_args_str) if isinstance(fn_args_str, str) else fn_args_str
                except json.JSONDecodeError:
                    fn_args = {}
                parsed_calls.append({"name": fn_name, "args": fn_args, "id": tc_id})
                all_tool_calls.append({"name": fn_name, "arguments": fn_args, "id": tc_id})
                logger.info(f"[query_engine] tool-call [{turn+1}/{qc.max_turns}]: {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:100]})")

            # ── 并行执行工具 (参考 claude-code parallel prefetch; v4.0: per-tool 超时隔离) ──
            async def _exec_one(pc):
                """执行单个工具调用，超时/异常均返回结构化结果 (不抛异常，让 LLM 自行决策)"""
                # v4.0: read_ref 元工具拦截 (预算裁剪的原文取回)
                # P0: 引用归属校验 — 同 actor+session 才可取回, 不能跨任务/跨用户读原文
                if pc["name"] == "read_ref":
                    ref_id = str((pc.get("args") or {}).get("ref", ""))
                    entry = _REF_STORE.get(ref_id)
                    if entry is None:
                        return pc, None, {"error": f"引用 {ref_id} 不存在或已过期"}
                    if not _ref_allows(entry, actor_hint, session_hint):
                        logger.warning(
                            f"[query_engine] read_ref 越权拒绝: ref={ref_id} "
                            f"actor={actor_hint or '-'} session={session_hint or '-'} "
                            f"(owner={entry.get('actor') or '-'}/{entry.get('session') or '-'})")
                        return pc, None, {"error": f"引用 {ref_id} 不属于当前请求上下文, 已拒绝取回"}
                    content = entry.get("text", "")
                    logger.info(f"[query_engine] read_ref: {ref_id} ({len(content)} chars)")
                    return pc, _RefExecution(content), None
                # v4.0 (Phase 2.3): search_tool_detail 元工具拦截 (schema 按需展开)
                if pc["name"] == "search_tool_detail" and getattr(self, "_full_tool_spec_map", None):
                    tn = str((pc.get("args") or {}).get("tool_name", "")).strip()
                    full_map = self._full_tool_spec_map
                    lines_map = getattr(self, "_lazy_tool_lines", {}) or {}
                    if tn and tn in full_map:
                        self._expand_lazy_spec(tn)
                        logger.info(f"[query_engine] search_tool_detail: 展开 {tn} 完整 schema (后续轮次全量注入)")
                        content = (
                            f"工具 {tn} 的完整定义如下, 本轮起可直接调用:\n"
                            + json.dumps(full_map[tn], ensure_ascii=False, indent=2)
                        )
                    else:
                        listing = "\n".join(f"- {n}: {d}" for n, d in lines_map.items())
                        prefix = f"未知名 '{tn}', " if tn else ""
                        content = (
                            f"{prefix}可用工具清单如下 (传 tool_name 获取完整参数定义):\n{listing}"
                        )
                    return pc, _RefExecution(content), None
                t_def = tool_map.get(pc["name"])
                # v4.0 Phase 6+: 工具级入账 — 真实工具调用写入 ExecutionRegistry，
                # 可观测性"执行追踪"由此能看到 query_backup_jobs 等具体工具的调用与成败
                # (旧版只记消息级 chat/handle，与 ToolPool 能力面完全对不上 → 数据脱节根因)
                _tool_t0 = time.time()

                async def _record_tool_exec(exec_result, struct_err=None):
                    try:
                        from core.execution_registry import get_execution_registry
                        _ok = struct_err is None and not getattr(exec_result, "error", None)
                        _err = ""
                        if struct_err is not None:
                            _err = str(struct_err.get("error") or f"timeout after {struct_err.get('budget_s')}s")[:200]
                        elif getattr(exec_result, "error", None):
                            _err = str(exec_result.error)[:200]
                        _out = (getattr(exec_result, "output", "") or "")[:200] if exec_result is not None else ""
                        registry = get_execution_registry()
                        await registry.record_tool_call(
                            agent_id=agent_hint or "unknown",
                            tool_name=pc["name"],
                            input_summary=json.dumps(pc.get("args") or {}, ensure_ascii=False)[:200],
                            output_summary=_out if _ok else (_err or _out),
                            success=_ok,
                            latency_ms=(time.time() - _tool_t0) * 1000,
                            session_id=session_hint or "",
                            error=_err,
                            trace_id=str(msg_context_id),
                        )
                    except Exception as _rec_err:
                        logger.debug(f"[query_engine] 工具入账失败 {pc['name']}: {_rec_err}")

                # v4.0 (Phase 2.3): 惰性模式下直接调用未展开的工具 → 顺手展开完整 schema
                if t_def is not None and getattr(self, "_full_tool_spec_map", None) \
                        and pc["name"] in self._full_tool_spec_map:
                    self._expand_lazy_spec(pc["name"])
                # v4.0 (Phase 4.1/4.2) + P0: 风险分级权限管线 (统一执行 gate)
                gated = await enforce_tool_risk(
                    t_def, pc["name"], pc.get("args") or {},
                    agent_hint=agent_hint, session_hint=session_hint,
                    msg_context_id=msg_context_id, actor_hint=actor_hint)
                if gated is not None:
                    await _record_tool_exec(gated)
                    return pc, gated, None
                timeout = t_def.effective_timeout() if t_def and hasattr(t_def, "effective_timeout") else None
                try:
                    if timeout:
                        _r = await _asyncio.wait_for(
                            tool_executor(pc["name"], pc["args"]), timeout=timeout + 5
                        )
                    else:
                        _r = await tool_executor(pc["name"], pc["args"])
                    await _record_tool_exec(_r)
                    return pc, _r, None
                except _asyncio.TimeoutError:
                    budget = timeout or 0
                    logger.error(f"[query_engine] 工具 {pc['name']} 执行超时 ({budget}s)")
                    _struct = {
                        "timeout": True, "tool": pc["name"], "budget_s": budget,
                        "advice": "工具执行超时，请换一种查询方式或缩小范围后重试",
                    }
                    await _record_tool_exec(None, _struct)
                    return pc, None, _struct
                except Exception as e:
                    logger.error(f"[query_engine] 并行执行异常 {pc['name']}: {e}")
                    await _record_tool_exec(None, {"error": str(e)})
                    return pc, None, {"error": str(e), "tool": pc["name"]}

            if len(parsed_calls) > 1:
                logger.info(f"[query_engine] 并行执行 {len(parsed_calls)} 个工具调用")
                results = await _asyncio.gather(
                    *[_exec_one(pc) for pc in parsed_calls],
                    return_exceptions=False
                )
                for pc, exec_result, struct_err in results:
                    if struct_err is not None:
                        # 超时/异常 → 结构化 JSON 结果回传 LLM (不中断循环)
                        result_text = json.dumps(struct_err, ensure_ascii=False)
                    else:
                        result_text = exec_result.output if hasattr(exec_result, 'output') else str(exec_result)
                    result_text = self._clip_result(result_text, actor=actor_hint, session_id=session_hint)
                    all_tool_results.append({"name": pc["name"], "result": result_text})
                    full_messages.append({
                        "role": "tool",
                        "tool_call_id": pc["id"],
                        "content": result_text,
                    })
            else:
                # 单个工具 → 直接执行 (同样带超时与结构化异常)
                for pc in parsed_calls:
                    _, exec_result, struct_err = await _exec_one(pc)
                    if struct_err is not None:
                        result_text = json.dumps(struct_err, ensure_ascii=False)
                    else:
                        result_text = exec_result.output if hasattr(exec_result, 'output') else str(exec_result)
                    result_text = self._clip_result(result_text, actor=actor_hint, session_id=session_hint)
                    all_tool_results.append({"name": pc["name"], "result": result_text})
                    full_messages.append({
                        "role": "tool",
                        "tool_call_id": pc["id"],
                        "content": result_text,
                    })

            usage.total_tool_calls += len(tool_calls)
            usage.total_turns += 1

            # v4.0: Tool-call loop checkpoint (借鉴 LangGraph durable execution 思想)
            # 每轮结束快照在途状态，进程崩溃/重启后可通知用户并支持审批挂起恢复
            try:
                from core.checkpoint import save_query_checkpoint
                save_query_checkpoint(
                    msg_id=str(msg_context_id), turn=turn + 1,
                    messages_tail=full_messages[-4:], usage=usage.to_dict(),
                    agent=agent_hint or "", session_id=session_hint or "",
                )
            except Exception as _ckpt_err:
                logger.debug(f"[query_engine] checkpoint 保存失败: {_ckpt_err}")
            
            # Debug: log tool results being passed to LLM
            for i, tr in enumerate(all_tool_results[-len(tool_calls):]):
                logger.info(f"[query_engine] tool-result [{i+1}]: name={tr['name']}, "
                           f"result_len={len(tr['result'])}, preview={tr['result'][:100]}")

        # 达到 max_turns: 做一次不带工具的 LLM 调用，强制用已获取的数据生成回复
        try:
            from core.checkpoint import mark_query_done
            mark_query_done(msg_context_id)
        except Exception:
            pass
        if not final_output and all_tool_results:
            logger.warning(f"[query_engine] 达到 max_turns={qc.max_turns}，执行最终总结调用 (无工具)")
            try:
                # 明确禁令: 轮数已尽，模型不得再输出工具调用，必须基于已有结果回答。
                # (实测: 无工具模式下 DeepSeek 仍会输出 <tool_call> XML 伪调用，
                #  且 XML 解析会把 content 清空 → 用户只收到 "达到最大工具调用轮数")
                summary_messages = list(full_messages) + [{
                    "role": "user",
                    "content": "（系统指令：已达到工具调用轮数上限，所有查询已结束。"
                               "请直接基于以上已获取的工具结果回答用户原问题；"
                               "若工具结果均为空或无匹配，请明确告知未查询到相关数据并简要说明已执行的查询。"
                               "禁止输出任何 <tool_call> 工具调用。）"
                }]
                summary_result = await self._call_llm(
                    messages=summary_messages,
                    tools=None,  # 不传工具，强制生成文本回复
                    config=cfg,
                    query_config=qc,
                    turn_number=qc.max_turns,
                )
                final_output = summary_result.get("content", "") or ""
                total_input += summary_result.get("input_tokens", 0)
                total_output += summary_result.get("output_tokens", 0)
                # 模型可能仍输出 XML 工具调用文本: 剥除标签取剩余文本
                if "<tool_call>" in final_output:
                    final_output = re.sub(r"<tool_call>.*?</tool_call>", "", final_output,
                                          flags=re.DOTALL).strip()
                if final_output:
                    logger.info(f"[query_engine] 最终总结调用成功, output_len={len(final_output)}")
            except Exception as e:
                logger.error(f"[query_engine] 最终总结调用失败: {e}")
        # 总结仍为空 (模型顽固输出伪调用/全被剥除): 用已执行的工具调用生成结构化兑底，
        # 而非干巴巴的 "达到最大工具调用轮数"
        if not final_output and all_tool_results:
            _lines = [f"- {tr['name']}: {str(tr['result'])[:60]}" for tr in all_tool_results[-6:]]
            final_output = (f"已完成 {len(all_tool_results)} 次工具调用，但各数据源均未返回可直接总结的数据：\n"
                            + "\n".join(_lines)
                            + "\n\n请尝试更换关键词（如具体主机名 / IP / 编号）或稍后重试。")
        
        return TurnResult(
            output=final_output or "达到最大工具调用轮数",
            stop_reason="max_turns",
            input_tokens=total_input,
            output_tokens=total_output,
            tool_calls=all_tool_calls,
            tool_results=all_tool_results,
            model=cfg.get("model", ""),
            provider=cfg.get("provider", ""),
            latency_ms=(time.time() - start_time) * 1000,
        )

    # ─── LLM 调用 (带重试) ───

    async def _call_llm(self, messages: List[dict], tools: List[dict] = None,
                        config: dict = None, query_config: QueryConfig = None,
                        turn_number: int = 0) -> dict:
        """
        调用 LLM API (OpenAI 兼容格式)
        
        返回: {content, tool_calls, input_tokens, output_tokens, error}
        """
        cfg = config or self._resolve_config()
        qc = query_config or QueryConfig()
        provider = cfg.get("provider", "")

        if provider == "anthropic":
            return await self._call_anthropic(messages, tools, cfg, qc)

        # OpenAI 兼容 (DeepSeek / OpenAI / Ollama / OneAPI)
        base_url = cfg.get("base_url", "").rstrip("/")
        api_key = cfg.get("api_key", "")

        body = {
            "model": cfg.get("model", ""),
            "messages": messages,
            "temperature": qc.temperature,
            "max_tokens": qc.max_output_tokens,
        }
        if tools:
            body["tools"] = tools
            # 第一轮强制调用工具，后续轮次自动 (DeepSeek 对 tool_choice=required 支持不完善)
            effective_choice = query_config.tool_choice
            if turn_number > 0 and query_config.tool_choice == "required":
                effective_choice = "auto"
            body["tool_choice"] = effective_choice
            logger.info(f"[query_engine] turn {turn_number+1}: 发送 {len(tools)} 个工具到 LLM, tool_choice={effective_choice}")
        else:
            logger.warning("[query_engine] 未发送工具到 LLM (tools 为空)")

        client = await self._get_http_client()

        # P3 (task-c17): 统一模型调用层 — endpoint 级限流/429 退避/熔断集中在
        # core/model_gateway (引擎的业务级处理 — 上下文超限/截断升限 — 保持不变;
        # 网关耗尽重试后如实返回最终响应, 下方 429 分支兜底)
        from core.model_gateway import get_model_gateway, _BreakerOpen
        _gw_cfg = dict(cfg)
        _gw_cfg.setdefault("base_url", base_url)
        gateway = get_model_gateway()

        # 429 限流重试参数 (OneAPI 网关限流窗口通常为分钟级, 短间隔重试必失败, 需指数退避)
        _RATE_LIMIT_MAX_RETRY = 5
        _RATE_LIMIT_BASE_DELAY = 2.0
        rate_limit_attempt = 0
        general_attempt = 0
        _length_escalation = 0          # v4.0: 输出截断升限重试计数 (上限 3 次)
        _LENGTH_ESCALATION_MAX = 3

        while True:
            try:
                resp = await gateway.request_chat(_gw_cfg, body, client)
                # ─── 网关限流 429: 指数退避重试 (尊重 Retry-After 头) ───
                if resp.status_code == 429:
                    rate_limit_attempt += 1
                    if rate_limit_attempt >= _RATE_LIMIT_MAX_RETRY:
                        return {"error": "LLM 网关限流 (429 Too Many Requests)，请稍后重试",
                                "content": "", "tool_calls": []}
                    retry_after = resp.headers.get("Retry-After", "")
                    try:
                        wait = min(float(retry_after), 60) if retry_after else \
                            min(_RATE_LIMIT_BASE_DELAY * (2 ** (rate_limit_attempt - 1)), 30)
                    except ValueError:
                        wait = min(_RATE_LIMIT_BASE_DELAY * (2 ** (rate_limit_attempt - 1)), 30)
                    logger.warning(f"[query_engine] ⚠️ LLM 网关限流 429 (第{rate_limit_attempt}次), {wait:.0f}s 后退避重试...")
                    await asyncio.sleep(wait)
                    continue
                # v4.0: 400 + 上下文超限 → prompt_too_long 标记 (上层触发 reactive collapse)
                if resp.status_code == 400:
                    err_body = ""
                    try:
                        err_body = resp.text[:500]
                    except Exception:
                        pass
                    low = err_body.lower()
                    if ("context" in low and ("length" in low or "exceed" in low)) \
                            or "prompt_too_long" in low or "maximum context length" in low:
                        logger.warning(f"[query_engine] 上下文超限 (400): {err_body[:150]}")
                        return {"error": f"prompt_too_long: {err_body[:200]}", "content": "", "tool_calls": []}
                resp.raise_for_status()
                data = resp.json()

                choice = data.get("choices", [{}])[0]
                msg = choice.get("message", {})
                usage = data.get("usage", {})

                # v4.0: 输出被截断 (finish_reason=length) → max_tokens 升级重试 (借鉴 claude-code)
                if choice.get("finish_reason") == "length" and _length_escalation < _LENGTH_ESCALATION_MAX:
                    _length_escalation += 1
                    old_cap = body.get("max_tokens", qc.max_output_tokens)
                    body["max_tokens"] = min(old_cap * 2, 24576)
                    logger.warning(
                        f"[query_engine] 输出被截断 (length)，max_tokens {old_cap} -> {body['max_tokens']} 升级重试 "
                        f"({_length_escalation}/{_LENGTH_ESCALATION_MAX})"
                    )
                    continue

                raw_content = msg.get("content", "") or ""
                raw_tool_calls = msg.get("tool_calls", [])
                # Fallback: parse XML-format tool calls if no standard tool_calls
                # (仅限带工具的模式; 无工具的总结调用中模型输出的 <tool_call> 是伪调用,
                #  解析会把 content 清空导致空回复, 应保留文本由上层剥除)
                if tools and not raw_tool_calls and raw_content and '<tool_call>' in raw_content:
                    raw_content, raw_tool_calls = _parse_xml_tool_calls(raw_content)
                    if raw_tool_calls:
                        logger.info(f"[query_engine] XML format tool calls detected: {len(raw_tool_calls)}")

                # ─── 网关安全误判检测: 改写用户消息(含历史上下文)后重试一次 ───
                # 纠错类正常对话会被 OneAPI 内容安全误判为 prompt injection
                if raw_content and not raw_tool_calls and _is_gateway_refusal(raw_content):
                    new_messages = _rewrite_messages_for_safety(messages)
                    if new_messages is not None:
                        logger.warning(f"[query_engine] ⚠️ 检测到网关内容安全误判拒绝，改写措辞后重试: {raw_content[:60]}")
                        try:
                            retry_body = dict(body)
                            retry_body["messages"] = new_messages
                            resp2 = await client.post(
                                f"{base_url}/chat/completions",
                                headers={"Authorization": f"Bearer {api_key}"},
                                json=retry_body,
                            )
                            resp2.raise_for_status()
                            data2 = resp2.json()
                            choice2 = data2.get("choices", [{}])[0]
                            msg2 = choice2.get("message", {})
                            content2 = msg2.get("content", "") or ""
                            tool_calls2 = msg2.get("tool_calls", [])
                            if not tool_calls2 and content2 and '<tool_call>' in content2:
                                content2, tool_calls2 = _parse_xml_tool_calls(content2)
                            if tool_calls2 or (content2 and not _is_gateway_refusal(content2)):
                                raw_content = content2
                                raw_tool_calls = tool_calls2
                                choice = choice2
                                usage = data2.get("usage", {})
                                logger.info("[query_engine] ✅ 改写重试成功，已绕过网关安全误判")
                            else:
                                logger.warning("[query_engine] 改写重试仍被网关拦截")
                        except Exception as re2:
                            logger.warning(f"[query_engine] 改写重试失败: {re2}")
                    else:
                        logger.warning(f"[query_engine] ⚠️ 网关安全误判拒绝且无可改写内容: {raw_content[:60]}")

                return {
                    "content": raw_content,
                    "tool_calls": raw_tool_calls,
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "finish_reason": choice.get("finish_reason", ""),
                }

            except _BreakerOpen as bo:
                # P3: endpoint 熔断/本地限流 → 快速失败 (不再原地重试, 交上层 fallback)
                logger.warning(f"[query_engine] 模型网关熔断/限流: {bo}")
                return {"error": f"模型网关暂不可用 ({bo})", "content": "", "tool_calls": []}

            except Exception as e:
                general_attempt += 1
                logger.warning(f"[query_engine] LLM 调用失败 (attempt {general_attempt}): {e}")
                if general_attempt < max(qc.max_retry, 1):
                    await asyncio.sleep(qc.retry_delay * general_attempt)
                    continue
                # v4.0: 主模型重试耗尽 → fallback 模型链逐个降级 (借鉴 claude-code fallbackModel)
                try:
                    _fallbacks = [m for m in config.LLM_FALLBACK_MODELS if m and m != cfg.get("model")]
                except Exception:
                    _fallbacks = []
                for fb in _fallbacks:
                    try:
                        fb_body = dict(body)
                        fb_body["model"] = fb
                        logger.warning(f"[query_engine] 主模型 {cfg.get('model')} 失败，降级尝试 {fb}")
                        resp_fb = await gateway.request_chat(_gw_cfg, fb_body, client)
                        resp_fb.raise_for_status()
                        data_fb = resp_fb.json()
                        choice_fb = data_fb.get("choices", [{}])[0]
                        msg_fb = choice_fb.get("message", {})
                        return {
                            "content": msg_fb.get("content", "") or "",
                            "tool_calls": msg_fb.get("tool_calls", []),
                            "input_tokens": data_fb.get("usage", {}).get("prompt_tokens", 0),
                            "output_tokens": data_fb.get("usage", {}).get("completion_tokens", 0),
                            "finish_reason": choice_fb.get("finish_reason", ""),
                            "fallback_model": fb,
                        }
                    except Exception as fb_err:
                        logger.warning(f"[query_engine] fallback {fb} 失败: {fb_err}")
                        continue
                return {"error": f"LLM 调用失败: {e}", "content": "", "tool_calls": []}

        return {"error": "LLM 调用达到最大重试次数", "content": "", "tool_calls": []}

    async def _call_anthropic(self, messages: List[dict], tools: List[dict],
                               cfg: dict, qc: QueryConfig) -> dict:
        """Anthropic Claude API 调用"""
        api_key = cfg.get("anthropic_key") or cfg.get("api_key", "")

        # 提取 system
        system_text = ""
        api_messages = []
        for m in messages:
            if m.get("role") == "system":
                system_text += m.get("content", "") + "\n"
            else:
                api_messages.append(m)

        body = {
            "model": cfg.get("model", "claude-sonnet-4-20250514"),
            "max_tokens": qc.max_output_tokens,
            "messages": api_messages,
            "temperature": qc.temperature,
        }
        if system_text.strip():
            body["system"] = system_text.strip()
        if tools:
            # 转换为 Anthropic 格式
            body["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get("parameters", {}),
                }
                for t in tools if "function" in t
            ]

        client = await self._get_http_client()
        try:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

            content = ""
            tool_calls = []
            for block in data.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        }
                    })

            usage = data.get("usage", {})
            return {
                "content": content,
                "tool_calls": tool_calls,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }

        except Exception as e:
            return {"error": f"Anthropic API 调用失败: {e}", "content": "", "tool_calls": []}

    # ─── 简单调用 (无 tool-call) ───

    async def simple_chat(self, prompt: str, system: str = "",
                          temperature: float = 0.7) -> str:
        """简单对话 (无 tool calling)"""
        result = await self.query(
            messages=[{"role": "user", "content": prompt}],
            system=system,
            query_config=QueryConfig(
                temperature=temperature,
                enable_tool_calls=False,
            ),
        )
        return result.output

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


# ═══ 全局单例 ═══

_query_engine: Optional[QueryEngine] = None


def get_query_engine(llm_config: dict = None) -> QueryEngine:
    """获取全局 QueryEngine 单例"""
    global _query_engine
    if _query_engine is None:
        _query_engine = QueryEngine(llm_config)
    return _query_engine
