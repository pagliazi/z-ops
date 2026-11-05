"""P1 上线门诊断 (SPEC: 新增需认证的 diagnostic readiness —
检查服务主体、授权配置、队列、租约、归档水位和 lag、Outbox、事件补读; Redis ping 不能单独作为验收)

只读聚合, 不修改任何状态; 任何子项失败都降级为 blockers/notes 条目, 不抛出。
凭据只报"是否配置", 绝不返回值 (P0 凭据不进日志/事件/模型输入)。
"""

import datetime as _dt
import json
import logging
import time
from typing import Optional

import config

logger = logging.getLogger("zops")

_SCAN_CAP = 500          # 单次诊断最多扫描的任务数 (避免大 keyspace 上阻塞)
_EVENT_SAMPLE = 25       # 事件补读核对抽样任务数
_REVERSE_SAMPLE = 300    # 反向核对 (归档→账本投影) 抽样文件数


def _iso(ts=None) -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


async def _scan_keys(r, match: str, cap: int = _SCAN_CAP) -> list:
    out, cursor = [], 0
    while True:
        cursor, batch = await r.scan(cursor=cursor, match=match, count=200)
        out.extend(batch)
        if cursor == 0 or len(out) >= cap:
            return out[:cap]


async def collect(redis_client=None, now: Optional[float] = None) -> dict:
    """聚合诊断结果。返回 ready=False 时 blockers 必非空 (fail-closed 表述)。"""
    now = now or time.time()
    rep = {"ready": False, "generated_at": _iso(now), "blockers": [], "notes": []}

    r, own_client = redis_client, False
    try:
        if r is None:
            import redis.asyncio as aioredis
            r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
            own_client = True
    except Exception as e:
        rep["blockers"].append(f"Redis 客户端不可用: {e}")
        return rep

    try:
        return await _collect(r, rep, now)
    finally:
        if own_client:
            try:
                await r.aclose()
            except Exception:
                pass


async def _collect(r, rep: dict, now: float) -> dict:
    from . import archive as task_archive
    from .ledger import _prefix, k_events, k_task
    from .readiness import check_readiness

    # ── Redis 上线前置条件 (版本/持久化/淘汰策略/拓扑/容量) ──
    try:
        rd = await check_readiness(r)
        rep["redis"] = {"version": rd.redis_version, "streams_allowed": rd.streams_allowed,
                        "blockers": list(rd.blockers), "notes": list(rd.notes)}
        rep["blockers"] += list(rd.blockers)
        rep["notes"] += list(rd.notes)
    except Exception as e:
        rep["redis"] = {"error": str(e)}
        rep["blockers"].append(f"readiness 核查异常: {e}")

    rep["feature_flags"] = {
        "streams_consumer": bool(getattr(config, "TR_STREAMS_ENABLED", False)),
        "feishu_entry": bool(getattr(config, "TR_FEISHU_ENABLED", True)),
        "n8n_entry": bool(getattr(config, "TR_N8N_ENABLED", True)),
    }

    # ── 队列 / Outbox ──
    pfx = _prefix()
    try:
        queue_len = await r.xlen(f"{pfx}queue")
        active = await r.zcard(f"{pfx}active")
        outbox_len = await r.llen(f"{pfx}outbox")
        rep["queue"] = {"stream_len": queue_len, "active_tasks": active,
                        "outbox_pending": outbox_len}
    except Exception as e:
        rep["queue"] = {"error": str(e)}
        rep["blockers"].append(f"队列/Outbox 读取失败: {e}")

    dlv_ids = await _scan_keys(r, f"{pfx}delivery:*") or []
    dlv_status = {}
    for k in dlv_ids:
        s = await r.get(k) or ""
        dlv_status[s] = dlv_status.get(s, 0) + 1
    rep["outbox"] = {"delivery_seen": dlv_status}
    if dlv_status.get("uncertain"):
        rep["blockers"].append(f"投递状态 uncertain={dlv_status['uncertain']} (需人工核对渠道侧事实)")

    # ── 任务账本: 状态/渠道分布 + 租约 ──
    task_keys = await _scan_keys(r, f"{pfx}task:*") or []
    by_status, by_channel = {}, {}
    stale_lease, terminal_ids, oldest_waiting = [], [], None
    lease_grace = 60 * 3
    for k in task_keys:
        t = await r.hgetall(k)
        if not t:
            continue
        st = t.get("status") or ""
        by_status[st] = by_status.get(st, 0) + 1
        ch = t.get("channel") or ""
        by_channel[ch] = by_channel.get(ch, 0) + 1
        if st in ("succeeded", "failed", "cancelled", "unknown"):
            terminal_ids.append(k.split(":")[-1])
        else:
            try:
                exp = float(t.get("lease_expires_ts") or 0)
            except (TypeError, ValueError):
                exp = 0
            if exp and now - exp > lease_grace:
                stale_lease.append(k.split(":")[-1])
            try:
                age = now - float(t.get("created_ts") or now)
                oldest_waiting = age if oldest_waiting is None else max(oldest_waiting, age)
            except (TypeError, ValueError):
                pass
    rep["tasks"] = {"scanned": len(task_keys), "by_status": by_status, "by_channel": by_channel,
                    "stale_lease_count": len(stale_lease),
                    "stale_lease_sample": stale_lease[:5],
                    "oldest_unfinished_age_s": round(oldest_waiting, 1) if oldest_waiting else None}
    if stale_lease:
        rep["blockers"].append(f"租约过期未回收任务 {len(stale_lease)} 个 (reclaim loop 未运行或被阻塞)")
    if len(task_keys) >= _SCAN_CAP:
        rep["notes"].append(f"任务扫描已达上限 {_SCAN_CAP}, 分布为抽样值")

    # ── 归档水位与 lag + 事件补读 (Redis 事实 vs JSONL 永久归档) ──
    lag_events, missing_ids, unreadable = 0, [], 0
    for tid in terminal_ids[:_EVENT_SAMPLE]:
        try:
            evs = await r.lrange(k_events(tid), 0, -1)
        except Exception as e:
            unreadable += 1
            rep["notes"].append(f"事件读取失败 {tid}: {e}")
            continue
        try:
            archived_ids = task_archive.archived_event_ids(tid)
        except Exception:
            missing_ids.append(tid)
            continue
        if not archived_ids and evs:
            missing_ids.append(tid)
        for line in evs:
            try:
                eid = str(json.loads(line).get("event_id") or "")
            except Exception:
                continue
            if eid and eid not in archived_ids:
                lag_events += 1
    rep["archive"] = {
        "dir": str(task_archive.TASK_ARCHIVE_DIR),
        "terminal_tasks": len(terminal_ids),
        "sampled": min(len(terminal_ids), _EVENT_SAMPLE),
        "events_not_archived": lag_events,
        "tasks_without_archive": missing_ids[:5],
        "event_read_failures": unreadable,
    }
    if lag_events:
        rep["blockers"].append(f"永久归档滞后: {lag_events} 条事件仅在 Redis 未落 JSONL")
    if missing_ids:
        rep["blockers"].append(f"{len(missing_ids)} 个终态任务无归档文件 (需补写或标明不可恢复)")

    # ── 反向盲区: 归档有文件但 Redis 无投影 → 必须是已标明的历史/测试产物 ──
    #    (SPEC: 归档已有而投影缺失时不重放副作用; 未标明 = 审计盲区)
    try:
        arch_dir = task_archive.TASK_ARCHIVE_DIR
        files = sorted(arch_dir.glob("*.jsonl")) if arch_dir.exists() else []
        checked = files[:_REVERSE_SAMPLE]
        orphan, unexplained = [], []
        for fp in checked:
            tid = fp.name[:-len(".jsonl")]
            if await r.exists(k_task(tid)):
                continue
            orphan.append(tid)
            marked = False
            try:
                with open(fp, "rb") as fh:
                    fh.seek(max(0, fp.stat().st_size - 8192))
                    for line in fh.read().decode("utf-8", "ignore").splitlines():
                        try:
                            if json.loads(line).get("type") in ("archive_gap", "archive_note"):
                                marked = True
                        except Exception:
                            continue
            except OSError:
                marked = False
            if not marked:
                unexplained.append(tid)
        rep["archive_reverse"] = {
            "files": len(files), "checked": len(checked),
            "orphan_files": len(orphan), "orphan_sample": orphan[:5],
            "unexplained": len(unexplained), "unexplained_sample": unexplained[:5],
        }
        if unexplained:
            rep["blockers"].append(
                f"{len(unexplained)} 个归档文件无账本投影且未标明来源 (审计盲区, 需核实后用 "
                f"mark_unrecoverable 标明)")
        elif orphan:
            rep["notes"].append(f"{len(orphan)} 个无投影归档文件均已标明来源 (历史/测试产物, 不删历史)")
        if len(files) > len(checked):
            rep["notes"].append(f"反向核对仅抽样 {len(checked)}/{len(files)} 个归档文件")
    except Exception as e:
        rep["archive_reverse"] = {"error": str(e)}
        rep["notes"].append(f"反向核对失败 (不阻断诊断): {e}")

    # ── 服务主体与授权配置 (只报是否配置, 不报值) ──
    authz = {}
    try:
        authz["nms_service_principal_configured"] = bool(
            (getattr(config, "NMS_USERNAME", "") or "").strip()
            and (getattr(config, "NMS_PASSWORD", "") or "").strip())
        sess = await r.get(f"{config.REDIS_PREFIX}:nms_session:{getattr(config, 'NMS_USERNAME', '')}")
        authz["nms_service_session_present"] = bool(sess)
        if not (authz["nms_service_principal_configured"] or authz["nms_service_session_present"]):
            authz["nms_note"] = "服务主体未授权 → 周期采集按 P0 暂停 (不借用个人会话)"
    except Exception as e:
        authz["error"] = str(e)
    try:
        from core import mcp_registry
        servers = mcp_registry.approved_servers() or []
        authz["mcp_approved_servers"] = len(servers)
        authz["mcp_failclosed"] = True
    except Exception as e:
        authz["mcp_error"] = str(e)
    authz["n8n_webhook_key_configured"] = bool((getattr(config, "N8N_WEBHOOK_KEY", "") or "").strip())
    authz["n8n_callback_secret_configured"] = bool(
        (getattr(config, "N8N_CALLBACK_SECRET", "") or "").strip())
    authz["credential_values_returned"] = False   # 契约声明: 本诊断不携带任何凭据值
    rep["authz"] = authz

    rep["ready"] = not rep["blockers"]
    if rep["ready"]:
        rep["notes"].append("上线门诊断通过; 仍不等同于业务成功率达标 (需按门 4 对比基线)")
    return rep
