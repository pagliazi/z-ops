"""多告警闭环覆盖度评估（纯函数，只读）

回答「SSL 之外还有哪些告警值得做闭环」：
- 按 checklist / event_group / summary 确定性归类到探针类型（kind）；
- 按可取证能力分层：L1 已有主动探针可直接闭环、L2 可自动取证但需接入探针
  或走簇级核验、L3 只能给知识建议；
- 输出按「严重度加权量」排序的候选，供排期与灰度决策。

约束：不读 Redis、不调 LLM、不产生副作用，输入就是 NMS 快照 dict。
"""

import re
from typing import Any, Dict, List, Optional

# 分层口径：
#   L1 = 已有主动探针（tool_checks 中已实现），可直接复用 SSL 闭环框架
#   L2 = 可自动取证但需接探针，或该类告警本身以「簇」为主、靠 NMS 清除核验恢复
#   L3 = 无可靠探针，只做预案/知识推荐与人工确认
TIER_LABELS = {
    "L1": "已有探针，可直接闭环",
    "L2": "可自动取证/簇级核验，需接入",
    "L3": "仅建议，不做自动核验",
}

# severity 权重：用于「严重度加权量」，避免只按条数排期（critical 只有几百条）
SEVERITY_WEIGHTS = {
    "critical": 8, "major": 4, "minor": 2, "warning": 1, "indeterminate": 1,
}
ACTIVE_SEVERITIES = set(SEVERITY_WEIGHTS)

# 顺序敏感：先精确 checklist，再按正则（network 放 auth 之前，避免 link/auth 混淆）
KINDS: List[Dict[str, Any]] = [
    {
        "kind": "certificate", "label": "SSL 证书", "tier": "L1", "probe": "existing",
        "checklists": ("SSL证书过期",),
        "patterns": (),
        "evidence": "TLS 严格握手 + 证书剩余天数 + NMS 清除证据",
        "next_step": "已闭环（见 Z-OPS-NMS-SSL-GRAY-CHECKLIST.md）",
    },
    {
        "kind": "dns", "label": "DNS 解析", "tier": "L1", "probe": "existing",
        "checklists": ("DNS解析异常",),
        "patterns": (r"dns.{0,12}(解析|resolve|fail)|无法解析",),
        "evidence": "DNS 实测解析结果 + 解析记录一致性",
        "next_step": "复用 SSL 状态机，探针换成 DNS 实测（tool_checks.dns 已产出）",
    },
    {
        "kind": "tcp", "label": "服务端口", "tier": "L1", "probe": "existing",
        "checklists": ("PDNS outage故障",),
        "patterns": (r"outage|端口.{0,6}(不可达|不通)|connection refused|服务不可用",),
        "evidence": "TCP 端口可达性 + 响应时延",
        "next_step": "复用 SSL 状态机，探针换成 TCP 连接（tool_checks.tcp 已产出）",
    },
    {
        "kind": "backup", "label": "备份作业", "tier": "L1", "probe": "existing",
        "checklists": ("VBR备份失败", "A2C VSPC备份失败", "A2C O365备份失败"),
        "patterns": (r"vbr|veeam|备份失败|o365|a2c|zerto",),
        "evidence": "备份作业状态 + 最近还原点时间",
        "next_step": "闭环判定改为「作业成功且还原点新鲜」，其余复用现有框架",
    },
    {
        "kind": "storage", "label": "存储容量/磁盘", "tier": "L2", "probe": "extendable",
        "checklists": ("存储磁盘故障", "ESXi Datastore使用率高"),
        "patterns": (r"datastore|存储|磁盘|raid|volume|pool",),
        "evidence": "阵列/Datastore 容量与磁盘状态（需接入存储 API 或只读命令）",
        "next_step": "先接只读容量探针，再评估是否值得闭环（当前量小）",
    },
    {
        "kind": "mail_queue", "label": "邮件队列", "tier": "L2", "probe": "extendable",
        "checklists": ("SCM邮件队列堆积",),
        "patterns": (r"邮件队列|mail.{0,8}queue",),
        "evidence": "队列长度趋势 + 投递失败计数（需 SCM 接口）",
        "next_step": "队列是单调指标，适合「阈值恢复」而非 TLS 式连续健康",
    },
    {
        "kind": "network", "label": "网络协议/链路", "tier": "L2", "probe": "cluster",
        "checklists": (),
        "patterns": (r"bgp|bfd|ospf|mpls|lsp|link|interface|vrf|trunk|"
                     r"storm control|flap|\bmep\b|unreachable|icmp",),
        "evidence": "同簇是否同源、是否维护窗口、flap 历史、整簇是否清除",
        "next_step": "按事件簇闭环：人工确认根因 → 整簇清除且 N 分钟无复发才恢复",
    },
    {
        "kind": "auth", "label": "认证/账号", "tier": "L2", "probe": "cluster",
        "checklists": (),
        "patterns": (r"auth|ldap|radius|tacacs|login|账号|认证|密码",),
        "evidence": "同源认证失败是否集中、AD/LDAP 侧是否锁定（需目录服务只读查询）",
        "next_step": "与「NMS 全局会话失效」告警联动，避免把采集故障当业务故障",
    },
    {
        "kind": "time", "label": "时间同步", "tier": "L2", "probe": "existing",
        "checklists": (),
        "patterns": (r"ntp|时间同步|clock",),
        "evidence": "NTP 偏差（只读查询即可，作为轻量探针）",
        "next_step": "探针简单，可排在存储类之后接入",
    },
    {
        "kind": "other", "label": "其他/长尾", "tier": "L3", "probe": "none",
        "checklists": (),
        "patterns": (),
        "evidence": "无可靠探针，依赖人工与知识库",
        "next_step": "只做预案/知识推荐，不做自动核验",
    },
]

_CHECKLIST_INDEX: Dict[str, Dict[str, Any]] = {
    name: spec for spec in KINDS for name in spec["checklists"]
}
_COMPILED = [(spec, [re.compile(p, re.I) for p in spec["patterns"]])
             for spec in KINDS if spec["patterns"]]


def classify(alarm: Dict[str, Any]) -> Dict[str, Any]:
    """把一条告警归到唯一 kind（checklist 精确优先，其次 event_group/summary 正则）。"""
    checklist = (alarm.get("checklist") or "").strip()
    spec: Optional[Dict[str, Any]] = _CHECKLIST_INDEX.get(checklist)
    matched_by = "checklist" if spec else ""
    if spec is None:
        haystack = " ".join([
            alarm.get("event_group") or "", alarm.get("summary") or "",
            ((alarm.get("fields") or {}).get("Summary") or ""),
        ])
        for candidate, patterns in _COMPILED:
            if any(p.search(haystack) for p in patterns):
                spec, matched_by = candidate, "content"
                break
    if spec is None:
        spec, matched_by = KINDS[-1], "default"
    return {
        "kind": spec["kind"], "label": spec["label"], "tier": spec["tier"],
        "probe": spec["probe"], "matched_by": matched_by,
    }


def _weight(alarm: Dict[str, Any]) -> int:
    return SEVERITY_WEIGHTS.get((alarm.get("severity") or "").lower(), 0)


def coverage_report(snapshot: Dict[str, Any], top_groups: int = 15) -> Dict[str, Any]:
    """统计活跃告警的闭环覆盖度，返回分层、分类与推荐顺序。

    snapshot 结构与 Redis `zops:nms_alarms:latest` 一致（含 alarms 列表）。
    """
    alarms = (snapshot or {}).get("alarms") or []
    active = [a for a in alarms if (a.get("severity") or "") in ACTIVE_SEVERITIES]

    by_kind: Dict[str, Dict[str, Any]] = {}
    by_tier: Dict[str, Dict[str, Any]] = {
        tier: {"label": TIER_LABELS[tier], "alarms": 0, "weighted": 0, "kinds": []}
        for tier in ("L1", "L2", "L3")
    }
    sources: Dict[str, set] = {}
    event_keys: Dict[str, set] = {}
    group_counts: Dict[str, Dict[str, Any]] = {}
    for alarm in active:
        cls = classify(alarm)
        kind = cls["kind"]
        row = by_kind.setdefault(kind, {
            "kind": kind, "label": cls["label"], "tier": cls["tier"],
            "probe": cls["probe"], "alarms": 0, "weighted": 0, "severities": {},
        })
        row["alarms"] += 1
        row["weighted"] += _weight(alarm)
        sev = alarm.get("severity") or "?"
        row["severities"][sev] = row["severities"].get(sev, 0) + 1
        sources.setdefault(kind, set()).add(alarm.get("source") or "")
        event_keys.setdefault(kind, set()).add(alarm.get("event_key") or "")

        group = (alarm.get("event_group") or "(未分组)").strip() or "(未分组)"
        g = group_counts.setdefault(group, {"group": group, "alarms": 0, "weighted": 0,
                                            "tier": cls["tier"], "kind": kind})
        g["alarms"] += 1
        g["weighted"] += _weight(alarm)

    spec_by_kind = {s["kind"]: s for s in KINDS}
    rows = []
    for kind, row in by_kind.items():
        spec = spec_by_kind[kind]
        row["sources"] = len(sources.get(kind) or ())
        row["event_keys"] = len(event_keys.get(kind) or ())
        row["evidence"] = spec["evidence"]
        row["next_step"] = spec["next_step"]
        rows.append(row)
    rows.sort(key=lambda r: (r["weighted"], r["alarms"]), reverse=True)

    for row in rows:
        bucket = by_tier[row["tier"]]
        bucket["alarms"] += row["alarms"]
        bucket["weighted"] += row["weighted"]
        bucket["kinds"].append(row["kind"])

    total = len(active)
    for bucket in by_tier.values():
        bucket["ratio"] = round(bucket["alarms"] / total * 100, 1) if total else 0.0

    top = sorted(group_counts.values(), key=lambda r: r["alarms"], reverse=True)[:top_groups]

    # 推荐顺序：先补 L1（改造成本最低、已有探针），再按加权量做 L2 簇级
    recommendations = []
    for row in [r for r in rows if r["tier"] == "L1" and r["kind"] != "certificate"]:
        recommendations.append({
            "priority": "P1", "kind": row["kind"], "label": row["label"],
            "alarms": row["alarms"], "reason": "已有探针，复用闭环框架即可",
        })
    for row in [r for r in rows if r["tier"] == "L2"][:3]:
        recommendations.append({
            "priority": "P2", "kind": row["kind"], "label": row["label"],
            "alarms": row["alarms"], "reason": row["next_step"],
        })
    if not recommendations:
        recommendations.append({
            "priority": "P3", "kind": "other", "label": "长尾告警",
            "alarms": by_tier["L3"]["alarms"],
            "reason": "当前无可自动取证类型，先补预案覆盖率",
        })

    checklist_hit = sum(1 for a in active if (a.get("checklist") or "").strip())
    unique_keys = len({a.get("event_key") or "" for a in active})
    return {
        "generated_at": (snapshot or {}).get("timestamp") or "",
        "total_alarms": len(alarms),
        "total_active": total,
        "checklist_hit": checklist_hit,
        "checklist_coverage": round(checklist_hit / total * 100, 2) if total else 0.0,
        "by_tier": by_tier,
        "by_kind": rows,
        "top_event_groups": top,
        "cluster_hint": {
            "event_keys": unique_keys,
            "sources": len({a.get("source") or "" for a in active}),
            "avg_alarms_per_event_key": (
                round(total / unique_keys, 1) if unique_keys else 0.0),
        },
        "recommendations": recommendations,
    }


def summarize_line(report: Dict[str, Any]) -> str:
    """单行摘要，便于日志/接口快速判读。"""
    tiers = report.get("by_tier") or {}
    top_kind = ((report.get("by_kind") or [{}])[0]).get("kind", "-")
    return (
        f"active={report.get('total_active', 0)} "
        f"checklist_coverage={report.get('checklist_coverage', 0.0)}% | "
        f"L1={tiers.get('L1', {}).get('alarms', 0)} "
        f"L2={tiers.get('L2', {}).get('alarms', 0)} "
        f"L3={tiers.get('L3', {}).get('alarms', 0)} | top_kind={top_kind}"
    )
