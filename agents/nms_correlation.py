"""
Z-OPS — nms_correlation: NMS 告警关联降噪引擎（纯函数，无 I/O）

把海量原始告警用确定性规则归并为少量「事件簇（incident）」：
- 同对象同事件类型（source + event_key）归并 -> 吸收抖动/重复通知
- 同一规范化摘要跨多源 -> 系统级事件（探针/监控面故障，最大降噪点）

聚类可解释、零 LLM 成本、不触碰采集链路。仅对活跃级别告警聚类。
"""

import re
import hashlib
from collections import defaultdict
from typing import List, Dict

# 与 nms_agent 保持一致的活跃级别集合
_ACTIVE_SEV = {"critical", "major", "minor", "warning", "indeterminate"}

# 级别权重（定级用）
_SEV_WEIGHT = {
    "critical": 100,
    "major": 60,
    "minor": 30,
    "warning": 10,
    "indeterminate": 5,
}

# 级别排序优先级（取「成员最高级别」用，数字越小越严重）
_SEV_RANK = {
    "critical": 0, "major": 1, "minor": 2,
    "warning": 3, "indeterminate": 4,
}


def _sev_weight(severity: str) -> int:
    return _SEV_WEIGHT.get(severity, 0)


def _normalize_summary(summary: str) -> str:
    """规范化摘要：去掉数字/ID/时间戳/IP 尾段等易变部分，保留告警的「类型骨架」。
    用于跨源模式识别 —— 不同设备同一类问题会得到相同的规范化摘要。"""
    if not summary:
        return ""
    s = summary.lower()
    # IPv4 -> <ip>
    s = re.sub(r"\d{1,3}(?:\.\d{1,3}){3}", "<ip>", s)
    # 时间戳/日期 -> <t>
    s = re.sub(r"\d{4}-\d{2}-\d{2}[ t]?[\d:]*", "<t>", s)
    # 十六进制/长 ID -> <id>
    s = re.sub(r"\b(?:0x)?[0-9a-f]{6,}\b", "<id>", s)
    # 其余独立数字 -> <n>
    s = re.sub(r"\d+", "<n>", s)
    # 压缩空白
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _signature(alarm: Dict) -> str:
    """单条告警的对象级签名：优先 source + event_key(回退 alert_key)，
    否则 source + 规范化摘要。"""
    source = (alarm.get("source") or alarm.get("node") or "").strip().lower()
    ekey = (alarm.get("event_key") or alarm.get("alert_key") or "").strip().lower()
    if ekey:
        return f"obj|{source}|{ekey}"
    norm = _normalize_summary(alarm.get("summary") or "")
    return f"obj|{source}|{norm}"


def _is_acked(alarm: Dict) -> bool:
    return str(alarm.get("acknowledged") or alarm.get("status") or "") == "1"


# ── 维度提取：IP 网段 / 告警类型（站点 vs 服务关联分析基础）────────────────────

# 前后不粘连数字/点，避免从 10.1.2.3:80 或版本号中截出错位 IP
_IP4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_TYPE_MAX_LEN = 64


def _alarm_primary_ip(a: Dict) -> str:
    """告警主 IP：source/node/node_alias 为 IP 时优先（设备地址），
    否则回退 summary 中首个 IP（无设备 IP 的场景）。"""
    for k in ("source", "node", "node_alias"):
        v = (a.get(k) or "").strip()
        if _IP4_RE.fullmatch(v):
            return v
    m = _IP4_RE.findall(a.get("summary") or "")
    return m[0] if m else ""


def alarm_segment(a: Dict) -> str:
    """告警所属 /24 网段；无 IP 返回空串。"""
    ip = _alarm_primary_ip(a)
    if not ip:
        return ""
    return ".".join(ip.split(".")[:3]) + ".0/24"


def alarm_type(a: Dict) -> str:
    """告警类型标签：EventGroup > AlertGroup > Class，截断过长名称。"""
    t = (a.get("event_group") or a.get("alarm_type")
         or a.get("aclass") or "(未分类)")
    t = str(t).strip()[:_TYPE_MAX_LEN]
    return t or "(未分类)"


def _scope_of(seg_count: int, source_count: int) -> str:
    """事件簇范围：cross=跨站点 / site=同站点多对象 / object=单对象。"""
    if seg_count >= 2:
        return "cross"
    if source_count >= 2:
        return "site"
    return "object"


def analyze_scope(alarms: List[Dict], top_n: int = 7) -> Dict:
    """站点/服务关联分析：按 IP 网段 × 告警类型统计活跃告警，
    给出聚集度评分与定性结论（纯规则，无 LLM）。

    - 站点聚集度：某网段告警占比 × 该网段类型多样度（类型越多越像站点性故障）
    - 服务扩散度：某类型告警占比 × 该类型跨网段数（跨段越多越像服务/协议面故障）
    """
    total = 0
    seg_counts: Dict[str, int] = {}
    type_counts: Dict[str, int] = {}
    pair_counts: Dict[tuple, int] = {}
    seg_types: Dict[str, set] = defaultdict(set)
    seg_sources: Dict[str, set] = defaultdict(set)
    type_segs: Dict[str, set] = defaultdict(set)
    type_sources: Dict[str, set] = defaultdict(set)
    no_ip = 0

    for a in alarms:
        if a.get("severity") not in _ACTIVE_SEV:
            continue
        total += 1
        t = alarm_type(a)
        src = (a.get("source") or a.get("node") or "").strip()
        seg = alarm_segment(a)
        type_counts[t] = type_counts.get(t, 0) + 1
        type_sources[t].add(src)
        if seg:
            seg_counts[seg] = seg_counts.get(seg, 0) + 1
            seg_types[seg].add(t)
            seg_sources[seg].add(src)
            type_segs[t].add(seg)
            pair_counts[(seg, t)] = pair_counts.get((seg, t), 0) + 1
        else:
            no_ip += 1

    if total == 0:
        return {"active_total": 0, "segment_total": 0, "type_total": 0,
                "no_ip": 0, "segments": [], "types": [], "matrix": None,
                "judgement": {"primary": "none", "site_score": 0,
                              "service_score": 0,
                              "text": "当前范围暂无活跃告警"}}

    segments = [{
        "cidr": s, "count": n,
        "share": round(n * 100.0 / total, 1),
        "types": len(seg_types[s]), "sources": len(seg_sources[s]),
    } for s, n in sorted(seg_counts.items(), key=lambda x: -x[1])[:10]]

    types = [{
        "name": t, "count": n,
        "share": round(n * 100.0 / total, 1),
        "segments": len(type_segs[t]), "sources": len(type_sources[t]),
    } for t, n in sorted(type_counts.items(), key=lambda x: -x[1])[:10]]

    # 站点/服务评分 (0~100)
    site_score = max(
        (n / total) * min(1.0, len(seg_types[s]) / 5.0)
        for s, n in seg_counts.items()) * 100 if seg_counts else 0
    service_score = max(
        (n / total) * min(1.0, len(type_segs[t]) / 5.0)
        for t, n in type_counts.items()) * 100 if type_counts else 0
    site_score, service_score = round(site_score), round(service_score)

    top_seg = segments[0] if segments else None
    top_type = types[0] if types else None
    if max(site_score, service_score) < 25:
        primary, text = "scattered", "告警分散：未发现明显的站点或服务集中特征"
    elif site_score >= 35 and service_score >= 35:
        primary = "mixed"
        text = (f"站点聚集与服务扩散并存：Top 网段 {top_seg['cidr']} 承载 "
                f"{top_seg['share']}% 活跃告警（{top_seg['types']} 类 / "
                f"{top_seg['sources']} 个来源），同时 {top_type['name']} "
                f"跨 {top_type['segments']} 个网段") if top_seg and top_type else "站点与服务特征并存"
    elif site_score > service_score:
        primary = "site"
        text = (f"站点异常主导：Top 网段 {top_seg['cidr']} 承载 "
                f"{top_seg['share']}% 活跃告警（{top_seg['types']} 类 / "
                f"{top_seg['sources']} 个来源），优先排查站点侧链路/设备") if top_seg else "站点异常主导"
    else:
        primary = "service"
        text = (f"服务异常主导：{top_type['name']} 跨 {top_type['segments']} 个网段 / "
                f"{top_type['sources']} 个来源，占 {top_type['share']}%，"
                f"优先排查共享服务/监控面") if top_type else "服务异常主导"

    # 网段 × 类型 矩阵（Top N + 其他，覆盖全部活跃告警）
    top_segs = [s for s, _ in sorted(seg_counts.items(), key=lambda x: -x[1])[:top_n]]
    top_types = [t for t, _ in sorted(type_counts.items(), key=lambda x: -x[1])[:top_n]]
    other_seg = any(s not in top_segs for s in seg_counts)
    other_type = any(t not in top_types for t in type_counts)
    row_keys = top_segs + (["其他"] if other_seg else [])
    col_keys = top_types + (["其他"] if other_type else [])
    counts = []
    for s in row_keys:
        row = []
        for t in col_keys:
            if s == "其他":
                val = sum(v for (ss, tt), v in pair_counts.items()
                          if ss not in top_segs and tt == t)
                if t == "其他":
                    val = sum(v for (ss, tt), v in pair_counts.items()
                              if ss not in top_segs and tt not in top_types)
            elif t == "其他":
                val = sum(v for (ss, tt), v in pair_counts.items()
                          if ss == s and tt not in top_types)
            else:
                val = pair_counts.get((s, t), 0)
            row.append(val)
        counts.append(row)

    return {
        "active_total": total,
        "segment_total": len(seg_counts),
        "type_total": len(type_counts),
        "no_ip": no_ip,
        "segments": segments,
        "types": types,
        "matrix": {
            "segments": row_keys,
            "types": col_keys,
            "counts": counts,
            "row_totals": [sum(r) for r in counts],
            "col_totals": [sum(counts[i][j] for i in range(len(counts)))
                           for j in range(len(col_keys))],
        },
        "judgement": {
            "primary": primary,
            "site_score": site_score,
            "service_score": service_score,
            "text": text,
        },
    }


def analyze_scope_by_group(alarms: List[Dict], top_n: int = 7) -> Dict[str, Dict]:
    """按告警分组分别计算站点/服务关联分析，供前端分组过滤时保持口径一致。

    跳过 AllEvents（等同全量，API 回退到 scope_analysis）。
    """
    groups = sorted({g for a in alarms for g in (a.get("groups") or [])})
    out: Dict[str, Dict] = {}
    for g in groups:
        if g == "AllEvents":
            continue
        subset = [a for a in alarms if g in (a.get("groups") or [])]
        if subset:
            out[g] = analyze_scope(subset, top_n)
    return out


def _priority(severity: str, member_count: int, source_count: int,
              unacked_count: int, is_systemic: bool) -> int:
    """事件定级分：级别权重 + 数量因子 + 扩散因子(仅系统级) + 未确认加成。"""
    score = _sev_weight(severity)
    score += min(20, member_count)
    if is_systemic:
        score += min(30, source_count)
    if unacked_count > 0:
        score += 10
    return score


def _min_time(a: str, b: str) -> str:
    """取较早时间（空值让位于非空）。字符串时间可直接字典序比较（ISO/同格式）。"""
    if not a:
        return b
    if not b:
        return a
    return a if a <= b else b


def _max_time(a: str, b: str) -> str:
    if not a:
        return b
    if not b:
        return a
    return a if a >= b else b


def correlate_alarms(alarms: List[Dict],
                     systemic_min_sources: int = 5,
                     new_ids: set = None) -> List[Dict]:
    """把告警列表归并为按 priority 降序的 incidents。

    Args:
        alarms: 原始告警列表（仅活跃级别参与聚类）。
        systemic_min_sources: 系统级事件的最小跨源数。
        new_ids: 本轮新增告警的 alarm_id 集合（用于标注每簇 new_count）。
    Returns:
        incidents 列表（按 priority 降序）。
    """
    new_ids = new_ids or set()

    # 1&2. 单趟扫描：过滤活跃级别，规范化摘要只算一次，同时完成
    #      对象级签名分桶 + 规范化摘要成员聚合 + 跨源统计。
    #      （避免对海量告警重复调用正则规范化，65k+ 数据下这是关键性能点）
    buckets: Dict[str, List[Dict]] = {}
    norm_members: Dict[str, List[Dict]] = {}
    norm_sources: Dict[str, set] = {}
    for a in alarms:
        if a.get("severity") not in _ACTIVE_SEV:
            continue
        source = (a.get("source") or a.get("node") or "").strip().lower()
        ekey = (a.get("event_key") or a.get("alert_key") or "").strip().lower()
        norm = _normalize_summary(a.get("summary") or "")
        sig = f"obj|{source}|{ekey}" if ekey else f"obj|{source}|{norm}"
        buckets.setdefault(sig, []).append(a)
        if norm:
            norm_members.setdefault(norm, []).append(a)
            norm_sources.setdefault(norm, set()).add(source)
    systemic_norms = {n for n, srcs in norm_sources.items()
                      if len(srcs) >= systemic_min_sources}

    # 3. 归并：系统级的成员从对象级桶中抽出，合并成一个跨源事件
    incidents: List[Dict] = []
    consumed = set()  # 已被系统级事件吸收的对象级签名

    # 3a. 先建系统级事件（成员在上一步已按规范化摘要聚合，无需重新扫描）
    for norm in systemic_norms:
        members = norm_members.get(norm, [])
        if not members:
            continue
        for a in members:
            consumed.add(id(a))
        incidents.append(_build_incident(members, norm, True, new_ids))

    # 3b. 对象级事件（排除已被系统级吸收的成员）
    for sig, members in buckets.items():
        remaining = [a for a in members if id(a) not in consumed]
        if not remaining:
            continue
        incidents.append(_build_incident(remaining, sig, False, new_ids))

    # 4. 按 priority 降序，其次成员数降序
    incidents.sort(key=lambda x: (-x["priority"], -x["member_count"]))
    return incidents


def _build_incident(members: List[Dict], sig: str, is_systemic: bool,
                    new_ids: set) -> Dict:
    """由一组成员告警构造 incident。"""
    incident_id = hashlib.md5(
        (("sys|" if is_systemic else "obj|") + sig).encode("utf-8", "ignore")
    ).hexdigest()[:16]

    # 最高级别
    severity = min(
        (m.get("severity", "indeterminate") for m in members),
        key=lambda s: _SEV_RANK.get(s, 99),
    )

    sources = {}
    groups = set()
    type_counts: Dict[str, int] = {}
    seg_counts: Dict[str, int] = {}
    first_time = ""
    last_time = ""
    acked = 0
    unacked = 0
    new_count = 0
    alarm_ids = []
    for m in members:
        src = (m.get("source") or m.get("node") or "").strip()
        if src:
            sources[src] = sources.get(src, 0) + 1
        t = alarm_type(m)
        type_counts[t] = type_counts.get(t, 0) + 1
        seg = alarm_segment(m)
        if seg:
            seg_counts[seg] = seg_counts.get(seg, 0) + 1
        for g in (m.get("groups") or ([m.get("group")] if m.get("group") else [])):
            if g:
                groups.add(g)
        first_time = _min_time(first_time, m.get("first_time") or m.get("time") or "")
        last_time = _max_time(last_time, m.get("last_time") or m.get("time") or "")
        if _is_acked(m):
            acked += 1
        else:
            unacked += 1
        aid = m.get("alarm_id", "")
        if aid:
            alarm_ids.append(aid)
            if aid in new_ids:
                new_count += 1

    # 代表标题：取最严重成员的摘要
    rep = min(members, key=lambda m: _SEV_RANK.get(m.get("severity"), 99))
    title = (rep.get("summary") or "").strip()[:160] or "(无摘要)"

    top_sources = [s for s, _ in sorted(sources.items(), key=lambda x: -x[1])[:5]]

    return {
        "incident_id": incident_id,
        "title": title,
        "severity": severity,
        "category": "systemic" if is_systemic else "object",
        "member_count": len(members),
        "source_count": len(sources),
        "sources": top_sources,
        "groups": sorted(groups),
        "first_time": first_time,
        "last_time": last_time,
        "acked_count": acked,
        "unacked_count": unacked,
        "new_count": new_count,
        "alarm_ids": alarm_ids[:500],
        "priority": _priority(severity, len(members), len(sources),
                              unacked, is_systemic),
        # 维度下钻：保留较完整的网段/类型（限 40 项）供前端联动筛选，
        # 仅 Top6 会导致下钻 Top10 后段网段时漏事件；卡片展示仍只取首位。
        "segments": [{"cidr": s, "count": n} for s, n in
                     sorted(seg_counts.items(), key=lambda x: -x[1])[:40]],
        "types": [{"name": t, "count": n} for t, n in
                  sorted(type_counts.items(), key=lambda x: -x[1])[:40]],
        "scope": _scope_of(len(seg_counts), len(sources)),
    }
