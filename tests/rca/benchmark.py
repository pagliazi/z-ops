#!/usr/bin/env python3
"""
Z-OPS v4.0 Phase 5.2 — A/B 基准门 (技能改进上线判定客观化)

改造前: SelfImprovingEngine._update_strategy_score 是 EMA 自评分 (±1分+速度奖励),
        无 ground truth — 自评 ≠ 真实改进。
改造后: skill_improver 自动上线判定改为 RCA 评测集基准:
        新策略命中率 ≥ 旧策略 + 5pp 才允许上线; EMA 分数降级为参考指标。

与 Phase 2.1 路由评测共用 runner 基础设施理念 (tests/eval_common.py)。
"""
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logger = logging.getLogger("zops.rca_benchmark")

# 上线门槛: 新策略基准命中率 ≥ 旧策略 + MIN_GAIN (5pp)
MIN_GAIN = 0.05
# 评测集健康度底线 (低于此值说明规则被改坏, 一律拦截)
BASELINE_MIN = 0.80

_RULE_CHANGE_KEYS = ("error_patterns", "rca_rules", "root_cause_keywords")


def run_rca_benchmark(analyzer=None, cases=None) -> float:
    """跑 RCA 评测集 → 根因命中率 (0-1). analyzer 可注入候选新规则."""
    from tests.rca.run_rca_eval import evaluate
    _, hit_rate, _ = evaluate(analyzer=analyzer, cases=cases)
    return hit_rate


def _build_analyzer_from_changes(changes: dict):
    """从 suggested_changes 中的规则类字段构造候选 analyzer; 无规则变更返回 None"""
    import copy
    from core.self_improving import RootCauseAnalyzer, ErrorCategory, _ERROR_PATTERNS
    for key in _RULE_CHANGE_KEYS:
        if key in changes and isinstance(changes[key], dict):
            patterns = copy.deepcopy(_ERROR_PATTERNS)
            for cat, kws in changes[key].items():
                try:
                    ec = ErrorCategory(cat)
                except ValueError:
                    continue
                merged = list(patterns.get(ec, []))
                for kw in (kws or []):
                    if kw not in merged:
                        merged.append(kw)
                patterns[ec] = merged
            return RootCauseAnalyzer(patterns=patterns)
    return None


def gate_improvement_on_rca(new_skill: dict, old_skill: dict):
    """技能改进的 RCA 基准门 (skill_improver.apply_improvement 调用)

    Args:
        new_skill: 应用 suggested_changes 后的技能定义
        old_skill: 当前生产技能定义

    Returns:
        (passed: bool, detail: str)
    """
    baseline = run_rca_benchmark()
    changes = new_skill.get("_suggested_changes") or {
        k: v for k, v in new_skill.items() if k in _RULE_CHANGE_KEYS
    }
    cand = _build_analyzer_from_changes(changes)

    if baseline < BASELINE_MIN:
        return False, f"baseline {baseline:.1%} < {BASELINE_MIN:.0%} (评测集健康度不足, 拦截)"

    if cand is None:
        # 无规则类变更 (description/参数/策略调整) — 基准无法区分, 记录 baseline 放行
        return True, f"no-rule-change, baseline={baseline:.1%} (仅记录)"

    new_rate = run_rca_benchmark(analyzer=cand)
    if new_rate >= baseline + MIN_GAIN:
        return True, f"rca {baseline:.1%} -> {new_rate:.1%} (>= +{MIN_GAIN:.0%})"
    return False, f"rca {baseline:.1%} -> {new_rate:.1%} (< +{MIN_GAIN:.0%} 门槛, 拦截)"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rate = run_rca_benchmark()
    print(f"当前生产规则 RCA 基准命中率: {rate:.1%}")
    # 自检: 假设一条"改坏"的规则 (清空 timeout 关键词) 应被拦截
    from core.self_improving import RootCauseAnalyzer, ErrorCategory, _ERROR_PATTERNS
    broken = RootCauseAnalyzer(patterns={k: ([] if k is ErrorCategory.TIMEOUT else v)
                                         for k, v in _ERROR_PATTERNS.items()})
    broken_rate = run_rca_benchmark(analyzer=broken)
    gate = "PASS" if broken_rate < rate else "UNEXPECTED"
    print(f"负例自检 (清空 timeout 规则): {broken_rate:.1%} → 门判定应为拦截: {gate}")
    sys.exit(0)
