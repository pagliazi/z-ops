#!/usr/bin/env python3
"""门 4 指标记录 — 用生产真实账本统计 (只读, 不写任何状态)

SPEC 上线门 4: 记录任务成功率、P95 延迟、模型 token/请求量、队列/归档延迟及恢复结果,
比较基线; 不承诺未测量的提升比例或工期。

数据源 (全部生产 Redis, 只读):
  zops:tr:task:* / zops:tr:events:* / zops:tr:delivery:* / zops:tr:queue / zops:tr:outbox
  logs/tasks/*.jsonl (永久归档水位)
  zops:executions:{date} (ExecutionRegistry: latency / token / success)

用法: python3 scripts/c17_gate4_metrics.py [--days 7] [--write]
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402

TERMINAL = ("succeeded", "failed", "cancelled", "unknown")


def pct(values, q):
    if not values:
        return None
    vs = sorted(values)
    idx = min(len(vs) - 1, int(round(q / 100 * (len(vs) - 1))))
    return vs[idx]


def fmt(v, suffix="", nd=1):
    return "n/a" if v is None else (f"{v:.{nd}f}{suffix}" if isinstance(v, float) else f"{v}{suffix}")


async def collect(days: int) -> dict:
    import redis.asyncio as aioredis
    from core.task_runtime import archive as task_archive

    r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    out = {"window_days": days, "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    try:
        pfx = f"{config.REDIS_PREFIX}:tr:"
        tasks = {}
        cursor = 0
        while True:
            cursor, batch = await r.scan(cursor=cursor, match=f"{pfx}task:*", count=500)
            for k in batch:
                tid = k.split(":")[-1]
                tasks[tid] = await r.hgetall(k)
            if cursor == 0:
                break

        by_status, by_channel, dur = {}, {}, []
        terminal_ids = []
        for k, t in tasks.items():
            st = t.get("status") or ""
            by_status[st] = by_status.get(st, 0) + 1
            ch = t.get("channel") or "(none)"
            by_channel[ch] = by_channel.get(ch, 0) + 1
            if st in TERMINAL:
                terminal_ids.append(k.split(":")[-1])   # 任务号取自语键 (hash 内无 task_id 字段)
                try:
                    a, b = float(t.get("created_ts") or 0), float(t.get("terminal_ts") or 0)
                    if b > a > 0:
                        dur.append(b - a)
                except (TypeError, ValueError):
                    pass
        done = sum(by_status.get(s, 0) for s in TERMINAL)
        out["tasks"] = {
            "total": len(tasks), "by_status": by_status, "by_channel": by_channel,
            "terminal": done,
            "success_rate_pct": round(by_status.get("succeeded", 0) / done * 100, 1) if done else None,
            "unknown_count": by_status.get("unknown", 0),
            "duration_s": {"p50": pct(dur, 50), "p95": pct(dur, 95), "max": max(dur) if dur else None,
                           "avg": statistics.fmean(dur) if dur else None},
        }

        dlv = {}
        cursor = 0
        while True:
            cursor, batch = await r.scan(cursor=cursor, match=f"{pfx}delivery:*", count=500)
            for k in batch:
                s = await r.get(k) or ""
                dlv[s or "(empty)"] = dlv.get(s or "(empty)", 0) + 1
            if cursor == 0:
                break
        out["delivery"] = dlv

        # 队列 / Outbox / 活跃租约 (Streams 消费组口径: pending 才是积压真值)
        try:
            from core.task_runtime.consumer import GROUP, queue_key
            qk = queue_key()
            try:
                pend = await r.xpending(qk, GROUP)
                pending = int((pend or {}).get("pending") or 0)
                groups = [g.get("name") for g in await r.xinfo_groups(qk)]
            except Exception as ge:
                pending, groups = None, f"消费组未就绪: {ge}"
            out["queue"] = {"stream_len": await r.xlen(qk), "group_pending": pending,
                            "consumer_groups": groups,
                            "active_tasks": await r.zcard(f"{pfx}active"),
                            "outbox_pending": await r.llen(f"{pfx}outbox")}
        except Exception as e:
            out["queue"] = {"error": str(e)}

        # 归档水位: 终态任务事件是否全部落 JSONL (永久保留核对)
        lag_tasks, lag_events, redis_event_total, archived_total = [], 0, 0, 0
        for tid in [t for t in terminal_ids if t][:400]:
            raw = await r.lrange(f"{pfx}events:{tid}", 0, -1)
            ids = set()
            for line in raw:
                try:
                    ids.add(str(json.loads(line).get("event_id") or ""))
                except Exception:
                    continue
            ids.discard("")
            redis_event_total += len(ids)
            arch_ids = task_archive.archived_event_ids(tid)
            archived_total += len(arch_ids)
            miss = len(ids - arch_ids)
            if miss:
                lag_events += miss
                lag_tasks.append(tid)
        out["archive"] = {
            "dir": str(task_archive.TASK_ARCHIVE_DIR),
            "terminal_sampled": min(len(terminal_ids), 400),
            "events_in_redis": redis_event_total, "events_archived": archived_total,
            "events_not_archived": lag_events, "tasks_with_lag": len(lag_tasks),
            "lag_sample": lag_tasks[:3],
        }

        # 模型调用 / token / 延迟
        #   LLM 真值: zops:llm_usage (最近 1000 条, 带 timestamp) + zops:llm_usage:total (累计不截断)
        #   执行明细: zops:executions:{date} 按 exec_type 分段 (混算会被后台长任务拉偏)
        from core.execution_registry import get_execution_registry
        reg = get_execution_registry()
        recs, per_day = [], {}
        today = dt.date.today()
        for i in range(days):
            d = (today - dt.timedelta(days=i)).strftime("%Y-%m-%d")
            rows = await reg.get_daily_executions(d, limit=5000)
            per_day[d] = len(rows)
            recs.extend(rows)
        by_type = {}
        for x in recs:
            et = x.get("exec_type") or x.get("tool_name") or "(none)"
            g = by_type.setdefault(et, {"n": 0, "ok": 0, "fail": 0, "lat": []})
            g["n"] += 1
            g["ok"] += 1 if x.get("success") in (True, "True") else 0
            g["fail"] += 1 if x.get("success") in (False, "False") else 0
            try:
                g["lat"].append(float(x.get("latency_ms") or 0))
            except (TypeError, ValueError):
                pass
        exec_summary = {}
        for et, g in by_type.items():
            exec_summary[et] = {"calls": g["n"], "success": g["ok"], "failure": g["fail"],
                                "p50_ms": pct(g["lat"], 50), "p95_ms": pct(g["lat"], 95)}
        succ = sum(g["ok"] for g in by_type.values())
        fail = sum(g["fail"] for g in by_type.values())

        cutoff = dt.datetime.now().timestamp() - days * 86400
        usage = []
        for raw in await r.lrange(config.KEY_LLM_USAGE, -1000, -1):
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            try:
                if float(e.get("timestamp") or 0) >= cutoff:
                    usage.append(e)
            except (TypeError, ValueError):
                continue
        elapsed = [float(x.get("elapsed") or 0) for x in usage if x.get("elapsed")]
        models = {}
        for x in usage:
            models[x.get("model") or "(none)"] = models.get(x.get("model") or "(none)", 0) + 1
        total_ctr = await r.get(config.KEY_LLM_USAGE_TOTAL)   # string 计数器 (incr, 不截断)
        out["llm"] = {
            "calls": len(usage),
            "input_tokens": sum(int(x.get("input_tokens") or 0) for x in usage),
            "output_tokens": sum(int(x.get("output_tokens") or 0) for x in usage),
            "estimated_flagged": sum(1 for x in usage if x.get("tokens_estimated")),
            "elapsed_s": {"p50": pct(elapsed, 50), "p95": pct(elapsed, 95),
                          "max": max(elapsed) if elapsed else None},
            "by_model": models,
            "lifetime_calls": total_ctr,
            "lifetime_counter_note": "zops:llm_usage:total 仅计数调用次数, 不累计 token",
            "usage_window_note": "zops:llm_usage 仅保留最近 1000 条 (清单口径), 故窗口 token 为可见清单内值; 调用次数全量真值取 :total 计数器",
        }
        out["executions"] = {
            "records": len(recs), "per_day": per_day,
            "success": succ, "failure": fail,
            "success_rate_pct": round(succ / max(succ + fail, 1) * 100, 1) if (succ + fail) else None,
            "by_type": exec_summary,
            "note": "executions 列表按日上限裁剪, 数值为可见窗口内事实 (非全量真值)",
        }

        info = await r.info("persistence")
        out["recovery"] = {
            "aof_enabled": int(info.get("aof_enabled") or 0),
            "aof_last_bgrewrite_status": info.get("aof_last_bgrewrite_status"),
            "rdb_last_bgsave_status": info.get("rdb_last_bgsave_status"),
            "loading": int(info.get("loading") or 0),
            "redis_version": (await r.info("server")).get("redis_version"),
        }
    finally:
        await r.aclose()
    return out


def render(o: dict) -> str:
    t, e, a, q, m = o["tasks"], o["executions"], o["archive"], o["queue"], o["llm"]
    L = [f"## 窗口 {o['window_days']} 天 · 生成 {o['generated_at']}", ""]
    L.append("| 门 4 指标 | 实测值 | 口径与说明 |")
    L.append("|---|---|---|")
    L.append(f"| 任务协议覆盖 | {t['total']} 个任务 (终态 {t['terminal']}) | 渠道分布 {t['by_channel']} |")
    L.append(f"| 任务成功率 | {fmt(t['success_rate_pct'], '%')} | succeeded/终态; UNKNOWN={t['unknown_count']} 个 (写崩溃人工核查, 不计成功也不计失败重试) |")
    L.append(f"| 任务端到端延迟 | P50 {fmt(t['duration_s']['p50'], 's')} / P95 {fmt(t['duration_s']['p95'], 's')} / avg {fmt(t['duration_s']['avg'], 's')} / max {fmt(t['duration_s']['max'], 's')} | created_ts→terminal_ts |")
    L.append(f"| 执行明细量 | {e['records']} 条 (按日 {e['per_day']}) | 成功 {e['success']} / 失败 {e['failure']} → {fmt(e['success_rate_pct'], '%')} |")
    for et, v in sorted((e["by_type"] or {}).items(), key=lambda x: -x[1]["calls"])[:6]:
        L.append(f"| 执行分项 · {et} | {v['calls']} 次 (ok {v['success']} / fail {v['failure']}) | P50 {fmt(v['p50_ms'], 'ms')} / P95 {fmt(v['p95_ms'], 'ms')} |")
    L.append(f"| 模型调用量 | {m['calls']} 次 | 模型分布 {m['by_model']} (窗口内清单口径) |")
    L.append(f"| Token 用量 | in {m['input_tokens']:,} / out {m['output_tokens']:,} | 估算标记 {m['estimated_flagged']} 条; 窗口内累计调用 {m['lifetime_calls']} 次 (全局计数器) |")
    L.append(f"| 单次模型耗时 | P50 {fmt(m['elapsed_s']['p50'], 's')} / P95 {fmt(m['elapsed_s']['p95'], 's')} / max {fmt(m['elapsed_s']['max'], 's')} | llm_usage.elapsed |")
    L.append(f"| 队列与投递 | stream_len={q.get('stream_len')} group_pending={q.get('group_pending')} active={q.get('active_tasks')} outbox_pending={q.get('outbox_pending')} | 消费组 {q.get('consumer_groups')}; delivery 状态 {o['delivery']} |")
    L.append(f"| 归档水位 | Redis 事件 {a['events_in_redis']:,} / 已归档 {a['events_archived']:,} | 滞后 {a['events_not_archived']} 条 (涉及 {a['tasks_with_lag']} 个任务), 核对 {a['terminal_sampled']} 个终态任务 |")
    rec = o["recovery"]
    L.append(f"| 恢复能力 | redis {rec['redis_version']} / AOF={rec['aof_enabled']} / aof_bgrewrite={rec['aof_last_bgrewrite_status']} / rdb_bgsave={rec['rdb_last_bgsave_status']} | loading={rec['loading']} (重启恢复中=1) |")
    L.append("")
    L.append(f"- 归档目录: `{a['dir']}`")
    L.append(f"- 口径提示: {e['note']}; {m['usage_window_note']}。")
    L.append("- 本轮为协议上线后首个可测窗口, 无同口径历史值 → 只记录当前值作为后续对比基线, 不推算提升比例或工期。")
    return "\n".join(L)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--write", action="store_true", help="追加写入 Z-OPS-GATE4-METRICS-c17.md")
    args = ap.parse_args()
    o = await collect(args.days)
    text = render(o)
    print(text)
    if args.write:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "Z-OPS-GATE4-METRICS-c17.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n\n")
        print(f"[written] {path}", file=sys.stderr)
    print(json.dumps(o, ensure_ascii=False, default=str), file=sys.stderr)


if __name__ == "__main__":
    asyncio.new_event_loop().run_until_complete(main())
