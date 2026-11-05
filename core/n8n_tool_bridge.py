"""
N8N 工作流 → ToolDef 桥接层
将 WORKFLOW_REGISTRY 中的工作流注册为统一 ToolDef，
使 Agent 可通过 ToolRegistry 触发 n8n 工作流。

参考 claw-code 的 ToolUse 架构 + claude-code AgentTool 模式。
"""

import logging
from typing import Dict, Any

from core.tool_schema import (
    ToolDef, ToolInputSchema, ToolParam, ParamType,
    PermissionLevel, get_tool_registry
)

logger = logging.getLogger("zops.n8n_tool_bridge")


def register_n8n_workflow_tools(registry=None):
    """
    将 n8n_workflows.WORKFLOW_REGISTRY 中的工作流
    注册为 ToolDef，关联到 ops_agent / opscore_agent
    """
    if registry is None:
        registry = get_tool_registry()

    from core.n8n_workflows import WORKFLOW_REGISTRY

    # 工作流分类 → Agent 映射
    CATEGORY_AGENT_MAP = {
        "vm": "ops_agent",
        "opscore": "opscore_agent",
        "ad": "ops_agent",
        "notification": "orchestrator",
        "report": "orchestrator",
        "ops": "ops_agent",
        "approval": "orchestrator",
        "event": "ops_agent",
    }

    registered = 0

    for wf_id, wf_def in WORKFLOW_REGISTRY.items():
        category = wf_def.get("category", "ops")
        agent_id = CATEGORY_AGENT_MAP.get(category, "ops_agent")
        params = wf_def.get("params", [])
        require_approval = wf_def.get("require_approval", False)

        # 构建参数 schema
        tool_params = [
            ToolParam(
                name=p,
                type=ParamType.STRING,
                description=f"{wf_def['name']} 参数: {p}",
                required=(p not in ("sync_mode", "check_items", "threshold", "force")),
            )
            for p in params
        ]

        # 权限级别
        permission = PermissionLevel.ADMIN if require_approval else PermissionLevel.PUBLIC

        # 创建 ToolDef (execute_fn 是异步函数)
        tool_name = f"n8n.{wf_id}"
        td = ToolDef(
            name=tool_name,
            description=f"[N8N工作流] {wf_def['name']}: {wf_def['description']}",
            agent_id=agent_id,
            category=f"n8n_{category}",
            input_schema=ToolInputSchema(params=tool_params),
            permission=permission,
            execute_fn=_make_n8n_executor(wf_id),
        )

        registry.register(td)
        registered += 1

    logger.info(f"[N8N Bridge] 注册 {registered} 个 n8n 工作流为 ToolDef")
    return registered


def _make_n8n_executor(workflow_id: str):
    """
    为每个工作流创建一个 executor 闭包
    调用时通过 N8NClient.trigger_by_registry 触发
    """
    async def _execute(params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from core.n8n_integration import get_n8n_integration
            n8n = await get_n8n_integration()
            result = await n8n.trigger_by_registry(workflow_id, params)
            return result
        except Exception as e:
            logger.error(f"[N8N Bridge] 执行工作流 {workflow_id} 失败: {e}")
            return {"success": False, "error": str(e)}

    return _execute


def get_n8n_tool_count() -> int:
    """获取已注册的 n8n 工具数"""
    registry = get_tool_registry()
    return len([t for t in registry.get_tools() if t.name.startswith("n8n.")])


# ═══ 供外部调用：一键注册所有 n8n 工具 ═══

def auto_register_n8n_tools():
    """自动注册所有 n8n 工作流为工具（供 agent 启动时调用）"""
    try:
        count = register_n8n_workflow_tools()
        logger.info(f"[N8N Bridge] 自动注册完成: {count} 个工具")
        return count
    except Exception as e:
        logger.error(f"[N8N Bridge] 自动注册失败: {e}")
        return 0
