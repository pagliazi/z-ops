"""Z-OPS /api 日志与审计 域路由 (v4.0 Phase 6: 从 webchat_api.py 拆分, 行为不变)

端点逻辑原样搬迁 (@app → @router); 共享依赖经 from webchat_api import * 获取 —
webchat_api.py 在文件底部 include_router, 彼时全部共享名已定义, 无循环导入问题。
"""
from webchat_api import *  # noqa: F401,F403  (config/logger/get_redis/verify_token/...)
from fastapi import APIRouter
from datetime import timedelta
from typing import Any, Dict

router = APIRouter()



# ═══ 聊天历史 (全局, 向后兼容) ═══
@router.get("/api/history")
async def history(limit: int = 50, current_user: str = Depends(verify_token)):
    await require_admin(current_user)
    r = await get_redis()
    raw_list = await r.lrange(config.KEY_CHAT_HISTORY, -limit, -1)
    records = []
    for raw in raw_list:
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return {"history": records, "count": len(records)}

# ═══ 每日工作日志 (增强: 会话分组 + 日期范围 + 用户审计合并) ═══
_USER_OPS_LEVEL_MAP = {
    # user_ops.action → 日志侧 level (供前端统一展示/过滤)
    "login":         "info",
    "logout":        "info",
    "query":         "info",
    "biz_perms":     "warning",   # 权限变更 = 敏感操作 → warning
    "biz_perms_ttl": "warning",
    "biz_scope":     "warning",
    "group_biz_perms": "warning",
    "create_api_key": "info",
    "delete_api_key": "warning",
    "llm_config":    "warning",
    "add_provider":  "info",
    "delete_provider": "warning",
    "active":        "info",
}

@router.get("/api/daily-log")
async def daily_log(date: str = "", start_date: str = "", end_date: str = "", agent: str = "", level: str = "",
                    category: str = "", keyword: str = "", session_id: str = "",
                    username: str = "",
                    include_user_ops: int = 1,    # 默认合并用户审计 (user_ops:{date}) → 一站式运维视图
                    current_user: str = Depends(verify_token)):
    await require_admin(current_user)
    r = await get_redis()
    # 日期范围: start_date~end_date 优先；否则单日 date；否则今天
    if start_date and end_date:
        dates = []
        d = datetime.strptime(start_date, "%Y-%m-%d").date()
        e = datetime.strptime(end_date, "%Y-%m-%d").date()
        if d > e:
            d, e = e, d
        while d <= e:
            dates.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
    else:
        dates = [date or datetime.now().strftime("%Y-%m-%d")]
    raw_list = []
    for dt in dates:
        raw_list.extend(await r.lrange(config.daily_log_key(dt), 0, -1))
        # 合并 user_ops: 登录/登出/查询/权限变更等用户审计 (审计页与运维页共享一份原始数据)
        if include_user_ops:
            for x in await r.lrange(config.user_ops_key(dt), 0, -1):
                try:
                    e_ops = json.loads(x)
                except json.JSONDecodeError:
                    continue
                u = e_ops.get("username", "")
                if not u or config.is_audit_excluded(u):
                    continue
                act = e_ops.get("action", "")
                # 归一化为 daily_log 同构 entry, 标记 source='user_ops' 供前端区分渲染
                raw_list.append(json.dumps({
                    "agent": "auth",
                    "content": f"[用户审计] {act}: {e_ops.get('detail', '')}".rstrip(": "),
                    "level": _USER_OPS_LEVEL_MAP.get(act, "info"),
                    "category": "auth",
                    "username": u,
                    "ip": e_ops.get("ip", ""),
                    "session_id": e_ops.get("session_id", ""),
                    "audit_action": act,                # 保留原 action, 前端可做徽章
                    "source": "user_ops",
                    "timestamp": e_ops.get("timestamp", 0),
                }, ensure_ascii=False))
    logs = []
    sessions_map = {}  # session_id -> [logs]
    all_entries = []

    for raw in raw_list:
        try:
            entry = json.loads(raw)
            # 统一 source 标记 (daily_log 来源补默认值)
            if not entry.get("source"):
                entry["source"] = "daily_log"
            all_entries.append(entry)
            if agent and entry.get("agent") != agent:
                continue
            if level and entry.get("level", "info") != level:
                continue
            if category and entry.get("category", "") != category:
                continue
            if username and entry.get("username", "") != username:
                continue
            if session_id and entry.get("session_id", "") != session_id:
                continue
            if keyword:
                kw_l = keyword.lower()
                hay = " ".join([
                    entry.get("content", ""),
                    entry.get("question", ""),
                    entry.get("answer", ""),
                    entry.get("username", ""),
                    entry.get("audit_action", ""),
                    entry.get("ip", ""),
                ]).lower()
                if kw_l not in hay:
                    continue
            logs.append(entry)

            # 会话分组
            sid = entry.get("session_id", "")
            if sid:
                if sid not in sessions_map:
                    sessions_map[sid] = []
                sessions_map[sid].append(entry)
        except json.JSONDecodeError:
            continue

    # 统计
    agents_set = set()
    levels_set = set()
    categories_set = set()
    sessions_set = set()
    usernames_set = set()
    sources_set = set()
    for e in all_entries:
        agents_set.add(e.get("agent", ""))
        levels_set.add(e.get("level", "info"))
        cat = e.get("category", "")
        if cat:
            categories_set.add(cat)
        sid = e.get("session_id", "")
        if sid:
            sessions_set.add(sid)
        u = e.get("username", "")
        if u:
            usernames_set.add(u)
        s = e.get("source", "daily_log")
        if s:
            sources_set.add(s)

    # 构建会话摘要
    session_summaries = []
    for sid, entries in sessions_map.items():
        first = entries[0]
        last = entries[-1]
        agents_in = list(set(e.get("agent", "") for e in entries))
        session_summaries.append({
            "session_id": sid,
            "count": len(entries),
            "agents": agents_in,
            "first_time": first.get("timestamp", 0),
            "last_time": last.get("timestamp", 0),
            "preview": first.get("content", "")[:60]
        })
    session_summaries.sort(key=lambda x: x["last_time"], reverse=True)

    return {
        "date": dates[0], "dates": dates, "logs": logs, "count": len(logs), "total": len(all_entries),
        "filters": {
            "agents": sorted(agents_set),
            "levels": sorted(levels_set),
            "categories": sorted(categories_set),
            "sessions": sorted(sessions_set),
            "usernames": sorted(usernames_set),
            "sources": sorted(sources_set),
        },
        "session_groups": session_summaries,
    }

@router.get("/api/user-ops")
async def user_ops_list(date: str = "", count: int = 200, username: str = "",
                       current_user: str = Depends(verify_token)):
    """查询用户操作日志 (仅管理员)"""
    await require_admin(current_user)
    r = await get_redis()
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    raw_list = await r.lrange(config.user_ops_key(date), -count, -1)
    entries = []
    for raw in raw_list:
        try:
            e = json.loads(raw)
            if username and e.get("username") != username:
                continue
            entries.append(e)
        except json.JSONDecodeError:
            continue
    return {"date": date, "entries": entries, "count": len(entries)}

# ═══ 审计日志 (v2.0.0) ═══
def _expand_date_range(start_date: str, end_date: str, single_date: str) -> list:
    """展开日期范围: start_date~end_date 优先, 否则单日, 否则今天 (yyyy-MM-dd 升序)"""
    if start_date and end_date:
        dates = []
        d = datetime.strptime(start_date, "%Y-%m-%d").date()
        e = datetime.strptime(end_date, "%Y-%m-%d").date()
        # 防御: 起止倒序时交换; 单日范围仍 OK
        if d > e:
            d, e = e, d
        # 范围过大 (跨年/季度) 仍按天展开, Redis LRANGE 天然按日切片, 单次最多 366 天
        while d <= e:
            dates.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
        return dates
    return [single_date or datetime.now().strftime("%Y-%m-%d")]


@router.get("/api/audit/recent")
async def audit_recent(count: int = 100, agent: str = "",
                       date: str = "", start_date: str = "", end_date: str = "",
                       current_user: str = Depends(verify_token)):
    """获取近期审计日志 (Agent 操作)

    时间范围: start_date+end_date 优先, 否则单日 date, 否则今天;
    count 控制单日最多返回条数 (多日合并时按日分别取末尾 N 条).
    """
    await require_admin(current_user)
    r = await get_redis()
    try:
        dates = _expand_date_range(start_date, end_date, date)
        per_day = max(1, count)
        entries = []
        for dt in dates:
            key = f"{config.REDIS_PREFIX}:audit:{dt}"
            raw_list = await r.lrange(key, -per_day, -1)
            for raw in raw_list:
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        if agent:
            entries = [e for e in entries if e.get("agent_id") == agent]
        # 统一为时序降序 (跨日合并后)
        entries.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        # 截断到 count
        entries = entries[:count]
        # 统计
        total = len(entries)
        errors = sum(1 for e in entries if not e.get("success", True))
        agents_set = set(e.get("agent_id", "") for e in entries)
        return {
            "date": dates[0], "dates": dates, "entries": entries, "count": total,
            "errors": errors,
            "success_rate": round((total - errors) / max(total, 1) * 100, 1),
            "agents": sorted(agents_set),
        }
    except Exception as e:
        logger.warning(f"[api] audit/recent 错误: {e}")
        return {"date": date, "entries": [], "count": 0, "error": str(e)}


@router.get("/api/audit/user-events")
async def audit_user_events(count: int = 200, username: str = "", action: str = "",
                            date: str = "", start_date: str = "", end_date: str = "",
                            current_user: str = Depends(verify_token)):
    """用户审计 (登录/查询/配置变更/key 管理等)

    数据源: {prefix}:user_ops:{YYYY-MM-DD} (来自 _log_user_op)
    时间范围: start_date+end_date 优先, 否则单日 date, 否则今天.
    """
    await require_admin(current_user)
    r = await get_redis()
    try:
        dates = _expand_date_range(start_date, end_date, date)
        per_day = max(1, count)
        entries = []
        for dt in dates:
            key = config.user_ops_key(dt)
            raw_list = await r.lrange(key, -per_day, -1)
            for raw in raw_list:
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        # 时序降序 (跨日合并后)
        entries.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        # 过滤
        if username:
            entries = [e for e in entries if e.get("username") == username]
        if action:
            entries = [e for e in entries if e.get("action") == action]
        entries = entries[:count]
        # 统计 + 可用过滤维度 (供前端下拉)
        usernames = sorted(set(e.get("username", "") for e in entries if e.get("username")))
        actions_set = sorted(set(e.get("action", "") for e in entries if e.get("action")))
        return {
            "date": dates[0], "dates": dates, "entries": entries, "count": len(entries),
            "filters": {"usernames": usernames, "actions": actions_set},
        }
    except Exception as e:
        logger.warning(f"[api] audit/user-events 错误: {e}")
        return {"date": date, "entries": [], "count": 0, "error": str(e)}


@router.get("/api/audit/user-stats")
async def audit_user_stats(date: str = "", start_date: str = "", end_date: str = "",
                           current_user: str = Depends(verify_token)):
    """用户汇总 (按用户名聚合)

    真实登录次数: 从 user_ops:{date} action='login' 计数
    真实查询次数: 从 user_ops:{date} action='query' 计数 (每次聊天消息 = 1)
    实际 token 消耗: 从 user_llm:{date} hash 累计 input/output tokens (来自 log_daily category=llm)

    返回 [{username, logins, queries, llm_calls, input_tokens, output_tokens, total_tokens, days_active}]
    按 total_tokens 降序排列.
    临时/测试账号 (config.AUDIT_USER_EXCLUDE_PATTERNS) 计入 excluded_count 但不出现在 rows 中.
    """
    await require_admin(current_user)
    r = await get_redis()
    try:
        dates = _expand_date_range(start_date, end_date, date)
        # 按用户聚合: {username: {logins:int, queries:int, days:set}}
        per_user: Dict[str, Dict[str, Any]] = {}
        excluded_count = 0
        for dt in dates:
            raw_list = await r.lrange(config.user_ops_key(dt), 0, -1)
            for raw in raw_list:
                try:
                    e = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                u = e.get("username", "")
                if not u:
                    continue
                # 排除临时/测试账号 (在聚合前过滤, 避免大表展开)
                if config.is_audit_excluded(u):
                    excluded_count += 1
                    continue
                agg = per_user.setdefault(u, {"logins": 0, "queries": 0, "days_active": set()})
                agg["days_active"].add(dt)
                a = e.get("action", "")
                if a == "login":
                    agg["logins"] += 1
                elif a == "query":
                    agg["queries"] += 1
        # 用户级 LLM 用量 (来自 user_llm:{date} hash)
        for dt in dates:
            key = config.user_llm_key(dt)
            try:
                flat = await r.hgetall(key)
            except Exception:
                flat = {}
            # 字段格式: {username}:calls / {username}:input_tokens / {username}:output_tokens
            per_user_llm: Dict[str, Dict[str, int]] = {}
            for k, v in (flat or {}).items():
                if ":" not in k:
                    continue
                u, metric = k.rsplit(":", 1)
                if config.is_audit_excluded(u):
                    continue
                d = per_user_llm.setdefault(u, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
                try:
                    d[metric] = int(v)
                except (TypeError, ValueError):
                    pass
            for u, d in per_user_llm.items():
                agg = per_user.setdefault(u, {"logins": 0, "queries": 0, "days_active": set()})
                agg["llm_calls"] = agg.get("llm_calls", 0) + d.get("calls", 0)
                agg["input_tokens"] = agg.get("input_tokens", 0) + d.get("input_tokens", 0)
                agg["output_tokens"] = agg.get("output_tokens", 0) + d.get("output_tokens", 0)

        # 汇总成列表 + 排序
        rows = []
        for u, agg in per_user.items():
            in_t = agg.get("input_tokens", 0)
            out_t = agg.get("output_tokens", 0)
            rows.append({
                "username": u,
                "logins": agg.get("logins", 0),
                "queries": agg.get("queries", 0),
                "llm_calls": agg.get("llm_calls", 0),
                "input_tokens": in_t,
                "output_tokens": out_t,
                "total_tokens": in_t + out_t,
                "days_active": len(agg["days_active"]),
            })
        rows.sort(key=lambda x: x["total_tokens"], reverse=True)
        # 汇总总数 (供顶部统计条)
        totals = {
            "users": len(rows),
            "logins": sum(r["logins"] for r in rows),
            "queries": sum(r["queries"] for r in rows),
            "llm_calls": sum(r["llm_calls"] for r in rows),
            "input_tokens": sum(r["input_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows),
            "total_tokens": sum(r["total_tokens"] for r in rows),
        }
        return {
            "dates": dates, "rows": rows, "totals": totals,
            "excluded_count": excluded_count,
            "excluded_patterns": config.AUDIT_USER_EXCLUDE_PATTERNS,
        }
    except Exception as e:
        logger.warning(f"[api] audit/user-stats 错误: {e}")
        return {"dates": [], "rows": [], "totals": {}, "excluded_count": 0, "error": str(e)}


@router.post("/api/audit/backfill")
async def audit_backfill(date: str = "", start_date: str = "", end_date: str = "",
                          current_user: str = Depends(verify_token)):
    """一次性回填: 扫描 daily_log:{date} 重建 user_ops (query) 与 user_llm (token) 历史

    场景: user_ops/user_llm 模块上线前已存在的历史数据, 现有实现 (chat.py / log_daily) 只对
    之后的事件生效; 此端点逆向解析 daily_log:
      - category=chat + username + content 含 "提问:"  → 追加 user_ops action='query'
      - category=llm  + username + content 含 "tokens=N+M" 或 "(N→M)" → 累加 user_llm hash
      - category=llm  + 无 username 但有 session_id     → 反查 user_ops 中同会话的 query,
        拿到 username 后归集 (修复 LLM 日志未带 username 的历史数据)
      - category=llm  + session_id 也缺失              → ±300秒时间窗就近归因

    幂等: 通过 (date, msg_id/session_id+ts, action) 去重, 重复执行安全
    返回: {scanned, queries_added, llm_added, llm_attributed, dates, skipped_duplicates}
    """
    await require_admin(current_user)
    import re as _re  # 全局引入, 避免循环内重复 import 引起局部变量未定义问题
    r = await get_redis()
    try:
        dates = _expand_date_range(start_date, end_date, date)
        scanned = 0
        queries_added = 0
        llm_added = 0
        llm_attributed = 0    # 通过 session_id 反查归属的次数
        skipped_duplicates = 0
        # 去重缓存: {(date, sig): True}; 同一 (session_id, timestamp, action) 不重复追加
        seen_keys: set = set()
        # 1) 先建会话 → 用户 反查表: 扫描 user_ops:{date} 中 action=query 的条目, 建立 session_id -> username
        # 2) 同时扫描 daily_log:{date} 中 category=chat "提问:" 条目, 补全 chat 中带 session_id 的归属
        session_user_map: Dict[str, str] = {}
        # query_ts_user: 用于时间窗就近归因 (session_id 缺失时兜底)
        query_ts_user: Dict[str, tuple] = {}   # session_id -> (timestamp, username)
        for dt in dates:
            for x in await r.lrange(config.user_ops_key(dt), 0, -1):
                try:
                    e = json.loads(x)
                except json.JSONDecodeError:
                    continue
                sid = e.get("session_id", "")
                u = e.get("username", "")
                ts_e = e.get("timestamp", 0)
                if sid and u and not config.is_audit_excluded(u):
                    session_user_map.setdefault(sid, u)
                    if e.get("action") == "query":
                        # 同 session 取最早的 query 时间
                        prev = query_ts_user.get(sid)
                        if not prev or ts_e < prev[0]:
                            query_ts_user[sid] = (ts_e, u)
            # daily_log 中 category=chat + "提问:" 也建一份索引 (回填的 query 与原 user_ops 双覆盖)
            for x in await r.lrange(config.daily_log_key(dt), 0, -1):
                try:
                    e = json.loads(x)
                except json.JSONDecodeError:
                    continue
                if e.get("category") == "chat" and e.get("username") and "提问" in (e.get("content", "")):
                    sid = e.get("session_id", "")
                    u = e.get("username", "")
                    ts_e = e.get("timestamp", 0)
                    if sid and u and not config.is_audit_excluded(u):
                        session_user_map.setdefault(sid, u)
                        prev = query_ts_user.get(sid)
                        if not prev or ts_e < prev[0]:
                            query_ts_user[sid] = (ts_e, u)

        for dt in dates:
            raw_list = await r.lrange(config.daily_log_key(dt), 0, -1)
            for raw in raw_list:
                scanned += 1
                try:
                    e = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                cat = e.get("category", "")
                u = e.get("username", "")
                content = e.get("content", "")
                ts = e.get("timestamp", 0)
                sid = e.get("session_id", "")
                # 1) 回填 query: category=chat 且 content 含 "提问:" (过滤掉 agent 回复的"问答:")
                if cat == "chat" and "提问:" in content:
                    sig = (sid, ts, "query")
                    if sig in seen_keys:
                        skipped_duplicates += 1
                        continue
                    seen_keys.add(sig)
                    # 检查 user_ops 是否已存在 (避免与 chat.py 在线写入重复)
                    existing = await r.lrange(config.user_ops_key(dt), 0, -1)
                    dup = False
                    for ex in existing[-200:]:  # 仅检查尾部 200 条 (性能与近期唯一性平衡)
                        try:
                            if json.loads(ex).get("timestamp") == ts and json.loads(ex).get("action") == "query" and json.loads(ex).get("session_id") == sid:
                                dup = True
                                break
                        except Exception:
                            continue
                    if dup:
                        skipped_duplicates += 1
                        continue
                    entry = json.dumps({
                        "username": u, "action": "query",
                        "detail": f"提问 (历史回填): {content[:120]}",
                        "ip": "", "session_id": sid, "timestamp": ts,
                    }, ensure_ascii=False)
                    await r.rpush(config.user_ops_key(dt), entry)
                    queries_added += 1
                # 2) 回填 user_llm: category=llm (两种格式都支持)
                elif cat == "llm":
                    in_tok, out_tok = 0, 0
                    if "tokens=" in content:
                        m = _re.search(r"tokens=(\d+)\s*\+\s*(\d+)", content)
                        if m:
                            in_tok, out_tok = int(m.group(1)), int(m.group(2))
                    elif "→" in content:
                        # _call_openai_compatible 格式: "(lat, N→M)" — 找最后一个 N→M 数字对
                        ms = _re.findall(r"(\d+)\s*→\s*(\d+)", content)
                        if ms:
                            in_tok, out_tok = int(ms[-1][0]), int(ms[-1][1])
                    if in_tok == 0 and out_tok == 0:
                        continue
                    sig = (sid, ts, "llm")
                    if sig in seen_keys:
                        skipped_duplicates += 1
                        continue
                    seen_keys.add(sig)
                    # username 兜底策略 (按可靠性递减):
                    #   a) 直接日志 username 字段 (最可靠)
                    #   b) session_id 反查 user_ops 中同会话的 query 用户 (可靠)
                    #   c) 时间窗就近归因 (兜底, 可能误归, 但避免数据全丢)
                    if not u or config.is_audit_excluded(u):
                        if sid and sid in session_user_map:
                            u = session_user_map[sid]
                            llm_attributed += 1
                        else:
                            # 时间窗就近归因: 找 ±300 秒内最近的 query
                            closest_user = None
                            closest_gap = 301
                            for q_sid, (q_ts, q_user) in query_ts_user.items():
                                gap = abs(ts - q_ts)
                                if gap < closest_gap and not config.is_audit_excluded(q_user):
                                    closest_gap = gap
                                    closest_user = q_user
                            if closest_user:
                                u = closest_user
                                llm_attributed += 1
                            else:
                                continue  # 兜底失败 → 跳过, 不污染统计
                    if config.is_audit_excluded(u):
                        continue
                    llm_key = config.user_llm_key(dt)
                    await r.hincrby(llm_key, f"{u}:calls", 1)
                    await r.hincrby(llm_key, f"{u}:input_tokens", in_tok)
                    await r.hincrby(llm_key, f"{u}:output_tokens", out_tok)
                    await r.expire(llm_key, max(config.DAILY_LOG_TTL, 90 * 86400))
                    llm_added += 1
        return {
            "scanned": scanned,
            "queries_added": queries_added,
            "llm_added": llm_added,
            "llm_attributed": llm_attributed,
            "skipped_duplicates": skipped_duplicates,
            "dates": dates,
        }
    except Exception as e:
        logger.warning(f"[api] audit/backfill 错误: {e}")
        return {"scanned": 0, "queries_added": 0, "llm_added": 0, "error": str(e)}
