"""NMS 异常活跃检测 — 从历史库算 per-source 基线, 当前告警超过 Nσ 时标记。

输入: history list (alarm dicts) + current active alarms
输出: 异常源列表 [(source, current_count, baseline_mean, baseline_std, z_score)]

实现: 按 (source, hour-of-day) 桶聚合历史, 计算每个源每个小时的均值与样本标准差;
当前小时内该源的告警数 > mean + z_threshold*std 即判定为异常活跃。

为单测暴露纯函数 compute_anomaly_scores(history, current_alarms, now=None)。
"""
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import math


def _hour_key(ts: str) -> Optional[int]:
    """ISO 或 'M/D/YY, h:MM AM/PM' 格式时间 → 0-23 小时; 失败返回 None"""
    if not ts:
        return None
    ts = ts.strip()
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S',
                '%m/%d/%y, %I:%M %p', '%m/%d/%Y, %I:%M %p'):
        try:
            return datetime.strptime(ts.split('.')[0].split('+')[0], fmt).hour
        except (ValueError, IndexError):
            continue
    return None


def compute_anomaly_scores(history: List[Dict],
                            current_alarms: List[Dict],
                            z_threshold: float = 2.0,
                            min_history_days: int = 3,
                            now: Optional[datetime] = None
                            ) -> List[Dict]:
    """纯函数: 计算当前异常源列表。

    Args:
        history: 历史告警 dict 列表 (来自 {base}:history, 最多 NMS_HISTORY_MAX=2000)
        current_alarms: 当前活跃告警 (按 hour 桶分)
        z_threshold: 几倍标准差算异常 (默认 2σ)
        min_history_days: 源历史样本数低于此值不判定 (冷启动保护)
        now: 当前时间 (测试注入)

    Returns:
        [{"source": str, "current": int, "mean": float, "std": float,
          "z_score": float, "hour": int, "examples": [alarm_id...]}]
        按 z_score 降序, 最多 8 项
    """
    now = now or datetime.now()
    cur_hour = now.hour

    # 历史桶: (source, hour) → [count per day]
    hist_buckets: Dict[Tuple[str, int], List[int]] = defaultdict(list)
    hist_dates: Dict[Tuple[str, int], set] = defaultdict(set)
    for h in history or []:
        src = (h.get("source") or h.get("node") or "").strip()
        if not src:
            continue
        hk = _hour_key(h.get("last_time") or h.get("time") or "")
        if hk is None:
            continue
        key = (src, hk)
        # 用日期去重: 同一源同一小时同一天多次告警计为 1 (避免单次风暴拉高基线)
        date_key = (h.get("last_time") or h.get("time") or "")[:10]
        if date_key in hist_dates[key]:
            continue
        hist_dates[key].add(date_key)
        hist_buckets[key].append(1)

    # 当前桶: (source, cur_hour) → count + 证据告警 ID 列表
    cur_buckets: Dict[str, Dict] = defaultdict(
        lambda: {"current": 0, "examples": []})
    for a in current_alarms or []:
        if a.get("severity") not in ("critical", "major", "minor",
                                       "warning", "indeterminate"):
            continue
        src = (a.get("source") or a.get("node") or "").strip()
        if not src:
            continue
        b = cur_buckets[src]
        b["current"] += 1
        aid = a.get("alarm_id") or a.get("event_id") or a.get("serial")
        if aid and len(b["examples"]) < 3:
            b["examples"].append(aid)

    # 计算异常
    out = []
    for src, b in cur_buckets.items():
        if b["current"] < 2:
            continue  # 当前至少 2 条才参与判定 (单条噪声)
        hist = hist_buckets.get((src, cur_hour), [])
        n_days = len(hist)
        if n_days < min_history_days:
            continue  # 冷启动: 历史样本不足不报异常
        mean = sum(hist) / n_days
        if n_days < 2:
            std = 0.0
        else:
            variance = sum((x - mean) ** 2 for x in hist) / (n_days - 1)
            std = math.sqrt(variance)
        # 冷启动 (std=0): current > mean 且 >= 3 直接报异常
        if std == 0:
            if b["current"] > mean and b["current"] >= 3:
                z = float("inf") if mean == 0 else (b["current"] - mean) / 1.0
            else:
                continue
        else:
            z = (b["current"] - mean) / std
        if z < z_threshold:
            continue
        out.append({
            "source": src,
            "current": b["current"],
            "mean": round(mean, 2),
            "std": round(std, 2),
            "z_score": round(z, 2) if math.isfinite(z) else z,
            "hour": cur_hour,
            "history_days": n_days,
            "examples": b["examples"],
        })
    out.sort(key=lambda d: -(d["z_score"] if math.isfinite(d["z_score"]) else 1e9))
    return out[:8]


def format_anomaly_block(anomalies: List[Dict]) -> str:
    """异常列表 → 注入 prompt 的【异常活跃检测】块"""
    if not anomalies:
        return ""
    lines = []
    for a in anomalies:
        z = a["z_score"]
        z_disp = f"{z:.1f}σ" if math.isfinite(z) else "∞σ (冷启动)"
        lines.append(
            f"- {a['source']}: 当前 {a['current']} 条 (z={z_disp}, "
            f"基线 {a['mean']:.1f}±{a['std']:.1f}/天, "
            f"历史 {a['history_days']} 天) "
            f"→ 异常活跃, 可能正在扩散"
        )
    return ("【异常活跃检测（per-source z-score, 超出历史基线的源）】\n"
            + "\n".join(lines)
            + "\n说明：上列源在该小时的告警数显著超过过去 N 天同一小时的均值+2σ，"
              "可能正在扩散（设备故障早期 / 配置变更 / 攻击 / 计划维护）。\n")