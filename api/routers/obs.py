"""Z-OPS /api 可观测性 域路由 (v4.0 Phase 6: 从 webchat_api.py 拆分, 行为不变)

端点逻辑原样搬迁 (@app → @router); 共享依赖经 from webchat_api import * 获取 —
webchat_api.py 在文件底部 include_router, 彼时全部共享名已定义, 无循环导入问题。
"""
from webchat_api import *  # noqa: F401,F403  (config/logger/get_redis/verify_token/...)
from core.bizperms import require_admin_or_biz_perm  # 业务权限门 (与系统角色分离)
from webchat_api import (_emit_daily_log)  # noqa: F401  本域引用的主文件私有辅助
from fastapi import APIRouter

router = APIRouter()



@router.get("/api/usage/current")
async def get_current_usage(session_id: str = "", current_user: str = Depends(verify_token)):
    """获取当前会话的 token 用量统计 (参考 claude-code cost-tracker)"""
    r = await get_redis()
    usage_data = []
    # 全窗口 (列表 ltrim 上限 1000); 会话口径=严格相等过滤 —
    # 旧条目缺 session_id 时不得计入他人会话 (10-19 修"新会话显示100次调用"假数据)
    usage_raw = await r.lrange(config.KEY_LLM_USAGE, -1000, -1)
    for raw in usage_raw:
        try:
            entry = json.loads(raw)
            if session_id and entry.get("session_id") != session_id:
                continue
            usage_data.append(entry)
        except json.JSONDecodeError:
            continue

    total_input = sum(e.get("input_tokens", 0) for e in usage_data)
    total_output = sum(e.get("output_tokens", 0) for e in usage_data)
    total_tool_calls = sum(e.get("tool_calls", 0) for e in usage_data)
    total_elapsed = sum(e.get("elapsed", 0) for e in usage_data)

    # 会话压缩状态
    compact_stats = {}
    if session_id:
        try:
            engine = get_session_engine()
            compact_stats = await engine.get_session_stats(session_id)
        except Exception:
            pass

    return {
        "total_calls": len(usage_data),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_tool_calls": total_tool_calls,
        "total_elapsed_seconds": round(total_elapsed, 2),
        "compact_stats": compact_stats,
        "recent": usage_data[-5:] if usage_data else [],
    }

# ═══ LLM 用量统计 ═══
@router.get("/api/usage")
async def usage(limit: int = 100, current_user: str = Depends(verify_token)):
    await require_admin_or_biz_perm(current_user, "observability.detail")  # 明细(含对话内容,敏感)
    r = await get_redis()
    raw_list = await r.lrange(config.KEY_LLM_USAGE, -limit, -1)
    records = []
    total_elapsed = 0
    for raw in raw_list:
        try:
            rec = json.loads(raw)
            records.append(rec)
            total_elapsed += rec.get("elapsed", 0)
        except json.JSONDecodeError:
            continue
    return {"records": records, "count": len(records), "total_elapsed": round(total_elapsed, 2)}

# ═══ 可观测性 API (Observability Dashboard) ═══
# 参考 claw-code execution_registry + claude-code cost-tracker
# 为 ExecutionRegistry / Harness / ToolPool 提供统一查询接口

@router.get("/api/observability/executions")
async def obs_executions(date: str = "", agent: str = "", limit: int = 200, trace_id: str = "", current_user: str = Depends(verify_token)):
    """查询执行记录 (ExecutionRegistry; v4.0: trace_id 全链聚合 msg_id → 路由 → agent → tool)"""
    await require_admin_or_biz_perm(current_user, "observability.detail")  # 明细(含对话内容,敏感)
    try:
        from core.execution_registry import get_execution_registry
        registry = get_execution_registry()
        records = await registry.get_daily_executions(date_str=date or None, agent_id=agent, limit=limit, trace_id=trace_id)
        return {"date": date, "trace_id": trace_id, "count": len(records), "records": records}
    except Exception as e:
        return {"error": str(e), "records": [], "count": 0}

async def _obs_recent_records(days: int = 7, per_day_limit: int = 1000) -> list:
    """聚合最近 N 天的真实执行记录 (供诊断/延迟/优化核算复用)"""
    from core.execution_registry import get_execution_registry
    from datetime import datetime as _dt, timedelta as _td
    registry = get_execution_registry()
    records = []
    for d in range(max(days, 1)):
        date_str = (_dt.now() - _td(days=d)).strftime("%Y-%m-%d")
        records.extend(await registry.get_daily_executions(date_str=date_str, limit=per_day_limit))
    records.sort(key=lambda x: x.get("started_at", 0))
    return records

@router.get("/api/observability/stats")
async def obs_stats(days: int = 7, current_user: str = Depends(verify_token)):
    """查询 Agent 聚合统计 (动态扫描全部 Agent + 延迟分位数)"""
    await require_admin_or_biz_perm(current_user, "observability.view")  # 聚合统计
    try:
        from core.execution_registry import get_execution_registry
        from core import obs_analyzer
        registry = get_execution_registry()
        r = await get_redis()
        # 动态扫描所有 exec_stats key，避免硬编码遗漏 nms_agent / browser_agent 等
        agent_ids = []
        async for key in r.scan_iter(match="zops:exec_stats:*", count=200):
            aid = key.split(":")[-1]
            if aid:
                agent_ids.append(aid)
        stats = {}
        for aid in sorted(set(agent_ids)):
            s = await registry.get_agent_stats(aid)
            if s:
                stats[aid] = s

        # 用真实执行记录补充分位数与慢调用（聚合统计只有均值，无法看尾延迟）
        records = await _obs_recent_records(days=days)
        latency = obs_analyzer.analyze_latency(records)
        by_agent = {a["agent_id"]: a for a in latency["agents"]}
        for aid, s in stats.items():
            la = by_agent.get(aid)
            if la:
                s.update({
                    "p50_latency_ms": la["p50_ms"],
                    "p95_latency_ms": la["p95_ms"],
                    "p99_latency_ms": la["p99_ms"],
                    "max_latency_ms": la["max_ms"],
                    "slow_calls": la["slow_calls"],
                    "slow_ratio": la["slow_ratio"],
                    "window_calls": la["calls"],
                })

        from datetime import datetime as _dt
        date_key = f"zops:executions:{_dt.now().strftime('%Y-%m-%d')}"
        today_count = await r.llen(date_key)
        fail_count = await r.llen("zops:exec_failures")
        return {
            "agents": stats,
            "today_executions": today_count,
            "total_failures": fail_count,
            "window_days": days,
            "slow_threshold_ms": latency["slow_threshold_ms"],
            "latency": latency,
            "timestamp": time.time()
        }
    except Exception as e:
        logger.warning(f"[api] observability/stats 错误: {e}")
        return {"error": str(e), "agents": {}}

@router.get("/api/observability/failures")
async def obs_failures(limit: int = 50, current_user: str = Depends(verify_token)):
    """查询最近失败记录 (供 Harness 分析)"""
    await require_admin_or_biz_perm(current_user, "observability.detail")  # 明细(含对话内容,敏感)
    try:
        from core.execution_registry import get_execution_registry
        registry = get_execution_registry()
        failures = await registry.get_recent_failures(limit=limit)
        return {"count": len(failures), "failures": failures}
    except Exception as e:
        return {"error": str(e), "failures": [], "count": 0}

@router.get("/api/observability/harness")
async def obs_harness(current_user: str = Depends(verify_token)):
    """查询 Harness 自我进化状态 — 技能生成/改进/A-B测试"""
    await require_admin_or_biz_perm(current_user, "observability.detail")  # 明细(含对话内容,敏感)
    try:
        r = await get_redis()
        # 读取 Harness 失败记录
        fail_entries = await r.lrange("zops:exec_failures", -100, -1)
        failures = []
        for e in fail_entries:
            try:
                failures.append(json.loads(e))
            except: pass

        # 读取 skill_improvements.json
        from pathlib import Path
        imp_path = Path("/opt/zops/data/skill_improvements.json")
        improvements = []
        if imp_path.exists():
            try:
                data = json.loads(imp_path.read_text())
                raw_imps = data if isinstance(data, list) else data.get("improvements", [])
                # improvements 可能是 dict{name: {...}} 也可能是 list
                if isinstance(raw_imps, dict):
                    improvements = [{"name": k, **v} if isinstance(v, dict) else {"name": k, "value": v} for k, v in raw_imps.items()]
                elif isinstance(raw_imps, list):
                    improvements = raw_imps
            except: pass

        # 读取经验库: experiences/*.jsonl 是 ExperienceDatabase 的真实存储 (每技能一个 jsonl);
        # 旧版误读 data/experiences.json (从未有代码写入 → 计数恒为 0)，现改为聚合真实库并保留旧文件兼容
        exp_dir = Path("/opt/zops/experiences")
        recent_experiences = []
        experiences_count = 0
        if exp_dir.exists():
            for ef in exp_dir.glob("*.jsonl"):
                try:
                    lines = ef.read_text(encoding="utf-8").strip().splitlines()
                except Exception:
                    continue
                lines = [ln for ln in lines if ln.strip()]
                experiences_count += len(lines)
                for ln in lines[-10:]:
                    try:
                        e = json.loads(ln)
                    except Exception:
                        continue
                    if e.get("success"):
                        lessons = e.get("lessons") or []
                        insight = (lessons[0][:120] if lessons
                                   else f"执行成功 ({float(e.get('execution_time') or 0):.1f}s)")
                    else:
                        insight = f"[{e.get('root_cause') or 'error'}] {(e.get('error_message') or '')[:100]}"
                    recent_experiences.append({
                        "category": f"{e.get('skill_name', ef.stem)}/{e.get('action', '')}",
                        "insight": insight,
                        "success": bool(e.get("success")),
                        "timestamp": e.get("timestamp"),
                    })
            recent_experiences.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)
            recent_experiences = recent_experiences[:20]
        # 兼容旧格式 data/experiences.json (若存在则并入)
        exp_path = Path("/opt/zops/data/experiences.json")
        if exp_path.exists():
            try:
                exp_data = json.loads(exp_path.read_text())
                all_exps = exp_data if isinstance(exp_data, list) else []
                experiences_count += len(all_exps)
                for e in all_exps[-20:]:
                    if isinstance(e, dict):
                        recent_experiences.append({
                            "category": e.get("category") or e.get("type") or "通用",
                            "insight": e.get("insight") or e.get("content") or "",
                            "success": bool(e.get("success", True)),
                            "timestamp": e.get("timestamp") or e.get("created_at"),
                        })
            except Exception:
                pass

        # 读取 skills 目录数量
        skills_dir = Path("/opt/zops/skills")
        skill_count = 0
        skill_list = []
        if skills_dir.exists():
            for p in skills_dir.rglob("*.yaml"):
                skill_count += 1
                skill_list.append(p.stem)

        return {
            "failures": {"count": len(failures), "recent": failures[-20:]},
            "improvements": {"count": len(improvements), "recent": improvements[-20:]},
            "experiences": {"count": experiences_count, "recent": recent_experiences},
            "skills": {"count": skill_count, "names": skill_list[:50]},
            "harness_enabled": config.HARNESS_AUTO_SKILL_ENABLED,
            "timestamp": time.time()
        }
    except Exception as e:
        return {"error": str(e)}

@router.get("/api/observability/toolpool")
async def obs_toolpool(current_user: str = Depends(verify_token)):
    """查询 ToolPool 注册信息 (从 Redis 读取, 跨进程可见)

    status 以 zops:heartbeats 实时心跳为准 (120s 内有心跳 = online)，
    避免陈旧注册 (如已停用 agent 的旧字段) 永远假装 online → 数据脱节修复
    """
    await require_admin_or_biz_perm(current_user, "observability.detail")  # 明细(含对话内容,敏感)
    try:
        r = await get_redis()
        raw = await r.hgetall("zops:toolpool:agents")
        heartbeats = await r.hgetall(config.KEY_HEARTBEATS)
        # 真实调用数据与性能页同源 (ExecutionRegistry)，注册面 ≠ 调用面从此可对照
        from core.execution_registry import get_execution_registry
        registry = get_execution_registry()
        now = time.time()
        hb_map = {}
        for aid, hb_str in heartbeats.items():
            try:
                hb = json.loads(hb_str)
                hb_map[aid] = float(hb.get("last_heartbeat", 0) or 0)
            except Exception:
                pass
        agents = []
        total_tools = 0
        total_caps = 0
        total_calls = 0
        for agent_id, data_str in raw.items():
            try:
                d = json.loads(data_str)
            except Exception:
                continue
            last_hb = hb_map.get(agent_id, 0)
            alive = last_hb and (now - last_hb) < 120
            d["status"] = "online" if alive else "offline"
            d["last_heartbeat"] = last_hb
            # Agent 级真实调用统计 + per-tool 调用热度
            try:
                d["usage"] = await registry.get_agent_stats(agent_id)
                d["tool_usage"] = {t["tool_name"]: t for t in await registry.get_agent_tool_stats(agent_id)}
                total_calls += int(d["usage"].get("total_calls", 0))
            except Exception:
                d["usage"] = {}
                d["tool_usage"] = {}
            agents.append(d)
            total_tools += d.get("tool_count", 0)
            total_caps += len(d.get("capabilities", []))
        agents.sort(key=lambda a: (a.get("status") != "online", a.get("agent_id", "")))
        online_count = sum(1 for a in agents if a.get("status") == "online")
        return {
            "agent_count": len(agents),
            "online_count": online_count,
            "total_tools": total_tools,
            "total_capabilities": total_caps,
            "total_calls": total_calls,
            "agents": agents,
            "timestamp": now
        }
    except Exception as e:
        return {"error": str(e), "agents": []}

@router.get("/api/observability/timeline")
async def obs_timeline(session_id: str = "", limit: int = 50, days: int = 3, trace_id: str = "", current_user: str = Depends(verify_token)):
    """查询执行时间线 (聚合最近N天数据; v4.0: trace_id 按全链聚合)"""
    await require_admin_or_biz_perm(current_user, "observability.detail")  # 明细(含对话内容,敏感)
    try:
        from core.execution_registry import get_execution_registry
        from datetime import datetime, timedelta
        registry = get_execution_registry()
        if trace_id:
            records = await registry.get_trace_executions(trace_id, limit=limit * 4)
            records.sort(key=lambda x: x.get("started_at", 0))
            return {"count": len(records), "trace_id": trace_id, "timeline": records[-limit:]}
        records = []
        # 聚合最近N天的执行记录
        for d in range(days):
            date_str = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")
            day_records = await registry.get_daily_executions(date_str=date_str, limit=limit)
            records.extend(day_records)
        if session_id:
            records = [r for r in records if r.get("session_id") == session_id]
        # 按时间排序
        records.sort(key=lambda x: x.get("started_at", 0))
        return {"count": len(records), "timeline": records[-limit:]}
    except Exception as e:
        return {"error": str(e), "timeline": []}

@router.get("/api/observability/diagnostics")
async def obs_diagnostics(days: int = 7, current_user: str = Depends(verify_token)):
    """失败诊断: 错误指纹聚类 + 根因分类 + 优化方向 + 慢调用离群点"""
    await require_admin_or_biz_perm(current_user, "observability.detail")  # 明细(含对话内容,敏感)
    try:
        from core.execution_registry import get_execution_registry
        from core import obs_analyzer
        registry = get_execution_registry()

        failures = await registry.get_recent_failures(limit=obs_analyzer_failure_limit())
        records = await _obs_recent_records(days=days)
        now = time.time()

        groups = obs_analyzer.group_failures(failures, now=now)
        latency = obs_analyzer.analyze_latency(records)

        window_fail = [r for r in records if r.get("success") is False]
        total = len(records)
        affected_sessions = {r.get("session_id") for r in failures if r.get("session_id")}
        lat_vals = [float(r.get("latency_ms") or 0) for r in records]

        by_category = {}
        for g in groups:
            c = by_category.setdefault(g["category"], {
                "category": g["category"], "label": g["category_label"],
                "count": 0, "groups": 0,
            })
            c["count"] += g["count"]
            c["groups"] += 1

        return {
            "summary": {
                "window_days": days,
                "window_executions": total,
                "window_failures": len(window_fail),
                "failure_rate": round(len(window_fail) / total * 100, 2) if total else 0,
                "failure_pool": len(failures),
                "error_groups": len(groups),
                "active_groups": sum(1 for g in groups if g["state"] == "active"),
                "affected_sessions": len(affected_sessions),
                "slow_calls": sum(1 for v in lat_vals if v >= latency["slow_threshold_ms"]),
                "p95_latency_ms": obs_analyzer.percentile(lat_vals, 95),
                "max_latency_ms": round(max(lat_vals), 1) if lat_vals else 0,
            },
            "error_groups": groups,
            "by_category": sorted(by_category.values(), key=lambda x: -x["count"]),
            "latency": latency,
            "timestamp": now,
        }
    except Exception as e:
        logger.warning(f"[api] observability/diagnostics 错误: {e}")
        return {"error": str(e), "error_groups": [], "summary": {}}

def obs_analyzer_failure_limit() -> int:
    """失败池拉取上限 (与 ExecutionRegistry.FAILURE_MAX 对齐)"""
    try:
        from core.execution_registry import ExecutionRegistry
        return ExecutionRegistry.FAILURE_MAX
    except Exception:
        return 500

@router.get("/api/observability/optimizations")
async def obs_optimizations(days: int = 30, current_user: str = Depends(verify_token)):
    """优化闭环台账: 用真实执行数据核算每条优化的落地效果"""
    await require_admin_or_biz_perm(current_user, "observability.detail")  # 明细(含对话内容,敏感)
    try:
        from core.execution_registry import get_execution_registry
        from core import obs_analyzer
        registry = get_execution_registry()
        failures = await registry.get_recent_failures(limit=obs_analyzer_failure_limit())
        records = await _obs_recent_records(days=days)
        # 护栏核算预取 (异步): 每条落地/已验证优化按各自的 landed_at 独立核算
        # (同 agent 多项不能共用窗口), 结果按 item_id 交给 build_optimizations 标红
        guardrails = {}
        try:
            from core.guardrail import guardrail_summaries
            cache: dict = {}   # (owner, landed_at) → summary, 避免重复扫描
            for it in obs_analyzer.load_ledger():
                if it.get("status") not in ("landed", "verified"):
                    continue
                la = float(it.get("landed_at") or 0)
                owner = (it.get("owner") or "").strip()
                if la <= 0 or not owner:
                    continue
                key = (owner, la)
                if key not in cache:
                    cache[key] = (await guardrail_summaries({owner: la})).get(owner)
                if cache[key]:
                    guardrails[it.get("id")] = cache[key]
        except Exception as e:
            logger.debug(f"[api] 护栏核算失败: {e}")
        return obs_analyzer.build_optimizations(failures, records, guardrails=guardrails)
    except Exception as e:
        logger.warning(f"[api] observability/optimizations 错误: {e}")
        return {"error": str(e), "items": [], "board": {}}

@router.post("/api/observability/optimizations")
async def obs_optimizations_upsert(request: Request, current_user: str = Depends(verify_token)):
    """新建/推进一条优化 (从错误组立项、标记落地或验证)"""
    await require_admin_or_biz_perm(current_user, "observability.detail")  # 明细(含对话内容,敏感)
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title and not (body.get("id") or "").strip():
        raise HTTPException(400, "缺少优化标题")
    status = body.get("status") or "planned"
    from core import obs_analyzer
    if status not in obs_analyzer.STATUS_ORDER:
        raise HTTPException(400, f"非法状态: {status}")
    try:
        item = obs_analyzer.upsert_optimization(body, actor=current_user)
    except Exception as e:
        logger.warning(f"[api] observability/optimizations 写入失败: {e}")
        raise HTTPException(500, f"写入失败: {e}")
    # 缺基线自动补采 (Harness 推进核算): 延迟类优化无 baseline 时,
    # 自动快照当前 exec_stats 均值作基线 → 落地后立即能做 before/after 对比。
    # 立项时点快照最优 (纯优化前数据), 已有基线则不动 (幂等)。
    try:
        import time
        metric = dict(item.get("metric") or {})
        if metric.get("type") == "latency" and not metric.get("baseline_avg_ms"):
            aid = metric.get("agent_id") or item.get("owner") or ""
            if aid:
                from core.execution_registry import get_execution_registry
                stats = await get_execution_registry().get_agent_stats(aid)
                avg = float(stats.get("avg_latency_ms") or 0)
                calls = int(stats.get("total_calls") or 0)
                if avg > 0 and calls > 0:
                    metric["baseline_avg_ms"] = round(avg, 1)
                    metric["baseline_source"] = (
                        f"harness 自动快照 ({time.strftime('%Y-%m-%d')} "
                        f"zops:exec_stats:{aid}，{calls} 次调用均值)"
                    )
                    item = obs_analyzer.upsert_optimization(
                        {"id": item["id"], "metric": metric}, actor="harness")
                    logger.info(f"[api] 优化 {item['id']} 自动补基线: {aid} avg={round(avg)}ms/{calls}次")
    except Exception as e:
        logger.debug(f"[api] 自动补基线失败: {e}")
    await _emit_daily_log(
        "system",
        f"{current_user} 更新优化台账: {item.get('title', item['id'])} → {item.get('status')}",
        level="info", category="observability", username=current_user,
    )
    return {"status": "ok", "item": item}
