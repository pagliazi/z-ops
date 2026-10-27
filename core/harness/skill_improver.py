"""
Z-OPS Harness Skill Improver v2.2 - Advanced Self-Improving Skill System
技能自我改进器（进阶版）- 参考 Hermes Agent 架构增强

核心功能:
1. 分析技能使用模式 + 根因分析
2. LLM 驱动的改进建议生成
3. A/B 测试与改进验证闭环
4. 与 SelfImprovingEngine 深度联动
5. 改进效果追踪与自动回滚
"""

import logging
import json
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
import asyncio

logger = logging.getLogger("zops.harness.improver")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
IMPROVEMENT_DB_PATH = DATA_DIR / "skill_improvements.json"


@dataclass
class Feedback:
    """技能使用反馈"""
    feedback_id: str
    skill_name: str
    session_id: str
    user_id: str
    timestamp: str
    success: bool
    execution_time: float = 0.0
    user_rating: int = 0  # 1-5
    user_comment: str = ""
    error_type: str = ""
    error_message: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    # v2.2 新增
    root_cause: str = ""          # 根因分类
    strategy_used: str = "default" # 使用的策略


@dataclass
class UsageAnalysis:
    """技能使用分析（增强版）"""
    skill_name: str
    total_uses: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_execution_time: float = 0.0
    avg_user_rating: float = 0.0
    recent_success_rate: float = 1.0  # 最近30次
    common_errors: List[str] = field(default_factory=list)
    improvement_needed: bool = False
    last_updated: str = ""
    # v2.2 新增
    trend: str = "stable"               # improving / degrading / stable
    root_cause_distribution: Dict[str, int] = field(default_factory=dict)
    consecutive_failures: int = 0
    best_strategy: str = "default"
    strategy_scores: Dict[str, float] = field(default_factory=dict)


@dataclass
class Improvement:
    """技能改进（增强版）"""
    improvement_id: str
    skill_name: str
    trigger_type: str  # success_rate_drop / error_repeat / user_feedback / auto / llm_reflect
    trigger_details: Dict[str, Any] = field(default_factory=dict)
    
    # 改进内容
    suggested_changes: Dict[str, str] = field(default_factory=dict)
    rationale: str = ""
    confidence: float = 0.5
    
    # 状态
    status: str = "pending"  # pending / applied / rejected / testing / verified / rolled_back
    applied_at: str = ""
    applied_version: str = ""
    test_results: Dict[str, Any] = field(default_factory=dict)
    
    created_at: str = ""
    # v2.2 新增
    source: str = "rule"  # rule / llm / reflection / transfer
    pre_apply_metrics: Dict[str, Any] = field(default_factory=dict)   # 应用前的指标快照
    post_apply_metrics: Dict[str, Any] = field(default_factory=dict)  # 应用后的指标快照
    verification_status: str = ""  # improved / degraded / neutral
    rollback_reason: str = ""


class SkillImprover:
    """
    技能持续改进器 v2.2

    参考 Hermes Agent 的技能自我改进机制，增强:
    1. 与 SelfImprovingEngine 深度联动 — 统一经验库、根因分析
    2. LLM 驱动改进生成 — 不再纯规则，用 LLM 生成改进方案
    3. A/B 测试框架 — 改进前拍快照，改进后验证效果
    4. 自动回滚 — 改进后若指标恶化则自动回滚
    """

    # 改进触发阈值
    TRIGGER_THRESHOLDS = {
        "success_rate_min": 0.8,
        "error_repeat_min": 3,
        "user_rating_min": 3,
        "uses_before_improve": 10,
        # v2.2 新增
        "consecutive_failures_trigger": 5,   # 连续失败5次触发
        "degradation_trigger": 0.15,         # 成功率下降15%触发
        "verification_window": 20,           # 改进后20次执行内验证
    }

    # A/B 测试配置
    AB_TEST_USES_REQUIRED = 10  # A/B 测试需要6次执行后才判定

    def __init__(self, llm_client=None, redis_client=None):
        self.llm = llm_client
        self.redis = redis_client
        self._feedbacks: Dict[str, List[Feedback]] = {}
        self._improvements: Dict[str, Improvement] = {}
        self._load_from_disk()

    def _load_from_disk(self):
        """加载改进历史"""
        if IMPROVEMENT_DB_PATH.exists():
            try:
                data = json.loads(IMPROVEMENT_DB_PATH.read_text())
                for skill_name, fbs in data.get("feedbacks", {}).items():
                    self._feedbacks[skill_name] = []
                    for f in fbs:
                        # 兼容旧版本数据
                        valid_fields = {field.name for field in Feedback.__dataclass_fields__.values()}
                        filtered = {k: v for k, v in f.items() if k in valid_fields}
                        self._feedbacks[skill_name].append(Feedback(**filtered))
                for iid, imp in data.get("improvements", {}).items():
                    valid_fields = {field.name for field in Improvement.__dataclass_fields__.values()}
                    filtered = {k: v for k, v in imp.items() if k in valid_fields}
                    self._improvements[iid] = Improvement(**filtered)
                logger.info(f"加载改进历史: {len(self._feedbacks)} 技能, {len(self._improvements)} 改进")
            except Exception as e:
                logger.warning(f"加载改进历史失败: {e}")

    def _save_to_disk(self):
        """保存到磁盘"""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "feedbacks": {
                    skill: [asdict(f) for f in fbs]
                    for skill, fbs in self._feedbacks.items()
                },
                "improvements": {
                    iid: asdict(imp) for iid, imp in self._improvements.items()
                },
                "updated_at": datetime.now().isoformat()
            }
            IMPROVEMENT_DB_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.error(f"保存改进历史失败: {e}")

    # ───────────────────────────────────────────
    #  反馈记录 (联动 SelfImprovingEngine)
    # ───────────────────────────────────────────

    async def record_feedback(
        self,
        skill_name: str,
        session_id: str,
        user_id: str,
        success: bool,
        execution_time: float = 0.0,
        user_rating: int = 0,
        user_comment: str = "",
        error_type: str = "",
        error_message: str = "",
        context: Dict = None,
        root_cause: str = "",
        strategy_used: str = "default",
    ) -> Feedback:
        """
        记录技能使用反馈

        联动: 同时通知 SelfImprovingEngine 记录执行经验
        """
        import uuid

        # 如果未提供 root_cause，通过 SelfImprovingEngine 分析
        if not root_cause and not success and error_message:
            try:
                from core.self_improving import get_improving_engine
                engine = get_improving_engine()
                rca_result = engine.rca.analyze(error_message, context)
                root_cause = rca_result.category.value
            except Exception:
                root_cause = error_type or ""

        fb = Feedback(
            feedback_id=f"fb_{uuid.uuid4().hex[:8]}",
            skill_name=skill_name,
            session_id=session_id,
            user_id=user_id,
            timestamp=datetime.now().isoformat(),
            success=success,
            execution_time=execution_time,
            user_rating=user_rating,
            user_comment=user_comment,
            error_type=error_type,
            error_message=error_message,
            context=context or {},
            root_cause=root_cause,
            strategy_used=strategy_used,
        )

        if skill_name not in self._feedbacks:
            self._feedbacks[skill_name] = []
        self._feedbacks[skill_name].append(fb)

        # 限制反馈数量
        if len(self._feedbacks[skill_name]) > 200:
            self._feedbacks[skill_name] = self._feedbacks[skill_name][-200:]

        self._save_to_disk()

        # 检查是否触发改进
        await self._check_improvement_trigger(skill_name)

        # 检查已应用的改进是否需要验证
        await self._check_improvement_verification(skill_name)

        logger.debug(f"记录反馈: {skill_name} ({'成功' if success else '失败'}) root_cause={root_cause}")
        return fb

    # ───────────────────────────────────────────
    #  触发检查 (增强版)
    # ───────────────────────────────────────────

    async def _check_improvement_trigger(self, skill_name: str):
        """检查是否需要触发改进（增强版）"""
        analysis = await self.analyze_usage_patterns(skill_name)
        triggers = []

        # 1. 成功率下降
        if analysis.total_uses >= self.TRIGGER_THRESHOLDS["uses_before_improve"]:
            if analysis.recent_success_rate < self.TRIGGER_THRESHOLDS["success_rate_min"]:
                triggers.append({
                    "type": "success_rate_drop",
                    "details": {
                        "success_rate": analysis.recent_success_rate,
                        "threshold": self.TRIGGER_THRESHOLDS["success_rate_min"]
                    }
                })

        # 2. 错误重复 (现在基于根因分类，不只是字符串)
        root_cause_counts = {}
        for fb in self._feedbacks.get(skill_name, [])[-30:]:
            if not fb.success:
                key = fb.root_cause or fb.error_type or "unknown"
                root_cause_counts[key] = root_cause_counts.get(key, 0) + 1

        for rc, count in root_cause_counts.items():
            if count >= self.TRIGGER_THRESHOLDS["error_repeat_min"]:
                triggers.append({
                    "type": "error_repeat",
                    "details": {
                        "root_cause": rc,
                        "count": count,
                        "threshold": self.TRIGGER_THRESHOLDS["error_repeat_min"]
                    }
                })

        # 3. 用户评分低
        if analysis.avg_user_rating > 0 and analysis.avg_user_rating < self.TRIGGER_THRESHOLDS["user_rating_min"]:
            triggers.append({
                "type": "user_feedback",
                "details": {
                    "avg_rating": analysis.avg_user_rating,
                    "threshold": self.TRIGGER_THRESHOLDS["user_rating_min"]
                }
            })

        # 4. v2.2: 连续失败触发
        if analysis.consecutive_failures >= self.TRIGGER_THRESHOLDS["consecutive_failures_trigger"]:
            triggers.append({
                "type": "consecutive_failure",
                "details": {
                    "count": analysis.consecutive_failures,
                    "threshold": self.TRIGGER_THRESHOLDS["consecutive_failures_trigger"]
                }
            })

        # 5. v2.2: 趋势恶化触发
        if analysis.trend == "degrading":
            triggers.append({
                "type": "degradation",
                "details": {
                    "trend": analysis.trend,
                    "recent_rate": analysis.recent_success_rate
                }
            })

        # 生成改进建议
        for trigger in triggers:
            improvement = await self._generate_improvement(skill_name, trigger, analysis)
            if improvement:
                self._improvements[improvement.improvement_id] = improvement
                self._save_to_disk()
                logger.info(f"生成改进建议: {improvement.improvement_id} ({trigger['type']}) source={improvement.source}")

    # ───────────────────────────────────────────
    #  使用模式分析 (增强版)
    # ───────────────────────────────────────────

    async def analyze_usage_patterns(self, skill_name: str) -> UsageAnalysis:
        """
        分析技能使用模式（增强版）

        联动 SelfImprovingEngine 获取更丰富的分析数据
        """
        feedbacks = self._feedbacks.get(skill_name, [])

        if not feedbacks:
            return UsageAnalysis(skill_name=skill_name)

        total = len(feedbacks)
        success = len([f for f in feedbacks if f.success])
        failure = total - success

        # 最近30次成功率
        recent = feedbacks[-30:]
        recent_success = len([f for f in recent if f.success])
        recent_rate = recent_success / len(recent) if recent else 1.0

        # 平均执行时间
        times = [f.execution_time for f in feedbacks if f.execution_time > 0]
        avg_time = sum(times) / len(times) if times else 0

        # 平均评分
        ratings = [f.user_rating for f in feedbacks if f.user_rating > 0]
        avg_rating = sum(ratings) / len(ratings) if ratings else 0

        # 根因分布
        root_cause_dist = {}
        for fb in feedbacks:
            if not fb.success:
                rc = fb.root_cause or fb.error_type or "unknown"
                root_cause_dist[rc] = root_cause_dist.get(rc, 0) + 1

        # 常见错误 (基于根因)
        common_errors = sorted(root_cause_dist.keys(), key=lambda x: root_cause_dist[x], reverse=True)[:5]

        # 趋势分析
        older = feedbacks[-60:-30] if len(feedbacks) > 30 else []
        older_rate = len([f for f in older if f.success]) / len(older) if older else recent_rate
        if recent_rate > older_rate + 0.1:
            trend = "improving"
        elif recent_rate < older_rate - 0.1:
            trend = "degrading"
        else:
            trend = "stable"

        # 连续失败计数
        consecutive = 0
        for fb in reversed(feedbacks):
            if not fb.success:
                consecutive += 1
            else:
                break

        # 策略分数 (从 SelfImprovingEngine 获取)
        strategy_scores = {}
        best_strategy = "default"
        try:
            from core.self_improving import get_improving_engine
            engine = get_improving_engine()
            strategy_scores = engine.get_strategy_scores(skill_name)
            best_strategy = engine.get_best_strategy(skill_name)
        except Exception:
            pass

        # 是否需要改进
        improvement_needed = (
            recent_rate < self.TRIGGER_THRESHOLDS["success_rate_min"] or
            (avg_rating > 0 and avg_rating < self.TRIGGER_THRESHOLDS["user_rating_min"]) or
            consecutive >= self.TRIGGER_THRESHOLDS["consecutive_failures_trigger"] or
            trend == "degrading"
        )

        return UsageAnalysis(
            skill_name=skill_name,
            total_uses=total,
            success_count=success,
            failure_count=failure,
            avg_execution_time=avg_time,
            avg_user_rating=avg_rating,
            recent_success_rate=recent_rate,
            common_errors=common_errors,
            improvement_needed=improvement_needed,
            last_updated=datetime.now().isoformat(),
            trend=trend,
            root_cause_distribution=root_cause_dist,
            consecutive_failures=consecutive,
            best_strategy=best_strategy,
            strategy_scores=strategy_scores,
        )

    # ───────────────────────────────────────────
    #  改进生成 (增强: LLM 驱动 + 反思联动)
    # ───────────────────────────────────────────

    async def _generate_improvement(
        self,
        skill_name: str,
        trigger: Dict,
        analysis: UsageAnalysis
    ) -> Optional[Improvement]:
        """生成改进建议（增强版: LLM + 规则 双路径）"""
        import uuid

        # 获取当前技能
        from .skill_registry import get_skill_registry
        registry = await get_skill_registry()
        current_skill = registry.get(skill_name)

        if not current_skill:
            logger.warning(f"找不到技能: {skill_name}")
            return None

        # 先尝试 LLM 生成改进
        improvement = await self._llm_generate_improvement(skill_name, trigger, analysis, current_skill)

        # 如果 LLM 失败，回退规则生成
        if not improvement:
            improvement = self._rule_generate_improvement(skill_name, trigger, analysis, current_skill)

        if improvement:
            # 去重: 同一 skill + trigger_type + error 模式已有 pending 改进则跳过
            trigger_type = improvement.trigger_type
            error_type = improvement.trigger_details.get("error_type", "")
            for existing in self._improvements.values():
                if (existing.status == "pending" 
                    and existing.skill_name == skill_name
                    and existing.trigger_type == trigger_type
                    and existing.trigger_details.get("error_type", "") == error_type):
                    logger.info(f"改进去重: {skill_name} 已有 pending 改进 ({trigger_type}:{error_type})，跳过")
                    return None
            
            # 记录应用前的指标快照 (用于 A/B 验证)
            improvement.pre_apply_metrics = {
                "success_rate": analysis.recent_success_rate,
                "avg_execution_time": analysis.avg_execution_time,
                "total_uses": analysis.total_uses,
                "snapshot_at": datetime.now().isoformat(),
            }

        return improvement

    async def _llm_generate_improvement(
        self,
        skill_name: str,
        trigger: Dict,
        analysis: UsageAnalysis,
        current_skill: dict,
    ) -> Optional[Improvement]:
        """使用 LLM 生成改进建议"""
        if not self.llm:
            # 尝试从 SelfImprovingEngine 获取反思结果
            try:
                from core.self_improving import get_improving_engine
                engine = get_improving_engine()
                reflection = await engine.maybe_reflect(skill_name)
                if reflection and reflection.what_to_improve:
                    import uuid
                    return Improvement(
                        improvement_id=f"imp_{uuid.uuid4().hex[:8]}",
                        skill_name=skill_name,
                        trigger_type=trigger["type"],
                        trigger_details=trigger["details"],
                        suggested_changes={"pitfalls": f"{current_skill.get('pitfalls', '')}\n- 反思建议: {reflection.what_to_improve}"},
                        rationale=reflection.summary or f"LLM 反思触发 ({trigger['type']})",
                        confidence=reflection.confidence * 0.8,
                        status="pending",
                        created_at=datetime.now().isoformat(),
                        source="reflection",
                    )
            except Exception:
                pass
            return None

        try:
            # 构建改进 prompt
            prompt = f"""你是技能优化专家。请分析以下技能的问题并给出具体改进方案。

技能名称: {skill_name}
当前版本: {current_skill.get('version', '1.0.0')}
触发原因: {trigger['type']} - {json.dumps(trigger['details'], ensure_ascii=False)}

使用统计:
- 总使用: {analysis.total_uses}次
- 成功率: {analysis.recent_success_rate:.1%}
- 趋势: {analysis.trend}
- 常见错误: {', '.join(analysis.common_errors[:3])}
- 根因分布: {json.dumps(analysis.root_cause_distribution, ensure_ascii=False)}

当前技能内容:
- 执行步骤: {current_skill.get('procedure', '')[:200]}
- 注意事项: {current_skill.get('pitfalls', '')[:200]}
- 验证方法: {current_skill.get('verification', '')[:200]}

请用JSON格式回答:
{{
  "pitfalls": "<更新后的注意事项，包含原有内容+新增建议>",
  "procedure": "<更新后的执行步骤，包含优化建议>",
  "rationale": "<改进理由，30字内>"
}}"""

            resp = await self.llm(prompt)
            if resp:
                data = json.loads(resp) if isinstance(resp, str) else resp
                import uuid
                suggested_changes = {}
                if data.get("pitfalls"):
                    suggested_changes["pitfalls"] = data["pitfalls"]
                if data.get("procedure"):
                    suggested_changes["procedure"] = data["procedure"]

                if suggested_changes:
                    return Improvement(
                        improvement_id=f"imp_{uuid.uuid4().hex[:8]}",
                        skill_name=skill_name,
                        trigger_type=trigger["type"],
                        trigger_details=trigger["details"],
                        suggested_changes=suggested_changes,
                        rationale=data.get("rationale", f"LLM 改进 ({trigger['type']})"),
                        confidence=0.75,
                        status="pending",
                        created_at=datetime.now().isoformat(),
                        source="llm",
                    )
        except Exception as e:
            logger.debug(f"LLM 生成改进失败: {e}")

        return None

    def _rule_generate_improvement(
        self,
        skill_name: str,
        trigger: Dict,
        analysis: UsageAnalysis,
        current_skill: dict,
    ) -> Optional[Improvement]:
        """规则生成改进建议（回退方案）"""
        import uuid

        suggested_changes = {}
        rationale = ""
        trigger_type = trigger["type"]

        if trigger_type == "success_rate_drop":
            rationale = f"成功率 {analysis.recent_success_rate:.1%} 低于阈值 {self.TRIGGER_THRESHOLDS['success_rate_min']:.1%}"
            failure_feedbacks = [f for f in self._feedbacks.get(skill_name, [])[-20:] if not f.success]
            if failure_feedbacks:
                # 基于根因分布生成pitfalls
                top_causes = sorted(analysis.root_cause_distribution.items(), key=lambda x: x[1], reverse=True)[:3]
                new_pitfalls = current_skill.get('pitfalls', '')
                for cause, count in top_causes:
                    new_pitfalls += f"\n- 注意 [{cause}] 错误 (已出现{count}次)"
                suggested_changes["pitfalls"] = new_pitfalls

        elif trigger_type == "error_repeat":
            rc = trigger["details"].get("root_cause", "unknown")
            count = trigger["details"].get("count", 0)
            rationale = f"根因 '{rc}' 重复出现 {count} 次"
            suggested_changes["pitfalls"] = f"{current_skill.get('pitfalls', '')}\n- 重点避免: {rc} 类型错误"
            suggested_changes["verification"] = f"{current_skill.get('verification', '')}\n- 检查是否出现 {rc} 错误"

        elif trigger_type == "user_feedback":
            rationale = f"用户平均评分 {analysis.avg_user_rating:.1f} 低于阈值"
            comments = [f.user_comment for f in self._feedbacks.get(skill_name, [])[-10:] if f.user_comment]
            if comments:
                suggested_changes["description"] = f"{current_skill.get('description', '')}\n用户反馈: {comments[0][:100]}"

        elif trigger_type == "consecutive_failure":
            count = trigger["details"].get("count", 0)
            rationale = f"连续失败 {count} 次，需紧急介入"
            suggested_changes["pitfalls"] = f"{current_skill.get('pitfalls', '')}\n- 紧急: 连续失败{count}次，需检查基础环境"

        elif trigger_type == "degradation":
            rationale = f"技能性能持续恶化，近期成功率 {analysis.recent_success_rate:.1%}"
            suggested_changes["pitfalls"] = f"{current_skill.get('pitfalls', '')}\n- 性能退化警告: 近期成功率下降"

        if not suggested_changes:
            return None

        return Improvement(
            improvement_id=f"imp_{uuid.uuid4().hex[:8]}",
            skill_name=skill_name,
            trigger_type=trigger_type,
            trigger_details=trigger["details"],
            suggested_changes=suggested_changes,
            rationale=rationale,
            confidence=0.6,
            status="pending",
            created_at=datetime.now().isoformat(),
            source="rule",
        )

    # ───────────────────────────────────────────
    #  改进应用 & A/B 验证闭环
    # ───────────────────────────────────────────

    async def apply_improvement(
        self,
        improvement_id: str,
        force: bool = False
    ) -> bool:
        """
        应用改进（增强: 带指标快照，用于后续验证）
        """
        if improvement_id not in self._improvements:
            logger.warning(f"找不到改进: {improvement_id}")
            return False

        improvement = self._improvements[improvement_id]

        if improvement.status not in ("pending", "testing") and not force:
            logger.warning(f"改进已处理: {improvement_id} ({improvement.status})")
            return False

        from .skill_registry import get_skill_registry
        registry = await get_skill_registry()
        current_skill = registry.get(improvement.skill_name)

        if not current_skill:
            logger.error(f"找不到技能: {improvement.skill_name}")
            return False

        # 应用改进
        new_skill = dict(current_skill)
        for fld, new_value in improvement.suggested_changes.items():
            new_skill[fld] = new_value

        # 版本更新
        current_version = current_skill.get("version", "1.0.0")
        parts = current_version.split(".")
        if len(parts) == 3:
            parts[2] = str(int(parts[2]) + 1)
            new_version = ".".join(parts)
        else:
            new_version = "1.0.1"

        new_skill["version"] = new_version
        new_skill["updated_at"] = datetime.now().isoformat()

        # v4.0 (Phase 5.2): A/B 基准门 — RCA 评测集 ground truth 判定,
        # 新策略命中率 ≥ 旧 +5pp 才允许自动上线 (EMA 分数仅作参考指标)
        try:
            from tests.rca.benchmark import gate_improvement_on_rca
            new_skill["_suggested_changes"] = improvement.suggested_changes
            passed, detail = gate_improvement_on_rca(new_skill, current_skill)
            new_skill.pop("_suggested_changes", None)
            improvement.pre_apply_metrics["rca_gate"] = detail
            if not passed:
                improvement.status = "rejected"
                improvement.verification_status = f"rca_benchmark_failed: {detail}"
                self._save_to_disk()
                logger.warning(f"改进 {improvement_id} 未过 RCA 基准门: {detail}")
                return False
            logger.info(f"改进 {improvement_id} 通过 RCA 基准门: {detail}")
        except ImportError:
            new_skill.pop("_suggested_changes", None)
            logger.debug("tests.rca.benchmark 不可用 (裁剪环境), 跳过基准门")
        except Exception as gate_err:
            new_skill.pop("_suggested_changes", None)
            logger.warning(f"RCA 基准门异常 (保守放行): {gate_err}")

        await registry.save_skill(new_skill)

        # 更新状态
        improvement.status = "applied"
        improvement.applied_at = datetime.now().isoformat()
        improvement.applied_version = new_version

        self._save_to_disk()
        logger.info(f"应用改进: {improvement_id} -> {improvement.skill_name} v{new_version} (source={improvement.source})")
        return True

    async def _check_improvement_verification(self, skill_name: str):
        """
        检查已应用的改进是否需要验证

        A/B 验证闭环: 改进应用后，在指定窗口内比较前后指标
        """
        for imp in self._improvements.values():
            if imp.skill_name != skill_name:
                continue
            if imp.status != "applied":
                continue
            if imp.verification_status:  # 已验证
                continue

            # 检查是否有足够的后续反馈
            applied_at = imp.applied_at
            post_feedbacks = [
                f for f in self._feedbacks.get(skill_name, [])
                if f.timestamp > applied_at
            ]

            if len(post_feedbacks) < self.AB_TEST_USES_REQUIRED:
                continue  # 还不够，继续等待

            # 计算后续指标
            post_success = len([f for f in post_feedbacks if f.success])
            post_rate = post_success / len(post_feedbacks)
            post_times = [f.execution_time for f in post_feedbacks if f.execution_time > 0]
            post_avg_time = sum(post_times) / len(post_times) if post_times else 0

            imp.post_apply_metrics = {
                "success_rate": post_rate,
                "avg_execution_time": post_avg_time,
                "sample_size": len(post_feedbacks),
                "verified_at": datetime.now().isoformat(),
            }

            # 对比前后
            pre_rate = imp.pre_apply_metrics.get("success_rate", 0)
            if post_rate >= pre_rate + 0.05:
                imp.verification_status = "improved"
                logger.info(f"✅ 改进验证通过: {imp.improvement_id} "
                           f"({pre_rate:.0%} → {post_rate:.0%})")
            elif post_rate < pre_rate - 0.1:
                imp.verification_status = "degraded"
                logger.warning(f"❌ 改进导致退化: {imp.improvement_id} "
                              f"({pre_rate:.0%} → {post_rate:.0%})，建议回滚")
                # 自动回滚建议
                await self._suggest_rollback(imp)
            else:
                imp.verification_status = "neutral"
                logger.info(f"➖ 改进效果中性: {imp.improvement_id} "
                           f"({pre_rate:.0%} → {post_rate:.0%})")

            self._save_to_disk()

    async def _suggest_rollback(self, improvement: Improvement):
        """建议回滚恶化的改进"""
        try:
            from .skill_registry import get_skill_registry
            registry = await get_skill_registry()
            old_version = improvement.pre_apply_metrics.get("version", "")

            if old_version:
                success = await registry.rollback_version(improvement.skill_name, old_version)
                if success:
                    improvement.status = "rolled_back"
                    improvement.rollback_reason = f"指标退化: {improvement.post_apply_metrics.get('success_rate', 0):.0%}"
                    logger.info(f"自动回滚: {improvement.improvement_id} -> v{old_version}")
                    self._save_to_disk()
        except Exception as e:
            logger.warning(f"回滚失败: {e}")

    # ───────────────────────────────────────────
    #  原有接口 (完整保留)
    # ───────────────────────────────────────────

    async def suggest_improvements(
        self,
        skill_name: str,
        feedbacks: List[Feedback] = None
    ) -> List[Improvement]:
        """手动请求改进建议"""
        if feedbacks is None:
            feedbacks = self._feedbacks.get(skill_name, [])

        analysis = await self.analyze_usage_patterns(skill_name)
        improvements = []

        if analysis.improvement_needed:
            trigger = {"type": "manual", "details": {"analysis": asdict(analysis)}}
            imp = await self._generate_improvement(skill_name, trigger, analysis)
            if imp:
                improvements.append(imp)

        return improvements

    async def self_refine_on_failure(
        self,
        skill_name: str,
        failure: Feedback
    ) -> Optional[Improvement]:
        """
        失败时自动改进（增强: 联动反思引擎）
        """
        if failure.success:
            return None

        if skill_name not in self._feedbacks:
            self._feedbacks[skill_name] = []
        self._feedbacks[skill_name].append(failure)

        analysis = await self.analyze_usage_patterns(skill_name)

        trigger = {
            "type": "failure_refine",
            "details": {
                "root_cause": failure.root_cause or failure.error_type,
                "error_message": failure.error_message[:200]
            }
        }

        improvement = await self._generate_improvement(skill_name, trigger, analysis)

        if improvement:
            improvement.confidence = 0.55
            self._improvements[improvement.improvement_id] = improvement
            self._save_to_disk()
            logger.info(f"自动改进生成: {improvement.improvement_id} (source={improvement.source})")

        return improvement


    async def auto_apply_pending(self, min_confidence: float = 0.6) -> int:
        """自动应用所有满足置信度阈值的 pending 改进，返回应用数量"""
        applied = 0
        pending = [imp for imp in self._improvements.values() if imp.status == "pending"]
        
        # 按置信度排序，优先应用高置信度改进
        pending.sort(key=lambda x: x.confidence, reverse=True)
        
        for imp in pending:
            if imp.confidence < min_confidence:
                continue
            try:
                success = await self.apply_improvement(imp.improvement_id)
                if success:
                    applied += 1
                    logger.info(f"自动应用改进: {imp.improvement_id} -> {imp.skill_name} (confidence={imp.confidence:.2f})")
            except Exception as e:
                logger.warning(f"自动应用改进失败: {imp.improvement_id}: {e}")
        
        return applied

    async def check_verification_all(self) -> int:
        """检查所有已应用改进的验证状态，返回已验证数量"""
        verified = 0
        for imp in self._improvements.values():
            if imp.status == "applied" and not imp.verification_status:
                try:
                    await self._check_improvement_verification(imp.skill_name)
                    if imp.verification_status:
                        verified += 1
                except Exception as e:
                    logger.debug(f"验证检查失败: {imp.improvement_id}: {e}")
        return verified

    async def get_pending_improvements(self, skill_name: str = None) -> List[Improvement]:
        """获取待处理的改进"""
        pending = [imp for imp in self._improvements.values() if imp.status == "pending"]
        if skill_name:
            pending = [imp for imp in pending if imp.skill_name == skill_name]
        return pending

    async def reject_improvement(self, improvement_id: str, reason: str = ""):
        """拒绝改进"""
        if improvement_id not in self._improvements:
            return
        self._improvements[improvement_id].status = "rejected"
        self._improvements[improvement_id].test_results = {"rejected_reason": reason}
        self._save_to_disk()
        logger.info(f"拒绝改进: {improvement_id}")

    async def get_skill_history(self, skill_name: str) -> Dict:
        """获取技能的改进历史（增强版）"""
        feedbacks = self._feedbacks.get(skill_name, [])
        improvements = [imp for imp in self._improvements.values() if imp.skill_name == skill_name]

        verified = [imp for imp in improvements if imp.verification_status]
        improved = len([imp for imp in verified if imp.verification_status == "improved"])
        degraded = len([imp for imp in verified if imp.verification_status == "degraded"])

        return {
            "feedbacks_count": len(feedbacks),
            "improvements_count": len(improvements),
            "verified_count": len(verified),
            "improved_count": improved,
            "degraded_count": degraded,
            "improvements": [asdict(imp) for imp in improvements],
        }


# 单例
_improver_instance: Optional[SkillImprover] = None


async def get_skill_improver() -> SkillImprover:
    """获取改进器实例"""
    global _improver_instance
    if _improver_instance is None:
        _improver_instance = SkillImprover()
    return _improver_instance