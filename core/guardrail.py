"""护栏信号 (guardrail signals) — 优化闭环的质量回归探测

背景: 优化台账原先只有延迟/错误核算 (只测提速、不测伤损)，曾发生
"跳过 LLM 查询改写提速 16s 但 KB 召回显著变差" 的事故且台账无感知。
本模块为落地后的优化提供副作用指标的自动采集与前后对比核算。

埋点 (agents 侧, 永不抛异常、不影响主流程):
    await bump_guardrail("kb_agent", "kb_hybrid_miss")

存储 (按日分桶, 支持"落地前基线 vs 落地后"窗口对比):
    {REDIS_PREFIX}:guardrail:YYYY-MM-DD  (hash)  field: {agent}:{signal} → count
    TTL 60 天

核算 (obs_analyzer 调用):
    summaries = await guardrail_summaries({"kb_agent": landed_ts})
    → {"kb_agent": {"signals": {...}, "regressions": ["kb_hybrid_miss"], ...}}
    恶化幅度 ≥ REGRESSION_PP 个百分点且样本足够 → 列入 regressions,
    evaluate_optimization 据此给出 verdict=regression (看板标红)。
"""

import time
import logging
from datetime import datetime, timedelta

import config

logger = logging.getLogger("zops")

GUARDRAIL_TTL = 60 * 86400   # 日桶保留 60 天
BASELINE_DAYS = 7            # 落地前基线窗口 (天)
MIN_SAMPLE = 20              # 前后样本均需达到才做回归判定
REGRESSION_PP = 15.0         # 恶化 ≥15 个百分点判为疑似回归
MAX_SCAN_DAYS = 90           # 核算最多回看 90 个日桶

# 信号语义: bad="up" 该比率上升是坏事; bad="down" 下降是坏事
SIGNAL_META = {
    "kb_hybrid_hit":         {"bad": "down", "label": "语义检索命中"},
    "kb_hybrid_miss":        {"bad": "up",   "label": "语义检索未命中"},
    "kb_variant_fallback":   {"bad": "up",   "label": "变体兜底触发"},
    "kb_final_no_hit":       {"bad": "up",   "label": "全路径无命中"},
}

# 互斥信号对: 两者之和 = 查询总数, 作为比率的分母
PAIR_DENOMINATOR = {
    "kb_agent": ("kb_hybrid_hit", "kb_hybrid_miss"),
}

_r = None


async def _get_redis():
    global _r
    if _r is None:
        import redis.asyncio as aioredis
        _r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    return _r


def _day_key(ts: float) -> str:
    day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    return f"{config.REDIS_PREFIX}:guardrail:{day}"


async def bump_guardrail(agent_id: str, signal: str, count: int = 1) -> None:
    """护栏埋点计数 — 永不抛异常, 不影响业务主流程"""
    try:
        r = await _get_redis()
        key = _day_key(time.time())
        await r.hincrby(key, f"{agent_id}:{signal}", count)
        await r.expire(key, GUARDRAIL_TTL)
    except Exception as e:
        logger.debug(f"[guardrail] 埋点失败 {agent_id}:{signal}: {e}")


def _rates(before: dict, after: dict, agent_id: str) -> dict:
    """计算每个信号的 基线/落地后 比率。分母优先取互斥对之和, 无对时取总查询数。"""
    pair = PAIR_DENOMINATOR.get(agent_id)
    den_b = den_a = 0
    if pair:
        den_b = before.get(pair[0], 0) + before.get(pair[1], 0)
        den_a = after.get(pair[0], 0) + after.get(pair[1], 0)
    if not den_b:
        den_b = sum(before.values()) or -1   # -1 = 无基线数据
    if not den_a:
        den_a = sum(after.values()) or -1

    signals = {}
    for sig in set(before) | set(after):
        meta = SIGNAL_META.get(sig, {"bad": "up", "label": sig})
        nb, na = before.get(sig, 0), after.get(sig, 0)
        pb = round(100.0 * nb / den_b, 1) if den_b > 0 else None
        pa = round(100.0 * na / den_a, 1) if den_a > 0 else None
        signals[sig] = {
            "label": meta["label"],
            "bad": meta["bad"],
            "before_n": nb, "after_n": na,
            "before_pct": pb, "after_pct": pa,
            "delta_pp": round(pa - pb, 1) if (pb is not None and pa is not None) else None,
            "den_before": max(den_b, 0), "den_after": max(den_a, 0),
        }
    return signals


async def guardrail_summaries(landed_map: dict) -> dict:
    """对每个 agent 做 落地前(BASELINE_DAYS 天) vs 落地后 的护栏比率对比。

    landed_map: {agent_id: landed_ts}; landed_ts ≤ 0 视为未落地 (跳过)。
    返回 {agent: {"signals": {...}, "regressions": [signal...],
                  "sample_ok": bool, "baseline_days": int}}
    """
    if not landed_map:
        return {}
    out = {}
    try:
        r = await _get_redis()
    except Exception as e:
        logger.debug(f"[guardrail] Redis 不可用: {e}")
        return {}

    now = time.time()
    for agent_id, landed_ts in landed_map.items():
        landed_ts = float(landed_ts or 0)
        if landed_ts <= 0:
            continue
        start = max(landed_ts - BASELINE_DAYS * 86400, now - MAX_SCAN_DAYS * 86400)
        before, after = {}, {}
        try:
            keys = []
            day = datetime.fromtimestamp(start)
            while day <= datetime.fromtimestamp(now):
                keys.append((day.strftime("%Y-%m-%d"), day.timestamp() + 43200))
                day += timedelta(days=1)
            pipe = r.pipeline()
            for _, mid in keys:
                pipe.hgetall(_day_key(mid))
            rows = await pipe.execute()
            for (day_str, mid), row in zip(keys, rows):
                if not row:
                    continue
                bucket = before if mid < landed_ts else after
                for field, val in row.items():
                    if not field.startswith(f"{agent_id}:"):
                        continue
                    sig = field.split(":", 1)[1]
                    bucket[sig] = bucket.get(sig, 0) + int(val or 0)
        except Exception as e:
            logger.debug(f"[guardrail] 核算失败 {agent_id}: {e}")
            continue

        signals = _rates(before, after, agent_id)
        den_b_ok = any(s["den_before"] >= MIN_SAMPLE for s in signals.values())
        den_a_ok = any(s["den_after"] >= MIN_SAMPLE for s in signals.values())
        regressions = []
        if den_b_ok and den_a_ok:
            for sig, s in signals.items():
                d = s["delta_pp"]
                if d is None or s["after_n"] < MIN_SAMPLE:
                    continue
                if (s["bad"] == "up" and d >= REGRESSION_PP) or \
                   (s["bad"] == "down" and d <= -REGRESSION_PP):
                    regressions.append(sig)
        out[agent_id] = {
            "signals": signals,
            "regressions": regressions,
            "sample_ok": bool(den_b_ok and den_a_ok),
            "baseline_days": BASELINE_DAYS,
        }
    return out
