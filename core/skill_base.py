"""
Z-OPS v2.2.0 — Skill 基类（进阶版）
所有 Skill 的基础接口定义

增强:
- 执行规划 (plan_execution) — 基于历史的预执行规划
- 策略自适应 (strategy) — 动态选择最优策略
- 参数自调整 (param_adjust) — 基于历史失败自动调整参数
- 反思触发 (reflection) — 连续失败后自动触发 LLM 反思
- 跨技能迁移 (transfer) — 从相似技能获取经验
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time

logger = logging.getLogger("zops")


@dataclass
class SkillParam:
    """Skill 参数定义"""
    name: str = ""
    type: str = "string"  # string / int / float / bool / list
    description: str = ""
    required: bool = False
    default: Any = None


@dataclass
class SkillResult:
    """Skill 执行结果"""
    success: bool = True
    data: Dict[str, Any] = field(default_factory=dict)
    text: str = ""
    error: str = ""

    def to_reply_dict(self) -> dict:
        """转换为 Agent reply 格式"""
        if self.error:
            return {"error": self.error}
        result = dict(self.data)
        if self.text:
            result["text"] = self.text
        return result


class BaseSkill(ABC):
    """
    Skill 基类 (v2.2.0 Advanced Self-Improving)

    子类需要:
    1. 设置 name, description
    2. 实现 execute() 方法
    3. 可选：重写 on_before_execute / on_after_execute 钩子

    Self-Improving 进阶特性:
    - 执行规划: 基于历史经验自动规划执行策略
    - 策略自适应: 根据历史成功率自动选择最佳策略
    - 参数自调整: 基于历史失败模式自动调整参数
    - LLM 反思: 连续失败时自动触发深度反思
    - 跨技能迁移: 从相似技能的经验中学习
    """

    name: str = ""
    description: str = ""
    parameters: list = []  # List[SkillParam]

    # Self-improving 配置
    enable_learning: bool = True
    confidence_score: float = 0.5

    # v2.2 新增: 策略与跨技能配置
    _current_strategy: str = "default"      # 当前执行策略
    skill_tags: list = []                    # 技能标签 (用于跨技能迁移)
    enable_reflection: bool = True           # 是否启用 LLM 反思
    enable_param_adjustment: bool = True     # 是否启用参数自调整

    @abstractmethod
    async def execute(self, params: Dict[str, Any], context: Dict[str, Any] = None) -> SkillResult:
        pass

    def validate_params(self, params: Dict[str, Any]) -> Optional[str]:
        for p in self.parameters:
            if isinstance(p, SkillParam) and p.required and p.name not in params:
                return f"缺少必需参数：{p.name}"
        return None

    async def on_before_execute(self, params: Dict[str, Any], context: Dict[str, Any] = None):
        """
        执行前钩子 (v2.2 增强)

        新增:
        - 执行规划生成
        - 策略自适应
        - 参数自调整
        - 跨技能迁移洞察
        """
        if not self.enable_learning:
            return

        try:
            from core.self_improving import get_improving_engine
            engine = get_improving_engine()

            # 注册技能标签 (用于跨技能迁移)
            if self.skill_tags:
                engine.register_skill_tags(self.name, self.skill_tags)

            # 1) 执行规划
            plan = engine.plan_execution(self.name, params)
            if plan.warnings:
                for w in plan.warnings:
                    logger.info(f"[{self.name}] 规划警告: {w}")

            # 2) 策略自适应
            self._current_strategy = plan.strategy
            if plan.strategy != "default":
                logger.info(f"[{self.name}] 策略选择: {plan.strategy}")

            # 3) 参数自调整
            if self.enable_param_adjustment and plan.param_adjustments:
                for key, value in plan.param_adjustments.items():
                    old_val = params.get(key)
                    params[key] = value
                    logger.info(f"[{self.name}] 参数调整: {key}={old_val} → {value}")

            # 4) 信心评估
            self.confidence_score = plan.confidence

            # 5) 跨技能迁移洞察
            if self.skill_tags:
                transfer_insights = engine.get_transfer_insights(self.name, limit=2)
                if transfer_insights:
                    logger.debug(f"[{self.name}] 迁移洞察: {transfer_insights}")

            # 6) 优化建议
            recommendations = engine.get_recommendations(self.name, params, limit=3)
            if recommendations:
                logger.debug(f"[{self.name}] 优化建议: {recommendations}")

            logger.debug(f"[{self.name}] 执行规划: confidence={self.confidence_score:.2f} "
                        f"strategy={self._current_strategy} risk={plan.risk_level}")

        except Exception as e:
            logger.debug(f"[{self.name}] on_before_execute 异常 (不影响执行): {e}")

    async def on_after_execute(
        self,
        params: Dict[str, Any],
        result: SkillResult,
        execution_time: float,
        context: Dict[str, Any] = None
    ):
        """
        执行后钩子 (v2.2 增强)

        新增:
        - 记录执行策略
        - 触发 LLM 反思 (连续失败时)
        """
        if not self.enable_learning:
            return

        try:
            from core.self_improving import get_improving_engine
            engine = get_improving_engine()

            # 记录执行经验 (包含策略信息)
            engine.record_execution(
                skill_name=self.name,
                action="execute",
                input_params=params,
                output_result=result.to_dict() if hasattr(result, 'to_dict') else {"success": result.success, "text": result.text},
                success=result.success,
                error_message=result.error,
                execution_time=execution_time,
                context=context,
                strategy_used=self._current_strategy,
            )

            logger.debug(f"[{self.name}] 经验已记录 (success={result.success}, "
                        f"time={execution_time:.2f}s, strategy={self._current_strategy})")

            # 连续失败时触发反思
            if not result.success and self.enable_reflection:
                consecutive = engine.get_consecutive_failures(self.name)
                if consecutive >= 3:
                    reflection = await engine.maybe_reflect(self.name)
                    if reflection:
                        logger.warning(f"[{self.name}] LLM 反思: {reflection.summary}")
                        if reflection.what_to_improve:
                            logger.warning(f"[{self.name}] 改进建议: {reflection.what_to_improve}")

        except Exception as e:
            logger.debug(f"[{self.name}] on_after_execute 异常 (不影响结果): {e}")

    async def execute_with_learning(
        self,
        params: Dict[str, Any],
        context: Dict[str, Any] = None
    ) -> SkillResult:
        """
        带学习的执行方法（推荐调用此方法）

        自动调用:
        1. on_before_execute — 规划 + 策略 + 参数调整
        2. execute — 实际执行
        3. on_after_execute — 经验记录 + 反思触发
        """
        # 执行前钩子
        await self.on_before_execute(params, context)

        # 执行技能
        start_time = time.time()
        try:
            result = await self.execute(params, context)
        except Exception as e:
            result = SkillResult(success=False, error=str(e))
        execution_time = time.time() - start_time

        # 执行后钩子
        await self.on_after_execute(params, result, execution_time, context)

        return result

    def __repr__(self):
        return f"<Skill:{self.name} (confidence={self.confidence_score:.2f}, strategy={self._current_strategy})>"
