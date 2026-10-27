"""
Z-OPS Parallel Executor - Parallel Task Execution
并行执行器 - 参考 Hermes Agent 的并行工作流

核心功能:
1. 并行执行多个任务
2. 任务依赖管理
3. 结果聚合
4. 错误隔离和容错
"""

import logging
import asyncio
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
import uuid

logger = logging.getLogger("zops.delegation.parallel")


@dataclass(order=True)
class ParallelTask:
    """并行任务"""
    sort_priority: int = field(init=False)  # 用于排序
    task_id: str = field(compare=False)
    task_func: Callable = field(compare=False, default=None)
    args: tuple = field(compare=False, default_factory=tuple)
    kwargs: Dict = field(compare=False, default_factory=dict)
    depends_on: List[str] = field(compare=False, default_factory=list)
    priority: int = field(compare=False, default=5)
    timeout: int = field(compare=False, default=60)
    retry_count: int = field(compare=False, default=0)
    max_retries: int = field(compare=False, default=3)
    
    def __post_init__(self):
        self.sort_priority = self.priority


@dataclass
class TaskResult:
    """任务结果"""
    task_id: str
    success: bool
    result: Any = None
    error: str = ""
    execution_time: float = 0.0
    retry_used: int = 0


class ParallelExecutor:
    """
    并行执行器
    
    参考 Hermes Agent 的并行执行设计:
    1. 支持任务依赖图
    2. 智能调度优化
    3. 错误隔离和重试
    4. 结果聚合
    """
    
    def __init__(self, max_workers: int = 10):
        self.max_workers = max_workers
        self._task_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._results: Dict[str, TaskResult] = {}
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._completed_tasks: set = set()
        self._failed_tasks: set = set()
    
    async def execute_parallel(
        self,
        tasks: List[ParallelTask],
        fail_fast: bool = False,
        aggregate_results: bool = True
    ) -> Dict[str, TaskResult]:
        """
        并行执行任务
        
        Args:
            tasks: 任务列表
            fail_fast: 是否快速失败 (一个失败则全部停止)
            aggregate_results: 是否聚合结果
        
        Returns:
            Dict[str, TaskResult]: 任务结果映射
        """
        # 添加任务到队列
        for task in tasks:
            priority = (task.priority, len(task.depends_on))
            await self._task_queue.put((priority, task))
        
        # 启动执行器
        workers = [
            asyncio.create_task(self._worker(fail_fast))
            for _ in range(self.max_workers)
        ]
        
        # 等待完成
        await self._task_queue.join()
        
        # 取消剩余 worker
        for w in workers:
            w.cancel()
        
        # 聚合结果
        if aggregate_results:
            return self._aggregate_results()
        
        return self._results
    
    async def _worker(self, fail_fast: bool):
        """工作协程"""
        while True:
            try:
                priority, task = await asyncio.wait_for(
                    self._task_queue.get(),
                    timeout=1.0
                )
                
                # 检查依赖
                if task.depends_on:
                    deps_ready = all(
                        d in self._completed_tasks 
                        for d in task.depends_on
                    )
                    deps_failed = any(
                        d in self._failed_tasks 
                        for d in task.depends_on
                    )
                    
                    if deps_failed:
                        # 依赖失败，跳过此任务
                        self._results[task.task_id] = TaskResult(
                            task_id=task.task_id,
                            success=False,
                            error="dependency_failed"
                        )
                        self._task_queue.task_done()
                        continue
                    
                    if not deps_ready:
                        # 依赖未完成，重新入队
                        await self._task_queue.put((priority, task))
                        await asyncio.sleep(0.5)
                        continue
                
                # 执行任务
                result = await self._execute_task(task)
                
                self._results[task.task_id] = result
                
                if result.success:
                    self._completed_tasks.add(task.task_id)
                else:
                    self._failed_tasks.add(task.task_id)
                    if fail_fast:
                        # 快速失败模式，清空队列
                        while not self._task_queue.empty():
                            try:
                                self._task_queue.get_nowait()
                                self._task_queue.task_done()
                            except asyncio.QueueEmpty:
                                break
                
                self._task_queue.task_done()
                
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker 错误: {e}")
    
    async def _execute_task(self, task: ParallelTask) -> TaskResult:
        """执行单个任务"""
        start_time = datetime.now()
        retries = 0
        
        while retries <= task.max_retries:
            try:
                result = await asyncio.wait_for(
                    task.task_func(*task.args, **task.kwargs),
                    timeout=task.timeout
                )
                
                execution_time = (datetime.now() - start_time).total_seconds()
                
                logger.debug(f"任务完成: {task.task_id} ({execution_time:.2f}s)")
                
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    result=result,
                    execution_time=execution_time,
                    retry_used=retries
                )
                
            except asyncio.TimeoutError:
                retries += 1
                logger.warning(f"任务超时: {task.task_id} (重试 {retries})")
                
            except Exception as e:
                retries += 1
                logger.error(f"任务失败: {task.task_id} - {e} (重试 {retries})")
        
        execution_time = (datetime.now() - start_time).total_seconds()
        
        return TaskResult(
            task_id=task.task_id,
            success=False,
            error="max_retries_exceeded",
            execution_time=execution_time,
            retry_used=retries
        )
    
    def _aggregate_results(self) -> Dict[str, Any]:
        """聚合结果"""
        successful = [r for r in self._results.values() if r.success]
        failed = [r for r in self._results.values() if not r.success]
        
        return {
            "total_tasks": len(self._results),
            "successful": len(successful),
            "failed": len(failed),
            "success_rate": len(successful) / len(self._results) if self._results else 0,
            "results": {r.task_id: r.result for r in successful},
            "errors": {r.task_id: r.error for r in failed},
            "execution_times": {r.task_id: r.execution_time for r in self._results.values()}
        }
    
    async def execute_with_callback(
        self,
        tasks: List[ParallelTask],
        callback: Callable[[TaskResult], None]
    ) -> Dict[str, TaskResult]:
        """
        带回调的并行执行
        
        每个任务完成后触发回调
        """
        results = await self.execute_parallel(tasks, aggregate_results=False)
        
        for task_id, result in results.items():
            try:
                callback(result)
            except Exception as e:
                logger.error(f"回调执行失败: {task_id} - {e}")
        
        return results
    
    async def map_parallel(
        self,
        func: Callable,
        items: List[Any],
        max_concurrent: int = 5
    ) -> List[Any]:
        """
        并行映射
        
        将函数并行应用于每个元素
        """
        tasks = [
            ParallelTask(
                task_id=f"map_{i}",
                task_func=func,
                args=(item,)
            )
            for i, item in enumerate(items)
        ]
        
        self.max_workers = max_concurrent
        results = await self.execute_parallel(tasks)
        
        # 按原始顺序返回结果
        ordered_results = []
        for i, item in enumerate(items):
            task_id = f"map_{i}"
            if task_id in results["results"]:
                ordered_results.append(results["results"][task_id])
            else:
                ordered_results.append(None)
        
        return ordered_results
    
    def get_stats(self) -> Dict:
        """获取执行统计"""
        return {
            "total_completed": len(self._completed_tasks),
            "total_failed": len(self._failed_tasks),
            "active_workers": len(self._running_tasks),
            "results_stored": len(self._results)
        }


# 单例
_executor_instance: Optional[ParallelExecutor] = None


def get_parallel_executor(max_workers: int = 10) -> ParallelExecutor:
    """获取并行执行器实例"""
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = ParallelExecutor(max_workers)
    return _executor_instance