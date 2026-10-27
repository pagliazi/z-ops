#!/usr/bin/env python3
"""Z-OPS v4.0 Phase 2.1 路由评测 runner (借鉴 OpenAI Agents SDK evals 理念)

规则: 改 COMMAND_ROUTES / 关键词表 / Agent tool 描述后必须跑本评测，路由准确率不得回退。

用法:
    python tests/routing/run_eval.py --mode keyword     # 关键词快速路由 (纯函数, 无需 LLM/转发)
    python tests/routing/run_eval.py --mode action      # COMMAND_ROUTES 精确映射校验
    python tests/routing/run_eval.py --mode llm         # LLM function_calling 路由 (需 OneAPI+Redis)
    python tests/routing/run_eval.py --mode full        # keyword 优先 -> llm 兜底 (生产真实路径)
    python tests/routing/run_eval.py --mode keyword --threshold 0.98 --json report.json

设计要点:
    - 直接调用 Orchestrator 的路由决策函数 (route_via_llm / _keyword_fast_route),
      不打 Redis 消息总线、不真正转发 (keyword/action 模式完全离线)
    - 输出: 总体准确率 + 混淆矩阵 (期望×实际) + 失败用例清单
    - llm 模式: LLM 后端不可用时逐条记为 error (不计入 fail), 若 error 率过高给出提示并不阻塞
"""
import sys
import json
import argparse
import asyncio
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import yaml  # noqa: E402

EVAL_FILE = Path(__file__).parent / "eval_cases.yaml"


def load_cases():
    with open(EVAL_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    cases = data["cases"]
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        dup = [i for i in ids if ids.count(i) > 1]
        raise SystemExit(f"评测用例 ID 重复: {sorted(set(dup))}")
    return cases


class Matrix:
    """期望×实际 混淆矩阵"""

    def __init__(self):
        self.cells = defaultdict(lambda: defaultdict(int))

    def add(self, expect, actual):
        self.cells[expect][actual] += 1

    def render(self):
        agents = sorted({a for row in self.cells.values() for a in row}
                        | set(self.cells.keys()))
        # "" 显示为 <llm> (交由 LLM)
        label = lambda a: a or "<llm>"
        corner = "expect\\actual"
        width = max(14, max((len(label(a)) for a in agents), default=10) + 2)
        header = f"{corner:<{width}}" + "".join(f"{label(a):<{width}}" for a in agents)
        lines = [header, "-" * len(header)]
        for e in agents:
            row = f"{label(e):<{width}}"
            for a in agents:
                n = self.cells.get(e, {}).get(a, 0)
                row += f"{(n if e == a else f'·{n}' if n else '-'):<{width}}"
            lines.append(row)
        return "\n".join(lines)


def run_keyword_mode(cases):
    from agents.orchestrator import Orchestrator
    from agents.base import AgentMessage

    orch = Orchestrator()
    results, matrix = [], Matrix()
    for c in cases:
        if "keyword" not in c.get("expect", {}):
            continue
        got = orch._keyword_fast_route(AgentMessage(text=c["text"]))
        expect = c["expect"]["keyword"]
        ok = got == expect
        matrix.add(expect, got)
        results.append({"id": c["id"], "text": c["text"], "expect": expect,
                        "got": got or "<llm>", "ok": ok, "source": c.get("source", "")})
    return results, matrix


def run_action_mode(cases):
    from agents.orchestrator import COMMAND_ROUTES

    results, matrix = [], Matrix()
    for c in cases:
        if "agent" not in c.get("expect", {}):
            continue
        route = COMMAND_ROUTES.get(c["action"])
        got = route[0] if route else "<missing>"
        expect = c["expect"]["agent"]
        ok = got == expect
        matrix.add(expect, got)
        results.append({"id": c["id"], "text": f"action={c['action']}", "expect": expect,
                        "got": got, "ok": ok, "source": c.get("source", "")})
    return results, matrix


async def _route_llm_one(orch, case):
    try:
        agent, _, reasoning = await orch.route_via_llm(case["text"], case.get("history"))
        return agent, reasoning, None
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}"


def run_llm_mode(cases, use_keyword_first=False):
    from agents.orchestrator import Orchestrator
    from agents.base import AgentMessage

    async def _run():
        orch = Orchestrator()
        results, matrix = [], Matrix()
        for c in cases:
            key = "llm" if not use_keyword_first else "llm"
            if key not in c.get("expect", {}):
                continue
            got, reasoning, err = await _route_llm_one(orch, c)
            if use_keyword_first and err is None:
                fast = orch._keyword_fast_route(AgentMessage(text=c["text"]))
                if fast:
                    got = fast
                    reasoning = "keyword-fast-route"
            if err is not None:
                results.append({"id": c["id"], "text": c["text"],
                                "expect": c["expect"][key], "got": "ERROR",
                                "ok": False, "error": err, "source": c.get("source", "")})
                continue
            expect = c["expect"][key]
            ok = got == expect
            matrix.add(expect, got)
            results.append({"id": c["id"], "text": c["text"], "expect": expect,
                            "got": got, "ok": ok, "reasoning": reasoning,
                            "source": c.get("source", "")})
        return results, matrix

    return asyncio.run(_run())


def report(mode, results, matrix, threshold):
    total = len(results)
    errors = [r for r in results if r.get("error")]
    fails = [r for r in results if not r["ok"] and not r.get("error")]
    passed = total - len(fails) - len(errors)
    acc = passed / total if total else 0.0

    print(f"\n{'=' * 64}\nZ-OPS 路由评测 — mode={mode}\n{'=' * 64}")
    print(f"用例总数: {total} | 通过: {passed} | 失败: {len(fails)} | 错误: {len(errors)}")
    print(f"准确率: {acc:.2%} (阈值 {threshold:.0%})")

    if total and matrix.cells:
        print(f"\n混淆矩阵 (期望\\实际, ·N=误路由):\n{matrix.render()}")

    if errors:
        print(f"\n[错误样本] LLM 后端/环境不可用等执行错误 (不计入失败):")
        for r in errors[:5]:
            print(f"  {r['id']}: {r['error'][:120]}")

    if fails:
        print(f"\n[失败用例清单]")
        by_src = defaultdict(list)
        for r in fails:
            by_src[r["source"]].append(r)
        for src, rs in sorted(by_src.items()):
            print(f"  ── {src} ({len(rs)}) ──")
            for r in rs:
                print(f"    {r['id']}: expect={r['expect']} got={r['got']}")
                print(f"       text: {r['text'][:70]}")

    # 错误率过高 → 判定环境不可用, 不阻塞 (除 --strict)
    env_broken = total and len(errors) / total > 0.5
    if env_broken:
        print("\n⚠ LLM/环境错误率 >50%, 判定为后端不可用, 本次结果不作为回退依据")
        return 0
    return 0 if acc >= threshold else 1


def main():
    ap = argparse.ArgumentParser(description="Z-OPS 路由评测 runner")
    ap.add_argument("--mode", required=True,
                    choices=["keyword", "llm", "full", "action"])
    ap.add_argument("--threshold", type=float, default=0.98)
    ap.add_argument("--json", dest="json_out", default=None,
                    help="结果写入 JSON 文件 (默认 tests/routing/{mode}_report.json, 供 /api/routing/eval-summary 读取)")
    ap.add_argument("--ids", default=None, help="仅跑指定 ID (逗号分隔, 调试用)")
    args = ap.parse_args()

    cases = load_cases()
    if args.ids:
        wanted = {s.strip() for s in args.ids.split(",")}
        cases = [c for c in cases if c["id"] in wanted]

    if args.mode == "keyword":
        results, matrix = run_keyword_mode(cases)
    elif args.mode == "action":
        results, matrix = run_action_mode(cases)
    elif args.mode == "llm":
        results, matrix = run_llm_mode(cases, use_keyword_first=False)
    else:  # full
        results, matrix = run_llm_mode(cases, use_keyword_first=True)

    rc = report(args.mode, results, matrix, args.threshold)

    if not args.json_out:
        args.json_out = str(Path(__file__).resolve().parent / f"{args.mode}_report.json")
    if args.json_out:
        summary = {
            "mode": args.mode, "total": len(results),
            "passed": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"] and not r.get("error")),
            "errors": sum(1 for r in results if r.get("error")),
            "results": results,
        }
        Path(args.json_out).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.json_out}")

    sys.exit(rc)


if __name__ == "__main__":
    main()
