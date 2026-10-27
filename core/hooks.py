"""
Z-OPS v4 — Hooks System (27 个生命周期事件拦截器)

参考 Claude Code 的 Hook 系统设计:
- 27 个生命周期事件 (PreToolUse, PostToolUse, PreLLMCall, etc.)
- 4 种 Hook 形式: shell command / single-shot prompt / agent loop / HTTP webhook
- 5 种决策类型: ALLOW / DENY / ASK / MODIFY / PASSTHROUGH
- 支持规则匹配 (tool name / command pattern / file glob)

核心接口:
    @hook("PreToolUse", tool="bash")
    async def check_bash_safety(tool_input, ctx):
        if "rm -rf /" in tool_input.command:
            return HookDecision.DENY("危险操作")
        return HookDecision.PASSTHROUGH
"""

import time
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union
from pathlib import Path

logger = logging.getLogger("zops")


# ═══ Hook 决策类型 ═══

class HookDecision(str, Enum):
    ALLOW = "allow"              # 允许继续
    DENY = "deny"                # 阻止并返回错误
    ASK = "ask"                  # 需要用户确认
    MODIFY = "modify"            # 修改输入后继续
    PASSTHROUGH = "passthrough"  # 不干预


@dataclass
class HookResult:
    """Hook 执行结果"""
    decision: HookDecision
    reason: str = ""
    modified_input: Optional[Dict] = None  # MODIFY 时的新输入
    error_message: str = ""                # DENY 时的错误信息


# ═══ 27 个生命周期事件 ═══

class HookEvent(str, Enum):
    # 工具执行相关 (8)
    PRE_TOOL_USE = "PreToolUse"           # 工具执行前
    POST_TOOL_USE = "PostToolUse"         # 工具执行后
    TOOL_CALL_START = "ToolCallStart"     # tool_call 开始
    TOOL_CALL_END = "ToolCallEnd"         # tool_call 结束
    TOOL_PERMISSION_CHECK = "ToolPermissionCheck"  # 权限检查
    TOOL_RESULT_BUDGET = "ToolResultBudget"        # 结果预算
    TOOL_VALIDATION = "ToolValidation"    # 工具验证
    TOOL_ERROR = "ToolError"              # 工具错误
    
    # LLM 调用相关 (6)
    PRE_LLM_CALL = "PreLLMCall"           # LLM 调用前
    POST_LLM_CALL = "PostLLMCall"         # LLM 调用后
    LLM_STREAM_START = "LLMStreamStart"   # 流式开始
    LLM_STREAM_CHUNK = "LLMStreamChunk"   # 流式 chunk
    LLM_STREAM_END = "LLMStreamEnd"       # 流式结束
    LLM_ERROR = "LLMError"                # LLM 错误
    
    # 会话生命周期 (6)
    SESSION_START = "SessionStart"        # 会话开始
    SESSION_END = "SessionEnd"            # 会话结束
    MESSAGE_RECEIVED = "MessageReceived"  # 收到消息
    REPLY_SENT = "ReplySent"              # 发送回复
    AGENT_LOOP_START = "AgentLoopStart"   # Agent Loop 开始
    AGENT_LOOP_END = "AgentLoopEnd"       # Agent Loop 结束
    
    # 系统级 (7)
    MEMORY_LOAD = "MemoryLoad"            # 记忆加载
    MEMORY_SAVE = "MemorySave"            # 记忆保存
    CONTEXT_COMPRESS = "ContextCompress"  # 上下文压缩
    TOKEN_BUDGET = "TokenBudget"          # Token 预算
    COST_TRACK = "CostTrack"              # 成本追踪
    ABORT_REQUEST = "AbortRequest"        # 中断请求
    SYSTEM_ERROR = "SystemError"          # 系统错误


# ═══ Hook 定义 ═══

@dataclass
class HookDefinition:
    """单个 Hook 定义"""
    name: str
    event: HookEvent
    handler: Callable
    priority: int = 0                # 优先级 (高优先先执行)
    tool_filter: Optional[str] = None  # 工具过滤 (仅 Pre/PostToolUse)
    command_pattern: Optional[str] = None  # 命令模式匹配
    enabled: bool = True
    
    def matches(self, context: Dict) -> bool:
        """检查是否匹配当前上下文"""
        if self.tool_filter:
            tool_name = context.get("tool_name", "")
            if tool_name != self.tool_filter:
                return False
        
        if self.command_pattern:
            command = context.get("command", "")
            if not re.search(self.command_pattern, command):
                return False
        
        return True


# ═══ Hook 注册中心 ═══

class HookRegistry:
    """
    Hook 注册中心 — 管理所有已注册的 Hook
    
    使用方式:
        registry = HookRegistry()
        
        # 注册 Hook
        @registry.hook("PreToolUse", tool="bash")
        async def check_bash_safety(tool_input, ctx):
            ...
        
        # 触发 Hook
        result = await registry.fire(HookEvent.PRE_TOOL_USE, context)
    """
    
    def __init__(self):
        self._hooks: Dict[HookEvent, List[HookDefinition]] = {}
    
    def hook(self, event: Union[HookEvent, str], **filters):
        """
        Hook 装饰器
        
        用法:
            @registry.hook("PreToolUse", tool="bash")
            async def my_hook(tool_input, ctx):
                return HookDecision.ALLOW
        """
        if isinstance(event, str):
            event = HookEvent(event)
        
        def decorator(func):
            hook_def = HookDefinition(
                name=func.__name__,
                event=event,
                handler=func,
                tool_filter=filters.get("tool"),
                command_pattern=filters.get("command_pattern"),
                priority=filters.get("priority", 0),
            )
            self.register(hook_def)
            return func
        return decorator
    
    def register(self, hook: HookDefinition):
        """注册 Hook"""
        self._hooks.setdefault(hook.event, []).append(hook)
        # 按优先级排序 (高优先先执行)
        self._hooks[hook.event].sort(key=lambda h: h.priority, reverse=True)
        logger.info(f"[hooks] 注册: {hook.event.value} - {hook.name} (priority={hook.priority})")
    
    def unregister(self, event: HookEvent, name: str) -> bool:
        """注销 Hook"""
        hooks = self._hooks.get(event, [])
        before = len(hooks)
        self._hooks[event] = [h for h in hooks if h.name != name]
        return len(self._hooks[event]) < before
    
    async def fire(
        self,
        event: HookEvent,
        context: Dict[str, Any],
    ) -> HookResult:
        """
        触发 Hook — 按优先级执行所有匹配的 Hook
        
        返回:
            第一个非 PASSTHROUGH 的决策，或 PASSTHROUGH
        
        注意:
            - DENY 立即返回，后续 Hook 不执行
            - MODIFY 更新 context 后继续
            - ASK 需要用户交互 (目前返回 ASK)
        """
        hooks = self._hooks.get(event, [])
        
        for hook in hooks:
            if not hook.enabled:
                continue
            
            if not hook.matches(context):
                continue
            
            try:
                # 执行 Hook
                result = await hook.handler(context)
                
                if isinstance(result, HookDecision):
                    result = HookResult(decision=result)
                elif isinstance(result, tuple):
                    decision, reason = result
                    result = HookResult(decision=decision, reason=reason)
                
                # 处理决策
                if result.decision == HookDecision.DENY:
                    logger.info(f"[hooks] {hook.name} DENY: {result.reason}")
                    return result
                
                if result.decision == HookDecision.MODIFY:
                    if result.modified_input:
                        context.update(result.modified_input)
                    logger.info(f"[hooks] {hook.name} MODIFY")
                    continue
                
                if result.decision == HookDecision.ASK:
                    logger.info(f"[hooks] {hook.name} ASK: {result.reason}")
                    return result
                
                if result.decision == HookDecision.ALLOW:
                    logger.debug(f"[hooks] {hook.name} ALLOW")
                    return result
                
                # PASSTHROUGH → 继续下一个 Hook
                logger.debug(f"[hooks] {hook.name} PASSTHROUGH")
            
            except Exception as e:
                logger.error(f"[hooks] {hook.name} 执行失败: {e}", exc_info=True)
                # Hook 失败不阻塞流程，继续执行
                continue
        
        # 所有 Hook 都 PASSTHROUGH
        return HookResult(decision=HookDecision.PASSTHROUGH, reason="所有 Hook 未干预")
    
    def list_hooks(self, event: HookEvent = None) -> List[Dict]:
        """列出已注册的 Hook"""
        if event:
            return [
                {"name": h.name, "event": h.event.value, "priority": h.priority, "enabled": h.enabled}
                for h in self._hooks.get(event, [])
            ]
        
        all_hooks = []
        for evt, hooks in self._hooks.items():
            for h in hooks:
                all_hooks.append({
                    "name": h.name,
                    "event": evt.value,
                    "priority": h.priority,
                    "enabled": h.enabled,
                })
        return all_hooks
    
    def stats(self) -> Dict:
        """统计信息"""
        return {
            "total_hooks": sum(len(hooks) for hooks in self._hooks.values()),
            "by_event": {evt.value: len(hooks) for evt, hooks in self._hooks.items()},
        }


# ═══ 内置 Hook 示例 ═══

async def builtin_dangerous_command_hook(context: Dict) -> HookResult:
    """
    内置 Hook: 危险命令拦截
    
    拦截 rm -rf /, chmod 777, dd if=/dev/zero 等危险操作
    """
    command = context.get("command", "")
    if not command:
        return HookResult(decision=HookDecision.PASSTHROUGH)
    
    dangerous_patterns = [
        (r'rm\s+-rf\s+/', '禁止删除根目录'),
        (r'chmod\s+777', '禁止设置 777 权限'),
        (r'dd\s+if=/dev/zero', '禁止 dd 清零'),
        (r'mkfs', '禁止格式化文件系统'),
        (r'>\s*/etc/passwd', '禁止修改 passwd'),
        (r'>\s*/etc/shadow', '禁止修改 shadow'),
    ]
    
    for pattern, reason in dangerous_patterns:
        if re.search(pattern, command):
            return HookResult(
                decision=HookDecision.DENY,
                reason=f"危险操作拦截: {reason}",
                error_message=f"命令被安全策略拒绝: {reason}"
            )
    
    return HookResult(decision=HookDecision.PASSTHROUGH)


async def builtin_audit_log_hook(context: Dict) -> HookResult:
    """
    内置 Hook: 审计日志
    
    记录所有工具调用到审计日志
    """
    tool_name = context.get("tool_name", "")
    user = context.get("user", "unknown")
    session_id = context.get("session_id", "")
    
    logger.info(
        f"[audit] 工具调用: user={user}, session={session_id}, "
        f"tool={tool_name}, time={time.time()}"
    )
    
    return HookResult(decision=HookDecision.PASSTHROUGH)


async def builtin_cost_tracking_hook(context: Dict) -> HookResult:
    """
    内置 Hook: 成本追踪
    
    记录 LLM 调用的 token 使用量
    """
    usage = context.get("usage", {})
    if usage:
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)
        
        logger.info(
            f"[cost] LLM 调用: input={input_tokens}, "
            f"output={output_tokens}, total={total_tokens}"
        )
    
    return HookResult(decision=HookDecision.PASSTHROUGH)


# ═══ 全局 Hook 注册中心 ═══

_global_registry = HookRegistry()


def get_hook_registry() -> HookRegistry:
    """获取全局 Hook 注册中心"""
    return _global_registry


def register_builtin_hooks():
    """注册内置 Hook"""
    registry = get_hook_registry()
    
    # 危险命令拦截 (高优先级)
    registry.register(HookDefinition(
        name="dangerous_command",
        event=HookEvent.PRE_TOOL_USE,
        handler=builtin_dangerous_command_hook,
        priority=100,
    ))
    
    # 审计日志 (低优先级，总是执行)
    registry.register(HookDefinition(
        name="audit_log",
        event=HookEvent.POST_TOOL_USE,
        handler=builtin_audit_log_hook,
        priority=-100,
    ))
    
    # 成本追踪
    registry.register(HookDefinition(
        name="cost_tracking",
        event=HookEvent.POST_LLM_CALL,
        handler=builtin_cost_tracking_hook,
        priority=0,
    ))
    
    logger.info("[hooks] 内置 Hook 注册完成")


# 自动注册内置 Hook
register_builtin_hooks()


# ═══ 便捷函数 ═══

async def fire_hook(event: Union[HookEvent, str], context: Dict) -> HookResult:
    """便捷函数: 触发 Hook"""
    if isinstance(event, str):
        event = HookEvent(event)
    return await get_hook_registry().fire(event, context)


# ═══ 测试 ═══

if __name__ == "__main__":
    import asyncio
    
    async def test():
        registry = get_hook_registry()
        
        print("=== Hook System 测试 ===\n")
        
        # 查看统计
        print(f"内置 Hook 统计: {registry.stats()}")
        
        # 测试危险命令拦截
        print("\n--- 测试: 危险命令拦截 ---")
        result = await registry.fire(HookEvent.PRE_TOOL_USE, {
            "tool_name": "bash",
            "command": "rm -rf /tmp/test",
        })
        print(f"安全命令: {result.decision}")
        
        result = await registry.fire(HookEvent.PRE_TOOL_USE, {
            "tool_name": "bash",
            "command": "rm -rf /",
        })
        print(f"危险命令: {result.decision} - {result.reason}")
        
        # 注册自定义 Hook
        @registry.hook("PreLLMCall")
        async def my_llm_hook(ctx):
            print(f"[my_llm_hook] 触发，messages={len(ctx.get('messages', []))}")
            return HookDecision.PASSTHROUGH
        
        print(f"\n注册自定义 Hook 后: {registry.stats()}")
        
        # 测试自定义 Hook
        print("\n--- 测试: 自定义 Hook ---")
        await registry.fire(HookEvent.PRE_LLM_CALL, {
            "messages": [{"role": "user", "content": "test"}],
        })
    
    asyncio.run(test())
