"""Z-OPS 统一能力注册表 — 前端可发现并调用全部系统能力

设计:
  /api/capabilities 返回所有可用能力 (agent 对话/工具调用/脚本执行/系统能力),
  前端据此渲染能力中心 + 命令面板 + ChatApp 能力块。

能力类型:
  chat      → 通过对话触发 (发送消息给指定 agent)
  tool      → 直接调用工具 (HTTP API)
  script    → 执行登记脚本 (受限沙箱)
  browser   → Computer Use 浏览器自动化
  system    → 系统管理 (admin API)
"""
import logging
from typing import Optional

import config

logger = logging.getLogger("zops")


async def get_all_capabilities(redis_client=None) -> dict:
    """返回全部可调用能力 (按分类分组)"""
    from core.script_exec import load_registry as load_scripts
    from core.bizperms import BIZ_PERMS, effective_perms

    caps = {
        "agents": [],
        "tools": [],
        "scripts": [],
        "system": [],
    }

    # ── 1. Agent 对话能力 ──
    agent_catalog = [
        {"id": "orchestrator", "name": "智能调度", "icon": "Sparkles",
         "description": "自动路由到最合适的专家", "type": "chat",
         "example": "帮我查一下 HKG 站点的备份状态"},
        {"id": "opscore_agent", "name": "OPS-CORE 数据查询", "icon": "Database",
         "description": "VM/备份/CMDB 实时生产数据", "type": "chat",
         "example": "查 GID 47141 的虚拟机列表"},
        {"id": "ops_agent", "name": "运维诊断", "icon": "Terminal",
         "description": "主机检查/日志分析/健康报告", "type": "chat",
         "example": "检查 198.18.96.169 的连通性"},
        {"id": "network_agent", "name": "网络分析", "icon": "Network",
         "description": "网络拓扑/连通性检测/配置审计", "type": "chat",
         "example": "测试到 10.0.0.1 的路由追踪"},
        {"id": "vm_agent", "name": "虚拟机管理", "icon": "Server",
         "description": "VMware/KVM/PVE 虚拟化操作", "type": "chat",
         "example": "查询 HKG 站点所有 VM 的快照状态"},
        {"id": "dev_agent", "name": "脚本执行", "icon": "Code",
         "description": "脚本生成与登记执行 (受限沙箱)", "type": "chat",
         "example": "执行 backup_check 脚本 --gid 47141"},
        {"id": "computer_agent", "name": "Computer Use", "icon": "Monitor",
         "description": "浏览器自动操作/网页任务/表单填写", "type": "browser",
         "example": "用浏览器打开 https://example.com 告诉我标题"},
        {"id": "kb_agent", "name": "知识库", "icon": "BookOpen",
         "description": "内部知识库检索问答", "type": "chat",
         "example": "搜索备份策略相关知识"},
        {"id": "general_agent", "name": "通用问答", "icon": "MessageSquare",
         "description": "通用问答与知识增强", "type": "chat",
         "example": "PowerShell 怎么查看进程"},
    ]
    caps["agents"] = agent_catalog

    # ── 2. 可直接调用的工具 (HTTP API) ──
    tools = [
        {"id": "nms_status", "name": "NMS 告警状态", "description": "获取当前告警摘要",
         "type": "tool", "method": "GET", "path": "/api/nms/status",
         "perm": "", "example": None},
        {"id": "nms_refresh", "name": "刷新 NMS 告警", "description": "立即采集一轮 NMS 告警",
         "type": "tool", "method": "POST", "path": "/api/nms/refresh", "perm": ""},
        {"id": "approval_stats", "name": "审批统计", "description": "获取审批中心统计数据",
         "type": "tool", "method": "GET", "path": "/api/approvals/stats", "perm": ""},
        {"id": "memory_search", "name": "搜索我的记忆", "description": "跨会话记忆全文搜索",
         "type": "tool", "method": "GET", "path": "/api/memory/search?q={query}", "perm": ""},
        {"id": "session_search", "name": "搜索历史会话", "description": "FTS5 全文搜索会话消息",
         "type": "tool", "method": "GET", "path": "/api/memory/sessions/search?q={query}", "perm": ""},
        {"id": "wiki_search", "name": "Wiki 知识搜索", "description": "LLM-Wiki 知识检索",
         "type": "tool", "method": "GET", "path": "/api/agents/llm-wiki/search?q={query}", "perm": "knowledge.manage"},
        {"id": "notify_test", "name": "通知测试", "description": "发送测试通知",
         "type": "tool", "method": "POST", "path": "/api/notify/test/feishu", "perm": "notify.manage"},
    ]
    caps["tools"] = tools

    # ── 3. 登记脚本 (受限执行) ──
    try:
        scripts = load_scripts()
        caps["scripts"] = [
            {"id": s["name"], "name": s["name"], "description": f"脚本 {s['file']} ({s['type']})",
             "type": "script", "params": s.get("params_schema", []),
             "hosts": s.get("hosts", ["localhost"]),
             "platform": s.get("platform", ["linux"]),
             "permissions": s.get("permissions", [s.get("risk", "read")]),
             "version": s.get("version", 1), "digest": s.get("sha256"),
             "cancel": s.get("cancel", {"grace_s": 5}),
             "target_dir_count": len(s.get("target_dirs", [])),
             "example": f"执行 {s['name']}"}
            for s in scripts
        ]
    except Exception:
        caps["scripts"] = []

    # ── 4. 系统能力 (admin) ──
    system_caps = [
        {"id": "sys_overview", "name": "系统概览", "description": "Agent 状态/模型/版本",
         "type": "system", "method": "GET", "path": "/api/overview", "perm": ""},
        {"id": "sys_health", "name": "健康检查", "description": "各组件 readiness",
         "type": "system", "method": "GET", "path": "/api/health", "perm": ""},
        {"id": "sys_audit", "name": "审计日志", "description": "最近操作审计",
         "type": "system", "method": "GET", "path": "/api/audit/recent", "perm": "admin"},
        {"id": "sys_usage", "name": "用量统计", "description": "LLM Token 用量与调用次数",
         "type": "system", "method": "GET", "path": "/api/usage", "perm": "observability.view"},
        {"id": "sys_tasks", "name": "任务运行时", "description": "任务队列/租约/Outbox 状态",
         "type": "system", "method": "GET", "path": "/api/admin/tasks/stats", "perm": "admin"},
        {"id": "sys_skills", "name": "技能生命周期", "description": "候选/审批/发布/回退",
         "type": "system", "method": "GET", "path": "/api/skills/lifecycle", "perm": "admin"},
        {"id": "sys_cu_config", "name": "Computer Use 配置", "description": "域名白名单/预算/会话",
         "type": "system", "method": "GET", "path": "/api/admin/computer-use/config", "perm": "admin"},
    ]
    caps["system"] = system_caps

    return caps


async def get_capability_detail(cap_id: str, cap_type: str) -> Optional[dict]:
    """获取单个能力的详细信息"""
    all_caps = await get_all_capabilities()
    for group in all_caps.values():
        for cap in group:
            if cap["id"] == cap_id and (not cap_type or cap["type"] == cap_type):
                return cap
    return None
