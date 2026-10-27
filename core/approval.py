"""
Z-OPS v4.0 Phase 4.1/4.2 — 风险分级权限管线 + 审批门
借鉴 claude-code 权限管线 (pre-filter → hook → 规则 → 审批) 与 Aurora L4 人审 remediation:

风险三级制 (ToolDef.risk_level, Phase 4.1):
    read        → 直接放行
    write       → 记录审计后放行
    destructive → 挂起进入审批门 (Phase 4.2)

审批门 (飞书通知 + API 决策, Phase 4.2) — 审批只走流程 (v4.0.2):
    destructive 工具调用 → 一律立即拦截 (系统绝不自动执行) → 创建审批单 (Redis)
    → 飞书交互卡片/审批面板展示。管理员「批准/拒绝」仅是流程决策
    (写审计日志 who/when/what/decision, 永久保留), 不会触发自动执行;
    实际执行由运维人员按变更流程线下完成, 发起方收到明确拦截提示。

决策入口 (三选一, 写同一张审批单):
    1. 群内卡片按钮 — 应用群模式下卡片带「同意/拒绝」按钮,
       card.action.trigger 回调 → feishu_bot.handle_card_action 直接决策 (推荐)
    2. 管理 API — POST /api/approvals/{id}/decide (Webhook 降级模式/管理面板)
    3. 审批面板 — admin 页 Approvals 列表操作
"""

import json
import time
import uuid
import logging
import asyncio
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

_PENDING = "pending"
_APPROVED = "approved"
_REJECTED = "rejected"
_CANCELLED = "cancelled"

_audit_cache: dict = {}   # 审计 logger 单例 (带异步 Redis, 事件落 zops:audit:* 供查询)


def _key(approval_id: str) -> str:
    return f"{config.REDIS_PREFIX}:approval:{approval_id}"


# 列表索引 (O(1) 定位, 替代全键扫描; 365 天保留下键持续增长, 扫描会线性变慢)
def _idx_pending() -> str:
    return f"{config.REDIS_PREFIX}:approvals:pending"


def _idx_user(username: str) -> str:
    return f"{config.REDIS_PREFIX}:approvals:user:{username}"


def _idx_add(record: dict):
    """创建/迁移时登记索引 (pending + 用户; 失败不阻断)"""
    try:
        r = _get_redis()
        pipe = r.pipeline()
        pipe.sadd(_idx_pending(), record["approval_id"])
        pipe.expire(_idx_pending(), 400 * 86400)
        if record.get("username"):
            pipe.sadd(_idx_user(record["username"]), record["approval_id"])
            pipe.expire(_idx_user(record["username"]), 400 * 86400)
        pipe.execute()
    except Exception as e:
        logger.debug(f"[approval] 索引登记失败 (不阻断): {e}")


# ═══ Phase 4.1: action 级风险规则 (AgentMessage action 路径, 补充 ToolDef.risk_level) ═══
# vm_power/batch_exec 等高危操作走 action 处理器 (不经 QueryEngine 工具循环),
# 在 BaseAgent._safe_handle 入口统一拦截。判定函数按 params 细分 (status 查询=read)。


def _vm_power_risk(params: dict) -> str:
    action = (params.get("power_action") or "status").lower()
    if action in ("shutdown", "reboot", "destroy", "poweroff", "reset"):
        return "destructive"
    return "read"  # status / start


def _vm_snapshot_risk(params: dict) -> str:
    sa = (params.get("snap_action") or "create").lower()
    if sa in ("revert", "delete", "remove"):
        return "destructive"
    if sa in ("create", ""):
        return "write"
    return "read"  # list


def _vm_migrate_risk(params: dict) -> str:
    # 仅当同时指定 vm 与目标宿主机才是真迁移; 纯查询迁移记录 = read
    if params.get("name") or params.get("vm") and params.get("target_host"):
        return "destructive"
    return "read"


# (agent, action) → risk 或 判定函数(params)->risk; 未列出 = read
ACTION_RISK_RULES = {
    ("vm_agent", "vm_power"): _vm_power_risk,
    ("vm_agent", "vm_snapshot"): _vm_snapshot_risk,
    ("vm_agent", "vm_migrate"): _vm_migrate_risk,
    ("vm_agent", "n8n_vm_power"): _vm_power_risk,
    ("ops_agent", "batch_exec"): "destructive",
    ("ops_agent", "emergency_reboot"): "destructive",
    ("dev_agent", "script_exec"): "write",
    ("dev_agent", "ansible_run"): "write",
}


def assess_action_risk(agent: str, action: str, params: dict = None) -> str:
    """评估 action 风险级别: read / write / destructive"""
    rule = ACTION_RISK_RULES.get((agent, action))
    if rule is None:
        return "read"
    return rule(params or {}) if callable(rule) else rule


def _get_redis():
    import redis
    return redis.from_url(config.REDIS_URL, decode_responses=True)


def _audit_async(entry: dict, action: str = "approval_decision", summary: str = ""):
    """决策审计异步落盘 (永久保留; 失败不阻断主流程)

    审计事件写入 Redis 按日分片 (zops:audit:YYYY-MM-DD, 保留 90 天),
    供 /logs 运维日志与单据流程轨迹 (/api/approvals/{id}/history) 查询。
    """
    try:
        from core.audit import AuditLogger, AuditEntry
        loop = asyncio.get_running_loop()

        if "alog" not in _audit_cache:
            import redis.asyncio as _aioredis
            _audit_cache["alog"] = AuditLogger(
                redis_client=_aioredis.from_url(config.REDIS_URL, decode_responses=True))

        async def _write():
            try:
                alog = _audit_cache["alog"]
                await alog.log(
                    agent_id=entry.get("agent", ""),
                    action=action,
                    tool_name=entry.get("tool_name", ""),
                    username=entry.get("approver") or entry.get("username", ""),
                    channel="approval",
                    session_id=entry.get("session_id", ""),
                    params={"approval_id": entry.get("approval_id"),
                            "tool_params": entry.get("params"),
                            "risk_note": entry.get("risk_note", "")},
                    result_summary=summary or f"{entry.get('status')} (操作发起人: {entry.get('username', '-')})",
                    success=entry.get("status") == _APPROVED,
                    msg_id=entry.get("msg_id", ""),
                )
            except Exception as e:
                logger.warning(f"[approval] 审计写入失败: {e}")

        loop.create_task(_write())
    except RuntimeError:
        pass


def create_approval(tool_name: str, params: dict, agent: str = "",
                    session_id: str = "", msg_id: str = "", username: str = "",
                    risk_note: str = "", site: str = "", service_type: str = "",
                    case_no: str = "", priority: str = "", planned_ts: str = "",
                    impact_scope: str = "", rollback_plan: str = "",
                    change_window: str = "", customer: str = "",
                    customer_gid: str = "", original_approval_id: str = "",
                    summary: str = "", approvers: str = "",
                    approver_ds: str = "", approver_tech: str = "",
                    approver_admin: str = "", target_host: str = "",
                    related_alerts: str = "", category: str = "",
                    citp: str = "", customer_site_ids: str = "",
                    customer_order_no: str = "", group: str = "") -> dict:
    """创建审批单并推送飞书卡片通知 (同步 Redis, 毫秒级)

    site/service_type/case_no/priority/planned_ts/impact_scope/rollback_plan/
    change_window/customer/summary/approvers 为用户申请的业务字段
    (agent 拦截单也可附带, 均为可选; 空值不入 Redis)。
    summary 为申请事由 (自然语言一句话), approvers 为指定审批人 (逗号分隔)。
    category 为审批分类 (daily=日常配置 / maintenance=维护, 空默认 daily);
    citp 为维护审批的 CITP 快照 (JSON 字符串: 设备匹配/客户影响/测试计划)。"""
    approval_id = f"apr_{uuid.uuid4().hex[:10]}"
    record = {
        "approval_id": approval_id,
        "tool_name": tool_name,
        "params": json.dumps(params, ensure_ascii=False)[:2000],
        "agent": agent,
        "session_id": session_id,
        "msg_id": msg_id,
        "username": username,
        "risk_note": risk_note or f"工具 {tool_name} 属于破坏性/不可逆操作 (destructive)",
        "status": _PENDING,
        "created_ts": time.time(),
        "decided_ts": "",
        "approver": "",
    }
    for k, v in (("site", site), ("service_type", service_type), ("case_no", case_no),
                 ("priority", priority), ("planned_ts", planned_ts),
                 ("impact_scope", impact_scope), ("rollback_plan", rollback_plan),
                 ("change_window", change_window), ("customer", customer),
                 ("customer_gid", customer_gid),
                 ("original_approval_id", original_approval_id),
                 ("summary", summary), ("approvers", approvers),
                 ("approver_ds", approver_ds), ("approver_tech", approver_tech),
                 ("approver_admin", approver_admin), ("target_host", target_host),
                 ("customer_site_ids", customer_site_ids),
                 ("customer_order_no", customer_order_no),
                 ("group", group),
                 ("category", category if category in APPROVAL_CATEGORIES else "daily")):
        if v:
            record[k] = str(v)[:500]
    if citp:  # CITP 快照 (维护审批; JSON, 截断 6000)
        record["citp"] = str(citp)[:6000]
    if related_alerts:  # 关联 NMS 告警 (JSON 数组, 勾选快照摘要, 截断 2000)
        record["related_alerts"] = str(related_alerts)[:2000]
    try:
        r = _get_redis()
        r.hset(_key(approval_id), mapping=record)
        r.expire(_key(approval_id), 365 * 86400)  # 审批单保留 1 年 (历史可查; 决策审计日志永久保留)
    except Exception as e:
        # P0: 持久化失败必须显式标记 — 调用方不得虚报「审批单已创建」
        logger.error(f"[approval] 审批单写入失败: {e}")
        record["_persist_failed"] = True
        record["_persist_error"] = str(e)[:300]
        return record

    _idx_add(record)
    _notify_feishu(record)
    _audit_async(record, action="approval_created", summary=(
        f"created (申请人 {record.get('username') or '-'} 提交{'维护' if record.get('category') == 'maintenance' else '日常配置'}单 "
        f"{record.get('tool_name')}; site={record.get('site') or '-'}; case={record.get('case_no') or '-'})"))
    logger.warning(f"[approval] 已创建审批单 {approval_id}: {tool_name} "
                   f"(user={username or '-'}, agent={agent}) — 已拦截走审批流程")
    return record


def _notify_feishu(record: dict):
    """推送审批卡片 (notify:feishu 频道 → FeishuBot)

    应用群模式: bot 据 extra.approval 渲染带「同意/拒绝」按钮的交互卡片,
    按钮回调 (card.action.trigger) 直接决策写回本审批单 (流程留痕, 不触发执行);
    Webhook 降级: 纯文本 + 管理 API/面板决策入口。"""
    try:
        r = _get_redis()
        p = json.loads(record.get("params") or "{}")
        params_preview = json.dumps(p, ensure_ascii=False)[:300]
        cat_label = APPROVAL_CATEGORIES.get(record.get("category") or "daily", "日常配置审批")
        # CITP 摘要 (维护审批): 设备匹配 + 客户影响一句话
        citp_line = ""
        citp_devices = ""
        try:
            citp = json.loads(record.get("citp") or "{}") if record.get("citp") else {}
            citp_no = str(citp.get("citp_no") or citp.get("reference_number") or "")
            if citp:
                devs = citp.get("devices") or ([citp["device"]] if citp.get("device") else [])
                ok = [d for d in devs if d.get("matched")]
                citp_devices = (f"{len(ok)}/{len(devs)} 台设备已匹配资源管理系统"
                                + (f" ({', '.join(d.get('name') or d.get('target', '') for d in ok[:3])}"
                                   + ('…' if len(ok) > 3 else '') if ok else ""))
                # 新模板结构: customers 列表 + vms_count; 旧结构: customer_impact
                custs = citp.get("customers") or list((citp.get("customer_impact") or {}).get("customers") or {})
                vms = citp.get("vms_count") or (citp.get("customer_impact") or {}).get("vms") or 0
                if vms or custs:
                    citp_line = (f"**CITP**: {citp_no or '-'} · {citp_devices}"
                                 + (f" · 影响 VM {vms} 台" if vms else "")
                                 + (f" · 客户 {', '.join(str(c.get('name', c) if isinstance(c, dict) else c) for c in list(custs)[:3])}" if custs else ""))
                else:
                    citp_line = f"**CITP**: {citp_no or '-'} · {citp_devices or '未匹配到设备'}"
        except Exception:
            citp_line = ""
        text = (f"⚠️ **destructive 操作已拦截 · 审批只走流程**\n"
                f"**审批分类**: {cat_label}\n"
                f"**工具**: {record['tool_name']}\n"
                f"**发起人**: {record.get('username') or '-'}\n"
                f"**Agent**: {record.get('agent') or '-'}\n"
                f"**申请事由**: {record.get('summary') or '-'}\n"
                f"**客户**: {record.get('customer') or '-'}\n"
                f"**站点**: {record.get('site') or '-'}\n"
                f"**服务类型**: {record.get('service_type') or '-'}\n"
                f"**Case 号**: {record.get('case_no') or '-'}\n"
                f"**优先级**: {record.get('priority') or '-'}\n"
                f"**变更窗口**: {record.get('change_window') or '-'}\n"
                f"**参数**: `{params_preview}`\n"
                f"**影响范围**: {record.get('impact_scope') or '-'}\n"
                f"**回退方案**: {record.get('rollback_plan') or '-'}\n"
                f"**风险说明**: {record.get('risk_note', '')}\n"
                + (f"{citp_line}\n" if citp_line else "")
                + f"**审批单号**: `{record['approval_id']}`\n"
                f"系统不会自动执行该操作；批准/拒绝仅作流程留痕（审计永久保留），\n"
                f"实际执行由运维人员按变更流程线下完成。\n"
                f"决策入口: 审批面板或 "
                f"`POST /api/approvals/{record['approval_id']}/decide` "
                f'body: {{"decision": "approve"}} 或 {{"decision": "reject"}}')
        # CITP 实质信息 (维护单): 故障类型/时间窗/设备 SN·IP/受影响客户
        citp_info: dict = {}
        try:
            _c = json.loads(record.get("citp") or "{}") if record.get("citp") else {}
            if _c:
                _devs = [d for d in (_c.get("devices") or ([_c["device"]] if _c.get("device") else []))
                         if d.get("matched")]
                citp_info = {
                    "citp_fault": _c.get("fault_type") or "",
                    "citp_window": _c.get("window") or "",
                    "citp_devs": "; ".join(
                        f"{d.get('name') or d.get('target')}(SN:{d.get('sn') or '-'},{d.get('ip') or '-'})"
                        for d in _devs[:3]) or "",
                    "citp_vms": _c.get("vms_count") or 0,
                    "citp_custs": len(_c.get("customers") or []),
                }
        except Exception:
            citp_info = {}
        _rel_n = 0
        try:
            _rel_n = len(json.loads(record.get("related_alerts") or "[]"))
        except Exception:
            pass
        r.publish(config.channel("notify:feishu"), json.dumps({
            "text": text, "topic": "approval", "source": record.get("agent", "zops"),
            "extra": {"approval": {
                "approval_id": record["approval_id"],
                "tool_name": record["tool_name"],
                "agent": record.get("agent", ""),
                "username": record.get("username", ""),
                "risk_note": record.get("risk_note", ""),
                "params_preview": params_preview,
                "summary": record.get("summary", ""),
                "customer": record.get("customer", ""),
                "approvers": record.get("approvers", ""),
                "category": record.get("category") or "daily",
                # ── 业务实质字段 (卡片按区块渲染) ──
                "service_type": record.get("service_type", ""),
                "site": record.get("site", ""),
                "case_no": record.get("case_no", ""),
                "priority": record.get("priority", ""),
                "planned_ts": record.get("planned_ts", ""),
                "change_window": record.get("change_window", ""),
                "target_host": record.get("target_host", ""),
                "customer_gid": record.get("customer_gid", ""),
                "customer_order_no": record.get("customer_order_no", ""),
                "customer_site_ids": record.get("customer_site_ids", ""),
                "impact_scope": record.get("impact_scope", ""),
                "rollback_plan": record.get("rollback_plan", ""),
                "approver_ds": record.get("approver_ds", ""),
                "approver_tech": record.get("approver_tech", ""),
                "approver_admin": record.get("approver_admin", ""),
                "related_alerts_n": _rel_n,
                "citp_no": citp_info.get("citp_no", "") or (json.loads(record.get("citp") or "{}").get("citp_no", "")
                                                             if record.get("citp") else ""),
                "citp_devices": citp_devices,
                **citp_info,
            }},
        }, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"[approval] 飞书通知发送失败 (不阻断审批流程): {e}")


def get_approval(approval_id: str) -> Optional[dict]:
    try:
        data = _get_redis().hgetall(_key(approval_id))
        return data if data else None
    except Exception:
        return None


def list_pending_approvals() -> list:
    """列出待流程决策的审批单 (索引优先 O(pending); 空索引回退全键扫描并回填迁移)"""
    pending = []
    try:
        r = _get_redis()
        ids = list(r.smembers(_idx_pending()))
        if ids:
            pipe = r.pipeline()
            for aid in ids:
                pipe.hgetall(_key(aid))
            for aid, data in zip(ids, pipe.execute()):
                if not data:
                    try:
                        r.srem(_idx_pending(), aid)   # 惰性清理: 单据已过期
                    except Exception:
                        pass
                elif data.get("status") == _PENDING:
                    pending.append(data)
                else:
                    try:
                        r.srem(_idx_pending(), aid)   # 惰性清理: 已终态未及移出
                    except Exception:
                        pass
            return pending
        # 索引为空 (首次部署/被清): 扫描迁移回填
        for key in r.scan_iter(match=f"{config.REDIS_PREFIX}:approval:*", count=100):
            try:
                data = r.hgetall(key)
                if data and data.get("status") == _PENDING:
                    pending.append(data)
                    _idx_add(data)
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"[approval] 扫描失败: {e}")
    return pending


# 用户主动申请的审批类型白名单 (对应 ACTION_RISK_RULES 的 destructive 级操作;
# 申请仅为流程留痕, 批准后由运维人员线下执行)
# 审批分类: 日常配置审批 (daily) / 维护审批 (maintenance)
# - daily: 日常配置类变更 (DNS/防火墙/账号/阈值等常规配置调整)
# - maintenance: 维护类变更 (需变更窗口/CITP, 涉及设备维护/重启/切换/恢复等)
APPROVAL_CATEGORIES = {
    "daily": "日常配置审批",
    "maintenance": "维护审批",
}

# 操作类型 → 建议分类 (硬件维护/停机/恢复类 → maintenance; 常规配置 → daily)
OP_CATEGORY_HINTS = {
    **{op: "maintenance" for ops in (
        ["vm_power_off", "vm_power_reboot", "vm_shutdown", "vm_remove", "vm_migrate",
         "vm_disk_expand", "vm_snapshot_revert",
         "backup_job_run", "backup_job_stop", "backup_restore", "backup_repo_cleanup",
         "storage_volume_expand", "storage_volume_offline", "storage_snapshot_clone",
         "db_restart", "db_failover_switch", "db_backup_manual", "db_restore",
         "cert_renew", "service_restart", "batch_exec", "emergency_reboot",
         "monitor_maintenance_window"]) for op in ops},
    **{op: "daily" for ops in (
        ["vm_power_on", "vm_resource_resize", "vm_snapshot_create", "vm_snapshot_delete",
         "dns_record_change", "firewall_rule_change", "network_route_change",
         "loadbalancer_change", "ip_address_change", "monitor_threshold_change",
         "db_schema_change", "account_permission_change", "security_scan"]) for op in ops},
    # 硬件维护类 (hw_*) 一律维护审批
    **{f"hw_{k}": "maintenance" for k in (
        "nic_replace", "bios_battery_replace", "disk_replace", "memory_replace",
        "psu_replace", "fan_replace", "motherboard_replace", "sfp_replace",
        "cable_replace", "reseat")},
}
# 操作类型白名单 (审批仅流程留痕; 历史键名保持兼容)
# 硬件维护类 (hw_*): 与 CITP 故障类型模板一一对应 (hw_<fault_type_key>)
APPROVAL_APPLY_TYPES = {
    # 云主机 (vCenter/VCD)
    "vm_power_off": "虚拟机断电",
    "vm_power_on": "虚拟机开机",
    "vm_power_reboot": "虚拟机重启",
    "vm_shutdown": "虚拟机安全关机",
    "vm_remove": "虚拟机移除",
    "vm_migrate": "虚拟机迁移",
    "vm_resource_resize": "CPU/内存规格调整",
    "vm_disk_expand": "磁盘扩容",
    # 硬件维护 (服务器/存储设备; 维护审批, 联动 CITP 故障模板)
    "hw_nic_replace": "板载网卡更换",
    "hw_bios_battery_replace": "主板BIOS电池更换",
    "hw_disk_replace": "硬盘更换",
    "hw_memory_replace": "内存更换",
    "hw_psu_replace": "电源更换",
    "hw_fan_replace": "风扇更换",
    "hw_motherboard_replace": "主板更换",
    "hw_sfp_replace": "光模块(SFP)更换",
    "hw_cable_replace": "网线/线缆更换",
    "hw_reseat": "硬件拔插（重新插拔固定）",
    # 快照管理 (Snapshot)
    "vm_snapshot_create": "创建快照",
    "vm_snapshot_delete": "删除快照",
    "vm_snapshot_revert": "快照回滚",
    # 备份恢复 (BRR/Veeam)
    "backup_job_run": "手动触发备份作业",
    "backup_job_stop": "停止备份作业",
    "backup_restore": "备份恢复",
    "backup_repo_cleanup": "备份仓库清理",
    # 存储 (Nimble/Compellent/NetApp)
    "storage_volume_expand": "存储卷扩容",
    "storage_volume_offline": "存储卷下线",
    "storage_snapshot_clone": "卷快照克隆",
    # 网络与DNS (SmartDNS)
    "dns_record_change": "DNS 记录变更",
    "firewall_rule_change": "防火墙策略变更",
    "network_route_change": "网络路由变更",
    "loadbalancer_change": "负载均衡变更",
    "ip_address_change": "IP 地址变更",
    # 监控告警 (NMS/Aria Ops)
    "monitor_maintenance_window": "监控屏蔽/维护窗口",
    "monitor_threshold_change": "告警阈值调整",
    # 数据库
    "db_restart": "数据库重启",
    "db_failover_switch": "数据库主备切换",
    "db_backup_manual": "数据库手动备份",
    "db_restore": "数据库恢复",
    "db_schema_change": "数据库结构变更",
    # 安全
    "cert_renew": "证书更新",
    "account_permission_change": "账号/权限变更",
    "security_scan": "安全扫描",
    # 通用
    "service_restart": "中间件/服务重启",
    "batch_exec": "批量命令执行",
    "emergency_reboot": "紧急重启",
}

# 三层层级目录: 产品类型 → 服务类型 → 操作类型
# - 各产品线归其位, 不互相套用: SCC/VONE/SF 为主机产品线 (仅主机服务);
#   存储为独立产品线, 按存储种类分服务 (阵列卷 / Ceph); BRR 为独立备份产品线;
#   硬件为物理层; 平台服务为共享域 (网络/监控/数据库/安全)。
# - daily 日常配置 / maintenance 维护; 新产品/服务/操作在对应数组追加即可。
_VM_OPS_DAILY = ["vm_power_on", "vm_resource_resize", "vm_snapshot_create", "vm_snapshot_delete"]
_STOR_ARRAY_DAILY = ["storage_snapshot_clone"]
_VM_OPS_MAINT = ["vm_power_off", "vm_power_reboot", "vm_shutdown", "vm_remove",
                 "vm_migrate", "vm_disk_expand", "vm_snapshot_revert"]
_STOR_ARRAY_MAINT = ["storage_volume_expand", "storage_volume_offline"]
_BAK_OPS_MAINT = ["backup_job_run", "backup_job_stop", "backup_restore",
                  "backup_repo_cleanup"]
_HW_OPS = ["hw_disk_replace", "hw_psu_replace", "hw_nic_replace", "hw_sfp_replace",
           "hw_cable_replace", "hw_reseat", "hw_bios_battery_replace", "hw_memory_replace",
           "hw_fan_replace", "hw_motherboard_replace"]

_HOST_SERVICES_DAILY = [("主机", _VM_OPS_DAILY)]
_HOST_SERVICES_MAINT = [("主机", _VM_OPS_MAINT)]
# 存储产品线: 按存储种类分服务; Ceph (RBD/CephFS) 操作清单待业务补充后填入
_STOR_SERVICES_DAILY = [
    ("阵列卷 (Nimble/Compellent/NetApp)", _STOR_ARRAY_DAILY),
    ("Ceph (RBD/CephFS)", []),
]
_STOR_SERVICES_MAINT = [
    ("阵列卷 (Nimble/Compellent/NetApp)", _STOR_ARRAY_MAINT),
    ("Ceph (RBD/CephFS)", []),
]

APPROVAL_OP_CATALOG = {
    "daily": [
        ("SCC", _HOST_SERVICES_DAILY),
        ("VONE", _HOST_SERVICES_DAILY),
        ("SF", _HOST_SERVICES_DAILY),
        ("存储", _STOR_SERVICES_DAILY),
        ("平台服务", [
            ("网络与DNS (SmartDNS)", ["dns_record_change", "firewall_rule_change",
                                     "network_route_change", "loadbalancer_change",
                                     "ip_address_change"]),
            ("监控 (NMS)", ["monitor_threshold_change"]),
            ("数据库", ["db_schema_change"]),
            ("安全", ["account_permission_change", "security_scan"]),
        ]),
    ],
    "maintenance": [
        ("SCC", _HOST_SERVICES_MAINT),
        ("VONE", _HOST_SERVICES_MAINT),
        ("SF", _HOST_SERVICES_MAINT),
        ("存储", _STOR_SERVICES_MAINT),
        ("BRR (备份)", [("备份恢复", _BAK_OPS_MAINT)]),
        ("硬件", [("服务器/存储设备", _HW_OPS)]),
        ("平台服务", [
            ("监控 (NMS)", ["monitor_maintenance_window"]),
            ("数据库", ["db_restart", "db_failover_switch", "db_backup_manual",
                       "db_restore"]),
            ("安全", ["cert_renew"]),
            ("通用", ["service_restart", "batch_exec", "emergency_reboot"]),
        ]),
    ],
}
# 合并视图 (兼容旧引用: 扁平 stype = "产品/服务")
APPROVAL_OP_GROUPS: list = [
    (f"{p}/{sv}", ops)
    for cat_groups in APPROVAL_OP_CATALOG.values()
    for p, services in cat_groups
    for sv, ops in services if ops
]
APPROVAL_OP_STYPE = {op: st for st, ops in APPROVAL_OP_GROUPS for op in ops}
# 三级目录的扁平键 (产品/服务) 按分类列出 (兼容引用)
APPROVAL_OP_CATALOG_STYPES = {cat: [f"{p}/{sv}" for p, services in groups
                                    for sv, ops in services if ops]
                              for cat, groups in APPROVAL_OP_CATALOG.items()}
# 产品类型清单 (层级第一级; 去重保序)
APPROVAL_PRODUCT_TYPES = list(dict.fromkeys(
    p for groups in APPROVAL_OP_CATALOG.values() for p, _ in groups))
# 服务类型清单 (层级第二级; 全量去重保序)
APPROVAL_SERVICE_TYPES = list(dict.fromkeys(
    sv for groups in APPROVAL_OP_CATALOG.values() for _, services in groups
    for sv, _ in services))


def list_user_approvals(username: str) -> list:
    """列出某用户发起的全部审批单 (用户索引优先; 空索引回退扫描迁移)"""
    mine = []
    try:
        r = _get_redis()
        ids = list(r.smembers(_idx_user(username))) if username else []
        if ids:
            pipe = r.pipeline()
            for aid in ids:
                pipe.hgetall(_key(aid))
            for aid, data in zip(ids, pipe.execute()):
                if not data:
                    try:
                        r.srem(_idx_user(username), aid)
                    except Exception:
                        pass
                elif data.get("username") == username:
                    mine.append(data)
        else:
            # 索引为空: 扫描迁移回填 (历史单据一次性)
            for key in r.scan_iter(match=f"{config.REDIS_PREFIX}:approval:*", count=100):
                try:
                    data = r.hgetall(key)
                    if data and data.get("username") == username:
                        mine.append(data)
                        _idx_add(data)
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"[approval] 用户审批单扫描失败: {e}")
    mine.sort(key=lambda d: float(d.get("created_ts") or 0), reverse=True)
    return mine


def decide_approval(approval_id: str, decision: str, approver: str = "",
                    level: str = "") -> Optional[dict]:
    """记录流程决策 (approve/reject) → 写审计日志

    审批只走流程 (v4.0.2): 决策仅是流程留痕 (批准=流程通过, 拒绝=流程否决),
    无论批准与否系统都不会自动执行 destructive 操作;
    实际执行由运维人员按变更流程线下完成。

    P0: 决策写入采用 WATCH/MULTI 版本 CAS — 并发决策只有一个生效,
    重复决策读到终态直接返回原单 (幂等), 不产生覆盖。
    """
    decision = decision.lower().strip()
    if decision not in ("approve", "approved", _APPROVED):
        decision = _REJECTED
    else:
        decision = _APPROVED
    try:
        import redis as _redis_mod
        r = _get_redis()
        key = _key(approval_id)
        with r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)                       # CAS: 决策期间他人改单 → WatchError 重试
                    data = pipe.hgetall(key)
                    if not data:
                        return None
                    if data.get("status") != _PENDING:
                        return data  # 已决策 (幂等; 并发后到者读到终态)
                    upd = {
                        "status": decision, "decided_ts": time.time(),
                        "approver": approver or "admin",
                        "decision_ver": str(int(float(data.get("decision_ver") or 0)) + 1),
                    }
                    if level:  # 分级审批流终态: 记录轨迹 (如 "ds=approve:svc-gcoc;admin=approve:user-1856")
                        log = (data.get("level_log") or "")
                        upd["level_log"] = (f"{log};{level}={decision}:{approver or 'admin'}@{time.time():.0f}"
                                            if log else f"{level}={decision}:{approver or 'admin'}@{time.time():.0f}")
                        upd["done_levels"] = ",".join(sorted(set(
                            [x for x in (data.get("done_levels") or "").split(",") if x] + [level])))
                    pipe.multi()
                    pipe.hset(key, mapping=upd)
                    pipe.srem(_idx_pending(), approval_id)   # 终态移出待办索引
                    pipe.execute()
                    data.update(upd)
                    break
                except _redis_mod.WatchError:
                    continue   # 并发写入: 重读最新状态再判定 (幂等收敛)
        # 决策结果通知申请人 (飞书; 失败不阻断)
        try:
            _txt = (f"{'✅' if decision == _APPROVED else '⛔'} 审批单 `{approval_id}` "
                    f"({data.get('tool_name') or '-'}) 已"
                    f"{'**批准**' if decision == _APPROVED else '**拒绝**'}\n"
                    f"决策人: {data.get('approver') or '-'} · 分类: {data.get('category') or 'daily'}\n"
                    f"申请事由: {data.get('summary') or '-'}\n"
                    + ("批准后由运维按变更流程线下执行; 维护单请在执行完成后做验收。"
                       if decision == _APPROVED else "如有疑问请联系决策人或在审批中心发起回退申请。"))
            r.publish(config.channel("notify:feishu"), json.dumps({
                "text": _txt, "topic": "approval_result",
                "source": data.get("agent") or "zops", "extra": {"to_user": data.get("username") or ""},
            }, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"[approval] 决策通知发送失败 (不阻断): {e}")
        summary = (f"{decision} (流程留痕; 操作发起人: {data.get('username', '-')}, "
                   f"系统不会自动执行, 实际执行由运维人员按流程完成)")
        _audit_async({**data, "approver": approver or "admin"}, summary=summary)
        logger.warning(f"[approval] 审批单 {approval_id} 流程决策: {decision} "
                       f"(approver={approver or 'admin'}, 仅流程留痕)")
        return data
    except Exception as e:
        logger.error(f"[approval] 决策写入失败: {e}")
        return None


def advance_approval_level(approval_id: str, level: str, approver: str = "") -> tuple:
    """分级审批流中间级推进: 当前级通过但仍有后续级 → 单据保持 pending, 记轨迹解锁下一级

    Returns: (rec|None, errmsg|None); 审计日志记录每一级推进 (action=approval_level)。
    P0: WATCH/MULTI CAS — 并发同级推进只记一次。
    """
    import redis as _redis_mod
    r = _get_redis()
    key = _key(approval_id)
    data = None
    with r.pipeline() as pipe:
        while True:
            try:
                pipe.watch(key)
                data = pipe.hgetall(key)
                if not data:
                    return None, "审批单不存在或已过期"
                if data.get("status") != _PENDING:
                    return None, f"单据已决策 ({data.get('status')}), 无需逐级推进"
                if level in (data.get("done_levels") or "").split(","):
                    return data, None  # 幂等
                log = (data.get("level_log") or "")
                entry = f"{level}=approve:{approver or 'admin'}@{time.time():.0f}"
                done = ",".join(sorted(set([x for x in (data.get("done_levels") or "").split(",") if x] + [level])))
                pipe.multi()
                pipe.hset(key, mapping={
                    "done_levels": done,
                    "level_log": (f"{log};{entry}" if log else entry),
                })
                pipe.execute()
                data.update({"done_levels": done, "level_log": (f"{log};{entry}" if log else entry)})
                break
            except _redis_mod.WatchError:
                continue
    _audit_async(data, action="approval_level",
                 summary=f"{level}=approve (分级审批推进; approver={approver}; done={data.get('done_levels')})")
    logger.warning(f"[approval] {approval_id} 分级推进: {level}=approve "
                   f"(approver={approver}, done={data.get('done_levels')})")
    return data, None


def cancel_approval(approval_id: str, username: str, reason: str = "") -> tuple:
    """申请人撤销待审批单 (仅 pending 且本人; 撤销同样写审计日志永久留痕)

    临时操作回退逻辑: 误提交/计划变更时由申请人自行撤销,
    已决策单 (approved/rejected) 不可撤销 — 审计红线, 决策记录永久保留。
    Returns: (rec|None, errmsg|None)
    """
    try:
        r = _get_redis()
        data = r.hgetall(_key(approval_id))
        if not data:
            return None, "审批单不存在或已过期"
        if data.get("status") != _PENDING:
            return None, f"仅待审批单可撤销 (当前状态: {data.get('status')})"
        if (data.get("username") or "").strip() != (username or "").strip():
            return None, "仅申请人本人可撤销该审批单"
        r.hset(_key(approval_id), mapping={
            "status": _CANCELLED, "cancelled_ts": time.time(),
            "cancel_reason": (reason or "申请人主动撤销")[:300],
        })
        try:
            r.srem(_idx_pending(), approval_id)
        except Exception:
            pass
        data.update({"status": _CANCELLED, "cancel_reason": reason or "申请人主动撤销"})
        _audit_async(data, action="approval_cancelled",
                     summary=f"cancelled (申请人 {username} 主动撤销待审批单; reason={reason or '-'})")
        logger.warning(f"[approval] 审批单 {approval_id} 已撤销 (user={username}, reason={reason or '-'})")
        return data, None
    except Exception as e:
        logger.error(f"[approval] 撤销写入失败: {e}")
        return None, f"撤销失败: {e}"


def confirm_execution(approval_id: str, executor: str) -> tuple:
    """执行确认: 已批准单由申请人/运维确认线下执行完成 (生命周期闭环)

    approved → executed (executed_ts/executor 留痕 + 审计); 仅 approved 可确认。
    Returns: (rec|None, errmsg|None)
    """
    try:
        r = _get_redis()
        data = r.hgetall(_key(approval_id))
        if not data:
            return None, "审批单不存在或已过期"
        if data.get("status") != _APPROVED:
            return None, f"仅已批准单可确认执行 (当前状态: {data.get('status')})"
        upd = {"status": "executed", "executed_ts": time.time(), "executor": executor[:100]}
        r.hset(_key(approval_id), mapping=upd)
        data.update(upd)
        _audit_async(data, action="approval_executed",
                     summary=f"executed (执行确认: {executor}; 单据 {approval_id} 线下执行完成)")
        logger.warning(f"[approval] 审批单 {approval_id} 执行确认 (executor={executor})")
        return data, None
    except Exception as e:
        logger.error(f"[approval] 执行确认写入失败: {e}")
        return None, f"执行确认失败: {e}"


async def gate_action_message(agent, msg) -> bool:
    """AgentMessage action 路径的审批门 (BaseAgent._safe_handle 入口调用)

    Args:
        agent: BaseAgent 实例 (取 .name 标识, 拒绝时用其 .reply 回复用户);
               兼容传 str agent_id (此时跳过用户回复, 仅拦截)
        msg: AgentMessage

    Returns:
        True → 放行 (read/write)
        False → 已拦截 (destructive 一律拦截: 生成审批单走流程, 系统绝不自动执行)

    P0 修复:
    - 高风险操作在所有配置组合下均不调用 executor (移除 APPROVAL_ENABLED 绕过开关);
    - write 级无法确认发起主体 → 拒绝 (不再因 username 为空跳过权限检查);
    - 审批单持久化失败 → 明确告知「未登记成功」, 不虚报已创建;
    - 审批门自身异常 → fail-closed 拦截并告警 (不再 fail-open 放行)。
    """
    agent_name = getattr(agent, "name", "") or (agent if isinstance(agent, str) else "")
    try:
        risk = assess_action_risk(agent_name, msg.action, msg.params or {})
        if risk != "destructive":
            if risk == "write":
                # v3 write 级业务权限门: 权限问题 ≠ 变更审批, 无权限直接拒并指引授权 (不生成审批单)
                from core.bizperms import WRITE_PERM_MAP
                required = WRITE_PERM_MAP.get((agent_name, msg.action))
                username = (getattr(msg, "username", "") or "").strip()
                if required:
                    if not username:
                        logger.warning(f"[approval] write 权限拦截: {agent_name}.{msg.action} "
                                       f"缺少发起主体 (username 为空), 拒绝")
                        reply_deny = getattr(agent, "reply", None)
                        if reply_deny:
                            await reply_deny(msg, result={"text": (
                                f"⛔ `{agent_name}.{msg.action}` 需要业务权限校验，但当前请求无法确认发起用户，已拦截。\n"
                                f"请从已登录渠道发起操作。"
                            )})
                        return False
                    from core.bizperms import effective_perms, biz_label, bump_perm_metric
                    eff = await effective_perms(username)
                    if required not in eff["effective"]:
                        await bump_perm_metric("denied", required)
                        logger.warning(f"[approval] write 权限拦截: {username} 触发 {agent_name}.{msg.action} "
                                       f"需要「{biz_label(required)}」({required}), 未授权")
                        reply_deny = getattr(agent, "reply", None)
                        if reply_deny:
                            await reply_deny(msg, result={"text": (
                                f"⛔ `{agent_name}.{msg.action}` 需要业务权限「{biz_label(required)}」，当前未授权，已拦截。\n"
                                f"这是权限问题（不同于变更审批）：请联系系统管理员在「用户管理 → 业务权限」中授予，"
                                f"或由已授权同事操作。"
                            )})
                        return False
                    await bump_perm_metric("used", required)
                    # 范围校验 (params 携带 site/customer 时; SCOPED 且缺失 → 拒绝)
                    _p = msg.params or {}
                    from core.bizperms import scope_allows as _scope_allows
                    ok_scope, why_scope = _scope_allows(
                        eff["scopes"], str(_p.get("site") or ""),
                        str(_p.get("customer") or ""), str(_p.get("customer_gid") or ""))
                    if not ok_scope:
                        logger.warning(f"[approval] write 范围拦截: {username} {agent_name}.{msg.action}: {why_scope}")
                        reply_deny = getattr(agent, "reply", None)
                        if reply_deny:
                            await reply_deny(msg, result={"text": (
                                f"⛔ `{agent_name}.{msg.action}` 已被资源范围约束拦截：{why_scope}"
                            )})
                        return False
                logger.info(f"[approval] write 级 action {agent_name}.{msg.action} 放行 (已记入审计链)")
            return True

        rec = create_approval(
            tool_name=f"{agent_name}.{msg.action}",
            params={k: v for k, v in (msg.params or {}).items() if k != "history"},
            agent=agent_name, session_id=msg.session_id, msg_id=msg.msg_id,
            username=msg.username,
        )
        # 审批只走流程: destructive 一律拦截, 不挂起等待, 不恢复执行
        reply_fn = getattr(agent, "reply", None)
        if rec.get("_persist_failed"):
            # P0: 审批单未登记成功 — 明确告知, 不虚报单号 (操作仍拦截)
            logger.error(f"[approval] 审批单登记失败, 拦截 {agent_name}.{msg.action} "
                         f"(persist_error={rec.get('_persist_error')})")
            if reply_fn:
                await reply_fn(msg, result={"text": (
                    f"⛔ `{agent_name}.{msg.action}` 属于高风险操作，系统不会自动执行，已拦截。\n"
                    f"⚠️ 审批单**未登记成功**（审批存储故障），请稍后重试或联系管理员；"
                    f"不要线下补单执行。"
                )})
            return False
        logger.warning(f"[approval] {rec['approval_id']} 拦截 {agent_name}.{msg.action} "
                       f"(destructive 操作系统不自动执行, 已生成审批单走流程)")
        if reply_fn:
            await reply_fn(msg, result={"text": (
                f"⛔ `{agent_name}.{msg.action}` 属于高风险操作，系统不会自动执行，已拦截。\n"
                f"已生成审批单 `{rec['approval_id']}` 走审批流程（申请留痕）。\n"
                f"审批通过后请由运维人员按变更流程线下执行；无需在系统内重试。"
            )})
        return False
    except Exception as e:
        # P0: 审批门自身故障 → fail-closed 拦截并告警 (高风险语义下不能 fail-open)
        logger.error(f"[approval] 审批门异常, fail-closed 拦截: {e}")
        reply_fn = getattr(agent, "reply", None)
        try:
            if reply_fn:
                await reply_fn(msg, result={"text": (
                    f"⛔ 审批门组件异常（{str(e)[:120]}），`{agent_name}.{msg.action or 'handle'}` 已按安全策略拦截。\n"
                    f"请稍后重试或联系管理员；紧急变更请走线下变更流程。"
                )})
        except Exception as reply_err:
            logger.error(f"[approval] fail-closed 回复失败: {reply_err}")
        return False


async def gate_destructive_call(tool_def, params: dict, exec_fn, agent: str = "",
                                session_id: str = "", msg_id: str = "",
                                username: str = ""):
    """destructive 工具的审批门包装 (query_engine._exec_one 调用)

    审批只走流程 (v4.0.2): destructive 一律立即拦截 (系统绝不自动执行),
    创建审批单作为流程记录, 返回 permission_denied=True 的结构化拒绝;
    管理员后续决策仅写审计, 不触发执行。
    P0: 持久化失败时返回未登记成功提示 — 不虚报审批单, 不降级为执行。
    """
    from core.tool_schema import ToolExecution

    rec = create_approval(
        tool_name=tool_def.name, params=params, agent=agent,
        session_id=session_id, msg_id=msg_id, username=username,
        risk_note=getattr(tool_def, "risk_note", "") or "",
    )
    if rec.get("_persist_failed"):
        logger.error(f"[approval] 审批单登记失败 (persist_error={rec.get('_persist_error')}), "
                     f"拦截 {tool_def.name}")
        return ToolExecution(
            tool_name=tool_def.name,
            success=False,
            output=json.dumps({
                "permission_denied": True,
                "approval_registered": False,
                "tool": tool_def.name,
                "reason": "审批存储故障, 审批单未登记成功; 高风险操作已拦截",
                "advice": "请稍后重试或联系管理员; 不要线下补单执行",
            }, ensure_ascii=False),
            error="approval_persist_failed",
            permission_denied=True,
        )
    reason = "系统不自动执行高风险操作, 已生成审批单走流程"
    logger.warning(f"[approval] {rec['approval_id']} 拦截 {tool_def.name} ({reason})")
    return ToolExecution(
        tool_name=tool_def.name,
        success=False,
        output=json.dumps({
            "permission_denied": True,
            "approval_id": rec["approval_id"],
            "tool": tool_def.name,
            "reason": f"destructive 操作已拦截：{reason}",
            "advice": "审批通过后请由运维人员按变更流程线下执行",
        }, ensure_ascii=False),
        error="approval_flow_only",
        permission_denied=True,
    )
