#!/usr/bin/env python3
"""
Z-OPS v4.0 — 评测共享基础设施 (Phase 2.1 路由评测与 Phase 5.1 RCA 评测共用)

用法:
    from tests.eval_common import load_yaml_cases, write_report, ratio

设计: 两个评测 runner (tests/routing/run_eval.py, tests/rca/run_rca_eval.py)
共用用例加载/报告落盘/准确率计算, 保证报告结构一致 (供 /api/routing/eval-summary
与管理面板复用)。
"""
import json
import sys
from pathlib import Path


def load_yaml_cases(path: Path) -> list:
    """加载 YAML 用例集 (cases: [...])"""
    import yaml
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return (data or {}).get("cases") or []


def ratio(part: int, total: int) -> float:
    return round(part / total, 4) if total else 0.0


def write_report(out_path, *, suite: str, total: int, passed: int,
                 failed: int, errors: int = 0, results: list = None,
                 extra: dict = None) -> Path:
    """统一报告落盘 (mode/total/passed/failed/errors/results 结构与路由评测一致)"""
    summary = {
        "suite": suite,
        "total": total, "passed": passed, "failed": failed, "errors": errors,
        "accuracy": ratio(passed, total),
        "results": results or [],
    }
    summary.update(extra or {})
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def console_summary(suite: str, total: int, passed: int, threshold: float) -> int:
    """打印摘要并按阈值决定退出码"""
    acc = passed / total if total else 0.0
    print(f"[{suite}] 用例总数: {total} | 通过: {passed} ({acc:.1%}) | 阈值: {threshold:.0%}")
    return 0 if acc >= threshold else 1


if __name__ == "__main__":
    print("eval_common: load_yaml_cases / write_report / ratio / console_summary")
