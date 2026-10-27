"""
Z-OPS Harness 技能生成器 - 集成自我改进机制
参考 Hermes Agent 的技能生成与改进循环
"""

import logging
import asyncio
from typing import Dict, List, Optional
from datetime import datetime
import uuid

logger = logging.getLogger("zops.harness.generator")


class FailureRecord:
    def __init__(self, task: str, error: str, context: dict, attempted_solution: str, agent_id: str = ""):
        self.id = str(uuid.uuid4())
        self.task = task
        self.error = error
        self.context = context
        self.attempted_solution = attempted_solution
        self.agent_id = agent_id
        self.timestamp = datetime.now().isoformat()
        self.resolved = False


class SkillGenerator:
    MIN_FAILURES = 3
    
    def __init__(self, llm_client=None):
        self.llm = llm_client
    
    async def generate_from_failures(self, failures: List[FailureRecord], context: dict = None) -> Optional[Dict]:
        if len(failures) < self.MIN_FAILURES:
            return None
        
        task_type = failures[0].task[:100]
        skill_name = "auto_" + "".join(c for c in task_type.lower() if c.isalnum() or c == "_")[:30]
        
        return {
            "name": skill_name,
            "description": f"自动生成: {task_type[:50]}",
            "version": "1.0.0",
            "author": "zops-system",
            "platforms": ["linux"],
            "metadata": {"zops": {"tags": ["自动生成"], "category": "auto"}},
            "when_to_use": task_type,
            "procedure": "\n".join([f"- 尝试: {f.attempted_solution}" for f in failures[:3]]),
            "pitfalls": "\n".join([f"- {f.error}" for f in failures[:3]]),
            "verification": "检查任务是否成功",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "usage_count": 0,
            # P2: 未验证成功率为 null/unknown, 不预置 1.0 (SPEC 红线)
            "success_rate": None,
            "auto_generated": True,
            "source_failure_id": failures[0].id if failures else None,
        }


class HarnessController:
    """
    Harness 控制器 - 协调技能生成与改进
    
    参考 Hermes Agent 的完整学习循环:
    1. 记录失败 -> 生成新技能
    2. 记录成功 -> 验证技能有效性
    3. 触发改进 -> 持续优化技能
    """
    
    def __init__(self, llm_client=None, redis_client=None):
        self.llm = llm_client
        self.redis = redis_client
        self.generator = SkillGenerator(llm_client)
        self._failure_buffer: List[FailureRecord] = []
        self._success_buffer: List[dict] = []  # 成功记录
        self._improver = None  # 延迟加载改进器
    
    async def record_failure(
        self, 
        task: str, 
        error: str, 
        context: dict, 
        attempted_solution: str, 
        agent_id: str = "",
        skill_name: str = ""  # 如果是已有技能失败
    ):
        """记录失败 - 触发技能生成或改进"""
        record = FailureRecord(task, error, context, attempted_solution, agent_id)
        self._failure_buffer.append(record)
        logger.info(f"记录失败: {task[:30]}... (缓冲: {len(self._failure_buffer)})")
        
        # 如果是已有技能的失败，触发改进
        if skill_name:
            await self._trigger_skill_improvement(skill_name, record)
        
        # 检查是否需要生成新技能
        if len(self._failure_buffer) >= 5:
            await self._check_and_generate()
    
    async def record_success(
        self, 
        task: str, 
        context: dict, 
        agent_id: str = "",
        skill_name: str = "",
        execution_time: float = 0.0
    ):
        """记录成功 - 用于技能验证和统计"""
        self._success_buffer.append({
            "task": task,
            "context": context,
            "agent_id": agent_id,
            "skill_name": skill_name,
            "execution_time": execution_time,
            "timestamp": datetime.now().isoformat()
        })
        logger.info(f"成功: {task[:30]}")
        
        # 更新技能成功率
        if skill_name:
            await self._update_skill_stats(skill_name, success=True, execution_time=execution_time)
    
    async def _update_skill_stats(self, skill_name: str, success: bool, execution_time: float = 0.0,
                                  verified: bool = False):
        """更新技能统计信息 (P2: 不再直写线上技能文件; 结果走生命周期 outcome 记录,
        成功率只从可核验证据计算, 未验证保持 null)"""
        try:
            from .skill_lifecycle import get_skill_lifecycle
            get_skill_lifecycle().record_outcome(
                skill_name, success=success,
                verified=bool(verified))   # verified 由调用方依验收证据判定
        except Exception as e:
            logger.warning(f"技能结果记录失败: {e}")
    
    async def _trigger_skill_improvement(self, skill_name: str, failure: FailureRecord):
        """触发技能改进"""
        try:
            # 延迟加载改进器避免循环导入
            from .skill_improver import get_skill_improver, Feedback
            improver = await get_skill_improver()
            
            # 创建失败反馈
            fb = Feedback(
                feedback_id=f"fb_{uuid.uuid4().hex[:8]}",
                skill_name=skill_name,
                session_id=failure.context.get("session_id", ""),
                user_id=failure.context.get("user_id", ""),
                timestamp=datetime.now().isoformat(),
                success=False,
                execution_time=0.0,
                error_type="execution_error",
                error_message=failure.error[:200],
                context=failure.context
            )
            
            # 记录到改进器，触发自动改进检查
            await improver.record_feedback(
                skill_name=skill_name,
                session_id=fb.session_id,
                user_id=fb.user_id,
                success=False,
                error_type=fb.error_type,
                error_message=fb.error_message,
                context=failure.context
            )
            
            logger.info(f"触发技能改进检查: {skill_name}")
        except Exception as e:
            logger.warning(f"触发改进失败: {e}")
    
    async def _check_and_generate(self):
        grouped = {}
        for f in self._failure_buffer:
            key = f.task[:50]
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(f)
        
        for task_key, failures in grouped.items():
            if len(failures) >= self.generator.MIN_FAILURES:
                # P2 (task-c17) 复盘隔离: 后台复盘只能写候选 (不自动发布),
                # 辅助模型预算独立限额, 超限暂停复盘 (不影响主链路)
                from core.harness.skill_lifecycle import get_skill_lifecycle, LifecycleError
                lc = get_skill_lifecycle()
                if lc.aux_budget_left() <= 0:
                    logger.warning("[harness] 复盘预算耗尽, 本轮候选生成暂停")
                    return
                skill_data = await self.generator.generate_from_failures(failures)
                if skill_data:
                    try:
                        lc.aux_budget_consume(1)
                        version = lc.submit_candidate(
                            skill_data, author="retro",
                            source_execution=failures[0].id,
                            evidence=[f"failure:{f.id}:{f.error[:80]}" for f in failures[:3]])
                        logger.info(f"复盘候选已提交: {skill_data['name']}@{version} "
                                    f"(待 evaluate→approve→publish, 不自动上线)")
                    except LifecycleError as e:
                        logger.warning(f"[harness] 候选提交被拒: {e}")
                    self._failure_buffer = [f for f in self._failure_buffer if f.task[:50] != task_key]
                    break
    
    async def get_failure_history(self, limit: int = 20) -> List[FailureRecord]:
        return self._failure_buffer[:limit]
    
    async def get_success_history(self, limit: int = 20) -> List[dict]:
        """获取成功历史"""
        return self._success_buffer[:limit]
    
    async def get_stats(self) -> dict:
        """获取 Harness 统计"""
        return {
            "failures_recorded": len(self._failure_buffer),
            "successes_recorded": len(self._success_buffer),
            "skills_generated": len([f for f in self._failure_buffer if f.resolved])
        }
    
    async def trigger_improvement_check(self, skill_name: str = None):
        """手动触发改进检查"""
        from .skill_improver import get_skill_improver
        improver = await get_skill_improver()
        pending = await improver.get_pending_improvements(skill_name)
        return pending
