"""
Z-OPS v4 — 集成示例: 用 Agent Loop + Tool System 改造 Agent

这个文件演示如何将现有的 general_agent 迁移到 v4 架构。
保留现有代码兼容性，同时引入新能力。

核心变化:
- 旧: handle() → 单次 LLM 调用 → reply()
- 新: handle() → agent_loop.run() → 多轮 tool-call → 流式 reply()
"""

import json
import time
import logging
from typing import AsyncGenerator

from agents.base import BaseAgent, AgentMessage
from core.agent_loop import AgentLoop, AgentLoopConfig, LoopEvent
from core.tool_system import get_tool_pool, ToolContext, ToolResult

logger = logging.getLogger("zops")


async def general_agent_v4(agent: BaseAgent, msg: AgentMessage):
    """
    General Agent v4 实现 — 基于 Agent Loop + Tool System
    
    对比旧版:
    - 旧版: _chat() → llm_chat() → 单次回复 (27-45s)
    - 新版: agent_loop → 多轮 tool-call → 自动查询 KB/检查主机/综合分析
    """
    query = msg.text or msg.params.get("text", "")
    if not query:
        await agent.reply(msg, error="缺少问题内容")
        return
    
    # 1. 构建消息
    messages = [{"role": "user", "content": query}]
    
    # 2. 添加会话历史 (最多 4 轮)
    if msg.params.get("history"):
        for h in msg.params["history"][-4:]:
            messages.append({
                "role": h.get("role", "user"),
                "content": h.get("content", ""),
            })
    
    # 3. 获取工具定义
    pool = get_tool_pool()
    tools = pool.get_all_definitions()
    
    # 4. 构建工具执行器
    async def tool_executor(tool_call):
        """执行单个工具调用"""
        func_name = tool_call.get("function", {}).get("name", "")
        func_args_raw = tool_call.get("function", {}).get("arguments", "{}")
        
        try:
            func_args = json.loads(func_args_raw) if isinstance(func_args_raw, str) else func_args_raw
        except json.JSONDecodeError:
            func_args = {}
        
        ctx = ToolContext(
            user=msg.username or "anonymous",
            session_id=msg.session_id or "",
            trace_id=msg.provenance.trace_id if msg.provenance else "",
        )
        
        result = await pool.execute(
            tool_call_id=tool_call["id"],
            tool_name=func_name,
            arguments=func_args,
            ctx=ctx,
        )
        return result.output
    
    # 5. 构建 LLM 调用函数
    async def llm_fn(messages, tools):
        """调用 LLM"""
        # 使用 agent 的 llm_chat 方法
        system_content = getattr(agent, 'SYSTEM_PROMPT', '你是运维助手')
        
        # 提取 system message
        system_msg = ""
        user_messages = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                user_messages.append(m)
        
        # 拼接为单次调用 (简化版，Phase 2 支持完整 messages 数组)
        prompt = "\n".join([m["content"] for m in user_messages])
        
        result = await agent.llm_chat(
            prompt=prompt,
            system=system_content or system_msg,
        )
        
        return {
            "content": result,
            "tool_calls": [],  # 当前 llm_chat 不支持 tool_call，Phase 2 接入
            "usage": {"total_tokens": 0},
        }
    
    # 6. 运行 Agent Loop
    config = AgentLoopConfig(
        max_turns=5,
        max_budget_tokens=20000,
        max_output_tokens=2048,
        timeout_seconds=120,
    )
    
    loop = AgentLoop(config)
    
    # 流式处理事件
    reply_parts = []
    async for event in loop.run(
        messages=messages,
        tools=tools,
        llm_fn=llm_fn,
        tool_executor=tool_executor,
        system_prompt=getattr(agent, 'SYSTEM_PROMPT', ''),
    ):
        if event.type == "progress":
            # 发送进度给前端
            await agent.send_progress(msg, event.data.get("text", ""))
        
        elif event.type == "llm_response":
            # 累积 LLM 响应
            content = event.data.get("content", "")
            if content:
                reply_parts.append(content)
        
        elif event.type == "tool_start":
            # 工具开始执行
            tool_name = event.data.get("tool", "")
            await agent.send_progress(msg, f"正在调用工具: {tool_name}...")
        
        elif event.type == "tool_result":
            # 工具执行完成
            tool_name = event.data.get("tool", "")
            output_preview = event.data.get("output", "")[:100]
            await agent.send_progress(msg, f"工具 {tool_name} 完成: {output_preview}")
        
        elif event.type == "terminal":
            state = event.data.get("state", {})
            kind = state.get("kind", "unknown")
            
            if kind == "completed":
                # 正常完成
                reply_text = "\n".join(reply_parts) if reply_parts else "处理完成"
                await agent.reply(msg, result={
                    "text": reply_text,
                    "source": "general_agent_v4",
                    "turns": state.get("turn_count", 0),
                    "tokens": state.get("token_used", 0),
                })
            else:
                # 异常终止
                await agent.reply(msg, result={
                    "text": f"处理中断: {state.get('message', '未知错误')}",
                    "source": "general_agent_v4",
                    "error": state.get("error", ""),
                    "terminal_kind": kind,
                })
            
            logger.info(
                f"[general_agent_v4] 完成: kind={kind}, "
                f"turns={state.get('turn_count', 0)}, "
                f"tokens={state.get('token_used', 0)}, "
                f"duration={time.time() - agent.start_time:.1f}s"
            )
            return


# ═══ 向后兼容: 渐进式迁移 ═══

async def general_agent_hybrid(agent: BaseAgent, msg: AgentMessage):
    """
    混合模式 — 先保持旧版兼容，逐步切换到 v4
    
    当前行为:
    1. 如果能用新架构处理 (有工具注册) → 走 v4 loop
    2. 否则 → 走旧版单次 LLM 调用
    
    这个函数可以替换现有 general_agent._chat()
    """
    pool = get_tool_pool()
    
    # 检查是否有工具注册
    if len(pool._tools) == 0:
        # 无工具 → 走旧版
        logger.info("[general_agent] 无工具注册，使用旧版单次调用")
        return await agent._chat(msg)
    
    # 有工具 → 走 v4
    logger.info(f"[general_agent] 已注册 {len(pool._tools)} 个工具，使用 v4 Agent Loop")
    return await general_agent_v4(agent, msg)


# ═══ 使用示例 (独立测试) ═══

if __name__ == "__main__":
    """独立测试 Agent Loop + Tool System"""
    import asyncio
    from core.tool_system import QueryKBTool, CheckHostTool
    
    async def test():
        print("=== Z-OPS v4 Agent Loop 测试 ===\n")
        
        # 1. 查看工具注册
        pool = get_tool_pool()
        print(f"已注册工具: {len(pool._tools)}")
        for t in pool.list_tools():
            print(f"  - {t['name']}: {t['description']}")
        
        # 2. 测试工具执行
        ctx = ToolContext(user="test", session_id="test-001")
        result = await pool.execute(
            tool_call_id="test-1",
            tool_name="query_kb",
            arguments={"query": "PowerShell 故障", "limit": 3},
            ctx=ctx,
        )
        print(f"\n工具执行结果: success={result.success}")
        print(f"输出: {result.output[:200]}...")
        
        # 3. 测试 Agent Loop (模拟 LLM)
        async def mock_llm(messages, tools):
            return {
                "content": "这是模拟回复",
                "tool_calls": [],
                "usage": {"total_tokens": 100},
            }
        
        print("\n=== Agent Loop 测试 ===")
        from core.agent_loop import run_agent_loop
        
        async for event in run_agent_loop(
            messages=[{"role": "user", "content": "PowerShell 出现问题怎么处理"}],
            llm_fn=mock_llm,
        ):
            print(f"事件: {event.type} -> {event.data}")
    
    asyncio.run(test())
