#!/usr/bin/env python3
"""从审计日志恢复已过期的审批单 (一次性修复: 原设计 Redis TTL 24h, 历史单据到期消失)

数据源: zops:audit:YYYY-MM-DD (保留 90 天) 中的 approval_* 事件。
重建字段: approval_id / tool_name / username / status(终态) / created_ts /
         category / site / case_no / summary(审计摘要) / restored=1 / TTL 365d。
字段不全的以审计摘要兜底, 保证历史列表可见; 决策细节仍以审计轨迹为准。
"""
import asyncio, json, re, sys, time

sys.path.insert(0, "/opt/zops")
import config


async def main():
    import redis.asyncio as aioredis
    r = aioredis.from_url(config.REDIS_URL, decode_responses=True)

    # 1) 收集全部审批事件 (按单聚合)
    events: dict = {}
    audit_keys = [k async for k in r.scan_iter(match=f"{config.REDIS_PREFIX}:audit:*", count=200)]
    print(f"审计键: {len(audit_keys)} 个")
    for k in audit_keys:
        for raw in await r.lrange(k, 0, -1):
            try:
                e = json.loads(raw)
            except Exception:
                continue
            if not str(e.get("action") or "").startswith("approval"):
                continue
            aid = str((e.get("params") or {}).get("approval_id") or "")
            if not aid:
                continue
            events.setdefault(aid, []).append(e)
    print(f"审计中出现过的审批单: {len(events)} 张")

    # 2) 重建缺失单据
    restored = 0
    for aid, evs in events.items():
        if await r.exists(f"{config.REDIS_PREFIX}:approval:{aid}"):
            continue  # 单据仍在, 不动
        evs.sort(key=lambda x: float(x.get("timestamp") or 0))
        first = evs[0]
        created_ts = float(first.get("timestamp") or 0)
        # 终态: 最后一个状态类事件
        status = "timeout"  # 只有 created → 已过期
        for e in evs:
            act = e.get("action") or ""
            if act == "approval_approved":
                status = "approved"
            elif act == "approval_rejected":
                status = "rejected"
            elif act == "approval_cancelled":
                status = "cancelled"
            elif act == "approval_decision" and "approved" in (e.get("result_summary") or ""):
                status = "approved"
            elif act == "approval_decision":
                status = "rejected"
        summary0 = first.get("result_summary") or ""
        tool = first.get("tool_name") or ""
        if tool.startswith("CITP"):
            tool = ""  # CITP 事件的 tool_name 是编号, 不是操作
        username = first.get("username") or ""
        category = "maintenance" if "维护单" in summary0 else "daily"
        m_site = re.search(r"site=([^;)]+)", summary0)
        m_case = re.search(r"case=([^;)]+)", summary0)
        rec = {
            "approval_id": aid,
            "tool_name": tool or "(审计恢复)",
            "params": "{}",
            "agent": "manual",
            "username": username,
            "risk_note": "",
            "status": status,
            "created_ts": created_ts,
            "decided_ts": float(evs[-1].get("timestamp") or 0) if status in ("approved", "rejected") else "",
            "approver": next((e.get("username") for e in reversed(evs)
                              if e.get("action") in ("approval_decision", "approval_approved",
                                                     "approval_rejected")), ""),
            "category": category,
            "site": m_site.group(1) if m_site else "",
            "case_no": m_case.group(1) if m_case else "",
            "summary": f"[审计恢复] {summary0[:200]}",
            "restored": "1",
            "restored_ts": time.time(),
        }
        await r.hset(f"{config.REDIS_PREFIX}:approval:{aid}", mapping=rec)
        await r.expire(f"{config.REDIS_PREFIX}:approval:{aid}", 365 * 86400)
        restored += 1
    print(f"恢复完成: {restored} 张 (其余 {len(events) - restored} 张仍在库)")
    # 3) 存量单据 TTL 统一续到 365 天
    extended = 0
    async for k in r.scan_iter(match=f"{config.REDIS_PREFIX}:approval:*", count=100):
        await r.expire(k, 365 * 86400)
        extended += 1
    print(f"存量单据 TTL 已续期 365 天: {extended} 张")

asyncio.run(main())
