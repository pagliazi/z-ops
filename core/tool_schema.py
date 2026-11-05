"""
Z-OPS Tool Schema — 统一工具定义系统
参考 claude-code Tool.ts + claw-code tools.py/permissions.py

核心设计:
1. ToolDef — 工具定义 (名称/描述/参数Schema/权限/执行函数)
2. ToolExecution — 工具执行结果 (含 token 追踪)
3. ToolPermissionContext — 工具权限上下文 (deny/allow 规则)
4. ToolRegistry — 工具注册表 (发现/查找/过滤/执行)
"""

import json
import time
import logging
import asyncio
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set
from enum import Enum

logger = logging.getLogger("zops")


# ═══ 工具参数 Schema (参考 claude-code ToolInputJSONSchema) ═══

class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass
class ToolParam:
    """工具参数定义 (等价于 JSON Schema property)"""
    name: str = ""
    type: ParamType = ParamType.STRING
    description: str = ""
    required: bool = False
    default: Any = None
    enum: List[str] = field(default_factory=list)  # 可选枚举值

    def to_json_schema(self) -> dict:
        schema = {"type": self.type.value, "description": self.description}
        if self.enum:
            schema["enum"] = self.enum
        if self.default is not None:
            schema["default"] = self.default
        return schema


@dataclass
class ToolInputSchema:
    """工具输入 Schema (等价于 claude-code ToolInputJSONSchema)"""
    params: List[ToolParam] = field(default_factory=list)

    def to_json_schema(self) -> dict:
        """生成 JSON Schema 格式 (LLM function calling 标准)"""
        properties = {}
        required = []
        for p in self.params:
            properties[p.name] = p.to_json_schema()
            if p.required:
                required.append(p.name)
        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def validate(self, params: dict) -> Optional[str]:
        """验证参数"""
        for p in self.params:
            if p.required and p.name not in params:
                return f"缺少必需参数: {p.name}"
            if p.name in params and p.enum and params[p.name] not in p.enum:
                return f"参数 {p.name} 的值必须是: {p.enum}"
        return None


# ═══ 工具权限 (参考 claw-code ToolPermissionContext + claude-code permissions) ═══

class PermissionLevel(str, Enum):
    """工具权限级别"""
    PUBLIC = "public"          # 所有 agent 都能用
    RESTRICTED = "restricted"  # 需要特定 agent 授权
    ADMIN = "admin"            # 仅管理员 agent


class RiskLevel(str, Enum):
    """工具风险级别 (v4.0 借鉴 claude-code 权限管线 + Aurora L4 审批门)

    - read: 只读查询，直接放行
    - write: 有状态变更但可逆/低危，记录放行
    - destructive: 破坏性/不可逆操作，挂起进入审批门
    """
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


# P0: 风险枚举统一规范化 — 字符串/别名/大小写归一到 RiskLevel,
# 消除「字符串化 risk_level 绕过枚举比较」的漏判路径。
RISK_ALIASES: Dict[str, RiskLevel] = {
    "read": RiskLevel.READ, "query": RiskLevel.READ, "view": RiskLevel.READ,
    "ro": RiskLevel.READ, "safe": RiskLevel.READ,
    "write": RiskLevel.WRITE, "update": RiskLevel.WRITE, "change": RiskLevel.WRITE,
    "modify": RiskLevel.WRITE,
    "destructive": RiskLevel.DESTRUCTIVE, "danger": RiskLevel.DESTRUCTIVE,
    "dangerous": RiskLevel.DESTRUCTIVE, "high": RiskLevel.DESTRUCTIVE,
    "critical": RiskLevel.DESTRUCTIVE, "irreversible": RiskLevel.DESTRUCTIVE,
}


def normalize_risk(value) -> RiskLevel:
    """归一化风险值: RiskLevel/字符串别名 → 枚举; 未知值 → WRITE (倾向进权限门, 不放行)"""
    if isinstance(value, RiskLevel):
        return value
    key = str(getattr(value, "value", value) or "").strip().lower()
    if key in RISK_ALIASES:
        return RISK_ALIASES[key]
    if key == "":
        return RiskLevel.READ  # 显式空 = 默认 read (ToolDef 缺省语义)
    logger.warning(f"[tool_schema] 未知 risk_level 值 '{value}', 归一为 WRITE (fail-closed)")
    return RiskLevel.WRITE


@dataclass(frozen=True)
class ToolPermissionContext:
    """
    工具权限上下文 (参考 claw-code ToolPermissionContext)
    
    用于过滤 agent 可用的工具集。
    """
    deny_names: frozenset = field(default_factory=frozenset)     # 禁用的工具名
    deny_prefixes: tuple = ()                                      # 禁用的前缀
    allow_names: frozenset = field(default_factory=frozenset)     # 仅允许的工具名 (空=不限)
    agent_id: str = ""                                             # 所属 agent

    @classmethod
    def from_config(cls, agent_id: str = "",
                    deny: List[str] = None,
                    deny_prefixes: List[str] = None,
                    allow: List[str] = None) -> "ToolPermissionContext":
        return cls(
            deny_names=frozenset(n.lower() for n in (deny or [])),
            deny_prefixes=tuple(p.lower() for p in (deny_prefixes or [])),
            allow_names=frozenset(n.lower() for n in (allow or [])),
            agent_id=agent_id,
        )

    def blocks(self, tool_name: str) -> bool:
        """检查是否阻止此工具"""
        lowered = tool_name.lower()
        if lowered in self.deny_names:
            return True
        if any(lowered.startswith(p) for p in self.deny_prefixes):
            return True
        if self.allow_names and lowered not in self.allow_names:
            return True
        return False


# ═══ 工具定义 (参考 claude-code Tool.ts 的自包含模块) ═══

# 工具执行函数签名
ToolExecuteFn = Callable[..., Coroutine[Any, Any, "ToolExecution"]]


@dataclass
class ToolDef:
    """
    统一工具定义 (参考 claude-code Tool System)
    
    每个工具是自包含模块:
    - 输入 Schema (参数定义)
    - 权限模型 (谁能调用)
    - 执行逻辑 (async function)
    - 描述信息 (LLM 用于工具选择)
    """
    name: str = ""                              # 工具名称 (唯一标识)
    description: str = ""                       # 工具描述 (LLM 可见)
    category: str = ""                          # 分类: opscore/ops/network/vm/general/kb
    input_schema: ToolInputSchema = field(default_factory=ToolInputSchema)
    permission: PermissionLevel = PermissionLevel.PUBLIC
    agent_id: str = ""                          # 所属 agent (空=全局)
    execute_fn: Optional[ToolExecuteFn] = None  # 异步执行函数
    source_hint: str = ""                       # 来源提示 (调试用)
    enabled: bool = True
    timeout_s: int = 0                          # 执行超时 (秒); 0 = 用 config.TOOL_TIMEOUT_DEFAULT
    risk_level: RiskLevel = RiskLevel.READ      # 风险级别 (v4.0 审批门依据; P0: 自动归一化)

    def __post_init__(self):
        # P0: 字符串 risk_level 归一为枚举 — dataclass 不做类型强制,
        # 传 "write"/"DESTRUCTIVE" 等字符串时枚举比较会漏判 (统一契约: 风险枚举规范化)
        if self.risk_level is not None and not isinstance(self.risk_level, RiskLevel):
            self.risk_level = normalize_risk(self.risk_level)

    def effective_timeout(self) -> int:
        """解析生效超时: 工具自定义 > 慢工具名单 > 全局默认"""
        if self.timeout_s > 0:
            return self.timeout_s
        try:
            import config as _config
            lowered = self.name.lower()
            if any(k in lowered for k in ("ssh", "batch", "exec", "ansible", "snapshot", "migrate")):
                return _config.TOOL_TIMEOUT_SLOW
            return _config.TOOL_TIMEOUT_DEFAULT
        except Exception:
            return 60

    def to_function_spec(self) -> dict:
        """生成 LLM function calling 规范 (OpenAI/DeepSeek 兼容)"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema.to_json_schema(),
            }
        }

    def to_summary(self) -> str:
        """LLM 可读的工具摘要"""
        params_desc = ", ".join(
            f"{p.name}{'*' if p.required else ''}: {p.description}"
            for p in self.input_schema.params
        )
        return f"- {self.name}: {self.description} ({params_desc})"


# ═══ 工具执行结果 (参考 claw-code ToolExecution + claude-code TurnResult) ═══

@dataclass
class ToolExecution:
    """工具执行结果"""
    tool_name: str = ""
    success: bool = True
    output: str = ""                    # 执行输出 (LLM 可见)
    data: Dict[str, Any] = field(default_factory=dict)  # 结构化数据
    error: str = ""
    execution_time: float = 0.0         # 执行耗时 (秒)
    input_tokens: int = 0               # 输入 token 估算
    output_tokens: int = 0              # 输出 token 估算
    permission_denied: bool = False     # 是否被权限拒绝
    source: str = ""                    # 来源 agent

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}


# ═══ 工具注册表 (参考 claw-code tools.py + claude-code tools.ts) ═══

class ToolRegistry:
    """
    全局工具注册表
    
    参考:
    - claude-code tools.ts: 注册/发现/过滤工具
    - claw-code tools.py: get_tools() + filter_tools_by_permission_context()
    - claw-code query_engine.py: submit_message() 中 matched_tools
    """

    def __init__(self):
        self._tools: Dict[str, ToolDef] = {}

    def register(self, tool: ToolDef):
        """注册工具"""
        if not tool.name:
            raise ValueError("工具名称不能为空")
        self._tools[tool.name] = tool
        logger.debug(f"[tool_registry] 注册: {tool.name} ({tool.category})")

    def register_many(self, tools: List[ToolDef]):
        for t in tools:
            self.register(t)

    def get(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    def get_tools(self, category: str = "",
                  agent_id: str = "",
                  permission_context: ToolPermissionContext = None,
                  enabled_only: bool = True) -> List[ToolDef]:
        """
        获取工具列表 (支持过滤)
        参考 claw-code get_tools() + filter_tools_by_permission_context()
        """
        tools = list(self._tools.values())

        if enabled_only:
            tools = [t for t in tools if t.enabled]
        if category:
            tools = [t for t in tools if t.category == category]
        if agent_id:
            tools = [t for t in tools if not t.agent_id or t.agent_id == agent_id]
        if permission_context:
            tools = [t for t in tools if not permission_context.blocks(t.name)]

        return tools

    def find_tools(self, query: str, limit: int = 10) -> List[ToolDef]:
        """搜索工具 (参考 claw-code find_tools)"""
        needle = query.lower()
        matches = [
            t for t in self._tools.values()
            if needle in t.name.lower() or needle in t.description.lower()
        ]
        return matches[:limit]

    def get_function_specs(self, agent_id: str = "",
                           permission_context: ToolPermissionContext = None) -> List[dict]:
        """生成 LLM function calling 规范列表"""
        tools = self.get_tools(agent_id=agent_id, permission_context=permission_context)
        return [t.to_function_spec() for t in tools]

    def get_tool_summary(self, agent_id: str = "") -> str:
        """生成工具摘要 (注入 LLM system prompt)"""
        tools = self.get_tools(agent_id=agent_id)
        if not tools:
            return ""
        lines = ["你可以使用以下工具:"]
        for t in tools:
            lines.append(t.to_summary())
        return "\n".join(lines)

    async def execute_tool(self, name: str, params: dict = None,
                           context: dict = None,
                           permission_context: ToolPermissionContext = None) -> ToolExecution:
        """
        执行工具 (参考 claw-code execute_tool + claude-code tool execution)
        """
        tool = self.get(name)
        if not tool:
            return ToolExecution(
                tool_name=name, success=False,
                error=f"未知工具: {name}", permission_denied=False
            )

        # 权限检查
        if permission_context and permission_context.blocks(name):
            return ToolExecution(
                tool_name=name, success=False,
                error=f"权限拒绝: {name}", permission_denied=True
            )

        # 参数验证
        params = params or {}
        validation_error = tool.input_schema.validate(params)
        if validation_error:
            return ToolExecution(
                tool_name=name, success=False, error=validation_error
            )

        # 执行 (v4.0: per-tool 超时隔离 — 借鉴 HolmesGPT toolsets，超时返回结构化结果而非异常)
        if not tool.execute_fn:
            return ToolExecution(
                tool_name=name, success=False,
                error=f"工具 {name} 未实现执行函数"
            )

        start = time.time()
        try:
            result = await asyncio.wait_for(
                tool.execute_fn(params=params, context=context or {}),
                timeout=tool.effective_timeout(),
            )
            result.execution_time = time.time() - start
            result.tool_name = name
            return result
        except asyncio.TimeoutError:
            budget = tool.effective_timeout()
            logger.warning(f"[tool_registry] 工具 {name} 执行超时 ({budget}s)，返回结构化超时结果")
            return ToolExecution(
                tool_name=name, success=False,
                output=json.dumps({
                    "timeout": True, "tool": name, "budget_s": budget,
                    "advice": "工具执行超时，请换一种查询方式或缩小范围后重试",
                }, ensure_ascii=False),
                error=f"timeout after {budget}s",
                execution_time=time.time() - start,
            )
        except Exception as e:
            return ToolExecution(
                tool_name=name, success=False,
                error=str(e), execution_time=time.time() - start
            )

    @property
    def count(self) -> int:
        return len(self._tools)

    def list_names(self) -> List[str]:
        return list(self._tools.keys())


# ═══ 全局单例 ═══

_tool_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """获取全局 ToolRegistry 单例"""
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
    return _tool_registry
