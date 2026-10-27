"""
Z-OPS Subagent Manager - Subagent Delegation System
子代理管理器 - 参考 Hermes Agent 的 spawn subagents 机制

核心功能:
1. 创建隔离的子代理实例
2. 并行委派任务给多个子代理
3. RPC 调用子代理的工具
4. 子代理生命周期管理
"""

import logging
import asyncio
import json
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field, asdict
from datetime import datetime
import uuid
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logger = logging.getLogger("zops.delegation.subagent")


@dataclass
class TaskSpec:
    """任务规格"""
    task_id: str
    task_description: str
    agent_type: str  # ops_agent, network_agent, vm_agent, etc.
    input_params: Dict[str, Any] = field(default_factory=dict)
    expected_output: str = ""  # 预期输出类型
    priority: int = 5  # 1-10
    timeout: int = 300  # 秒
    requires_isolation: bool = True  # 是否需要隔离执行
    # v4.0 (Phase 2.4): 进程内执行函数 async (task, params) -> result
    # (run_agent 是进程入口不可 await — 此前 delegation 闲置的根因)
    exec_fn: Optional[Callable] = None


@dataclass
class SubagentHandle:
    """子代理句柄"""
    subagent_id: str
    agent_type: str
    task_id: str
    status: str = "created"  # created / running / completed / failed / terminated
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    result: Any = None
    error: str = ""
    isolation_key: str = ""  # Redis 隔离键
    rpc_endpoint: str = ""


@dataclass
class SubagentResult:
    """子代理执行结果"""
    subagent_id: str
    task_id: str
    success: bool
    result: Any
    error: str = ""
    execution_time: float = 0.0
    agent_type: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class SubagentManager:
    """
    子代理管理器
    
    参考 Hermes Agent 的 spawn subagents + RPC 调用机制:
    1. spawn_subagent: 创建隔离的子代理实例
    2. delegate_parallel: 并行委派多个任务
    3. rpc_call: 通过 RPC 调用子代理工具
    4. terminate_subagent: 终止子代理
    """
    
    def __init__(self, redis_client=None, agent_registry=None):
        self.redis = redis_client
        self.agent_registry = agent_registry
        self._subagents: Dict[str, SubagentHandle] = {}
        self._task_queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        
    async def spawn_subagent(
        self,
        task: str,
        agent_type: str,
        isolated: bool = True,
        input_params: Dict = None,
        timeout: int = 300,
        exec_fn: Callable = None,
        task_id: str = ""
    ) -> SubagentHandle:
        """
        创建子代理实例
        
        Args:
            task: 任务描述
            agent_type: 代理类型
            isolated: 是否隔离执行 (独立 Redis 键空间)
            input_params: 输入参数
            timeout: 超时时间
        
        Returns:
            SubagentHandle: 子代理句柄
        """
        subagent_id = f"sub_{agent_type}_{uuid.uuid4().hex[:8]}"
        
        # 创建隔离键
        isolation_key = ""
        if isolated and self.redis:
            isolation_key = f"zops:isolated:{subagent_id}"
        
        handle = SubagentHandle(
            subagent_id=subagent_id,
            agent_type=agent_type,
            # v4.0 (Phase 2.4): 显式 task_id 优先 — 与 TaskSpec/ExecutionRegistry/sidechain
            # 文件名对齐 (否则审计无法按 task_id 关联)
            task_id=task_id or str(uuid.uuid4().hex[:8]),
            status="created",
            created_at=datetime.now().isoformat(),
            isolation_key=isolation_key
        )
        
        self._subagents[subagent_id] = handle
        logger.info(f"创建子代理: {subagent_id} ({agent_type})")
        
        # 立即开始执行
        await self._start_subagent_execution(handle, task, input_params, timeout, exec_fn)
        
        return handle
    
    async def _start_subagent_execution(
        self,
        handle: SubagentHandle,
        task: str,
        input_params: Dict,
        timeout: int,
        exec_fn: Callable = None
    ):
        """启动子代理执行"""
        handle.status = "running"
        handle.started_at = datetime.now().isoformat()
        
        # 在后台执行任务
        asyncio.create_task(
            self._execute_subagent_task(handle, task, input_params, timeout, exec_fn)
        )
    
    async def _execute_subagent_task(
        self,
        handle: SubagentHandle,
        task: str,
        input_params: Dict,
        timeout: int,
        exec_fn: Callable = None
    ):
        """执行子代理任务 (v4.0 Phase 2.4: sidechain transcript 完整审计)"""
        from core.delegation.sidechain import append_sidechain_event
        await append_sidechain_event(handle.agent_type, handle.task_id, {
            "type": "task_started", "task_id": handle.task_id,
            "subagent_id": handle.subagent_id,
            "description": task, "params": input_params or {},
        })
        try:
            # 加载对应 Agent
            start_time = datetime.now()
            
            # 调用 Agent 执行:
            # v4.0 (Phase 2.4): exec_fn 进程内执行优先 (run_agent 是进程入口不可 await);
            # 无 exec_fn 时保留原 _invoke_agent 路径 (超时降级)
            if exec_fn is not None:
                result = await asyncio.wait_for(
                    exec_fn(task, input_params or {}), timeout=timeout
                )
            else:
                result = await self._invoke_agent(
                    handle.agent_type,
                    task,
                    input_params,
                    handle.isolation_key,
                    timeout
                )
            
            execution_time = (datetime.now() - start_time).total_seconds()
            
            handle.status = "completed"
            handle.completed_at = datetime.now().isoformat()
            handle.result = result
            
            logger.info(f"子代理完成: {handle.subagent_id} ({execution_time:.2f}s)")
            await append_sidechain_event(handle.agent_type, handle.task_id, {
                "type": "task_completed", "task_id": handle.task_id,
                "subagent_id": handle.subagent_id, "success": True,
                "result": result, "execution_time": round(execution_time, 2),
            })
            
        except asyncio.TimeoutError:
            handle.status = "failed"
            handle.error = f"超时 ({timeout}s)"
            handle.completed_at = datetime.now().isoformat()
            logger.warning(f"子代理超时: {handle.subagent_id}")
            await append_sidechain_event(handle.agent_type, handle.task_id, {
                "type": "task_failed", "task_id": handle.task_id,
                "subagent_id": handle.subagent_id, "error": handle.error,
            })
            
        except Exception as e:
            handle.status = "failed"
            handle.error = str(e)
            handle.completed_at = datetime.now().isoformat()
            logger.error(f"子代理失败: {handle.subagent_id} - {e}")
            await append_sidechain_event(handle.agent_type, handle.task_id, {
                "type": "task_failed", "task_id": handle.task_id,
                "subagent_id": handle.subagent_id, "error": str(e),
            })
    
    async def _invoke_agent(
        self,
        agent_type: str,
        task: str,
        input_params: Dict,
        isolation_key: str,
        timeout: int
    ) -> Any:
        """调用 Agent 执行任务"""
        # 导入对应 Agent
        try:
            module = __import__(f"agents.{agent_type}", fromlist=["run_agent"])
            run_agent_func = getattr(module, "run_agent", None)
            
            if run_agent_func:
                # 构建消息
                message = {
                    "content": task,
                    "params": input_params or {},
                    "isolated": bool(isolation_key)
                }
                
                # 执行
                result = await asyncio.wait_for(
                    run_agent_func(message),
                    timeout=timeout
                )
                
                return result
            else:
                # 降级处理 - 直接返回任务描述
                return {"text": f"[{agent_type}] 任务已接收: {task[:100]}"}
                
        except ImportError:
            logger.warning(f"找不到 Agent: {agent_type}")
            return {"error": f"Agent {agent_type} not found"}
    
    async def delegate_parallel(
        self,
        tasks: List[TaskSpec],
        max_concurrent: int = 5
    ) -> List[SubagentResult]:
        """
        并行委派多个任务
        
        Args:
            tasks: 任务列表
            max_concurrent: 最大并发数
        
        Returns:
            List[SubagentResult]: 结果列表
        """
        results: List[SubagentResult] = []
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def run_with_semaphore(task_spec: TaskSpec):
            async with semaphore:
                handle = await self.spawn_subagent(
                    task=task_spec.task_description,
                    agent_type=task_spec.agent_type,
                    isolated=task_spec.requires_isolation,
                    input_params=task_spec.input_params,
                    timeout=task_spec.timeout,
                    exec_fn=task_spec.exec_fn,
                    task_id=task_spec.task_id
                )
                
                # 等待完成
                await self._wait_for_completion(handle, task_spec.timeout)
                
                return SubagentResult(
                    subagent_id=handle.subagent_id,
                    task_id=task_spec.task_id,
                    success=handle.status == "completed",
                    result=handle.result,
                    error=handle.error,
                    execution_time=0.0,  # 计算在 wait_for_completion 中
                    agent_type=handle.agent_type
                )
        
        # 并行执行
        coroutines = [run_with_semaphore(t) for t in tasks]
        results = await asyncio.gather(*coroutines, return_exceptions=True)
        
        # 处理异常
        final_results = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                final_results.append(SubagentResult(
                    subagent_id="error",
                    task_id=tasks[i].task_id,
                    success=False,
                    result=None,
                    error=str(r),
                    agent_type=tasks[i].agent_type
                ))
            else:
                final_results.append(r)
        
        logger.info(f"并行委派完成: {len(final_results)} 任务")
        return final_results
    
    async def _wait_for_completion(self, handle: SubagentHandle, timeout: int):
        """等待子代理完成"""
        start = datetime.now()
        
        while handle.status in ["created", "running"]:
            elapsed = (datetime.now() - start).total_seconds()
            if elapsed >= timeout:
                handle.status = "failed"
                handle.error = "wait_timeout"
                break
            await asyncio.sleep(0.5)
    
    async def rpc_call(
        self,
        subagent_id: str,
        tool_name: str,
        params: dict
    ) -> Any:
        """
        RPC 调用子代理工具
        
        参考 Hermes Agent 的 RPC 桥接:
        通过 RPC 调用已创建子代理的工具方法
        """
        if subagent_id not in self._subagents:
            raise ValueError(f"子代理不存在: {subagent_id}")
        
        handle = self._subagents[subagent_id]
        
        if handle.status != "running":
            raise ValueError(f"子代理状态异常: {handle.status}")
        
        # 调用工具
        try:
            result = await self._call_agent_tool(
                handle.agent_type,
                tool_name,
                params,
                handle.isolation_key
            )
            return result
        except Exception as e:
            logger.error(f"RPC 调用失败: {subagent_id}/{tool_name} - {e}")
            raise
    
    async def _call_agent_tool(
        self,
        agent_type: str,
        tool_name: str,
        params: dict,
        isolation_key: str
    ) -> Any:
        """调用 Agent 工具"""
        try:
            # 加载技能
            from core.skill_registry import SkillRegistry
            registry = SkillRegistry()
            skill = registry.get(tool_name)
            
            if skill:
                # 执行技能
                # 这里需要实际的技能执行逻辑
                return {"executed": tool_name, "params": params}
            else:
                return {"error": f"工具 {tool_name} 不存在"}
                
        except Exception as e:
            return {"error": str(e)}
    
    async def terminate_subagent(self, subagent_id: str):
        """终止子代理"""
        if subagent_id not in self._subagents:
            return
        
        handle = self._subagents[subagent_id]
        handle.status = "terminated"
        handle.completed_at = datetime.now().isoformat()
        
        # 清理隔离数据
        if handle.isolation_key and self.redis:
            await self.redis.delete(handle.isolation_key)
        
        logger.info(f"终止子代理: {subagent_id}")
    
    async def get_subagent_status(self, subagent_id: str) -> Optional[SubagentHandle]:
        """获取子代理状态"""
        return self._subagents.get(subagent_id)
    
    async def list_active_subagents(self) -> List[SubagentHandle]:
        """列出活跃子代理"""
        return [h for h in self._subagents.values() if h.status in ["created", "running"]]
    
    async def cleanup_completed(self, max_age_minutes: int = 30):
        """清理已完成的子代理"""
        cutoff = datetime.now() - timedelta(minutes=max_age_minutes)
        
        to_remove = []
        for sid, handle in self._subagents.items():
            if handle.status in ["completed", "failed", "terminated"]:
                if handle.completed_at:
                    completed_time = datetime.fromisoformat(handle.completed_at)
                    if completed_time < cutoff:
                        to_remove.append(sid)
        
        for sid in to_remove:
            del self._subagents[sid]
        
        logger.debug(f"清理子代理: {len(to_remove)} 个")


# 单例
_manager_instance: Optional[SubagentManager] = None


async def get_subagent_manager() -> SubagentManager:
    """获取子代理管理器实例"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = SubagentManager()
    return _manager_instance


# 导入 timedelta
from datetime import timedelta