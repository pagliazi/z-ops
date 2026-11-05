"""护栏/锚定/回滚 端到端验证脚本 (一次性)
场景模拟: opt-guardrail-test 5 天前落地, 落地前命中 80% → 落地后崩到 30%, 兜底暴增
预期: verdict=regression 标红 + 护栏明细 + 漂移黄条 + rolled_back 归档 + auto_suggest 默认关
"""
import asyncio, json, sys, time
sys.path.insert(0, "/opt/zops")
import redis as sync_redis

LEDGER = "/opt/zops/data/optimizations.json"
API = "http://127.0.0.1:7789/api/observability/optimizations"
now = time.time()
LANDED = now - 5 * 86400
import config as cfg

OK, FAIL = "  ✓", "  ✗"
fails = 0


def check(name, cond, extra=""):
    global fails
    print(f"{OK if cond else FAIL} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails += 1


async def main():
    import httpx
    r = sync_redis.from_url(cfg.REDIS_URL, decode_responses=True)

    # ── 0. 备份台账 + 造临时 token ─────────────────────────
    backup = open(LEDGER).read()
    tok = f"gt-{int(now)}"
    r.set(cfg.key_token(tok), "svc-opscore", ex=600)
    H = {"Authorization": f"Bearer {tok}"}
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            # ── 1. 基线: GET 正常, board 含 rolled_back, auto_suggest 默认空 ──
            d = (await c.get(API, headers=H)).json()
            check("GET 台账正常", "board" in d, str(d)[:120])
            check("board 含 rolled_back", "rolled_back" in (d.get("board") or {}))
            check("auto_suggest 默认关闭(空)", d.get("auto_suggest") == [], str(d.get("auto_suggest")))

            # ── 2. 建测试项 (进行中 → 落地, 走真实转态触发锚定) ─────────
            p = await c.post(API, headers=H, json={
                "id": "opt-guardrail-test", "title": "[验证]护栏回归探测联调项",
                "owner": "kb_agent", "status": "in_progress", "category": "latency",
                "files": ["agents/kb_agent.py"],
                "problem": "模拟: 跳过改写导致召回变差", })
            check("建项(进行中)", p.status_code == 200, p.text[:150])

            # ── 3. 种护栏数据: 落地前 7 天 80% 命中, 落地后 5 天 30% 命中 ──
            pipe = r.pipeline()
            for back in range(7, 0, -1):   # 落地前: now-12d .. now-6d
                ts = LANDED - back * 86400
                day = time.strftime("%Y-%m-%d", time.localtime(ts + 43200))
                k = f"{cfg.REDIS_PREFIX}:guardrail:{day}"
                pipe.hset(k, mapping={"kb_agent:kb_hybrid_hit": 40, "kb_agent:kb_hybrid_miss": 10})
            for fwd in range(5, -1, -1):   # 落地后: now-5d .. today
                ts = now - fwd * 86400
                day = time.strftime("%Y-%m-%d", time.localtime(ts + 43200))
                k = f"{cfg.REDIS_PREFIX}:guardrail:{day}"
                pipe.hset(k, mapping={"kb_agent:kb_hybrid_hit": 15, "kb_agent:kb_hybrid_miss": 35,
                              "kb_agent:kb_variant_fallback": 30, "kb_agent:kb_final_no_hit": 25})
            pipe.execute()
            print("  · 已种护栏数据: 前 80% 命中 → 后 30% 命中")

            # ── 4. 标记落地 (触发锚定: git commit + 文件指纹) ───────────
            p = await c.post(API, headers=H, json={"id": "opt-guardrail-test", "status": "landed"})
            check("标记落地", p.status_code == 200, p.text[:150])
            led = json.loads(open(LEDGER).read())
            item = next(x for x in led["optimizations"] if x["id"] == "opt-guardrail-test")
            anc = item.get("landed_anchor") or {}
            check("锚定含 git commit", bool(anc.get("git")), str(anc))
            check("锚定含文件指纹", anc.get("files", {}).get("agents/kb_agent.py", "") != "", str(anc))

            # ── 5. 核算: landed_at 回拨 5 天 → verdict=regression + 明细 ─
            item["landed_at"] = LANDED
            open(LEDGER, "w").write(json.dumps(led, ensure_ascii=False, indent=2))
            d = (await c.get(API, headers=H)).json()
            it = next((x for x in d.get("items", []) if x["id"] == "opt-guardrail-test"), None)
            check("GET 找到测试项", it is not None)
            if it:
                res = it.get("result") or {}
                g = res.get("guardrail") or {}
                check("verdict=regression(护栏)", res.get("verdict") == "regression", res.get("verdict_label"))
                check("护栏明细存在", bool(g.get("signals")), json.dumps(g, ensure_ascii=False)[:200])
                sig = g.get("signals", {}).get("kb_hybrid_miss", {})
                check("未命中率 20→70 (±2pp)", abs((sig.get("after_pct") or 0) - 70) <= 2 and abs((sig.get("before_pct") or 0) - 20) <= 2,
                      f"before={sig.get('before_pct')} after={sig.get('after_pct')}")
                check("regressions 含 miss+fallback", {"kb_hybrid_miss", "kb_variant_fallback"} <= set(g.get("regressions") or []),
                      str(g.get("regressions")))
                check("漂移未触发(文件未再变)", it.get("drift") is None, str(it.get("drift")))

            # ── 6. 漂移: 篡改锚定指纹 → GET 亮黄条 ─────────────────────
            led = json.loads(open(LEDGER).read())
            item = next(x for x in led["optimizations"] if x["id"] == "opt-guardrail-test")
            item["landed_anchor"].setdefault("files", {})["agents/kb_agent.py"] = "deadbeefcafe"
            open(LEDGER, "w").write(json.dumps(led, ensure_ascii=False, indent=2))
            d = (await c.get(API, headers=H)).json()
            it = next((x for x in d.get("items", []) if x["id"] == "opt-guardrail-test"), None)
            drift = (it or {}).get("drift") or {}
            check("漂移检测亮起", "agents/kb_agent.py" in (drift.get("changed") or []), str(drift))

            # ── 7. 回滚归档: 带原因 → 台账字段 + 不再判回归 ────────────
            p = await c.post(API, headers=H, json={
                "id": "opt-guardrail-test", "status": "rolled_back",
                "rollback_reason": "验证: 模拟召回回归后人工回滚"})
            check("标记已回滚", p.status_code == 200, p.text[:150])
            d = (await c.get(API, headers=H)).json()
            it = next((x for x in d.get("items", []) if x["id"] == "opt-guardrail-test"), None)
            check("状态=已回滚", (it or {}).get("status") == "rolled_back" and (it or {}).get("status_label") == "已回滚")
            check("回滚原因入账", "模拟召回回归" in ((it or {}).get("rollback_reason") or ""))
            check("已回滚不再判回归", ((it or {}).get("result") or {}).get("verdict") != "regression")
            check("board 计数 rolled_back=1", (d.get("board") or {}).get("rolled_back") == 1)

            # ── 8. 自动建议: 默认关 → 打开后 (直接调 build) 有输出 ─────
            from core import obs_analyzer
            led = json.loads(open(LEDGER).read())
            item = next(x for x in led["optimizations"] if x["id"] == "opt-guardrail-test")
            item["status"] = "landed"   # 临时恢复 landed 以便测开关
            open(LEDGER, "w").write(json.dumps(led, ensure_ascii=False, indent=2))
            gs = {"opt-guardrail-test": {"signals": {}, "regressions": ["kb_hybrid_miss"], "sample_ok": True, "baseline_days": 7}}
            import config
            before_flag = config.OPT_AUTO_ROLLBACK_SUGGEST
            config.OPT_AUTO_ROLLBACK_SUGGEST = False
            out = obs_analyzer.build_optimizations([], [], guardrails=gs)
            check("开关关: auto_suggest=[]", out.get("auto_suggest") == [])
            config.OPT_AUTO_ROLLBACK_SUGGEST = True
            config.OPT_ROLLBACK_SUGGEST_DAYS = 3
            out = obs_analyzer.build_optimizations([], [], guardrails=gs)
            sug = [s for s in out.get("auto_suggest", []) if s["id"] == "opt-guardrail-test"]
            check("开关开: 建议回滚(5天≥3天)", len(sug) == 1 and sug[0]["days"] >= 3, str(sug))
            config.OPT_AUTO_ROLLBACK_SUGGEST = before_flag
        finally:
            # ── 9. 清理: 还原台账 + 删护栏假数据 + 删 token ─────────────
            open(LEDGER, "w").write(backup)
            for k in r.scan_iter(f"{cfg.REDIS_PREFIX}:guardrail:*"):
                r.delete(k)
            r.delete(cfg.key_token(tok))
            print("  · 已还原台账/清除测试数据")

    # ── 10. kb_agent 埋点 live: bump 直接调用后今日桶有计数 ──────────
    from core.guardrail import bump_guardrail, guardrail_summaries
    await bump_guardrail("kb_agent", "kb_hybrid_hit", 3)
    await bump_guardrail("kb_agent", "kb_hybrid_miss", 1)
    r2 = sync_redis.from_url(cfg.REDIS_URL, decode_responses=True)
    today = time.strftime("%Y-%m-%d")
    vals = r2.hgetall(f"{cfg.REDIS_PREFIX}:guardrail:{today}")
    check("埋点写入今日桶", vals.get("kb_agent:kb_hybrid_hit") == "3", str(vals))
    summ = await guardrail_summaries({"kb_agent": now - 86400})
    check("核算函数返回", "kb_agent" in summ and summ["kb_agent"].get("signals"), str(summ)[:150])
    r2.delete(f"{cfg.REDIS_PREFIX}:guardrail:{today}")

    print(f"\n{'✅ 全部通过' if fails == 0 else f'❌ {fails} 项失败'}")


asyncio.run(main())
