"""
Z-OPS v4 — Agent Loop (AsyncGenerator 流式状态机)

参考 Claude Code / Claw Code 的 Query Loop 设计:
- AsyncGenerator 作为控制平面，yield 中间结果给 UI
- 10 种终止状态 (Terminal discriminated union)
- 多轮 tool-call 循环: LLM → tool_call → 执行 → 结果返回 LLM → 继续
- 错误恢复阶梯 (非简单 fallback)

架构要点:
- run() 是唯一的 async generator，产生所有事件
- 内部方法 (如 _exec_tools) 通过 event_cb 回调发送事件，不直接 yield
- 这样避免了 "return + yield" 的语法冲突
"""

import asyncio
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

logger = logging.getLogger("zops")


class TerminalKind(str, Enum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    TOKEN_BUDGET_EXHAUSTED = "token_budget"
    PROMPT_TOO_LONG = "prompt_too_long"
    MODEL_ERROR = "model_error"
    STREAM_ABORTED = "stream_aborted"
    TOOL_ERROR = "tool_error"
    TIMEOUT = "timeout"
    HOOK_BLOCKED = "hook_blocked"
    RECOVERY_FAILED = "recovery_failed"


@dataclass
class TerminalState:
    kind: TerminalKind
    message: str = ""
    turn_count: int = 0
    token_used: int = 0
    error: Optional[str] = None


@dataclass
class LoopEvent:
    type: str
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class AgentLoopConfig:
    max_turns: int = 8
    max_budget_tokens: int = 30000
    max_output_tokens: int = 2048
    max_retry: int = 2
    retry_delay: float = 1.0
    temperature: float = 0.7
    timeout_seconds: float = 120.0
    enable_tool_calls: bool = True
    tool_choice: str = "auto"


class AgentLoop:
    """
    Agent Loop — AsyncGenerator 流式状态机
    
    使用方式:
        loop = AgentLoop(config)
        async for event in loop.run(messages, tools, llm_fn, tool_executor):
            if event.type == 'terminal':
                break
            yield event  # 转发给 SSE
    """
    
    def __init__(self, config: AgentLoopConfig):
        self.config = config
        self.turn_count = 0
        self.total_tokens = 0
        self.start_time = time.time()
        self._abort = False
        self._event_queue: asyncio.Queue = None
    
    def abort(self):
        self._abort = True
    
    def _term(self, kind: TerminalKind, msg: str, **kw) -> Dict:
        return {"state": {"kind": kind.value, "message": msg, "turn_count": self.turn_count, "token_used": self.total_tokens, **kw}}
    
    async def run(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None,
        llm_fn=None,
        tool_executor=None,
        system_prompt: str = "",
    ) -> AsyncGenerator[LoopEvent, None]:
        if system_prompt and (not messages or messages[0].get("role") != "system"):
            messages = [{"role": "system", "content": system_prompt}] + messages
        
        self.start_time = time.time()
        self._event_queue = asyncio.Queue()
        
        try:
            while True:
                # 终止检查
                if self._abort:
                    yield LoopEvent(type="terminal", data=self._term(TerminalKind.STREAM_ABORTED, "用户中断"))
                    return
                if self.turn_count >= self.config.max_turns:
                    yield LoopEvent(type="terminal", data=self._term(TerminalKind.MAX_TURNS, f"达到最大轮数 ({self.config.max_turns})"))
                    return
                if self.total_tokens >= self.config.max_budget_tokens:
                    yield LoopEvent(type="terminal", data=self._term(TerminalKind.TOKEN_BUDGET_EXHAUSTED, "Token 预算耗尽"))
                    return
                if time.time() - self.start_time >= self.config.timeout_seconds:
                    yield LoopEvent(type="terminal", data=self._term(TerminalKind.TIMEOUT, "超时"))
                    return
                
                # Context Compression (Phase 5)
                messages = await self._compress(messages)
                
                yield LoopEvent(type="progress", data={"text": f"第 {self.turn_count + 1} 轮推理..."})
                
                # LLM 调用
                llm_response = await self._call_llm(messages, tools, llm_fn)
                if llm_response is None:
                    yield LoopEvent(type="terminal", data=self._term(TerminalKind.MODEL_ERROR, "LLM 调用失败"))
                    return
                
                # 记录 token
                usage = llm_response.get("usage", {})
                self.total_tokens += usage.get("total_tokens", 0)
                
                # 添加 assistant 消息
                assistant_msg = {"role": "assistant", "content": llm_response.get("content", "")}
                if llm_response.get("tool_calls"):
                    assistant_msg["tool_calls"] = llm_response["tool_calls"]
                messages.append(assistant_msg)
                
                # Yield 内容
                if llm_response.get("content"):
                    yield LoopEvent(type="llm_response", data={"content": llm_response["content"]})
                
                # 检查 tool_call
                tool_calls = llm_response.get("tool_calls", [])
                if not tool_calls:
                    yield LoopEvent(type="terminal", data=self._term(TerminalKind.COMPLETED, "正常完成"))
                    return
                
                # 执行工具 (通过回调发送事件)
                tool_results = await self._exec_tools(tool_calls, tool_executor, event_cb=self._emit)
                for tr in tool_results:
                    messages.append({"role": "tool", "tool_call_id": tr["tool_call_id"], "content": tr["output"]})
                
                self.turn_count += 1
        
        except asyncio.CancelledError:
            yield LoopEvent(type="terminal", data=self._term(TerminalKind.STREAM_ABORTED, "用户中断"))
        except Exception as e:
            logger.error(f"[agent_loop] 错误: {e}", exc_info=True)
            yield LoopEvent(type="terminal", data=self._term(TerminalKind.TOOL_ERROR, str(e), error=str(e)))
    
    async def _emit(self, event: LoopEvent):
        """发送事件到队列"""
        await self._event_queue.put(event)
    
    async def _compress(self, messages):
        return messages
    
    async def _call_llm(self, messages, tools, llm_fn):
        for attempt in range(self.config.max_retry + 1):
            try:
                if attempt > 0:
                    await asyncio.sleep(self.config.retry_delay * (2 ** (attempt - 1)))
                return await llm_fn(messages, tools)
            except Exception as e:
                logger.warning(f"[agent_loop] LLM 失败 ({attempt+1}/{self.config.max_retry+1}): {e}")
        return None
    
    async def _exec_tools(self, tool_calls, tool_executor, event_cb: Callable):
        """
        执行工具 — 通过 event_cb 发送事件，不直接 yield
        """
        if not tool_executor:
            return [{"tool_call_id": tc["id"], "output": f"工具 {tc['function']['name']} 未注册"} for tc in tool_calls]
        
        results = []
        for tc in tool_calls:
            try:
                await event_cb(LoopEvent(type="tool_start", data={"tool": tc["function"]["name"]}))
                output = await tool_executor(tc)
                results.append({"tool_call_id": tc["id"], "output": str(output)})
                await event_cb(LoopEvent(type="tool_result", data={"tool": tc["function"]["name"], "output": str(output)[:200]}))
            except Exception as e:
                logger.error(f"[agent_loop] 工具失败 {tc['function']['name']}: {e}")
                results.append({"tool_call_id": tc["id"], "output": f"失败: {e}"})
        return results


async def run_agent_loop(messages, llm_fn, config=None, tools=None, tool_executor=None, system_prompt=""):
    loop = AgentLoop(config or AgentLoopConfig())
    async for event in loop.run(messages=messages, tools=tools, llm_fn=llm_fn, tool_executor=tool_executor, system_prompt=system_prompt):
        yield event
