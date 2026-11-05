"""
Z-OPS — opscore_agent: OPS-CORE 平台数据集成 Agent
对接两个 OPS-CORE 平台 API:
  - 198.18.18.145: 信息管理系统 (CMDB/机柜/监控/案例/DNS/数据中心)
  - 198.18.18.146: 管理平台 (BRR备份/快照管理/VCD资源/Aria Ops/M365/Smart-Ops)
使用 agents/opscore_api.py 共享模块
"""

import asyncio
import json
import logging
import re
import time
from typing import Optional, Dict, Any

from agents.base import BaseAgent, AgentMessage, run_agent
from agents.opscore_api import (
    OpscoreApiClient, get_info_client, get_mgt_client,
    INFO_API_CATALOG, MGT_API_CATALOG, DOMAIN_ENDPOINTS,
    get_domain_api_desc, query_api, smart_query,
    query_crm_customers, query_crm_customer_by_gid, query_crm_customer_resources,
    query_crm_dashboard, query_crm_storage_mappings,
    # 新增优化工具函数
    normalize_gid, bytes_to_gb, query_all_pages, query_by_server_url,
    robust_query, query_with_concurrency_limit, cached_query_api, clear_cache,
    # 主机 VM 查询 (MCP 全量缓存 + REST host= 服务端过滤)
    query_vms_on_esxi_host, expand_host_code,
)
from agents.ha_api import get_ha_client

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

OPSCORE_ANALYSIS_PROMPT = """你是 Z-OPS 运维智能体的 OPS-CORE 数据分析模块。

你通过两个 OPS-CORE API 服务器获取运维数据:
1. OPS-CORE 信息管理系统 (API服务) — 采集并存储: CMDB主机/服务器、机柜、IDC机房、监控、告警、案例/工单、DNS、数据中心、厂商、密码管理
2. OPS-CORE 管理平台 (API服务) — 采集并存储: Veeam备份(BRR)、虚拟机快照、VCD资源、Aria Ops/Logs、Smart-Ops智能运维(VM/事件/告警/状态)、M365备份

极其重要的概念区分:
- 以上两个 API 服务只是数据源，不是用户提问的对象
- 用户提问的服务器IP、主机名、站点、备份作业等信息，都来自 API 返回的数据内容（如 server_url、site、name、vm_name 等字段）
- 回答时引用的服务器、IP地址必须来自数据中的字段，绝不要把 API 服务器地址当作查询结果

你通过 API 可以查询以下数据 (数据中的 server_url/site/name 等字段才是实际服务器信息):
- 虚拟机: vCenter 资源池 VM、VCD 资源、Aria Ops VM、CMDB信息
- 备份: BRR作业/对象/仓库/会话/统计、服务器健康、还原点
- 监控告警: 监控数据、告警、运维事件(严重/未确认)、日志告警/事件/任务
- DNS: 记录查询、SPF、DMARC、Zone
- 基础设施: CMDB、机柜、IDC、站点、数据中心、厂商
- 快照: 快照列表、汇总、问题检查
- Aria: 仪表盘、容量、统计、HA事件
- M365: 备份作业/会话、许可证
- CRM客户: 客户列表/详情、资源池、存储映射、VCD VM统计

GID 概念 (极其重要):
- GID 是客户编号 (如 47915、50123、TW48915)，是 OPS-CORE 平台中标识客户的核心ID
- GID 通常嵌入在备份作业名称、备份名称、资源池名称中 (如 "TICKET-47915-Daily-Backup")
- 当用户提到 GID 时，优先查询 CRM 客户详情，可获取该客户的资源池、VM数量、备份作业数等信息
- 客户资源包括: Resource Pools (vCenter资源池)、Storage Mappings (存储卷映射)、VCD VMs、快照VMs、BRR备份

分析数据时请:
- 用 🔴(异常/失败) 🟡(警告) 🟢(正常) 标注状态
- 对备份/快照异常给出处理建议
- 汇总关键数字 (总数/成功率/异常数)
- 用表格呈现结构化数据
- 如果数据中有匹配用户查询的具体记录，优先展示这些匹配项的详细信息
- **极其重要**: 回答时必须使用查询数据中的实际 GID 和客户名称，不要假设或编造。如果 CRM 客户详情中显示的公司名与用户输入不同，请以 CRM 数据为准并说明。
不可逾越的准确性铁律:
- **API 查询返回空/未包含目标 IP 或对象时，必须明确说"未查到相关数据"，绝不编造表格、DNS 记录、CMDB 配置**。
- 任何表格中的数据 (IP/DNS指向/主机名/GID) 必须能在原始查询结果中找到，否则不可输出。
- 如果你不确定数据来源，标注 [推测] 并明确说明是推理而非查证结果。"""


# ═══ Tool-Use 模式 System Prompt (参考 claude-code QueryEngine + AgentTool 模式) ═══

OPSCORE_TOOL_USE_PROMPT = """你是 Z-OPS 运维智能体的 OPS-CORE 数据查询模块。你通过调用工具来查询 OPS-CORE 平台的运维数据，并基于查询结果回答用户问题。

数据源概览:
- BRR 备份: 备份作业/对象/仓库/会话 (按 site 站点组织，site 如 HKHKGBBB、CNSHHCCC 等站点代码)
- CRM 客户: 客户详情/资源池/存储映射 (通过 GID 编号标识，如 47141、46445、TW43727)
- vCenter VM: vCenter 资源池中的虚拟机 (通过资源池名关联，如 TICKET-46445-HKGBBB2)
- VCD: vCloud Director 资源/组织/容量
- CMDB: 主机/服务器信息
- DNS: 域名记录/SPF/DMARC/Zone
- Aria Ops: 仪表盘/容量/告警/HA事件
- M365: 备份作业/许可证
- HA: vSphere HA 高可用重启 VM
- 监控/告警: 实时监控数据和告警信息

工具选择指南:
1. GID 编号/客户名称/业务/资源池查询 → 先调 query_crm_customer
2. 备份作业/站点备份状态/作业状态 → query_backup_jobs
3. 备份VM/还原点数 → query_backup_objects
4. 虚拟机列表/查询VM → query_smartops_vms (从 vCenter 资源池和 VCD 查询)
5. 运维事件/迁移记录/HA事件 → query_smartops_events
6. 快照检查/快照列表 → query_snapshots
7. 主机/服务器/CMDB → query_cmdb_hosts
8. DNS域名记录 → query_dns_records
9. HA高可用VM → query_ha_vms
10. 监控/告警 → query_monitors 或 query_alerts
11. 存储卷/阵列/Nimble/NetApp → query_storage_volumes

重要规则:
- **必须先调用工具获取数据，然后才能回答用户问题。严禁在没有调用任何工具的情况下直接回答。**
- **一旦获取到与用户问题直接相关的数据，立即基于这些数据生成回复，绝对不要继续调用多余工具。** 例如：查存储只需调 query_storage_volumes，拿到数据就直接回答，绝对不要再调 aria_ops/smartops_vms/cmdb_hosts 等无关工具。每多调一轮工具都在浪费 token 和时间，可能导致回复为空。
- 所有引用的数据必须来自工具返回的结果，严禁编造数据
- **极其重要**: 工具返回的所有原始数据必须如实展示，包括 IP 地址、服务器名、域名、URL 等。绝对不要将 IP 替换为占位符 (如 [IP]、IP-xxxx、x.x.x.x)、不要遮蔽任何字段值、不要脱敏。用户是运维人员，必须看到完整的真实数据。
- 用 🔴(异常/失败) 🟡(警告) 🟢(正常) 标注状态
- 表格呈现结构化数据，汇总关键数字。**同一类数据必须放在一个表格中，绝对不要拆分成多个表格。**
- 给出具体的处理建议
- 如果工具返回空结果，直接说明未找到，不要用其他数据替代
- 数据量大时 (如 400+ 条备份仓库)，按站点分组汇总，展示每站点的数量和关键指标，不要逐条列出
- **主机查询规则**: 当工具返回“主机 XXX 上的全部 N 台虚拟机”时，必须将全部 N 台 VM 放在一个完整的表格中展示给用户，不要只展示部分、不要拆分表格、不要筛选子集。
- **备份查询规则 (极其重要)**: 查询备份信息时，最多调用 2 个工具即可回答。例如:
  - 按 IP/主机名查备份 → 调 query_backup_jobs 即可，不要再去调 query_backup_objects/query_backup_sessions/query_backup_repos
  - 按 GID 查备份 → 调 query_backup_jobs + query_backup_objects，拿到数据就直接回答
  - **按 VM 名称查备份保护情况** (该 VM 被哪些备份作业覆盖、作业类型、是否禁用、最近还原点时间) → 调 query_backup_jobs(search=VM名) + query_backup_objects(search=VM名)
  - **用户给出多台 VM 清单要求逐一核查备份状态 (≥3台)** → 调 query_backup_objects_batch(search=所有VM名逗号分隔)，一次查完，绝对不要逐台调 query_backup_objects (会触发多轮 LLM 调用和网关限流)。汇总成【一张】表格: 每行 = VM名 | 备份作业 | 作业类型 | 是否禁用 | 最近还原点；查不到任何备份对象的 VM 必须明确标注 "未纳入任何备份作业 (无备份保护)"，绝不能漏掉任何一台
  - **绝对不要在拿到备份作业信息后还继续调 query_backup_sessions 或 query_backup_repos**，这些工具返回的数据量巨大(数百条会话/仓库)且与用户问题无关
  - 如果第一次搜索无结果，可以尝试缩短关键词再搜一次，但最多重试 1 次

GID 查询最佳实践 (当用户问 "GID XXXX 有什么 VM" 时):
1. 先调 query_crm_customer 获取客户详情和资源池信息
2. 从 CRM 返回的资源池名中提取 GID (如 "TICKET-46445-HKGBBB2" → 用 "TICKET-46445" 搜索)
3. 用资源池 GID 调 query_smartops_vms 获取该资源池下的所有 VM (从 vCenter 资源池和 VCD 查询)
4. 可选: 调 query_backup_jobs 获取备份作业信息
5. 综合所有结果回答用户问题

VCD 资源查询规则 (当用户问 GID/客户的 VCD 资源时):
- query_vcd_resources 工具会自动返回 CRM 客户信息(订单/服务编号)、CPU/内存/存储的分配量与已分配量、存储配置详情
- **重要**: VCD 显示的是资源分配量(总量 vs 已分配)，不是真实使用用量。真实用量只有 vCenter 能看到
- **必须展示资源分配**: CPU 总量 vs 已分配 (GHz)、内存 总量 vs 已分配 (GB)、存储 总量 vs 已分配 (GB/TB)，以及分配率百分比
- **必须展示客户订单**: 服务编号(service_id)、订单编号(order_no) 是客户核心信息，必须在回复中展示
- **必须展示存储配置**: 每个存储配置的总量/已分配/分配率
- 风险描述应使用"客户资源剩余可分配资源不足，如要调整资源可能资源不够"，而非"使用率过高"
- 所有数值数据必须放在表格中，方便导出为 CSV

存储卷查询规则 (当用户问存储/卷/datastore/Nimble/NetApp时):
- **只需调用 query_storage_volumes 一个工具即可**，拿到数据后直接生成回复，绝对不要调用其他工具
- 返回数据包含: 卷名、阵列名(array_name)、阵列IP、容量(size_gb)、已用(used_gb)、状态、CRM映射(datastore名、vCenter IP)
- 支持按站点/GID/卷名搜索
- 对比vCenter与Nimble的卷：直接在回复中分析工具返回的数据即可
不可逾越的准确性铁律:
- **API 查询返回空/未包含目标 IP 或对象时，必须明确说"未查到相关数据"，绝不编造表格、DNS 记录、CMDB 配置**。
- 任何表格中的数据 (IP/DNS指向/主机名/GID) 必须能在原始查询结果中找到，否则不可输出。
- 如果你不确定数据来源，标注 [推测] 并明确说明是推理而非查证结果。"""


class OpscoreAgent(BaseAgent):
    name = "opscore_agent"
    description = "OPS-CORE 平台数据集成 (CMDB/监控/备份/快照/VCD/Aria Ops/M365)"

    def __init__(self):
        super().__init__()
        self.info_api = get_info_client()
        self.mgt_api = get_mgt_client()
        self._register_tools()
        self._register_to_pool()

    def _register_to_pool(self):
        """注册到全局 ToolPool (参考 claude-code AgentTool 跨 Agent 发现)"""
        try:
            from core.tool_pool import get_tool_pool
            pool = get_tool_pool()
            pool.register_agent(
                agent_id=self.name,
                description=self.description,
                capabilities=[
                    "备份查询", "BRR", "CRM客户", "GID", "快照", "VCD",
                    "Smart-Ops", "CMDB", "DNS", "Aria Ops", "M365", "HA",
                    "监控", "告警", "运维事件", "虚拟机", "站点",
                ],
                tools=self._build_tool_defs(),
            )
        except Exception as e:
            logger.debug(f"[opscore_agent] ToolPool 注册异常: {e}")

    def _register_tools(self):
        """将所有 action handler 注册为 ToolDef (参考 claude-code AgentTool)"""
        try:
            from core.tool_schema import ToolParam, ParamType, get_tool_registry
            from core.skill_tool_bridge import agent_tools_from_handlers

            handlers = {
                "cmdb_query": {
                    "description": "查询 CMDB 主机/服务器信息",
                    "handler": self._tool_cmdb_query,
                    "params": [ToolParam(name="search", type=ParamType.STRING, description="搜索关键词")],
                },
                "backup_status": {
                    "description": "查询 BRR 备份作业状态 (按站点聚合, 支持 GID/关键词搜索)",
                    "handler": self._tool_backup_status,
                    "params": [ToolParam(name="search", type=ParamType.STRING, description="GID/站点名/作业名")],
                },
                "backup_objects": {
                    "description": "查询备份对象 (VM 级别, 含还原点数)",
                    "handler": self._tool_backup_objects,
                    "params": [ToolParam(name="search", type=ParamType.STRING, description="VM名称/关键词")],
                },
                "snapshot_check": {
                    "description": "查询虚拟机快照列表和问题检查",
                    "handler": self._tool_snapshot_check,
                    "params": [ToolParam(name="search", type=ParamType.STRING, description="搜索关键词")],
                },
                "smartops_vms": {
                    "description": "查询虚拟机列表 (从 vCenter 资源池和 VCD 查询)",
                    "handler": self._tool_smartops_vms,
                    "params": [ToolParam(name="search", type=ParamType.STRING, description="GID/VM名/资源池名")],
                },
                "smartops_events": {
                    "description": "查询运维事件 (含严重/未确认事件, 支持迁移/HA筛选)",
                    "handler": self._tool_smartops_events,
                    "params": [ToolParam(name="search", type=ParamType.STRING, description="事件关键词")],
                },
                "vcd_resources": {
                    "description": "查询 VCD 云平台资源 (组织/资源池/CPU内存存储分配 + CRM客户订单)",
                    "handler": self._tool_vcd_resources,
                    "params": [ToolParam(name="search", type=ParamType.STRING, description="组织名/站点")],
                },
                "dns_query": {
                    "description": "查询 DNS 记录 (OPS-CORE DNS API, 支持 A/AAAA/CNAME/MX/TXT/NS/SOA/PTR/SRV 及批量域名/IP反向解析)",
                    "handler": self._tool_dns_query,
                    "params": [ToolParam(name="domain", type=ParamType.STRING, description="域名或 IP (支持多行批量)", required=True),
                                ToolParam(name="record_type", type=ParamType.STRING, description="记录类型: A/AAAA/CNAME/MX/TXT/NS/SOA/PTR/SRV (默认 A)"),
                                ToolParam(name="server", type=ParamType.STRING, description="指定 DNS 服务器 (默认 223.5.5.5)")],
                },
                "dns_resolve": {
                    "description": "智能 DNS 解析: 输入 IP 自动反向解析主机名(PTR)，输入域名自动正向解析 IP(A)，支持批量目标",
                    "handler": self._tool_dns_resolve,
                    "params": [ToolParam(name="targets", type=ParamType.STRING, description="IP/域名列表 (逗号/空格/换行分隔)", required=True)],
                },
                "dns_compare": {
                    "description": "多 DNS 服务器对比查询 (同时在多个服务器解析并对比结果是否一致，可校验预期结果)",
                    "handler": self._tool_dns_compare,
                    "params": [ToolParam(name="domain", type=ParamType.STRING, description="域名 (支持多行批量，最多20个)", required=True),
                                ToolParam(name="servers", type=ParamType.STRING, description="DNS 服务器列表 (换行/逗号/空格分隔)"),
                                ToolParam(name="record_type", type=ParamType.STRING, description="记录类型 (默认 A)")],
                },
                "dns_trace": {
                    "description": "DNS 全链路解析追踪 (类似 dig +trace, 从根服务器逐级追踪解析链路)",
                    "handler": self._tool_dns_trace,
                    "params": [ToolParam(name="domain", type=ParamType.STRING, description="域名", required=True),
                                ToolParam(name="record_type", type=ParamType.STRING, description="记录类型 (默认 A)")],
                },
                "ha_query": {
                    "description": "查询 vSphere HA 高可用重启的 VM 列表和事件",
                    "handler": self._tool_ha_query,
                    "params": [ToolParam(name="search", type=ParamType.STRING, description="VM名/站点")],
                },
                "monitor_query": {
                    "description": "查询监控数据",
                    "handler": self._tool_monitor_query,
                    "params": [ToolParam(name="search", type=ParamType.STRING, description="搜索关键词")],
                },
                "alert_query": {
                    "description": "查询告警信息",
                    "handler": self._tool_alert_query,
                    "params": [ToolParam(name="search", type=ParamType.STRING, description="搜索关键词")],
                },
                "aria_ops": {
                    "description": "查询 Aria Ops 仪表盘/容量/统计/HA事件",
                    "handler": self._tool_aria_ops,
                    "params": [ToolParam(name="search", type=ParamType.STRING, description="搜索关键词"),
                                ToolParam(name="type", type=ParamType.STRING, description="类型: dashboard/alerts/capacity/statistics/ha")],
                },
                "m365_status": {
                    "description": "查询 M365 备份作业和许可证状态",
                    "handler": self._tool_m365_status,
                    "params": [],
                },
                "crm_query": {
                    "description": "查询 CRM 客户信息 (GID 编号或客户公司名称搜索, 返回客户详情/资源池/存储映射)",
                    "handler": self._tool_crm_query,
                    "params": [ToolParam(name="gid_or_name", type=ParamType.STRING, description="GID编号(如47141)或客户公司名称")],
                },
            }
            agent_tools_from_handlers(handlers, agent_id="opscore_agent", category="opscore")
            logger.info(f"[opscore_agent] 已注册 {len(handlers)} 个工具到 ToolRegistry")
        except Exception as e:
            logger.warning(f"[opscore_agent] 工具注册失败 (不影响核心功能): {e}")

    # ─── Tool executor wrappers (返回 ToolExecution, 参考 claude-code Tool System) ───

    async def _tool_query(self, endpoint_key: str, label: str, search: str = "",
                          max_items: int = 30) -> dict:
        """通用工具查询包装 — 返回预格式化的可读文本 (参考 claude-code TurnResult)"""
        params = {"page_size": 500}
        if search:
            params["search"] = search
        data = await query_api(endpoint_key, params)
        if isinstance(data, dict) and "error" in data:
            return {"success": False, "error": f"{label}查询失败: {data['error']}"}
        items = self._extract_items(data)
        count = len(items) if isinstance(items, list) else 0

        if count == 0:
            return {"success": True, "text": f"{label}: 无匹配结果" + (f" (关键词: '{search}')" if search else ""),
                    "data": {"count": 0, "items": []}}

        # 格式化为可读文本 (不仅仅返回 count, 让 LLM 能看到数据内容)
        lines = [f"{label}: 共 {count} 条" + (f" (关键词: '{search}')" if search else "")]
        _useful_keys = ['name', 'site', 'status', 'server_url', 'vm_name', 'domain', 'ip',
                        'type', 'severity', 'backup_name', 'job_name', 'platform_name',
                        'creation_time', 'gid', 'company_name', 'title', 'event_type',
                        'is_disabled', 'last_result', 'job_type', 'vi_type', 'restore_points_count',
                        'vcd_url', 'provider_vdc', 'full_name', 'is_enabled', 'vapp_count', 'vm_count', 'vdc_count',
                        'cpu_allocated_hz', 'cpu_used_hz', 'memory_allocated_mb', 'memory_used_mb',
                        'storage_allocated_mb', 'storage_used_mb']
        for item in items[:max_items]:
            parts = []
            for k in _useful_keys:
                v = item.get(k)
                if v is not None and str(v).strip() and str(v) != 'None':
                    parts.append(f"{k}={str(v)[:60]}")
            if parts:
                lines.append("  " + " | ".join(parts[:8]))
        if count > max_items:
            lines.append(f"  ... (还有 {count - max_items} 条未显示)")

        return {"success": True, "text": "\n".join(lines)[:4000],
                "data": {"count": count, "items": items[:max_items]}}

    async def _tool_cmdb_query(self, params=None, context=None):
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        r = await self._tool_query("cmdb_info", "CMDB", search)
        return ToolExecution(tool_name="cmdb_query", success=r.get("success", False), output=r.get("text", ""), data=r.get("data", {}), error=r.get("error", ""))

    async def _tool_backup_status(self, params=None, context=None):
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        r = await self._tool_query("brr_backups", "备份", search)
        return ToolExecution(tool_name="backup_status", success=r.get("success", False), output=r.get("text", ""), data=r.get("data", {}), error=r.get("error", ""))

    async def _tool_backup_objects(self, params=None, context=None):
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        r = await self._tool_query("brr_backup_objects", "备份对象", search)
        return ToolExecution(tool_name="backup_objects", success=r.get("success", False), output=r.get("text", ""), data=r.get("data", {}), error=r.get("error", ""))

    async def _tool_backup_objects_batch(self, params=None, context=None):
        """批量查询多台 VM 的备份对象 — 一次调用查多台，避免逐台调用产生大量 LLM 轮次

        用户给出 VM 清单 (逗号/空格/换行分隔) 时，LLM 只调一次本工具，
        内部并发查所有 VM，返回聚合结果。把 22 台 VM 的查询从 5+ 轮 LLM 循环
        降到 1-2 轮，大幅降低 token 消耗和 OneAPI 网关 429 限流概率。
        """
        import asyncio
        import re as _re
        from core.tool_schema import ToolExecution
        raw = (params or {}).get("search", "") or (params or {}).get("vm_names", "")
        # 拆分 VM 名: 逗号/顿号/分号/换行/空格分隔
        names = [n.strip().upper() for n in _re.split(r'[,，;；\s\n]+', raw) if n.strip()]
        # 去重保序 + 过滤明显非 VM 名的杂质 (如"查询""状态"等中文词)
        seen, vms = set(), []
        for n in names:
            if n in seen or not _re.match(r'^[A-Z]{2,}[A-Z0-9]{2,}$', n):
                continue
            seen.add(n)
            vms.append(n)
        if not vms:
            return ToolExecution(tool_name="query_backup_objects_batch", success=False,
                                 error="未识别出有效的 VM 名称列表")

        # 并发查询每台 VM 的备份对象
        results = await asyncio.gather(
            *[query_api("brr_backup_objects", {"search": vm}) for vm in vms],
            return_exceptions=True
        )
        lines = [f"批量备份对象查询: {len(vms)} 台 VM"]
        for vm, res in zip(vms, results):
            if isinstance(res, Exception):
                lines.append(f"\n### {vm}\n查询异常: {res}")
                continue
            items = self._extract_items(res)
            if not items:
                lines.append(f"\n### {vm}\n无任何备份对象 → 未纳入任何备份作业 (无备份保护)")
                continue
            lines.append(f"\n### {vm} (共 {len(items)} 条备份对象)")
            for it in items[:12]:
                bk = str(it.get('backup_name', '-'))[:70]
                jb = str(it.get('job_name', '-'))[:70]
                lines.append(f"  - 备份:{bk} | 作业:{jb} "
                             f"| 类型:{it.get('job_type','-')} | 禁用:{'是' if it.get('job_disabled') else '否'} "
                             f"| 还原点:{it.get('restore_points_count', it.get('restore_points_list', '-'))}")
        return ToolExecution(tool_name="query_backup_objects_batch", success=True,
                             output="\n".join(lines)[:14000],
                             data={"vm_count": len(vms), "queried": vms})

    async def _tool_snapshot_check(self, params=None, context=None):
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        r = await self._tool_query("snapshot_list", "快照", search)
        return ToolExecution(tool_name="snapshot_check", success=r.get("success", False), output=r.get("text", ""), data=r.get("data", {}), error=r.get("error", ""))

    # ── 主机查询工具 ──
    # 模式: 站点设备代码 (字母+数字+可选后缀), 如 ESXI-02, GDS3103BKP
    # 短名解析/全量缓存/服务端过滤逻辑统一在 opscore_api.query_vms_on_esxi_host

    async def _expand_site_search(self, search: str) -> tuple:
        """主机 VM 查询: 将 'ESXI-02' 视为主机名，查找该主机上运行的所有 VM
        数据链路 (opscore_api.query_vms_on_esxi_host):
          MCP 全量缓存建 esxi_host 索引 → 短名解析完整宿主机 →
          REST host= 服务端精确过滤 (~0.2s, 替代旧的全量分页拉取 20s+/页) →
          snapshot_vms 站点前缀合并 (命中率达标即早停, 不做全量兑底)
        """
        info = await query_vms_on_esxi_host(search)
        if not info or not info["vms"]:
            return None, None
        filtered = info["vms"]
        host_display = ", ".join(info["esxi_hosts"][:2])
        
        # 生成格式化文本
        lines = [f"🖥️ 主机 {search.upper()} ({host_display}) 上的全部 {len(filtered)} 台虚拟机:"]
        _useful_keys = ['name', 'site', 'resource_pool', 'power_state', 'cpu_count', 'memory_mb',
                       'guest_os', 'host_name', 'guest_ip', 'customer_gid', 'biz_type',
                       'cluster', 'vcenter_server', 'connection_state']
        for i, item in enumerate(sorted(filtered, key=lambda x: x.get('name', '')), 1):
            parts = [f"#{i}"]
            for k in _useful_keys:
                v = item.get(k)
                if v is not None and str(v).strip() and str(v) != 'None' and v != []:
                    val = str(v)[:50]
                    parts.append(f"{k}={val}")
            lines.append("  " + " | ".join(parts))
        return filtered, "\n".join(lines)[:12000]

    async def _tool_smartops_vms(self, params=None, context=None):
        """VM 查询 — 从 vCenter/smartops_vms 和 VCD 查询虚拟机资源
        支持主机查询: 'ESXI-02' → 按 host 字段查找该主机上的所有 VM"""
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        
        # 1. 优先尝试主机查询: 检测是否为站点设备代码 (如 ESXI-02)
        host_items, host_text = None, None
        if search:
            host_items, host_text = await self._expand_site_search(search)
        
        # 2. 查 vCenter 资源池 VM (snapshot_vms 端点)
        r1 = await self._tool_query("snapshot_vms", "vCenter VM", search)
        items1 = r1.get("data", {}).get("items", [])
        
        # 如果主机查询找到了更多 VM，优先使用主机结果
        if host_items and len(host_items) > len(items1):
            logger.info(f"[opscore_agent] 主机查询优先: host={len(host_items)} > name={len(items1)}")
            r1 = {"text": host_text, "data": {"count": len(host_items), "items": host_items}}
            items1 = host_items
        
        # 3. 查 VCD 资源
        r2 = await self._tool_query("vcd_resources", "VCD 资源", search)
        items2 = r2.get("data", {}).get("items", [])
        
        count1 = len(items1)
        count2 = len(items2)
        if count1 == 0 and count2 == 0:
            return ToolExecution(tool_name="smartops_vms", success=True,
                                output=f"vCenter VM 和 VCD 资源: 无匹配结果 (关键词: '{search}')",
                                data={"count": 0, "items": []})
        # 合并文本
        texts = []
        if count1 > 0:
            texts.append(r1.get("text", ""))
        if count2 > 0:
            texts.append(r2.get("text", ""))
        combined_text = "\n\n".join(texts)
        combined_items = items1 + items2
        # 主机查询时不截断，确保 LLM 看到全部数据
        max_len = 12000 if host_items else 6000
        return ToolExecution(tool_name="smartops_vms", success=True,
                            output=combined_text[:max_len],
                            data={"count": count1 + count2, "items": combined_items[:60]})

    async def _tool_smartops_events(self, params=None, context=None):
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        r = await self._tool_query("smartops_events", "运维事件", search)
        return ToolExecution(tool_name="smartops_events", success=r.get("success", False), output=r.get("text", ""), data=r.get("data", {}), error=r.get("error", ""))

    async def _tool_vcd_resources(self, params=None, context=None):
        """VCD 资源查询 — 查询 vcd_resources(容量用量) + vcd_organizations(组织) + CRM客户(订单)
        自动检测 GID 并关联 CRM 客户信息，输出完整的资源用量 vs 总量"""
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")

        # ── 1. 查询 VCD 资源池 (含 CPU/内存/存储 用量) ──
        res_params = {"page_size": 500}
        if search:
            res_params["search"] = search
        res_data = await query_api("vcd_resources", res_params)
        resources = res_data if isinstance(res_data, list) else (res_data.get("results", []) if isinstance(res_data, dict) else [])

        # ── 2. 查询 VCD 组织 ──
        org_params = {"page_size": 500}
        if search:
            org_params["search"] = search
        org_data = await query_api("vcd_organizations", org_params)
        orgs = org_data if isinstance(org_data, list) else (org_data.get("results", []) if isinstance(org_data, dict) else [])

        if not resources and not orgs:
            return ToolExecution(tool_name="vcd_resources", success=True,
                                output=f"VCD 资源: 无匹配结果" + (f" (关键词: '{search}')" if search else ""),
                                data={"count": 0, "items": []})

        lines = []

        # ── 3. 自动检测 GID -> 拉取 CRM 客户信息 (订单/服务/资源池) ──
        crm_data = None
        gid_match = re.search(r'GID?(\d{4,6})', search, re.IGNORECASE) if search else None
        if gid_match:
            raw_gid = gid_match.group(1)
            try:
                crm_data = await query_crm_customer_resources(raw_gid)
                if crm_data and "error" not in crm_data:
                    cust = crm_data.get("customer", {})
                    res_summary = crm_data.get("resources", {})
                    lines.append("\u2550\u2550\u2550 CRM \u5ba2\u6237\u4fe1\u606f \u2550\u2550\u2550")
                    lines.append(f"\u516c\u53f8\u540d\u79f0: {cust.get('company_name', '-')}")
                    lines.append(f"\u4e3bGID: {cust.get('gid', '-')}")
                    lines.append(f"\u670d\u52a1\u7f16\u53f7(service_id): {cust.get('service_id', '-')}")
                    lines.append(f"\u8ba2\u5355\u7f16\u53f7(order_no): {cust.get('order_no', '-')}")
                    lines.append(f"\u5f53\u524d\u670d\u52a1(current_service): {cust.get('current_service', '-')}")
                    lines.append(f"\u5173\u8054GID: {', '.join(crm_data.get('related_gids', []))}")
                    lines.append(f"\u8d44\u6e90\u6c60\u6570\u91cf: {res_summary.get('resource_pools_count', 0)}")
                    lines.append(f"\u5b58\u50a8\u6620\u5c04\u6570\u91cf: {res_summary.get('storage_mapping_count', 0)}")
                    lines.append(f"vCenter VM\u6570\u91cf: {res_summary.get('snapshot_vm_count', 0)}")
                    lines.append(f"VCD VM\u6570\u91cf: {res_summary.get('vcd_vm_count', 0)}")
                    lines.append(f"BRR\u5907\u4efd\u4f5c\u4e1a\u6570: {res_summary.get('brr_job_count', 0)}")
                    lines.append(f"BRR\u5907\u4efdVM\u6570: {res_summary.get('brr_vm_count', 0)}")
                    # \u8d44\u6e90\u6c60\u5217\u8868
                    rps = crm_data.get("resource_pools", [])
                    if rps:
                        lines.append("")
                        lines.append(f"\u2500\u2500 \u8d44\u6e90\u6c60\u5217\u8868 ({len(rps)}\u4e2a) \u2500\u2500")
                        for rp in rps:
                            lines.append(f"  {rp.get('pool_name', '-')} | vCenter:{rp.get('vcenter', '-')}")
                    # \u5b58\u50a8\u6620\u5c04\u6c47\u603b
                    sms = crm_data.get("storage_mappings", [])
                    if sms:
                        vol_summary = {}
                        for sm in sms:
                            vol = sm.get("storage_volume_name") or sm.get("datastore_name") or "unknown"
                            if vol not in vol_summary:
                                vol_summary[vol] = {"count": 0, "cpu": 0, "mem_mb": 0,
                                                     "array": sm.get("storage_array_name", ""),
                                                     "vms": []}
                            vol_summary[vol]["count"] += 1
                            vol_summary[vol]["cpu"] += (sm.get("cpu_count") or 0)
                            vol_summary[vol]["mem_mb"] += (sm.get("memory_mb") or 0)
                            vol_summary[vol]["vms"].append(sm.get("vm_name", ""))
                        lines.append("")
                        lines.append(f"\u2500\u2500 \u5b58\u50a8\u5377\u6620\u5c04\u6c47\u603b ({len(sms)}\u53f0VM, {len(vol_summary)}\u4e2a\u5377) \u2500\u2500")
                        for vol, info in vol_summary.items():
                            mem_gb = info['mem_mb'] / 1024
                            lines.append(f"  {vol} | \u9635\u5217:{info['array']} | VM:{info['count']}\u53f0 | CPU:{info['cpu']}\u6838 | \u5185\u5b58:{mem_gb:.0f}GB")
                    lines.append("")
            except Exception as e:
                logger.warning(f"[opscore_agent] CRM \u5ba2\u6237\u67e5\u8be2\u5931\u8d25 (GID={raw_gid}): {e}")

        # \u2500\u2500 4. VCD \u8d44\u6e90\u6c60 \u2014 \u5bb9\u91cf\u7528\u91cf\u8be6\u60c5 \u2500\u2500
        if resources:
            lines.append(f"═══ VCD 资源池分配情况 ({len(resources)}个) ═══")
            for res in resources:
                ovdc = res.get("ovdc_name") or res.get("name", "-")
                org_name = res.get("organization", "-")
                site = res.get("site", "-")
                vcd_display = res.get("provider_vdc") or res.get("vcd_url", "-")
                cpu_alloc = float(res.get("cpu_allocated") or 0)
                cpu_used = float(res.get("cpu_used") or 0)
                mem_alloc = float(res.get("memory_allocated") or 0)
                mem_used = float(res.get("memory_used") or 0)
                stor_alloc = float(res.get("storage_allocated") or 0)
                stor_used = float(res.get("storage_used") or 0)
                cpu_pct = f"{cpu_used/cpu_alloc*100:.1f}%" if cpu_alloc > 0 else "N/A"
                mem_pct = f"{mem_used/mem_alloc*100:.1f}%" if mem_alloc > 0 else "N/A"
                stor_pct = f"{stor_used/stor_alloc*100:.1f}%" if stor_alloc > 0 else "N/A"
                if stor_alloc > 1024:
                    stor_alloc_str = f"{stor_alloc/1024:.1f}TB"
                    stor_used_str = f"{stor_used/1024:.1f}TB"
                else:
                    stor_alloc_str = f"{stor_alloc:.0f}GB"
                    stor_used_str = f"{stor_used:.0f}GB"
                lines.append("")
                lines.append(f"  OvDC: {ovdc}")
                lines.append(f"  组织: {org_name} | 站点: {site} | VCD: {vcd_display}")
                lines.append(f"  CPU:   总量 {cpu_alloc:.0f} GHz | 已分配 {cpu_used:.0f} GHz | 分配率 {cpu_pct}")
                lines.append(f"  内存:  总量 {mem_alloc:.0f} GB | 已分配 {mem_used:.0f} GB | 分配率 {mem_pct}")
                lines.append(f"  存储:  总量 {stor_alloc_str} | 已分配 {stor_used_str} | 分配率 {stor_pct}")
                profiles = res.get("storage_profiles") or []
                if profiles:
                    lines.append(f"  ── 存储配置 ({len(profiles)}个) ──")
                    for sp in profiles:
                        sp_name = sp.get("name", "-")
                        sp_alloc = float(sp.get("storage_allocated") or 0)
                        sp_used = float(sp.get("storage_used") or 0)
                        sp_enable = sp.get("storage_enable", True)
                        sp_default = sp.get("storage_default", False)
                        if sp_alloc > 1024:
                            sp_alloc_s = f"{sp_alloc/1024:.1f}TB"
                            sp_used_s = f"{sp_used/1024:.1f}TB"
                        else:
                            sp_alloc_s = f"{sp_alloc:.0f}GB"
                            sp_used_s = f"{sp_used:.0f}GB"
                        sp_pct = f"{sp_used/sp_alloc*100:.1f}%" if sp_alloc > 0 else "N/A"
                        flags = []
                        if sp_default:
                            flags.append("默认")
                        if not sp_enable:
                            flags.append("已禁用")
                        flag_str = f" [{', '.join(flags)}]" if flags else ""
                        lines.append(f"    {sp_name}: 总量{sp_alloc_s} / 已分配{sp_used_s} ({sp_pct}){flag_str}")

        # \u2500\u2500 5. VCD \u7ec4\u7ec7\u6982\u89c8 \u2500\u2500
        if orgs:
            lines.append("")
            lines.append(f"\u2550\u2550\u2550 VCD \u7ec4\u7ec7\u6982\u89c8 ({len(orgs)}\u4e2a) \u2550\u2550\u2550")
            for org in orgs:
                name = org.get("name", "-")
                site = org.get("site", "-")
                status = org.get("status", "-")
                enabled = "\u2705" if org.get("is_enabled") else "\u274c"
                vdc_count = org.get("vdc_count", 0)
                vapp_count = org.get("vapp_count", 0)
                vm_count = org.get("vm_count", 0)
                vcd_display = org.get("provider_vdc") or org.get("vcd_url", "-")
                lines.append(f"  {name} | 站点:{site} | 状态:{status} | 启用:{enabled} | vDC:{vdc_count} | vApp:{vapp_count} | VM:{vm_count} | VCD:{vcd_display}")

        combined_text = "\n".join(lines)
        combined_items = resources + orgs
        return ToolExecution(tool_name="vcd_resources", success=True,
                            output=combined_text[:12000],
                            data={"count": len(resources) + len(orgs),
                                  "items": combined_items[:60],
                                  "resources": resources,
                                  "organizations": orgs,
                                  "crm": crm_data})

    async def _tool_storage_query(self, params=None, context=None):
        """存储卷查询工具 — 查询存储卷及CRM映射 (含Nimble/NetApp阵列信息)"""
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        try:
            # API search 参数无效，获取全量数据后本地过滤
            data = await query_api("crm_storage_mappings", {"page_size": 2000})
            all_volumes = data.get("results", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            
            if not all_volumes:
                return ToolExecution(tool_name="storage_query", success=True,
                                    output="存储卷: 无数据",
                                    data={"count": 0, "items": []})
            
            # 本地过滤：按卷名、阵列名、datastore名匹配
            volumes = all_volumes
            if search:
                search_upper = search.upper()
                filtered = []
                for vol in all_volumes:
                    name = vol.get("name", "").upper()
                    array = vol.get("array_name", "").upper()
                    mappings = vol.get("crm_mappings", [])
                    # 检查卷名或阵列名
                    if search_upper in name or search_upper in array:
                        filtered.append(vol)
                        continue
                    # 检查 datastore 名
                    match = False
                    for m in mappings:
                        ds = m.get("datastore_name", "").upper()
                        if search_upper in ds:
                            match = True
                            break
                    if match:
                        filtered.append(vol)
                volumes = filtered
            
            if not volumes:
                return ToolExecution(tool_name="storage_query", success=True,
                                    output=f"存储卷: 无匹配结果 (关键词: '{search}')",
                                    data={"count": 0, "items": []})
            
            # 表格格式输出，减少 token
            lines = [f"存储卷信息 (共{len(volumes)}个)"]
            lines.append("| 卷名 | 阵列 | 容量GB | 已用GB | 使用率 | Datastore | vCenter | GID | VM数 |")
            lines.append("|:---|:---|---:|---:|---:|:---|:---|:---|---:|")
            for vol in volumes[:100]:
                name = vol.get("name", "-")[:40]
                array_name = vol.get("array_name", "-")
                size_gb = vol.get("size_gb", 0)
                used_gb = vol.get("used_gb", 0)
                usage_pct = f"{used_gb/size_gb*100:.0f}%" if size_gb > 0 else "N/A"
                crm_mappings = vol.get("crm_mappings", [])
                datastores = set(m.get("datastore_name", "") for m in crm_mappings if m.get("datastore_name"))
                vcenters = set(m.get("vcenter_ip", "") for m in crm_mappings if m.get("vcenter_ip"))
                gids = set(m.get("gid", "") for m in crm_mappings if m.get("gid"))
                ds_str = ",".join(sorted(datastores)[:3])[:30]
                vc_str = ",".join(sorted(vcenters)[:2])
                gid_str = ",".join(sorted(gids)[:3])
                lines.append(f"| {name} | {array_name} | {size_gb} | {used_gb} | {usage_pct} | {ds_str} | {vc_str} | {gid_str} | {len(crm_mappings)} |")
            combined_text = "\n".join(lines)
            return ToolExecution(tool_name="storage_query", success=True,
                                output=combined_text[:8000],
                                data={"count": len(volumes), "items": volumes[:100]})
        except Exception as e:
            return ToolExecution(tool_name="storage_query", success=False, error=str(e))


    async def _tool_dns_query(self, params=None, context=None):
        """DNS 查询工具 — 调用 OPS-CORE DNS API (新接口, 参考 /dns/api-doc)"""
        from core.tool_schema import ToolExecution
        domain = (params or {}).get("domain", "")
        record_type = ((params or {}).get("record_type") or "A").upper()
        server = (params or {}).get("server", "")
        if not domain:
            return ToolExecution(tool_name="dns_query", success=False, error="缺少 domain 参数")
        try:
            from agents.dns_api import get_dns_client, RECORD_TYPES
            if record_type not in RECORD_TYPES:
                record_type = "A"
            data = await asyncio.to_thread(get_dns_client().query, domain, record_type, server)
            if data.get("success") is False:
                return ToolExecution(tool_name="dns_query", success=False, error=str(data.get("error", "查询失败")))
            results = data.get("successful_results", [])
            errs = data.get("error_results", [])
            lines = [f"DNS 查询 ({record_type}): {len(results)} 条结果" + (f" (服务器 {data.get('server')})" if data.get('server') else "")]
            for r in results[:30]:
                lines.append(f"- {r.get('domain', '?')} → {r.get('result', '')} (TTL {r.get('ttl', '-')})")
            if errs:
                lines.append(f"失败 {len(errs)} 项: " + "; ".join(str(e) for e in errs[:5]))
            return ToolExecution(tool_name="dns_query", success=True, output="\n".join(lines)[:3000], data=data)
        except Exception as e:
            return ToolExecution(tool_name="dns_query", success=False, error=str(e))

    async def _tool_dns_resolve(self, params=None, context=None):
        """智能 DNS 解析工具 — IP 反向解析 / 域名正向解析，支持批量"""
        from core.tool_schema import ToolExecution
        raw = (params or {}).get("targets", "")
        if not raw:
            return ToolExecution(tool_name="dns_resolve", success=False, error="缺少 targets 参数")
        try:
            from agents.dns_api import extract_targets, resolve_targets, format_resolve_report
            ips, domains = extract_targets(raw, max_items=15)
            if not ips and not domains:
                return ToolExecution(tool_name="dns_resolve", success=False, error="未在输入中识别到 IP 或域名")
            result = await resolve_targets(ips, domains)
            report = format_resolve_report(result)
            if not report:
                return ToolExecution(tool_name="dns_resolve", success=False, error="解析失败 (API 不可达或未配置凭据)")
            return ToolExecution(tool_name="dns_resolve", success=True, output=report[:3000], data=result)
        except Exception as e:
            return ToolExecution(tool_name="dns_resolve", success=False, error=str(e))

    async def _tool_dns_compare(self, params=None, context=None):
        """多 DNS 服务器对比查询工具"""
        from core.tool_schema import ToolExecution
        domain = (params or {}).get("domain", "")
        servers = (params or {}).get("servers", "")
        record_type = ((params or {}).get("record_type") or "A").upper()
        if not domain:
            return ToolExecution(tool_name="dns_compare", success=False, error="缺少 domain 参数")
        try:
            from agents.dns_api import get_dns_client
            data = await asyncio.to_thread(get_dns_client().compare, domain, record_type, servers)
            if data.get("success") is False:
                return ToolExecution(tool_name="dns_compare", success=False, error=str(data.get("error", "对比失败")))
            text = json.dumps(data, ensure_ascii=False, indent=1)
            return ToolExecution(tool_name="dns_compare", success=True, output=text[:3000], data=data)
        except Exception as e:
            return ToolExecution(tool_name="dns_compare", success=False, error=str(e))

    async def _tool_dns_trace(self, params=None, context=None):
        """DNS 全链路解析追踪工具 (dig +trace)"""
        from core.tool_schema import ToolExecution
        domain = (params or {}).get("domain", "")
        record_type = ((params or {}).get("record_type") or "A").upper()
        if not domain:
            return ToolExecution(tool_name="dns_trace", success=False, error="缺少 domain 参数")
        try:
            from agents.dns_api import get_dns_client
            data = await asyncio.to_thread(get_dns_client().trace, domain, record_type)
            if data.get("success") is False:
                return ToolExecution(tool_name="dns_trace", success=False, error=str(data.get("error", "追踪失败")))
            text = json.dumps(data, ensure_ascii=False, indent=1)
            return ToolExecution(tool_name="dns_trace", success=True, output=text[:3000], data=data)
        except Exception as e:
            return ToolExecution(tool_name="dns_trace", success=False, error=str(e))

    async def _tool_ha_query(self, params=None, context=None):
        """HA 查询工具 — 使用新版 HA Reports API"""
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        site = (params or {}).get("site", "")
        try:
            ha_client = get_ha_client()
            # 优先使用新版 Reports API
            if site:
                data = await ha_client.get_ha_reports(limit=5, site=site)
            elif search:
                data = await ha_client.get_ha_reports(limit=10)
            else:
                data = await ha_client.get_ha_monitor_dashboard()
            if isinstance(data, dict) and "error" in data:
                return ToolExecution(tool_name="ha_query", success=False, error=data["error"])
            return ToolExecution(tool_name="ha_query", success=True, output=f"HA Reports: {json.dumps(data, ensure_ascii=False)[:2000]}", data=data if isinstance(data, dict) else {"results": data})
        except Exception as e:
            return ToolExecution(tool_name="ha_query", success=False, error=str(e))

    async def _tool_monitor_query(self, params=None, context=None):
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        r = await self._tool_query("monitor_info", "监控", search)
        return ToolExecution(tool_name="monitor_query", success=r.get("success", False), output=r.get("text", ""), data=r.get("data", {}), error=r.get("error", ""))

    async def _tool_alert_query(self, params=None, context=None):
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        r = await self._tool_query("alarm_info", "告警", search)
        return ToolExecution(tool_name="alert_query", success=r.get("success", False), output=r.get("text", ""), data=r.get("data", {}), error=r.get("error", ""))

    async def _tool_aria_ops(self, params=None, context=None):
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        qtype = (params or {}).get("type", "dashboard")
        type_map = {"alerts": "aria_logs_alerts", "capacity": "aria_ops_capacity", "statistics": "aria_ops_statistics", "ha": "aria_ha_events", "dashboard": "aria_dashboard"}
        ep = type_map.get(qtype, "aria_dashboard")
        r = await self._tool_query(ep, f"Aria {qtype}", search)
        return ToolExecution(tool_name="aria_ops", success=r.get("success", False), output=r.get("text", ""), data=r.get("data", {}), error=r.get("error", ""))

    async def _tool_m365_status(self, params=None, context=None):
        from core.tool_schema import ToolExecution
        r = await self._tool_query("m365_dashboard", "M365")
        return ToolExecution(tool_name="m365_status", success=r.get("success", False), output=r.get("text", ""), data=r.get("data", {}), error=r.get("error", ""))

    # ─── 增强版 Tool 函数 (参考 claude-code Tool 自包含执行逻辑) ───

    async def _tool_backup_enhanced(self, params=None, context=None):
        """增强版备份查询 — 预聚合 by site, 支持 GID 搜索"""
        import re
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        # GID 归一化
        gid_search = ""
        if search:
            gid_match = re.match(r'^(GID[A-Z]{0,2}?\d+|\d{5,6})$', search.strip(), re.IGNORECASE)
            if gid_match:
                raw = gid_match.group(1)
                gid_search = raw if raw.upper().startswith("GID") else f"GID{raw}"
                search = gid_search
        # 并发查询
        tasks = [
            query_api("brr_backups", {"search": search, "page_size": 300} if search else {"page_size": 300}),
            query_api("brr_jobs", {"search": search, "page_size": 300} if search else {"page_size": 300}),
        ]
        if gid_search:
            tasks.append(query_api("brr_gid_jobs", {"search": gid_search, "page_size": 100}))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        backups = self._extract_items(results[0]) if not isinstance(results[0], Exception) else []
        jobs = self._extract_items(results[1]) if not isinstance(results[1], Exception) else []
        gid_jobs = []
        if len(results) > 2 and not isinstance(results[2], Exception):
            gd = results[2]
            gid_jobs = gd.get("backups", []) if isinstance(gd, dict) and "backups" in gd else self._extract_items(gd)
        if not backups and not jobs and not gid_jobs:
            return ToolExecution(tool_name="query_backup_jobs", success=True,
                                output=f"未找到与 '{search}' 相关的备份作业")
        # 预聚合 by site
        site_agg: Dict[str, Dict] = {}
        for b in backups:
            s = b.get("site", "unknown")
            if s not in site_agg:
                site_agg[s] = {"bk": 0, "jb": 0, "dis": 0, "svrs": set(), "names": []}
            site_agg[s]["bk"] += 1
            if b.get("server_url"): site_agg[s]["svrs"].add(b["server_url"])
            site_agg[s]["names"].append(b.get("backup_name", ""))
        for j in jobs:
            s = j.get("site", "unknown")
            if s not in site_agg:
                site_agg[s] = {"bk": 0, "jb": 0, "dis": 0, "svrs": set(), "names": []}
            site_agg[s]["jb"] += 1
            if j.get("is_disabled"): site_agg[s]["dis"] += 1
            if j.get("server_url"): site_agg[s]["svrs"].add(j["server_url"])
        lines = [f"备份查询 (关键词:'{search}', {len(backups)}备份, {len(jobs)}作业, {len(site_agg)}站点):"]
        lines.append("站点 | 备份数 | 作业数 | 禁用 | Veeam服务器")
        for st in sorted(site_agg, key=lambda x: site_agg[x]["bk"], reverse=True):
            a = site_agg[st]
            lines.append(f"{st} | {a['bk']} | {a['jb']} | {a['dis']} | {','.join(sorted(a['svrs']))[:40]}")
        if search and backups:
            lines.append(f"\n匹配备份详情 (前15条):")
            for b in backups[:15]:
                lines.append(f"  {b.get('site','-')}/{b.get('backup_name','-')} | 作业:{b.get('job_name','-')} | 平台:{b.get('platform_name','-')} | 时间:{(b.get('creation_time',''))[:19]}")
        if search and jobs:
            lines.append(f"\n匹配作业详情 (前15条):")
            for j in jobs[:15]:
                lines.append(f"  {j.get('site','-')}/{j.get('name','-')} | 类型:{j.get('job_type','-')} | 结果:{j.get('last_result','-')} | 禁用:{'是' if j.get('is_disabled') else '否'}")
        if gid_jobs:
            lines.append(f"\nGID作业 ({len(gid_jobs)}条):")
            for g in gid_jobs[:10]:
                lines.append(f"  {g.get('site','-')}/{g.get('name','-')} | 类型:{g.get('job_type','-')} | GID:{g.get('gid','-')}")
        return ToolExecution(tool_name="query_backup_jobs", success=True, output="\n".join(lines)[:4000],
                             data={"backup_count": len(backups), "job_count": len(jobs), "site_count": len(site_agg)})

    async def _tool_crm_query(self, params=None, context=None):
        """CRM 客户查询 — 支持 GID 或客户名搜索"""
        from core.tool_schema import ToolExecution
        import re
        search = (params or {}).get("gid_or_name", "") or (params or {}).get("search", "")
        if not search:
            return ToolExecution(tool_name="query_crm_customer", success=False, error="缺少搜索参数")
        # 判断是否是 GID
        is_gid = bool(re.match(r'^(GID[A-Z]{0,2}?\d+|\d{5,6})$', search.strip(), re.IGNORECASE))
        if is_gid:
            # 直接用原始 GID (不加 GID 前缀) 查询 CRM
            gid = search.strip()
            raw_gid = re.sub(r'^(?:GID|gid)', '', gid, flags=re.IGNORECASE).strip()
            crm_data = await query_crm_customer_resources(raw_gid)
            if isinstance(crm_data, dict) and "error" in crm_data:
                return ToolExecution(tool_name="query_crm_customer", success=False, error=crm_data["error"])
            customer = crm_data.get("customer", {})
            lines = [f"CRM客户详情 (GID {gid}):"]
            lines.append(f"  公司: {customer.get('company_name', '-')}")
            lines.append(f"  主GID: {customer.get('gid', '-')}")
            lines.append(f"  关联GID: {', '.join(crm_data.get('related_gids', []))}")
            rps = crm_data.get("resource_pools", [])
            if rps:
                lines.append(f"  资源池 ({len(rps)}个):")
                for rp in rps[:10]:
                    pool_name = rp.get('pool_name', rp.get('name', '-'))
                    lines.append(f"    {pool_name} | vCenter:{rp.get('vcenter','-')} | 站点:{rp.get('site','-')}")
            sms = crm_data.get("storage_mappings", [])
            if sms:
                lines.append(f"  存储映射 ({len(sms)}个):")
                for sm in sms[:5]:
                    lines.append(f"    {sm.get('name','-')} | {sm.get('storage_type','-')}")
            return ToolExecution(tool_name="query_crm_customer", success=True, output="\n".join(lines)[:3000],
                                data=crm_data)
        else:
            # 按客户名搜索
            crm_data = await query_crm_customers(search=search, page_size=10)
            if isinstance(crm_data, dict) and "error" in crm_data:
                return ToolExecution(tool_name="query_crm_customer", success=False, error=crm_data["error"])
            results = crm_data.get("results", [])
            if not results:
                return ToolExecution(tool_name="query_crm_customer", success=True,
                                    output=f"未找到与 '{search}' 匹配的客户")
            lines = [f"CRM客户搜索 '{search}' ({len(results)}条):"]
            for r in results[:10]:
                lines.append(f"  GID {r.get('gid','-')}: {r.get('company_name','-')} | 资源池:{r.get('resource_pools_count',0)} | VM:{r.get('vm_count',0)}")
            return ToolExecution(tool_name="query_crm_customer", success=True, output="\n".join(lines)[:2000],
                                data={"results": results})

    async def _tool_backup_sessions_fn(self, params=None, context=None):
        """备份会话查询"""
        from core.tool_schema import ToolExecution
        search = (params or {}).get("search", "")
        r = await self._tool_query("brr_sessions", "备份会话", search)
        return ToolExecution(tool_name="query_backup_sessions", success=r.get("success", False),
                             output=r.get("text", ""), data=r.get("data", {}), error=r.get("error", ""))

    async def _tool_backup_repos_fn(self, params=None, context=None):
        """备份仓库查询"""
        from core.tool_schema import ToolExecution
        r = await self._tool_query("brr_repositories", "备份仓库")
        return ToolExecution(tool_name="query_backup_repos", success=r.get("success", False),
                             output=r.get("text", ""), data=r.get("data", {}), error=r.get("error", ""))

    # ─── Tool-Use 模式基础设施 (参考 claude-code QueryEngine + AgentTool) ───

    def _build_tool_defs(self) -> list:
        """构建 ToolDef 列表, 用于 LLM function calling (参考 claude-code tools.ts)"""
        from core.tool_schema import ToolDef, ToolInputSchema, ToolParam, ParamType
        _s = lambda desc: ToolInputSchema(params=[ToolParam(name="search", type=ParamType.STRING, description=desc)])
        return [
            ToolDef(name="query_backup_jobs",
                    description="查询 BRR 备份作业状态, 支持按 GID/站点代码/作业名搜索, 返回按站点聚合的备份概览",
                    input_schema=_s("搜索关键词: GID(如47141), 站点(如HKHKGBBB), 作业名"),
                    execute_fn=self._tool_backup_enhanced),
            ToolDef(name="query_backup_objects",
                    description="查询备份对象(VM级别), 含还原点数量, 支持按VM名/站点搜索",
                    input_schema=_s("VM名称/站点代码/关键词"),
                    execute_fn=self._tool_backup_objects),
            ToolDef(name="query_backup_objects_batch",
                    description="批量查询多台VM的备份对象，一次查完整个VM清单(逗号/空格/换行分隔)。用户给出≥3台VM清单要求逐一核查备份状态时，必须优先用本工具一次性查询，绝对不要逐台调 query_backup_objects",
                    input_schema=_s("多台VM名称，逗号/空格分隔，如 ESXI-HK-8025,ESXI-US-8002,ESXI-SH-8015"),
                    execute_fn=self._tool_backup_objects_batch),
            ToolDef(name="query_backup_sessions",
                    description="查询最近的备份会话/执行记录",
                    input_schema=_s("搜索关键词"),
                    execute_fn=self._tool_backup_sessions_fn),
            ToolDef(name="query_backup_repos",
                    description="查询备份仓库/存储空间使用情况",
                    input_schema=ToolInputSchema(params=[]),
                    execute_fn=self._tool_backup_repos_fn),
            ToolDef(name="query_crm_customer",
                    description="查询 CRM 客户信息, 支持 GID 编号(如47141/TICKET-47141)或客户公司名称搜索, 返回客户详情/资源池/存储映射",
                    input_schema=ToolInputSchema(params=[
                        ToolParam(name="gid_or_name", type=ParamType.STRING,
                                  description="GID编号(如47141)或客户公司名称", required=True)]),
                    execute_fn=self._tool_crm_query),
            ToolDef(name="query_snapshots",
                    description="查询虚拟机快照列表和问题检查",
                    input_schema=_s("VM名/站点/关键词"),
                    execute_fn=self._tool_snapshot_check),
            ToolDef(name="query_smartops_vms",
                    description="查询虚拟机列表 (从 vCenter 资源池和 VCD 查询), 支持按 GID/VM名/站点/资源池名搜索",
                    input_schema=_s("GID/VM名/站点/资源池名"),
                    execute_fn=self._tool_smartops_vms),
            ToolDef(name="query_smartops_events",
                    description="查询运维事件(含严重/未确认事件), 支持按迁移/HA/vMotion等关键词搜索",
                    input_schema=_s("事件关键词如vMotion/HA/迁移"),
                    execute_fn=self._tool_smartops_events),
            ToolDef(name="query_vcd_resources",
                    description="查询 VCD 云平台资源(组织/资源池/CPU分配vs使用/内存分配vs使用/存储分配vs使用/存储配置) + 自动关联CRM客户信息(订单/服务编号/资源池/存储映射)。输入GID时自动返回客户订单、服务、存储卷详情。",
                    input_schema=_s("组织名/站点/GID"),
                    execute_fn=self._tool_vcd_resources),
            ToolDef(name="query_cmdb_hosts",
                    description="查询 CMDB 主机/服务器信息",
                    input_schema=_s("主机名/IP/关键词"),
                    execute_fn=self._tool_cmdb_query),
            ToolDef(name="query_dns_records",
                    description="查询 DNS 记录 (A/AAAA/CNAME/MX/TXT/NS/SOA/PTR/SRV, 支持批量域名与 IP 反向解析)",
                    input_schema=ToolInputSchema(params=[
                        ToolParam(name="domain", type=ParamType.STRING, description="域名或 IP (支持多行批量)", required=True),
                        ToolParam(name="record_type", type=ParamType.STRING, description="记录类型 (默认 A)")]),
                    execute_fn=self._tool_dns_query),
            ToolDef(name="resolve_dns_targets",
                    description="智能 DNS 解析: IP→反向解析主机名(PTR), 域名→正向解析IP(A), 支持批量目标 (逗号/空格/换行分隔)",
                    input_schema=_s("IP/域名列表"),
                    execute_fn=self._tool_dns_resolve),
            ToolDef(name="compare_dns_servers",
                    description="多 DNS 服务器对比查询: 同时在多个 DNS 服务器解析并对比结果是否一致",
                    input_schema=ToolInputSchema(params=[
                        ToolParam(name="domain", type=ParamType.STRING, description="域名 (支持多行批量, 最多20个)", required=True),
                        ToolParam(name="servers", type=ParamType.STRING, description="DNS 服务器列表 (换行/逗号/空格分隔)")]),
                    execute_fn=self._tool_dns_compare),
            ToolDef(name="trace_dns_path",
                    description="DNS 全链路解析追踪 (类似 dig +trace, 从根服务器逐级追踪)",
                    input_schema=_s("域名"),
                    execute_fn=self._tool_dns_trace),
            ToolDef(name="query_ha_vms",
                    description="查询 vSphere HA 高可用重启的 VM 列表和事件",
                    input_schema=_s("VM名/站点"),
                    execute_fn=self._tool_ha_query),
            ToolDef(name="query_monitors",
                    description="查询实时监控数据",
                    input_schema=_s("搜索关键词"),
                    execute_fn=self._tool_monitor_query),
            ToolDef(name="query_alerts",
                    description="查询告警信息",
                    input_schema=_s("搜索关键词"),
                    execute_fn=self._tool_alert_query),
            ToolDef(name="query_aria_ops",
                    description="查询 Aria Ops 仪表盘/容量/统计/HA事件",
                    input_schema=ToolInputSchema(params=[
                        ToolParam(name="search", type=ParamType.STRING, description="搜索关键词"),
                        ToolParam(name="type", type=ParamType.STRING, description="类型: dashboard/alerts/capacity/statistics/ha")]),
                    execute_fn=self._tool_aria_ops),
            ToolDef(name="query_storage_volumes",
                    description="查询存储卷信息 (含Nimble/NetApp阵列、容量、CRM映射、datastore关联)。用于查询存储部署情况、对比vCenter卷与阵列卷。",
                    input_schema=_s("站点/GID/卷名/阵列名"),
                    execute_fn=self._tool_storage_query),
            ToolDef(name="query_m365",
                    description="查询 M365 备份作业和许可证状态",
                    input_schema=ToolInputSchema(params=[]),
                    execute_fn=self._tool_m365_status),
        ]

    async def _tool_executor(self, name: str, args: dict):
        """Tool-call 循环中的工具执行器 (参考 claude-code tool execution loop)"""
        from core.tool_schema import ToolExecution
        tool_map = {t.name: t.execute_fn for t in self._build_tool_defs()}
        fn = tool_map.get(name)
        if fn:
            try:
                return await fn(params=args)
            except Exception as e:
                logger.error(f"[opscore_agent] 工具执行异常 {name}: {e}")
                return ToolExecution(tool_name=name, success=False, error=str(e))
        return ToolExecution(tool_name=name, success=False, error=f"未知工具: {name}")

    async def _prefetch_gid_data(self, gid_num: str, msg: AgentMessage) -> str:
        """预获取 GID 相关数据 (CRM 客户信息 + vCenter VM 列表)，不依赖 LLM 调用工具"""
        import asyncio
        logger.info(f"[opscore_agent] 预获取 GID {gid_num} 的数据...")
        await self.send_progress(msg, f"[1] 正在查询 CRM客户...")
        await self.send_progress(msg, f"[2] 正在查询 vCenter VM...")
        
        # 并行查询 CRM 和 vCenter VM
        crm_result, vm_result = await asyncio.gather(
            self._tool_crm_query({"search": gid_num}),
            self._tool_smartops_vms({"search": f"GID{gid_num}"}),
            return_exceptions=True
        )
        
        context_parts = []
        # 处理 CRM 结果
        if isinstance(crm_result, Exception):
            logger.warning(f"[opscore_agent] CRM 查询失败: {crm_result}")
        elif crm_result and crm_result.output:
            context_parts.append(f"## CRM 客户信息\n{crm_result.output}")
        
        # 处理 VM 结果
        if isinstance(vm_result, Exception):
            logger.warning(f"[opscore_agent] VM 查询失败: {vm_result}")
        elif vm_result and vm_result.output:
            context_parts.append(f"## vCenter VM 列表\n{vm_result.output}")
        
        if context_parts:
            result = "\n\n".join(context_parts)
            logger.info(f"[opscore_agent] 预获取完成: {len(result)} 字符")
            return f"\n\n---\n[系统已预获取以下数据，请基于这些数据回答用户问题]\n\n{result}\n\n---\n\n"
        else:
            logger.info(f"[opscore_agent] 预获取无数据")
            return ""

    # LLM 安全过滤触发词改写 ("真实"/"胡扯" 等词被 OneAPI 安全系统解读为 prompt injection)
    # 规则已统一下沉到 BaseAgent.rewrite_for_safety (含纠错/否定措辞防护)
    @classmethod
    def _rewrite_for_safety(cls, text: str) -> str:
        """改写用户查询中可能触发 LLM 安全过滤的措辞，转为中性运维查询"""
        return cls.rewrite_for_safety(text)

    async def _handle_with_tools(self, msg: AgentMessage):
        """
        Tool-Use 执行模式 (参考 claude-code QueryEngine tool-call loop)
        LLM 自主选择调用哪些工具, 可多次调用, 最后综合回答用户问题。
        """
        tools = self._build_tool_defs()
        history_prefix = await self._build_history_context(msg, max_turns=4, max_content_len=200)
        # 查询规范化: 清洗情绪用语 + 改写安全触发词
        user_text = self.sanitize_user_text(msg.text or "")
        rewritten_text = self._rewrite_for_safety(user_text)
        if rewritten_text != (msg.text or ""):
            logger.info(f"[opscore_agent] 查询规范化: '{msg.text}' → '{rewritten_text}'")
        user_text = rewritten_text
        # GID 正则: 要求明确前缀 gid/g，或数字序列不在字母中（避免 ESXI-03 中 3103 被误认为 GID）
        gid_match = re.search(r'(?:(?:gid|g)\s*(\d{4,6})|(?<![a-zA-Z])(\d{4,6})(?![a-zA-Z]))', user_text, re.IGNORECASE)
        prefetched_context = ""
        if gid_match and len(user_text) < 30:
            # 短查询如 "46445有什么vm" → "请查询GID 46445相关的资源信息: 有什么vm"
            gid_num = gid_match.group(1) or gid_match.group(2)
            user_text = f"请查询GID {gid_num}相关的资源信息: {msg.text}"
            logger.info(f"[opscore_agent] 查询规范化: '{msg.text}' → '{user_text}'")
            # 预获取 GID 相关数据 (CRM + vCenter VM)，不依赖 LLM 调用工具
            prefetched_context = await self._prefetch_gid_data(gid_num, msg)
        prompt = f"{history_prefix}{prefetched_context}用户需要查询以下运维数据: {user_text}"

        # 创建带进度报告的 executor
        _call_count = [0]
        async def executor_with_progress(name, args):
            _call_count[0] += 1
            tool_label = {
                "query_backup_jobs": "备份作业", "query_backup_objects": "备份对象",
                "query_backup_objects_batch": "批量备份对象",
                "query_crm_customer": "CRM客户", "query_snapshots": "快照",
                "query_smartops_vms": "虚拟机", "query_smartops_events": "运维事件",
                "query_vcd_resources": "VCD资源", "query_cmdb_hosts": "CMDB",
                "query_dns_records": "DNS", "query_ha_vms": "HA",
                "query_monitors": "监控", "query_alerts": "告警",
                "query_aria_ops": "Aria", "query_m365": "M365",
                "query_backup_sessions": "备份会话", "query_backup_repos": "备份仓库",
            }.get(name, name)
            await self.send_progress(msg, f"[{_call_count[0]}] 正在查询 {tool_label}...")
            return await self._tool_executor(name, args)

        result = await self.llm_query(
            prompt=prompt,
            system=OPSCORE_TOOL_USE_PROMPT,
            tools=tools,
            tool_executor=executor_with_progress,
            max_turns=6,
            max_budget_tokens=40000,
            temperature=0.3,
            username=msg.username,
            tool_choice="auto",
            # v4.0 (Phase 2.3): 21 工具 schema 延迟加载 (精简清单 + search_tool_detail 元工具)
            lazy_tool_schemas=bool(getattr(config, "TOOL_SCHEMA_LAZY_LOAD", False)),
            # 工具级入账归属与全链 trace (msg_id → 路由 → agent → tool)
            session_id=msg.session_id,
            context_id=msg.msg_id,
        )

        if result.get("error"):
            await self.reply(msg, error=f"OPS-CORE 查询失败: {result['error']}")
        else:
            text = result.get("text", "")
            tool_calls = result.get("tool_calls", [])
            
            # 自动导出表格数据为 CSV
            file_info = {}
            try:
                from utils.csv_export import export_text_as_csv, get_download_url
                csv_files = export_text_as_csv(text, query=msg.text or "")
                if csv_files:
                    # 取第一个文件 (如果有多个表格，合并为一个文件)
                    _, filename = csv_files[0]
                    file_info = {
                        "file_url": get_download_url(filename),
                        "file_name": filename,
                        "file_count": len(csv_files),
                    }
                    logger.info(f"[opscore_agent] 自动生成 CSV: {filename}")
            except Exception as e:
                logger.warning(f"[opscore_agent] CSV 导出失败: {e}")
            
            reply_data = {
                "text": text,
                "source": "opscore_tool_use",
                "tool_calls_count": len(tool_calls),
                "tools_used": list(set(tc.get("name", "") for tc in tool_calls)),
            }
            reply_data.update(file_info)
            await self.reply(msg, result=reply_data)
            tools_str = ",".join(set(tc.get("name", "") for tc in tool_calls))
            await self.log_daily(
                f"OPS-CORE Tool-Use: '{msg.text[:40]}' → {len(tool_calls)} 次工具调用 ({tools_str})",
                level="success", category="llm", session_id=msg.session_id,
                username=(msg.username or "")  # 完整问答全文由 BaseAgent.reply() 兜底收录
            )

    async def handle(self, msg: AgentMessage):
        """处理消息: 特定 action 走直接路由, chat/generic 走 tool_use 模式
        参考 claude-code: Agent 接收任务 → QueryEngine tool-call loop → LLM 自主选择工具"""
        action = msg.action or "chat"

        # 特定 action: 向后兼容, 直接走对应 handler
        _direct = {
            "cmdb_query": self._cmdb_query, "rack_query": self._rack_query,
            "monitor_query": self._monitor_query, "alert_query": self._alert_query,
            "case_query": self._case_query, "dns_query": self._dns_query,
            "datacenter_info": self._datacenter_info, "login_info": self._login_info,
            "backup_status": self._backup_status, "backup_objects": self._backup_objects,
            "backup_repos": self._backup_repos, "backup_sessions": self._backup_sessions,
            "snapshot_check": self._snapshot_check, "vcd_resources": self._vcd_resources,
            "aria_ops": self._aria_ops, "m365_status": self._m365_status,
            "smartops_vms": self._smartops_vms, "smartops_events": self._smartops_events,
            "smartops_status": self._smartops_status, "ha_query": self._ha_query,
            "overview": self._platform_overview,
        }
        if action != "chat" and action in _direct:
            await _direct[action](msg)
            return

        # chat / 未匹配 action → Tool-Use 模式: LLM 自主选择工具
        await self._handle_with_tools(msg)

    # ═══ 通用查询辅助 ═══

    @staticmethod
    def _extract_items(data) -> list:
        """从 API 返回数据中提取列表 (兼容 list / dict / paginated 格式)"""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if "error" in data:
                return []
            return data.get("results", [])
        return []

    async def _query_and_analyze(self, msg: AgentMessage, endpoint_key: str,
                                  label: str, search: str = ""):
        """通用: 按 endpoint_key 查询 API → LLM 分析 → 回复"""
        params = {}
        if search:
            params["search"] = search

        data = await query_api(endpoint_key, params)

        # 处理 API 返回的各种格式 (dict 或 list)
        if isinstance(data, dict) and "error" in data:
            await self.reply(msg, error=f"{label}查询失败: {data['error']}")
            return

        if isinstance(data, list):
            count = len(data)
            raw = json.dumps(data, ensure_ascii=False, indent=2)
        elif isinstance(data, dict):
            count = data.get("count", len(data.get("results", [])))
            raw = json.dumps(data, ensure_ascii=False, indent=2)
        else:
            count = "N/A"
            raw = str(data)[:6000]

        if raw == "[]" or raw == "{}" or count == 0:
            await self.reply(msg, result={"text": f"{label}查询结果为空 (关键词: '{search}')", "count": 0})
            return

        # 构建对话历史上下文 (统一使用 BaseAgent 辅助方法)
        history_prefix = await self._build_history_context(msg, max_turns=4, max_content_len=200)

        analysis = await self.llm_chat(
            prompt=f"{history_prefix}用户问题: {msg.text}\n\n以下是{label}查询结果 (关键词: '{search}', 共 {count} 条):\n\n{raw[:6000]}",
            system=OPSCORE_ANALYSIS_PROMPT
        )

        source = "opscore_info" if endpoint_key in INFO_API_CATALOG else "opscore_mgt"
        await self.reply(msg, result={"text": analysis, "count": count, "source": source})
        await self.log_daily(
            f"{label}: '{search}' → {count} 条",
            session_id=msg.session_id, username=(msg.username or "")
        )  # 完整问答全文由 BaseAgent.reply() 兜底收录

    # ═══════════════════════════════════════
    #  信息管理系统 (198.18.18.145)
    # ═══════════════════════════════════════

    async def _cmdb_query(self, msg: AgentMessage):
        search = msg.params.get("search") or msg.params.get("args") or msg.text or ""
        await self._query_and_analyze(msg, "cmdb_info", "CMDB 主机", search)

    async def _rack_query(self, msg: AgentMessage):
        search = msg.params.get("search") or msg.params.get("args") or msg.text or ""
        await self._query_and_analyze(msg, "rack_info", "机柜", search)

    async def _monitor_query(self, msg: AgentMessage):
        search = msg.params.get("search") or msg.params.get("args") or msg.text or ""
        await self._query_and_analyze(msg, "monitor_info", "监控", search)

    async def _alert_query(self, msg: AgentMessage):
        search = msg.params.get("search") or msg.params.get("args") or msg.text or ""
        await self._query_and_analyze(msg, "alarm_info", "告警", search)

    async def _case_query(self, msg: AgentMessage):
        search = msg.params.get("search") or msg.params.get("args") or msg.text or ""
        await self._query_and_analyze(msg, "case_info", "案例/工单", search)

    async def _dns_query(self, msg: AgentMessage):
        """DNS 查询 direct action — 调用 OPS-CORE DNS API (新接口)"""
        domain = msg.params.get("domain") or msg.params.get("args") or msg.text or ""
        query_type = (msg.params.get("type", "query") or "query").lower()
        record_type = (msg.params.get("record_type") or "A").upper()
        server = msg.params.get("server", "")
        if not domain:
            await self.reply(msg, error="DNS 查询缺少 domain 参数")
            return
        try:
            from agents.dns_api import get_dns_client
            client = get_dns_client()
            endpoint_map = {"spf": client.spf, "dmarc": client.dmarc, "zone": client.zone}
            fn = endpoint_map.get(query_type)
            if fn:
                data = await asyncio.to_thread(fn, domain)
            else:
                data = await asyncio.to_thread(client.query, domain, record_type, server)
            if isinstance(data, dict) and data.get("success") is False:
                await self.reply(msg, error=f"DNS 查询失败: {data.get('error', '未知错误')}")
                return
            raw = json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, (dict, list)) else str(data)[:6000]
            analysis = await self.llm_chat(
                prompt=f"以下是 DNS 查询结果 (域名: '{domain}', 类型: {query_type}):\n\n{raw[:6000]}",
                system=OPSCORE_ANALYSIS_PROMPT
            )
            await self.reply(msg, result={"text": analysis, "source": "opscore_info"})
            await self.log_daily(f"DNS 查询: {domain}", session_id=msg.session_id, username=(msg.username or ""))
        except Exception as e:
            await self.reply(msg, error=f"DNS 查询异常: {e}")

    async def _datacenter_info(self, msg: AgentMessage):
        data = await query_api("datacenter")
        if isinstance(data, dict) and "error" in data:
            await self.reply(msg, error=f"数据中心查询失败: {data['error']}")
            return
        raw = json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, (dict, list)) else str(data)[:6000]
        analysis = await self.llm_chat(
            prompt=f"以下是数据中心信息:\n\n{raw[:6000]}", system=OPSCORE_ANALYSIS_PROMPT
        )
        await self.reply(msg, result={"text": analysis, "source": "opscore_info"})

    async def _login_info(self, msg: AgentMessage):
        search = msg.params.get("search") or msg.params.get("args") or msg.text or ""
        info_type = msg.params.get("type", "idc")
        type_map = {"idc": "idc_info", "vendor": "vendor_info", "access": "opscore_access"}
        endpoint_key = type_map.get(info_type, "idc_info")
        await self._query_and_analyze(msg, endpoint_key, f"{info_type} 信息", search)

    # ═══════════════════════════════════════
    #  管理平台 (198.18.18.146)
    # ═══════════════════════════════════════

    async def _backup_status(self, msg: AgentMessage):
        """备份作业查询 — 按 site(站点) 预聚合，搜索时附加具体匹配项"""
        import re
        search = msg.params.get("search") or msg.params.get("args") or ""
        
        # 如果 params 中没有 search，从用户原文中智能提取关键词
        if not search and msg.text:
            # 提取 GID 或数字编号
            gid_from_text = re.findall(r'GID[A-Z]{0,2}\d{4,6}|(?<!\d)\d{5,6}(?!\d)', msg.text, re.IGNORECASE)
            if gid_from_text:
                search = gid_from_text[0]
                logger.info(f"[opscore_agent] 从用户消息提取搜索关键词: '{search}'")
        
        # 智能提取GID：如果search是纯数字，转换为GID格式
        gid_search_term = ""
        if search:
            # 提取纯数字GID（如47141）转换为GID47141格式
            gid_match = re.match(r'^(GID[A-Z]{0,2}?\d+|\d{5,6})$', search.strip(), re.IGNORECASE)
            if gid_match:
                raw_gid = gid_match.group(1)
                gid_search_term = raw_gid if raw_gid.upper().startswith("GID") else f"GID{raw_gid}"
                search = gid_search_term  # 统一使用GID格式搜索

        # 1. 查询数据: 有关键词时用 API search 精准查询 + 全量概览；无关键词时仅拉全量
        if search:
            # 精准搜索: 同时搜索纯数字和GID格式
            search_terms = [search]
            if gid_search_term:
                pure_num = gid_search_term.upper().replace("GID", "")
                if pure_num != search:
                    search_terms.append(pure_num)
            
            tasks = [
                query_api("brr_backups", {"search": search, "page_size": 500}),
                query_api("brr_jobs", {"search": search, "page_size": 500}),
                query_api("brr_gid_jobs", {"search": search, "page_size": 500}),
            ]
            # 同时拉少量全量数据用于站点概览
            tasks.append(query_api("brr_backups", {"page_size": 100}))
            tasks.append(query_api("brr_jobs", {"page_size": 100}))
        else:
            tasks = [
                query_api("brr_backups", {"page_size": 1000}),
                query_api("brr_jobs", {"page_size": 1000}),
                asyncio.sleep(0),  # placeholder for gid_jobs
                asyncio.sleep(0),  # placeholder
                asyncio.sleep(0),  # placeholder
            ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        if search:
            # 精准搜索结果 (用于重点分析)
            backups_data = results[0] if not isinstance(results[0], Exception) else {}
            jobs_data = results[1] if not isinstance(results[1], Exception) else {}
            gid_data = results[2] if not isinstance(results[2], Exception) else {}
            # 概览数据 (用于全局统计)
            overview_backups = results[3] if not isinstance(results[3], Exception) else {}
            overview_jobs = results[4] if not isinstance(results[4], Exception) else {}
        else:
            backups_data = results[0] if not isinstance(results[0], Exception) else {}
            jobs_data = results[1] if not isinstance(results[1], Exception) else {}
            gid_data = {}
            overview_backups = backups_data
            overview_jobs = jobs_data

        backups = self._extract_items(backups_data)
        jobs = self._extract_items(jobs_data)
        # brr_gid_jobs 返回格式不同: {total_count, server_stats, backups, gid_numbers}
        if isinstance(gid_data, dict) and "backups" in gid_data:
            gid_jobs = gid_data.get("backups", [])
            if not isinstance(gid_jobs, list):
                gid_jobs = []
        else:
            gid_jobs = self._extract_items(gid_data) if isinstance(gid_data, (dict, list)) else []

        # 概览数据用于全局统计
        overview_bk = self._extract_items(overview_backups) if search else backups
        overview_jb = self._extract_items(overview_jobs) if search else jobs

        if not backups and not jobs and not gid_jobs:
            await self.reply(msg, result={"text": f"备份查询结果为空 (关键词: '{search}')", "count": 0})
            return

        # 2. 按 site(站点) 预聚合 — 使用概览数据做全局统计
        site_stats: Dict[str, Dict] = {}
        def _ensure_site(site):
            if site not in site_stats:
                site_stats[site] = {
                    "backup_count": 0, "job_count": 0, "disabled_jobs": 0,
                    "platforms": set(), "policy_tags": set(), "job_types": set(),
                    "latest_creation": "", "backup_names": [], "job_names": [],
                    "server_urls": set(),
                }

        # 使用概览数据做站点统计
        for b in overview_bk:
            site = b.get("site", "unknown")
            _ensure_site(site)
            site_stats[site]["backup_count"] += 1
            site_stats[site]["platforms"].add(b.get("platform_name", ""))
            if b.get("policy_tag"):
                site_stats[site]["policy_tags"].add(b["policy_tag"])
            ct = b.get("creation_time", "")
            if ct > site_stats[site]["latest_creation"]:
                site_stats[site]["latest_creation"] = ct
            site_stats[site]["backup_names"].append(b.get("backup_name", ""))
            if b.get("server_url"):
                site_stats[site]["server_urls"].add(b["server_url"])

        for j in overview_jb:
            site = j.get("site", "unknown")
            _ensure_site(site)
            site_stats[site]["job_count"] += 1
            if j.get("is_disabled"):
                site_stats[site]["disabled_jobs"] += 1
            site_stats[site]["job_types"].add(j.get("job_type", ""))
            site_stats[site]["job_names"].append(j.get("name", ""))
            if j.get("server_url"):
                site_stats[site]["server_urls"].add(j["server_url"])

        # 3. 构建聚合摘要表
        total_bk = backups_data.get("count", len(overview_bk)) if isinstance(backups_data, dict) else len(overview_bk)
        total_jb = jobs_data.get("count", len(overview_jb)) if isinstance(jobs_data, dict) else len(overview_jb)
        lines = [
            f"备份作业站点汇总 (共 {len(site_stats)} 个站点, {total_bk} 个备份, {total_jb} 个作业)",
            "",
            "| 站点 | 备份数 | 作业数 | 禁用作业 | 作业类型 | 平台 | Veeam服务器 | 最近备份时间 | 示例备份 | 示例作业 |",
            "|:---|---:|---:|---:|:---|:---|:---|:---|:---|:---|",
        ]
        for site in sorted(site_stats.keys(), key=lambda s: site_stats[s]["backup_count"], reverse=True):
            s = site_stats[site]
            platforms = ", ".join(sorted(s["platforms"] - {""}))[:40] or "-"
            jtypes = ", ".join(sorted(s["job_types"] - {""}))[:30] or "-"
            svrs = ", ".join(sorted(s["server_urls"]))[:40] or "-"
            latest = s["latest_creation"][:19] if s["latest_creation"] else "-"
            bk_samples = ", ".join(s["backup_names"][:2])[:50]
            job_samples = ", ".join(s["job_names"][:2])[:50]
            lines.append(
                f"| {site} | {s['backup_count']} | {s['job_count']} | {s['disabled_jobs']} | {jtypes} | {platforms} | {svrs} | {latest} | {bk_samples} | {job_samples} |"
            )

        aggregated = "\n".join(lines)
        total_count = total_bk + total_jb

        # 4. 搜索匹配: 直接使用 API 精准搜索结果 (backups/jobs 已经是过滤后的)
        extra_detail = ""
        if search:
            matched_backups = backups  # API search 已精准过滤
            matched_jobs = jobs        # API search 已精准过滤

            detail_parts = []
            if matched_backups:
                detail_parts.append(f"\n### 与 '{search}' 相关的备份 ({len(matched_backups)} 条)\n")
                detail_parts.append("| 站点 | 备份名称 | 作业名 | 平台 | 策略标签 | 创建时间 | 服务器 |")
                detail_parts.append("|:---|:---|:---|:---|:---|:---|:---|")
                for b in matched_backups[:30]:
                    detail_parts.append(f"| {b.get('site','-')} | {b.get('backup_name','-')} | {b.get('job_name','-')} | {b.get('platform_name','-')} | {b.get('policy_tag','-')} | {(b.get('creation_time','') or '')[:19]} | {b.get('server_url','-')} |")

            if matched_jobs:
                detail_parts.append(f"\n### 与 '{search}' 相关的作业 ({len(matched_jobs)} 条)\n")
                detail_parts.append("| 站点 | 作业名称 | 类型 | 最后结果 | 禁用 | 服务器 |")
                detail_parts.append("|:---|:---|:---|:---|:---|:---|")
                for j in matched_jobs[:30]:
                    detail_parts.append(f"| {j.get('site','-')} | {j.get('name','-')} | {j.get('job_type','-')} | {j.get('last_result', j.get('status','-'))} | {'是' if j.get('is_disabled') else '否'} | {j.get('server_url','-')} |")

            if gid_jobs:
                detail_parts.append(f"\n### GID 作业接口匹配结果 ({len(gid_jobs)} 条)\n")
                detail_parts.append("| 站点 | 作业名称 | 类型 | GID | 状态 | 服务器 |")
                detail_parts.append("|:---|:---|:---|:---|:---|:---|")
                for g in gid_jobs[:30]:
                    detail_parts.append(f"| {g.get('site','-')} | {g.get('name','-')} | {g.get('job_type','-')} | {g.get('gid','-')} | {g.get('status','-')} | {g.get('server_url','-')} |")

            if not matched_backups and not matched_jobs and not gid_jobs:
                detail_parts.append(f"\n未找到与 '{search}' 相关的备份或作业记录")

            extra_detail = "\n".join(detail_parts)

        # 5. LLM 分析 - 增强系统提示防止数据混淆
        enhanced_system = OPSCORE_ANALYSIS_PROMPT
        if gid_search_term:
            enhanced_system += f"\n\n⚠️ 用户查询的是 GID: {gid_search_term} 的备份信息。\n- 回答时必须严格引用匹配该GID的备份数据\n- 如果gid_jobs结果中有其他GID的数据，那是其他客户，绝不能用于本问题回答\n- 如果没有找到匹配数据，直接说明未找到，不要用其他站点/GID的数据填充！"
        
        # 有搜索关键词时以匹配结果为主，概览为辅
        if search and extra_detail:
            prompt_text = f"用户问题: {msg.text}\n\n★ 以下是与关键词 '{search}' 精准匹配的备份/作业记录 (这是最重要的数据，必须基于这些数据回答):\n{extra_detail}\n\n— 以下是全局备份概览 (仅供参考，不是查询结果):\n{aggregated}"
        else:
            prompt_text = f"用户问题: {msg.text}\n\n以下是备份作业按站点(site)聚合的完整数据 (无截断):\n\n{aggregated}"
            if extra_detail:
                prompt_text += f"\n\n以下是与关键词 '{search}' 匹配的具体记录 (重点分析这些):\n{extra_detail}"

        logger.info(f"[opscore_agent] 备份查询数据: search={search}, 匹配备份={len(backups)}, 匹配作业={len(jobs)}, gid_jobs={len(gid_jobs)}, 概览备份={len(overview_bk)}, 概览作业={len(overview_jb)}")

        analysis = await self.llm_chat(prompt=prompt_text, system=enhanced_system)
        await self.reply(msg, result={"text": analysis, "count": total_count, "source": "opscore_mgt"})
        await self.log_daily(
            f"备份查询: {msg.text[:50]} → {len(site_stats)} 站点, {total_count} 条",
            session_id=msg.session_id, username=(msg.username or "")
        )  # 完整问答全文由 BaseAgent.reply() 兜底收录

    async def _backup_objects(self, msg: AgentMessage):
        """备份对象查询 — 按 site(站点) 预聚合"""
        search = msg.params.get("search") or msg.params.get("vm_name") or ""
        params = {"page_size": 1000}
        if search:
            params["vm_name__icontains"] = search

        data = await query_api("brr_backup_objects", params)
        objects = self._extract_items(data)

        if not objects:
            await self.reply(msg, result={"text": f"备份对象查询结果为空 (关键词: '{search}')", "count": 0})
            return

        # 按 site(站点) 预聚合
        site_stats: Dict[str, Dict] = {}
        for o in objects:
            site = o.get("site", "unknown")
            if site not in site_stats:
                site_stats[site] = {
                    "obj_count": 0, "vi_types": set(), "platforms": set(),
                    "total_restore_points": 0, "vm_names": [],
                    "server_urls": set(),
                }
            site_stats[site]["obj_count"] += 1
            site_stats[site]["vi_types"].add(o.get("vi_type", ""))
            site_stats[site]["platforms"].add(o.get("platform_name", ""))
            site_stats[site]["total_restore_points"] += int(o.get("restore_points_count") or 0)
            if o.get("vm_name"):
                site_stats[site]["vm_names"].append(o["vm_name"])
            if o.get("server_url"):
                site_stats[site]["server_urls"].add(o["server_url"])

        # 构建聚合摘要表
        lines = [
            f"备份对象站点汇总 (共 {len(site_stats)} 个站点, {len(objects)} 个备份对象)",
            "",
            "| 站点 | 对象数 | 还原点总数 | 虚拟化类型 | 平台 | Veeam服务器 | 示例VM |",
            "|:---|---:|---:|:---|:---|:---|:---|",
        ]
        for site in sorted(site_stats.keys(), key=lambda s: site_stats[s]["obj_count"], reverse=True):
            s = site_stats[site]
            vi_types = ", ".join(sorted(s["vi_types"] - {""})) or "-"
            platforms = ", ".join(sorted(s["platforms"] - {""}))[:40] or "-"
            svrs = ", ".join(sorted(s["server_urls"]))[:40] or "-"
            samples = ", ".join(s["vm_names"][:3])[:60]
            lines.append(
                f"| {site} | {s['obj_count']} | {s['total_restore_points']} | {vi_types} | {platforms} | {svrs} | {samples} |"
            )

        aggregated = "\n".join(lines)

        analysis = await self.llm_chat(
            prompt=f"用户问题: {msg.text}\n\n以下是备份对象按站点(site)聚合的完整数据 (无截断):\n\n{aggregated}",
            system=OPSCORE_ANALYSIS_PROMPT
        )
        await self.reply(msg, result={"text": analysis, "count": len(objects), "source": "opscore_mgt"})
        await self.log_daily(f"备份对象: '{search}' → {len(site_stats)} 站点, {len(objects)} 条",
                             session_id=msg.session_id, username=(msg.username or ""))

    async def _backup_repos(self, msg: AgentMessage):
        await self._query_and_analyze(msg, "brr_repositories", "备份仓库")

    async def _backup_sessions(self, msg: AgentMessage):
        search = msg.params.get("search") or msg.params.get("args") or ""
        await self._query_and_analyze(msg, "brr_sessions", "备份会话", search)

    async def _snapshot_check(self, msg: AgentMessage):
        search = msg.params.get("search") or msg.params.get("args") or msg.text or ""
        await self._query_and_analyze(msg, "snapshot_list", "快照", search)

    async def _vcd_resources(self, msg: AgentMessage):
        # search 仅用显式参数，不用 msg.text（避免把自然语言问题当过滤关键词）
        search = msg.params.get("search") or msg.params.get("args") or ""
        resource_type = msg.params.get("type", "")
        # 根据资源类型选择端点
        if resource_type in ("vms", "resources"):
            await self._query_and_analyze(msg, "vcd_resources", f"VCD {resource_type}", search)
            return

        # ── 默认: 查组织+资源两个端点，按站点预聚合后交给 LLM ──
        orgs_data = await query_api("vcd_organizations")
        res_data = await query_api("vcd_resources")

        orgs = orgs_data if isinstance(orgs_data, list) else (orgs_data.get("results", []) if isinstance(orgs_data, dict) else [])
        resources = res_data if isinstance(res_data, list) else (res_data.get("results", []) if isinstance(res_data, dict) else [])

        if not orgs and not resources:
            await self.reply(msg, result={"text": "VCD 查询结果为空", "count": 0})
            return

        # 按 site 预聚合，避免向 LLM 发送海量原始数据
        site_stats = {}
        for o in orgs:
            site = o.get("site", "unknown")
            if site not in site_stats:
                site_stats[site] = {"org_count": 0, "res_count": 0, "org_names": [],
                                     "cpu_alloc": 0, "cpu_used": 0, "mem_alloc": 0, "mem_used": 0,
                                     "stor_alloc": 0, "stor_used": 0, "vcd_urls": set()}
            site_stats[site]["org_count"] += 1
            site_stats[site]["org_names"].append(o.get("name", ""))
            vcd_display = o.get("provider_vdc") or o.get("vcd_url", "")
            if vcd_display:
                site_stats[site]["vcd_urls"].add(vcd_display)

        for r in resources:
            site = r.get("site", "unknown")
            if site not in site_stats:
                site_stats[site] = {"org_count": 0, "res_count": 0, "org_names": [],
                                     "cpu_alloc": 0, "cpu_used": 0, "mem_alloc": 0, "mem_used": 0,
                                     "stor_alloc": 0, "stor_used": 0, "vcd_urls": set()}
            site_stats[site]["res_count"] += 1
            site_stats[site]["cpu_alloc"] += float(r.get("cpu_allocated") or 0)
            site_stats[site]["cpu_used"] += float(r.get("cpu_used") or 0)
            site_stats[site]["mem_alloc"] += float(r.get("memory_allocated") or 0)
            site_stats[site]["mem_used"] += float(r.get("memory_used") or 0)
            site_stats[site]["stor_alloc"] += float(r.get("storage_allocated") or 0)
            site_stats[site]["stor_used"] += float(r.get("storage_used") or 0)

        # 构建聚合摘要 (完整数据，无截断)
        summary_lines = []
        summary_lines.append(f"VCD 平台站点汇总 (共 {len(site_stats)} 个站点, {len(orgs)} 个组织, {len(resources)} 个资源)")
        summary_lines.append("")
        summary_lines.append("| 站点 | 组织数 | 资源数 | CPU总量(GHz) | CPU已分配(GHz) | 分配率 | 内存总量(GB) | 内存已分配(GB) | 分配率 | 存储总量(TB) | 存储已分配(TB) | 分配率 | VCD | 示例组织 |")
        summary_lines.append("|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---|:---|")

        for site in sorted(site_stats.keys(), key=lambda s: site_stats[s]["org_count"], reverse=True):
            s = site_stats[site]
            cpu_pct = f"{s['cpu_used']/s['cpu_alloc']*100:.1f}%" if s['cpu_alloc'] > 0 else "-"
            mem_pct = f"{s['mem_used']/s['mem_alloc']*100:.1f}%" if s['mem_alloc'] > 0 else "-"
            stor_alloc_tb = s['stor_alloc'] / 1024 if s['stor_alloc'] > 1024 else s['stor_alloc']
            stor_used_tb = s['stor_used'] / 1024 if s['stor_used'] > 1024 else s['stor_used']
            stor_pct = f"{s['stor_used']/s['stor_alloc']*100:.1f}%" if s['stor_alloc'] > 0 else "-"
            urls = ", ".join(sorted(s['vcd_urls'])[:2]) if s['vcd_urls'] else "-"
            samples = ", ".join(s['org_names'][:3])
            summary_lines.append(
                f"| {site} | {s['org_count']} | {s['res_count']} | {s['cpu_alloc']:.0f} | {s['cpu_used']:.0f} | {cpu_pct} | {s['mem_alloc']:.0f} | {s['mem_used']:.0f} | {mem_pct} | {stor_alloc_tb:.1f} | {stor_used_tb:.1f} | {stor_pct} | {urls} | {samples} |"
            )

        aggregated = "\n".join(summary_lines)
        total_count = len(orgs) + len(resources)

        analysis = await self.llm_chat(
            prompt=f"用户问题: {msg.text}\n\n以下是 VCD 平台按站点聚合的完整数据 (无截断):\n\n{aggregated}",
            system=OPSCORE_ANALYSIS_PROMPT
        )
        await self.reply(msg, result={"text": analysis, "count": total_count, "source": "opscore_mgt"})
        await self.log_daily(f"VCD 查询: {msg.text[:50]} → {len(site_stats)} 站点, {total_count} 条",
                             session_id=msg.session_id, username=(msg.username or ""))

    async def _aria_ops(self, msg: AgentMessage):
        search = msg.params.get("search") or msg.params.get("args") or msg.text or ""
        query_type = msg.params.get("type", "dashboard")
        type_map = {
            "alerts": "aria_logs_alerts", "inventory": "aria_ops_inventory",
            "capacity": "aria_ops_capacity", "statistics": "aria_ops_statistics",
            "servers": "aria_ops_servers", "vms": "aria_ops_vms",
            "dashboard": "aria_dashboard", "ha": "aria_ha_events",
        }
        endpoint_key = type_map.get(query_type, "aria_dashboard")
        await self._query_and_analyze(msg, endpoint_key, f"Aria {query_type}", search)

    async def _m365_status(self, msg: AgentMessage):
        await self._query_and_analyze(msg, "m365_dashboard", "M365 备份")

    # ── Smart-Ops 新增 ──

    async def _smartops_vms(self, msg: AgentMessage):
        search = msg.params.get("search") or msg.params.get("args") or msg.text or ""

        # 主机代码 (如 ESXI-02) → 主机 VM 查询 (与 _tool_smartops_vms 工具路径一致):
        # MCP 全量缓存解析 esxi_host → REST host= 服务端精确过滤 → snapshot 合并
        if expand_host_code(search):
            info = await query_vms_on_esxi_host(search)
            if info and info["vms"]:
                vms = info["vms"]
                host_display = ", ".join(info["esxi_hosts"][:2])
                # 紧凑行格式 (~240字符/台) 替代原始 JSON (~1100字符/台):
                # raw[:9000] 截 JSON 会丢 VM (18台只含8台), 紧凑格式 18 台仅 4.4KB 全量可见
                _keys = ['name', 'site', 'resource_pool', 'power_state', 'cpu_count',
                         'memory_mb', 'guest_os', 'guest_ip', 'customer_gid',
                         'biz_type', 'cluster', 'vcenter_server']
                _lines = []
                for _i, _v in enumerate(sorted(vms, key=lambda x: x.get('name', '')), 1):
                    _parts = [f"#{_i}"]
                    for _k in _keys:
                        _val = _v.get(_k)
                        if _val is not None and str(_val).strip() and str(_val) != 'None' and _val != []:
                            _parts.append(f"{_k}={str(_val)[:50]}")
                    _lines.append("  " + " | ".join(_parts))
                raw = "\n".join(_lines)
                history_prefix = await self._build_history_context(
                    msg, max_turns=4, max_content_len=200)

                # 流式上屏 (同 general_agent/kb_agent): 长生成 (~40 tok/s, 18台全量
                # 表格+分析需 60-80s) 边生成边推送, 用户秒级看到内容而非干等一次拉全
                async def _llm_wait(elapsed: int):
                    await self.send_progress(msg, f"⏳ 模型仍在生成中 (已等待 {elapsed}s)，请稍候...")

                async def _llm_delta(seg: str):
                    await self.send_delta(msg, seg)

                async def _llm_thinking(seg: str):
                    await self.send_delta(msg, seg, kind="thinking")

                await self.send_progress(
                    msg, f"已获取 {search.upper()} 全部 {len(vms)} 台虚拟机数据，正在生成分析报告...")
                analysis = await self.llm_chat(
                    prompt=(f"{history_prefix}用户问题: {msg.text}\n\n"
                            f"以下是主机 {search.upper()} ({host_display}) 上的全部 {len(vms)} 台虚拟机 "
                            f"(smartops_vms host=服务端过滤 + snapshot_vms 合并, "
                            f"快照覆盖 {info['snapshot_matched']}/{len(vms)}):\n\n{raw[:12000]}"),
                    system=OPSCORE_ANALYSIS_PROMPT,
                    on_wait=_llm_wait, on_delta=_llm_delta,
                    on_reasoning=_llm_thinking, username=(msg.username or ""))
                await self.reply(msg, result={
                    "text": analysis, "count": len(vms),
                    "source": "opscore_mgt", "esxi_hosts": info["esxi_hosts"]})
                await self.log_daily(
                    f"主机VM: '{search}' → {len(vms)} 台",
                    session_id=msg.session_id, username=(msg.username or ""))
                return

        await self._query_and_analyze(msg, "smartops_vms", "Smart-Ops 虚拟机", search)

    async def _smartops_events(self, msg: AgentMessage):
        search = msg.params.get("search") or msg.params.get("args") or ""
        text = msg.text or ""
        event_type = msg.params.get("type", "all")

        # 智能提取搜索关键词: 不用完整自然语言文本作 API search
        if not search and text:
            # 迁移相关查询 → 用 vMotion 关键词搜索
            migration_kw = ["迁移", "vMotion", "migrate", "vmotion"]
            if any(kw in text.lower() for kw in ["迁移", "vmotion", "migrate"]):
                search = "vMotion"
            # HA 相关 → 用 HA 关键词搜索
            elif any(kw in text.upper() for kw in ["HA", "高可用", "FAILOVER"]):
                search = "HA"
            # 其他情况: 不加 search 过滤，让 LLM 分析最新事件

        type_map = {
            "critical": "smartops_events_critical",
            "unack": "smartops_events_unack",
        }
        endpoint_key = type_map.get(event_type, "smartops_events")
        await self._query_and_analyze(msg, endpoint_key, f"运维事件({event_type})", search)

    async def _smartops_status(self, msg: AgentMessage):
        await self._query_and_analyze(msg, "smartops_status_summary", "运维状态汇总")

    # ═══════════════════════════════════════
    #  综合
    # ═══════════════════════════════════════

    async def _platform_overview(self, msg: AgentMessage):
        """两个平台的综合概览"""
        results = {}
        tasks = [
            self._safe_query("cmdb", "cmdb_info"),
            self._safe_query("monitor", "monitor_info"),
            self._safe_query("alarm", "alarm_info"),
            self._safe_query("backups", "brr_backups"),
            self._safe_query("brr_stats", "brr_stats"),
            self._safe_query("snapshots", "snapshot_summary"),
            self._safe_query("smartops_vms", "smartops_vms_summary"),
            self._safe_query("smartops_status", "smartops_status_summary"),
        ]
        query_results = await asyncio.gather(*tasks)
        for name, data in query_results:
            results[name] = {
                "count": data.get("count", "N/A") if isinstance(data, dict) else "error",
                "sample": data.get("results", [])[:2] if isinstance(data, dict) else [],
                "data": {k: v for k, v in data.items() if k not in ("results",)} if isinstance(data, dict) and "error" not in data else None
            }

        raw = json.dumps(results, ensure_ascii=False, indent=2)
        analysis = await self.llm_chat(
            prompt=f"以下是 OPS-CORE 两个平台的数据概览，请生成综合状态报告:\n\n{raw[:8000]}",
            system=OPSCORE_ANALYSIS_PROMPT
        )
        await self.reply(msg, result={"text": analysis, "source": "opscore_all", "overview": results})
        await self.log_daily("OPS-CORE 平台综合概览", session_id=msg.session_id, username=(msg.username or ""))

    async def _safe_query(self, name: str, endpoint_key: str, params: dict = None) -> tuple:
        try:
            data = await query_api(endpoint_key, params)
            return (name, data)
        except Exception as e:
            return (name, {"error": str(e)})

    async def _opscore_chat(self, msg: AgentMessage):
        """OPS-CORE 智能对话 — 自动识别并查询所有可用 API，支持对话历史上下文"""
        prompt = msg.text or msg.params.get("text", "")
        if not prompt:
            await self.reply(msg, error="缺少问题内容")
            return

        # 构建对话历史上下文 (统一使用 BaseAgent 辅助方法, 支持 SessionEngine compact 感知)
        history_context = await self._build_history_context(msg)

        # ═══ 检测 CRM/GID 相关查询 ═══
        import re
        gid_pattern = r'GID[A-Z]{0,2}\d{5}|\b\d{5}\b'
        gid_matches = re.findall(gid_pattern, prompt.upper())
        
        # 检测是否包含中文字符（可能是客户名称）
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', prompt))
        has_crm_keywords = any(kw in prompt.lower() for kw in ["crm", "资源池", "storage mapping", "客户", "公司"])
        
        if gid_matches or has_crm_keywords or has_chinese:
            gid = gid_matches[0] if gid_matches else None
            
            # 如果没有GID但包含中文，尝试从CRM搜索客户名称
            if not gid and has_chinese:
                await self.send_progress(msg, "正在从CRM搜索匹配的客户...")
                # 提取搜索词：移除常见运维词汇，保留可能的公司名
                search_term = prompt
                for kw in ["备份", "backup", "有哪些", "的", "查询", "查看", "信息", "数据", "vm", "虚拟机", "快照"]:
                    search_term = search_term.replace(kw, "")
                search_term = search_term.strip()[:30]
                
                if search_term:
                    crm_search = await query_crm_customers(search=search_term, page_size=10)
                    if "error" not in crm_search and crm_search.get("results"):
                        results = crm_search.get("results", [])
                        # 计算适配度：优先返回公司名包含搜索词的结果
                        matched = []
                        for r in results:
                            company = r.get("company_name", "")
                            gid_val = r.get("gid", "")
                            # 计算匹配分数
                            score = 0
                            if search_term.lower() in company.lower():
                                score += 10
                            if search_term.lower() in gid_val.lower():
                                score += 5
                            if any(c in company for c in search_term if '\u4e00' <= c <= '\u9fff'):
                                score += 20  # 中文字符匹配加分
                            matched.append((score, r))
                        
                        # 按分数排序
                        matched.sort(key=lambda x: x[0], reverse=True)
                        
                        if matched and matched[0][0] > 0:
                            best_match = matched[0][1]
                            gid = best_match.get("gid")
                            await self.send_progress(msg, f"CRM匹配: {best_match.get('company_name')} (GID {gid}, 适配度:{matched[0][0]})")
                        elif len(results) == 1:
                            gid = results[0].get("gid")
                            await self.send_progress(msg, f"找到客户: {results[0].get('company_name')} (GID {gid})")
                        elif len(results) > 1:
                            # 多个匹配，返回列表让用户选择
                            companies = [f"GID {r.get('gid')}: {r.get('company_name')} (资源池:{r.get('resource_pools_count',0)})" for r in results[:5]]
                            await self.reply(msg, result={
                                "text": f"找到多个匹配客户:\n" + "\n".join(companies) + "\n\n请指定具体的 GID 查询详细备份信息。",
                                "source": "opscore_crm"
                            })
                            return
            
            if gid:
                await self.send_progress(msg, f"正在查询 GID {gid} 的客户及备份信息...")
                crm_data = await query_crm_customer_resources(gid)
                
                if "error" not in crm_data:
                    # 同时查询其他相关数据
                    extra_data = []
                    
                    # 获取所有关联的GID（支持多GID客户）
                    related_gids = crm_data.get("related_gids", [gid])
                    primary_gid = crm_data.get("primary_gid", gid)
                    await self.send_progress(msg, f"发现关联GID: {', '.join(related_gids)}")
                    
                    # 查询所有关联GID的备份作业 - 优先使用 brr_gid_jobs 端点
                    for search_gid in related_gids:
                        # 确保GID格式正确：API期望 "TICKET-47141" 格式
                        gid_search_term = search_gid if search_gid.upper().startswith("GID") else f"GID{search_gid}"
                        
                        # 优先使用 brr_gid_jobs 端点（专门为GID搜索优化）
                        brr_gid_jobs_data = await query_api("brr_gid_jobs", {"search": gid_search_term, "page_size": 50})
                        if "error" not in brr_gid_jobs_data and brr_gid_jobs_data.get("results"):
                            extra_data.append(f"=== BRR GID备份作业 (GID {search_gid}) ===\n{json.dumps(brr_gid_jobs_data, ensure_ascii=False, indent=1)[:2000]}")
                        
                        # 同时查询普通brr_jobs作为补充
                        brr_jobs = await query_api("brr_jobs", {"search": gid_search_term, "page_size": 20})
                        if "error" not in brr_jobs and brr_jobs.get("results"):
                            extra_data.append(f"=== BRR备份作业补充 (GID {search_gid}) ===\n{json.dumps(brr_jobs, ensure_ascii=False, indent=1)[:1500]}")
                        
                        # 查询备份对象(VM)
                        brr_vms = await query_api("brr_backup_objects", {"search": search_gid, "page_size": 20})
                        if "error" not in brr_vms and brr_vms.get("results"):
                            extra_data.append(f"=== 备份VM列表 (GID {search_gid}) ===\n{json.dumps(brr_vms, ensure_ascii=False, indent=1)[:1500]}")
                        
                        # 查询快照VM
                        snapshot_vms = await query_api("snapshot_vms", {"search": search_gid, "page_size": 20})
                        if "error" not in snapshot_vms and snapshot_vms.get("results"):
                            extra_data.append(f"=== 快照VM (GID {search_gid}) ===\n{json.dumps(snapshot_vms, ensure_ascii=False, indent=1)[:1500]}")
                    
                    # 组装完整数据 - 明确标注客户信息
                    customer_info = crm_data.get("customer", {})
                    company_name = customer_info.get("company_name", "未知")
                    actual_gid = customer_info.get("gid", gid)
                    
                    full_data = f"""=== 查询客户信息 ===
用户输入: {prompt}
匹配客户: {company_name}
主GID: {actual_gid}
关联GID: {', '.join(related_gids)}

=== CRM客户详情 ===
{json.dumps(crm_data, ensure_ascii=False, indent=1)}"""
                    if extra_data:
                        full_data += "\n\n" + "\n\n".join(extra_data)
                    
                    # 增强系统提示：强制要求LLM使用CRM返回的实际GID和公司名
                    enhanced_prompt = OPSCORE_ANALYSIS_PROMPT + f"\n\n⚠️ 极其重要：回答时必须严格使用CRM返回的实际GID和公司名称！\n- 用户查询的GID: {gid}\n- CRM返回的主GID: {primary_gid}\n- CRM返回的公司名: {company_name}\n- 如果数据中出现其他GID（如GID19934），那是其他客户的数据，绝不能用于回答本问题！\n- 所有引用的备份数据必须来自上述GID: {primary_gid} 或关联GID: {', '.join(related_gids)}\n- 如果找不到这些GID的备份数据，直接说明'未找到该客户的备份记录'，不要用其他客户的数据填充！"
                    
                    analysis = await self.llm_chat(
                        prompt=f"{history_context}用户问题: {prompt}\n\n从 OPS-CORE 平台查询到的数据:\n{full_data}",
                        system=enhanced_prompt
                    )
                    await self.reply(msg, result={"text": analysis, "source": "opscore_crm"})
                    await self.log_daily(f"OPS-CORE CRM查询: GID {gid} (关联: {','.join(related_gids)})",
                                          session_id=msg.session_id, username=(msg.username or ""))
                    return
                else:
                    # CRM 查询失败，但可能BRR有数据
                    await self.send_progress(msg, f"CRM无记录，直接查询 BRR 备份数据...")
                    # 确保GID格式正确
                    gid_search_term = gid if gid.upper().startswith("GID") else f"GID{gid}"
                    brr_jobs = await query_api("brr_gid_jobs", {"search": gid_search_term, "page_size": 50})
                    if "error" not in brr_jobs and brr_jobs.get("results"):
                        full_data = f"=== BRR GID备份作业 (GID {gid}) ===\n{json.dumps(brr_jobs, ensure_ascii=False, indent=1)[:4000]}"
                        analysis = await self.llm_chat(
                            prompt=f"{history_context}用户问题: {prompt}\n\n从 OPS-CORE 平台查询到的数据:\n{full_data}\n\n⚠️ 注意：CRM无记录，这些备份作业来自BRR GID端点搜索。",
                            system=OPSCORE_ANALYSIS_PROMPT + f"\n\n⚠️ 回答时必须使用查询的GID: {gid}，不要引用其他GID的数据！"
                        )
                        await self.reply(msg, result={"text": analysis, "source": "opscore_brr"})
                        return

        # 使用 smart_query 自动路由到正确的 API
        all_domains = list(DOMAIN_ENDPOINTS.keys())
        raw_data = await smart_query(
            question=prompt,
            domains=all_domains,
            llm_func=self.llm_chat,
            max_queries=3
        )

        if raw_data:
            analysis = await self.llm_chat(
                prompt=f"{history_context}用户问题: {prompt}\n\n从 OPS-CORE 平台查询到的数据:\n{raw_data}",
                system=OPSCORE_ANALYSIS_PROMPT
            )
            await self.reply(msg, result={"text": analysis, "source": "opscore_auto"})
            await self.log_daily(f"OPS-CORE 智能查询: {prompt[:50]}", session_id=msg.session_id, username=(msg.username or ""))
        else:
            # 纯知识问答
            result = await self.llm_chat(
                prompt=history_context + prompt,
                system=(
                    "你是 Z-OPS 运维智能体的 OPS-CORE 平台专家。"
                    "熟悉 OPS-CORE 信息管理系统和管理平台的所有功能。"
                    "如果用户询问的是可以从 OPS-CORE 平台查询到的数据，请告知用户你正在查询。"
                    "如果有对话历史，请结合历史上下文理解用户的意图。"
                )
            )
            await self.reply(msg, result={"text": result})

    # ═══ HA 高可用 (198.18.18.166:8088) ═══

    async def _ha_query(self, msg: AgentMessage):
        """HA 查询 — 同时查询历史报告 + 实时告警 + 站点概览"""
        search = msg.params.get("search") or msg.params.get("args") or ""
        site = msg.params.get("site") or ""
        text = msg.text or ""

        ha_client = get_ha_client()
        await self.send_progress(msg, "正在查询 HA 数据...")

        results = {}
        errors = []
        ha_api_ok = False

        # ── 并行查询：历史报告 + 实时告警 + 站点列表 ──
        try:
            # 1. 获取 HA 监控仪表盘（概览 + 近期报告 + 告警分布）
            await self.send_progress(msg, "正在获取 HA 监控仪表盘...")
            dashboard = await ha_client.get_ha_monitor_dashboard()
            if isinstance(dashboard, dict) and "error" not in dashboard:
                results["dashboard"] = dashboard
                ha_api_ok = True
            else:
                errors.append(f"HA 仪表盘: {dashboard.get('error', 'unknown')}")

            # 2. 获取实时活跃告警
            await self.send_progress(msg, "正在获取实时 HA 告警...")
            alerts = await ha_client.get_ha_monitor_alerts()
            if isinstance(alerts, dict) and "error" not in alerts:
                results["active_alerts"] = alerts
                ha_api_ok = True
            else:
                errors.append(f"HA 告警: {alerts.get('error', 'unknown')}")

            # 3. 获取所有 vCenter 站点列表
            await self.send_progress(msg, "正在获取 vCenter 站点列表...")
            sites = await ha_client.get_ha_monitor_sites()
            if isinstance(sites, dict) and "error" not in sites:
                results["sites"] = sites
                ha_api_ok = True
            else:
                errors.append(f"HA 站点: {sites.get('error', 'unknown')}")

            # 4. 如果指定了站点，获取该站点的近期报告 + 最新报告详情
            if site:
                await self.send_progress(msg, f"正在获取 {site} 站点的 HA 报告...")
                reports = await ha_client.get_ha_reports(limit=5, site=site)
                if isinstance(reports, dict) and "error" not in reports:
                    results["reports"] = reports
                    ha_api_ok = True
                    # 获取最新报告详情（含 VM 列表、客户 GID）
                    report_data = reports.get("data", {})
                    report_list = report_data if isinstance(report_data, list) else report_data.get("reports", [])
                    if report_list:
                        latest_report_id = report_list[0].get("id")
                        if latest_report_id:
                            await self.send_progress(msg, f"正在获取报告详情 {latest_report_id}...")
                            report_detail = await ha_client.get_ha_report(latest_report_id)
                            if isinstance(report_detail, dict) and "error" not in report_detail:
                                results["latest_report_detail"] = report_detail
                else:
                    errors.append(f"HA 报告({site}): {reports.get('error', 'unknown')}")
            # 5. 如果有搜索关键词，获取所有报告并过滤
            elif search:
                await self.send_progress(msg, f"正在搜索 HA 报告（关键词：{search}）...")
                reports = await ha_client.get_ha_reports(limit=20)
                if isinstance(reports, dict) and "error" not in reports:
                    results["reports"] = reports
                    ha_api_ok = True
                else:
                    errors.append(f"HA 报告搜索: {reports.get('error', 'unknown')}")
            else:
                # 无具体参数，获取近期报告列表
                await self.send_progress(msg, "正在获取近期 HA 报告...")
                reports = await ha_client.get_ha_reports(limit=10)
                if isinstance(reports, dict) and "error" not in reports:
                    results["reports"] = reports
                    ha_api_ok = True
                    # 如果有报告，获取最新一个的详情
                    report_data = reports.get("data", {})
                    report_list = report_data if isinstance(report_data, list) else report_data.get("reports", [])
                    if report_list:
                        latest_report_id = report_list[0].get("id")
                        if latest_report_id:
                            await self.send_progress(msg, f"正在获取最新报告详情 {latest_report_id}...")
                            report_detail = await ha_client.get_ha_report(latest_report_id)
                            if isinstance(report_detail, dict) and "error" not in report_detail:
                                results["latest_report_detail"] = report_detail
                else:
                    errors.append(f"HA 报告列表: {reports.get('error', 'unknown')}")
        except Exception as e:
            errors.append(f"HA Reports API 异常: {e}")

        # ── Fallback: 如果 HA API 失败，尝试旧版端点 ──
        if not ha_api_ok:
            logger.warning(f"[opscore_agent] HA Reports API 不可用 ({'; '.join(errors)})，尝试旧版端点")
            try:
                if site:
                    vm_data = await ha_client.get_ha_vms(site, vm_name=search or None)
                    if isinstance(vm_data, dict) and "error" not in vm_data:
                        results["ha_vms"] = vm_data
                        ha_api_ok = True
                    else:
                        errors.append(f"HA VM查询({site}): {vm_data.get('error', 'unknown')}")
                else:
                    search_data = await ha_client.search_ha_vms(vm_name=search or None)
                    if isinstance(search_data, dict) and "error" not in search_data:
                        results["ha_vms_search"] = search_data
                        ha_api_ok = True
                    else:
                        errors.append(f"HA VM搜索: {search_data.get('error', 'unknown')}")
            except Exception as e:
                errors.append(f"HA 旧版端点异常: {e}")

        # ── Fallback: 如果所有 HA API 都失败，从 OPS-CORE Smart-Ops 查询 HA 相关事件 ──
        if not ha_api_ok:
            logger.warning(f"[opscore_agent] HA API 完全不可用 ({'; '.join(errors)})，fallback 到 OPS-CORE Smart-Ops 事件")
            await self.send_progress(msg, "HA API 不可用，正在从 OPS-CORE 平台查询 HA 相关事件...")

            # 尝试多个 HA 相关端点
            fallback_data = {}
            for ep_key, ep_label in [
                ("smartops_events", "Smart-Ops 事件"),
                ("smartops_events_critical", "严重事件"),
            ]:
                try:
                    params = {"page_size": 100}
                    if search:
                        params["search"] = search
                    data = await query_api(ep_key, params)
                    if isinstance(data, dict) and "error" not in data:
                        fallback_data[ep_label] = data
                except Exception:
                    pass

            if not fallback_data:
                # 所有数据源都失败
                error_msg = "; ".join(errors)
                if "认证失败" in error_msg or "API Key" in error_msg:
                    await self.reply(msg, error=(
                        f"HA 数据查询失败:\n"
                        f"- HA API (198.18.18.166:8088): 认证失败，请更新 zops.env 中的 HA_API_TOKEN\n"
                        f"- OPS-CORE Smart-Ops: 查询失败"
                    ))
                else:
                    await self.reply(msg, error=f"HA 查询失败: {error_msg}")
                return

            results = fallback_data
            data_source = "OPS-CORE Smart-Ops (包含 HA 相关事件)"
        elif "dashboard" in results:
            data_source = "HA Reports API (198.18.18.166:8088)"
        else:
            data_source = "HA API 旧版端点 (198.18.18.166:8088)"

        # 构建对话历史上下文 (统一使用 BaseAgent 辅助方法)
        ha_history_prefix = await self._build_history_context(msg, max_turns=4, max_content_len=200)

        # 组装数据给 LLM 分析
        raw = json.dumps(results, ensure_ascii=False, indent=2)
        prompt_text = f"{ha_history_prefix}用户问题: {text}\n\n以下是来自 {data_source} 的 vSphere HA 高可用数据:\n\n{raw[:8000]}"
        if errors and ha_api_ok:
            prompt_text += f"\n\n部分查询失败: {'; '.join(errors)}"

        analysis = await self.llm_chat(
            prompt=prompt_text,
            system=(
                "你是 Z-OPS 运维智能体的 HA 高可用分析模块。\n"
                "你通过 HA Reports API 获取 vSphere HA 重启的虚拟机数据。\n"
                "数据包含多个部分：\n"
                "1. **dashboard**: HA 监控仪表盘（总站点数、活跃告警数、近期报告数）\n"
                "2. **active_alerts**: 当前活跃告警列表（包含告警类型、状态、站点）\n"
                "3. **sites**: 所有 vCenter 站点列表（站点 ID、vCenter IP、业务类型）\n"
                "4. **reports**: 近期 HA 报告列表（报告 ID、站点、事件数、时间范围、外部客户数）\n"
                "5. **latest_report_detail**: 最新报告详情（包含：\n"
                "   - summary: 总事件数、唯一客户数、外部客户数、时间范围\n"
                "   - customer_summary: 每个受影响客户的 GID、VM 列表、源/目标主机、集群\n"
                "   - source_host_stats: 源主机统计\n"
                "   - target_host_stats: 目标主机统计）\n"
                "\n"
                "请分析数据，重点关注：\n"
                "1. HA 重启的 VM 列表及所属客户（GID）\n"
                "2. 事件时间范围和分布\n"
                "3. 源主机和目标主机信息\n"
                "4. 受影响的外部客户数量\n"
                "5. 当前是否有活跃告警（如果有，列出告警类型和站点）\n"
                "\n"
                "输出要求：\n"
                "- 用 🔴(异常/失败) 🟡(警告) 🟢(正常) 标注状态\n"
                "- 用表格呈现结构化数据，汇总关键数字\n"
                "- 如果有多个站点，按站点分组展示\n"
                "- 给出具体的处理建议\n"
                "- 如果数据为空或无 HA 事件，明确说明'近期无 HA 事件'"
            )
        )
        source_key = "ha_api" if ha_api_ok else "opscore_mgt"
        await self.reply(msg, result={"text": analysis, "source": source_key})
        await self.log_daily(f"HA 查询({data_source}): site={site}, search={search}",
                             session_id=msg.session_id, username=(msg.username or ""))

    async def _cleanup(self):
        await self.info_api.close()
        await self.mgt_api.close()
        # 关闭 HA API 客户端
        try:
            ha_client = get_ha_client()
            await ha_client.close()
        except Exception:
            pass
        await super()._cleanup()


if __name__ == "__main__":
    run_agent(OpscoreAgent())
