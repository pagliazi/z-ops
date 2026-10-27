"""通用告警闭环引擎 — 单元聚合 / 人工确认 / 恢复核验 / 案例库

单元口径：`source × 归一类`（复用 core.alert_closure.classify），一台设备的同一类
告警合成一个「故障单元」，不再以 alarm_id/event_key 为闭环单位（2236 个单元 vs 3.8 万条告警）。

恢复判据（缺一不可，任一未知一律保持等待）：
- L1（备份/DNS/TCP/NTP）：探针连续健康次数达标 + 相邻跨度达标 + 最近健康证据新鲜 + NMS 清除；
- L2（设备级簇）：单元活跃告警清零并连续静默 NMS_CLOSURE_QUIET_S；
- 任意层级：维护窗口内不判定恢复；空快照/采集异常不推动清除；确认后超跟踪窗口 → expired。

硬约束：不自动 ACK / 屏蔽 / 改配置 / 调 LLM；探针只读；所有状态变更留事件与操作人。
"""

import asyncio
import json
import logging
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
from core.alert_closure import SEVERITY_WEIGHTS, classify
from core import alert_probes

logger = logging.getLogger("zops")

RULE_VERSION = "closure-v1"
UNIT_STATUSES = ("observed", "confirmed", "waiting_clear", "recovered", "paused", "expired")
TRACKED_STATUSES = {"confirmed", "waiting_clear"}
CLOSED_STATUSES = {"recovered", "expired"}
PROBE_KINDS = set(alert_probes.PROBE_KINDS)
_UNIT_STATS = ("alarm_count", "weighted", "severities", "event_keys", "event_groups",
               "sample_summaries", "first_seen", "first_seen_ts", "last_seen",
               "acked", "targets", "label", "tier", "probe_kind", "handoff")


def _k(name: str) -> str:
    return f"{config.REDIS_PREFIX}:alert_closure:{name}"


KEY_UNITS = _k("units")
KEY_CASES = _k("cases")
KEY_EVENTS = _k("events")
KEY_METRICS = _k("metrics")
KEY_PROBE_LOCK = _k("probe_lock")


class ClosureError(Exception):
    """带 HTTP 语义的业务错误（API 层统一转 code/message/retryable）。"""

    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


# ═══════════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════════

def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(),
                                  tz=timezone.utc).isoformat(timespec="seconds")


def _stamp(unit: Dict[str, Any], name: str, ts: Optional[float] = None) -> float:
    """同时写入展示用 ISO 与计算用 epoch，避免两套时间字段各自漂移。"""
    ts = _now() if ts is None else ts
    unit[name] = _iso(ts)
    unit[f"{name}_ts"] = ts
    return ts


def _norm_source(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text or "unknown"


def unit_key(kind: str, source: Any) -> str:
    return f"{kind}:{_norm_source(source)}"


def _parse_alarm_time(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%y, %I:%M %p", "%m/%d/%Y, %I:%M %p",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def _event_key(alarm: Dict[str, Any]) -> str:
    raw = (alarm.get("event_key") or "").strip()
    if raw:
        return raw
    group = alarm.get("event_group") or alarm.get("alarm_type") or "event"
    return f"{group}|{(alarm.get('summary') or '')[:80]}"


def _severity_rank(sev: str) -> int:
    return SEVERITY_WEIGHTS.get((sev or "").lower(), 0)


# ═══════════════════════════════════════════════════════════════════════════
# 单元聚合（纯函数）
# ═══════════════════════════════════════════════════════════════════════════

def build_units(alarms: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把活跃告警聚合成 `source × kind` 单元；只输出统计字段，不含状态机字段。"""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for alarm in alarms or []:
        sev = (alarm.get("severity") or "").lower()
        if sev not in SEVERITY_WEIGHTS:
            continue
        source = alarm.get("source") or alarm.get("node") or alarm.get("node_alias")
        kind = classify(alarm)["kind"]
        groups[unit_key(kind, source)].append(alarm)

    units: Dict[str, Dict[str, Any]] = {}
    for key, items in groups.items():
        spec = classify(items[0])
        severities = Counter((a.get("severity") or "").lower() for a in items)
        event_keys = Counter(_event_key(a) for a in items)
        event_groups = Counter(a.get("event_group") or a.get("alarm_type") or "" for a in items)
        first_ts = [_parse_alarm_time(a.get("first_time")) for a in items]
        first_ts = [t for t in first_ts if t]
        samples = [a.get("summary") or "" for a in sorted(
            items, key=lambda x: -_severity_rank(x.get("severity")))[:3]]
        source = items[0].get("source") or items[0].get("node") or items[0].get("node_alias")
        targets = alert_probes.extract_targets(spec["kind"], items) \
            if spec["kind"] in PROBE_KINDS else []
        units[key] = {
            "key": key,
            "kind": spec["kind"],
            "label": spec["label"],
            "tier": spec["tier"],
            "source": _norm_source(source),
            "handoff": "nms_ssl_workflow" if spec["kind"] == "certificate" else "",
            "probe_kind": spec["kind"] if spec["kind"] in PROBE_KINDS and targets else "",
            "targets": targets,
            "alarm_count": len(items),
            "weighted": sum(_severity_rank(a.get("severity")) for a in items),
            "severities": dict(severities),
            "event_keys": [k for k, _ in event_keys.most_common(10)],
            "event_groups": [g for g, _ in event_groups.most_common(6) if g],
            "sample_summaries": samples,
            "acked": sum(1 for a in items if str(a.get("acknowledged") or "0") != "0"),
            "first_seen": _iso(min(first_ts)) if first_ts else "",
            "first_seen_ts": min(first_ts) if first_ts else None,
            "last_seen": str(items[0].get("last_time") or ""),
        }
    return units


# ═══════════════════════════════════════════════════════════════════════════
# 状态机（纯函数）
# ═══════════════════════════════════════════════════════════════════════════

def recovery_check(unit: Dict[str, Any], now: float) -> tuple:
    """返回 (是否可恢复, 原因)。任何未知/证据缺失都返回 False。"""
    clear_ts = unit.get("clear_since_ts")
    if not clear_ts:
        return False, "no_clear_evidence"
    probe_kind = unit.get("probe_kind") or ""
    if probe_kind in PROBE_KINDS:
        need = max(1, config.NMS_CLOSURE_L1_HEALTHY)
        streak = unit.get("probe_ok_streak") or 0
        # 连续无法取证（如该主机无备份作业数据）：回退簇级判据，并在证据里标注
        if (unit.get("probe_unknown_streak") or 0) >= config.NMS_CLOSURE_PROBE_UNKNOWN_LIMIT:
            quiet = max(0, config.NMS_CLOSURE_QUIET_S)
            if now - clear_ts < quiet:
                return False, f"probe_unavailable+quiet={int(now - clear_ts)}s/{quiet}s"
            return True, (f"probe_unavailable={unit['probe_unknown_streak']}"
                          f"+cluster_clear={int(now - clear_ts)}s")
        if streak < need:
            return False, f"probe_streak={streak}/{need}"
        spacing = max(0, config.NMS_CLOSURE_L1_MIN_SPACING_S) * (need - 1)
        span = now - (unit.get("probe_first_ok_at_ts") or now)
        if span < spacing:
            return False, f"probe_span={int(span)}s<{spacing}s"
        fresh_limit = max(2 * config.NMS_CLOSURE_PROBE_INTERVAL, 300)
        if now - (unit.get("probe_last_ok_at_ts") or 0) > fresh_limit:
            return False, "probe_stale"
        return True, f"probe_healthy={streak}+nms_clear={int(now - clear_ts)}s"
    quiet = max(0, config.NMS_CLOSURE_QUIET_S)
    if now - clear_ts < quiet:
        return False, f"quiet={int(now - clear_ts)}s/{quiet}s"
    return True, f"cluster_clear={int(now - clear_ts)}s"


def apply_tick(unit: Dict[str, Any], active_count: int, now: float,
               maint_until: float = 0.0, empty_snapshot: bool = False) -> List[str]:
    """推进单个单元（原地修改）；返回发生的状态事件名。"""
    events: List[str] = []
    unit["active_count"] = active_count
    _stamp(unit, "last_tick_at", now)
    if unit.get("status") not in TRACKED_STATUSES:
        return events
    if empty_snapshot:
        # 采集异常/空快照不推动清除：把"本轮为空"与"真的清除"区分开
        unit["clear_since"] = None
        unit["clear_since_ts"] = None
        return events
    if maint_until and now < maint_until:
        unit["in_maintenance"] = True
        unit["clear_since"] = None
        unit["clear_since_ts"] = None
        unit["maintenance_until"] = _iso(maint_until)
        return events
    unit["in_maintenance"] = False
    unit.pop("maintenance_until", None)
    if active_count > 0:
        if unit.get("clear_since_ts"):
            events.append("clear_reset")
        unit["clear_since"] = None
        unit["clear_since_ts"] = None
        if unit.get("status") == "waiting_clear":
            unit["status"] = "confirmed"
            events.append("back_to_active")
    elif not unit.get("clear_since_ts"):
        _stamp(unit, "clear_since", now)
        unit["status"] = "waiting_clear"
        events.append("clear_started")

    confirmed_ts = unit.get("confirmed_at_ts")
    window = max(0, config.NMS_CLOSURE_TRACK_WINDOW_S)
    if confirmed_ts and window and now - confirmed_ts > window:
        unit["status"] = "expired"
        unit["expire_reason"] = "track_window_exceeded"
        _stamp(unit, "expired_at", now)
        events.append("expired")
        return events

    ok, reason = recovery_check(unit, now)
    if ok:
        unit["status"] = "recovered"
        unit["recovery_evidence"] = reason
        _stamp(unit, "recovered_at", now)
        events.append("recovered")
    return events


# ═══════════════════════════════════════════════════════════════════════════
# Redis 存储
# ═══════════════════════════════════════════════════════════════════════════

_redis = None


async def get_redis():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(config.REDIS_URL, decode_responses=True,
                                   max_connections=4)
    return _redis


def set_redis(client) -> None:
    """测试注入（生产不经此路径）。"""
    global _redis
    _redis = client


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None


async def _load_units(r=None) -> Dict[str, Dict[str, Any]]:
    r = r or await get_redis()
    raw = await r.hgetall(KEY_UNITS)
    units: Dict[str, Dict[str, Any]] = {}
    for key, value in (raw or {}).items():
        try:
            unit = json.loads(value)
        except Exception:
            continue
        if isinstance(unit, dict) and unit.get("key"):
            units[key] = unit
    return units


async def _save_units(units: List[Dict[str, Any]], r=None) -> None:
    if not units:
        return
    r = r or await get_redis()
    pipe = r.pipeline()
    for unit in units:
        pipe.hset(KEY_UNITS, unit["key"], json.dumps(unit, ensure_ascii=False))
    await pipe.execute()


async def _save_unit(unit: Dict[str, Any]) -> None:
    r = await get_redis()
    await r.hset(KEY_UNITS, unit["key"], json.dumps(unit, ensure_ascii=False))


async def _load_maint() -> Dict[str, float]:
    try:
        r = await get_redis()
        raw = await r.hgetall(config.KEY_NMS_MAINT_WINDOWS)
    except Exception:
        return {}
    out: Dict[str, float] = {}
    for source, until in (raw or {}).items():
        try:
            out[_norm_source(source)] = float(until)
        except (TypeError, ValueError):
            continue
    return out


async def _event(unit_key: str, name: str, detail: str = "") -> None:
    try:
        r = await get_redis()
        payload = json.dumps({"at": _iso(), "unit": unit_key, "event": name,
                              "detail": detail}, ensure_ascii=False)
        await r.rpush(KEY_EVENTS, payload)
        await r.ltrim(KEY_EVENTS, -1000, -1)
    except Exception as e:
        logger.debug(f"[alert_closure] 事件写入失败: {e}")


async def _bump(field: str, amount: int = 1) -> None:
    try:
        r = await get_redis()
        await r.hincrby(KEY_METRICS, field, amount)
    except Exception:
        pass


async def _notify(title: str, content: str, topic: str = "nms_closure") -> None:
    """飞书卡片通知（经 notify:feishu 频道）；失败只降级日志，不影响闭环。"""
    try:
        r = await get_redis()
        await r.publish(config.channel("notify:feishu"), json.dumps({
            "text": f"{title}\n{content}", "topic": topic,
            "source": "alert_closure", "extra": {"card_type": "info"},
        }, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"[alert_closure] 通知失败: {e}")


async def _cap_units(units: Dict[str, Dict[str, Any]], r=None) -> List[str]:
    """超上限时优先丢弃低权重 observed 单元；已确认/已恢复单元始终保留。"""
    limit = max(100, config.NMS_CLOSURE_MAX_UNITS)
    if len(units) <= limit:
        return []
    tracked = [u for u in units.values() if u.get("status") in TRACKED_STATUSES]
    others = [u for u in units.values() if u.get("status") not in TRACKED_STATUSES]
    others.sort(key=lambda u: -(u.get("weighted") or 0))
    keep = {u["key"] for u in tracked} | {u["key"] for u in others[:max(0, limit - len(tracked))]}
    dropped = [k for k in units if k not in keep]
    if dropped:
        try:
            r = r or await get_redis()
            await r.hdel(KEY_UNITS, *dropped)
        except Exception as e:
            logger.debug(f"[alert_closure] 清理超限单元失败: {e}")
    return dropped


# ═══════════════════════════════════════════════════════════════════════════
# 刷新（采集快照 → 单元）
# ═══════════════════════════════════════════════════════════════════════════

async def refresh_units(snapshot: Optional[Dict[str, Any]] = None,
                        alarms: Optional[List[Dict[str, Any]]] = None,
                        now: Optional[float] = None,
                        maint: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    now = _now() if now is None else now
    if alarms is None:
        alarms = (snapshot or {}).get("alarms") or []
    empty = not alarms
    current = build_units(alarms)
    stored = await _load_units()
    maint = await _load_maint() if maint is None else maint

    changed: List[Dict[str, Any]] = []
    recovered, expired = [], []
    for key, fresh in current.items():
        prev = stored.get(key)
        if prev:
            for field in _UNIT_STATS:
                if field in fresh:
                    prev[field] = fresh[field]
            unit = prev
        else:
            unit = dict(fresh)
            unit["status"] = "observed"
            _stamp(unit, "created_at", now)
        _stamp(unit, "last_observed_at", now)
        events = apply_tick(unit, fresh["alarm_count"], now,
                            float(maint.get(unit["source"]) or 0), empty)
        changed.append(unit)
        if "recovered" in events:
            recovered.append(key)
        if "expired" in events:
            expired.append(key)

    dropped = 0
    for key, unit in stored.items():
        if key in current:
            continue
        if unit.get("status") in TRACKED_STATUSES:
            events = apply_tick(unit, 0, now,
                                float(maint.get(unit["source"]) or 0), empty)
            changed.append(unit)
            if "recovered" in events:
                recovered.append(key)
            if "expired" in events:
                expired.append(key)
        elif unit.get("status") == "observed":
            last = unit.get("last_observed_at_ts") or 0
            if now - last > max(60, config.NMS_CLOSURE_FORGET_S):
                dropped += 1
                try:
                    await (await get_redis()).hdel(KEY_UNITS, key)
                except Exception:
                    pass

    await _save_units(changed)
    merged = {u["key"]: u for u in changed}
    for key, unit in stored.items():
        if key not in merged and unit.get("status") in TRACKED_STATUSES:
            merged[key] = unit
    capped = await _cap_units(merged) if len(merged) > config.NMS_CLOSURE_MAX_UNITS else []

    by_status = Counter(u.get("status") for u in merged.values())
    for key in recovered:
        unit = merged.get(key) or {}
        await _event(key, "recovered", unit.get("recovery_evidence") or "")
        await _bump("recovered_total")
        await _notify(
            f"告警闭环已恢复｜{unit.get('label') or ''} {unit.get('source') or ''}",
            f"单元 {key}（{unit.get('alarm_count') or 0} 条告警）已满足恢复判据："
            f"{unit.get('recovery_evidence') or ''}")
    for key in expired:
        unit = merged.get(key) or {}
        await _event(key, "expired", unit.get("expire_reason") or "")
        await _bump("expired_total")

    return {
        "observed_at": _iso(now),
        "alarms": len(alarms),
        "empty_snapshot": empty,
        "units_total": len(merged),
        "by_status": dict(by_status),
        "new_units": sum(1 for k in current if k not in stored),
        "recovered": recovered,
        "expired": expired,
        "dropped": dropped + len(capped),
        "maintenance_sources": sum(1 for s, until in maint.items() if until > now),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 人工动作
# ═══════════════════════════════════════════════════════════════════════════

async def get_unit(key: str) -> Dict[str, Any]:
    unit = (await _load_units()).get(key)
    if not unit:
        raise ClosureError(404, "unit_not_found", f"单元不存在: {key}")
    return unit


async def confirm_unit(key: str, operator: str, root_cause: str = "",
                       note: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    now = _now() if now is None else now
    unit = await get_unit(key)
    if unit.get("status") in CLOSED_STATUSES:
        raise ClosureError(409, "unit_closed",
                           f"单元已{'恢复' if unit['status'] == 'recovered' else '过期'}，如需继续跟踪请先重新打开")
    if unit.get("status") in TRACKED_STATUSES and unit.get("root_cause") == root_cause:
        return unit
    unit["status"] = "confirmed"
    unit["confirmed_by"] = operator
    _stamp(unit, "confirmed_at", now)
    unit["root_cause"] = (root_cause or "").strip()[:200]
    unit["note"] = (note or "").strip()[:500]
    unit["clear_since"] = None
    unit["clear_since_ts"] = None
    await _save_unit(unit)
    await _event(key, "confirmed", f"{operator}: {unit['root_cause'] or '已确认'}")
    await _bump("confirmed_total")
    await _notify(f"告警闭环已确认｜{unit.get('label') or ''} {unit.get('source') or ''}",
                  f"单元 {key}｜{unit.get('alarm_count') or 0} 条告警｜处理人 {operator}\n"
                  f"根因：{unit['root_cause'] or '未填写'}；等待自动恢复核验。")
    if unit.get("probe_kind") in PROBE_KINDS:
        asyncio.create_task(_safe_probe(key))
    return unit


async def reopen_unit(key: str, operator: str, note: str = "",
                      now: Optional[float] = None) -> Dict[str, Any]:
    """已恢复/已过期单元重新打开跟踪（需要新的处置依据）。"""
    now = _now() if now is None else now
    unit = await get_unit(key)
    if unit.get("status") not in CLOSED_STATUSES:
        raise ClosureError(409, "unit_open", "单元仍在跟踪中，无需重新打开")
    unit["status"] = "confirmed"
    unit["confirmed_by"] = operator
    _stamp(unit, "confirmed_at", now)
    unit["note"] = (note or "").strip()[:500]
    unit.pop("recovery_evidence", None)
    unit.pop("expire_reason", None)
    unit["clear_since"] = None
    unit["clear_since_ts"] = None
    unit["probe_ok_streak"] = 0
    await _save_unit(unit)
    await _event(key, "reopened", f"{operator}: {note or ''}")
    return unit


async def pause_unit(key: str, operator: str, note: str = "") -> Dict[str, Any]:
    unit = await get_unit(key)
    if unit.get("status") in CLOSED_STATUSES:
        raise ClosureError(409, "unit_closed", "已关闭的单元不能暂停")
    unit["status"] = "paused"
    unit["paused_by"] = operator
    unit["note"] = (note or "").strip()[:500]
    _stamp(unit, "paused_at")
    await _save_unit(unit)
    await _event(key, "paused", f"{operator}: {note or ''}")
    return unit


async def resume_unit(key: str, operator: str) -> Dict[str, Any]:
    unit = await get_unit(key)
    if unit.get("status") != "paused":
        raise ClosureError(409, "unit_not_paused", "仅暂停中的单元可恢复跟踪")
    unit["status"] = "confirmed"
    unit["resumed_by"] = operator
    _stamp(unit, "resumed_at")
    unit["clear_since"] = None
    unit["clear_since_ts"] = None
    await _save_unit(unit)
    await _event(key, "resumed", operator)
    return unit


# ═══════════════════════════════════════════════════════════════════════════
# 探针（L1）
# ═══════════════════════════════════════════════════════════════════════════

async def _probe_unit(unit: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    now = _now() if now is None else now
    kind = unit.get("probe_kind") or ""
    try:
        result = await alert_probes.probe(
            kind, unit.get("targets") or [],
            timeout=max(3, config.NMS_CLOSURE_PROBE_TIMEOUT))
    except Exception as e:
        logger.warning(f"[alert_closure] 探针异常 ({unit.get('key')}): {e}")
        result = {"ok": None, "summary": f"探针异常: {str(e)[:80]}",
                  "results": [], "checked": 0}
    ok = result.get("ok")
    unit.setdefault("probe_ok_streak", 0)
    unit.setdefault("probe_unknown_streak", 0)
    unit["probe_status"] = {True: "ok", False: "fail"}.get(ok, "unknown")
    unit["probe_summary"] = (result.get("summary") or "")[:300]
    unit["probe_results"] = (result.get("results") or [])[:6]
    _stamp(unit, "probe_last_at", now)
    if ok is True:
        if not unit.get("probe_ok_streak"):
            _stamp(unit, "probe_first_ok_at", now)
        unit["probe_ok_streak"] = (unit.get("probe_ok_streak") or 0) + 1
        unit["probe_unknown_streak"] = 0
        _stamp(unit, "probe_last_ok_at", now)
    elif ok is False:
        unit["probe_ok_streak"] = 0
        unit["probe_unknown_streak"] = 0
        unit["probe_first_ok_at"] = None
        unit["probe_first_ok_at_ts"] = None
    else:
        unit["probe_unknown_streak"] = (unit.get("probe_unknown_streak") or 0) + 1
        await _bump("probe_unavailable")
    if unit["probe_status"] in ("ok", "fail"):
        await _bump(f"probe_{unit['probe_status']}")
    return result


async def _safe_probe(key: str) -> None:
    try:
        unit = await get_unit(key)
        if unit.get("probe_kind") not in PROBE_KINDS:
            return
        await _probe_unit(unit)
        maint = await _load_maint()
        events = apply_tick(unit, unit.get("active_count") or 0, _now(),
                            float(maint.get(unit["source"]) or 0))
        await _save_unit(unit)
        if "recovered" in events:
            await _event(key, "recovered", unit.get("recovery_evidence") or "")
            await _bump("recovered_total")
            await _notify(f"告警闭环已恢复｜{unit.get('label') or ''} {unit.get('source') or ''}",
                          f"单元 {key}：{unit.get('recovery_evidence') or ''}")
    except Exception as e:
        logger.warning(f"[alert_closure] 后台探针失败 ({key}): {e}")


async def recheck_unit(key: str, now: Optional[float] = None) -> Dict[str, Any]:
    """按需核验：L1 立即探测，L2 用最近一次快照重算；不做任何写告警动作。"""
    now = _now() if now is None else now
    unit = await get_unit(key)
    if unit.get("status") in CLOSED_STATUSES:
        raise ClosureError(409, "unit_closed", "已关闭的单元无需核验，如需继续请重新打开")
    if unit.get("probe_kind") in PROBE_KINDS:
        await _probe_unit(unit, now)
    maint = await _load_maint()
    events = apply_tick(unit, unit.get("active_count") or 0, now,
                        float(maint.get(unit["source"]) or 0))
    await _save_unit(unit)
    for name in events:
        await _event(key, name, unit.get("recovery_evidence") or unit.get("probe_summary") or "")
    if "recovered" in events:
        await _bump("recovered_total")
        await _notify(f"告警闭环已恢复｜{unit.get('label') or ''} {unit.get('source') or ''}",
                      f"单元 {key}：{unit.get('recovery_evidence') or ''}")
    return unit


# ═══════════════════════════════════════════════════════════════════════════
# 查询与案例
# ═══════════════════════════════════════════════════════════════════════════

async def list_units(status: str = "", tier: str = "", kind: str = "",
                     q: str = "", limit: int = 50) -> Dict[str, Any]:
    units = list((await _load_units()).values())
    if status:
        wanted = {s.strip() for s in status.split(",") if s.strip()}
        units = [u for u in units if u.get("status") in wanted]
    if tier:
        units = [u for u in units if u.get("tier") == tier]
    if kind:
        units = [u for u in units if u.get("kind") == kind]
    if q:
        needle = q.strip().lower()
        units = [u for u in units
                 if needle in (u.get("key") or "") or needle in (u.get("source") or "")]
    units.sort(key=lambda u: (u.get("status") not in TRACKED_STATUSES,
                              -(u.get("weighted") or 0)))
    limit = max(1, min(int(limit or 50), 200))
    return {"total": len(units), "items": units[:limit],
            "statuses": list(UNIT_STATUSES)}


async def get_unit_detail(key: str) -> Dict[str, Any]:
    unit = await get_unit(key)
    events, case = [], None
    try:
        r = await get_redis()
        raw = await r.lrange(KEY_EVENTS, -500, -1)
        for item in reversed(raw or []):
            try:
                ev = json.loads(item)
            except Exception:
                continue
            if ev.get("unit") == key:
                events.append(ev)
                if len(events) >= 50:
                    break
        if unit.get("case_id"):
            payload = await r.hget(KEY_CASES, unit["case_id"])
            case = json.loads(payload) if payload else None
    except Exception as e:
        logger.debug(f"[alert_closure] 读取详情失败: {e}")
    return {"unit": unit, "events": events, "case": case}


async def record_case(key: str, operator: str, resolution: str,
                      prevention: str = "", root_cause: str = "") -> Dict[str, Any]:
    unit = await get_unit(key)
    if unit.get("status") != "recovered":
        raise ClosureError(409, "unit_not_recovered",
                           "仅已恢复的单元可归档案例（恢复判据未满足前先保持跟踪）")
    case_id = uuid.uuid4().hex[:16]
    case = {
        "case_id": case_id,
        "unit_key": key,
        "kind": unit.get("kind"),
        "source": unit.get("source"),
        "alarm_count": unit.get("alarm_count"),
        "root_cause": (root_cause or unit.get("root_cause") or "").strip()[:200],
        "resolution": (resolution or "").strip()[:800],
        "prevention": (prevention or "").strip()[:800],
        "recovery_evidence": unit.get("recovery_evidence") or "",
        "probe_summary": unit.get("probe_summary") or "",
        "operator": operator,
        "status": "pending",
        "created_at": _iso(),
        "rule_version": RULE_VERSION,
    }
    r = await get_redis()
    await r.hset(KEY_CASES, case_id, json.dumps(case, ensure_ascii=False))
    unit["case_id"] = case_id
    await _save_unit(unit)
    await _event(key, "case_recorded", case_id)
    await _bump("cases_total")
    return case


async def list_cases(limit: int = 50, kind: str = "") -> Dict[str, Any]:
    r = await get_redis()
    raw = await r.hgetall(KEY_CASES)
    cases = []
    for value in (raw or {}).values():
        try:
            case = json.loads(value)
        except Exception:
            continue
        if kind and case.get("kind") != kind:
            continue
        cases.append(case)
    cases.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    limit = max(1, min(int(limit or 50), 200))
    return {"total": len(cases), "items": cases[:limit]}


async def stats() -> Dict[str, Any]:
    units = list((await _load_units()).values())
    by_status = Counter(u.get("status") for u in units)
    by_tier = Counter(u.get("tier") for u in units)
    by_kind = Counter(u.get("kind") for u in units)
    tracked = [u for u in units if u.get("status") in TRACKED_STATUSES]
    metrics = {}
    try:
        r = await get_redis()
        metrics = await r.hgetall(KEY_METRICS) or {}
        case_count = await r.hlen(KEY_CASES)
    except Exception:
        case_count = 0
    return {
        "rule_version": RULE_VERSION,
        "enabled": bool(config.NMS_CLOSURE_ENABLED),
        "units": len(units),
        "by_status": dict(by_status),
        "by_tier": dict(by_tier),
        "by_kind": dict(by_kind),
        "tracked": len(tracked),
        "waiting_clear": sum(1 for u in tracked if u.get("status") == "waiting_clear"),
        "in_maintenance": sum(1 for u in tracked if u.get("in_maintenance")),
        "probe_due": sum(1 for u in tracked if u.get("probe_kind") in PROBE_KINDS),
        "cases": int(case_count or 0),
        "metrics": metrics,
        "thresholds": {
            "quiet_s": config.NMS_CLOSURE_QUIET_S,
            "l1_healthy": config.NMS_CLOSURE_L1_HEALTHY,
            "l1_spacing_s": config.NMS_CLOSURE_L1_MIN_SPACING_S,
            "probe_unknown_limit": config.NMS_CLOSURE_PROBE_UNKNOWN_LIMIT,
            "probe_interval_s": config.NMS_CLOSURE_PROBE_INTERVAL,
            "track_window_s": config.NMS_CLOSURE_TRACK_WINDOW_S,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# 后台循环
# ═══════════════════════════════════════════════════════════════════════════

async def _probe_due(now: Optional[float] = None) -> Dict[str, Any]:
    now = _now() if now is None else now
    units = await _load_units()
    interval = max(30, config.NMS_CLOSURE_PROBE_INTERVAL)
    due = [u for u in units.values()
           if u.get("status") in TRACKED_STATUSES
           and u.get("probe_kind") in PROBE_KINDS
           and not u.get("in_maintenance")
           and now - (u.get("probe_last_at_ts") or 0) >= interval]
    due.sort(key=lambda u: u.get("probe_last_at_ts") or 0)
    due = due[:max(0, config.NMS_CLOSURE_MAX_PROBES)]
    if not due:
        return {"due": 0, "probed": 0}
    r = await get_redis()
    token = uuid.uuid4().hex
    try:
        locked = await r.set(KEY_PROBE_LOCK, token, nx=True, ex=180)
    except Exception:
        locked = True
    if not locked:
        return {"due": len(due), "probed": 0, "skipped": "lock_busy"}
    probed = 0
    try:
        for unit in due:
            await _probe_unit(unit, now)
            await _save_unit(unit)
            probed += 1
            await _bump("probe_rounds")
    finally:
        try:
            if await r.get(KEY_PROBE_LOCK) == token:
                await r.delete(KEY_PROBE_LOCK)
        except Exception:
            pass
    return {"due": len(due), "probed": probed}


async def run_once(now: Optional[float] = None) -> Dict[str, Any]:
    if not config.NMS_CLOSURE_ENABLED:
        return {"skipped": "disabled"}
    r = await get_redis()
    raw = await r.get(f"{config.REDIS_PREFIX}:nms_alarms:latest")
    snapshot = json.loads(raw) if raw else None
    result = await refresh_units(snapshot=snapshot, now=now)
    result["probes"] = await _probe_due(now)
    try:
        await r.hset(KEY_METRICS, mapping={"last_refresh_at": _iso(now),
                                           "last_units": result["units_total"],
                                           "last_empty": int(result["empty_snapshot"])})
    except Exception:
        pass
    logger.info(
        "[alert_closure] units=%s tracked=%s recovered=%s expired=%s new=%s "
        "dropped=%s empty=%s probes=%s/%s",
        result["units_total"],
        result["by_status"].get("confirmed", 0) + result["by_status"].get("waiting_clear", 0),
        len(result["recovered"]), len(result["expired"]), result["new_units"],
        result["dropped"], result["empty_snapshot"],
        result["probes"].get("probed", 0), result["probes"].get("due", 0))
    return result


async def worker_loop() -> None:
    """常驻后台：定期把采集快照刷新成单元、推进恢复核验、按需探测 L1。"""
    logger.info("[alert_closure] 告警闭环后台已启动 (rule=%s)", RULE_VERSION)
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[alert_closure] worker 循环异常（继续）: {e}")
        await asyncio.sleep(max(30, config.NMS_CLOSURE_WORKER_INTERVAL))
