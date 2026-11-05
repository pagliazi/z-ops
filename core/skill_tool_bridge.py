"""
Z-OPS Skill → Tool Bridge — 技能工具化桥接层
参考 claude-code SkillTool + claw-code tools.py

核心功能:
1. 将现有 BaseSkill 自动转换为 ToolDef (保持向后兼容)
2. 将 Agent action handler 注册为 ToolDef (函数式工具)
3. 统一 ToolRegistry 管理所有工具 (Skills + Agent Actions)
4. 提供 agent_tools_from_handlers() 快捷注册
"""

import logging
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional

from core.tool_schema import (
    ToolDef, ToolExecution, ToolInputSchema, ToolParam,
    ToolRegistry, ParamType, PermissionLevel, get_tool_registry,
)
from core.skill_base import BaseSkill, SkillParam, SkillResult

logger = logging.getLogger("zops")


# ═══ Skill → ToolDef 转换 ═══

def _skill_param_type_to_param_type(skill_type: str) -> ParamType:
    """SkillParam.type → ParamType 映射"""
    mapping = {
        "string": ParamType.STRING,
        "str": ParamType.STRING,
        "int": ParamType.INTEGER,
        "integer": ParamType.INTEGER,
        "float": ParamType.NUMBER,
        "number": ParamType.NUMBER,
        "bool": ParamType.BOOLEAN,
        "boolean": ParamType.BOOLEAN,
        "list": ParamType.ARRAY,
        "array": ParamType.ARRAY,
        "dict": ParamType.OBJECT,
        "object": ParamType.OBJECT,
    }
    return mapping.get(skill_type.lower(), ParamType.STRING)


def skill_to_tool(skill: BaseSkill, agent_id: str = "", category: str = "") -> ToolDef:
    """
    将 BaseSkill 转换为 ToolDef

    参考 claude-code SkillTool: 每个 Skill 变成一个自包含的 Tool，
    具有完整的 inputSchema + execute 逻辑。

    Args:
        skill: BaseSkill 实例
        agent_id: 所属 agent
        category: 工具分类 (空则使用 skill domain)
    """
    # 转换参数
    tool_params = []
    for sp in (skill.parameters or []):
        if isinstance(sp, SkillParam):
            tool_params.append(ToolParam(
                name=sp.name,
                type=_skill_param_type_to_param_type(sp.type),
                description=sp.description,
                required=sp.required,
                default=sp.default,
            ))

    # 构建执行函数 (包裹 Skill 的 execute_with_learning)
    async def _execute_fn(params: dict = None, context: dict = None) -> ToolExecution:
        start = time.time()
        try:
            result: SkillResult = await skill.execute_with_learning(
                params=params or {}, context=context or {}
            )
            return ToolExecution(
                tool_name=skill.name,
                success=result.success,
                output=result.text or str(result.data),
                data=result.data,
                error=result.error,
                execution_time=time.time() - start,
                source=agent_id,
            )
        except Exception as e:
            return ToolExecution(
                tool_name=skill.name,
                success=False,
                error=str(e),
                execution_time=time.time() - start,
                source=agent_id,
            )

    return ToolDef(
        name=skill.name,
        description=skill.description,
        category=category or getattr(skill, 'domain', '') or agent_id,
        input_schema=ToolInputSchema(params=tool_params),
        permission=PermissionLevel.PUBLIC,
        agent_id=agent_id,
        execute_fn=_execute_fn,
        source_hint=f"skill:{skill.__class__.__name__}",
        enabled=True,
    )


def register_skills_as_tools(skills: Dict[str, BaseSkill],
                              agent_id: str = "",
                              category: str = "",
                              registry: ToolRegistry = None):
    """
    批量将 Skills 注册到 ToolRegistry

    Args:
        skills: {skill_name: BaseSkill} 字典
        agent_id: 所属 agent
        category: 工具分类
        registry: ToolRegistry (默认使用全局单例)
    """
    reg = registry or get_tool_registry()
    count = 0
    for name, skill in skills.items():
        try:
            tool = skill_to_tool(skill, agent_id=agent_id, category=category)
            reg.register(tool)
            count += 1
        except Exception as e:
            logger.warning(f"[skill_tool_bridge] 注册 skill {name} 失败: {e}")
    logger.info(f"[skill_tool_bridge] 注册 {count} 个 skill → tool (agent={agent_id})")
    return count


# ═══ Agent Action Handler → ToolDef 转换 ═══

def action_to_tool(
    name: str,
    description: str,
    handler_fn: Callable,
    params: List[ToolParam] = None,
    agent_id: str = "",
    category: str = "",
    permission: PermissionLevel = PermissionLevel.PUBLIC,
) -> ToolDef:
    """
    将 Agent action handler 转换为 ToolDef

    参考 claude-code AgentTool: 将 agent 内部方法暴露为标准化工具。

    Args:
        name: 工具名
        description: 描述
        handler_fn: async handler(params, context) → ToolExecution
        params: 参数列表
        agent_id: 所属 agent
        category: 分类
    """
    async def _execute_fn(params: dict = None, context: dict = None) -> ToolExecution:
        start = time.time()
        try:
            result = await handler_fn(params=params or {}, context=context or {})
            if isinstance(result, ToolExecution):
                result.execution_time = time.time() - start
                return result
            elif isinstance(result, SkillResult):
                return ToolExecution(
                    tool_name=name,
                    success=result.success,
                    output=result.text or str(result.data),
                    data=result.data,
                    error=result.error,
                    execution_time=time.time() - start,
                    source=agent_id,
                )
            elif isinstance(result, dict):
                return ToolExecution(
                    tool_name=name,
                    success=not result.get("error"),
                    output=result.get("text", str(result)),
                    data=result,
                    execution_time=time.time() - start,
                    source=agent_id,
                )
            else:
                return ToolExecution(
                    tool_name=name,
                    success=True,
                    output=str(result),
                    execution_time=time.time() - start,
                    source=agent_id,
                )
        except Exception as e:
            return ToolExecution(
                tool_name=name,
                success=False,
                error=str(e),
                execution_time=time.time() - start,
                source=agent_id,
            )

    return ToolDef(
        name=name,
        description=description,
        category=category or agent_id,
        input_schema=ToolInputSchema(params=params or []),
        permission=permission,
        agent_id=agent_id,
        execute_fn=_execute_fn,
        source_hint=f"agent_action:{agent_id}.{name}",
        enabled=True,
    )


def agent_tools_from_handlers(
    handlers: Dict[str, dict],
    agent_id: str = "",
    category: str = "",
    registry: ToolRegistry = None,
) -> int:
    """
    批量注册 Agent action handlers 为 ToolDef

    用法:
        agent_tools_from_handlers({
            "backup_status": {
                "description": "查询备份状态",
                "handler": self._backup_status_tool,
                "params": [ToolParam(name="search", ...)],
            },
        }, agent_id="opscore_agent")

    Args:
        handlers: {action_name: {"description", "handler", "params"}} 字典
        agent_id: 所属 agent
        category: 分类
        registry: ToolRegistry
    """
    reg = registry or get_tool_registry()
    count = 0
    for action_name, cfg in handlers.items():
        try:
            tool = action_to_tool(
                name=f"{agent_id}.{action_name}" if agent_id else action_name,
                description=cfg.get("description", action_name),
                handler_fn=cfg["handler"],
                params=cfg.get("params", []),
                agent_id=agent_id,
                category=category or agent_id,
                permission=cfg.get("permission", PermissionLevel.PUBLIC),
            )
            reg.register(tool)
            count += 1
        except Exception as e:
            logger.warning(f"[skill_tool_bridge] 注册 action {action_name} 失败: {e}")
    logger.info(f"[skill_tool_bridge] 注册 {count} 个 agent action → tool (agent={agent_id})")
    return count


# ═══ 自动发现并注册所有 Skills ═══

def auto_register_all_skills(registry: ToolRegistry = None) -> int:
    """
    扫描 SkillRegistry 中所有已注册的 Skills，自动转为 ToolDef 并注册。

    通常在系统启动时调用一次。
    """
    try:
        from core.skill_registry import get_skill_registry
        skill_reg = get_skill_registry()
        skills = skill_reg.list_all()
        if not skills:
            logger.info("[skill_tool_bridge] SkillRegistry 中无已注册 Skills")
            return 0
        return register_skills_as_tools(skills, registry=registry)
    except Exception as e:
        logger.warning(f"[skill_tool_bridge] 自动注册失败: {e}")
        return 0
