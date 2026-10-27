"""
Z-OPS 运维智能体 — BaseAgent 基类
所有 Agent 的基础类，提供:
  - Redis Pub/Sub 通信
  - 心跳注册
  - 消息收发协议
  - LLM 调用封装
"""

import asyncio
import json
import time
import uuid
import logging
import traceback
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import re as _re

import httpx
import redis.asyncio as aioredis

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.registry import get_registry, AgentConfig
from core.soul_loader import load_soul
from core.audit import get_audit_logger
from core.policy import get_policy_engine
from core.request_context import (  # P0-A: 请求上下文 (ContextVar 按任务隔离)
    RequestContext,
    current_username,
    current_session_id,
    current_msg_id,
    context_from_agent_message,
    bind_request_context,
    reset_request_context,
)

logger = logging.getLogger("zops")

# Harness 自我进化控制器进程级单例 (失败缓冲需跨消息累积，见 _harness_record_failure)
_harness_controller = None


# ═══ 数据查询类问题边界识别 (kb_agent/general_agent 共用) ═══
# 知识库/通用问答 Agent 无真实数据能力，用户列出具体对象清单 (VM名/主机名/IP)
# 要求查询实时状态时，必须转发给 gcoc_agent 数据查询助手，
# 绝不能编造"请用户自行登录第三方系统核查"式踢皮球回答。

# 主机名模式: 至少两个字母开头 + 必须含数字 (如 ESXI-HK-8025)，避免误匹配纯英文单词 (Veeam/Backup 等)
_HOSTNAME_RE = _re.compile(r'\b[A-Z]{2,}[A-Z0-9]*\d[A-Z0-9]*\b', _re.IGNORECASE)


def detect_data_query(text: str) -> str:
    """检测数据查询类问题 (查具体对象的实时状态/清单)，返回命中类型或空串"""
    t = text or ""
    hosts = [h for h in _HOSTNAME_RE.findall(t) if h.upper() not in ("VM", "DNS", "API", "CPU", "MEM", "SSD", "HDD", "UPS", "CRM", "IDC")]
    if len(hosts) >= 3 and _re.search(r'备份|快照|状态|监控|告警|配置|是否|恢复', t):
        return "多主机清单状态查询"
    if _re.search(r'(备份(任务|作业)?(状态|情况|结果|覆盖))|((有没有|是否有|哪些).{0,8}备份)', t):
        if _re.search(r'VM|虚拟机|主机|服务器|查|列|看', t):
            return "备份状态查询"
    return ""


# ═══ LLM-Wiki 检索（Karpathy 知识编译思想）═══

def _build_wiki_context(query: str, top_k: int = 5,
                        actor: str = "", audience: str = "") -> str:
    """
    检索 LLM-Wiki 并返回格式化上下文。

    设计理念：
    - Ingest 时 LLM 编译的结构化知识，查询时直接注入
    - 与 KB RAG 独立，两者互补
    - 失败时优雅降级，返回空字符串
    - P0: actor/audience 参与检索前过滤 — 私有记忆/他群内容不进入任何回复
    """
    try:
        from core.memory.wiki_query import search_wiki
        hits = search_wiki(query, top_k=top_k, actor=actor, audience=audience)
        if not hits:
            return ""
        
        # 格式化为 prompt 上下文 (P2: 外部参考页显式标注, 不冒充内部验证经验)
        sections = []
        for hit in hits:
            title = getattr(hit, 'title', hit.page_id)
            snippet = getattr(hit, 'snippet', '')
            page_type = getattr(hit, 'page_type', '')

            ref_mark = "【外部参考·未验证】" if getattr(hit, "origin", "") == "reference" else ""
            section = f"### {ref_mark}{title} ({page_type})"
            if snippet:
                section += f"\n{snippet}"
            sections.append(section)
        
        wiki_context = "\n\n".join(sections)
        return (
            "## Z-OPS Wiki 相关知识（LLM 编译的结构化记忆）\n"
            "请优先参考以下 Wiki 知识回答用户问题：\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{wiki_context}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        )
    except Exception as e:
        logger.warning(f"[wiki] 检索失败（不阻断对话）: {e}")
        return ""

# ═══ 消息去重═══
_processed_msg_ids: dict = {}  # msg_id -> timestamp
_MSG_DEDUP_TTL = 60  # 去重窗口 60秒
_MSG_DEDUP_MAX = 5000  # 最多缓存条数

# ═══ 共享 httpx 连接池 ═══
_shared_http_client: Optional[httpx.AsyncClient] = None


async def get_shared_http_client() -> httpx.AsyncClient:
    """获取共享的 httpx AsyncClient (连接池复用)"""
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.AsyncClient(
            # 读超时 300s：OneAPI 网关后端为推理模型（glm5），长报告高峰期生成可超 180s
            timeout=httpx.Timeout(300, connect=15),
            verify=False,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _shared_http_client


# ═══ 消息协议 ═══

class _IncrementalRestorer:
    """流式输出增量还原: LLM 回复中的脱敏占位符 [TAG_###] 可能被 chunk 边界切割，
    末尾未闭合的 '[' 暂扣缓冲，待占位符完整后再还原，保证前端实时看到的已是真实值。
    """
    def __init__(self, ctx):
        self._ctx = ctx
        self._buf = ""

    @staticmethod
    def _split_pending(text: str) -> tuple:
        idx = text.rfind("[")
        if idx >= 0 and idx > text.rfind("]"):
            return text[:idx], text[idx:]  # 末尾存在未闭合 '[' → 暂扣
        return text, ""

    def feed(self, seg: str) -> str:
        """喂入增量文本，返回可直接展示的还原文本"""
        self._buf += seg
        emit, self._buf = self._split_pending(self._buf)
        if emit:
            try:
                from utils.sanitizer import get_sanitizer
                emit = get_sanitizer().restore(emit, self._ctx)
            except Exception:
                pass
        return emit

    def flush(self) -> str:
        """结束时冲刷缓冲 (残留的不完整占位符原样还原后返回)"""
        buf, self._buf = self._buf, ""
        if buf:
            try:
                from utils.sanitizer import get_sanitizer
                buf = get_sanitizer().restore(buf, self._ctx)
            except Exception:
                pass
        return buf


@dataclass
class Provenance:
    """消息来源追踪 — 完整的消息生命周期追溯"""
    trace_id: str = ""          # 跨 Agent 追踪 ID (整个请求链共享)
    origin_channel: str = ""    # 原始通道: webchat / feishu / telegram / wecom
    origin_user: str = ""       # 原始用户
    origin_timestamp: float = 0.0  # 原始请求时间
    route_path: list = field(default_factory=list)  # 路由路径: ["orchestrator", "ops_agent"]
    route_method: str = ""      # 路由方式: binding / command / llm

    def __post_init__(self):
        if not self.trace_id:
            self.trace_id = str(uuid.uuid4())[:16]
        if not self.origin_timestamp:
            self.origin_timestamp = time.time()

    def add_hop(self, agent_name: str):
        """记录路由路径"""
        if agent_name not in self.route_path:
            self.route_path.append(agent_name)


@dataclass
class AgentMessage:
    """Agent 间通信的标准消息格式 (v2.0: 含 provenance 追踪)"""
    msg_id: str = ""
    source: str = ""          # 发送方
    target: str = ""          # 接收方
    action: str = ""          # 动作名
    params: dict = field(default_factory=dict)
    text: str = ""            # 自然语言文本
    timestamp: float = 0.0
    reply_to: str = ""        # 回复的消息 ID
    channel_source: str = ""  # 来源渠道: webchat / feishu / telegram / wecom
    session_id: str = ""        # 会话 ID (同一对话的多轮消息共享)
    username: str = ""          # 发起用户
    provenance: dict = field(default_factory=dict)  # 来源追踪 (序列化后的 Provenance)

    def __post_init__(self):
        if not self.msg_id:
            self.msg_id = str(uuid.uuid4())[:12]
        if not self.timestamp:
            self.timestamp = time.time()
        # 自动填充 provenance
        if not self.provenance:
            self.provenance = asdict(Provenance(
                origin_channel=self.channel_source,
                origin_user=self.username,
                route_path=[self.source] if self.source else []
            ))

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "AgentMessage":
        try:
            d = json.loads(data)
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"消息解析失败: {e}")
            return cls(text=str(data))


@dataclass
class AgentReply:
    """Agent 回复消息"""
    msg_id: str = ""
    source: str = ""
    reply_to: str = ""
    result: dict = field(default_factory=dict)
    error: str = ""
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.msg_id:
            self.msg_id = str(uuid.uuid4())[:12]
        if not self.timestamp:
            self.timestamp = time.time()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "AgentReply":
        try:
            d = json.loads(data)
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError):
            return cls(error=str(data))


# ═══ BaseAgent ═══

class BaseAgent:
    """
    Agent 基类，子类只需要:
      1. 设置 name 属性
      2. 覆写 handle(msg) 方法
    v2.0: 支持从 AgentRegistry 读取声明式配置
    """

    name: str = "base_agent"
    description: str = ""

    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None
        self._running = False
        self._heartbeat_interval = 10  # 秒
        self._agent_config: Optional[AgentConfig] = None
        self._soul_prompt: str = ""
        self._workspace_dir: Optional[Path] = None
        # 问答日志去重: 已记录完整问答的 msg_id (防止 reply 兜底与手工记录重复)
        self._qa_logged_msg_ids: set = set()

        # 从 Registry 加载声明式配置
        self._load_from_registry()
        self._setup_logging()

    def _load_from_registry(self):
        """从 AgentRegistry 加载配置，覆盖默认值"""
        try:
            registry = get_registry()
            cfg = registry.get(self.name)
            if cfg:
                self._agent_config = cfg
                if cfg.description:
                    self.description = cfg.description
                # 设置 workspace 目录
                if cfg.workspace:
                    self._workspace_dir = Path(cfg.workspace)
                    self._workspace_dir.mkdir(parents=True, exist_ok=True)
                # 加载 SOUL.md
                if cfg.soul:
                    self._soul_prompt = load_soul(cfg.soul)
                logger.info(f"[{self.name}] ✅ 从 Registry 加载配置: {cfg.name}")
            else:
                logger.debug(f"[{self.name}] Registry 中未找到配置，使用默认值")
        except Exception as e:
            logger.debug(f"[{self.name}] Registry 加载失败 (向下兼容): {e}")

    @property
    def workspace_dir(self) -> Optional[Path]:
        """Agent 的 workspace 目录"""
        return self._workspace_dir

    @property
    def soul_prompt(self) -> str:
        """Agent 的 SOUL prompt (system prompt)"""
        return self._soul_prompt

    @property
    def agent_config(self) -> Optional[AgentConfig]:
        """Agent 的声明式配置"""
        return self._agent_config

    def _setup_logging(self):
        """彩色分级日志: 控制台带颜色 + Per-Agent 文件日志"""
        # 只移除本 Agent 创建的 handler, 不清除全局 handlers
        self._log_handler = None
        self._file_handler = None
        for h in logger.handlers[:]:
            if hasattr(h, '_zops_agent_name') and h._zops_agent_name == self.name:
                logger.removeHandler(h)

        class ColorFormatter(logging.Formatter):
            COLORS = {
                'DEBUG':    '\033[36m',   # cyan
                'INFO':     '\033[32m',   # green
                'WARNING':  '\033[33m',   # yellow
                'ERROR':    '\033[31m',   # red
                'CRITICAL': '\033[1;31m', # bold red
            }
            ICONS = {
                'DEBUG':    '\U0001f50d',
                'INFO':     '\u2714',
                'WARNING':  '\u26a0\ufe0f',
                'ERROR':    '\u274c',
                'CRITICAL': '\U0001f4a5',
            }
            RESET = '\033[0m'
            AGENT_COLORS = {
                'orchestrator': '\033[1;35m',  # bold magenta
                'general_agent': '\033[1;36m', # bold cyan
                'ops_agent': '\033[1;33m',     # bold yellow
                'network_agent': '\033[1;34m', # bold blue
                'vm_agent': '\033[1;32m',      # bold green
                'dev_agent': '\033[37m',       # white
                'browser_agent': '\033[36m',   # cyan
                'gcoc_agent': '\033[35m',      # magenta
                'kb_agent': '\033[34m',        # blue
            }

            def __init__(self, agent_name):
                super().__init__()
                self.agent_name = agent_name

            def format(self, record):
                ts = self.formatTime(record, '%H:%M:%S')
                lvl = record.levelname
                icon = self.ICONS.get(lvl, '')
                clr = self.COLORS.get(lvl, '')
                agent_clr = self.AGENT_COLORS.get(self.agent_name, '\033[37m')
                msg = record.getMessage()
                return (f"\033[90m{ts}\033[0m "
                        f"{clr}{icon} {lvl:<7}{self.RESET} "
                        f"{agent_clr}[{self.agent_name}]{self.RESET} "
                        f"{msg}")

        handler = logging.StreamHandler()
        handler.setFormatter(ColorFormatter(self.name))
        handler._zops_agent_name = self.name  # 标记归属 Agent
        logger.addHandler(handler)
        self._log_handler = handler
        logger.setLevel(logging.INFO)

        # Per-Agent 文件日志 — 写入各自 workspace/logs/
        if self._workspace_dir:
            try:
                log_dir = self._workspace_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                from datetime import datetime
                log_file = log_dir / f"{self.name}.log"
                fh = logging.FileHandler(log_file, encoding="utf-8")
                fh.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S"
                ))
                fh._zops_agent_name = self.name
                logger.addHandler(fh)
                self._file_handler = fh
            except Exception as e:
                logger.debug(f"[{self.name}] 文件日志初始化失败: {e}")

    async def get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                config.REDIS_URL,
                decode_responses=True,
                max_connections=10
            )
        return self._redis

    async def wiki_rag_context(self, query: str, top_k: int = 5) -> str:
        """
        LLM-Wiki 检索增强：检索 Wiki 并返回 RAG 上下文。
        
        使用方法：
            wiki_ctx = await self.wiki_rag_context(user_query)
            if wiki_ctx:
                prompt = f"{wiki_ctx}\n用户问题: {user_query}"
        
        设计理念：
        - 与 KB RAG 独立，两者可同时使用
        - 失败时优雅降级，不阻断对话
        - P0: 自动携带当前请求主体/受众 (RequestContext) — 私有记忆只归本人,
          群受众只见本群内容, 禁止发言者私有记忆注入群回复
        """
        from core.request_context import current_request_context as _cur_ctx
        _ctx = _cur_ctx()
        return _build_wiki_context(
            query, top_k=top_k,
            actor=(_ctx.actor if _ctx else "") or getattr(self, "_current_username", ""),
            audience=(_ctx.audience if _ctx else ""))

    # ── P2 收尾: 技能消费端 + 用户记忆 (Hermes 构件接线, published/owner 双重门) ──

    async def skill_context_for(self, msg: AgentMessage) -> tuple:
        """已发布技能 prompt 块 (仅 published; 见 skill_lifecycle.prompt_for_task)"""
        try:
            from core.harness.skill_lifecycle import get_skill_lifecycle
            return get_skill_lifecycle().prompt_for_task(msg.text or "")
        except Exception as e:
            logger.debug(f"[{self.name}] 技能上下文构建失败: {e}")
            return "", []

    async def record_skill_outcomes(self, names: list, success: bool) -> None:
        """技能使用结果回写 (verified=False — 口径不虚高, 成功率留给真实验收证据)"""
        if not names:
            return
        try:
            from core.harness.skill_lifecycle import get_skill_lifecycle
            get_skill_lifecycle().record_skill_outcomes(names, success=success)
        except Exception as e:
            logger.debug(f"[{self.name}] 技能结果回写失败: {e}")

    async def preference_context(self, actor: str, query: str, top_k: int = 3) -> str:
        """Hermes MemorySystem 召回: 用户偏好/反馈记忆注入 LLM system prompt

        ACL: 仅本人 MemorySystem; 记忆开关关闭时不注入。
        与 cross_session_memory (事实层) 互补 — 这里是偏好层 (格式/风格/禁忌)。
        优先用 LLM 语义召回 (recall_semantic), LLM 不可用降级到 bigram recall。
        """
        actor = (actor or "").strip()
        if not actor:
            return ""
        try:
            _raw = await (await self.get_redis()).hget(config.KEY_USERS, actor)
            if _raw and json.loads(_raw).get("memory_enabled") is False:
                return ""
            from core.memory_system import get_memory
            ms = get_memory(actor)
            # LLM 语义召回: 用当前 llm_chat 做语义匹配 (解决同义词/跨领域)
            try:
                async def _llm_fn(prompt):
                    return await self.llm_chat(prompt=prompt, username=actor)
                memories = ms.recall_semantic(query, top_k=top_k, llm_func=_llm_fn)
            except Exception:
                memories = ms.recall(query, top_k=top_k)
            if not memories:
                return ""
            return ms.build_system_context(memories)
        except Exception as e:
            logger.debug(f"[{self.name}] 偏好召回失败 (不阻断): {e}")
            return ""

    async def memory_nudge(self, session_id: str, query: str,
                            actor: str = "", limit: int = 2) -> str:
        """MemoryNudge (Hermes MemoryNudgeSystem): 主动推送相关历史经验

        调用 Hermes 原生 MemoryNudgeSystem.check_and_generate_nudges:
        - failure_recall: 相似失败推送历史解法
        - task_suggestion: 任务相关推送过往方案
        - daily_summary: 每日摘要 (24h 一次)
        ACL: 仅本人; 记忆开关关闭时不推送。
        """
        actor = (actor or "").strip()
        if not actor or not query:
            return ""
        try:
            _raw = await (await self.get_redis()).hget(config.KEY_USERS, actor)
            if _raw and json.loads(_raw).get("memory_enabled") is False:
                return ""
            from core.memory.memory_nudge import MemoryNudgeSystem
            nudge_sys = MemoryNudgeSystem(
                redis_client=await self.get_redis())
            # 传入用户上下文触发规则判定 + 真实统计 (每日/每周模板的
            # {count}{topics}{top_agent}{top_tasks} 需要从会话记忆计算,
            # 2026-10-22 前缺失 → 用户收到的是未渲染的占位符原文)
            context = {"current_query": query, "session_id": session_id}
            try:
                from core.memory.cross_session_memory import get_cross_session_memory
                from datetime import datetime as _dt, timedelta as _td
                csm = await get_cross_session_memory()
                sessions = await csm.get_recent_sessions(actor, limit=100)
                now = _dt.now()
                yd = [s for s in sessions
                      if (now - _td(days=1)) <= _dt.fromisoformat(s.created_at) <= now]
                week = [s for s in sessions
                        if (now - _td(days=7)) <= _dt.fromisoformat(s.created_at) <= now]
                context["count"] = len(yd)
                context["topics"] = sorted(
                    {t for s in yd for t in (s.topics or [])})[:5] or ["日常运维"]
                if week:
                    from collections import Counter as _C
                    agent_counter = _C(s.agent_id for s in week if s.agent_id)
                    top_agent = agent_counter.most_common(1)[0][0] if agent_counter else "助手"
                    context["top_agent"] = top_agent
                    top_sess = [s for s in week if s.agent_id == top_agent][:3]
                    context["top_tasks"] = [s.summary[:30] for s in top_sess
                                            if s.summary] or ["日常任务"]
            except Exception as _stat_err:
                logger.debug(f"[{self.name}] nudge 统计计算失败: {_stat_err}")
            nudges = await nudge_sys.check_and_generate_nudges(actor, context)
            if not nudges:
                return ""
            lines = ["\n## 记忆提醒 (Hermes Nudge)"]
            for n in nudges[:limit]:
                lines.append(f"- [{n.priority}] {n.content[:150]}")
            return "\n".join(lines) + "\n"
        except Exception as e:
            logger.debug(f"[{self.name}] MemoryNudge 失败 (不阻断): {e}")
            return ""

    async def user_memory_context(self, msg: AgentMessage, limit: int = 3,
                                  max_chars: int = 600) -> str:
        """同用户跨会话记忆 (Hermes cross_session_memory 接线; ACL: 仅本人)

        SPEC P2: 用户记忆检索前过滤 — owner 必须等于当前 actor, 返回前复核。
        画像/他人记忆一律不可见; 无 actor (服务/后台) 不注入。
        """
        actor = (msg.username or "").strip()
        if not actor:
            return ""
        try:
            # 用户记忆开关 (默认开; 关闭后不注入 — 设置页可切)
            import redis as _sync_redis
            _raw = await (await self.get_redis()).hget(config.KEY_USERS, actor)
            if _raw and json.loads(_raw).get("memory_enabled") is False:
                return ""
            from core.memory.cross_session_memory import get_cross_session_memory
            mem = await get_cross_session_memory()
            hits = await mem.search_past_sessions(msg.text or "", user_id=actor,
                                                  limit=limit)
            logger.info(f"[{self.name}] 用户记忆检索: {len(hits)} 条 (actor={actor})")
            # 返回前复核 (防御实现变化)
            hits = [h for h in hits if (h.user_id or "") == actor][:limit]
            if not hits:
                return ""
            lines = ["\n## 该用户的历史会话记忆 (仅本人可见; 这是系统为你检索的真实历史, 可直接引用作答)"]
            for h in hits:
                summ = str(h.summary or "").replace("\n", " ")[:max_chars // max(1, len(hits))]
                ts = str(h.created_at or "")[:10]
                lines.append(f"- [{ts}] {summ}")
            return "\n".join(lines) + "\n"
        except Exception as e:
            logger.debug(f"[{self.name}] 用户记忆检索失败 (不阻断): {e}")


    # ─── 生命周期 ───

    async def start(self):
        """启动 Agent: 注册心跳 + 订阅消息"""
        self._running = True
        r = await self.get_redis()
        logger.info(f"[{self.name}] \U0001f680 \u542f\u52a8\u4e2d...")

        # 注册心跳
        await r.hset(config.KEY_HEARTBEATS, self.name, json.dumps({
            "name": self.name,
            "status": "online",
            "started": time.time(),
            "last_heartbeat": time.time(),
            "description": self.description
        }))

        # 启动心跳协程
        asyncio.create_task(self._heartbeat_loop())

        # v4.0: 扫描孤儿 checkpoint (上一进程周期内中断的在途任务) → 通知并清理
        try:
            from core.checkpoint import report_and_clear_orphans
            orphans = report_and_clear_orphans(self.name, notify_fn=self.notify)
            if orphans:
                logger.warning(f"[{self.name}] 清理了 {len(orphans)} 个中断任务的 checkpoint")
        except Exception as e:
            logger.debug(f"[{self.name}] checkpoint 孤儿扫描失败: {e}")

        # 启动配置热加载监控
        asyncio.create_task(self._config_watch_loop())

        # 订阅自己的消息频道
        channel_name = config.channel(self.name)
        pubsub = r.pubsub()
        await pubsub.subscribe(channel_name)
        logger.info(f"[{self.name}] \U0001f4e1 \u5df2\u8ba2\u9605\u9891\u9053: {channel_name}")

        # 消息处理循环
        try:
            async for raw_msg in pubsub.listen():
                if not self._running:
                    break
                if raw_msg["type"] != "message":
                    continue
                try:
                    msg = AgentMessage.from_json(raw_msg["data"])
                    # 消息去重: 同一 msg_id 在窗口内不重复处理
                    if self._is_duplicate(msg.msg_id):
                        logger.debug(f"[{self.name}] 跳过重复消息: {msg.msg_id}")
                        continue
                    logger.info(f"[{self.name}] \U0001f4e8 \u6536\u5230\u6d88\u606f: action={msg.action}, from={msg.source}")
                    asyncio.create_task(self._safe_handle(msg))
                except Exception as e:
                    logger.error(f"[{self.name}] 消息处理异常: {e}")
        finally:
            await pubsub.unsubscribe(channel_name)
            await self._cleanup()

    async def stop(self):
        """停止 Agent"""
        self._running = False
        logger.info(f"[{self.name}] \u23f9\ufe0f \u505c\u6b62\u4e2d...")

    async def _cleanup(self):
        """清理资源"""
        r = await self.get_redis()
        await r.hset(config.KEY_HEARTBEATS, self.name, json.dumps({
            "name": self.name,
            "status": "offline",
            "stopped": time.time()
        }))
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    async def _heartbeat_loop(self):
        """定期更新心跳 + ToolPool 注册/续期"""
        _hb_count = 0
        _toolpool_ok = False
        while self._running:
            try:
                r = await self.get_redis()
                await r.hset(config.KEY_HEARTBEATS, self.name, json.dumps({
                    "name": self.name,
                    "status": "online",
                    "last_heartbeat": time.time(),
                    "description": self.description
                }))
                # ToolPool 注册/续期: 首次心跳补注册，之后每 60 次 (~10min) 续期自身。
                # 旧逻辑只对显式 register_agent 的 3 个 agent 生效，其余 agent
                # 在可观测性工具池页永远缺失或停留陈旧数据 → 数据脱节根因修复
                _hb_count += 1
                if not _toolpool_ok or _hb_count % 60 == 0:
                    try:
                        from core.tool_pool import get_tool_pool
                        pool = get_tool_pool()
                        caps = []
                        try:
                            hints = (self._agent_config or {}).routing_hints if self._agent_config else None
                            caps = list(hints or [])
                        except Exception:
                            pass
                        if not _toolpool_ok:
                            # 无论返回值 (False=已在册)，注册状态都已确认，后续转 touch 续期
                            await asyncio.to_thread(
                                pool.ensure_agent_registered, self.name,
                                self.description, caps)
                            _toolpool_ok = True
                        else:
                            await asyncio.to_thread(pool.touch_agent, self.name)
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"[{self.name}] 心跳更新失败: {e}")
            await asyncio.sleep(self._heartbeat_interval)

    async def _config_watch_loop(self):
        """定期检查 agents.yaml 变更，支持热加载"""
        while self._running:
            try:
                registry = get_registry()
                if registry.is_config_changed():
                    logger.info(f"[{self.name}] 检测到 agents.yaml 变更，重新加载配置")
                    registry.reload()
                    self._load_from_registry()
            except Exception as e:
                logger.debug(f"[{self.name}] 配置监控异常: {e}")
            await asyncio.sleep(30)  # 每30秒检查一次

    _EMOTIONAL_PATTERNS = _re.compile(
        r'|'.join([
            # 中文情绪化/粗鲁用语
            r'不要给我胡扯', r'别胡扯', r'胡扯', r'别废话', r'废话少说',
            r'不要给我废话', r'少废话', r'闭嘴', r'你[说讲]的[是什什么]乱七八糟',
            r'乱七八糟', r'什么鬼', r'搞[什啥]么[鬼名堂]',
            r'你[这那个]?(?:笨|蠢|傻)', r'垃圾', r'狗屁', r'放屁',
            r'滚[蛋开]', r'去[死你]的', r'脑[残障]',
            r'给我[认认]?真[点看查]', r'认[真点]点',
            r'不要[给]?我?(?:乱[说来搞]|瞎[说搞扯])',
            r'你(?:能不能|可以|会)(?:不)?(?:靠谱|正常)',
            # 命令式粗鲁前缀
            r'^给[我老子]\s*', r'^你[给跟]我\s*',
        ])
    )

    @staticmethod
    def sanitize_user_text(raw: str) -> str:
        """清洗用户消息中的情绪化/粗鲁用语，避免触发 LLM 安全过滤。

        策略:
        1. 移除已知的攻击性短语
        2. 清理残留的前导标点（逗号、句号等）
        3. 移除多余空白
        4. 如果清洗后为空，返回原文（保证有内容可处理）
        """
        if not raw:
            return raw
        cleaned = BaseAgent._EMOTIONAL_PATTERNS.sub('', raw)
        # 清理残留的前导标点
        cleaned = _re.sub(r'^[,，、。\s]+', '', cleaned).strip()
        # 清洗后为空则保留原文
        if not cleaned:
            return raw.strip()
        return cleaned

    # ─── LLM 网关安全误判防护 ───
    # 规则统一下沉到 utils/safety.py (base 与 core/query_engine 共用)
    @staticmethod
    def rewrite_for_safety(text: str) -> str:
        """改写可能触发 LLM 网关内容安全误判的措辞，转为中性运维措辞"""
        from utils.safety import rewrite_for_safety
        return rewrite_for_safety(text)

    @staticmethod
    def _is_gateway_refusal(text: str) -> bool:
        """判断 LLM 返回是否为网关内容安全的误判拒绝"""
        from utils.safety import is_gateway_refusal
        return is_gateway_refusal(text)

    @staticmethod
    def _is_duplicate(msg_id: str) -> bool:
        """检查消息是否重复 (滑动窗口去重)"""
        global _processed_msg_ids
        now = time.time()

        # 清理过期条目
        if len(_processed_msg_ids) > _MSG_DEDUP_MAX:
            expired = [k for k, v in _processed_msg_ids.items() if now - v > _MSG_DEDUP_TTL]
            for k in expired:
                del _processed_msg_ids[k]

        if msg_id in _processed_msg_ids:
            if now - _processed_msg_ids[msg_id] < _MSG_DEDUP_TTL:
                return True

        _processed_msg_ids[msg_id] = now
        return False

    async def _safe_handle(self, msg: AgentMessage):
        """安全调用 handle, 捕获异常, 记录审计日志 + ExecutionRegistry + Harness"""
        # Hermes self_improving: 执行前基于历史经验规划 (策略/风险/耗时预估)
        # 真正消费 experiences/*.jsonl 中的 363+ 条经验数据
        _exec_plan = None
        try:
            from core.self_improving import get_improving_engine
            _engine = get_improving_engine()
            _exec_plan = _engine.plan_execution(self.name, dict(msg.params or {}))
            if _exec_plan:
                logger.info(f"[{self.name}] 经验引擎规划: risk={_exec_plan.risk_level} "
                            f"conf={_exec_plan.confidence:.2f} strategy={_exec_plan.strategy}")
            if _exec_plan and _exec_plan.risk_level in ("medium", "high"):
                await self.send_progress(msg,
                    f"⚡ 经验引擎预警: 风险={_exec_plan.risk_level}"
                    + (f" · {_exec_plan.warnings[0]}" if _exec_plan.warnings else ""))
        except Exception as _plan_err:
            logger.debug(f"[{self.name}] 执行规划失败 (不阻断): {_plan_err}")

        # v4.1+ (P0-A): 把请求上下文挂到 self 上, 供所有内部工具方法 (llm_chat / llm_query / _call_openai_compatible)
        # 自动透传 username / session_id / msg_id. 这样 vm_agent / ops_agent / network_agent 等
        # 子类内部调用 llm_chat 时, _call_openai_compatible 里的 log_daily 也能拿到 username,
        # LLM 日志不再全部 anonymous, user_llm 归集能正确按用户统计 token 消耗.
        # ── P0-A 修复: 同步绑定 RequestContext (ContextVar) — 单例 Agent 并发处理多条消息时,
        #    实例属性在 await 点互相覆盖 (用户 A 的请求可能记到用户 B 头上);
        #    ContextVar 按任务隔离, set 不外溢; token 在 finally 中无条件 reset. ──
        from core.request_context import (
            RequestContext, context_from_agent_message,
            bind_request_context, reset_request_context,
        )
        prev_user = getattr(self, "_current_username", "")
        prev_sid = getattr(self, "_current_session_id", "")
        prev_msg = getattr(self, "_current_msg_id", "")
        self._current_username = msg.username or ""
        self._current_session_id = msg.session_id or ""
        self._current_msg_id = msg.msg_id or ""
        req_ctx: RequestContext = context_from_agent_message(msg)
        _ctx_token = bind_request_context(req_ctx)
        start_time = time.time()
        audit = get_audit_logger()
        audit.set_redis(await self.get_redis())
        # 获取当前 agent 模型信息
        llm_cfg = await self._get_llm_config()
        model_str = f"{llm_cfg['provider']}/{llm_cfg['model']}"

        try:
            # v4.0 (Phase 4.1/4.2): action 路径风险管线 — destructive 挂起审批门,
            # read/write 直接放行 (决策与拒绝回复见 core/approval.py)
            from core.approval import gate_action_message
            if not await gate_action_message(self, msg):
                duration_ms = (time.time() - start_time) * 1000
                # P2 (task-c17): 审批/权限拦截 = manual_required, 不计入业务成功率
                await audit.log(
                    agent_id=self.name, action=msg.action or "handle",
                    username=msg.username, channel=msg.channel_source,
                    session_id=msg.session_id, params=msg.params,
                    result_summary="manual_required", success=False,
                    duration_ms=duration_ms, msg_id=msg.msg_id,
                )
                await self._record_execution(
                    msg, success=False, duration_ms=duration_ms,
                    error="manual_required",
                )
                return  # finally 仍会执行, 清理 _current_username
            await self.handle(msg)
            duration_ms = (time.time() - start_time) * 1000
            await audit.log(
                agent_id=self.name, action=msg.action or "handle",
                username=msg.username, channel=msg.channel_source,
                session_id=msg.session_id, params=msg.params,
                result_summary="OK", success=True,
                duration_ms=duration_ms, msg_id=msg.msg_id,
                model=model_str
            )
            # ExecutionRegistry: 记录成功执行
            await self._record_execution(
                msg, success=True, duration_ms=duration_ms, model=model_str
            )
        except Exception as e:
            tb = traceback.format_exc()
            duration_ms = (time.time() - start_time) * 1000
            logger.error(f"[{self.name}] handle 异常: {e}\n{tb}")
            await audit.log(
                agent_id=self.name, action=msg.action or "handle",
                username=msg.username, channel=msg.channel_source,
                session_id=msg.session_id, params=msg.params,
                success=False, error=str(e),
                duration_ms=duration_ms, msg_id=msg.msg_id,
                model=model_str
            )
            # ExecutionRegistry: 记录失败 + Harness 自动学习
            await self._record_execution(
                msg, success=False, duration_ms=duration_ms,
                model=model_str, error=str(e)
            )
            await self._harness_record_failure(msg, str(e), tb)
            await self.reply(msg, error=f"Agent 执行异常: {str(e)}")
        finally:
            # 恢复请求上下文 (防止后续异步任务错位); P0-A: ContextVar 无条件先 reset
            reset_request_context(_ctx_token)
            self._current_username = prev_user
            self._current_session_id = prev_sid
            self._current_msg_id = prev_msg

    async def _record_execution(self, msg: AgentMessage, success: bool,
                                 duration_ms: float, model: str = "",
                                 error: str = ""):
        """记录到 ExecutionRegistry (参考 claw-code execution_registry)"""
        try:
            from core.execution_registry import get_execution_registry, ExecutionType
            registry = get_execution_registry()
            await registry.record_tool_call(
                agent_id=self.name,
                tool_name=msg.action or "handle",
                input_summary=(msg.text or "")[:200],
                output_summary="OK" if success else error[:200],
                success=success,
                latency_ms=duration_ms,
                session_id=msg.session_id,
                error=error,
                trace_id=msg.msg_id,  # v4.0 Phase 6: 全链 trace 贯穿
                exec_type=ExecutionType.AGENT_MESSAGE,  # 消息级口径: 与 query_engine 工具级入账区分
            )
        except Exception as e:
            logger.debug(f"[{self.name}] ExecutionRegistry 记录异常: {e}")

        # Harness 经验沉淀: 每次执行成败都写入 SelfImprovingEngine 经验库 (experiences/*.jsonl)，
        # 否则经验库无数据源 → 可观测性页"经验沉淀"恒为 0
        try:
            from core.self_improving import get_improving_engine
            engine = get_improving_engine()
            await asyncio.to_thread(
                engine.record_execution,
                skill_name=self.name,
                action=msg.action or "handle",
                input_params={"action": msg.action or "handle", "text": (msg.text or "")[:80]},
                output_result={},
                success=success,
                error_message=(error or "")[:200],
                execution_time=duration_ms / 1000,
                context={"session_id": msg.session_id, "username": msg.username or ""},
            )
        except Exception as e:
            logger.debug(f"[{self.name}] 经验沉淀失败: {e}")

    async def _harness_record_failure(self, msg: AgentMessage,
                                       error: str, traceback_str: str = ""):
        """Harness 自我进化: 记录失败以驱动自动技能生成 (参考 claw-code harness)"""
        try:
            from core.harness import HarnessController, FailureRecord
            r = await self.get_redis()
            # 进程级单例: 失败缓冲在内存累积，攒满 5 条才触发技能生成;
            # 若每次新建实例 buffer 永远只有 1 条 → 生成条件永不可达
            global _harness_controller
            if _harness_controller is None:
                _harness_controller = HarnessController(redis_client=r)
            await _harness_controller.record_failure(
                task=f"{msg.action or 'handle'}: {(msg.text or '')[:100]}",
                error=error,
                context={
                    "agent": self.name,
                    "action": msg.action,
                    "session_id": msg.session_id,
                    "traceback": traceback_str[:500],
                },
                attempted_solution="auto",
                agent_id=self.name,
            )
        except Exception as e:
            logger.debug(f"[{self.name}] Harness 记录失败: {e}")

    # ─── 消息收发 ───

    async def handle(self, msg: AgentMessage):
        """子类覆写此方法处理消息"""
        await self.reply(msg, error=f"{self.name} 未实现 handle 方法")

    # ═══ Chat Harness: 结构化问询 (ask 卡片) — 任何 agent 均可调用 ═══
    # 调用方式: await self.send_ask(msg, "问题", options=["选项A","选项B"], input_hint="密码", context="任务上下文")
    # 效果: 前端渲染信息卡片(选项点选/输入框); 用户提交的下一条消息由 chat harness
    # 路由回本 agent (zops:ask:pending 记录归属), 由本 agent 自行合并续跑。
    async def send_ask(self, msg: AgentMessage, question: str,
                       options: list = None, input_hint: str = "",
                       context: str = "", result: dict = None) -> None:
        try:
            r = await self.get_redis()
            pend = {"agent": self.name, "context": context or question,
                    "question": question, "ts": time.time()}
            if msg.session_id:
                await r.set(f"{config.REDIS_PREFIX}:ask:pending:{msg.session_id}",
                            json.dumps(pend, ensure_ascii=False), ex=600)
            ask_meta = {"question": question,
                        "options": [str(o) for o in (options or []) if str(o).strip()],
                        "input": input_hint or ""}
            result = dict(result or {})
            result["ask"] = ask_meta
            question += "\n\n💡 可直接点击下方卡片选择或输入 (10 分钟内有效)"
            result.setdefault("text", question)
            await self.reply(msg, result=result)
        except Exception as e:
            logger.warning(f"[{self.name}] send_ask 异常: {e}")
            await self.reply(msg, result={"text": question})

    async def reply(self, msg: AgentMessage, result: dict = None, error: str = ""):
        """回复消息到回复频道"""
        r = await self.get_redis()
        # ─── 参与链元数据: 把处理过该消息的 agent 链带给前端 ───
        # provenance.route_path 记录完整转发链 (webchat → orchestrator → a → b)，
        # 只保留真正的 agent (命名约定 *_agent)，过滤渠道名 (webchat/feishu 等)
        # 与 orchestrator 中转节点；多 Agent 协作场景前端可全部展示来源徽章
        result = dict(result or {})
        route = list((msg.provenance or {}).get("route_path") or [])
        agents = [a for a in route if a and a.endswith("_agent")]
        if self.name not in agents:
            agents.append(self.name)
        result.setdefault("agents", agents)
        reply_msg = AgentReply(
            source=self.name,
            reply_to=msg.msg_id,
            result=result,
            error=error
        )
        reply_ch = config.reply_channel(msg.msg_id)
        await r.publish(reply_ch, reply_msg.to_json())
        logger.info(f"[{self.name}] ✅ 回复消息 {msg.msg_id}: error={bool(error)}")
    
        # ─── 问答日志兜底收录: 所有用户问答自动记全文，防止分支路径漏记 ───
        # 若流程中已手工 log_daily 记录过该 msg 的问答 (带 msg_id)，则跳过避免重复
        if not error and msg.username and msg.text and msg.msg_id \
                and msg.msg_id not in self._qa_logged_msg_ids:
            answer_text = self._extract_reply_text(result or {})
            if answer_text:
                try:
                    self._qa_logged_msg_ids.add(msg.msg_id)
                    await self.log_daily(
                        f"问答: {msg.text[:50]} → 已回复",
                        level="success", category="chat",
                        session_id=msg.session_id, username=msg.username,
                        question=msg.text, answer=answer_text,
                        msg_id=msg.msg_id,
                    )
                except Exception as e:
                    logger.warning(f"[{self.name}] 问答兜底日志记录失败: {e}")
    
    @staticmethod
    def _extract_reply_text(result: dict, depth: int = 2) -> str:
        """从 reply result 中提取回复正文 (兼容 {'text':...} / {'result': {'text':...}})"""
        if not isinstance(result, dict) or depth < 0:
            return ""
        text = result.get("text")
        if isinstance(text, str) and text.strip():
            return text
        for k in ("result", "data"):
            sub = result.get(k)
            if isinstance(sub, dict):
                found = BaseAgent._extract_reply_text(sub, depth - 1)
                if found:
                    return found
        return ""

    async def forward_to_gcoc(self, msg: AgentMessage, raw_query: str) -> bool:
        """把数据查询类问题转发给 gcoc_agent 数据查询助手 (保持原 msg_id 回复到原频道)

        知识库/通用问答 Agent 无真实数据能力，检测到数据查询类问题 (VM清单/备份状态等)
        时调用此方法，让 gcoc_agent 用 GCOC API 查真实数据并回复。

        返回 True=已转发, False=gcoc_agent 离线或转发异常 (调用方降级为边界说明)
        """
        try:
            r = await self.get_redis()
            # 心跳在线检测: 避免 gcoc_agent 宕机时用户收不到任何回复
            hb_raw = await r.hget(config.KEY_HEARTBEATS, "gcoc_agent")
            gcoc_online = False
            if hb_raw:
                try:
                    hb = json.loads(hb_raw)
                    gcoc_online = hb.get("status") == "online" and \
                        (time.time() - float(hb.get("last_heartbeat", 0))) < 120
                except Exception:
                    pass
            if not gcoc_online:
                logger.warning(f"[{self.name}] gcoc_agent 离线，无法转发数据查询")
                return False

            forward = AgentMessage(
                source=self.name,
                target="gcoc_agent",
                action="chat",
                text=raw_query,
                params=dict(msg.params or {}),
                msg_id=msg.msg_id,          # 保持原 msg_id → 回复到同一 reply 频道
                reply_to=msg.reply_to,
                channel_source=msg.channel_source,
                session_id=msg.session_id,
                username=msg.username,
                provenance=dict(msg.provenance or {}),
            )
            # provenance 记录本次转发跳
            route = list(forward.provenance.get("route_path") or [])
            if self.name not in route:
                route.append(self.name)
            forward.provenance["route_path"] = route

            await r.publish(config.channel("gcoc_agent"), forward.to_json())
            logger.info(f"[{self.name}] 数据查询已转发 gcoc_agent: {raw_query[:60]}")
            return True
        except Exception as e:
            logger.warning(f"[{self.name}] 转发 gcoc_agent 异常: {e}")
            return False

    async def send_progress(self, msg: AgentMessage, text: str):
        """发送进度消息到回复频道 (不终止 SSE 流)"""
        r = await self.get_redis()
        progress = json.dumps({
            "_type": "progress",
            "source": self.name,
            "reply_to": msg.msg_id,
            "text": text
        }, ensure_ascii=False)
        reply_ch = config.reply_channel(msg.msg_id)
        await r.publish(reply_ch, progress)
        logger.info(f"[{self.name}] ⏳ 进度[{msg.username or '-'}]: {text[:50]}")
    
    async def send_delta(self, msg: AgentMessage, text: str, kind: str = "content"):
        """发送流式增量文本到回复频道 (LLM 边生成边推送，前端实时渲染，不终止 SSE 流)
        kind: content=正式回答 (正文区渲染), thinking=推理模型思考链 (前端折叠展示)
        """
        if not text:
            return
        r = await self.get_redis()
        delta = json.dumps({
            "_type": "delta",
            "kind": kind,
            "source": self.name,
            "reply_to": msg.msg_id,
            "text": text
        }, ensure_ascii=False)
        reply_ch = config.reply_channel(msg.msg_id)
        await r.publish(reply_ch, delta)
    
    async def send_to(self, target: str, action: str = "", text: str = "",
                      params: dict = None) -> str:
        """发送消息到指定 Agent

        P0-A 修复: 自动透传当前请求上下文 (actor/session/channel/父关联)。
        旧行为丢失 username → 下游 write 权限门看到空用户直接放行 (权限绕过链)。
        显式传入的 params 优先, 上下文仅补缺。
        """
        from core import request_context as _rc
        r = await self.get_redis()
        cur = _rc.current_request_context()
        msg = AgentMessage(
            source=self.name,
            target=target,
            action=action,
            text=text,
            params=params or {},
            username=(params or {}).get("username") or (cur.actor if cur else "")
            or getattr(self, "_current_username", ""),
            session_id=(cur.session_id if cur else "")
            or getattr(self, "_current_session_id", ""),
            channel_source=(cur.channel if cur else ""),
            reply_to=(cur.msg_id if cur else "")
            or getattr(self, "_current_msg_id", ""),
        )
        # 上下文委托: 子消息 provenance 记录父 request_id (跨 Agent 可追溯)
        if cur is not None:
            prov = dict(msg.provenance or {})
            prov["request_id"] = cur.request_id
            if cur.audience:
                prov["audience"] = cur.audience
            msg.provenance = prov
        ch = config.channel(target)
        await r.publish(ch, msg.to_json())
        logger.info(f"[{self.name}] \u27a1\ufe0f \u53d1\u9001\u6d88\u606f\u5230 {target}: action={action}"
                    + (f" (actor={msg.username or '-'})" if msg.username else ""))
        return msg.msg_id

    async def send_and_wait(self, target: str, action: str = "", text: str = "",
                            params: dict = None, timeout: float = 60) -> AgentReply:
        """发送消息并等待回复 (P0-A: 同 send_to 透传主体/会话/父关联)"""
        from core import request_context as _rc
        r = await self.get_redis()
        cur = _rc.current_request_context()
        msg = AgentMessage(
            source=self.name,
            target=target,
            action=action,
            text=text,
            params=params or {},
            username=(params or {}).get("username") or (cur.actor if cur else "")
            or getattr(self, "_current_username", ""),
            session_id=(cur.session_id if cur else "")
            or getattr(self, "_current_session_id", ""),
            channel_source=(cur.channel if cur else ""),
            reply_to=(cur.msg_id if cur else "")
            or getattr(self, "_current_msg_id", ""),
        )
        if cur is not None:
            prov = dict(msg.provenance or {})
            prov["request_id"] = cur.request_id
            if cur.audience:
                prov["audience"] = cur.audience
            msg.provenance = prov

        # 先订阅回复频道
        reply_ch = config.reply_channel(msg.msg_id)
        pubsub = r.pubsub()
        await pubsub.subscribe(reply_ch)

        # 发送消息
        ch = config.channel(target)
        await r.publish(ch, msg.to_json())

        # 等待回复
        try:
            deadline = time.time() + timeout
            async for raw_msg in pubsub.listen():
                if time.time() > deadline:
                    return AgentReply(reply_to=msg.msg_id, error="回复超时")
                if raw_msg["type"] != "message":
                    continue
                reply = AgentReply.from_json(raw_msg["data"])
                return reply
            # 循环正常结束 (无消息)
            return AgentReply(reply_to=msg.msg_id, error="未收到回复")
        finally:
            await pubsub.unsubscribe(reply_ch)

    async def notify(self, text: str, topic: str = "general", extra: dict = None):
        """发送通知到飞书/全局。
        extra: 可选结构化数据（如 NMS 卡片区块），飞书通道据此渲染富卡片。"""
        r = await self.get_redis()
        payload = {
            "text": text,
            "topic": topic,
            "source": self.name,
            "timestamp": time.time()
        }
        if extra:
            payload["extra"] = extra
        payload = json.dumps(payload, ensure_ascii=False)
        await r.publish(config.channel("notify:feishu"), payload)
        await r.publish(config.channel("notify:all"), payload)

    # ─── 对话历史上下文 (参考 claude-code context + claw-code QueryEnginePort) ───

    async def _build_history_context(self, msg: AgentMessage,
                                      max_turns: int = 6,
                                      max_content_len: int = 300) -> str:
        """统一构建对话历史上下文字符串
        
        优先使用 SessionEngine (支持 compact 摘要感知),
        回退到 msg.params["history"] (webchat_api 注入)。
        
        Args:
            msg: AgentMessage (含 session_id 和 params.history)
            max_turns: 最多使用最近多少轮历史
            max_content_len: 每条内容最大字符数
        
        Returns:
            格式化的对话历史字符串前缀, 如果无历史则返回空字符串
        """
        history = msg.params.get("history", [])
        
        # Fallback: 如果 webchat 未传 history, 从 SessionEngine 加载
        if not history and msg.session_id:
            try:
                from core.session_engine import get_session_engine
                engine = get_session_engine()
                context_str = await engine.build_context_for_agent(
                    msg.session_id, current_text=msg.text
                )
                if context_str:
                    return context_str
            except Exception as e:
                logger.debug(f"[{self.name}] SessionEngine 上下文加载失败: {e}")
        
        if not history:
            return ""
        
        lines = []
        for h in history[-max_turns:]:
            role = h.get("role", "user")
            content = h.get("content", "")[:max_content_len]
            source = h.get("source", "")
            if role == "user":
                lines.append(f"用户: {content}")
            elif role == "system" and source == "compact":
                lines.append(f"[对话摘要]: {content[:200]}")
            else:
                agent_hint = f" (via {source})" if source else ""
                lines.append(f"助手{agent_hint}: {content}")
        
        return "以下是之前的对话记录:\n" + "\n".join(lines) + "\n\n"

    # ─── 统一 LLM 查询引擎 (参考 claude-code QueryEngine + claw-code QueryEnginePort) ───

    async def llm_query(
        self,
        prompt: str,
        system: str = "",
        tools: list = None,
        tool_executor=None,
        max_turns: int = 8,
        max_budget_tokens: int = 30000,
        temperature: float = 0.7,
        username: str = "",
        tool_choice: str = "auto",
        lazy_tool_schemas: bool = False,
        session_id: str = "",
        context_id: str = "",
    ) -> dict:
        """
        统一 LLM 调用引擎入口 (支持 tool-call 循环)

        参考:
        - claude-code QueryEngine.ts: LLM → tool_calls → execute → LLM → ...
        - claw-code QueryEnginePort: submit_message + max_turns + max_budget_tokens

        与 llm_chat() 的区别:
        - llm_query() 支持 tool calling 循环, LLM 可以自主选择工具
        - llm_chat() 是简单的单次 prompt → response

        Args:
            prompt: 用户 prompt
            system: system prompt
            tools: ToolDef 列表 (启用 tool calling)
            tool_executor: async (name, args) → ToolExecution
            max_turns: tool-call 最大循环轮数
            max_budget_tokens: token 预算
            temperature: 温度
            username: 用户名 (用量追踪, 缺省时从 _safe_handle 设置的 _current_username 上下文获取)
            lazy_tool_schemas: v4.0 (Phase 2.3) 工具 schema 延迟加载 — 精简注入 + search_tool_detail 元工具

        Returns:
            dict: {"text": str, "tool_calls": list, "tool_results": list,
                   "usage": {"input_tokens", "output_tokens"}, "model": str,
                   "stop_reason": str, "error": str}
        """
        from core.query_engine import QueryEngine, QueryConfig

        llm_cfg = await self._get_llm_config()

        # 构建 QueryEngine 配置
        engine = QueryEngine(llm_config=llm_cfg)
        qc = QueryConfig(
            max_turns=max_turns,
            max_budget_tokens=max_budget_tokens,
            temperature=temperature,
            enable_tool_calls=bool(tools),
            tool_choice=tool_choice,
        )

        messages = [{"role": "user", "content": prompt}]

        # username 兜底: 子类调用 llm_query 可能没传。
        # v4.1 (P0-A): 优先取 RequestContext (ContextVar 按任务隔离),
        # 实例属性仅作旧路径兼容回退 (并发下可能错位, 不再是权威来源)
        from core.request_context import current_username as _rc_user, current_session_id as _rc_sid
        if not username:
            username = _rc_user() or getattr(self, "_current_username", "") or ""
        if not session_id:
            session_id = _rc_sid() or getattr(self, "_current_session_id", "") or session_id

        # ─── v4.0 (Phase 4.3): 可逆脱敏 — 工具循环路径闭环 (借鉴 OpenSRE reversible masking) ───
        # prompt/system 出网前掩码 (IP/主机名/GID → [TAG_n], 映射仅存内存);
        # LLM 以占位符调用工具 → executor 先 restore args 再真实执行;
        # 工具结果 sanitize 后才进 LLM; 最终回复 restore 后返回用户。
        sanitize_ctx = None
        sanitizer = None
        sanitize_on = bool(llm_cfg.get("sanitize_enabled", True))
        if sanitize_on and (
            llm_cfg.get("provider") == "ollama"
            or (llm_cfg.get("base_url") or "").startswith("http://127.0.0.1")
        ):
            sanitize_on = False  # 本地模型不需要脱敏
        if sanitize_on:
            try:
                from utils.sanitizer import DataSanitizer
                r_san = await self.get_redis()
                custom_raw = await r_san.get(f"{config.REDIS_PREFIX}:sanitize_mappings")
                sanitizer = DataSanitizer(custom_mappings=json.loads(custom_raw) if custom_raw else {})
                p_masked, sanitize_ctx = sanitizer.sanitize(prompt)
                if system:
                    s_masked, sys_ctx = sanitizer.sanitize(system)
                    for orig, info in sys_ctx.mapping.items():
                        if orig not in sanitize_ctx.mapping:
                            sanitize_ctx.mapping[orig] = info
                            sanitize_ctx.reverse_mapping[info["placeholder"]] = orig
                    system = s_masked
                prompt = p_masked
                if sanitize_ctx.count:
                    logger.info(f"[{self.name}] 🛡️ 脱敏(tool-loop): {sanitize_ctx.summary()}")
            except Exception as e:
                logger.warning(f"[{self.name}] llm_query 脱敏初始化异常, 跳过: {e}")
                sanitize_ctx = None
                sanitizer = None

        if tool_executor is not None and sanitizer is not None and sanitize_ctx is not None:
            _orig_exec, _san, _sctx = tool_executor, sanitizer, sanitize_ctx

            async def _sanitized_executor(name, args):
                # args 占位符还原为真实值 (如 LLM 用 [GID_001] 调工具 → 还原成 GID 47141)
                if isinstance(args, dict):
                    args = {
                        k: (_san.restore(v, _sctx) if isinstance(v, str) else v)
                        for k, v in args.items()
                    }
                exec_result = await _orig_exec(name, args)
                out = getattr(exec_result, "output", None)
                if isinstance(out, str) and out:
                    masked, out_ctx = _san.sanitize(out)
                    if out_ctx.count:
                        for orig, info in out_ctx.mapping.items():
                            if orig not in _sctx.mapping:
                                _sctx.mapping[orig] = info
                                _sctx.reverse_mapping[info["placeholder"]] = orig
                        exec_result.output = masked
                return exec_result

            tool_executor = _sanitized_executor

        result = await engine.query(
            messages=messages,
            system=system,
            tools=tools,
            query_config=qc,
            tool_executor=tool_executor,
            lazy_tool_schemas=lazy_tool_schemas,
            # 工具级入账归属: 否则 query_engine 里的真实工具调用记录 agent_id 落成 unknown
            agent_hint=self.name,
            session_hint=session_id,
            context_id=context_id,
        )

        # 记录用量
        r = await self.get_redis()
        import json as _json
        usage_entry = _json.dumps({
            "agent": self.name,
            "model": result.model or llm_cfg.get("model", ""),
            "provider": result.provider or llm_cfg.get("provider", ""),
            "username": username or "",
            "session_id": session_id or "",
            "prompt_len": len(prompt),
            "result_len": len(result.output),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "tool_calls": len(result.tool_calls),
            "stop_reason": result.stop_reason,
            "elapsed": round(result.latency_ms / 1000, 2),
            "timestamp": time.time()
        }, ensure_ascii=False)
        await r.rpush(config.KEY_LLM_USAGE, usage_entry)
        await r.ltrim(config.KEY_LLM_USAGE, -1000, -1)
        # 累计计数 (不受 ltrim 截断影响; 旧口径 llen 恒为 1000 导致页面假数据)
        await r.incr(config.KEY_LLM_USAGE_TOTAL)

        model_str = f"{result.provider or llm_cfg.get('provider', '')}/{result.model or llm_cfg.get('model', '')}"
        await self.log_daily(
            f"LLM Query: {model_str} ({round(result.latency_ms/1000, 1)}s, "
            f"tools={len(result.tool_calls)}, tokens={result.input_tokens}+{result.output_tokens})",
            level="info", category="llm",
            model=model_str, username=username,
            session_id=session_id or current_session_id() or getattr(self, "_current_session_id", "") or "",
        )

        # v4.0 (Phase 4.3): 回复反向还原 — 模型输出仍可含真实名, 前端无需二次处理
        output_text = result.output
        if sanitize_ctx is not None and sanitizer is not None and output_text:
            try:
                output_text = sanitizer.restore(output_text, sanitize_ctx)
            except Exception:
                pass

        return {
            "text": output_text,
            "tool_calls": result.tool_calls,
            "tool_results": result.tool_results,
            "usage": {
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "total_tokens": result.input_tokens + result.output_tokens,
            },
            "model": result.model,
            "provider": result.provider,
            "stop_reason": result.stop_reason,
            "error": result.error,
            "latency_ms": result.latency_ms,
        }

    # ─── 日志记录 ───

    # 完整问答文本单条最大保存长度 (收录全文; 仅设 1MB 兜底防止异常超大输出)
    QA_MAX_LEN = 1_000_000

    async def log_daily(self, content: str, level: str = "info", category: str = "",
                        session_id: str = "", model: str = "", username: str = "",
                        question: str = "", answer: str = "", msg_id: str = ""):
        """记录到每日工作日志 (增强版: 带级别、分类、会话、模型、用户)

        question / answer: 完整问答原文, 用于日志页展开查看完整问答记录
        msg_id: 传入后标记该消息已记录问答, reply() 兜底收录会跳过避免重复
        """
        r = await self.get_redis()
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        key = config.daily_log_key(today)
        entry_data = {
            "agent": self.name,
            "content": content,
            "level": level,       # info / warn / error / success
            "category": category, # chat / llm / route / system / kb / sanitize
            "timestamp": time.time()
        }
        if session_id:
            entry_data["session_id"] = session_id
        if model:
            entry_data["model"] = model
        if username:
            entry_data["username"] = username
        if question:
            entry_data["question"] = question[:self.QA_MAX_LEN]
        if answer:
            entry_data["answer"] = answer[:self.QA_MAX_LEN]
        # 标记该 msg 已记录完整问答 (reply 兜底收录去重用)
        if msg_id and (question or answer):
            self._qa_logged_msg_ids.add(msg_id)
            if len(self._qa_logged_msg_ids) > 2000:  # 防无限增长
                self._qa_logged_msg_ids.clear()
        entry = json.dumps(entry_data, ensure_ascii=False)
        await r.rpush(key, entry)
        if config.DAILY_LOG_TTL > 0:
            await r.expire(key, config.DAILY_LOG_TTL)

        # 用户级 LLM 用量归集 (用于审计页"实际消耗 token"统计)
        # 仅对 category=llm 的条目解析归集. 支持两种格式:
        #   a) "tokens=N+M"   — llm_query 路径 (含 tools 字段)
        #   b) "N→M"           — llm_chat / _call_openai_compatible 路径 (箭头, 在 "(lat, N→M)" 内)
        # 跳过 chat/route/sanitize 等其他类别
        if username and category == "llm":
            in_tok, out_tok = 0, 0
            if "tokens=" in content:
                import re as _re
                seg = content.split("tokens=", 1)[1]
                m = _re.match(r"\s*(\d+)\s*\+\s*(\d+)", seg)
                if m:
                    in_tok, out_tok = int(m.group(1)), int(m.group(2))
            elif "→" in content:
                # 兼容 "(9.0s, 2460→186)" 这种格式: 找最后一次 N→M 数字对 (在括号里)
                import re as _re
                ms = _re.findall(r"(\d+)\s*→\s*(\d+)", content)
                if ms:
                    # 取最后一个匹配 (括号内的就是最后一个)
                    in_tok, out_tok = int(ms[-1][0]), int(ms[-1][1])
            if in_tok > 0 or out_tok > 0:
                try:
                    user_llm_key = f"{config.REDIS_PREFIX}:user_llm:{today}"
                    await r.hincrby(user_llm_key, f"{username}:calls", 1)
                    await r.hincrby(user_llm_key, f"{username}:input_tokens", in_tok)
                    await r.hincrby(user_llm_key, f"{username}:output_tokens", out_tok)
                    await r.expire(user_llm_key, max(config.DAILY_LOG_TTL, 90 * 86400))
                except Exception as _e:
                    logger.debug(f"[log_daily] 用户 LLM 用量归集失败: {_e}")

    # ─── LLM 调用 ───

    async def _get_llm_config(self) -> dict:
        """模型配置解析 — P3 (task-c17) 委托统一模型调用层 core/model_gateway

        优先级语义不变: Per-Agent Redis 覆盖 > agents.yaml model > 全局 Redis > env;
        provider/model 同层解析; OneAPI 专用密钥优先; 凭据绑定 provider。
        (单一事实源, QueryEngine/Wiki/synthesizer 共用同一解析链)
        """
        from core.model_gateway import resolve_model_config
        r = await self.get_redis()
        yaml_model = ""
        try:
            if self._agent_config and self._agent_config.model:
                yaml_model = self._agent_config.model
        except Exception:
            yaml_model = ""
        return await resolve_model_config(r, agent_id=self.name,
                                          agent_yaml_model=yaml_model)

    async def llm_chat(self, prompt: str, system: str = "", model: str = "",
                       temperature: float = 0.7, username: str = "", max_tokens: int = 0,
                       on_wait=None, on_delta=None, on_reasoning=None,
                       session_id: str = "") -> str:
        """调用 LLM (支持 DeepSeek / OpenAI / Anthropic / 自定义 OpenAI 兼容)
        自动执行数据脱敏→LLM 调用→结果还原，确保敏感数据不发送到公共 API。
        on_wait: 异步回调 async fn(elapsed_seconds)，长调用等待期间每 30s 触发一次，
                 用于向用户发送"仍在生成中"的进度心跳 (防止 SSE 空闲超时/用户焦虑)
        on_delta: 异步回调 async fn(delta_text)，LLM 流式输出时逐段触发 (已做脱敏
                 增量还原)，用于向前端实时推送生成内容；流式失败自动回退非流式
        on_reasoning: 异步回调 async fn(seg)，推理模型思考链逐段触发 (DeepSeek thinking
                     mode，前端实时展示思考过程；思考结束自动切换为正式回答)
        """
        from utils.sanitizer import DataSanitizer, SanitizeContext

        r = await self.get_redis()
        llm_cfg = await self._get_llm_config()

        # username 兜底: 子类调用 llm_chat 可能没传, 取请求上下文
        # v4.1 (P0-A): RequestContext 优先 (任务级隔离), 实例属性仅兼容回退
        from core.request_context import current_username as _rc_user
        if not username:
            username = _rc_user() or getattr(self, "_current_username", "") or ""

        provider = llm_cfg["provider"]
        if not model:
            model = llm_cfg["model"]

        # 安全检查: 如果 model 为空, 给出明确警告并使用 provider 默认
        if not model:
            logger.warning(f"[{self.name}] ⚠️ 模型名称为空 (provider={provider}), 请在管理后台配置模型")
            # provider 对应的默认模型, 避免空请求
            _default_models = {
                "deepseek": "deepseek-chat", "openai": "gpt-4o-mini",
                "anthropic": "claude-sonnet-4-20250514", "ollama": "qwen2.5:latest",
            }
            model = _default_models.get(provider, "default")

        if not system:
            system = (
                "你是 Z-OPS 运维智能体，专注于基础设施运维领域。"
                "你擅长网络、系统、VMware/KVM/PVE 虚拟化、信创环境等运维场景。"
                "请用简洁专业的中文回答。"
            )

        # ─── 数据脱敏 ───
        sanitize_on = llm_cfg.get("sanitize_enabled", True)
        # 本地模型不需要脱敏
        if provider == "ollama" or (llm_cfg.get("base_url") or "").startswith("http://127.0.0.1"):
            sanitize_on = False

        sanitize_ctx = None
        if sanitize_on:
            try:
                # 加载自定义映射
                custom_raw = await r.get(f"{config.REDIS_PREFIX}:sanitize_mappings")
                custom_mappings = json.loads(custom_raw) if custom_raw else {}
                sanitizer = DataSanitizer(custom_mappings=custom_mappings)

                prompt_masked, sanitize_ctx = sanitizer.sanitize(prompt)
                system_masked, sys_ctx = sanitizer.sanitize(system)
                # 合并 system 的映射到主 ctx
                for orig, info in sys_ctx.mapping.items():
                    if orig not in sanitize_ctx.mapping:
                        sanitize_ctx.mapping[orig] = info
                        sanitize_ctx.reverse_mapping[info["placeholder"]] = orig

                if sanitize_ctx.count > 0:
                    logger.info(f"[{self.name}] \U0001f6e1\ufe0f \u8131\u654f: {sanitize_ctx.summary()}")
                    prompt = prompt_masked
                    system = system_masked
            except Exception as e:
                logger.warning(f"[{self.name}] 脱敏处理异常, 跳过: {e}")
                sanitize_ctx = None

        start_time = time.time()

        # ─── 长调用心跳: 等待期间周期性回调 (如发送进度到前端) ───
        ticker = None
        if on_wait:
            async def _ticker():
                while True:
                    await asyncio.sleep(30)
                    elapsed = int(time.time() - start_time)
                    try:
                        await on_wait(elapsed)
                    except Exception:
                        pass
            ticker = asyncio.create_task(_ticker())

        _RETRYABLE = (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError)
        result = ""
        try:
            if provider == "anthropic":
                result = await self._call_anthropic(
                    prompt, system, model, temperature, llm_cfg, max_tokens
                )
            elif on_delta:
                # 流式优先: 边生成边回调增量文本 (前端实时显示)，失败自动回退非流式
                restorer = _IncrementalRestorer(sanitize_ctx) if (sanitize_ctx and sanitize_ctx.count) else None
                # 思考链用独立还原器: reasoning 与 content 交替输出，互不干扰暂扣缓冲
                think_restorer = _IncrementalRestorer(sanitize_ctx) if (sanitize_ctx and sanitize_ctx.count) else None

                async def _stream_cb(seg: str):
                    if restorer:
                        seg = restorer.feed(seg)
                    if seg:
                        await on_delta(seg)

                async def _think_cb(seg: str):
                    # 思考链逐段回调 (DeepSeek thinking mode)，同样经过脱敏增量还原
                    if think_restorer:
                        seg = think_restorer.feed(seg)
                    if seg and on_reasoning:
                        await on_reasoning(seg)

                try:
                    result = await self._call_openai_compatible_stream(
                        prompt, system, model, temperature, llm_cfg, max_tokens,
                        on_delta=_stream_cb, on_reasoning=_think_cb)
                    if restorer:
                        tail = restorer.flush()
                        if tail:
                            await on_delta(tail)
                    if think_restorer:
                        ttail = think_restorer.flush()
                        if ttail and on_reasoning:
                            await on_reasoning(ttail)
                except Exception as e:
                    logger.warning(f"[{self.name}] 流式调用失败 ({type(e).__name__})，回退非流式")
                    result = await self._call_openai_compatible(
                        prompt, system, model, temperature, llm_cfg, max_tokens
                    )
            else:
                result = await self._call_openai_compatible(
                    prompt, system, model, temperature, llm_cfg, max_tokens
                )
        except _RETRYABLE as e:
            # 网络超时类故障自动重试一次 (短超时 150s，避免再次长时间占用；
            # OneAPI 网关多节点负载均衡时换一个健康节点的概率很高)
            logger.warning(f"[{self.name}] ⚠️ LLM 网络超时 ({type(e).__name__})，自动重试一次...")
            if on_wait:
                try:
                    await on_wait(int(time.time() - start_time))
                except Exception:
                    pass
            # 备用模型降级: 主模型超时后换备用模型重试 (如 deepseek-v3 超时 → openclaw)
            _retry_model = (getattr(config, "LLM_FALLBACK_MODEL", "") or "").strip() or model
            _note = f" (降级到备用模型 {_retry_model})" if _retry_model != model else ""
            try:
                if provider == "anthropic":
                    result = await self._call_anthropic(
                        prompt, system, _retry_model, temperature, llm_cfg, max_tokens, timeout=150)
                else:
                    result = await self._call_openai_compatible(
                        prompt, system, _retry_model, temperature, llm_cfg, max_tokens, timeout=150)
                logger.info(f"[{self.name}] ✅ 超时重试成功{_note}")
            except Exception as e2:
                logger.error(f"[{self.name}] ❌ 超时重试失败: {type(e2).__name__}: {e2}")
                result = f"LLM 调用失败(重试后仍超时): {type(e2).__name__}: {str(e2)}"
        except Exception as e:
            # httpx.ReadTimeout 等异常 str() 为空，附带类型名便于定位
            logger.error(f"❌ LLM 调用失败: {type(e).__name__}: {e}")
            result = f"LLM 调用失败: {type(e).__name__}: {str(e)}"
        finally:
            if ticker:
                ticker.cancel()

        # ─── 网关安全误判检测: 改写后自动重试 (如纠错类正常对话被判为提示词注入) ───
        # 重试候选: 1) 改写后的完整 prompt  2) 去除历史/KB上下文后仅保留当前问题
        # (触发词可能藏在历史上下文里，仅改写完整 prompt 不一定能消除)
        if self._is_gateway_refusal(result):
            logger.warning(f"[{self.name}] ⚠️ 检测到网关内容安全误判拒绝，启动改写重试: {result[:80]}")
            candidates = []
            from utils.safety import strip_refusal_text
            rewritten_prompt = strip_refusal_text(self.rewrite_for_safety(prompt))
            if rewritten_prompt != prompt:
                candidates.append(rewritten_prompt)
            if "用户问题:" in prompt:
                last_q = self.rewrite_for_safety(prompt.split("用户问题:")[-1].strip())
                if last_q and last_q not in candidates:
                    candidates.append(f"用户问题: {last_q}")
            for cand in candidates:
                retry_prompt = cand
                retry_ctx = sanitize_ctx
                if sanitize_on:
                    try:
                        from utils.sanitizer import DataSanitizer
                        custom_raw = await r.get(f"{config.REDIS_PREFIX}:sanitize_mappings")
                        custom_mappings = json.loads(custom_raw) if custom_raw else {}
                        retry_prompt, retry_ctx = DataSanitizer(custom_mappings=custom_mappings).sanitize(cand)
                    except Exception:
                        retry_prompt = cand
                        retry_ctx = sanitize_ctx
                try:
                    if provider == "anthropic":
                        retry_result = await self._call_anthropic(
                            retry_prompt, system, model, temperature, llm_cfg, max_tokens)
                    else:
                        retry_result = await self._call_openai_compatible(
                            retry_prompt, system, model, temperature, llm_cfg, max_tokens)
                    if retry_result and not self._is_gateway_refusal(retry_result):
                        result = retry_result
                        sanitize_ctx = retry_ctx
                        logger.info(f"[{self.name}] ✅ 改写重试成功，已绕过网关安全误判 (候选长度={len(cand)})")
                        break
                    else:
                        logger.warning(f"[{self.name}] 改写重试仍被网关拦截，尝试下一候选")
                except Exception as e:
                    logger.warning(f"[{self.name}] 改写重试失败: {e}")

        # 保底: 防止底层返回 None 导致后续 len() 报错
        if result is None:
            result = ""

        # ─── 反向还原 ───
        if sanitize_ctx and sanitize_ctx.count > 0:
            try:
                from utils.sanitizer import get_sanitizer
                result = get_sanitizer().restore(result, sanitize_ctx)
                logger.info(f"[{self.name}] \U0001f504 \u8fd8\u539f: {sanitize_ctx.count} \u9879\u5360\u4f4d\u7b26")
            except Exception as e:
                logger.warning(f"[{self.name}] 还原处理异常: {e}")

        # 记录用量
        elapsed = time.time() - start_time
        # ─── 慢调用观测: 超过 120s 记 warning，便于定位网关性能劣化 ───
        if elapsed > 120:
            logger.warning(
                f"[{self.name}] 🐢 慢 LLM 调用: {elapsed:.0f}s "
                f"(model={model}, provider={provider}, user={username or '-'})")
        usage_entry = json.dumps({
            "agent": self.name,
            "model": model,
            "provider": provider,
            "username": username or "",
            "session_id": session_id or "",
            "prompt_len": len(prompt),
            "result_len": len(result),
            # llm_chat 路径无网关 token 回执 → 按平台口径估算 (1 token ≈ 1.5 中文字符)
            "input_tokens": round(len(prompt) / 1.5),
            "output_tokens": round(len(result) / 1.5),
            "tokens_estimated": True,
            "elapsed": round(elapsed, 2),
            "sanitized": sanitize_ctx.count if sanitize_ctx else 0,
            "timestamp": time.time()
        }, ensure_ascii=False)
        await r.rpush(config.KEY_LLM_USAGE, usage_entry)
        await r.ltrim(config.KEY_LLM_USAGE, -1000, -1)
        # 累计计数 (与 llm_query 入账点同口径)
        await r.incr(config.KEY_LLM_USAGE_TOTAL)

        # 记录到每日日志 (含模型信息)
        await self.log_daily(
            f"LLM 调用: {provider}/{model} ({round(elapsed, 1)}s, {len(prompt)}→{len(result)})",
            level="info", category="llm",
            model=f"{provider}/{model}", username=username,
            session_id=current_session_id() or getattr(self, "_current_session_id", "") or "",
        )

        return result

    async def _call_openai_compatible_stream(self, prompt: str, system: str,
                                             model: str, temperature: float,
                                             llm_cfg: dict = None,
                                             max_tokens: int = 0,
                                             on_delta=None, on_reasoning=None) -> str:
        """OpenAI 兼容 API 流式调用 (SSE)，逐段回调 on_delta(delta_text)，返回完整文本。
        推理模型 (glm5-pg1) 的 reasoning_content 逐段回调 on_reasoning(seg) 供前端
        实时展示思考过程 (DeepSeek thinking mode)；
        若最终 content 为空 (思考吃光 max_tokens)，则用 reasoning_content 兑底。
        """
        cfg = llm_cfg or await self._get_llm_config()
        provider = cfg["provider"]

        if provider == "oneapi":
            base_url = (cfg.get("oneapi_base_url") or config.ONEAPI_BASE_URL).rstrip("/")
            api_key = cfg.get("oneapi_key") or config.ONEAPI_API_KEY or cfg.get("api_key") or "sk-oneapi"
        elif cfg.get("base_url"):
            base_url = cfg["base_url"].rstrip("/")
            api_key = cfg.get("api_key") or "sk-custom"
        else:
            try:
                from webchat_api import PROVIDER_URL_MAP
                base_url = PROVIDER_URL_MAP.get(provider, "https://api.openai.com/v1").rstrip("/")
            except ImportError:
                base_url = "https://api.openai.com/v1"
            api_key = cfg.get("api_key") or "sk-default"

        client = await get_shared_http_client()
        # P3: 流式请求过网关前置门 (熔断/限流); 结束后配对上报
        from core.model_gateway import get_model_gateway as _gm
        _gw = _gm()
        _gw.preflight(base_url)
        parts: list[str] = []
        reasoning_parts: list[str] = []  # 推理模型 (glm5-pg1) 思考链，仅在 content 为空时兜底
        # 分阶段耗时观测 (思考 vs 生成)，定位"慢在哪一环"
        _t0 = time.time()
        _t_first_reasoning = None
        _t_first_content = None
        _finish_reason = ""
        async with client.stream(
            "POST", f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                "temperature": temperature,
                "max_tokens": max_tokens or 4096,
                "stream": True
            },
        ) as resp:
            if resp.status_code != 200:
                _gw.report(base_url, False)
                body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                raise RuntimeError(f"LLM 流式请求失败 HTTP {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except Exception:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                if choices[0].get("finish_reason"):
                    _finish_reason = choices[0].get("finish_reason")
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    if _t_first_content is None:
                        _t_first_content = time.time() - _t0
                    parts.append(content)
                    if on_delta:
                        await on_delta(content)
                rc = delta.get("reasoning_content")
                if rc:
                    if _t_first_reasoning is None:
                        _t_first_reasoning = time.time() - _t0
                    reasoning_parts.append(rc)
                    if on_reasoning:
                        # 思考链逐片回调 → 前端实时展示思考过程 (DeepSeek thinking mode)
                        try:
                            await on_reasoning(rc)
                        except Exception:
                            pass
        if _t_first_content is not None or _t_first_reasoning is not None:
            think = f"{_t_first_reasoning:.1f}s" if _t_first_reasoning is not None else "-"
            gen = f"{_t_first_content:.1f}s" if _t_first_content is not None else "-"
            logger.info(f"[{self.name}] 流式阶段: 首思考 {think}, 首回答 {gen}, finish={_finish_reason or '-'}")
        if _finish_reason == "length":
            logger.warning(f"[{self.name}] ⚠️ LLM 回答被 max_tokens 截断 (finish_reason=length)")
        if not parts and reasoning_parts:
            # 与 _call_openai_compatible 的兜底一致: 推理模型 content 为空时用思考链
            result = "".join(reasoning_parts)
            if on_delta:
                await on_delta(result)
            return result
        if not parts:
            _gw.report(base_url, False)
            raise RuntimeError("流式响应无内容 (网关可能未启用 stream)")
        _gw.report(base_url, True)
        return "".join(parts)

    async def _call_openai_compatible(self, prompt: str, system: str,
                                       model: str, temperature: float,
                                       llm_cfg: dict = None,
                                       max_tokens: int = 0,
                                       timeout=None) -> str:
        """OpenAI 兼容 API (DeepSeek / OpenAI / Ollama / OneAPI / 自定义)
        使用共享 httpx 连接池复用连接
        timeout: 本次请求的读超时 (秒)，不传则用连接池默认 300s
        """
        cfg = llm_cfg or await self._get_llm_config()
        provider = cfg["provider"]

        # 根据 provider 决定 base_url 和 api_key (通用查找表，不再硬编码)
        if provider == "oneapi":
            base_url = (cfg.get("oneapi_base_url") or config.ONEAPI_BASE_URL).rstrip("/")
            api_key = cfg.get("oneapi_key") or config.ONEAPI_API_KEY
            if not api_key:
                logger.warning(f"[{self.name}] ⚠️ OneAPI 配置缺少 API Key, 请在管理后台设置 OneAPI Token")
                api_key = cfg.get("api_key") or "sk-oneapi"
            logger.info(f"[{self.name}] LLM 调用: provider=oneapi, base_url={base_url}, model={model}")
        elif cfg.get("base_url"):
            # 用户显式配置了 base_url (自定义 provider)
            base_url = cfg["base_url"].rstrip("/")
            api_key = cfg.get("api_key") or "sk-custom"
        else:
            # 通用查找: 从 PROVIDER_URL_MAP 获取默认 base_url
            try:
                from webchat_api import PROVIDER_URL_MAP
                base_url = PROVIDER_URL_MAP.get(provider, "https://api.openai.com/v1").rstrip("/")
            except ImportError:
                # fallback: 内置常见 provider URL
                _fallback_urls = {
                    "deepseek": "https://api.deepseek.com/v1",
                    "openai": "https://api.openai.com/v1",
                    "ollama": "http://127.0.0.1:11434/v1",
                }
                base_url = _fallback_urls.get(provider, "https://api.openai.com/v1")
            api_key = cfg.get("api_key") or "sk-default"

        client = await get_shared_http_client()
        req_kwargs = {}
        if timeout:
            req_kwargs["timeout"] = httpx.Timeout(timeout, connect=15)
        # P3: 统一模型调用层 — 限流/退避/熔断集中处理 (业务语义不变)
        from core.model_gateway import get_model_gateway, _BreakerOpen
        try:
            resp = await get_model_gateway().request_chat(
                {"provider": provider, "api_key": api_key, "base_url": base_url},
                {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens or 4096
                },
                client, req_kwargs=req_kwargs)
        except _BreakerOpen as bo:
            raise RuntimeError(f"模型网关暂不可用: {bo}")
        resp.raise_for_status()
        data = resp.json()

        # OneAPI 网关有时返回包装格式 {"body": "...", "error": "...", "status": 403}
        if "choices" not in data:
            # 尝试提取真实错误信息
            body = data.get("body", "")
            gw_err = data.get("error", "")
            if body:
                try:
                    import json as _j
                    inner = _j.loads(body) if isinstance(body, str) else body
                    err_msg = inner.get("error", {}).get("message", str(inner))
                except Exception:
                    err_msg = str(body)[:200]
            else:
                err_msg = gw_err or str(data)[:200]
            raise RuntimeError(f"LLM API 返回异常 ({data.get('status', 'unknown')}): {err_msg}")

        # 部分模型/网关可能返回 content=null（如 reasoner 只给 reasoning_content）
        msg_obj = data["choices"][0].get("message", {})
        content = msg_obj.get("content")
        if content is None:
            content = msg_obj.get("reasoning_content") or ""
        return content

    async def _call_anthropic(self, prompt: str, system: str,
                               model: str, temperature: float,
                               llm_cfg: dict = None,
                               max_tokens: int = 0,
                               timeout=None) -> str:
        """Anthropic Claude API (使用共享连接池)"""
        cfg = llm_cfg or await self._get_llm_config()
        api_key = cfg.get("anthropic_key") or cfg.get("api_key")

        client = await get_shared_http_client()
        req_kwargs = {}
        if timeout:
            req_kwargs["timeout"] = httpx.Timeout(timeout, connect=15)
        # P3: Anthropic 路径同样过网关 (熔断/限流 endpoint 级统一)
        from core.model_gateway import get_model_gateway, _BreakerOpen
        _anth = "https://api.anthropic.com"
        gw = get_model_gateway()
        try:
            gw.preflight(_anth)
        except _BreakerOpen as bo:
            raise RuntimeError(f"模型网关暂不可用: {bo}")
        resp = await client.post(
            f"{_anth}/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": model,
                "max_tokens": max_tokens or 2048,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature
            },
            **req_kwargs
        )
        gw.report(_anth, resp.status_code < 500)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]


# ═══ 启动入口 ═══

def run_agent(agent: BaseAgent):
    """启动 Agent 的便捷函数"""
    import signal

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(sig, frame):
        logger.info(f"收到信号 {sig}, 正在停止 {agent.name}...")
        loop.create_task(agent.stop())

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(agent.start())
    except KeyboardInterrupt:
        loop.run_until_complete(agent.stop())
    finally:
        loop.close()
