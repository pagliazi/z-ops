"""
Z-OPS v4.0 Phase 4.4 — 确定性优先分析模式 (借鉴 K8sGPT "先规则后 LLM")

规则引擎先出结论: 告警分类 / Checklist 预案归属 / 优先级定级 / 检查方向
全部由确定性规则决定, LLM 只做解释措辞与报告组织, 不做分类决策 —
直接降低告警误贴率 (LLM 自由归类时易把告警贴错预案/类别)。

适用场景: NMS Checklist 匹配、告警分组分析
    agents/nms_agent.py _nms_analyze_alarms (config.DETERMINISTIC_FIRST 开关)

设计:
    classify_alarm()     单条告警 → {category, priority, advice, checklist}
    build_conclusion_table()  告警列表 → 确定性分组结论表 (P0/P1 排序)
    结论表注入 prompt, LLM 的角色从「分析决策者」降为「解释措辞者」
"""

import logging

logger = logging.getLogger("zops")

# ── 类别规则 (按序匹配, 首个命中生效; 与 NMS prompt 铁律第 3 条的分类口径一致) ──
# (类别名, 关键词表, 确定检查方向)
CLASS_RULES = [
    ("网络", ["bgp", "mpls", "lsp", "bfd", "link", "linkdown", "lineproto", "icmp",
              "ping", "接口", "链路", "丢包", "时延", "unreachable", "timeout"],
     "检查端口/链路状态与路由邻居, 网络类同步 GNOC 排查传输侧"),
    ("存储", ["disk", "storage", "volume", "lun", "datastore", "path", "磁盘",
              "存储", "容量", "space"],
     "登录存储管理页面检查阵列/卷/容量/datastore 状态"),
    ("系统", ["cpu", "memory", "swap", "process", "load", "服务", "进程",
              "内存", "重启", "restart", "down"],
     "登录系统检查进程/资源占用/系统日志"),
    ("电源硬件", ["power", "fan", "temperature", "电源", "风扇", "温度", "hardware"],
     "检查设备硬件状态 (电源/风扇/温度) 与机房环境"),
    ("应用", ["http", "url", "service", "应用", "响应", "response", "证书", "cert"],
     "检查应用服务状态与端口监听/证书有效期"),
]

# ── 严重级别 → 优先级 (确定性映射) ──
SEVERITY_PRIORITY = {
    "critical": "P0", "major": "P0",
    "minor": "P1", "warning": "P1",
    "indeterminate": "P2", "unknown": "P2",
    "clear": "P2", "cleared": "P2",
}

_PRIORITY_ORDER = ["P0", "P1", "P2"]


def classify_alarm(summary: str, severity: str, checklist_title: str = "") -> dict:
    """单条告警 → 确定性分类结论

    Args:
        summary: 告警摘要
        severity: 严重级别 (critical/major/...)
        checklist_title: 规则匹配到的 Checklist 预案名 (空 = 无匹配)
    """
    s = (summary or "").lower()
    cat, advice = "其他", "按告警对象逐项排查 (无规则命中)"
    for name, kws, adv in CLASS_RULES:
        if any(k in s for k in kws):
            cat, advice = name, adv
            break
    pri = SEVERITY_PRIORITY.get((severity or "").lower(), "P2")
    return {"category": cat, "priority": pri, "advice": advice,
            "checklist": checklist_title or ""}


def build_conclusion_table(alarms: list, match_fn=None) -> list:
    """告警列表 → 确定性分组结论表

    按 (类别, 预案) 聚合: 同组告警数、级别集合、对象列表;
    优先级取组内成员最高; 输出按 (优先级, 数量降序) 排序。

    Args:
        alarms: 告警 dict 列表 (summary/severity/source/checklist...)
        match_fn: 可选, summary → checklist 预案名 (alarm 自带 checklist 字段优先)

    Returns:
        [{"category", "checklist", "priority", "advice", "count",
          "objects", "severities"}, ...]
    """
    groups: dict = {}
    for a in alarms or []:
        title = a.get("checklist") or ""
        if not title and match_fn:
            try:
                title = match_fn(a.get("summary") or "") or ""
            except Exception:
                title = ""
        cl = classify_alarm(a.get("summary"), a.get("severity"), title)
        key = (cl["category"], cl["checklist"])
        g = groups.get(key)
        if g is None:
            g = {"category": cl["category"], "checklist": cl["checklist"],
                 "priority": cl["priority"], "advice": cl["advice"],
                 "count": 0, "objects": [], "severities": set()}
            groups[key] = g
        g["count"] += 1
        obj = ((a.get("source") or a.get("node") or a.get("host") or "") or "")[:40].strip()
        if obj and obj not in g["objects"] and len(g["objects"]) < 8:
            g["objects"].append(obj)
        g["severities"].add((a.get("severity") or "unknown").lower())

    out = []
    for g in groups.values():
        g["severities"] = sorted(g["severities"])
        # 组优先级 = 成员最高 (P0 最优先; 有 checklist 命中的组不降级)
        member_pri = {SEVERITY_PRIORITY.get(s, "P2") for s in g["severities"]}
        g["priority"] = min(member_pri, key=lambda p: _PRIORITY_ORDER.index(p))
        out.append(g)
    out.sort(key=lambda g: (_PRIORITY_ORDER.index(g["priority"]), -g["count"]))
    return out


def format_conclusion_block(table: list, max_rows: int = 15) -> str:
    """结论表 → 注入 LLM prompt 的确定性结论文本块"""
    if not table:
        return ""
    lines = []
    for g in table[:max_rows]:
        cl = f"〔{g['checklist']}〕" if g["checklist"] else "〔Checklist无匹配〕"
        lines.append(
            f"- {g['priority']} | 类别: {g['category']} | 预案: {cl} | "
            f"告警数: {g['count']} | 级别: {','.join(g['severities'])} | "
            f"对象: {', '.join(g['objects'] or ['—'])} | 确定检查方向: {g['advice']}")
    return ("【规则引擎确定性结论 (分类/预案归属/优先级/分组已定, 不得更改)】\n"
            + "\n".join(lines))


if __name__ == "__main__":
    demo = [
        {"summary": "BGP Peer Down on core-rtr-01", "severity": "critical", "source": "core-rtr-01"},
        {"summary": "Disk usage > 90% on vm-web-01", "severity": "major", "source": "vm-web-01"},
        {"summary": "HTTP service no response", "severity": "minor", "source": "app-01"},
    ]
    for row in format_conclusion_block(build_conclusion_table(demo)).splitlines():
        print(row)
