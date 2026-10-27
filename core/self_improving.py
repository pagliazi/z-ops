"""
Z-OPS v2.2.0 — Advanced Self-Improving Skill System
自我进化技能系统（进阶版）

核心机制:
1. 经验记录 (Experience) — 结构化执行轨迹
2. LLM 反思分析 (Reflection) — 深度失败归因
3. 根因分析 (Root Cause Analysis) — 结构化故障分类
4. 经验衰减 (Decay) — 时间加权，近期经验权重更高
5. 跨技能迁移 (Transfer) — 相似技能共享经验
6. 策略自适应 (Strategy) — 动态执行策略选择
7. 执行规划 (Planning) — 基于历史的预执行规划
8. 信心校准 (Confidence) — 多维度信心评估

让 Z-OPS 从每次执行中深度学习，真正越来越聪明
"""

import json
import time
import math
import logging
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
import asyncio

logger = logging.getLogger("zops")


# ═══════════════════════════════════════════════════════════════
#  数据模型
# ═══════════════════════════════════════════════════════════════

class ErrorCategory(str, Enum):
    """错误根因分类"""
    TIMEOUT = "timeout"              # 超时
    CONNECTION = "connection"        # 连接失败
    AUTH = "auth"                    # 认证/权限
    PARAM_INVALID = "param_invalid"  # 参数错误
    RESOURCE = "resource"            # 资源不足/不存在
    DEPENDENCY = "dependency"        # 依赖服务故障
    LOGIC = "logic"                  # 业务逻辑错误
    RATE_LIMIT = "rate_limit"        # 限流
    DATA = "data"                    # 数据异常
    UNKNOWN = "unknown"              # 未知


# 错误关键词映射 → 根因分类
_ERROR_PATTERNS: Dict[ErrorCategory, List[str]] = {
    ErrorCategory.TIMEOUT: ["timeout", "timed out", "超时", "deadline", "too slow"],
    ErrorCategory.CONNECTION: ["connection", "refused", "unreachable", "连接", "network", "ECONNREFUSED", "socket"],
    ErrorCategory.AUTH: ["auth", "permission", "denied", "unauthorized", "forbidden", "认证", "权限", "401", "403"],
    ErrorCategory.PARAM_INVALID: ["invalid", "missing", "required", "参数", "缺少", "格式错误", "validation"],
    ErrorCategory.RESOURCE: ["not found", "no such", "不存在", "404", "missing resource"],
    ErrorCategory.DEPENDENCY: ["service unavailable", "502", "503", "upstream", "依赖", "服务不可用"],
    ErrorCategory.RATE_LIMIT: ["rate limit", "throttl", "too many", "限流", "429"],
    ErrorCategory.DATA: ["decode", "parse", "json", "format", "解析", "数据异常", "corrupt"],
}


@dataclass
class RootCause:
    """根因分析结果"""
    category: ErrorCategory
    confidence: float = 0.0        # 分类置信度
    description: str = ""          # 根因描述
    suggested_fix: str = ""        # 建议修复
    similar_count: int = 0         # 历史相似次数
    first_seen: float = 0.0        # 首次出现时间


@dataclass
class Experience:
    """经验记录（增强版）"""
    id: str
    skill_name: str
    action: str
    timestamp: float
    success: bool
    input_params: Dict[str, Any]
    output_result: Dict[str, Any]
    error_message: str = ""
    execution_time: float = 0.0
    context: Dict[str, Any] = field(default_factory=dict)
    lessons_learned: List[str] = field(default_factory=list)
    confidence_score: float = 0.0
    user_feedback: str = ""
    # v2.2 新增字段
    root_cause: str = ""           # 根因分类 (ErrorCategory value)
    root_cause_detail: str = ""    # 根因详细描述
    strategy_used: str = "default" # 使用的执行策略
    param_fingerprint: str = ""    # 参数指纹（用于精准匹配）
    reflection: str = ""           # LLM 反思总结

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'Experience':
        # 兼容旧版本数据（缺少新字段时填默认值）
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


@dataclass
class SkillInsight:
    """技能洞察（从经验中提取）"""
    skill_name: str
    pattern_type: str  # success_pattern | error_pattern | optimization | strategy
    pattern_description: str
    frequency: int = 1
    last_seen: float = field(default_factory=time.time)
    confidence: float = 0.0
    examples: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    # v2.2 新增
    root_cause: str = ""           # 关联的根因分类
    avg_execution_time: float = 0.0
    trend: str = "stable"          # improving | degrading | stable


@dataclass
class ExecutionPlan:
    """执行规划（基于历史经验生成）"""
    skill_name: str
    strategy: str = "default"                  # 执行策略
    confidence: float = 0.5                    # 预期信心
    estimated_time: float = 0.0                # 预估耗时
    risk_level: str = "low"                    # low / medium / high
    warnings: List[str] = field(default_factory=list)
    param_adjustments: Dict[str, Any] = field(default_factory=dict)  # 建议的参数调整
    fallback_strategy: str = ""                # 备选策略
    reasoning: str = ""                        # 规划理由


@dataclass
class ReflectionResult:
    """LLM 反思分析结果"""
    summary: str = ""              # 反思总结
    root_cause: str = ""           # 识别的根因
    what_went_wrong: str = ""      # 出了什么问题
    what_to_improve: str = ""      # 如何改进
    pattern_detected: str = ""     # 识别的模式
    confidence: float = 0.0        # 反思置信度


# ═══════════════════════════════════════════════════════════════
#  根因分析器
# ═══════════════════════════════════════════════════════════════

class RootCauseAnalyzer:
    """
    根因分析器 — 结构化错误分类

    从错误信息中提取根因分类，支持:
    1. 关键词规则匹配（快速）
    2. 历史模式匹配（基于积累）
    3. LLM 深度分析（可选，慢但准）
    """

    def __init__(self, patterns: Dict = None):
        """patterns: 可注入的规则替代 (v4.0 Phase 5.2 A/B 基准用; 默认 None → 生产规则)"""
        self._patterns: Dict[ErrorCategory, List[str]] = patterns or _ERROR_PATTERNS
        self._error_history: Dict[str, List[Tuple[ErrorCategory, float]]] = {}  # error_hash -> [(category, timestamp)]

    def analyze(self, error_message: str, context: Dict[str, Any] = None) -> RootCause:
        """快速根因分析（规则+历史）"""
        if not error_message:
            return RootCause(category=ErrorCategory.UNKNOWN, confidence=0.0)

        error_lower = error_message.lower()

        # 1) 规则匹配
        best_category = ErrorCategory.UNKNOWN
        best_score = 0.0
        for category, keywords in self._patterns.items():
            score = sum(1.0 for kw in keywords if kw in error_lower)
            if score > best_score:
                best_score = score
                best_category = category

        confidence = min(0.95, best_score * 0.3 + 0.2) if best_score > 0 else 0.1

        # 2) 历史加成
        error_hash = self._hash_error(error_message)
        if error_hash in self._error_history:
            history = self._error_history[error_hash]
            if history:
                # 历史中最常见的分类
                from collections import Counter
                cat_counts = Counter(cat for cat, _ in history)
                most_common_cat, count = cat_counts.most_common(1)[0]
                if count >= 2:
                    best_category = most_common_cat
                    confidence = min(0.95, confidence + 0.15)

        # 3) 记录到历史
        if error_hash not in self._error_history:
            self._error_history[error_hash] = []
        self._error_history[error_hash].append((best_category, time.time()))
        # 只保留最近50条
        self._error_history[error_hash] = self._error_history[error_hash][-50:]

        # 生成修复建议
        suggested_fix = self._get_fix_suggestion(best_category, error_message)

        return RootCause(
            category=best_category,
            confidence=confidence,
            description=f"[{best_category.value}] {error_message[:200]}",
            suggested_fix=suggested_fix,
            similar_count=len(self._error_history.get(error_hash, [])),
        )

    async def deep_analyze(self, error_message: str, skill_name: str,
                           input_params: Dict, context: Dict = None,
                           llm_func=None) -> RootCause:
        """
        LLM 深度根因分析

        调用 LLM 进行深度分析，返回结构化根因
        """
        # 先做快速分析
        quick = self.analyze(error_message, context)

        if not llm_func:
            return quick

        try:
            prompt = f"""你是运维专家，请分析以下技能执行失败的根因。

技能: {skill_name}
错误信息: {error_message[:500]}
输入参数: {json.dumps(input_params, ensure_ascii=False, default=str)[:300]}
初步分类: {quick.category.value}

请用JSON格式回答:
{{
  "root_cause": "<根因分类: timeout/connection/auth/param_invalid/resource/dependency/logic/rate_limit/data/unknown>",
  "description": "<简短根因描述，30字内>",
  "suggested_fix": "<建议修复方案，50字内>"
}}"""
            resp = await llm_func(prompt)
            if resp:
                data = json.loads(resp) if isinstance(resp, str) else resp
                category = ErrorCategory(data.get("root_cause", quick.category.value))
                return RootCause(
                    category=category,
                    confidence=min(0.95, quick.confidence + 0.2),
                    description=data.get("description", quick.description),
                    suggested_fix=data.get("suggested_fix", quick.suggested_fix),
                    similar_count=quick.similar_count,
                )
        except Exception as e:
            logger.debug(f"[root_cause] LLM 分析失败，回退规则分析: {e}")

        return quick

    def _hash_error(self, error_message: str) -> str:
        """生成错误指纹（归一化后哈希）"""
        import re
        # 移除数字、IP、路径等易变内容
        normalized = re.sub(r'\d+\.\d+\.\d+\.\d+', '<IP>', error_message)
        normalized = re.sub(r'\d+', '<N>', normalized)
        normalized = re.sub(r'/[\w/.-]+', '<PATH>', normalized)
        normalized = normalized.strip().lower()[:200]
        return hashlib.md5(normalized.encode()).hexdigest()[:12]

    def _get_fix_suggestion(self, category: ErrorCategory, error_msg: str) -> str:
        """基于分类生成修复建议"""
        suggestions = {
            ErrorCategory.TIMEOUT: "增加超时时间或检查目标服务响应",
            ErrorCategory.CONNECTION: "检查网络连通性和目标服务状态",
            ErrorCategory.AUTH: "检查认证凭据和权限配置",
            ErrorCategory.PARAM_INVALID: "检查输入参数格式和必填项",
            ErrorCategory.RESOURCE: "确认目标资源存在且可访问",
            ErrorCategory.DEPENDENCY: "检查依赖服务的运行状态",
            ErrorCategory.LOGIC: "检查业务逻辑和边界条件",
            ErrorCategory.RATE_LIMIT: "降低请求频率或增加重试间隔",
            ErrorCategory.DATA: "检查数据格式和编码",
            ErrorCategory.UNKNOWN: "请查看详细日志进行排查",
        }
        return suggestions.get(category, "请查看详细日志")


# ═══════════════════════════════════════════════════════════════
#  经验数据库（增强版）
# ═══════════════════════════════════════════════════════════════

class ExperienceDatabase:
    """
    经验数据库（v2.2 增强）

    新增:
    - 时间衰减加权
    - 参数指纹匹配
    - 跨技能搜索
    - 统计趋势分析
    """

    # 经验衰减半衰期（秒）— 7天
    DECAY_HALF_LIFE = 7 * 24 * 3600

    def __init__(self, db_path: str = None):
        if db_path:
            self.db_path = Path(db_path)
        else:
            self.db_path = Path(__file__).resolve().parent.parent / "experiences"
        self.db_path.mkdir(exist_ok=True)
        self._cache: Dict[str, List[Experience]] = {}

    def save_experience(self, experience: Experience):
        """保存经验"""
        skill_file = self.db_path / f"{experience.skill_name}.jsonl"
        with open(skill_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(experience.to_dict(), ensure_ascii=False) + "\n")

        if experience.skill_name not in self._cache:
            self._cache[experience.skill_name] = []
        self._cache[experience.skill_name].append(experience)
        # 缓存上限
        if len(self._cache[experience.skill_name]) > 500:
            self._cache[experience.skill_name] = self._cache[experience.skill_name][-500:]

        logger.debug(f"[experience] 保存经验：{experience.id} ({experience.skill_name})")

    def get_experiences(
        self,
        skill_name: str,
        limit: int = 100,
        success_only: bool = False,
        failure_only: bool = False
    ) -> List[Experience]:
        """获取经验列表"""
        skill_file = self.db_path / f"{skill_name}.jsonl"
        if not skill_file.exists():
            return []

        experiences = []
        with open(skill_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    exp = Experience.from_dict(data)
                    if success_only and not exp.success:
                        continue
                    if failure_only and exp.success:
                        continue
                    experiences.append(exp)
                except Exception:
                    continue

        experiences.sort(key=lambda x: x.timestamp, reverse=True)
        return experiences[:limit]

    def get_similar_experiences(
        self,
        skill_name: str,
        input_params: Dict[str, Any],
        limit: int = 10
    ) -> List[Experience]:
        """获取相似经验（增强：指纹匹配 + 时间衰减加权）"""
        all_exp = self.get_experiences(skill_name, limit=200)
        target_fp = self._param_fingerprint(input_params)
        now = time.time()

        def scored(exp: Experience) -> float:
            # 参数相似度
            sim = 0.0
            # 指纹精确匹配（最高分）
            if exp.param_fingerprint and exp.param_fingerprint == target_fp:
                sim += 3.0
            # 参数键值匹配
            for key, value in input_params.items():
                if key in exp.input_params:
                    if exp.input_params[key] == value:
                        sim += 1.0
                    elif isinstance(value, str) and isinstance(exp.input_params[key], str):
                        v_lower, e_lower = value.lower(), exp.input_params[key].lower()
                        if v_lower in e_lower or e_lower in v_lower:
                            sim += 0.5
            # 时间衰减
            age = now - exp.timestamp
            decay = math.exp(-0.693 * age / self.DECAY_HALF_LIFE)  # 指数衰减
            return sim * decay

        all_exp.sort(key=scored, reverse=True)
        return all_exp[:limit]

    def get_cross_skill_experiences(
        self,
        skill_names: List[str],
        error_category: str = "",
        limit: int = 20
    ) -> List[Experience]:
        """跨技能搜索经验（用于迁移学习）"""
        all_exp = []
        for sn in skill_names:
            exps = self.get_experiences(sn, limit=50)
            all_exp.extend(exps)

        if error_category:
            all_exp = [e for e in all_exp if e.root_cause == error_category]

        all_exp.sort(key=lambda x: x.timestamp, reverse=True)
        return all_exp[:limit]

    def get_statistics(self, skill_name: str) -> dict:
        """获取统计信息（增强：趋势分析）"""
        experiences = self.get_experiences(skill_name, limit=1000)
        if not experiences:
            return {}

        total = len(experiences)
        success = sum(1 for e in experiences if e.success)
        failure = total - success
        avg_time = sum(e.execution_time for e in experiences) / total if total else 0
        avg_confidence = sum(e.confidence_score for e in experiences) / total if total else 0

        # 趋势分析：对比最近 20 次与之前
        recent = experiences[:20]
        older = experiences[20:40] if len(experiences) > 20 else []
        recent_rate = sum(1 for e in recent if e.success) / len(recent) if recent else 0
        older_rate = sum(1 for e in older if e.success) / len(older) if older else recent_rate

        if recent_rate > older_rate + 0.1:
            trend = "improving"
        elif recent_rate < older_rate - 0.1:
            trend = "degrading"
        else:
            trend = "stable"

        # 根因分布
        root_cause_dist = {}
        for e in experiences:
            if not e.success and e.root_cause:
                root_cause_dist[e.root_cause] = root_cause_dist.get(e.root_cause, 0) + 1

        return {
            "total": total,
            "success": success,
            "failure": failure,
            "success_rate": success / total if total else 0,
            "recent_success_rate": recent_rate,
            "avg_execution_time": avg_time,
            "avg_confidence": avg_confidence,
            "last_execution": experiences[0].timestamp if experiences else 0,
            "trend": trend,
            "root_cause_distribution": root_cause_dist,
        }

    def get_all_skill_names(self) -> List[str]:
        """获取所有有经验记录的技能名"""
        return [f.stem for f in self.db_path.glob("*.jsonl")]

    @staticmethod
    def _param_fingerprint(params: Dict[str, Any]) -> str:
        """生成参数指纹"""
        try:
            canonical = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
            return hashlib.md5(canonical.encode()).hexdigest()[:12]
        except Exception:
            return ""


# ═══════════════════════════════════════════════════════════════
#  LLM 反思器
# ═══════════════════════════════════════════════════════════════

class ReflectionEngine:
    """
    LLM 反思引擎

    在关键时刻调用 LLM 进行深度分析:
    - 连续失败后的反思
    - 性能趋势恶化时的诊断
    - 新模式发现时的确认
    """

    # 触发深度反思的条件
    REFLECT_AFTER_CONSECUTIVE_FAILURES = 3
    REFLECT_ON_DEGRADATION = True

    def __init__(self, llm_func=None):
        self._llm_func = llm_func
        self._reflection_cache: Dict[str, ReflectionResult] = {}  # key -> result

    def set_llm(self, llm_func):
        """设置 LLM 调用函数"""
        self._llm_func = llm_func

    async def reflect_on_failure(
        self,
        skill_name: str,
        recent_failures: List[Experience],
        recent_successes: List[Experience] = None,
    ) -> ReflectionResult:
        """
        对连续失败进行反思分析

        如果有 LLM，调用 LLM 分析；否则使用规则分析
        """
        # 缓存键
        cache_key = f"{skill_name}_{len(recent_failures)}_{recent_failures[0].id if recent_failures else ''}"
        if cache_key in self._reflection_cache:
            return self._reflection_cache[cache_key]

        result = ReflectionResult()

        # 提取错误模式
        error_types = {}
        for f in recent_failures:
            cat = f.root_cause or "unknown"
            error_types[cat] = error_types.get(cat, 0) + 1
        dominant_error = max(error_types, key=error_types.get) if error_types else "unknown"

        if self._llm_func:
            result = await self._llm_reflect(skill_name, recent_failures, recent_successes)
        else:
            # 规则反思
            result.summary = f"技能 {skill_name} 最近 {len(recent_failures)} 次执行失败"
            result.root_cause = dominant_error
            result.what_went_wrong = "; ".join(
                set(f.error_message[:80] for f in recent_failures[:5] if f.error_message)
            )
            result.what_to_improve = self._rule_based_suggestion(dominant_error, recent_failures)
            result.confidence = 0.6

        self._reflection_cache[cache_key] = result
        # 缓存上限
        if len(self._reflection_cache) > 100:
            oldest = list(self._reflection_cache.keys())[:50]
            for k in oldest:
                self._reflection_cache.pop(k, None)

        return result

    async def _llm_reflect(
        self,
        skill_name: str,
        failures: List[Experience],
        successes: List[Experience] = None,
    ) -> ReflectionResult:
        """使用 LLM 进行深度反思"""
        try:
            failure_summary = "\n".join(
                f"- [{f.root_cause or 'unknown'}] {f.error_message[:100]} (耗时{f.execution_time:.1f}s)"
                for f in failures[:5]
            )
            success_summary = ""
            if successes:
                success_summary = "\n最近成功执行:\n" + "\n".join(
                    f"- 参数: {json.dumps(s.input_params, ensure_ascii=False, default=str)[:100]} (耗时{s.execution_time:.1f}s)"
                    for s in successes[:3]
                )

            prompt = f"""你是智能运维系统的反思模块。请分析以下技能执行失败的情况，给出深度反思。

技能名称: {skill_name}

最近失败记录:
{failure_summary}
{success_summary}

请用JSON格式回答:
{{
  "summary": "<一句话总结反思结论>",
  "root_cause": "<根本原因>",
  "what_went_wrong": "<出了什么问题>",
  "what_to_improve": "<具体改进方案，限50字>",
  "pattern_detected": "<识别到的失败模式>"
}}"""
            resp = await self._llm_func(prompt)
            if resp:
                data = json.loads(resp) if isinstance(resp, str) else resp
                return ReflectionResult(
                    summary=data.get("summary", ""),
                    root_cause=data.get("root_cause", ""),
                    what_went_wrong=data.get("what_went_wrong", ""),
                    what_to_improve=data.get("what_to_improve", ""),
                    pattern_detected=data.get("pattern_detected", ""),
                    confidence=0.85,
                )
        except Exception as e:
            logger.debug(f"[reflection] LLM 反思失败: {e}")

        return ReflectionResult(
            summary=f"LLM 反思失败，回退规则分析",
            confidence=0.4,
        )

    def _rule_based_suggestion(self, dominant_error: str, failures: List[Experience]) -> str:
        """基于规则的改进建议"""
        suggestions = {
            "timeout": "建议增加超时时间或优化目标服务性能",
            "connection": "建议检查网络连通性，增加重试机制",
            "auth": "建议检查凭据配置，确认权限设置",
            "param_invalid": "建议加强参数校验，提供默认值",
            "resource": "建议预检查资源存在性",
            "dependency": "建议增加依赖服务健康检查",
            "rate_limit": "建议增加请求间隔和退避策略",
            "data": "建议增加数据格式校验和容错处理",
        }
        return suggestions.get(dominant_error, "建议详细检查日志并优化执行逻辑")


# ═══════════════════════════════════════════════════════════════
#  自我进化引擎（进阶版）
# ═══════════════════════════════════════════════════════════════

class SelfImprovingEngine:
    """
    自我进化引擎 v2.2

    增强功能:
    1. 根因分析 — 结构化错误分类与归因
    2. LLM 反思 — 深度失败分析与改进建议
    3. 经验衰减 — 近期经验权重更高
    4. 跨技能迁移 — 相似技能共享经验
    5. 执行规划 — 基于历史的预执行规划
    6. 策略自适应 — 根据历史自动选择最佳策略
    7. 多维信心评估 — 综合多因素的信心计算
    """

    def __init__(self, db: ExperienceDatabase = None, llm_func=None):
        self.db = db or ExperienceDatabase()
        self.rca = RootCauseAnalyzer()
        self.reflector = ReflectionEngine(llm_func)
        self._insights: Dict[str, List[SkillInsight]] = {}
        self._strategies: Dict[str, Dict[str, float]] = {}  # skill -> {strategy: score}
        self._consecutive_failures: Dict[str, int] = {}     # skill -> count
        self._skill_tags: Dict[str, List[str]] = {}          # skill -> tags (用于跨技能迁移)

    def set_llm(self, llm_func):
        """设置 LLM 函数（支持延迟注入）"""
        self.reflector.set_llm(llm_func)

    def register_skill_tags(self, skill_name: str, tags: List[str]):
        """注册技能标签（用于跨技能迁移学习）"""
        self._skill_tags[skill_name] = tags

    def record_execution(
        self,
        skill_name: str,
        action: str,
        input_params: Dict[str, Any],
        output_result: Dict[str, Any],
        success: bool,
        error_message: str = "",
        execution_time: float = 0.0,
        context: Dict[str, Any] = None,
        strategy_used: str = "default",
    ):
        """
        记录技能执行（增强版）

        新增: 根因分析、参数指纹、策略记录
        """
        import uuid

        # 根因分析
        root_cause = ""
        root_cause_detail = ""
        if not success and error_message:
            rca_result = self.rca.analyze(error_message, context)
            root_cause = rca_result.category.value
            root_cause_detail = rca_result.description

        experience = Experience(
            id=str(uuid.uuid4())[:8],
            skill_name=skill_name,
            action=action,
            timestamp=time.time(),
            success=success,
            input_params=input_params,
            output_result=output_result,
            error_message=error_message,
            execution_time=execution_time,
            context=context or {},
            root_cause=root_cause,
            root_cause_detail=root_cause_detail,
            strategy_used=strategy_used,
            param_fingerprint=ExperienceDatabase._param_fingerprint(input_params),
        )

        # 提取经验教训
        self._extract_lessons(experience)

        # 保存
        self.db.save_experience(experience)

        # 更新洞察
        self._update_insights(experience)

        # 更新策略分数
        self._update_strategy_score(skill_name, strategy_used, success, execution_time)

        # 更新连续失败计数
        if success:
            self._consecutive_failures[skill_name] = 0
        else:
            self._consecutive_failures[skill_name] = self._consecutive_failures.get(skill_name, 0) + 1

        logger.debug(f"[self_improving] 记录: {skill_name}.{action} success={success} "
                     f"root_cause={root_cause} strategy={strategy_used}")

    def _extract_lessons(self, experience: Experience):
        """从经验中提取经验教训（增强版）"""
        lessons = []

        if not experience.success:
            if experience.error_message:
                lessons.append(f"避免错误[{experience.root_cause}]: {experience.error_message[:100]}")
            if experience.root_cause:
                fix = self.rca._get_fix_suggestion(
                    ErrorCategory(experience.root_cause) if experience.root_cause in [e.value for e in ErrorCategory] else ErrorCategory.UNKNOWN,
                    experience.error_message
                )
                lessons.append(f"修复建议: {fix}")
        else:
            if experience.execution_time > 10:
                lessons.append(f"性能警告: 执行耗时 {experience.execution_time:.1f}s，考虑优化")
            key_factors = [f"{k}={v}" for k, v in experience.input_params.items() if v][:5]
            if key_factors:
                lessons.append(f"成功因素: {', '.join(key_factors)}")
            if experience.strategy_used != "default":
                lessons.append(f"有效策略: {experience.strategy_used}")

        experience.lessons_learned = lessons

    def _update_insights(self, experience: Experience):
        """更新技能洞察（增强版）"""
        skill_name = experience.skill_name
        if skill_name not in self._insights:
            self._insights[skill_name] = []

        pattern_type = "success_pattern" if experience.success else "error_pattern"

        # 查找匹配的洞察
        matching = None
        for insight in self._insights[skill_name]:
            if insight.pattern_type == pattern_type:
                if self._is_similar_pattern(experience, insight):
                    matching = insight
                    break

        if matching:
            matching.frequency += 1
            matching.last_seen = experience.timestamp
            matching.examples.append(experience.id)
            if len(matching.examples) > 20:
                matching.examples = matching.examples[-20:]
            # 更新平均耗时
            n = matching.frequency
            matching.avg_execution_time = (
                matching.avg_execution_time * (n - 1) / n + experience.execution_time / n
            )
        else:
            new_insight = SkillInsight(
                skill_name=skill_name,
                pattern_type=pattern_type,
                pattern_description=self._describe_pattern(experience),
                frequency=1,
                confidence=0.5,
                examples=[experience.id],
                root_cause=experience.root_cause,
                avg_execution_time=experience.execution_time,
            )
            self._insights[skill_name].append(new_insight)

        # 限制洞察数量
        if len(self._insights[skill_name]) > 50:
            self._insights[skill_name].sort(key=lambda x: x.frequency, reverse=True)
            self._insights[skill_name] = self._insights[skill_name][:50]

    def _update_strategy_score(self, skill_name: str, strategy: str, success: bool, exec_time: float):
        """更新策略分数（策略自适应）

        v4.0 (Phase 5.2): EMA 自评分无 ground truth — 降级为参考指标;
        技能改进的上线判定以 tests/rca/benchmark.py 的 RCA 基准门为准。
        """
        if skill_name not in self._strategies:
            self._strategies[skill_name] = {}
        strategies = self._strategies[skill_name]

        old_score = strategies.get(strategy, 0.5)
        # 成功+1分，失败-1分，加上执行速度奖励
        reward = 1.0 if success else -1.0
        if success and exec_time < 3.0:
            reward += 0.2  # 快速执行奖励
        # 指数移动平均
        new_score = old_score * 0.8 + (0.5 + reward * 0.25) * 0.2
        strategies[strategy] = max(0.0, min(1.0, new_score))

    def _is_similar_pattern(self, experience: Experience, insight: SkillInsight) -> bool:
        if insight.pattern_type == "error_pattern":
            # 同根因分类 = 相似
            if experience.root_cause and insight.root_cause:
                return experience.root_cause == insight.root_cause
            return experience.error_message and insight.pattern_description in experience.error_message
        return experience.success

    def _describe_pattern(self, experience: Experience) -> str:
        if experience.success:
            return f"成功执行 {experience.action} (策略={experience.strategy_used})"
        else:
            cat = f"[{experience.root_cause}] " if experience.root_cause else ""
            return f"{cat}{experience.error_message[:100]}" if experience.error_message else "执行失败"

    # ───────────────────────────────────────────
    #  执行规划
    # ───────────────────────────────────────────

    def plan_execution(
        self,
        skill_name: str,
        input_params: Dict[str, Any],
    ) -> ExecutionPlan:
        """
        基于历史经验生成执行规划

        Returns:
            ExecutionPlan 包含策略选择、风险评估、参数调整建议
        """
        plan = ExecutionPlan(skill_name=skill_name)
        stats = self.db.get_statistics(skill_name)

        if not stats or stats.get("total", 0) < 3:
            plan.reasoning = "历史数据不足，使用默认策略"
            return plan

        # 1) 策略选择
        plan.strategy = self.get_best_strategy(skill_name)

        # 2) 信心评估
        plan.confidence = self.calculate_confidence(skill_name, input_params)

        # 3) 耗时预估
        plan.estimated_time = stats.get("avg_execution_time", 0)

        # 4) 风险评估
        recent_rate = stats.get("recent_success_rate", 1.0)
        if recent_rate < 0.5:
            plan.risk_level = "high"
        elif recent_rate < 0.8:
            plan.risk_level = "medium"
        else:
            plan.risk_level = "low"

        # 5) 警告
        warnings = []
        trend = stats.get("trend", "stable")
        if trend == "degrading":
            warnings.append(f"⚠️ 该技能近期成功率下降趋势 ({recent_rate:.0%})")

        consecutive = self._consecutive_failures.get(skill_name, 0)
        if consecutive >= 2:
            warnings.append(f"⚠️ 已连续失败 {consecutive} 次")

        # 根因分布警告
        root_dist = stats.get("root_cause_distribution", {})
        if root_dist:
            top_cause = max(root_dist, key=root_dist.get)
            warnings.append(f"💡 历史最常见错误: {top_cause} ({root_dist[top_cause]}次)")

        plan.warnings = warnings

        # 6) 参数调整建议
        similar_failures = self.db.get_similar_experiences(skill_name, input_params, limit=20)
        timeout_failures = [e for e in similar_failures if e.root_cause == "timeout" and not e.success]
        if len(timeout_failures) > 2:
            if "timeout" in input_params:
                plan.param_adjustments["timeout"] = int(input_params["timeout"] * 1.5)
                plan.warnings.append(f"⏱️ 建议增加超时: {input_params['timeout']}s → {plan.param_adjustments['timeout']}s")

        # 7) 备选策略
        strategies = self._strategies.get(skill_name, {})
        if len(strategies) > 1:
            sorted_strats = sorted(strategies.items(), key=lambda x: x[1], reverse=True)
            if len(sorted_strats) > 1:
                plan.fallback_strategy = sorted_strats[1][0]

        plan.reasoning = (
            f"基于 {stats['total']} 次执行历史 | "
            f"成功率 {stats['success_rate']:.0%} | "
            f"趋势 {trend} | "
            f"策略 {plan.strategy}"
        )

        return plan

    # ───────────────────────────────────────────
    #  策略选择
    # ───────────────────────────────────────────

    def get_best_strategy(self, skill_name: str) -> str:
        """获取最佳执行策略"""
        strategies = self._strategies.get(skill_name, {})
        if not strategies:
            return "default"
        return max(strategies, key=strategies.get)

    def get_strategy_scores(self, skill_name: str) -> Dict[str, float]:
        """获取所有策略的分数"""
        return dict(self._strategies.get(skill_name, {}))

    # ───────────────────────────────────────────
    #  跨技能迁移学习
    # ───────────────────────────────────────────

    def get_transfer_insights(
        self,
        skill_name: str,
        error_category: str = "",
        limit: int = 5
    ) -> List[str]:
        """
        从相似技能中获取迁移洞察

        基于技能标签找到相似技能，提取其经验教训
        """
        current_tags = set(self._skill_tags.get(skill_name, []))
        if not current_tags:
            return []

        # 找到有共同标签的技能
        related_skills = []
        for sn, tags in self._skill_tags.items():
            if sn != skill_name and set(tags) & current_tags:
                overlap = len(set(tags) & current_tags)
                related_skills.append((sn, overlap))

        related_skills.sort(key=lambda x: x[1], reverse=True)
        related_names = [s[0] for s in related_skills[:5]]

        if not related_names:
            return []

        # 从相似技能中提取经验
        cross_exp = self.db.get_cross_skill_experiences(related_names, error_category, limit=30)
        insights = []
        seen = set()
        for exp in cross_exp:
            for lesson in exp.lessons_learned:
                if lesson not in seen:
                    seen.add(lesson)
                    insights.append(f"[迁移自 {exp.skill_name}] {lesson}")
                    if len(insights) >= limit:
                        return insights

        return insights

    # ───────────────────────────────────────────
    #  反思触发
    # ───────────────────────────────────────────

    async def maybe_reflect(
        self,
        skill_name: str,
    ) -> Optional[ReflectionResult]:
        """
        检查是否需要触发 LLM 反思

        触发条件:
        - 连续失败 >= 3 次
        - 趋势恶化
        """
        consecutive = self._consecutive_failures.get(skill_name, 0)
        if consecutive < self.reflector.REFLECT_AFTER_CONSECUTIVE_FAILURES:
            return None

        recent_failures = self.db.get_experiences(skill_name, limit=consecutive, failure_only=True)
        recent_successes = self.db.get_experiences(skill_name, limit=5, success_only=True)

        result = await self.reflector.reflect_on_failure(
            skill_name, recent_failures, recent_successes
        )

        logger.info(f"[self_improving] 反思结果({skill_name}): {result.summary}")
        return result

    # ───────────────────────────────────────────
    #  原有接口（兼容 v2.0）
    # ───────────────────────────────────────────

    def get_recommendations(
        self,
        skill_name: str,
        input_params: Dict[str, Any],
        limit: int = 5
    ) -> List[str]:
        """获取优化建议（增强版）"""
        recommendations = []

        stats = self.db.get_statistics(skill_name)
        if stats:
            rate = stats.get("success_rate", 0)
            if rate < 0.8:
                recommendations.append(f"⚠️ 成功率 {rate:.1%}，请检查参数")
            recent_rate = stats.get("recent_success_rate", 0)
            if recent_rate < rate - 0.1:
                recommendations.append(f"📉 近期成功率下降: {rate:.0%} → {recent_rate:.0%}")
            if stats.get("avg_execution_time", 0) > 5:
                recommendations.append(f"⏱️ 平均耗时 {stats['avg_execution_time']:.1f}s，考虑优化")

            # 根因分布建议
            root_dist = stats.get("root_cause_distribution", {})
            if root_dist:
                top_cause = max(root_dist, key=root_dist.get)
                recommendations.append(f"🔍 最常见失败原因: {top_cause}")

        # 跨技能迁移建议
        transfer = self.get_transfer_insights(skill_name, limit=2)
        recommendations.extend(transfer)

        # 洞察建议
        if skill_name in self._insights:
            for insight in self._insights[skill_name][:3]:
                if insight.frequency >= 3:
                    recommendations.append(f"💡 {insight.pattern_description}")

        return recommendations[:limit]

    def calculate_confidence(
        self,
        skill_name: str,
        input_params: Dict[str, Any]
    ) -> float:
        """
        多维信心评估

        综合因素:
        - 整体成功率
        - 近期趋势
        - 相似经验成功率
        - 连续失败惩罚
        - 策略分数
        """
        stats = self.db.get_statistics(skill_name)
        if not stats:
            return 0.5

        # 基础信心 = 整体成功率
        base = stats.get("success_rate", 0.5)

        # 近期趋势修正
        recent_rate = stats.get("recent_success_rate", base)
        trend_factor = 0.0
        if recent_rate > base:
            trend_factor = 0.05  # 上升奖励
        elif recent_rate < base - 0.1:
            trend_factor = -0.1  # 下降惩罚

        # 相似经验加成
        similar = self.get_similar_experiences(skill_name, input_params, limit=10)
        sim_bonus = 0.0
        if similar:
            sim_success = sum(1 for e in similar if e.success)
            sim_bonus = (sim_success / len(similar) - 0.5) * 0.15

        # 连续失败惩罚
        consecutive = self._consecutive_failures.get(skill_name, 0)
        fail_penalty = min(0.3, consecutive * 0.08)

        # 策略分数加成
        best_strategy_score = max(self._strategies.get(skill_name, {}).values(), default=0.5)
        strategy_bonus = (best_strategy_score - 0.5) * 0.1

        confidence = base + trend_factor + sim_bonus - fail_penalty + strategy_bonus
        return max(0.05, min(0.99, confidence))

    def get_similar_experiences(
        self,
        skill_name: str,
        input_params: Dict[str, Any],
        limit: int = 10
    ) -> List[Experience]:
        """获取相似经验"""
        return self.db.get_similar_experiences(skill_name, input_params, limit)

    def get_statistics(self, skill_name: str) -> dict:
        """获取统计信息"""
        return self.db.get_statistics(skill_name)

    def get_insights(self, skill_name: str) -> List[SkillInsight]:
        """获取技能洞察列表"""
        return self._insights.get(skill_name, [])

    def get_consecutive_failures(self, skill_name: str) -> int:
        """获取连续失败次数"""
        return self._consecutive_failures.get(skill_name, 0)


# ═══════════════════════════════════════════════════════════════
#  全局单例 & 装饰器
# ═══════════════════════════════════════════════════════════════

_improving_engine: Optional[SelfImprovingEngine] = None


def get_improving_engine() -> SelfImprovingEngine:
    """获取全局自我进化引擎"""
    global _improving_engine
    if _improving_engine is None:
        _improving_engine = SelfImprovingEngine()
    return _improving_engine


def record_skill_execution(func):
    """
    装饰器：记录技能执行并自动学习

    使用方式:
    @record_skill_execution
    async def my_skill(self, params):
        ...
    """
    import functools

    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        skill_name = getattr(self, 'name', self.__class__.__name__)
        action = func.__name__
        start_time = time.time()
        strategy = getattr(self, '_current_strategy', 'default')

        try:
            result = await func(self, *args, **kwargs)
            execution_time = time.time() - start_time

            engine = get_improving_engine()
            engine.record_execution(
                skill_name=skill_name,
                action=action,
                input_params=args[0] if args else kwargs,
                output_result=result.to_dict() if hasattr(result, 'to_dict') else (result if isinstance(result, dict) else {}),
                success=True,
                execution_time=execution_time,
                strategy_used=strategy,
            )
            return result

        except Exception as e:
            execution_time = time.time() - start_time

            engine = get_improving_engine()
            engine.record_execution(
                skill_name=skill_name,
                action=action,
                input_params=args[0] if args else kwargs,
                output_result={},
                success=False,
                error_message=str(e),
                execution_time=execution_time,
                strategy_used=strategy,
            )
            raise

    return wrapper
