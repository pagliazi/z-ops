"""NMS SSL 证书调查闭环 — 状态机 / 持久化 / 恢复跟踪 / 案例库

范围（与 NMS_SSL_调查闭环 计划一致）：
  告警 → 只读取证 → 证据化判断 → 人工处置标记 → 自动恢复核验 → 案例审核 → 后续调查引用。

硬约束：
- 不自动 ACK / 屏蔽告警 / 换证书 / 调 LLM / 改生产配置；
- 恢复必须同时满足「连续 3 次严格取证健康（相邻≥60s、跨度≥120s、证据≤5min）」
  与「NMS 明确清除记录（晚于人工完成时间、年龄不超 max(2×采集间隔, 300s)）」；
- 告警消失、ACK、旧快照、采集失败、记录缺字段都不算清除，一律保持等待；
- 调查用独立 UUID 永久保存，事件簇重新归并/告警退出列表都不影响访问。
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import config

logger = logging.getLogger("zops")

RULE_VERSION = "ssl-inv-v1"
ACTIVE_SEVERITIES = {"critical", "major", "minor", "warning", "indeterminate"}
STATUSES = ("queued", "investigating", "awaiting_action", "observing",
            "waiting_nms", "recovered", "paused")
ACTIVE_STATUSES = {"queued", "investigating", "awaiting_action", "observing", "waiting_nms"}
SCHEDULED_STATUSES = {"queued", "investigating", "observing", "waiting_nms"}
TERMINAL_STATUSES = {"recovered", "paused"}
MANUAL_CATEGORIES = ("renewed", "replaced", "chain_fixed", "config_changed",
                     "human_checked_no_change", "other")
CASE_STATUSES = ("pending", "approved", "returned", "revoked")
# 租约只使用 TTL：60 秒有效期，探测期间每 15 秒续租（单调 fencing 令牌）
LEASE_TTL_SECONDS = 60
LEASE_RENEW_SECONDS = 15.0


class WorkflowError(Exception):
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
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  tz=timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _norm_source(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_ref(user: str, base: str, snapshot: Dict) -> str:
    """快照不透明引用：用户 + 实际快照 base + 时间 + 内容摘要。

    客户端无法伪造（也不允许指定 base）；创建调查时服务端重算并比对，
    防止同时间戳、个人/全局切换导致绑定到错误的告警集合。
    """
    ids = sorted(str(a.get("alarm_id") or "") for a in (snapshot.get("alarms") or []))
    return _sha(f"{user}|{base}|{snapshot.get('timestamp')}|{len(ids)}|"
                f"{_sha('|'.join(ids))}")[:40]


def investigation_fingerprint(scope: str, alarm: Dict, target: Dict) -> str:
    """稳定指纹：scope + source + 域名 + 端口 + event_key（缺失回退 event_group）。

    不使用 Serial（会因事件重插换手）与剩余天数（每轮都变）。
    """
    identity = str(alarm.get("event_key") or alarm.get("event_group") or "")
    if not identity:
        raise WorkflowError(422, "unstable_identity",
                            "告警缺少 event_key/event_group，无法建立稳定调查身份")
    return _sha("|".join([
        scope, _norm_source(alarm.get("source")), target["host"], str(target["port"]), identity,
    ]))[:40]


def _manual_quiet_s() -> int:
    return max(60, int(getattr(config, "NMS_SSL_MANUAL_QUIET_S", 1800)))


def probe_policy() -> Dict:
    return {
        "min_days": int(getattr(config, "NMS_SSL_MIN_DAYS", 30)),
        "allowed_ports": list(getattr(config, "NMS_SSL_ALLOWED_PORTS", [443])),
        "manual_quiet_s": _manual_quiet_s(),
        "ca_file": getattr(config, "NMS_SSL_CA_FILE", "") or "",
        "dns_timeout": 5,
        "connect_timeout": 8,
        "total_timeout": 40,
        "max_addresses": 4,
    }


def _enabled() -> bool:
    return bool(getattr(config, "NMS_SSL_WORKFLOW_ENABLED", True))


# ═══════════════════════════════════════════════════════════════════════════
# 运行时可观测性：进程内轻量指标（无外部依赖，进程重启归零）
#
# 只收灰度评估需要的最小集合：
#   - 租约：续租尝试/失败次数、丢租次数（围栏是否健康）
#   - 探测：轮次、耗时、unknown 比例（是否接近超时/租约上限）
#   - 采集：轮次/异常/空快照、最近窗口异常率（NMS 数据是否可信）
#     采集发生在 nms_agent 进程，因此同时写入 Redis 供 backend 汇总读取
#   - 调度：worker 周期、到期数、抢锁失败、并发槽满（是否饱和）
#   - 等待：waiting_nms 进入/退出、停留时长（清除证据是否迟迟不到）
#   - 恢复：manual_completed_at → recovered_at 耗时（端到端处置效率）
# 日志统一前缀 [nms_ssl_metrics]，便于灰度期 grep 与看板采集。
# ═══════════════════════════════════════════════════════════════════════════

_COLLECTION_WINDOW = 50  # 最近 N 轮采集的异常率窗口
_SLOW_PROBE_SECONDS = 10.0  # 超过该耗时的探测单独告警（总超时 40s、租约 60s）
_PROCESS_STARTED_AT = _iso()


def _new_metrics() -> Dict[str, Any]:
    return {
        "lease_renew_attempts": 0,
        "lease_renew_failures": 0,
        "lease_lost": 0,
        "probe_discarded": 0,
        "probe_rounds": 0,
        "probe_unknown": 0,
        "probe_seconds_count": 0,
        "probe_seconds_sum": 0.0,
        "probe_seconds_max": 0.0,
        "probe_seconds_last": 0.0,
        "worker_cycles": 0,
        "worker_due_total": 0,
        "lease_busy": 0,
        "slot_full": 0,
        "collection_rounds": 0,
        "collection_errors": 0,
        "collection_empty": 0,
        "collection_recent": deque(maxlen=_COLLECTION_WINDOW),
        "scopes": {},
        "waiting_nms_entered": 0,
        "waiting_nms_exited": 0,
        "waiting_nms_dwell_count": 0,
        "waiting_nms_dwell_sum": 0.0,
        "waiting_nms_dwell_max": 0.0,
        "waiting_nms_dwell_last": 0.0,
        "waiting_nms_pending_max": 0.0,
        "recovered_total": 0,
        "recovery_count": 0,
        "recovery_sum": 0.0,
        "recovery_max": 0.0,
        "recovery_last": 0.0,
    }


_metrics: Dict[str, Any] = _new_metrics()
_metrics_lock = threading.Lock()
_last_summary_at = 0.0


def reset_metrics() -> None:
    """测试用：清空进程内指标（生产不调用）。"""
    global _metrics, _last_summary_at
    with _metrics_lock:
        _metrics = _new_metrics()
        _last_summary_at = 0.0


def _recent_error_rate(recent) -> float:
    total = len(recent)
    if not total:
        return 0.0
    return sum(1 for ok in recent if not ok) / total * 100


def record_lease_renew(ok: bool) -> int:
    """记一次续租尝试，返回累计失败次数（调用方负责一次性告警，避免重复刷屏）。"""
    with _metrics_lock:
        _metrics["lease_renew_attempts"] += 1
        if not ok:
            _metrics["lease_renew_failures"] += 1
        return _metrics["lease_renew_failures"]


def record_lease_lost() -> int:
    with _metrics_lock:
        _metrics["lease_lost"] += 1
        return _metrics["lease_lost"]


def record_probe_discarded() -> int:
    """失租后丢弃的探测结果数（含提交前失效），与租约失败分开统计。"""
    with _metrics_lock:
        _metrics["probe_discarded"] += 1
        return _metrics["probe_discarded"]


def _note_probe(result: Dict, elapsed: float, inv_id: str = "") -> None:
    """记录一轮探测耗时与 unknown 比例；超过 10s 单独告警（租约 60s 将至）。"""
    diagnosis = (result or {}).get("diagnosis")
    with _metrics_lock:
        _metrics["probe_rounds"] += 1
        if diagnosis == "unknown":
            _metrics["probe_unknown"] += 1
        _metrics["probe_seconds_count"] += 1
        _metrics["probe_seconds_sum"] += elapsed
        _metrics["probe_seconds_max"] = max(_metrics["probe_seconds_max"], elapsed)
        _metrics["probe_seconds_last"] = elapsed
        count = _metrics["probe_seconds_count"]
        avg = _metrics["probe_seconds_sum"] / count
    if elapsed >= _SLOW_PROBE_SECONDS:
        logger.warning(f"[nms_ssl_metrics] probe 慢探测 id={inv_id} "
                       f"elapsed={elapsed:.1f}s avg={avg:.1f}s diagnosis={diagnosis}")


def record_worker_cycle(due: int) -> None:
    """worker 调度周期与到期量（观察是否积压）。"""
    with _metrics_lock:
        _metrics["worker_cycles"] += 1
        _metrics["worker_due_total"] += max(0, int(due or 0))


def record_lease_busy() -> None:
    """抢租失败：上一轮未结束或锁未过期（并发配置偏小/探测变慢）。"""
    with _metrics_lock:
        _metrics["lease_busy"] += 1


def record_slot_full() -> None:
    """并发槽位已满，本轮延后 5s 重试。"""
    with _metrics_lock:
        _metrics["slot_full"] += 1


def record_collection(scope_base: str, ok: bool, error: str = "",
                      empty: bool = False) -> str:
    """记录一轮 NMS 采集（每 scope 每轮一次），返回紧凑日志行。"""
    scope = scope_base or "-"
    with _metrics_lock:
        _metrics["collection_rounds"] += 1
        if not ok:
            _metrics["collection_errors"] += 1
        elif empty:
            _metrics["collection_empty"] += 1
        _metrics["collection_recent"].append(bool(ok))
        st = _metrics["scopes"].setdefault(scope, {"rounds": 0, "errors": 0})
        st["rounds"] += 1
        if not ok:
            st["errors"] += 1
        rounds, errors = st["rounds"], st["errors"]
        empty_total = _metrics["collection_empty"]
        recent_rate = _recent_error_rate(_metrics["collection_recent"])
    scope_rate = errors / rounds * 100 if rounds else 0.0
    tail = f" last_error={error[:120]}" if error else ""
    return (f"scope={scope} rounds={rounds} errors={errors} "
            f"scope_error_rate={scope_rate:.1f}% recent_error_rate={recent_rate:.1f}% "
            f"empty={empty_total}{tail}")


def _collection_metrics_key() -> str:
    return f"{config.REDIS_PREFIX}:nms:ssl:metrics"


async def record_collection_remote(r, scope_base: str, ok: bool, error: str = "",
                                   empty: bool = False) -> str:
    """采集计数：本地进度 + 写 Redis。

    nms_agent 与 backend 是不同进程，写 Redis 后 backend 的汇总与指标接口
    能看到跨进程采集异常率；Redis 异常只记日志，不影响采集主流程。
    """
    line = record_collection(scope_base, ok, error, empty)
    if r is None:
        return line
    scope = scope_base or "-"
    try:
        key = _collection_metrics_key()
        pipe = r.pipeline(transaction=False)
        pipe.hincrby(key, "collection_rounds", 1)
        if not ok:
            pipe.hincrby(key, "collection_errors", 1)
        elif empty:
            pipe.hincrby(key, "collection_empty", 1)
        pipe.hincrby(key, f"scope:{scope}:rounds", 1)
        if not ok:
            pipe.hincrby(key, f"scope:{scope}:errors", 1)
        pipe.hset(key, mapping={
            "last_scope": scope,
            "last_error": (error or "")[:200],
            "last_at": _iso(),
        })
        await pipe.execute()
    except Exception as e:
        logger.warning(f"[nms_ssl_metrics] 采集指标写入 Redis 失败（不阻断采集）: {e}")
    return line


async def read_collection_metrics(r) -> Optional[Dict[str, Any]]:
    """读取跨进程采集计数；无数据或 Redis 异常时返回 None（由调用方回退本地）。"""
    if r is None:
        return None
    try:
        raw = await r.hgetall(_collection_metrics_key())
    except Exception as e:
        logger.debug(f"[nms_ssl_metrics] 读取采集指标失败: {e}")
        return None
    if not raw:
        return None

    def _int(field: str) -> int:
        try:
            return int(raw.get(field) or 0)
        except (TypeError, ValueError):
            return 0

    rounds = _int("collection_rounds")
    errors = _int("collection_errors")
    scopes = {}
    for field in raw:
        if field.startswith("scope:") and field.endswith(":rounds"):
            scope = field[len("scope:"):-len(":rounds")]
            scopes[scope] = {"rounds": _int(field),
                             "errors": _int(f"scope:{scope}:errors")}
    return {
        "collection_rounds": rounds,
        "collection_errors": errors,
        "collection_empty": _int("collection_empty"),
        "error_rate": round(errors / rounds * 100, 1) if rounds else 0.0,
        "scopes": scopes,
        "last_scope": raw.get("last_scope") or "",
        "last_error": raw.get("last_error") or "",
        "last_at": raw.get("last_at") or "",
        "source": "redis",
    }


def _note_waiting_enter(inv: Dict) -> None:
    with _metrics_lock:
        _metrics["waiting_nms_entered"] += 1
    logger.info(f"[nms_ssl_metrics] waiting_nms 开始 id={inv.get('id')} "
                f"streak={inv.get('healthy_streak')}")


def _note_waiting_pending(inv: Dict, now: float) -> None:
    """仍停留在 waiting_nms 的记录：记录当前最长停留（评估证据是否长期不到）。"""
    since = _parse_iso(inv.get("waiting_since"))
    if since is None:
        return
    with _metrics_lock:
        _metrics["waiting_nms_pending_max"] = max(
            _metrics["waiting_nms_pending_max"], max(0.0, now - since))


def _note_waiting_exit(inv: Dict, now: float, outcome: str) -> float:
    """离开 waiting_nms（恢复/回退/暂停/窗口到期）时记录停留时长。"""
    since = _parse_iso(inv.get("waiting_since"))
    if since is None:
        return 0.0
    dwell = max(0.0, now - since)
    with _metrics_lock:
        _metrics["waiting_nms_exited"] += 1
        _metrics["waiting_nms_dwell_count"] += 1
        _metrics["waiting_nms_dwell_sum"] += dwell
        _metrics["waiting_nms_dwell_max"] = max(_metrics["waiting_nms_dwell_max"], dwell)
        _metrics["waiting_nms_dwell_last"] = dwell
        count = _metrics["waiting_nms_dwell_count"]
        avg = _metrics["waiting_nms_dwell_sum"] / count
        worst = _metrics["waiting_nms_dwell_max"]
    logger.info(f"[nms_ssl_metrics] waiting_nms 结束 id={inv.get('id')} outcome={outcome} "
                f"dwell={dwell:.0f}s avg={avg:.0f}s max={worst:.0f}s exits={count}")
    return dwell


def _note_recovery(inv: Dict, now: float) -> None:
    """恢复耗时 = recovered_at - manual_completed_at（人工处置完成到自动核验通过）。"""
    recovered_ts = _parse_iso(inv.get("recovered_at")) or now
    with _metrics_lock:
        _metrics["recovered_total"] += 1
    manual_ts = _parse_iso(inv.get("manual_completed_at"))
    if manual_ts is None:
        logger.info(f"[nms_ssl_metrics] recovered id={inv.get('id')} recovery_seconds=unknown")
        return
    dur = max(0.0, recovered_ts - manual_ts)
    with _metrics_lock:
        _metrics["recovery_count"] += 1
        _metrics["recovery_sum"] += dur
        _metrics["recovery_max"] = max(_metrics["recovery_max"], dur)
        _metrics["recovery_last"] = dur
        count = _metrics["recovery_count"]
        avg = _metrics["recovery_sum"] / count
        worst = _metrics["recovery_max"]
    logger.info(f"[nms_ssl_metrics] recovered id={inv.get('id')} recovery={dur:.0f}s "
                f"avg={avg:.0f}s max={worst:.0f}s count={count}")


def metrics_snapshot() -> Dict[str, Any]:
    """当前指标快照（含派生率/均值），供汇总日志、测试与人工排查使用。"""
    with _metrics_lock:
        recent = list(_metrics["collection_recent"])
        dwell_count = int(_metrics["waiting_nms_dwell_count"])
        dwell_sum = float(_metrics["waiting_nms_dwell_sum"])
        recovery_count = int(_metrics["recovery_count"])
        recovery_sum = float(_metrics["recovery_sum"])
        probe_count = int(_metrics["probe_seconds_count"])
        probe_sum = float(_metrics["probe_seconds_sum"])
        snap = {
            "lease_renew_attempts": _metrics["lease_renew_attempts"],
            "lease_renew_failures": _metrics["lease_renew_failures"],
            "lease_lost": _metrics["lease_lost"],
            "probe_discarded": _metrics["probe_discarded"],
            "probe_rounds": _metrics["probe_rounds"],
            "probe_unknown": _metrics["probe_unknown"],
            "probe_avg": round(probe_sum / probe_count, 2) if probe_count else 0.0,
            "probe_max": round(float(_metrics["probe_seconds_max"]), 2),
            "probe_last": round(float(_metrics["probe_seconds_last"]), 2),
            "worker_cycles": _metrics["worker_cycles"],
            "worker_due_total": _metrics["worker_due_total"],
            "lease_busy": _metrics["lease_busy"],
            "slot_full": _metrics["slot_full"],
            "collection_rounds": _metrics["collection_rounds"],
            "collection_errors": _metrics["collection_errors"],
            "collection_empty": _metrics["collection_empty"],
            "scopes": {k: dict(v) for k, v in _metrics["scopes"].items()},
            "waiting_nms_entered": _metrics["waiting_nms_entered"],
            "waiting_nms_exited": _metrics["waiting_nms_exited"],
            "waiting_nms_dwell_count": dwell_count,
            "waiting_nms_dwell_avg": round(dwell_sum / dwell_count, 1) if dwell_count else 0.0,
            "waiting_nms_dwell_max": round(float(_metrics["waiting_nms_dwell_max"]), 1),
            "waiting_nms_dwell_last": round(float(_metrics["waiting_nms_dwell_last"]), 1),
            "waiting_nms_pending_max": round(float(_metrics["waiting_nms_pending_max"]), 1),
            "recovered_total": _metrics["recovered_total"],
            "recovery_count": recovery_count,
            "recovery_avg": round(recovery_sum / recovery_count, 1) if recovery_count else 0.0,
            "recovery_max": round(float(_metrics["recovery_max"]), 1),
            "recovery_last": round(float(_metrics["recovery_last"]), 1),
        }
    rounds = snap["collection_rounds"]
    attempts = snap["lease_renew_attempts"]
    snap["collection_error_rate"] = (
        round(snap["collection_errors"] / rounds * 100, 1) if rounds else 0.0)
    snap["lease_failure_rate"] = (
        round(snap["lease_renew_failures"] / attempts * 100, 1) if attempts else 0.0)
    snap["probe_unknown_rate"] = (
        round(snap["probe_unknown"] / snap["probe_rounds"] * 100, 1)
        if snap["probe_rounds"] else 0.0)
    snap["recent_error_rate"] = round(_recent_error_rate(recent), 1)
    snap["collection_window"] = len(recent)
    return snap


def metrics_line(snapshot: Optional[Dict] = None) -> str:
    """单行 key=value 摘要，便于日志检索与采集。"""
    s = snapshot or metrics_snapshot()
    return (
        f"lease_attempts={s['lease_renew_attempts']} lease_failures={s['lease_renew_failures']} "
        f"lease_lost={s['lease_lost']} probe_discarded={s['probe_discarded']} | "
        f"probe_rounds={s['probe_rounds']} probe_unknown={s['probe_unknown']} "
        f"probe_avg={s['probe_avg']:.1f}s probe_max={s['probe_max']:.1f}s | "
        f"collection_rounds={s['collection_rounds']} collection_errors={s['collection_errors']} "
        f"collection_empty={s['collection_empty']} error_rate={s['collection_error_rate']:.1f}% "
        f"recent_error_rate={s['recent_error_rate']:.1f}% | "
        f"worker_cycles={s['worker_cycles']} worker_due={s['worker_due_total']} "
        f"lease_busy={s['lease_busy']} slot_full={s['slot_full']} | "
        f"waiting_entered={s['waiting_nms_entered']} waiting_exited={s['waiting_nms_exited']} "
        f"waiting_dwell_avg={s['waiting_nms_dwell_avg']:.0f}s "
        f"waiting_dwell_max={s['waiting_nms_dwell_max']:.0f}s "
        f"waiting_pending_max={s['waiting_nms_pending_max']:.0f}s | "
        f"recovered={s['recovered_total']} recovery_avg={s['recovery_avg']:.0f}s "
        f"recovery_max={s['recovery_max']:.0f}s"
    )


async def metrics_report(r=None) -> Dict[str, Any]:
    """只读指标报告（管理员接口用）：进程内指标 + 跨进程采集计数 + 关键配置。

    采集发生在 nms_agent 进程，backend 本进程采集计数恒为 0；有 Redis 计数时
    将其合并到 metrics.collection_*，同时保留 collection_remote 作为出处。
    """
    local = metrics_snapshot()
    remote = await read_collection_metrics(r)
    if remote and remote.get("collection_rounds"):
        local["collection_rounds"] = remote["collection_rounds"]
        local["collection_errors"] = remote["collection_errors"]
        local["collection_empty"] = remote["collection_empty"]
        local["collection_error_rate"] = remote["error_rate"]
        local["scopes"] = remote.get("scopes") or local["scopes"]
        if not local["collection_window"]:
            local["recent_error_rate"] = remote["error_rate"]
    return {
        "generated_at": _iso(),
        "enabled": _enabled(),
        "process": {"pid": os.getpid(), "started_at": _PROCESS_STARTED_AT},
        "metrics": local,
        "collection_remote": remote,
        "config": {
            "check_interval": int(getattr(config, "NMS_SSL_CHECK_INTERVAL", 60)),
            "consecutive_healthy": int(getattr(config, "NMS_SSL_CONSECUTIVE_HEALTHY", 3)),
            "global_concurrency": int(getattr(config, "NMS_SSL_GLOBAL_CONCURRENCY", 2)),
            "max_active_per_user": int(getattr(config, "NMS_SSL_MAX_ACTIVE_PER_USER", 10)),
            "metrics_log_interval_s": int(
                getattr(config, "NMS_SSL_METRICS_LOG_INTERVAL_S", 300)),
        },
    }


def log_metrics_summary(force: bool = False,
                        collection: Optional[Dict[str, Any]] = None) -> bool:
    """按 NMS_SSL_METRICS_LOG_INTERVAL_S 输出汇总行；<=0 表示关闭周期汇总。

    collection：跨进程采集计数（Redis）。存在时覆盖本地采集字段，
    避免 backend 进程看不到 nms_agent 的采集异常率。
    """
    global _last_summary_at
    interval = int(getattr(config, "NMS_SSL_METRICS_LOG_INTERVAL_S", 300))
    if interval <= 0 and not force:
        return False
    now = _now()
    if not force and now - _last_summary_at < interval:
        return False
    snap = metrics_snapshot()
    if collection and collection.get("collection_rounds"):
        snap["collection_rounds"] = collection["collection_rounds"]
        snap["collection_errors"] = collection["collection_errors"]
        snap["collection_empty"] = collection["collection_empty"]
        snap["collection_error_rate"] = collection["error_rate"]
        snap["scopes"] = collection.get("scopes") or snap["scopes"]
        if not snap["collection_window"]:
            # 本进程无采集窗口（采集在 nms_agent）：用累计异常率近似
            snap["recent_error_rate"] = collection["error_rate"]
    active = (snap["collection_rounds"] or snap["lease_renew_attempts"]
              or snap["probe_rounds"] or snap["waiting_nms_entered"]
              or snap["recovered_total"]
              # 有到期调查即算活动：避免"一直在排队但汇总静默"的情况
              or snap["worker_due_total"])
    _last_summary_at = now
    if not active:
        return False  # 无活动不刷屏，且退避到下一个汇总周期
    logger.info(f"[nms_ssl_metrics] summary {metrics_line(snap)}")
    return True


async def log_metrics_summary_async(store, force: bool = False) -> bool:
    """worker 用：先判到期，再读跨进程采集计数，合并后输出。"""
    interval = int(getattr(config, "NMS_SSL_METRICS_LOG_INTERVAL_S", 300))
    if interval <= 0 and not force:
        return False
    if not force and _now() - _last_summary_at < interval:
        return False
    r = getattr(store, "_r", None)
    collection = await read_collection_metrics(r) if r is not None else None
    return log_metrics_summary(force=True, collection=collection)


# ═══════════════════════════════════════════════════════════════════════════
# 存储：接口一致的内存实现 + Redis 实现（业务记录不设 TTL、不裁剪）
# ═══════════════════════════════════════════════════════════════════════════

class MemoryNmsSslStore:
    """测试/降级用内存存储；与 RedisNmsSslStore 行为对齐。"""

    kind = "memory"

    def __init__(self):
        self.invs: Dict[str, str] = {}
        self.due: Dict[str, float] = {}
        self.fp: Dict[str, Dict[str, str]] = {}
        self.idx: Dict[str, Dict[str, float]] = {}
        self.cases: Dict[str, str] = {}
        self.case_idx: Dict[str, float] = {}
        self.idem: Dict[str, str] = {}
        self.leases: Dict[str, Tuple[str, float]] = {}
        self.slots: Dict[int, Tuple[str, float]] = {}

    async def get_inv(self, inv_id: str) -> Optional[str]:
        return self.invs.get(inv_id)

    async def cas_inv(self, inv_id: str, expected: Optional[str], new: str) -> bool:
        if self.invs.get(inv_id) != expected:
            return False
        self.invs[inv_id] = new
        return True

    async def all_inv_ids(self) -> List[str]:
        return [i for i, _ in sorted(self.idx_all().items(), key=lambda x: -x[1])][:500]

    def idx_all(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for owner_idx in self.idx.values():
            out.update(owner_idx)
        return out

    async def save_index(self, owner: str, inv_id: str, created: float) -> None:
        self.idx.setdefault(owner, {})[inv_id] = created

    async def list_owner_ids(self, owner: str) -> List[str]:
        return [i for i, _ in sorted(self.idx.get(owner, {}).items(), key=lambda x: -x[1])]

    async def fingerprint_map(self, owner: str) -> Dict[str, str]:
        return dict(self.fp.get(owner, {}))

    async def set_fingerprint(self, owner: str, fingerprint: str, inv_id: str) -> None:
        self.fp.setdefault(owner, {})[fingerprint] = inv_id

    async def add_due(self, inv_id: str, ts: float) -> None:
        self.due[inv_id] = ts

    async def drop_due(self, inv_id: str) -> None:
        self.due.pop(inv_id, None)

    async def due_ids(self, now: float, limit: int) -> List[str]:
        return [i for i, ts in sorted(self.due.items(), key=lambda x: x[1]) if ts <= now][:limit]

    async def acquire_lease(self, inv_id: str, token: str, ttl: int) -> bool:
        cur = self.leases.get(inv_id)
        if cur and cur[1] > time.time():
            return False
        self.leases[inv_id] = (token, time.time() + ttl)
        return True

    async def renew_lease(self, inv_id: str, token: str, ttl: int) -> bool:
        cur = self.leases.get(inv_id)
        if not cur or cur[0] != token:
            return False
        self.leases[inv_id] = (token, time.time() + ttl)
        return True

    async def release_lease(self, inv_id: str, token: str) -> None:
        if self.leases.get(inv_id, ("",))[0] == token:
            self.leases.pop(inv_id, None)

    async def acquire_slot(self, token: str, ttl: int, slots: int) -> Optional[int]:
        now = time.time()
        for i in range(max(1, slots)):
            cur = self.slots.get(i)
            if not cur or cur[1] <= now:
                self.slots[i] = (token, now + ttl)
                return i
        return None

    async def release_slot(self, slot: int, token: str) -> None:
        if self.slots.get(slot, ("",))[0] == token:
            self.slots.pop(slot, None)

    async def get_idem(self, owner: str, key: str) -> Optional[str]:
        return self.idem.get(f"{owner}|{key}")

    async def set_idem(self, owner: str, key: str, raw: str) -> None:
        self.idem[f"{owner}|{key}"] = raw

    async def get_case(self, case_id: str) -> Optional[str]:
        return self.cases.get(case_id)

    async def save_case(self, case_id: str, raw: str) -> None:
        self.cases[case_id] = raw
        self.case_idx.setdefault(case_id, time.time())

    async def list_case_ids(self) -> List[str]:
        return [i for i, _ in sorted(self.case_idx.items(), key=lambda x: -x[1])]


class RedisNmsSslStore(MemoryNmsSslStore):
    """Redis 实现：调查/证据/案例永久保存；仅租约与并发槽使用 TTL。"""

    kind = "redis"

    def __init__(self, client):
        self._r = client
        self._p = f"{config.REDIS_PREFIX}:nms:ssl"

    # --- 调查 ---
    async def get_inv(self, inv_id: str) -> Optional[str]:
        return await self._r.hget(f"{self._p}:inv", inv_id)

    async def cas_inv(self, inv_id: str, expected: Optional[str], new: str) -> bool:
        import redis.asyncio as aioredis
        key = f"{self._p}:inv"
        for _ in range(5):
            async with self._r.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    cur = await pipe.hget(key, inv_id)
                    if (cur or None) != (expected or None):
                        await pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.hset(key, inv_id, new)
                    await pipe.execute()
                    return True
                except aioredis.WatchError:
                    continue
        return False

    async def all_inv_ids(self) -> List[str]:
        return await self._r.zrevrange(f"{self._p}:all", 0, 499)

    async def save_index(self, owner: str, inv_id: str, created: float) -> None:
        await self._r.zadd(f"{self._p}:idx:{owner}", {inv_id: created})
        await self._r.zadd(f"{self._p}:all", {inv_id: created})

    async def list_owner_ids(self, owner: str) -> List[str]:
        return await self._r.zrevrange(f"{self._p}:idx:{owner}", 0, -1)

    async def fingerprint_map(self, owner: str) -> Dict[str, str]:
        return await self._r.hgetall(f"{self._p}:fp:{owner}")

    async def set_fingerprint(self, owner: str, fingerprint: str, inv_id: str) -> None:
        await self._r.hset(f"{self._p}:fp:{owner}", fingerprint, inv_id)

    async def add_due(self, inv_id: str, ts: float) -> None:
        await self._r.zadd(f"{self._p}:due", {inv_id: ts})

    async def drop_due(self, inv_id: str) -> None:
        await self._r.zrem(f"{self._p}:due", inv_id)

    async def due_ids(self, now: float, limit: int) -> List[str]:
        return await self._r.zrangebyscore(f"{self._p}:due", "-inf", now, start=0, num=limit)

    # --- 租约 / 并发槽（唯一使用 TTL 的部分）---
    async def acquire_lease(self, inv_id: str, token: str, ttl: int) -> bool:
        return bool(await self._r.set(f"{self._p}:lease:{inv_id}", token, nx=True, ex=ttl))

    async def renew_lease(self, inv_id: str, token: str, ttl: int) -> bool:
        key = f"{self._p}:lease:{inv_id}"
        cur = await self._r.get(key)
        if cur != token:
            return False
        await self._r.expire(key, ttl)
        return True

    async def release_lease(self, inv_id: str, token: str) -> None:
        key = f"{self._p}:lease:{inv_id}"
        if await self._r.get(key) == token:
            await self._r.delete(key)

    async def acquire_slot(self, token: str, ttl: int, slots: int) -> Optional[int]:
        for i in range(max(1, slots)):
            if await self._r.set(f"{self._p}:slot:{i}", token, nx=True, ex=ttl):
                return i
        return None

    async def release_slot(self, slot: int, token: str) -> None:
        key = f"{self._p}:slot:{slot}"
        if await self._r.get(key) == token:
            await self._r.delete(key)

    # --- 幂等 / 案例 ---
    async def get_idem(self, owner: str, key: str) -> Optional[str]:
        return await self._r.hget(f"{self._p}:idem:{owner}", key)

    async def set_idem(self, owner: str, key: str, raw: str) -> None:
        await self._r.hset(f"{self._p}:idem:{owner}", key, raw)

    async def get_case(self, case_id: str) -> Optional[str]:
        return await self._r.hget(f"{self._p}:cases", case_id)

    async def save_case(self, case_id: str, raw: str) -> None:
        await self._r.hset(f"{self._p}:cases", case_id, raw)
        await self._r.zadd(f"{self._p}:case_idx", {case_id: time.time()})

    async def list_case_ids(self) -> List[str]:
        return await self._r.zrevrange(f"{self._p}:case_idx", 0, 999)


_store: Optional[Any] = None
_redis_client: Optional[Any] = None


async def get_store():
    global _store, _redis_client
    if _store is not None:
        return _store
    import redis.asyncio as aioredis
    _redis_client = aioredis.from_url(config.REDIS_URL, decode_responses=True,
                                      max_connections=8)
    _store = RedisNmsSslStore(_redis_client)
    return _store


def set_store(store) -> None:
    """测试注入（生产不经此路径）。"""
    global _store
    _store = store


async def close_store() -> None:
    global _store, _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:
            pass
    _store = None
    _redis_client = None


# ═══════════════════════════════════════════════════════════════════════════
# 状态机（纯函数：给定调查与取证结果 → 新调查，便于单测）
# ═══════════════════════════════════════════════════════════════════════════

def _cert_signature(result: Dict) -> str:
    parts = []
    for c in result.get("certificates") or []:
        parts.append(f"{c.get('ip')}|{c.get('fingerprint') or ''}")
    return _sha("|".join(sorted(parts)) or "none")


def _evidence_fresh(inv: Dict, now: float, window: int = 300) -> bool:
    ts = _parse_iso(inv.get("last_check_at"))
    # 允许 5s 负偏差：ISO 微秒舍入与"本轮探测刚完成"的正常时序
    return bool(ts is not None and -5 <= now - ts <= window)


def _clear_evidence_ok(inv: Dict, now: float) -> bool:
    clear_ts = _parse_iso((inv.get("clear_evidence") or {}).get("observed_at"))
    manual_ts = _parse_iso(inv.get("manual_completed_at"))
    if not clear_ts or not manual_ts or clear_ts < manual_ts:
        return False
    interval = int(getattr(config, "NMS_SSL_CHECK_INTERVAL", 60))
    return now - clear_ts <= max(2 * interval, 300)


def apply_probe_result(inv: Dict, result: Dict, now: Optional[float] = None) -> Dict:
    """把一轮严格取证结果并入调查状态（不落库）。"""
    now = now or _now()
    inv = json.loads(json.dumps(inv, ensure_ascii=False))
    if (inv.get("verification") or "tls") == "manual":
        return inv  # 人工核验模式不做 TLS 取证，探测结果不得改写状态
    status = inv.get("status")
    min_healthy = int(getattr(config, "NMS_SSL_CONSECUTIVE_HEALTHY", 3))
    min_spacing = int(getattr(config, "NMS_SSL_HEALTHY_MIN_SPACING_S", 60))
    min_span = int(getattr(config, "NMS_SSL_HEALTHY_MIN_SPAN_S", 120))
    interval = int(getattr(config, "NMS_SSL_CHECK_INTERVAL", 60))
    track_window = int(getattr(config, "NMS_SSL_TRACK_WINDOW_S", 86400))

    inv["diagnosis"] = result
    inv["last_check_at"] = result.get("observed_at") or _iso(now)
    inv.setdefault("checks", []).append({
        "at": inv["last_check_at"],
        "diagnosis": result.get("diagnosis"),
        "defect_type": result.get("defect_type"),
        "addresses": result.get("addresses") or [],
        "certificates": [{"ip": c.get("ip"), "fingerprint": c.get("fingerprint"),
                          "remaining_seconds": c.get("remaining_seconds"),
                          "verified": c.get("verified")} for c in (result.get("certificates") or [])],
        "error": result.get("error") or "",
    })
    inv["checks"] = inv["checks"][-40:]
    inv["health"] = {
        "diagnosis": result.get("diagnosis"),
        "defect_type": result.get("defect_type"),
        "error": result.get("error") or "",
        "observed_at": inv["last_check_at"],
        "min_days": result.get("min_days", getattr(config, "NMS_SSL_MIN_DAYS", 30)),
        "certificates": result.get("certificates") or [],
    }

    # 24h 跟踪窗口到期：不再调度，历史保留（手动 recheck 仍可单次执行）
    manual_ts = _parse_iso(inv.get("manual_completed_at"))
    if status in ("observing", "waiting_nms") and manual_ts and now - manual_ts > track_window:
        if status == "waiting_nms":
            _note_waiting_exit(inv, now, "track_window_expired")
        inv["status"] = "paused"
        inv["paused_at"] = _iso(now)
        inv["pause_reason"] = "track_window_expired"
        inv["next_check_at"] = None
        _timeline(inv, "paused", "system", "跟踪窗口到期，停止自动调度（历史保留）")
        return inv

    if status == "paused":
        # 暂停中的单次复核不改变调度状态
        inv.pop("paused_recheck", None)
        inv["next_check_at"] = None
        _timeline(inv, "recheck", "user", f"暂停状态下单次复核：{result.get('diagnosis')}")
        return inv

    if status in ("queued", "investigating"):
        inv["status"] = "awaiting_action"
        inv["next_check_at"] = None
        _timeline(inv, "diagnosed", "system",
                  f"首次取证完成：{result.get('diagnosis')}/{result.get('defect_type')}")
        return inv

    if status == "observing":
        healthy = result.get("diagnosis") == "healthy"
        sig = _cert_signature(result)
        last_sig = inv.get("streak_signature") or ""
        last_ts = _parse_iso(inv.get("streak_last_at"))
        can_count = healthy and _evidence_fresh(inv, now)
        # 定时器抖动容差 1s：相邻两次复核按计划刚好 60s，不应因毫秒级抖动丢计
        spacing_ok = bool(last_ts and now - last_ts >= max(1, min_spacing) - 1)
        if can_count and sig == last_sig and spacing_ok:
            inv["healthy_streak"] = int(inv.get("healthy_streak") or 0) + 1
            inv["streak_last_at"] = _iso(now)
        elif can_count and sig != last_sig:
            inv["healthy_streak"] = 1
            inv["streak_signature"] = sig
            inv["streak_first_at"] = _iso(now)
            inv["streak_last_at"] = _iso(now)
        elif not healthy:
            inv["healthy_streak"] = 0
            inv["streak_signature"] = ""
            inv["streak_first_at"] = None
            inv["streak_last_at"] = None
        # 间隔不足 60s 的成功不计入连续次数（保持已有进度，等下一轮）
        first_ts = _parse_iso(inv.get("streak_first_at"))
        if (int(inv.get("healthy_streak") or 0) >= min_healthy and first_ts
                and now - first_ts >= min_span and _evidence_fresh(inv, now)):
            inv["status"] = "waiting_nms"
            inv["waiting_since"] = _iso(now)
            _note_waiting_enter(inv)
            _timeline(inv, "waiting_nms", "system",
                      f"连续 {inv['healthy_streak']} 次严格核验健康，等待 NMS 清除证据")
        inv["next_check_at"] = _iso(now + interval)
        inv["next_check_ts"] = now + interval
        return inv

    if status == "waiting_nms":
        if result.get("diagnosis") != "healthy":
            _note_waiting_exit(inv, now, "regressed")
            inv["status"] = "observing"
            inv["healthy_streak"] = 0
            inv["streak_signature"] = ""
            inv["streak_first_at"] = None
            inv["streak_last_at"] = None
            _timeline(inv, "regressed", "system",
                      f"TLS 重新异常（{result.get('defect_type')}），退回观察")
        elif _clear_evidence_ok(inv, now) and _evidence_fresh(inv, now):
            _note_waiting_exit(inv, now, "recovered")
            inv["status"] = "recovered"
            inv["recovered_at"] = _iso(now)
            inv["next_check_at"] = None
            _note_recovery(inv, now)
            _timeline(inv, "recovered", "system",
                      "严格取证健康且 NMS 已明确清除原告警")
            return inv
        _note_waiting_pending(inv, now)
        inv["next_check_at"] = _iso(now + interval)
        inv["next_check_ts"] = now + interval
        return inv

    return inv


def _timeline(inv: Dict, event: str, actor: str, detail: str) -> None:
    inv.setdefault("timeline", []).append({
        "at": _iso(), "event": event, "actor": actor, "detail": detail,
    })


# ═══════════════════════════════════════════════════════════════════════════
# 创建 / 查询 / 人工动作
# ═══════════════════════════════════════════════════════════════════════════

async def _active_inv(store, owner: str, fingerprint: str) -> Optional[Tuple[Dict, str]]:
    inv_id = (await store.fingerprint_map(owner)).get(fingerprint)
    if not inv_id:
        return None
    raw = await store.get_inv(inv_id)
    if not raw:
        return None
    inv = json.loads(raw)
    return (inv, raw) if inv.get("status") in ACTIVE_STATUSES else None


async def _previous_inv(store, owner: str, fingerprint: str) -> Optional[Dict]:
    """同指纹已终结（已恢复/已暂停）的最近一条调查：复发时关联旧记录。"""
    for inv_id in await store.list_owner_ids(owner):
        raw = await store.get_inv(inv_id)
        if not raw:
            continue
        prev = json.loads(raw)
        if prev.get("fingerprint") == fingerprint \
                and prev.get("status") in ("recovered", "paused"):
            return prev
    return None


async def _active_count(store, owner: str) -> int:
    n = 0
    for inv_id in await store.list_owner_ids(owner):
        raw = await store.get_inv(inv_id)
        if raw and json.loads(raw).get("status") in ACTIVE_STATUSES:
            n += 1
    return n


async def create_investigation(*, owner: str, scope_base: str, alarm: Dict,
                               incident_id: str = "", snapshot_timestamp: str = "",
                               snapshot_ref_value: str = "",
                               idempotency_key: str = "") -> Tuple[Dict, bool]:
    """创建（或幂等返回）一条调查。返回 (调查, 是否新建)。"""
    if not _enabled():
        raise WorkflowError(503, "workflow_disabled", "SSL 调查闭环已停用", retryable=False)
    from agents.ssl_check import extract_strict_target, extract_target_lenient

    verification = "tls"
    verification_note = ""
    try:
        target = extract_strict_target(alarm, getattr(config, "NMS_SSL_ALLOWED_PORTS", [443]))
    except ValueError as e:
        if "不在允许取证范围" not in str(e):
            raise WorkflowError(422, "invalid_target", str(e))
        # 端口不在自动取证白名单：降级为人工核验调查（不探测，仍可闭环）
        try:
            target = extract_target_lenient(alarm)
        except ValueError as e2:
            raise WorkflowError(422, "invalid_target", str(e2))
        verification = "manual"
        verification_note = str(e)

    store = await get_store()
    if idempotency_key:
        cached = await store.get_idem(owner, f"create:{idempotency_key}")
        if cached:
            return json.loads(cached), False

    fingerprint = investigation_fingerprint(scope_base, alarm, target)
    existing_pair = await _active_inv(store, owner, fingerprint)
    if existing_pair:
        existing, existing_raw = existing_pair
        ref = {"alarm_id": alarm.get("alarm_id"), "snapshot_timestamp": snapshot_timestamp,
               "at": _iso()}
        refs = existing.setdefault("source_refs", [])
        if ref not in refs:
            refs.append(ref)
        await _save(store, existing, existing_raw)
        return existing, False

    limit = int(getattr(config, "NMS_SSL_MAX_ACTIVE_PER_USER", 10))
    if await _active_count(store, owner) >= limit:
        raise WorkflowError(429, "quota_exceeded",
                            f"同时跟踪的调查已达上限 {limit}，请先完成或暂停已有调查")

    now = _now()
    previous = await _previous_inv(store, owner, fingerprint)
    inv_id = uuid.uuid4().hex
    inv = {
        "id": inv_id,
        "rule_version": RULE_VERSION,
        "status": "queued" if verification == "tls" else "awaiting_action",
        "verification": verification,
        "verification_note": verification_note,
        "owner": owner,
        "scope_base": scope_base,
        "scope": "personal" if ":user:" in scope_base else "global",
        "fingerprint": fingerprint,
        "target": target,
        "alarm": {
            "alarm_id": alarm.get("alarm_id"), "serial": alarm.get("serial"),
            "source": alarm.get("source"), "summary": alarm.get("summary"),
            "severity": alarm.get("severity"), "event_key": alarm.get("event_key"),
            "event_group": alarm.get("event_group"),
            "first_time": alarm.get("first_time"), "last_time": alarm.get("last_time"),
            "summary_raw": (alarm.get("fields") or {}).get("Summary") or "",
        },
        "incident_id": incident_id or "",
        "source_refs": [{"alarm_id": alarm.get("alarm_id"),
                         "snapshot_timestamp": snapshot_timestamp,
                         "snapshot_ref": snapshot_ref_value, "at": _iso(now)}],
        "created_at": _iso(now),
        "updated_at": _iso(now),
        "version": 1,
        "healthy_streak": 0,
        "checks": [],
        "timeline": [],
    }
    _timeline(inv, "created", owner, f"创建 SSL 证书调查：{target['host']}:{target['port']}")
    if verification == "manual":
        _timeline(inv, "manual_verification", owner,
                  f"无法自动取证（{verification_note}），转人工核验："
                  f"处置完成后需 NMS 清除持续 {_manual_quiet_s()} 秒方可判定恢复")
    if previous:
        inv["previous_investigation_id"] = previous["id"]
        _timeline(inv, "recurrence", owner,
                  f"复发新建：关联已终结调查 {previous['id']}"
                  f"（{previous.get('recovered_at') or previous.get('paused_at') or '已终结'}）")
    raw = json.dumps(inv, ensure_ascii=False)
    if not await store.cas_inv(inv_id, None, raw):
        raise WorkflowError(409, "create_conflict", "调查创建冲突，请重试", retryable=True)
    await store.save_index(owner, inv_id, now)
    await store.set_fingerprint(owner, fingerprint, inv_id)
    if verification == "tls":
        await store.add_due(inv_id, now)
    if idempotency_key:
        await store.set_idem(owner, f"create:{idempotency_key}", raw)
    return inv, True


async def _save(store, inv: Dict, expected_raw: Optional[str]) -> Dict:
    inv["version"] = int(inv.get("version") or 0) + 1
    inv["updated_at"] = _iso()
    raw = json.dumps(inv, ensure_ascii=False)
    if not await store.cas_inv(inv["id"], expected_raw, raw):
        raise WorkflowError(409, "version_conflict", "调查已被更新，请刷新后重试", retryable=True)
    return inv


async def _load_owned(store, inv_id: str, owner: str) -> Tuple[Dict, str]:
    raw = await store.get_inv(inv_id)
    if not raw:
        raise WorkflowError(404, "not_found", "调查不存在")
    inv = json.loads(raw)
    if inv.get("owner") != owner:
        raise WorkflowError(404, "not_found", "调查不存在")
    return inv, raw


async def get_investigation(owner: str, inv_id: str) -> Dict:
    store = await get_store()
    inv, _raw = await _load_owned(store, inv_id, owner)
    out = json.loads(json.dumps(inv, ensure_ascii=False))
    out["timeline_total"] = len(out.get("timeline") or [])
    out["timeline"] = (out.get("timeline") or [])[-50:]
    out["checks"] = (out.get("checks") or [])[-20:]
    out["cases"] = await _referenced_cases(inv)
    manual = (inv.get("verification") or "tls") == "manual"
    out.setdefault("verification", "manual" if manual else "tls")
    out["recovery"] = {
        "mode": "manual" if manual else "tls",
        "needs": (["manual_action", "nms_clear_quiet"] if manual
                  else ["tls_healthy_streak", "nms_clear"]),
        "tls_ok": None if manual else inv.get("status") in ("waiting_nms", "recovered"),
        "nms_clear_ok": bool(inv.get("clear_evidence")),
        "clear_evidence": inv.get("clear_evidence"),
        "manual_completed_at": inv.get("manual_completed_at"),
        "manual_clear_first_at": inv.get("manual_clear_first_at"),
        "manual_quiet_s": _manual_quiet_s(),
    }
    return out


async def list_investigations(owner: str, cursor: str = "", limit: int = 50) -> Dict:
    store = await get_store()
    ids = await store.list_owner_ids(owner)
    start = 0
    if cursor:
        try:
            start = ids.index(cursor) + 1
        except ValueError:
            start = 0
    page = ids[start:start + limit]
    items = []
    for inv_id in page:
        raw = await store.get_inv(inv_id)
        if not raw:
            continue
        inv = json.loads(raw)
        items.append({
            "id": inv["id"], "status": inv["status"], "target": inv.get("target"),
            "verification": inv.get("verification") or "tls",
            "owner": inv.get("owner"), "scope": inv.get("scope"),
            "created_at": inv.get("created_at"), "updated_at": inv.get("updated_at"),
            "healthy_streak": inv.get("healthy_streak", 0),
            "diagnosis": (inv.get("health") or {}).get("diagnosis"),
            "defect_type": (inv.get("health") or {}).get("defect_type"),
            "last_check_at": inv.get("last_check_at"),
            "nms_last_observed_at": inv.get("nms_last_observed_at"),
            "manual_completed_at": inv.get("manual_completed_at"),
            "recovered_at": inv.get("recovered_at"),
            "pause_reason": inv.get("pause_reason"),
            "alarm": {"summary": (inv.get("alarm") or {}).get("summary"),
                      "source": (inv.get("alarm") or {}).get("source"),
                      "severity": (inv.get("alarm") or {}).get("severity")},
        })
    return {"items": items, "next_cursor": page[-1] if len(page) == limit else None,
            "total": len(ids)}


async def manual_completed(owner: str, inv_id: str, category: str, note: str,
                           expected_version: Optional[int] = None) -> Dict:
    if category not in MANUAL_CATEGORIES:
        raise WorkflowError(422, "invalid_category", "处理类别非法")
    if not (note or "").strip():
        raise WorkflowError(422, "note_required", "请填写实际处置说明")
    store = await get_store()
    inv, raw = await _load_owned(store, inv_id, owner)
    if expected_version is not None and int(expected_version) != int(inv.get("version") or 0):
        raise WorkflowError(409, "version_conflict", "调查已被更新，请刷新后重试", retryable=True)
    if inv["status"] == "recovered":
        raise WorkflowError(409, "already_recovered", "调查已恢复，不可重复标记")
    now = _now()
    manual = (inv.get("verification") or "tls") == "manual"
    inv["status"] = "waiting_nms" if manual else "observing"
    inv["manual_completed_at"] = _iso(now)
    inv["manual_action"] = {"category": category, "note": (note or "").strip()[:1000]}
    inv["healthy_streak"] = 0
    inv["streak_signature"] = ""
    inv["streak_first_at"] = None
    inv["streak_last_at"] = None
    inv["clear_evidence"] = None
    inv.pop("manual_clear_first_at", None)
    inv.pop("pause_reason", None)
    inv.pop("paused_recheck", None)
    if manual:
        # 人工核验模式不探测：直接等待 NMS 清除并持续静默 / 不重新入调度
        inv["waiting_since"] = _iso(now)
        inv["next_check_at"] = None
        inv.pop("next_check_ts", None)
        _timeline(inv, "manual_completed", owner,
                  f"人工处置完成（{category}）：{inv['manual_action']['note']}；"
                  f"需 NMS 清除持续 {_manual_quiet_s()} 秒")
        await _save(store, inv, raw)
        await store.drop_due(inv_id)
        return inv
    inv["next_check_at"] = _iso(now + int(getattr(config, "NMS_SSL_CHECK_INTERVAL", 60)))
    inv["next_check_ts"] = now + int(getattr(config, "NMS_SSL_CHECK_INTERVAL", 60))
    _timeline(inv, "manual_completed", owner, f"人工处置完成（{category}）：{inv['manual_action']['note']}")
    await _save(store, inv, raw)
    await store.add_due(inv_id, inv["next_check_ts"])
    return inv


async def pause_investigation(owner: str, inv_id: str, reason: str = "") -> Dict:
    store = await get_store()
    inv, raw = await _load_owned(store, inv_id, owner)
    if inv["status"] in TERMINAL_STATUSES:
        return inv
    if inv["status"] == "waiting_nms":
        _note_waiting_exit(inv, _now(), "paused")
    inv["status"] = "paused"
    inv["paused_at"] = _iso()
    inv["pause_reason"] = (reason or "user_paused")[:200]
    inv["next_check_at"] = None
    _timeline(inv, "paused", owner, inv["pause_reason"])
    await _save(store, inv, raw)
    await store.drop_due(inv_id)
    return inv


async def resume_investigation(owner: str, inv_id: str) -> Dict:
    store = await get_store()
    inv, raw = await _load_owned(store, inv_id, owner)
    if inv["status"] != "paused":
        return inv
    now = _now()
    manual = (inv.get("verification") or "tls") == "manual"
    manual_ts = _parse_iso(inv.get("manual_completed_at"))
    if manual:
        inv["status"] = "waiting_nms" if manual_ts else "awaiting_action"
    else:
        inv["status"] = "observing" if manual_ts else "awaiting_action"
    inv.pop("pause_reason", None)
    inv["healthy_streak"] = 0
    inv["streak_signature"] = ""
    inv["streak_first_at"] = None
    inv["streak_last_at"] = None
    if manual:
        # 人工核验模式不探测：静默窗口从恢复时刻重新计时
        inv["manual_completed_at"] = _iso(now) if manual_ts else None
        inv["next_check_at"] = None
        inv.pop("next_check_ts", None)
        inv.pop("manual_clear_first_at", None)
    elif manual_ts:
        inv["manual_completed_at"] = _iso(now)  # 恢复跟踪重新计时
        inv["next_check_at"] = _iso(now + int(getattr(config, "NMS_SSL_CHECK_INTERVAL", 60)))
        inv["next_check_ts"] = now + int(getattr(config, "NMS_SSL_CHECK_INTERVAL", 60))
        await store.add_due(inv_id, inv["next_check_ts"])
    _timeline(inv, "resumed", owner, "恢复自动跟踪")
    await _save(store, inv, raw)
    return inv


async def recheck(owner: str, inv_id: str) -> Dict:
    store = await get_store()
    inv, raw = await _load_owned(store, inv_id, owner)
    if (inv.get("verification") or "tls") == "manual":
        raise WorkflowError(409, "manual_verification",
                            f"人工核验模式不做自动取证，恢复由 NMS 清除持续 "
                            f"{_manual_quiet_s()} 秒自动判定", retryable=False)
    if inv["status"] in ("recovered",):
        raise WorkflowError(409, "already_recovered", "调查已恢复，无需复核")
    if inv["status"] == "paused":
        inv["paused_recheck"] = True
    _timeline(inv, "recheck_requested", owner, "请求立即复核")
    await _save(store, inv, raw)
    await store.add_due(inv_id, _now())
    return inv


# ═══════════════════════════════════════════════════════════════════════════
# 采集观察：只采信明确的 cleared 行
# ═══════════════════════════════════════════════════════════════════════════

def _target_of_row(alarm: Dict) -> Optional[Dict]:
    # 身份比对不涉及网络探测，不做端口白名单限定（否则非 443 调查永远拿不到清除证据）
    from agents.ssl_check import extract_target_lenient
    try:
        return extract_target_lenient(alarm)
    except Exception:
        return None


def _identity_match(inv: Dict, alarm: Dict) -> bool:
    base = inv.get("alarm") or {}
    if _norm_source(alarm.get("source")) != _norm_source(base.get("source")):
        return False
    a_key = str(alarm.get("event_key") or "")
    b_key = str(base.get("event_key") or "")
    if a_key and b_key:
        return a_key == b_key
    a_grp = str(alarm.get("event_group") or "")
    b_grp = str(base.get("event_group") or "")
    return bool(a_grp and b_grp and a_grp == b_grp)


def match_clear_row(inv: Dict, alarms: List[Dict]) -> Tuple[Optional[Dict], str]:
    """在同一份快照里找原告警的明确清除记录。

    返回 (clear_row | None, 原因)。缺 Serial / source / 事件键 / 目标任一项，
    或者仍存在同身份的活跃记录 → 不算清除。
    """
    target = inv.get("target") or {}
    active_replacement = None
    cleared = []
    for a in alarms:
        if not a.get("serial") or not _identity_match(inv, a):
            continue
        if a.get("serial_source") == "identifier_list":
            continue  # 行内 Serial 缺失：身份不可靠，既不作清除证据也不作活跃替代
        row_target = _target_of_row(a)
        if not row_target or row_target.get("host") != target.get("host") \
                or int(row_target.get("port") or 0) != int(target.get("port") or 0):
            continue
        if a.get("severity") in ACTIVE_SEVERITIES:
            active_replacement = a
        else:
            cleared.append(a)
    if active_replacement is not None:
        return None, "有同身份活跃记录，尚不能判定清除"
    if not cleared:
        return None, "未观察到明确清除记录"
    if len(cleared) > 1:
        # 多条清除记录指向同一身份时可接受，取最新一条；身份不一致在循环内已过滤
        cleared.sort(key=lambda a: str(a.get("last_time") or ""), reverse=True)
    return cleared[0], ""


def _manual_quiet_tick(inv: Dict, now: float) -> None:
    """人工核验模式的静默推进：只看静默时长，不依赖当轮是否仍带清除行。"""
    manual_ts = _parse_iso(inv.get("manual_completed_at"))
    first = _parse_iso(inv.get("manual_clear_first_at"))
    if not manual_ts or not first:
        return  # 未人工处置或未观察到清除：不算闭环起点
    quiet = _manual_quiet_s()
    elapsed = int(now - first)
    if elapsed < quiet:
        inv["nms_clear_note"] = f"清除持续 {elapsed}s/{quiet}s"
        return
    _note_waiting_exit(inv, now, "recovered")
    inv["status"] = "recovered"
    inv["recovered_at"] = _iso(now)
    inv["next_check_at"] = None
    inv["verification_result"] = "manual_clear_quiet"
    _note_recovery(inv, now)
    _timeline(inv, "recovered", "system",
              f"人工核验模式：NMS 清除持续 {elapsed}s（≥{quiet}s）→ 调查终结")


async def observe_snapshot(scope_base: str, alarms: List[Dict],
                           observed_at: Optional[str] = None) -> int:
    """采集完成后调用：为当前 scope 的活跃调查记录观测结果与清除证据。

    空快照/采集失败不推动恢复，只记录"本轮为空"，避免把抓取异常当清除。
    """
    if not _enabled():
        return 0
    try:
        store = await get_store()
        ids = await store.all_inv_ids()
        observed_at = observed_at or _iso()
        touched = 0
        for inv_id in ids:
            raw = await store.get_inv(inv_id)
            if not raw:
                continue
            inv = json.loads(raw)
            now = _now()
            if inv.get("scope_base") != scope_base:
                continue
            if inv.get("status") not in ("observing", "waiting_nms", "awaiting_action"):
                continue
            inv["nms_last_observed_at"] = observed_at
            inv["nms_last_result"] = "rows" if alarms else "empty"
            clear_row, reason = (None, "本轮采集为空，不能作为清除证据")
            if alarms:
                clear_row, reason = match_clear_row(inv, alarms)
            if clear_row:
                prev = (inv.get("clear_evidence") or {}).get("serial")
                inv["clear_evidence"] = {
                    "observed_at": observed_at,
                    "serial": clear_row.get("serial"),
                    "severity": clear_row.get("severity"),
                    "source": clear_row.get("source"),
                    "event_key": clear_row.get("event_key"),
                    "last_time": clear_row.get("last_time"),
                }
                if prev != clear_row.get("serial"):
                    _timeline(inv, "clear_observed", "collector",
                              f"NMS 明确清除记录：serial={clear_row.get('serial')} "
                              f"severity={clear_row.get('severity')}")
                if (inv.get("verification") or "tls") == "manual":
                    manual_ts = _parse_iso(inv.get("manual_completed_at"))
                    first = _parse_iso(inv.get("manual_clear_first_at"))
                    if manual_ts and (not first or first < manual_ts):
                        inv["manual_clear_first_at"] = observed_at
                        _timeline(inv, "clear_streak_start", "collector",
                                  f"观察到 NMS 清除，开始静默计时 {_manual_quiet_s()} 秒")
                elif (inv.get("status") == "waiting_nms"
                        and inv.get("health", {}).get("diagnosis") == "healthy"
                        and _evidence_fresh(inv, now)
                        and _clear_evidence_ok(inv, now)):
                    _note_waiting_exit(inv, now, "recovered")
                    inv["status"] = "recovered"
                    inv["recovered_at"] = _iso(now)
                    inv["next_check_at"] = None
                    _note_recovery(inv, now)
                    _timeline(inv, "recovered", "system",
                              "NMS 清除证据到达且 TLS 实测健康 → 调查终结")
                touched += 1
            else:
                inv["nms_clear_note"] = reason
                if (inv.get("verification") or "tls") == "manual" \
                        and inv.get("manual_clear_first_at") and "活跃" in reason:
                    # 只有「同身份仍有活跃替代」才重置；行自然消失不算反悔
                    inv.pop("manual_clear_first_at", None)
                    _timeline(inv, "clear_reset", "collector",
                              f"清除不再成立，静默重新计时：{reason}")
                touched += 1
            if (inv.get("verification") or "tls") == "manual" and alarms:
                # 仅真实快照（非空）才推进静默计时，避免采集空窗口被当成清除
                _manual_quiet_tick(inv, now)
            if inv.get("status") == "waiting_nms":
                _note_waiting_pending(inv, now)
            try:
                await _save(store, inv, raw)
            except WorkflowError:
                continue
            if inv["status"] in TERMINAL_STATUSES or inv["status"] == "awaiting_action":
                await store.drop_due(inv_id)
        return touched
    except Exception as e:
        logger.warning(f"[nms_ssl_workflow] 采集观察失败（不阻断采集）: {e}")
        return 0


async def observe_collection_error(scope_base: str, error: str,
                                   observed_at: Optional[str] = None) -> int:
    """采集失败/身份不可靠时调用：只记录「本轮采集异常」，绝不推动恢复。

    失败观测不写 clear_evidence、不改变状态机，仅用于前端明确提示采集异常。
    """
    if not _enabled():
        return 0
    try:
        store = await get_store()
        ids = await store.all_inv_ids()
        observed_at = observed_at or _iso()
        touched = 0
        for inv_id in ids:
            raw = await store.get_inv(inv_id)
            if not raw:
                continue
            inv = json.loads(raw)
            if inv.get("scope_base") != scope_base:
                continue
            if inv.get("status") not in ("observing", "waiting_nms", "awaiting_action"):
                continue
            prev = inv.get("nms_last_result")
            inv["nms_last_observed_at"] = observed_at
            inv["nms_last_result"] = "error"
            inv["nms_last_error"] = (error or "采集失败")[:200]
            inv["nms_clear_note"] = "采集异常，不能作为清除证据"
            if prev != "error":
                _timeline(inv, "nms_collect_error", "collector",
                          f"NMS 采集异常：{inv['nms_last_error']}")
            try:
                await _save(store, inv, raw)
            except WorkflowError:
                continue
            touched += 1
        return touched
    except Exception as e:
        logger.warning(f"[nms_ssl_workflow] 采集异常记录失败（不阻断采集）: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════════════════
# 案例库：recovered → 提交 → 审核 → 下次引用
# ═══════════════════════════════════════════════════════════════════════════

async def submit_case(owner: str, inv_id: str, root_cause: str, action: str,
                      conditions: str) -> Dict:
    if not (root_cause or "").strip() or not (action or "").strip():
        raise WorkflowError(422, "case_incomplete", "实际原因与实际操作均为必填")
    store = await get_store()
    inv, raw = await _load_owned(store, inv_id, owner)
    if inv.get("status") != "recovered":
        raise WorkflowError(409, "not_recovered", "只有已恢复的调查可提交案例")
    for case_id in await store.list_case_ids():
        raw = await store.get_case(case_id)
        if not raw:
            continue
        case = json.loads(raw)
        if case.get("investigation_id") == inv_id and case.get("status") in ("pending", "approved"):
            return case
    now = _now()
    version = int(inv.get("case_version") or 0) + 1
    inv["case_version"] = version
    case = {
        "id": uuid.uuid4().hex,
        "investigation_id": inv_id,
        "version": version,
        "status": "pending",
        "owner": owner,
        "scope": inv.get("scope"),
        "scope_base": inv.get("scope_base"),
        "target": inv.get("target"),
        "fault_type": "ssl_certificate",
        "root_cause": root_cause.strip()[:2000],
        "action": action.strip()[:2000],
        "conditions": (conditions or "").strip()[:1000],
        "evidence": {
            "verification": inv.get("verification") or "tls",
            "verification_result": inv.get("verification_result") or "",
            "manual_clear_first_at": inv.get("manual_clear_first_at"),
            "diagnosis": inv.get("diagnosis"),
            "health": inv.get("health"),
            "clear_evidence": inv.get("clear_evidence"),
            "manual_action": inv.get("manual_action"),
            "manual_completed_at": inv.get("manual_completed_at"),
            "recovered_at": inv.get("recovered_at"),
            "rule_version": inv.get("rule_version"),
        },
        "created_at": _iso(now),
        "submitted_by": owner,
        "reviewed_by": "", "reviewed_at": "", "review_note": "",
    }
    await store.save_case(case["id"], json.dumps(case, ensure_ascii=False))
    _timeline(inv, "case_submitted", owner, f"提交成功案例 v{version}（待审核）")
    await _save(store, inv, raw)
    return case


def _is_admin(user: Dict) -> bool:
    return (user or {}).get("role") == "admin"


async def review_case(actor: str, actor_user: Dict, case_id: str, action: str,
                      note: str = "") -> Dict:
    if action not in ("approve", "return"):
        raise WorkflowError(422, "invalid_action", "审核动作非法")
    if not _is_admin(actor_user):
        raise WorkflowError(403, "forbidden", "仅管理员可审核案例")
    store = await get_store()
    raw = await store.get_case(case_id)
    if not raw:
        raise WorkflowError(404, "not_found", "案例不存在")
    case = json.loads(raw)
    if case.get("status") != "pending":
        raise WorkflowError(409, "case_not_pending", "案例不在待审核状态")
    if case.get("owner") == actor:
        raise WorkflowError(403, "self_review", "创建人不得审核自己的案例")
    if action == "return" and not (note or "").strip():
        raise WorkflowError(422, "note_required", "退回需说明原因")
    case["status"] = "approved" if action == "approve" else "returned"
    case["reviewed_by"] = actor
    case["reviewed_at"] = _iso()
    case["review_note"] = (note or "").strip()[:1000]
    await store.save_case(case_id, json.dumps(case, ensure_ascii=False))
    return case


async def revoke_case(actor: str, actor_user: Dict, case_id: str, note: str = "") -> Dict:
    if not (note or "").strip():
        raise WorkflowError(422, "note_required", "撤回需说明原因")
    store = await get_store()
    raw = await store.get_case(case_id)
    if not raw:
        raise WorkflowError(404, "not_found", "案例不存在")
    case = json.loads(raw)
    if case.get("owner") != actor and not _is_admin(actor_user):
        raise WorkflowError(404, "not_found", "案例不存在")
    if case.get("status") not in ("approved", "returned", "pending"):
        raise WorkflowError(409, "case_not_revocable", "案例当前状态不可撤回")
    case["status"] = "revoked"
    case["revoked_by"] = actor
    case["revoked_at"] = _iso()
    case["revoke_note"] = (note or "").strip()[:1000]
    await store.save_case(case_id, json.dumps(case, ensure_ascii=False))
    return case


async def get_case(actor: str, actor_user: Dict, case_id: str) -> Dict:
    store = await get_store()
    raw = await store.get_case(case_id)
    if not raw:
        raise WorkflowError(404, "not_found", "案例不存在")
    case = json.loads(raw)
    if case.get("owner") != actor and not _is_admin(actor_user):
        raise WorkflowError(404, "not_found", "案例不存在")
    return case


async def list_cases(actor: str, actor_user: Dict, status: str = "", limit: int = 50) -> Dict:
    store = await get_store()
    items = []
    for case_id in await store.list_case_ids():
        raw = await store.get_case(case_id)
        if not raw:
            continue
        case = json.loads(raw)
        if case.get("owner") != actor and not _is_admin(actor_user):
            continue
        if status and case.get("status") != status:
            continue
        items.append({k: case.get(k) for k in (
            "id", "investigation_id", "version", "status", "owner", "scope", "target",
            "fault_type", "root_cause", "action", "created_at", "reviewed_by",
            "reviewed_at", "review_note")})
        if len(items) >= limit:
            break
    return {"items": items, "total": len(items)}


async def _referenced_cases(inv: Dict) -> List[Dict]:
    """相同 scope、target、故障类型下最近 3 条已批准案例（不含自己）。"""
    store = await get_store()
    out = []
    target = inv.get("target") or {}
    for case_id in await store.list_case_ids():
        raw = await store.get_case(case_id)
        if not raw:
            continue
        case = json.loads(raw)
        if case.get("status") != "approved" or case.get("investigation_id") == inv.get("id"):
            continue
        if case.get("fault_type") != "ssl_certificate":
            continue
        if (case.get("target") or {}).get("host") != target.get("host"):
            continue
        if case.get("scope") == "personal" and case.get("owner") != inv.get("owner"):
            continue
        if case.get("scope") == "global" and case.get("scope_base") != inv.get("scope_base"):
            continue
        out.append({k: case.get(k) for k in (
            "id", "investigation_id", "version", "owner", "target", "root_cause",
            "action", "conditions", "created_at", "reviewed_by", "reviewed_at")})
        if len(out) >= 3:
            break
    return out


async def add_feedback(owner: str, inv_id: str, helpful: bool, note: str = "") -> Dict:
    store = await get_store()
    inv, raw = await _load_owned(store, inv_id, owner)
    inv.setdefault("feedback", []).append({
        "at": _iso(), "by": owner, "helpful": bool(helpful), "note": (note or "").strip()[:1000],
    })
    _timeline(inv, "feedback", owner, "反馈：有帮助" if helpful else "反馈：未解决/建议有误")
    await _save(store, inv, raw)
    return inv


# ═══════════════════════════════════════════════════════════════════════════
# 后台 worker：每 60s 复核，连续失败按 60/120/240/300 退避
# ═══════════════════════════════════════════════════════════════════════════

_retry_counts: Dict[str, int] = {}


async def _lease_heartbeat(store, inv_id: str, token: str, lost: asyncio.Event) -> None:
    """60 秒租约每 15 秒续租；续租失败立即置 lost，探测须终止且旧响应不得提交。"""
    while True:
        await asyncio.sleep(LEASE_RENEW_SECONDS)
        ok = False
        exc = ""
        try:
            ok = await store.renew_lease(inv_id, token, LEASE_TTL_SECONDS)
        except Exception as e:  # 存储抖动按续租失败处理，不能无声丢掉围栏语义
            exc = f"{type(e).__name__}: {e}"
        fails = record_lease_renew(ok)
        if not ok:
            logger.warning(f"[nms_ssl_metrics] 租约续租失败 id={inv_id} 累计失败={fails} 次"
                           + (f" 异常={exc}" if exc else ""))
            lost.set()
            return


async def _run_one(store, inv_id: str) -> bool:
    raw = await store.get_inv(inv_id)
    if not raw:
        await store.drop_due(inv_id)
        return False
    inv = json.loads(raw)
    if (inv.get("verification") or "tls") == "manual":
        # 人工核验模式不探测，恢复由采集侧静默计时推进
        await store.drop_due(inv_id)
        return False
    if inv.get("status") == "paused" and inv.get("paused_recheck"):
        pass  # 暂停状态只允许手动触发的一次性复核
    elif inv.get("status") not in SCHEDULED_STATUSES:
        await store.drop_due(inv_id)
        return False
    token = uuid.uuid4().hex
    if not await store.acquire_lease(inv_id, token, LEASE_TTL_SECONDS):
        record_lease_busy()
        return False
    slot = await store.acquire_slot(
        token, 90, int(getattr(config, "NMS_SSL_GLOBAL_CONCURRENCY", 2)))
    if slot is None:
        record_slot_full()
        await store.release_lease(inv_id, token)
        await store.add_due(inv_id, _now() + 5)
        return False
    lost = asyncio.Event()
    beat: Optional[asyncio.Task] = asyncio.create_task(
        _lease_heartbeat(store, inv_id, token, lost))
    probe_task: Optional[asyncio.Task] = None
    lost_wait: Optional[asyncio.Task] = None
    try:
        from agents.ssl_check import probe_cert
        probe_started = _now()
        probe_task = asyncio.create_task(probe_cert(inv.get("target") or {}, probe_policy()))
        lost_wait = asyncio.create_task(lost.wait())
        await asyncio.wait({probe_task, lost_wait}, return_when=asyncio.FIRST_COMPLETED)
        if lost.is_set():
            lost_total = record_lease_lost()
            discarded = record_probe_discarded()
            # 失去围栏令牌：终止探测，本轮响应一律丢弃
            probe_task.cancel()
            with contextlib.suppress(BaseException):
                await probe_task
            logger.warning(f"[nms_ssl_workflow] 调查 {inv_id} 租约丢失，终止探测且不提交 "
                           f"(累计丢租={lost_total}, 丢弃探测={discarded})")
            return False
        result = probe_task.result()
        _note_probe(result, _now() - probe_started, inv_id)
        # 提交前再确认令牌有效性，避免续租过期后写入旧证据
        renewed = await store.renew_lease(inv_id, token, LEASE_TTL_SECONDS)
        fails = record_lease_renew(renewed)
        if not renewed:
            discarded = record_probe_discarded()
            logger.warning(f"[nms_ssl_metrics] 提交前租约失效 id={inv_id} "
                           f"累计失败={fails} 次，丢弃本轮结果 (discarded={discarded})")
            return False
        updated = apply_probe_result(inv, result, _now())
        await _save(store, updated, raw)
        if updated.get("status") in TERMINAL_STATUSES or updated.get("status") == "awaiting_action":
            await store.drop_due(inv_id)
        elif updated.get("next_check_ts"):
            await store.add_due(inv_id, float(updated["next_check_ts"]))
        backoff = [60, 120, 240, 300]
        if result.get("diagnosis") == "unknown":
            n = min(_retry_counts.get(inv_id, 0), len(backoff) - 1)
            _retry_counts[inv_id] = n + 1
            await store.add_due(inv_id, _now() + backoff[n])
        else:
            _retry_counts.pop(inv_id, None)
        return True
    except WorkflowError as e:
        logger.info(f"[nms_ssl_workflow] 调查 {inv_id} 保存冲突: {e.message}")
        return False
    except Exception as e:
        logger.warning(f"[nms_ssl_workflow] 调查 {inv_id} 复核失败: {e}")
        n = min(_retry_counts.get(inv_id, 0), 3)
        _retry_counts[inv_id] = n + 1
        await store.add_due(inv_id, _now() + [60, 120, 240, 300][n])
        return False
    finally:
        for t in (probe_task, lost_wait, beat):
            if t is not None and not t.done():
                t.cancel()
        await asyncio.gather(
            *[t for t in (probe_task, lost_wait, beat) if t is not None],
            return_exceptions=True)
        await store.release_slot(slot, token)
        await store.release_lease(inv_id, token)


async def run_due_once(limit: int = 10) -> int:
    if not _enabled():
        return 0
    store = await get_store()
    ids = await store.due_ids(_now(), limit)
    record_worker_cycle(len(ids))
    if not ids:
        return 0
    results = await asyncio.gather(*[_run_one(store, i) for i in ids], return_exceptions=True)
    return sum(1 for r in results if r is True)


async def worker_loop() -> None:
    """常驻后台：15s 扫描到期调查；用户关页面不影响跟踪。"""
    logger.info("[nms_ssl_workflow] SSL 调查后台已启动")
    while True:
        try:
            await run_due_once()
            await log_metrics_summary_async(await get_store())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[nms_ssl_workflow] worker 循环异常（继续）: {e}")
        await asyncio.sleep(15)
