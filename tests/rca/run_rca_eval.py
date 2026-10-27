#!/usr/bin/env python3
"""
Z-OPS v4.0 Phase 5.1 — RCA 评测 runner (借鉴 OpenSRE synthetic RCA 套件)

判定指标:
  1. 根因命中率   analyze(symptom).category == expect_category
  2. 证据引用完整度 description 含 required_evidence 全部关键词

用法:
  python tests/rca/run_rca_eval.py [--threshold 0.95] [--json out.json]
改动 _ERROR_PATTERNS / RootCauseAnalyzer 后必须跑本评测。
"""
import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import yaml


def load_cases():
    f = Path(__file__).parent / "fixtures" / "rca_cases.yaml"
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    return data["cases"]


def evaluate(analyzer=None, cases=None):
    """跑全量用例 → (results, hit_rate, evidence_rate)"""
    from core.self_improving import RootCauseAnalyzer
    rca = analyzer or RootCauseAnalyzer()
    cases = cases or load_cases()
    results = []
    hits, evid_full = 0, 0
    for c in cases:
        rc = rca.analyze(c["symptom"])
        got = rc.category.value
        ok = got == c["expect_category"]
        hits += ok
        desc = (rc.description or "").lower()
        evids = [e.lower() for e in (c.get("required_evidence") or [])]
        evid_ok = all(e in desc for e in evids) if evids else True
        evid_full += evid_ok
        results.append({
            "id": c["id"], "skill": c.get("skill", ""),
            "expect": c["expect_category"], "got": got,
            "hit": ok, "evidence_ok": evid_ok,
            "confidence": round(rc.confidence, 2),
        })
    n = max(len(cases), 1)
    return results, hits / n, evid_full / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.95)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    results, hit_rate, evid_rate = evaluate()
    passed = sum(1 for r in results if r["hit"])
    print(f"用例总数: {len(results)} | 根因命中: {passed} ({hit_rate:.1%}) "
          f"| 证据完整: {evid_rate:.1%} | 阈值: {args.threshold:.0%}")
    for r in results:
        if not r["hit"]:
            print(f"  MISS {r['id']}: expect={r['expect']} got={r['got']} "
                  f"(skill={r['skill']}, conf={r['confidence']})")
        elif not r["evidence_ok"]:
            print(f"  EVID {r['id']}: 证据引用不完整 (expect 含 required_evidence)")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "total": len(results), "hit_rate": round(hit_rate, 4),
            "evidence_rate": round(evid_rate, 4),
            "passed": passed, "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.json_out}")

    sys.exit(0 if hit_rate >= args.threshold else 1)


if __name__ == "__main__":
    main()
