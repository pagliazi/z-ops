"""
Z-OPS 运维智能体 — Orchestrator 编排器
核心职责:
  - 接收所有用户请求
  - 通过 LLM 原生 function_calling 路由到对应 Agent
  - 管理任务队列
  - 聚合结果，推送通知

路由策略 (参考 claude-code/hermes-agent 模式):
  - Layer 0: 显式 target → 直接转发
  - Layer 1: action 精确匹配 → COMMAND_ROUTES
  - Layer 2: LLM function_calling — 将每个 Agent 暴露为 tool，
             模型原生 tool_call 决定路由，无需关键词写死或 JSON hack
"""

import asyncio
import json
import re
import time
import logging

from agents.base import BaseAgent, AgentMessage, AgentReply, run_agent

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from core.router import get_router

logger = logging.getLogger("zops")


# ═══ 命令路由表 (向下兼容 + Router 补充) ═══
COMMAND_ROUTES = {
    # ops_agent
    "host_check":    ("ops_agent", "host_check"),
    "log_analyze":   ("ops_agent", "log_analyze"),
    "batch_exec":    ("ops_agent", "batch_exec"),
    "health_report": ("ops_agent", "health_report"),

    # network_agent
    "ping_sweep":    ("network_agent", "ping_sweep"),
    "port_scan":     ("network_agent", "port_scan"),
    "config_backup": ("network_agent", "config_backup"),
    "route_trace":   ("network_agent", "route_trace"),

    # vm_agent
    "vm_list":       ("vm_agent", "vm_list"),
    "vm_power":      ("vm_agent", "vm_power"),
    "vm_snapshot":   ("vm_agent", "vm_snapshot"),
    "vm_migrate":    ("vm_agent", "vm_migrate"),
    "host_status":   ("vm_agent", "host_status"),

    # dev_agent
    "script_gen":    ("dev_agent", "script_gen"),
    "script_exec":   ("dev_agent", "script_exec"),

    # browser_agent
    "web_screenshot": ("browser_agent", "web_screenshot"),
    "web_action":     ("browser_agent", "web_action"),
    # nms_agent
    "nms_alarm_query": ("nms_agent", "nms_alarm_query"),
    "nms_alarm_now":   ("nms_agent", "nms_alarm_now"),

    # kb_agent
    "kb_search":       ("kb_agent", "search"),
    "kb_chat":         ("kb_agent", "chat"),

    # dev_agent — 补充
    "ansible_run":     ("dev_agent", "ansible_run"),

    # opscore_agent — 信息管理系统 (145)
    "cmdb_query":      ("opscore_agent", "cmdb_query"),
    "rack_query":      ("opscore_agent", "rack_query"),
    "monitor_query":   ("opscore_agent", "monitor_query"),
    "alert_query":     ("opscore_agent", "alert_query"),
    "case_query":      ("opscore_agent", "case_query"),
    "dns_query":       ("opscore_agent", "dns_query"),
    "datacenter_info": ("opscore_agent", "datacenter_info"),
    "login_info":      ("opscore_agent", "login_info"),
    # opscore_agent — 管理平台 (146) BRR 备份
    "backup_status":   ("opscore_agent", "backup_status"),
    "backup_objects":  ("opscore_agent", "backup_objects"),
    "backup_repos":    ("opscore_agent", "backup_repos"),
    "backup_sessions": ("opscore_agent", "backup_sessions"),
    "brr_jobs":        ("opscore_agent", "backup_status"),  # 新增：备份作业查询
    "brr_stats":       ("opscore_agent", "backup_status"),  # 新增：备份统计
    # opscore_agent — 管理平台 (146) 快照
    "snapshot_check":  ("opscore_agent", "snapshot_check"),
    "snapshot_list":   ("opscore_agent", "snapshot_check"),  # 新增：快照列表
    "snapshot_summary": ("opscore_agent", "snapshot_check"),  # 新增：快照汇总
    # opscore_agent — 管理平台 (146) Smart-Ops
    "smartops_vms":    ("opscore_agent", "smartops_vms"),
    "smartops_events": ("opscore_agent", "smartops_events"),
    "smartops_status": ("opscore_agent", "smartops_status"),
    "smartops_alerts": ("opscore_agent", "smartops_events"),  # 新增：运维告警
    # opscore_agent — 管理平台 (146) Aria
    "aria_ops":        ("opscore_agent", "aria_ops"),
    "aria_dashboard":  ("opscore_agent", "aria_ops"),  # 新增：Aria 仪表盘
    "aria_capacity":   ("opscore_agent", "aria_ops"),  # 新增：Aria 容量
    # opscore_agent — 管理平台 (146) VCD
    "vcd_resources":   ("opscore_agent", "vcd_resources"),
    "vcd_orgs":       ("opscore_agent", "vcd_resources"),  # 新增：VCD 组织
    # opscore_agent — 管理平台 (146) M365
    "m365_status":     ("opscore_agent", "m365_status"),
    "m365_dashboard":  ("opscore_agent", "m365_status"),  # 新增：M365 仪表盘
    # opscore_agent — 管理平台 (146) CRM
    "crm_query":       ("opscore_agent", "backup_status"),  # 新增：CRM 客户查询
    "crm_customers":   ("opscore_agent", "backup_status"),  # 新增：CRM 客户列表
    # opscore_agent — HA 高可用
    "ha_query":        ("opscore_agent", "ha_query"),
    "ha_vms":          ("opscore_agent", "ha_query"),  # 新增：HA VM 查询
    # opscore_agent — 综合
    "opscore_overview":   ("opscore_agent", "overview"),
    "opscore_chat":       ("opscore_agent", "chat"),  # 新增：OPS-CORE 通用聊天
}


# ═══ 路由 System Prompt (参考 claude-code coordinator + hermes-agent function_calling) ═══
# 不再做 JSON 文本分类 — 而是让 LLM 通过原生 tool_call 选择 Agent
ROUTE_SYSTEM_PROMPT = """你是 Z-OPS 运维智能体的路由协调器。
你的职责是理解用户意图，然后调用最合适的 Agent 工具来处理请求。

规则:
- 仔细理解用户问题的真实意图，选择最匹配的 Agent 工具
- 如果用户在追问上一轮的问题，请路由到上次处理的同一个 Agent
- 极短追问/省略指代 (如"有几台？""继续""再查一次") 必须依据上文对象选择 Agent，而非默认 general_agent
- 如果不确定，优先选择 general_agent
- 每次只调用一个 Agent 工具"""


# ═══ Agent 作为 Tool 的定义 (v4.0 Phase 2.2: 从 agents.yaml 单一事实源动态生成) ═══
# 每个 Agent 被建模为一个 function，LLM 通过 tool_call 决定路由。
# 描述文本来自 AgentConfig.capability_summary (agents.yaml) — 修改路由知识只需改 yaml + 跑评测:
#   python tests/routing/run_eval.py --mode keyword && ... --mode llm
# 原 338 行硬编码 (含 "47141"/"22台VM" 等 case 补丁) 已收敛到 agents.yaml;
# 具体误判 case 由 tests/routing/eval_cases.yaml 评测集守护。

# registry 不可用时的内置兜底描述 (与 agents.yaml capability_summary 保持同构, 仅能力边界三句话)
_FALLBACK_SUMMARIES = {
    "opscore_agent": (
        "OPS-CORE 平台数据查询 Agent。能力: CMDB 主机/服务器、备份作业(BRR/Veeam)状态、快照、"
        "VCD 云平台资源、Smart-Ops 虚拟机、运维事件/迁移记录、DNS 记录、Aria Ops 容量、"
        "M365 备份、HA 高可用事件、监控告警、CRM 客户、机柜/IDC、vCenter 集群资源概览。\n"
        "适用: 任何数据查询类请求 — 备份状态、GID/客户/站点信息、虚拟机列表、"
        "事件/告警记录、平台/集群资源、存储/datastore、容量使用; "
        "用户列出对象清单(VM名/主机名/IP)查询状态/备份/覆盖情况、以 IP 地址开头的资源查询、"
        "G+数字 GID (如 G123456)、4-6 位纯数字编号 (如 88381) 也是数据查询。\n"
        "边界: 5-6 位数字编号或 GID、查询/列出/搜索虚拟机、平台资源/集群资源/资源池查询"
        "均发此 Agent (而非 kb_agent/vm_agent/general_agent)。"
    ),
    "kb_agent": (
        "知识库搜索和 RAG 问答 Agent。能力: 搜索内部知识库文档、运维流程规范、"
        "故障排查方法、最佳实践。\n"
        "适用: 用户问'XX 怎么处理/排查'、查运维手册/流程/规范、搜索文档资料、"
        "问 VPN/网络/系统故障的处理方法。\n"
        "边界: 问'怎么做/怎么处理/排查方法/操作指南'发此 Agent; "
        "问'有哪些故障事件/告警记录'是数据查询应发 opscore_agent。"
    ),
    "ops_agent": (
        "系统运维 Agent。能力: 主机巡检、资源监控、日志分析、批量命令执行、健康报告生成。\n"
        "适用: 检查主机 CPU/内存/磁盘、分析系统日志、执行健康检查、批量运维操作。\n"
        "边界: 询问排查方法/处理指南是知识问题应发 kb_agent。"
    ),
    "network_agent": (
        "网络运维 Agent。能力: 网络连通性检测、端口扫描、路由追踪、DNS 解析测试。\n"
        "适用: ping 检测、端口扫描、traceroute、交换机/路由器配置审计。"
    ),
    "vm_agent": (
        "虚拟化直接操作 Agent。能力: 对具体已知 VM 名称执行开机/关机/重启/快照/迁移。\n"
        "适用: 对某台具名 VM 下达操作指令 (重启/开关机/创建快照/迁移到某宿主机)。\n"
        "边界: 查询/列出/搜索虚拟机信息应使用 opscore_agent 而非此 Agent。"
    ),
    "dev_agent": (
        "脚本开发 Agent。能力: 编写运维脚本、执行 Ansible Playbook、SSH 批量执行、配置自动化。\n"
        "适用: 写脚本/写代码/Python 自动化/Ansible Playbook 等开发任务。"
    ),
    "browser_agent": (
        "浏览器自动化 Agent。能力: Web 管理页面截图、自动登录、页面操作。\n"
        "适用: 截图管理后台、打开某管理页面、自动登录并操作。"
    ),
    "nms_agent": (
        "IBM Netcool NMS 告警 Agent。能力: NMS/网管告警查询、分析、立即抓取、告警监控报告。\n"
        "适用: NMS 告警怎么样、查网管告警、立即抓取 NMS 告警、告警分析报告。"
    ),
    "general_agent": (
        "通用问答 Agent。能力: 一般性对话、运维常识问答、概念解释、帮助信息。\n"
        "适用: 日常对话、模糊问题、无法明确归类的问题。当其他 Agent 都不合适时作为兜底。"
    ),
}


def build_agent_tool_definitions() -> list:
    """从 agents.yaml (AgentRegistry 单一事实源) 动态生成 Agent-as-Tool 定义。

    优先级: capability_summary > description > 内置兜底;
    registry 加载失败或条目过少时整体回退内置兜底, 保证路由永不中断。
    """
    defs = []
    try:
        from core.registry import get_registry
        registry = get_registry()
        for tool_name, agent_name in _TOOL_TO_AGENT.items():
            cfg = registry.get(agent_name)
            summary = ""
            if cfg:
                summary = cfg.capability_summary or cfg.description
            if not summary:
                summary = _FALLBACK_SUMMARIES.get(agent_name, "")
            if not summary:
                continue
            defs.append({
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": summary,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "用户的原始问题或查询意图"
                            }
                        },
                        "required": ["query"]
                    }
                }
            })
        if len(defs) >= 5:
            return defs
        logger.warning(f"[orchestrator] registry 动态生成仅 {len(defs)} 条, 回退内置兜底")
    except Exception as e:
        logger.warning(f"[orchestrator] registry 不可用, 使用内置兜底描述: {e}")
    # 兜底: 从 _FALLBACK_SUMMARIES 生成
    return [
        {
            "type": "function",
            "function": {
                "name": f"dispatch_{agent_name}",
                "description": summary,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "用户的原始问题或查询意图"}
                    },
                    "required": ["query"]
                },
            },
        }
        for agent_name, summary in _FALLBACK_SUMMARIES.items()
    ]


_TOOL_TO_AGENT = {
    "dispatch_opscore_agent": "opscore_agent",
    "dispatch_kb_agent": "kb_agent",
    "dispatch_ops_agent": "ops_agent",
    "dispatch_network_agent": "network_agent",
    "dispatch_vm_agent": "vm_agent",
    "dispatch_dev_agent": "dev_agent",
    "dispatch_browser_agent": "browser_agent",
    "dispatch_nms_agent": "nms_agent",
    "dispatch_general_agent": "general_agent",
}

# 向下兼容的模块级快照 — 必须在 _TOOL_TO_AGENT 之后: build_agent_tool_definitions()
# 内部遍历 _TOOL_TO_AGENT, 定义在前会在 import 时 NameError 回退内置兑底
# (曾表现为启动日志 "registry 不可用: name '_TOOL_TO_AGENT' is not defined")
AGENT_TOOL_DEFINITIONS = build_agent_tool_definitions()


class Orchestrator(BaseAgent):
    name = "orchestrator"
    description = "Z-OPS 核心编排器 — 意图识别 & 任务路由"

    def __init__(self):
        super().__init__()
        self._pending_tasks = {}  # msg_id -> task info
        # v4.0 (Phase 2.2): 从 agents.yaml 加载声明式 routing_hints (快速路由补充层)
        self._routing_hints: dict = {}
        try:
            from core.registry import get_registry
            for aid, cfg in get_registry().get_enabled().items():
                if cfg.routing_hints:
                    self._routing_hints[aid] = cfg.routing_hints
        except Exception as e:
            logger.debug(f"[orchestrator] routing_hints 加载失败: {e}")

    async def handle(self, msg: AgentMessage):
        """处理消息 (参考 claude-code/hermes-agent 路由模式)

        路由层级:
        - Layer 0: 显式 target → 直接转发
        - Layer 1: 指定 action → COMMAND_ROUTES 精确匹配
        - Layer 2: 自然语言 → LLM function_calling 原生路由
        """

        # Layer 0: 显式指定 target agent
        if msg.target and msg.target not in ("orchestrator", "manager", ""):
            await self._forward_to_agent(msg, msg.target, msg.action or "chat")
            return

        # Layer 1: 程序性 action 直接路由 (仅限 API/按钮触发的精确 action)
        if msg.action and msg.action in COMMAND_ROUTES:
            await self._route_command(msg)
            return

        # Layer 0.5: 显式记忆指令 "记住: ..." → 直接落 wiki 并回复 (v4.0 Phase 3.2)
        # (行首严格匹配, 不影响正常运维对话; 路由评测不走 handle 不受影响)
        # P0 受众: 私聊记忆 private 仅本人可见; 群聊记忆 group 仅该群可见 (不流入全局检索)
        if msg.text:
            try:
                from core.memory.synthesizer import capture_explicit_memory, save_explicit_memory
                mem_content = capture_explicit_memory(msg.text)
                if mem_content:
                    _aud = str((msg.provenance or {}).get("audience") or "")
                    pid = save_explicit_memory(mem_content, msg.username or msg.source or "user",
                                               audience=_aud)
                    # Hermes MemorySystem: 同步写入文件化记忆 (偏好/事实分层)
                    try:
                        from core.memory_system import get_memory, MemoryEntry, MemoryType
                        _uname = msg.username or "user"
                        _ms = get_memory(_uname)
                        _mt = MemoryType.USER if any(k in mem_content for k in ("喜欢", "偏好", "习惯", "总是")) \
                            else MemoryType.FEEDBACK if any(k in mem_content for k in ("不要", "错误", "应该")) \
                            else MemoryType.PROJECT
                        _ms.add(MemoryEntry(name=mem_content[:40], description="用户显式指令",
                                            type=_mt, content=mem_content))
                    except Exception as _ms_err:
                        logger.debug(f"[orchestrator] MemorySystem 写入失败 (不阻断): {_ms_err}")
                    # P2 收尾: 显式记忆同步画像 (opt-in ZOPS_USER_PROFILE; 仅显式来源)
                    if getattr(config, "USER_PROFILE_ENABLED", False) and msg.username:
                        try:
                            from core.memory.user_profile import get_profile_manager
                            await (await get_profile_manager()).add_explicit_note(
                                msg.username, mem_content)
                        except Exception as _pe:
                            logger.debug(f"[orchestrator] 画像显式写入失败: {_pe}")
                    await self.send_progress(msg, "已写入长期记忆")
                    _vis_note = (f"\n可见范围: 本群 (群聊内容仅群内检索可见)"
                                 if _aud else "\n可见范围: 仅本人 (私聊记忆不会出现在他人或群回复中)")
                    await self.reply(msg, result={
                        "text": (f"✅ 已记住：{mem_content}\n"
                                 "(可在管理面板「记忆」页查看或删除)"
                                 + _vis_note),
                        "memory_page_id": pid,
                    })
                    return
            except Exception as e:
                logger.warning(f"[orchestrator] 显式记忆处理失败 (继续正常路由): {e}")

        # Layer 1.4: 复合任务拆分 (浏览器+shell 等强意图组合, 串行执行合并回复)
        if msg.text and (msg.action or "chat") in ("chat", ""):
            compound = self._split_compound(msg.text)
            if compound:
                await self.send_progress(msg, f"🔀 检测到复合任务, 拆分为 {len(compound)} 个子任务串行执行")
                sections, agents_all = [], []
                for i, (part_text, target) in enumerate(compound, 1):
                    await self.send_progress(msg, f"▶ 子任务 {i}/{len(compound)} → {target}: {part_text[:40]}")
                    sub_text, sub_agents = await self._run_subtask(msg, part_text, target)
                    sections.append(f"**{target}**\n{sub_text}")
                    agents_all.extend(sub_agents or [target])
                combined = "🔀 **复合任务结果**\n\n" + "\n\n---\n\n".join(sections)
                await self.reply(msg, result={"text": combined,
                                              "agents": list(dict.fromkeys(agents_all))})
                return

        # Layer 1.45: 多 agent 并行协同 (fan-out/fan-in)
        # 复杂运维问题 → 同时分派各领域专家做检测/查询 → 并行执行 → 综合
        if msg.text and (msg.action or "chat") in ("chat", ""):
            team = self._detect_multi_agent_team(msg.text)
            if team and len(team) >= 2:
                await self.send_progress(msg, f"👥 检测到多面问题, 并行调度 {len(team)} 个专家: {', '.join(team)}")
                import asyncio as _aio
                tasks = {}
                for agent in team:
                    sub_prompt = self._team_subtask_prompt(agent, msg.text)
                    tasks[agent] = self._run_subtask(msg, sub_prompt, agent, timeout_s=120)
                results = await _aio.gather(*tasks.values(), return_exceptions=True)
                sections, agents_all = [], []
                for (agent, _), res in zip(tasks.items(), results):
                    if isinstance(res, Exception):
                        sections.append(f"**{agent}**\n⚠️ 执行异常: {str(res)[:150]}")
                    else:
                        sub_text, sub_agents = res
                        if sub_text and sub_text.strip():
                            sections.append(f"**{agent}**\n{sub_text}")
                    agents_all.append(agent)
                if sections:
                    combined = await self._synthesize_team_report(msg, team, sections)
                    await self.reply(msg, result={"text": combined,
                                                  "agents": list(dict.fromkeys(agents_all))})
                    return

        # Layer 1.5: 关键词快速路由 (跳过 LLM 调用, 延迟 <1ms)
        if msg.text:
            fast_agent = self._keyword_fast_route(msg)
            if fast_agent:
                await self.send_progress(msg, f"已路由到 {fast_agent}，正在处理...")
                await self._forward_to_agent(msg, fast_agent, "chat")
                return

        # Layer 2: 自然语言 → LLM function_calling 智能路由 (回退, 5-15s)
        if msg.text:
            # 立即发送进度，让前端知道消息已收到、正在处理
            await self.send_progress(msg, "正在分析您的问题，智能路由中...")
            await self._intent_route(msg)
            return

        await self.reply(msg, error="消息格式无效: 缺少 action 或 text")

    async def _route_command(self, msg: AgentMessage):
        """根据命令路由表转发"""
        agent_name, action = COMMAND_ROUTES[msg.action]
        await self._forward_to_agent(msg, agent_name, action)

    # ═══ 复合任务拆分 (10-21 修"浏览器+shell 调度错 agent") ═══
    # 显式浏览器意图必须压过 _DEV_EXACT_PATTERNS 的 shell 词匹配;
    # 浏览器+shell 复合句 (含分隔词) → 拆分串行执行, 合并一条回复 (单回复契约不变)
    _BROWSER_INTENT = ['用浏览器打开', '打开网页', '浏览器上', '网页上操作', 'computer use',
                       '访问网站', '帮我截图网页', '浏览器访问', '用浏览器']
    _SHELL_INTENT_RE = [r'\bshell\b', r'\bbash\b', r'执行.{0,6}命令', r'运行.{0,6}命令']
    _COMPOUND_SEPS = r'[，,。;；\n]\s*(?:然后|之后|接着|随后|再|并且)'

    def _split_compound(self, text: str):
        """浏览器意图 + shell 意图 + 分隔词 → [(part, agent), ...]; 不满足返回 None"""
        t = (text or '').strip()
        if not (any(k in t for k in self._BROWSER_INTENT)
                and any(re.search(p, t) for p in self._SHELL_INTENT_RE)):
            return None
        parts = re.split(self._COMPOUND_SEPS, t, maxsplit=1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            return None
        a, b = parts[0].strip(), parts[1].strip()
        a_browser = any(k in a for k in self._BROWSER_INTENT)
        b_browser = any(k in b for k in self._BROWSER_INTENT)
        if a_browser == b_browser:
            return None   # 两段同向 (都浏览器/都 shell) → 不拆
        # 按原文顺序排列: 浏览器段→computer_agent, 命令执行段→ops_agent
        # (ops 有真实 subprocess 执行工具; dev_agent 仅限登记脚本, 会给示例不真执行)
        return ([(a, "computer_agent"), (b, "ops_agent")] if a_browser
                else [(a, "ops_agent"), (b, "computer_agent")])

    # ── 多 agent 并行协同: 领域意图检测 ──
    # 问题同时命中 >=2 个领域 → 该领域 agent 加入并行 team
    _TEAM_DOMAINS = {
        'kb_agent':       ['知识库', '文档', '操作指南', '最佳实践', '手册', '流程', '规范', '方案', '架构'],
        'opscore_agent':     ['cmdb', 'veeam', '备份', '虚拟机', 'vm', '资源', 'brr', 'vcd', 'smart-ops', '工单',
                           'goc', '平台数据', '资产', '清单', 'ip', '源ip', 'dns记录', '域名解析',
                           'cidr', '/32', '/24', 'vlan', '路由', 'firewall', 'acl'],
        'network_agent':  ['dns', 'sdns', '网络', '连通性', '路由', 'traceroute', 'ping',
                           '防火墙', 'nat', 'ipsec', 'vpn', 'bandwidth', '延迟', '丢包',
                           'instagram', '域名', 'cdn', '解析', '出口'],
        'ops_agent':      ['巡检', '健康', '告警', '监控', '故障', '排查', '日志', '性能',
                           'cpu', '内存', '磁盘', 'uptime', '服务状态', '系统分析'],
        'nms_agent':      ['nms', 'netcool', '网管告警', '告警事件'],
    }

    def _detect_multi_agent_team(self, text: str) -> list:
        """检测问题是否命中多个领域 → 返回应并行调度的 agent 列表 (2-4 个)
        条件: 文本 >= 15 字 + 命中 >= 2 个不同领域 + 含技术细节信号"""
        t = (text or '').strip()
        if len(t) < 15:
            return []
        hits = set()
        for agent, kws in self._TEAM_DOMAINS.items():
            for kw in kws:
                if kw in t.lower():
                    hits.add(agent)
                    break
        # 单领域不动 (走正常单路由); >=2 个领域 → 并行
        if len(hits) < 2:
            return []
        # 上限 4 个 agent (避免过度并行)
        return sorted(hits)[:4]

    def _team_subtask_prompt(self, agent: str, user_question: str) -> str:
        """为并行协同的每个 agent 生成领域专属子任务指令 — 不是把原问题扔给所有人做同样的事,
        而是让每个 agent 只做自己擅长的那部分 (检测/查询/检索), 分析由综合层完成"""
        _anti_halluc = ("\n⚠️ 准确性铁律: 查询/检测返回空或未包含目标对象时, 必须明确说\"未查到相关数据\"。"
                        "\n绝不编造数据 (IP/DNS记录/表格/CMDB配置)。如未找到, 原样说明即可。\n")
        prompts = {
            "network_agent": (
                "执行网络连通性检测并返回原始结果。\n"
                "只做检测, 不要做根因分析或修复建议 (分析由综合层完成)。\n"
                "可以做的事: DNS 解析对比 (不同 DNS 服务器对同一域名的解析结果)、"
                "ping/traceroute 连通性测试、端口检测。\n"
                f"用户问题: {user_question}"
            ),
            "opscore_agent": (
                "查询平台数据并返回原始查询结果。\n"
                "只做数据查询 (CMDB/监控/备份/DNS记录/资产), 不要做分析或建议。\n"
                f"用户问题: {user_question}"
            ),
            "kb_agent": (
                "在知识库中搜索与该问题相关的文档/流程/最佳实践, 返回检索到的相关文档列表和要点。\n"
                f"用户问题: {user_question}"
            ),
            "ops_agent": (
                "执行系统诊断检查并返回原始结果 (主机状态/服务状态/日志关键行)。\n"
                "只做检查, 不要做根因分析 (分析由综合层完成)。\n"
                f"用户问题: {user_question}"
            ),
            "nms_agent": (
                "查询 NMS 网管告警数据, 返回相关告警事件的原始数据。\n"
                f"用户问题: {user_question}"
            ),
        }
        # 反幻觉铁律: 每个领域指令都附加
        for k in prompts:
            prompts[k] = prompts[k] + _anti_halluc
        return prompts.get(agent, user_question + _anti_halluc)

    async def _synthesize_team_report(self, msg: AgentMessage, team: list, sections: list) -> str:
        """综合各领域专家的检测结果 → 生成统一分析报告 (根因分析/结论/建议在这里完成)"""
        combined_data = "\n\n---\n\n".join(sections)
        sys_prompt = (
            "你是 Z-OPS 运维智能体的高级综合分析专家。多个领域专家已并行完成检测/查询,"
            "以下是他们的原始结果。请你:\n"
            "1. 综合所有发现, 进行根因分析 (不要重复各专家已做的检查)\n"
            "2. 给出明确的结论 (故障原因/状态判定)\n"
            "3. 给出具体的下一步操作建议\n"
            "4. 如果某方面数据不足, 明确说明还需要什么\n"
            "⚠️ 准确性铁律:\n"
            "- 各专家结果中标注\"未查到\"或无数据的项, 绝不可编造补充数据\n"
            "- 你的综合报告中的每个结论必须有数据支撑 (来自至少一个专家的原始结果)\n"
            "- 如果所有专家都没查到相关数据, 必须如实说\"各数据源均未查到相关数据\"\n"
            "用清晰的 Markdown 格式输出。"
        )
        try:
            synthesis = await self.llm_chat(
                prompt=f"用户原始问题: {msg.text}\n\n各领域专家检测结果:\n{combined_data[:6000]}",
                system=sys_prompt,
                username=msg.username or "",
                session_id=msg.session_id or "",
            )
            return (f"👥 **多专家协同报告**\n\n"
                    "---\n\n"
                    f"## 综合分析结论\n\n{synthesis}\n\n"
                    "---\n\n"
                    "## 各专家原始检测结果\n\n" + combined_data)
        except Exception as e:
            logger.warning(f"[orchestrator] 综合分析 LLM 失败: {e}")
            return "👥 **多专家协同报告**\n\n" + combined_data

    async def _run_subtask(self, base_msg: AgentMessage, part_text: str, target: str,
                           timeout_s: int = 180) -> tuple:
        """子任务: 新 msg_id 转发给 target, 订阅子回复通道等待结果 (不影响主回复契约)"""
        import uuid as _uuid
        sub = AgentMessage(
            msg_id=f"sub_{_uuid.uuid4().hex[:12]}",
            source="orchestrator", target=target, action="chat",
            params={}, text=part_text,
            channel_source=base_msg.channel_source,
            session_id=base_msg.session_id, username=base_msg.username,
            provenance={"route_path": ["orchestrator", target]})
        r = await self.get_redis()
        sub_ch = config.reply_channel(sub.msg_id)
        p = r.pubsub()
        await p.subscribe(sub_ch)
        try:
            await r.publish(config.channel(target), sub.to_json())
            logger.info(f"[orchestrator] 子任务转发: {part_text[:40]} → {target}")
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                raw = await p.get_message(ignore_subscribe_messages=True,
                                          timeout=min(5, max(1, deadline - time.time())))
                if not raw or raw.get("type") != "message":
                    continue
                data = raw.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                try:
                    maybe = json.loads(data)
                    # progress/delta 中间事件与回复同通道 (webchat 同款过滤), 略过等待最终回复
                    if maybe.get("_type") in ("progress", "delta"):
                        continue
                    rp = AgentReply.from_json(data)
                except Exception:
                    continue
                if rp.error:
                    return f"❌ {target}: {rp.error[:300]}", [target]
                return (rp.result.get("text") or "")[:4000], (rp.result.get("agents") or [target])
            return f"⏱️ {target} 子任务超时 ({timeout_s}s)", [target]
        finally:
            try:
                await p.unsubscribe(sub_ch)
                await p.close()
            except Exception:
                pass

    # ═══ 关键词快速路由 (跳过 LLM, 延迟 <1ms) ═══
    _GREETING_PATTERNS = [
        '你好', 'hello', 'hi', 'hey', '早上好', '下午好', '晚上好',
        '在吗', '在不在', '帮帮我', '有人吗', '测试', 'test',
    ]
    # v4.0 (评测 llm-0104 守护): greeting 命中但文本含运维实质词时不是问候 —
    # 如 "这台测试虚拟机能关掉吗" (11字含'测试'曾被误拦到 general, 应走 vm-power 规则)
    _GREETING_BLOCK_WORDS = [
        '虚拟机', 'vm', '备份', '快照', '告警', '监控', '主机', '服务器',
        '存储', '网络', '交换机', '防火墙', 'dns', '域名', '迁移',
        '开机', '关机', '关掉', '重启', 'ping',
    ]
    # 运维工具/产品名优先匹配 (通用 IT 工具 → general_agent，有 Wiki + LLM 广泛知识)
    _OPS_PRIORITY = ['powershell', 'vmware', 'esxi', 'vcenter', 'hyper-v', 'nutanix']
    _DEV_KEYWORDS = ['写脚本', '写代码', 'python', '脚本开发', '编程', '代码review']
    # shell 用词边界匹配避免误匹配 powershell
    _DEV_EXACT_PATTERNS = [r'\bshell\b', r'\bbash\b']
    _OPS_KEYWORDS = ['快照', '存储', '网络', 'dns', 'vpn',
                     '服务器', 'linux', 'windows', '备份', '监控', '告警', '故障', '排查',
                     '部署', '配置', '安装', '重启', '日志', '权限', '证书', '防火墙',
                     # DNS 知识类问题 (zonefile 导出/域名查询等) 稳定路由 kb_agent，与 KB 徽章语义一致
                     'zonefile', '域名', 'dns记录', '域名解析', 'cname', 'mx记录']
    # OPS-CORE 资源查询: GID/数字编号 + 资源关键词 → opscore_agent (有真实 API 数据)
    _OPSCORE_RESOURCE_KEYWORDS = [
        'vm', '虚拟机', '备份', '快照', '存储', 'vcd', 'cmdb', 'dns',
        '域名', '监控', '告警', '资源', '有什么', '有哪些', '查看', '列表',
        '站点', '组织', '迁移', 'ha', 'aria', 'brr', 'm365',
        '电源', '开机', '关机', '重启', 'snapshot', 'backup',
    ]
    # 无 GID 也能路由到 opscore_agent 的平台资源关键词 (集群/平台层面的资源查询)
    _PLATFORM_RESOURCE_KEYWORDS = [
        '平台资源', '集群资源', '资源池', '资源概览', '资源情况',
        '资源显示', '资源使用', 'vcenter资源', 'vcd资源',
        '宿主机资源', '存储空间', '存储池', 'datastore',
        '容量规划', '容量使用', '资源分配',
        '检查资源', '查资源', '真实资源', '检查真实',
        # 存储相关查询 (优先于 _OPS_KEYWORDS 的 '存储', 确保走 opscore_agent 而非 kb_agent)
        '存储资源', '存储部署', '存储配置', '存储检查',
        'vcenter', 'nimble', 'netapp',
    ]
    # IP + 资源查询模式: IP地址 + 备份/虚拟机等关键词 → opscore_agent (查实际数据而非知识库)
    _IP_RESOURCE_PATTERN = re.compile(
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}.*?(备份|虚拟机|vm|快照|存储|backup|snapshot)',
        re.IGNORECASE
    )
    # 备份查询动词模式: "有哪些备份" / "备份上有多少" 等 → opscore_agent
    _BACKUP_QUERY_PATTERNS = [
        '有哪些备份', '有多少备份', '备份上有多少', '备份任务', '备份作业',
        '备份文件', '备份大小', '备份数量', '哪些备份', '多少备份',
        '备份job', '备份失败',
    ]
    # v4.0 (Phase 2.1 评测驱动): 平台事件/告警记录查询 → opscore_agent (数据查询而非知识)
    _OPSCORE_EVENT_KEYWORDS = ['故障事件', '事件记录', '告警记录', '运维事件']
    # v4.0 (Phase 2.1 评测驱动): 具名 VM 电源操作 → vm_agent (优先于 opscore 查询/ops 知识)
    _VM_POWER_VERBS = ['开机', '关机', '关掉', '关了', '重启', '创建快照', '做快照', '打快照',
                       '快照回滚', '恢复快照', '迁移', 'power on', 'power off',
                       'reboot', 'shutdown']
    # 连字符主机名 (vm-web-01/test-server): 用 ASCII lookaround 而非 \b (中文属于 \w 会使 \b 失效)
    _HYPHEN_NAME_PATTERN = re.compile(
        r'(?<![a-zA-Z0-9-])[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)+(?![a-zA-Z0-9-])')
    # v4.0 (Phase 2.1 评测驱动): NMS 网管告警 → nms_agent / 网络检测动词 → network_agent
    _NMS_KEYWORDS = ['nms', 'netcool', '网管告警', '网管']
    _NETWORK_PATTERN = re.compile(
        r'(\bping\b|traceroute|tracert|端口扫描|扫描.{0,4}端口|port\s*scan)', re.IGNORECASE)
    # v4.0 (Phase 2.1 评测驱动): 方法疑问词 / 动作请求词 — 用于 opscore事件、vm操作、ops知识 三处分支的细分
    # 知识名词 (原因/原理/区别) 亦属方法询问 (评测 llm-0105 "不建议关机的原因" → kb_agent)
    _KB_METHOD_WORDS = ['怎么', '如何', '为什么', '什么', '啥', '方法', '指南',
                        '规范', '流程', '手册', '最佳实践', '排查', '处理',
                        '原因', '原理', '区别', '风险']
    _OPS_ACTION_WORDS = ['分析', '检查', '巡检', '执行', '生成', '批量', '抓取']
    # GID 匹配: 要求明确前缀 gid/g，或数字序列不在字母中（避免把 ESXI-03 中的 3103 误认为 GID）
    _GID_PATTERN = re.compile(r'(?:(?:gid|g)(\d{4,6})|(?<![a-zA-Z])(\d{4,6})(?![a-zA-Z]))', re.IGNORECASE)

    # VM 主机名模式: 包含字母+数字的混合字符串 + 虚拟机/VM关键词 → opscore_agent
    _VM_HOSTNAME_PATTERN = re.compile(r'[A-Za-z]{2,}\d+[A-Za-z0-9]*', re.IGNORECASE)
    _VM_QUERY_KEYWORDS = ['虚拟机', 'vm', 'vps', '虚机', '云主机']
    # 主机类查询词: "ESXI-02主机状态"/"查一下ESXI-02上有什么机器" 不含 VM 关键词,
    # 原先落入 LLM 路由 (5-6s 延迟 + 网关内容安全偶发误判降级 general_agent 答非所问)
    _HOST_QUERY_KEYWORDS = ['主机', '宿主机', 'esxi', '机器', '服务器', '节点']

    def _keyword_fast_route(self, msg: AgentMessage) -> str:
        """关键词匹配快速路由，避免 LLM 调用开销 (5-15s)
        
        设计原则: 仅在高置信度匹配时快速路由，其余返回空字符串
        交由 LLM 智能路由 (能理解上下文、追问场景)。
        
        返回 agent 名称，空字符串表示需要 LLM 路由"""
        text = msg.text.lower().strip()
        if not text:
            return ""
        # 长文本 VM 清单查询 (如"查询这22台VM的备份任务状态: ESXI-HK-8025, ..."):
        # 文本长度超 200 会触发提前返回，但清单类查询恰是长文本，
        # 必须在此检测多个主机名+状态查询词 → opscore_agent，否则落入 LLM 路由被误判
        if len(text) > 200:
            hosts = set(self._VM_HOSTNAME_PATTERN.findall(text))
            if len(hosts) >= 3 and any(k in text for k in ('备份', '状态', '快照', 'backup', '恢复', '监控')):
                logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}...' → opscore_agent (vm-list-query)")
                return "opscore_agent"
            return ""
        # 简单问候 → general_agent (快速响应, 无需 LLM)
        # v4.0: 仅短文本生效 — 防止"测试一下网络连通性"/"你好，请查GID 47141的虚拟机"被误拦 (评测 kw-1xx 守护)
        # v4.0: 命中 greeting 词但含运维实质词时跳过 (评测 llm-0104 "这台测试虚拟机能关掉吗" 守护)
        if len(text) <= 12 and any(p in text for p in self._GREETING_PATTERNS) \
                and not any(b in text for b in self._GREETING_BLOCK_WORDS):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → general_agent (greeting)")
            return "general_agent"
        has_method_word = any(w in text for w in self._KB_METHOD_WORDS)
        # OPS-CORE 资源查询: GID/数字编号 + 资源关键词 → opscore_agent (有真实 API 数据)
        if self._GID_PATTERN.search(text) and any(k in text for k in self._OPSCORE_RESOURCE_KEYWORDS):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → opscore_agent (gid+resource)")
            return "opscore_agent"
        # 具名 VM 电源操作: 电源动词 + 具体对象名 (vm词/主机名/连字符名) 且非方法询问 → vm_agent
        # v4.0: 修复"帮我重启vm-web-01"被 ops '重启' 关键词误路由 kb_agent (评测 kw-5xx 守护)
        # 仅限「单对象短指令」: 多主机名是清单查询(→opscore), 长文本是方案咨询(→LLM), 均非单机操作
        if len(text) <= 60 and any(v in text for v in self._VM_POWER_VERBS) and not has_method_word \
                and len(set(self._VM_HOSTNAME_PATTERN.findall(text))) <= 1 \
                and (any(k in text for k in self._VM_QUERY_KEYWORDS)
                     or self._VM_HOSTNAME_PATTERN.search(text)
                     or self._HYPHEN_NAME_PATTERN.search(text)):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → vm_agent (vm-power)")
            return "vm_agent"
        # VM/主机名资源查询: 字母数字混合名/连字符名 + (VM关键词|主机类词) → opscore_agent
        # (如 "找到ESXI-03的虚拟机列表"; "ESXI-02主机状态"; "怎么给vm-web-01做快照"是知识问题 → kb_agent)
        # "有什么/哪些/几台"是列举询问非知识提问, 穿透方法词检查
        # (否则 "查一下ESXI-02上有什么机器" 被 '什么' 误伤落入 LLM 路由)
        _list_query = any(w in text for w in ('有什么', '哪些', '几台'))
        if (not has_method_word or _list_query) \
                and (self._VM_HOSTNAME_PATTERN.search(text) or self._HYPHEN_NAME_PATTERN.search(text)) \
                and any(k in text for k in self._VM_QUERY_KEYWORDS + self._HOST_QUERY_KEYWORDS):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → opscore_agent (vm-hostname)")
            return "opscore_agent"
        # 平台资源查询 (无需 GID, 关键词明确指向平台数据)
        if any(k in text for k in self._PLATFORM_RESOURCE_KEYWORDS):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → opscore_agent (platform-resource)")
            return "opscore_agent"
        # IP + 资源查询: "198.18.96.169上有哪些备份" → opscore_agent (查实际数据)
        if self._IP_RESOURCE_PATTERN.search(text):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → opscore_agent (ip+resource)")
            return "opscore_agent"
        # 备份查询模式: "有哪些备份" / "备份上有多少" → opscore_agent
        if any(p in text for p in self._BACKUP_QUERY_PATTERNS):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → opscore_agent (backup-query)")
            return "opscore_agent"
        # 平台事件/告警记录查询 (数据) → opscore_agent; 问排查方法 (知识) 落到后面 ops 分支 → kb_agent
        if not has_method_word and any(k in text for k in self._OPSCORE_EVENT_KEYWORDS):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → opscore_agent (opscore-events)")
            return "opscore_agent"
        # 显式浏览器意图 → computer_agent (必须早于 shell 词匹配:
        # 10-21 "用浏览器打开X 然后执行 shell 命令" 曾被 shell 词抢路由到 dev_agent)
        if any(k in text for k in self._BROWSER_INTENT):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → computer_agent (browser-intent)")
            return "computer_agent"
        # 命令执行意图 (执行/运行…命令) → ops_agent 本机只读诊断直通
        # (排在写脚本关键词之后, 避免劫持 "写python脚本执行..." → dev)
        if re.search(r'(?:执行|运行)\s*(?:shell\s*|bash\s*)?命令', text) and \
           not any(k in text for k in self._DEV_KEYWORDS):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → ops_agent (cmd-exec)")
            return "ops_agent"
        # 开发/编码类 → dev_agent (shell/bash 用词边界匹配)
        if any(k in text for k in self._DEV_KEYWORDS) or \
           any(re.search(p, text) for p in self._DEV_EXACT_PATTERNS):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → dev_agent (dev)")
            return "dev_agent"
        # 网络检测动词 → network_agent (ping/traceroute/端口扫描, 高置信操作请求)
        if self._NETWORK_PATTERN.search(text):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → network_agent (network-probe)")
            return "network_agent"
        # NMS/网管告警 → nms_agent (优先于 ops '告警' 关键词, 避免误路由 kb_agent)
        if any(k in text for k in self._NMS_KEYWORDS):
            logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → nms_agent (nms)")
            return "nms_agent"
        # 运维关键词细分 (v4.0 评测驱动):
        #   方法疑问 (怎么/如何/排查...) → kb_agent 知识库
        #   动作请求 (分析/巡检/执行...) 且无疑问词 → ops_agent
        #   其余低置信命中 → 交由 LLM 智能路由
        if any(k in text for k in self._OPS_KEYWORDS):
            if has_method_word:
                logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → kb_agent (ops-knowledge)")
                return "kb_agent"
            if any(w in text for w in self._OPS_ACTION_WORDS):
                logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → ops_agent (ops-action)")
                return "ops_agent"
        # v4.0 (Phase 2.2): agents.yaml routing_hints 声明式补充层
        # (内置规则之后、LLM 之前; 仅高置信词 + 排除方法疑问句, 由评测集守护)
        if self._routing_hints and not has_method_word:
            for agent_name, hints in self._routing_hints.items():
                if any(h in text for h in hints):
                    logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → {agent_name} (yaml-hints)")
                    return agent_name
        # 未匹配 → 返回空, 交由 LLM 智能路由 (能理解上下文和追问)
        logger.info(f"[orchestrator] 关键词快速路由: '{text[:30]}' → 无匹配, 交由 LLM 路由")
        return ""

    async def route_via_llm(self, text: str, history: list = None) -> tuple:
        """LLM 原生 function_calling 路由决策 (纯决策，不转发 — 供 _intent_route 与评测 runner 共用)

        Returns:
            (agent_name, action, reasoning)
        """
        from core.query_engine import QueryEngine, QueryConfig

        # 构建消息 (含历史，供 LLM 理解追问场景)
        messages = []
        if history:
            for h in history[-4:]:  # 最近 4 条历史
                role = h.get("role", "user")
                content = h.get("content", "")[:300]
                source = h.get("source", "")
                if role == "assistant" and source:
                    content = f"[via {source}] {content}"
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": text})

        llm_cfg = await self._get_llm_config()
        engine = QueryEngine(llm_config=llm_cfg)
        qc = QueryConfig(
            max_turns=1,
            temperature=0.1,
            enable_tool_calls=True,
            tool_choice="required",  # 强制模型必须调用一个 tool
            max_output_tokens=256,   # 路由决策不需要长输出
        )
        cfg = engine._resolve_config()
        full_messages = [{"role": "system", "content": ROUTE_SYSTEM_PROMPT}] + messages

        turn_result = await engine._call_llm(
            messages=full_messages,
            tools=build_agent_tool_definitions(),  # 动态生成: 感知 agents.yaml 热加载
            config=cfg,
            query_config=qc,
        )

        # 部分网关对 tool_choice=required 支持不完整 (模型纯文本回复) —
        # 追加强制指令重试一次; 仍无 tool_call 才降级 general_agent
        if not turn_result.get("tool_calls"):
            logger.info("[orchestrator] LLM 未返回 tool_call, 追加强制指令重试一次")
            turn_result = await engine._call_llm(
                messages=full_messages + [
                    {"role": "assistant", "content": (turn_result.get("content") or "")[:200]},
                    {"role": "user", "content": "请必须通过调用上面列出的 Agent 工具来完成路由决策，禁止纯文本回复。"},
                ],
                tools=build_agent_tool_definitions(),
                config=cfg,
                query_config=qc,
            )

        # 解析 tool_call 结果
        tool_calls = turn_result.get("tool_calls", [])
        agent_name = "general_agent"
        reasoning = ""

        if tool_calls:
            tc = tool_calls[0]
            func_name = tc.get("function", {}).get("name", "")
            func_args_raw = tc.get("function", {}).get("arguments", "{}")
            agent_name = _TOOL_TO_AGENT.get(func_name, "general_agent")
            try:
                func_args = json.loads(func_args_raw) if isinstance(func_args_raw, str) else func_args_raw
            except json.JSONDecodeError:
                func_args = {}
            reasoning = f"tool_call={func_name}"
            logger.info(f"[orchestrator] function_call 路由结果: "
                        f"{func_name} → {agent_name} (args={func_args})")
        else:
            content = turn_result.get("content", "")
            # 省略指代追问 ("有几台？") 且 LLM 拒绝 tool_call 时 —
            # 按确定性规则路由到 history 中最后处理该会话的 Agent (与 system prompt 规则一致)
            last_agent = next((h.get("source") for h in reversed(history or [])
                               if h.get("role") == "assistant"
                               and str(h.get("source", "")).endswith("_agent")), "")
            if last_agent:
                agent_name = last_agent
                reasoning = f"no_tool_call→last_agent({last_agent})"
                logger.info(f"[orchestrator] LLM 未返回 tool_call, 按 history 回退到 {last_agent}")
            elif self._VM_HOSTNAME_PATTERN.search(text) or self._HYPHEN_NAME_PATTERN.search(text):
                # 网关内容安全误判 (如 '涉及提示词注入') / 纯文本拒绝时:
                # 含主机名模式的资源查询直接降 general 会答非所问 (知识库无平台数据),
                # 启发式兜底 opscore_agent (CMDB/VM/监控/告警全家桶)
                agent_name = "opscore_agent"
                reasoning = "no_tool_call→hostname_heuristic"
                logger.info(f"[orchestrator] LLM 未返回 tool_call, 主机名启发式兑底 → opscore_agent "
                            f"(content='{content[:60]}')")
            else:
                logger.warning(f"[orchestrator] LLM 未返回 tool_call, "
                               f"content='{content[:100]}', 降级 general_agent")
                reasoning = "no_tool_call_fallback"

        return agent_name, "chat", reasoning

    async def _intent_route(self, msg: AgentMessage):
        """LLM 原生 function_calling 路由 (参考 claude-code/hermes-agent 模式)

        核心思路: 将每个 Agent 暴露为一个 OpenAI tool/function，
        让模型通过原生 tool_call 机制选择最合适的 Agent。
        不依赖关键词写死、不做 JSON 文本分类、不注入 jieba 亲和度。
        路由决策逻辑在 route_via_llm (与 tests/routing 评测 runner 共用)。
        """
        try:
            # 构建对话上下文 (仅用于帮助 LLM 理解追问场景)
            history = msg.params.get("history", [])
            if not history and msg.session_id:
                try:
                    from core.session_engine import get_session_engine
                    engine = get_session_engine()
                    history = await engine.build_context_for_routing(
                        msg.session_id, current_text=msg.text
                    )
                    if history:
                        msg.params["history"] = history
                except Exception as e:
                    logger.debug(f"[orchestrator] SessionEngine fallback: {e}")

            logger.info(f"[orchestrator] function_call 路由: text='{msg.text[:60]}', "
                        f"history_len={len(history)}")

            # 通知前端：正在进行 LLM 路由决策
            await self.send_progress(msg, "正在进行智能路由决策...")

            agent_name, action, reasoning = await self.route_via_llm(msg.text, history)

            # 通知前端：路由已确定，正在转发
            await self.send_progress(msg, f"已路由到 {agent_name}，正在处理...")

            # 记录路由日志
            await self.log_daily(
                f"路由(function_call): '{msg.text[:50]}' → {agent_name} ({reasoning})",
                level="info", category="route", session_id=msg.session_id
            )

            await self._forward_to_agent(msg, agent_name, action)

        except Exception as e:
            logger.error(f"[orchestrator] function_call 路由异常: {e}", exc_info=True)
            await self.send_progress(msg, "路由异常，正在降级到通用问答...")
            await self._forward_to_agent(msg, "general_agent", "chat")



    async def _forward_to_agent(self, original_msg: AgentMessage,
                                 agent_name: str, action: str):
        """转发消息到目标 Agent 并等待回复"""
        r = await self.get_redis()

        # 检查目标 Agent 是否在线
        heartbeat_raw = await r.hget(config.KEY_HEARTBEATS, agent_name)
        if heartbeat_raw:
            try:
                hb = json.loads(heartbeat_raw)
                if hb.get("status") != "online":
                    await self.reply(original_msg,
                                     error=f"Agent [{agent_name}] 当前不在线")
                    return
                # 检查心跳超时 (30秒)
                last_hb = hb.get("last_heartbeat", 0)
                if time.time() - last_hb > 30:
                    await self.reply(original_msg,
                                     error=f"Agent [{agent_name}] 心跳超时，可能已离线")
                    return
            except json.JSONDecodeError:
                pass
        else:
            await self.reply(original_msg,
                             error=f"Agent [{agent_name}] 未注册")
            return

        # 构造转发消息 (带 provenance 追踪)
        provenance = dict(original_msg.provenance) if original_msg.provenance else {}
        if "route_path" not in provenance:
            provenance["route_path"] = []
        if "orchestrator" not in provenance["route_path"]:
            provenance["route_path"].append("orchestrator")
        if agent_name not in provenance["route_path"]:
            provenance["route_path"].append(agent_name)
        provenance["route_method"] = provenance.get("route_method", "")

        forward_msg = AgentMessage(
            msg_id=original_msg.msg_id,  # 保持原始 msg_id
            source="orchestrator",
            target=agent_name,
            action=action,
            params=original_msg.params,
            text=original_msg.text,
            channel_source=original_msg.channel_source,
            session_id=original_msg.session_id,
            username=original_msg.username,
            provenance=provenance
        )

        # 发送到目标 Agent
        ch = config.channel(agent_name)
        await r.publish(ch, forward_msg.to_json())
        logger.info(f"[orchestrator] 转发到 {agent_name}.{action} [user={original_msg.username or '-'}]")
        # v4.0 (Phase 6): 路由环节写入 ExecutionRegistry, trace_id=msg_id 贯穿全链
        try:
            from core.execution_registry import get_execution_registry, ExecutionType
            await get_execution_registry().record_tool_call(
                agent_id="orchestrator",
                tool_name=f"route:{agent_name}",
                input_summary=(original_msg.text or "")[:200],
                output_summary=f"forwarded to {agent_name}.{action}",
                success=True,
                latency_ms=0.0,
                session_id=original_msg.session_id,
                trace_id=original_msg.msg_id,
                exec_type=ExecutionType.AGENT_FORWARD,  # 路由转发不算工具调用 (口径拆分)
            )
        except Exception:
            pass
        # 路由决策写入每日日志 (带用户/会话，便于追溯哪个用户的问题被路由到哪个 agent)
        try:
            await self.log_daily(
                f"路由: {original_msg.text[:50]} → {agent_name}",
                level="info", category="route",
                session_id=original_msg.session_id, username=original_msg.username
            )
        except Exception:
            pass

        # 注意: 回复会直接通过 reply_channel(msg_id) 返回给调用者
        # Orchestrator 不需要中转回复 (Agent 直接回复到 reply channel)

    async def handle_command(self, cmd: str, args: str = "") -> dict:
        """处理直接命令调用 (来自 webchat_api)"""
        # 优先从 Router 查找
        router = get_router()
        route = router.get_command_route(cmd)
        if not route:
            route = COMMAND_ROUTES.get(cmd)
        if route:
            agent_name, action = route
            msg = AgentMessage(
                source="orchestrator",
                target=agent_name,
                action=action,
                text=args,
                params={"args": args}
            )
            reply = await self.send_and_wait(
                agent_name, action=action, text=args,
                params={"args": args}, timeout=config.REPLY_TIMEOUT
            )
            return {
                "msg_id": reply.msg_id,
                "result": reply.result,
                "error": reply.error
            }
        else:
            return {"error": f"未知命令: {cmd}"}


if __name__ == "__main__":
    run_agent(Orchestrator())
