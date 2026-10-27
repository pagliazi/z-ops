"""
Z-OPS v4 — Tool System (自描述工具 + 14 步执行管线)

参考 Claude Code / Claw Code 的工具系统设计:
- 每个工具自描述: JSON Schema + 权限 + 并发安全性
- 14 步执行管线 (checkPermissionsAndCallTool)
- 工具注册中心 (ToolPool)
- 失败安全默认值 (fail-closed)

核心接口:
    @tool(name="host_check", description="检查主机健康状态")
    class HostCheckTool(BaseTool):
        input_schema = {...}
        is_concurrency_safe = True
        permission = "read_only"
        
        async def call(self, ctx, **kwargs):
            ...
"""

import json
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type

logger = logging.getLogger("zops")


# ═══ 权限级别 ═══

class PermissionLevel(str, Enum):
    """工具权限级别 (参考 Claude Code 7 种模式)"""
    READ_ONLY = "read_only"          # 只读，自动允许
    WRITE = "write"                  # 写操作，可能需要确认
    DESTRUCTIVE = "destructive"      # 破坏性操作，需要确认
    ADMIN = "admin"                  # 管理员权限
    BUBBLE = "bubble"                # 子 agent 上抛给父 agent


# ═══ 工具执行上下文 ═══

@dataclass
class ToolContext:
    """工具执行上下文"""
    user: str = ""                   # 执行用户
    session_id: str = ""             # 会话 ID
    trace_id: str = ""               # 追踪 ID
    permission_mode: str = "default" # 权限模式: default / auto / plan / bypass
    abort_flag: bool = False         # 中断标志


# ═══ 工具结果 ═══

@dataclass
class ToolResult:
    """工具执行结果"""
    tool_call_id: str
    tool_name: str
    success: bool
    output: str                      # 工具输出 (字符串)
    error: Optional[str] = None      # 错误信息
    duration_ms: float = 0.0         # 执行耗时
    new_messages: List[Dict] = field(default_factory=list)  # 附加消息 (如子 agent 转录)


# ═══ 基础工具类 ═══

class BaseTool(ABC):
    """
    工具基类 — 所有工具必须继承此类
    
    子类需实现:
    - name: 工具名
    - description: 工具描述 (LLM 用来选择工具)
    - input_schema: JSON Schema (LLM 用来生成参数)
    - call(): 执行逻辑
    """
    
    name: str = ""
    description: str = ""
    input_schema: Dict = {}
    is_concurrency_safe: bool = False    # 是否可并发执行 (默认串行，失败安全)
    permission: PermissionLevel = PermissionLevel.READ_ONLY
    is_read_only: bool = False           # 是否只读
    is_destructive: bool = False         # 是否破坏性
    should_defer: bool = False           # 是否延迟加载 (减小初始 prompt)
    
    @abstractmethod
    async def call(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """执行工具逻辑"""
        pass
    
    def get_tool_definition(self) -> Dict:
        """获取 OpenAI tool schema (供 LLM tool_call 使用)"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema or {
                    "type": "object",
                    "properties": {},
                },
            },
        }
    
    def check_permission(self, ctx: ToolContext, parsed_input: Dict) -> Dict:
        """
        检查权限 (14 步管线的第 8 步)
        
        返回:
            {"behavior": "allow" | "deny" | "ask" | "passthrough", "reason": ""}
        """
        if ctx.permission_mode == "bypass":
            return {"behavior": "allow", "reason": "权限绕过"}
        
        if self.is_read_only and ctx.permission_mode != "plan":
            return {"behavior": "allow", "reason": "只读工具自动允许"}
        
        if self.permission == PermissionLevel.READ_ONLY:
            return {"behavior": "allow", "reason": "只读工具"}
        
        if ctx.permission_mode == "plan":
            return {"behavior": "deny", "reason": "规划模式禁止执行"}
        
        if self.is_destructive and ctx.permission_mode != "admin":
            return {"behavior": "ask", "reason": "破坏性操作需要确认"}
        
        return {"behavior": "passthrough", "reason": "使用默认策略"}
    
    def validate_input(self, parsed_input: Dict) -> Optional[str]:
        """
        语义验证 (14 步管线的第 4 步)
        返回错误信息，None 表示通过
        """
        # 默认: 无额外验证
        return None


# ═══ 工具注册中心 ═══

class ToolPool:
    """
    工具注册中心 — 管理所有已注册工具
    
    使用方式:
        pool = ToolPool()
        pool.register(HostCheckTool())
        pool.register(LogQueryTool())
        
        # 获取所有工具定义 (供 LLM 选择)
        tools = pool.get_all_definitions()
        
        # 执行工具
        result = await pool.execute("host_check", ctx, hostname="server01")
    """
    
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        self._aliases: Dict[str, str] = {}  # 别名映射 (旧 transcript 兼容)
    
    def register(self, tool: BaseTool):
        """注册工具"""
        if not tool.name:
            raise ValueError(f"工具必须设置 name 属性: {tool.__class__}")
        if tool.name in self._tools:
            logger.warning(f"[tool_pool] 工具 {tool.name} 已存在，将被覆盖")
        self._tools[tool.name] = tool
        logger.info(f"[tool_pool] 注册工具: {tool.name} (并发安全={tool.is_concurrency_safe}, 权限={tool.permission.value})")
    
    def get(self, name: str) -> Optional[BaseTool]:
        """获取工具 (支持别名)"""
        # 先查别名
        real_name = self._aliases.get(name, name)
        return self._tools.get(real_name)
    
    def has(self, name: str) -> bool:
        return self.get(name) is not None
    
    def get_all_definitions(self) -> List[Dict]:
        """获取所有工具定义 (供 LLM tool_call 使用)"""
        # 排序: builtins 先，MCP 后 (保持 cache 稳定)
        sorted_tools = sorted(self._tools.values(), key=lambda t: t.name)
        return [t.get_tool_definition() for t in sorted_tools]
    
    def get_concurrency_safe(self, tool_calls: List[Dict]) -> List[Dict]:
        """筛选可并发执行的工具调用"""
        safe = []
        for tc in tool_calls:
            tool = self.get(tc.get("function", {}).get("name", ""))
            if tool and tool.is_concurrency_safe:
                safe.append(tc)
        return safe
    
    def set_alias(self, alias: str, real_name: str):
        """设置工具别名 (用于旧 transcript 兼容)"""
        self._aliases[alias] = real_name
    
    async def execute(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: Dict,
        ctx: ToolContext,
    ) -> ToolResult:
        """
        执行工具 (简化版 14 步管线)
        
        完整管线 (Phase 2 详细实现):
        1.  工具查找 (含别名)
        2.  中断检查
        3.  Schema 验证 (Zod)
        4.  语义验证
        5.  推测性分类器启动
        6.  输入回填 (~/foo → 绝对路径)
        7.  PreToolUse Hooks
        8.  权限解析
        9.  权限拒绝路径
        10. 执行 call()
        11. 结果预算控制
        12. PostToolUse Hooks
        13. 附加新消息
        14. 错误分类
        """
        start_time = time.time()
        
        # Step 1: 工具查找
        tool = self.get(tool_name)
        if not tool:
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                success=False,
                output=f"工具 '{tool_name}' 未注册",
                error="tool_not_found",
                duration_ms=(time.time() - start_time) * 1000,
            )
        
        # Step 2: 中断检查
        if ctx.abort_flag:
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                success=False,
                output="执行被中断",
                error="aborted",
                duration_ms=(time.time() - start_time) * 1000,
            )
        
        # Step 7: PreToolUse Hooks (Phase 4 详细实现)
        # TODO: 调用 hook 系统
        
        # Step 8: 权限检查
        perm_result = tool.check_permission(ctx, arguments)
        if perm_result["behavior"] == "deny":
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                success=False,
                output=f"权限拒绝: {perm_result['reason']}",
                error="permission_denied",
                duration_ms=(time.time() - start_time) * 1000,
            )
        
        # Step 10: 执行
        try:
            result = await tool.call(ctx, **arguments)
            result.duration_ms = (time.time() - start_time) * 1000
            return result
        except Exception as e:
            logger.error(f"[tool_pool] 工具执行失败 {tool_name}: {e}", exc_info=True)
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                success=False,
                output=f"执行失败: {e}",
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )
        
        # Step 12: PostToolUse Hooks (Phase 4 详细实现)
        # TODO: 调用 hook 系统
    
    def list_tools(self) -> List[Dict]:
        """列出所有已注册工具 (调试用)"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "concurrency_safe": t.is_concurrency_safe,
                "permission": t.permission.value,
                "read_only": t.is_read_only,
                "defer": t.should_defer,
            }
            for t in self._tools.values()
        ]


# ═══ 工具装饰器 (便捷注册) ═══

def tool(name: str, description: str, **kwargs):
    """
    工具装饰器 — 自动注册工具到全局池
    
    用法:
        @tool(name="host_check", description="检查主机健康状态")
        class HostCheckTool(BaseTool):
            input_schema = {"hostname": {"type": "string"}}
            is_concurrency_safe = True
            
            async def call(self, ctx, hostname):
                return ToolResult(...)
    """
    def decorator(cls):
        cls.name = name
        cls.description = description
        for k, v in kwargs.items():
            setattr(cls, k, v)
        
        # 自动注册到全局池
        _GLOBAL_TOOL_POOL.register(cls())
        return cls
    return decorator


# 全局工具池
_GLOBAL_TOOL_POOL = ToolPool()


def get_tool_pool() -> ToolPool:
    """获取全局工具池"""
    return _GLOBAL_TOOL_POOL


# ═══ 内置运维工具示例 ═══

@tool(
    name="query_kb",
    description="查询知识库，搜索运维文档和最佳实践",
    is_concurrency_safe=True,
    permission=PermissionLevel.READ_ONLY,
    is_read_only=True,
)
class QueryKBTool(BaseTool):
    """知识库查询工具"""
    
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "limit": {"type": "integer", "description": "返回结果数量", "default": 5},
        },
        "required": ["query"],
    }
    
    async def call(self, ctx: ToolContext, query: str, limit: int = 5) -> ToolResult:
        """执行知识库查询"""
        try:
            from agents.kb_agent import kb_search, format_kb_results
            results = await kb_search(query, limit=limit)
            if results:
                output = format_kb_results(results)
                return ToolResult(
                    tool_call_id="",
                    tool_name="query_kb",
                    success=True,
                    output=f"找到 {len(results)} 条结果:\n{output}",
                )
            else:
                return ToolResult(
                    tool_call_id="",
                    tool_name="query_kb",
                    success=True,
                    output=f"知识库中未找到与 '{query}' 相关的内容",
                )
        except Exception as e:
            return ToolResult(
                tool_call_id="",
                tool_name="query_kb",
                success=False,
                output=f"查询失败: {e}",
                error=str(e),
            )


@tool(
    name="check_host",
    description="检查主机健康状态 (CPU/内存/磁盘/网络)",
    is_concurrency_safe=True,
    permission=PermissionLevel.READ_ONLY,
    is_read_only=True,
)
class CheckHostTool(BaseTool):
    """主机健康检查工具"""
    
    input_schema = {
        "type": "object",
        "properties": {
            "hostname": {"type": "string", "description": "主机名或 IP"},
            "check_type": {
                "type": "string",
                "description": "检查类型: all / cpu / memory / disk / network",
                "default": "all",
            },
        },
        "required": ["hostname"],
    }
    
    async def call(self, ctx: ToolContext, hostname: str, check_type: str = "all") -> ToolResult:
        """执行主机检查"""
        # TODO: 实际调用 ops_agent 的检查逻辑
        return ToolResult(
            tool_call_id="",
            tool_name="check_host",
            success=True,
            output=f"[示例] 主机 {hostname} 状态: CPU 45%, 内存 62%, 磁盘 78%",
        )
