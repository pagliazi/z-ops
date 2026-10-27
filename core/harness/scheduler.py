"""
Z-OPS Harness - 自动化任务调度模块
负责定时触发 N8N 工作流，实现主动式运维
"""

import logging
import asyncio
from typing import Dict, List, Optional, Callable
from datetime import datetime, time as dt_time
from dataclasses import dataclass
import json

logger = logging.getLogger("zops.harness.scheduler")


@dataclass
class ScheduledTask:
    """调度任务定义"""
    task_id: str
    name: str
    workflow: str  # N8N workflow 名称
    schedule: str  # cron 表达式或特殊标识
    params: Dict  # 传递给工作流的参数
    enabled: bool = True
    last_run: Optional[str] = None
    next_run: Optional[str] = None
    on_success: Optional[str] = None  # 成功时回调
    on_failure: Optional[str] = None  # 失败时回调


class TaskScheduler:
    """
    任务调度器
    管理定时任务的注册和执行
    """
    
    # 内置任务模板
    TEMPLATES = {
        "daily_health_check": {
            "name": "每日健康检查",
            "workflow": "health_check",
            "schedule": "0 9 * * *",  # 每天 9:00
            "params": {"check_type": "full", "notify": True}
        },
        "daily_backup_check": {
            "name": "每日备份检查",
            "workflow": "backup_check",
            "schedule": "0 2 * * *",  # 每天 2:00
            "params": {"check_type": "all"}
        },
        "weekly_report": {
            "name": "每周健康报告",
            "workflow": "daily_health_report",
            "schedule": "0 8 * * 1",  # 每周一 8:00
            "params": {"period": "weekly"}
        },
    }
    
    def __init__(self, n8n_executor=None):
        self.n8n_executor = n8n_executor
        self.tasks: Dict[str, ScheduledTask] = {}
        self._running = False
        self._task_handlers: Dict[str, Callable] = {}
    
    def register_template(self, template_name: str) -> ScheduledTask:
        """注册内置任务模板"""
        if template_name not in self.TEMPLATES:
            raise ValueError(f"未知模板: {template_name}")
        
        template = self.TEMPLATES[template_name].copy()
        task = ScheduledTask(
            task_id=template_name,
            name=template["name"],
            workflow=template["workflow"],
            schedule=template["schedule"],
            params=template["params"]
        )
        self.tasks[task.task_id] = task
        logger.info(f"注册任务: {task.name} ({task.schedule})")
        return task
    
    def register_custom_task(
        self,
        task_id: str,
        name: str,
        workflow: str,
        schedule: str,
        params: Dict,
        on_success: str = None,
        on_failure: str = None
    ) -> ScheduledTask:
        """注册自定义任务"""
        task = ScheduledTask(
            task_id=task_id,
            name=name,
            workflow=workflow,
            schedule=schedule,
            params=params,
            on_success=on_success,
            on_failure=on_failure
        )
        self.tasks[task_id] = task
        logger.info(f"注册自定义任务: {name}")
        return task
    
    def register_handler(self, task_id: str, handler: Callable):
        """注册任务处理函数"""
        self._task_handlers[task_id] = handler
    
    async def execute_task(self, task: ScheduledTask) -> Dict:
        """执行任务"""
        logger.info(f"执行任务: {task.name}")
        task.last_run = datetime.now().isoformat()
        
        try:
            if self.n8n_executor:
                result = await self.n8n_executor.execute(
                    task.workflow,
                    task.params
                )
            else:
                # 如果没有 N8N 执行器，模拟执行
                result = {
                    "success": True,
                    "mode": "simulated",
                    "workflow": task.workflow,
                    "params": task.params
                }
            
            # 调用成功回调
            if task.on_success and task.on_success in self._task_handlers:
                await self._task_handlers[task.on_success](result)
            
            logger.info(f"任务完成: {task.name}, 结果: {result}")
            return result
            
        except Exception as e:
            logger.error(f"任务执行失败: {task.name}, 错误: {e}")
            
            # 调用失败回调
            if task.on_failure and task.on_failure in self._task_handlers:
                await self._task_handlers[task.on_failure](e)
            
            return {"success": False, "error": str(e)}
    
    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        return self.tasks.get(task_id)
    
    def get_all_tasks(self) -> List[ScheduledTask]:
        return list(self.tasks.values())
    
    def enable_task(self, task_id: str):
        if task_id in self.tasks:
            self.tasks[task_id].enabled = True
            logger.info(f"启用任务: {task_id}")
    
    def disable_task(self, task_id: str):
        if task_id in self.tasks:
            self.tasks[task_id].enabled = False
            logger.info(f"禁用任务: {task_id}")
    
    async def run_scheduler(self):
        """运行调度器（主循环）"""
        self._running = True
        logger.info("任务调度器启动")
        
        while self._running:
            now = datetime.now()
            
            for task_id, task in self.tasks.items():
                if not task.enabled:
                    continue
                
                # 简单调度：每小时执行一次
                # 实际生产环境应使用真正的 cron 解析器
                if task.last_run:
                    last = datetime.fromisoformat(task.last_run)
                    hours_diff = (now - last).total_seconds() / 3600
                    
                    # 根据 schedule 简单判断是否需要执行
                    if self._should_run(task.schedule, hours_diff):
                        await self.execute_task(task)
                elif task.last_run is None:
                    # 首次执行
                    await self.execute_task(task)
            
            await asyncio.sleep(60)  # 每分钟检查一次
    
    def _should_run(self, schedule: str, hours_diff: float) -> bool:
        """简单判断是否应该执行"""
        schedule = schedule.strip()
        
        # 每天一次
        if schedule.startswith("0 ") and hours_diff >= 23:
            return True
        
        # 每小时
        if schedule == "hourly" and hours_diff >= 1:
            return True
        
        # 每周
        if schedule.startswith("0 ") and hours_diff >= 168:  # 7天
            return True
        
        return False
    
    def stop(self):
        """停止调度器"""
        self._running = False
        logger.info("任务调度器停止")


# ========== 全局调度器实例 ==========

_scheduler: Optional[TaskScheduler] = None


async def get_task_scheduler() -> TaskScheduler:
    """获取全局任务调度器"""
    global _scheduler
    if _scheduler is None:
        from core.harness.n8n_bridge import get_harness_executor
        executor = await get_harness_executor()
        _scheduler = TaskScheduler(executor)
        
        # 注册内置任务
        _scheduler.register_template("daily_health_check")
        _scheduler.register_template("daily_backup_check")
        _scheduler.register_template("weekly_report")
    
    return _scheduler


async def start_scheduler():
    """启动调度器"""
    scheduler = await get_task_scheduler()
    asyncio.create_task(scheduler.run_scheduler())


# ========== 任务触发 API ==========

async def trigger_task(task_id: str) -> Dict:
    """手动触发任务"""
    scheduler = await get_task_scheduler()
    task = scheduler.get_task(task_id)
    
    if not task:
        return {"success": False, "error": f"任务不存在: {task_id}"}
    
    result = await scheduler.execute_task(task)
    return result


async def list_tasks() -> List[Dict]:
    """列出所有任务"""
    scheduler = await get_task_scheduler()
    tasks = scheduler.get_all_tasks()
    return [
        {
            "task_id": t.task_id,
            "name": t.name,
            "workflow": t.workflow,
            "schedule": t.schedule,
            "enabled": t.enabled,
            "last_run": t.last_run
        }
        for t in tasks
    ]


async def toggle_task(task_id: str, enabled: bool) -> Dict:
    """启用/禁用任务"""
    scheduler = await get_task_scheduler()
    
    if enabled:
        scheduler.enable_task(task_id)
    else:
        scheduler.disable_task(task_id)
    
    return {"success": True, "task_id": task_id, "enabled": enabled}