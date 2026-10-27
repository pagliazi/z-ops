"""核算闭环自动推进 验证脚本 (一次性):
1. 缺基线自动补采: POST 建延迟类优化 → baseline 自动快照
2. 达标自动升级: 伪造 records 直调 build → landed 项收益达标≥3天 自动升级已验证+证据; 幂等
3. pending 新文案
"""
import asyncio, json, sys, time
sys.path.insert(0, "/opt/zops")
import config as cfg
import redis as sr

LEDGER = "/opt/zops/data/optimizations.json"
API = "http://127.0.0.1:7789/api/observability/optimizations"
now = time.time()
fails = 0


def check(name, cond, extra=""):
    global fails
    print(f"{'  ✓' if cond else '  ✗'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails += 1


async def main():
    import httpx
    from core import obs_analyzer
    r = sr.from_url(cfg.REDIS_URL, decode_responses=True)
    backup = open(LEDGER).read()
    tok = f"av-{int(now)}"
    r.set(cfg.key_token(tok), "svc-gcoc", ex=600)
    H = {"Authorization": f"Bearer {tok}"}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            # ── 1. 缺基线自动补采 (受控: 种 t_agent exec_stats 6次/600s → 基线=100s) ──
            r.hset(f"{cfg.REDIS_PREFIX}:exec_stats:t_agent", mapping={
                "total_calls": 6, "success_calls": 6, "total_latency_ms": 600000})
            p = await c.post(API, headers=H, json={
                "id": "opt-autoverify-test", "title": "[验证]核算自动推进",
                "owner": "t_agent", "status": "in_progress",
                "metric": {"type": "latency", "agent_id": "t_agent"},
            })
            item = p.json().get("item") or {}
            m = item.get("metric") or {}
            check("建项返回 200", p.status_code == 200, p.text[:120])
            check("基线自动补采", float(m.get("baseline_avg_ms") or 0) == 100000.0,
                  f"baseline={m.get('baseline_avg_ms')}")
            check("基线来源标注 harness", "harness" in (m.get("baseline_source") or ""), m.get("baseline_source"))
            # 幂等: 再 POST 一次不带 metric → 基线不被覆盖
            p2 = await c.post(API, headers=H, json={"id": "opt-autoverify-test", "status": "in_progress"})
            m2 = ((p2.json().get("item") or {}).get("metric") or {})
            check("基线幂等不覆盖", m2.get("baseline_avg_ms") == m.get("baseline_avg_ms"))

            # ── 2. pending 新文案 ─────────────────────────────────────
            out = obs_analyzer.build_optimizations([], [])
            it = next((x for x in out["items"] if x["id"] == "opt-autoverify-test"), None)
            det = ((it or {}).get("result") or {}).get("detail") or ""
            check("pending 新文案", "自动锚定" in det, det[:80])

            # ── 3. 达标自动升级 (受控: 伪 records + 临时种台账) ────────
            led = json.loads(open(LEDGER).read())
            ti = next(x for x in led["optimizations"] if x["id"] == "opt-autoverify-test")
            ti["status"] = "landed"
            ti["landed_at"] = now - 4 * 86400   # 落地 4 天 ≥ 3 天
            ti["metric"] = {"type": "latency", "agent_id": "t_agent",
                            "baseline_avg_ms": 100000.0}
            open(LEDGER, "w").write(json.dumps(led, ensure_ascii=False, indent=2))
            records = [{"agent_id": "t_agent", "latency_ms": 10000 + i * 100,
                        "started_at": now - i * 3600} for i in range(8)]  # 均值≈10.35s → -89.6%
            out = obs_analyzer.build_optimizations([], records)
            it = next((x for x in out["items"] if x["id"] == "opt-autoverify-test"), None)
            check("自动升级已验证", (it or {}).get("status") == "verified", (it or {}).get("status"))
            check("自动证据入账", "[harness auto]" in ((it or {}).get("verification") or ""),
                  ((it or {}).get("verification") or "")[:100])
            check("本次看板即时生效", (out.get("board") or {}).get("verified", 0) >= 1)
            led2 = json.loads(open(LEDGER).read())
            ti2 = next(x for x in led2["optimizations"] if x["id"] == "opt-autoverify-test")
            check("台账落盘 verified", ti2.get("status") == "verified" and ti2.get("updated_by") == "harness")
            # 幂等: 再跑一次不重复追加证据
            out = obs_analyzer.build_optimizations([], records)
            it = next((x for x in out["items"] if x["id"] == "opt-autoverify-test"), None)
            n_proof = ((it or {}).get("verification") or "").count("[harness auto]")
            check("升级幂等(仅1条证据)", n_proof == 1, f"n={n_proof}")
    finally:
        open(LEDGER, "w").write(backup)
        r.delete(cfg.key_token(tok))
        r.delete(f"{cfg.REDIS_PREFIX}:exec_stats:t_agent")
        print("  · 已还原台账")

    print(f"\n{'✅ 全部通过' if fails == 0 else f'❌ {fails} 项失败'}")


asyncio.run(main())
