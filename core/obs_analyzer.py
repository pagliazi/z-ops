"""
Z-OPS 可观测性分析器 — 失败可见化 / 根因分类 / 优化方向 / 优化闭环核算

职责:
1. 错误指纹归一化与聚类 (相同根因的失败合并为一个错误组)
2. 失败分类 (代码缺陷 / 超时 / 网络 / 鉴权 / 限流 / 数据缺失 / LLM 输出)
3. 按分类给出可执行的优化方向 (方向 + 落地动作 + 预期收益)
4. 延迟分析 (P50/P95/P99 + 慢调用离群点 + 延迟优化方向)
5. 优化台账读写，并用真实执行数据核算优化结果 (前后对比, 复现次数)

数据全部来自 ExecutionRegistry 写入 Redis 的真实执行记录，不做任何模拟。
"""

import json
import re
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("zops")

OPT_LEDGER_PATH = Path("/opt/zops/data/optimizations.json")

# 慢调用阈值 (毫秒) — 超过此值视为需要关注的慢调用
SLOW_THRESHOLD_MS = 30_000

# 失败池保留条数 (与 ExecutionRegistry.FAILURE_MAX 一致，仅用于文案提示)
FAILURE_POOL_HINT = 500

# ─────────────────────────────────────────────────────────
# 错误指纹归一化
# ─────────────────────────────────────────────────────────

_RE_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_RE_HEX = re.compile(r"\b0x[0-9a-f]+\b", re.I)
_RE_PATH = re.compile(r"(/[\w.\-]+){2,}")
_RE_URL = re.compile(r"https?://[^\s'\"]+")
_RE_IP = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")
_RE_NUM = re.compile(r"\b\d+(\.\d+)?\b")
_RE_WS = re.compile(r"\s+")


def normalize_error(err: str) -> str:
    """把具体错误文本归一化成可聚类的指纹。

    保留引号内的标识符 (如 name 're' is not defined 中的 're')，
    因为它对定位代码缺陷是关键信息；只抹掉真正的可变量 (UUID/IP/数字/路径)。
    """
    if not err:
        return "(空错误信息)"
    s = str(err).strip()
    s = _RE_URL.sub("<url>", s)
    s = _RE_UUID.sub("<uuid>", s)
    s = _RE_HEX.sub("<hex>", s)
    s = _RE_IP.sub("<ip>", s)
    s = _RE_PATH.sub("<path>", s)
    s = _RE_NUM.sub("N", s)
    s = _RE_WS.sub(" ", s)
    return s[:180]


# ─────────────────────────────────────────────────────────
# 失败分类 + 优化方向规则
# ─────────────────────────────────────────────────────────
# 每条规则: (匹配正则, 分类 key, 中文标签, 严重度, 方向标题, 落地动作列表)

_RULES: List[Tuple[str, str, str, str, str, List[str]]] = [
    (
        r"is not defined|has no attribute|NoneType|unsupported operand|"
        r"object is not subscriptable|KeyError|IndexError|"
        r"referenced before assignment|takes \d+ positional|unhashable type",
        "code_defect", "代码缺陷", "high",
        "定位并修复 Agent 代码缺陷，补齐空值/类型防御",
        [
            "在报错 Agent 源码中定位该符号，检查作用域与顶层导入是否缺失",
            "对外部 API 返回值统一用 `data.get(key) or default` 兜底，禁止直接 len()/下标",
            "为该输入样本补一条回归用例，确保修复后不再复现",
        ],
    ),
    (
        r"timeout|timed out|ReadTimeout|ConnectTimeout|TimeoutError|超时",
        "timeout", "超时", "high",
        "拉长慢路径超时窗口 + 串行化竞争的 LLM 调用",
        [
            "核对失败间隔是否精确等于客户端超时值，据此区分真超时与网关掐断",
            "对长文本生成路径单独提高读超时，连接超时保持短值以便快速失败",
            "同一 Agent 内并发 LLM 调用改为 asyncio.Lock 串行化，避免互相拖慢",
        ],
    ),
    (
        r"Connection refused|ConnectError|name resolution|getaddrinfo|"
        r"Network is unreachable|502|503|504|Bad Gateway|SSLError|EOF occurred",
        "network", "网络/网关", "high",
        "网关与 DNS 侧加固，失败快速降级",
        [
            "对上游网关调用增加有限次退避重试，重试耗尽后走本地降级回答",
            "为固定域名配置解析缓存或 hosts 兜底，规避瞬时 DNS 故障",
            "在错误日志中带上 type(e).__name__，避免空错误信息无法定位",
        ],
    ),
    (
        r"\b401\b|\b403\b|Unauthorized|Forbidden|invalid token|token.*expired|权限不足",
        "auth", "认证鉴权", "medium",
        "凭据与权限链路收敛到统一中心",
        [
            "检查上游平台 token 是否过期，改为按需刷新而非启动时一次性加载",
            "鉴权失败直接返回可读提示，不进入重试消耗时间",
        ],
    ),
    (
        r"\b429\b|rate limit|too many requests|quota",
        "rate_limit", "限流/配额", "medium",
        "控制并发与调用频次",
        [
            "为该上游接口加并发上限与令牌桶限流",
            "对可缓存的查询结果加短 TTL 缓存，削减重复调用",
        ],
    ),
    (
        r"JSONDecodeError|Expecting value|invalid json|无法解析|not valid JSON|函数调用格式",
        "llm_output", "LLM 输出异常", "medium",
        "约束模型输出格式并做解析兜底",
        [
            "对 function_call / JSON 输出增加解析失败重试一次 (降温 + 强约束提示)",
            "解析失败时降级为纯文本回答，避免整轮请求失败",
        ],
    ),
    (
        r"\b404\b|not found|无结果|empty result|no such",
        "data_missing", "数据缺失", "low",
        "补齐多路召回与友好空结果提示",
        [
            "直接查询无结果时走关键词变体多路召回，仍无结果再降级问答",
            "空结果返回明确提示与建议查询方式，不作为失败上报",
        ],
    ),
]

_FALLBACK = (
    "unknown", "未分类", "medium",
    "补充上下文后再定位",
    [
        "在该 Agent 的失败分支补充结构化日志 (输入摘要 / 上游响应 / 异常类型)",
        "复现该输入样本，确认是否为偶发",
    ],
)


def classify_error(err: str) -> dict:
    """返回错误分类与对应的优化方向。"""
    text = str(err or "")
    for pattern, key, label, severity, title, actions in _RULES:
        if re.search(pattern, text, re.I):
            return {
                "category": key, "category_label": label, "severity": severity,
                "direction": {"title": title, "actions": list(actions)},
            }
    key, label, severity, title, actions = _FALLBACK
    return {
        "category": key, "category_label": label, "severity": severity,
        "direction": {"title": title, "actions": list(actions)},
    }


# ─────────────────────────────────────────────────────────
# 统计辅助
# ─────────────────────────────────────────────────────────

def percentile(values: List[float], p: float) -> float:
    """线性插值分位数 (p 取 0~100)。"""
    if not values:
        return 0.0
    vals = sorted(values)
    if len(vals) == 1:
        return round(vals[0], 1)
    k = (len(vals) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 1)


def _ts(rec: dict) -> float:
    try:
        return float(rec.get("started_at") or 0)
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────────────────────────────────
# 失败聚类
# ─────────────────────────────────────────────────────────

def group_failures(failures: List[dict], now: Optional[float] = None,
                   sample_limit: int = 5) -> List[dict]:
    """把失败记录按错误指纹聚类，每组附带分类、样本与优化方向。"""
    now = now or time.time()
    buckets: Dict[str, dict] = {}

    for rec in failures or []:
        err = rec.get("error") or rec.get("output_summary") or ""
        sig = normalize_error(err)
        g = buckets.get(sig)
        if g is None:
            meta = classify_error(err)
            g = buckets[sig] = {
                "signature": sig,
                "sample_error": str(err)[:400],
                "category": meta["category"],
                "category_label": meta["category_label"],
                "severity": meta["severity"],
                "direction": meta["direction"],
                "count": 0,
                "agents": [],
                "tools": [],
                "sessions": [],
                "first_seen": _ts(rec),
                "last_seen": _ts(rec),
                "samples": [],
                "recent_24h": 0,
                "recent_7d": 0,
            }
        g["count"] += 1
        ts = _ts(rec)
        if ts:
            g["first_seen"] = min(g["first_seen"] or ts, ts)
            g["last_seen"] = max(g["last_seen"] or ts, ts)
            if now - ts <= 86400:
                g["recent_24h"] += 1
            if now - ts <= 7 * 86400:
                g["recent_7d"] += 1
        for field, key in (("agents", "agent_id"), ("tools", "tool_name"), ("sessions", "session_id")):
            v = rec.get(key)
            if v and v not in g[field]:
                g[field].append(v)
        if len(g["samples"]) < sample_limit:
            g["samples"].append({
                "exec_id": rec.get("exec_id", ""),
                "agent_id": rec.get("agent_id", ""),
                "tool_name": rec.get("tool_name", ""),
                "exec_type": rec.get("exec_type", ""),
                "session_id": rec.get("session_id", ""),
                "username": rec.get("username", ""),
                "input_summary": rec.get("input_summary", ""),
                "output_summary": rec.get("output_summary", ""),
                "error": rec.get("error", ""),
                "latency_ms": rec.get("latency_ms", 0),
                "started_at": ts,
                "model": rec.get("model", ""),
            })

    groups = list(buckets.values())
    for g in groups:
        # 活跃度: 7 天内仍在发生 = 活跃; 否则视为已消退 (待复盘/已修复)
        g["state"] = "active" if g["recent_7d"] > 0 else "dormant"
        g["silent_days"] = round((now - (g["last_seen"] or now)) / 86400, 1)
        g["session_count"] = len(g["sessions"])
    # 活跃优先，其次按次数
    groups.sort(key=lambda x: (x["state"] != "active", -x["count"], -(x["last_seen"] or 0)))
    return groups


# ─────────────────────────────────────────────────────────
# 延迟分析
# ─────────────────────────────────────────────────────────

def analyze_latency(records: List[dict], slow_ms: int = SLOW_THRESHOLD_MS,
                    outlier_limit: int = 25) -> dict:
    """按 Agent 聚合延迟分位数，并挑出慢调用离群点与优化方向。"""
    per_agent: Dict[str, List[float]] = {}
    per_tool: Dict[str, List[float]] = {}
    outliers: List[dict] = []

    for rec in records or []:
        try:
            lat = float(rec.get("latency_ms") or 0)
        except (TypeError, ValueError):
            continue
        aid = rec.get("agent_id") or "unknown"
        per_agent.setdefault(aid, []).append(lat)
        tkey = f"{aid}/{rec.get('tool_name') or rec.get('exec_type') or '-'}"
        per_tool.setdefault(tkey, []).append(lat)
        if lat >= slow_ms:
            outliers.append({
                "exec_id": rec.get("exec_id", ""),
                "agent_id": aid,
                "tool_name": rec.get("tool_name") or rec.get("exec_type") or "-",
                "session_id": rec.get("session_id", ""),
                "input_summary": rec.get("input_summary", ""),
                "latency_ms": lat,
                "success": rec.get("success", True),
                "started_at": _ts(rec),
            })

    agents = []
    for aid, vals in per_agent.items():
        slow_cnt = sum(1 for v in vals if v >= slow_ms)
        agents.append({
            "agent_id": aid,
            "calls": len(vals),
            "avg_ms": round(sum(vals) / len(vals), 1),
            "p50_ms": percentile(vals, 50),
            "p95_ms": percentile(vals, 95),
            "p99_ms": percentile(vals, 99),
            "max_ms": round(max(vals), 1),
            "slow_calls": slow_cnt,
            "slow_ratio": round(slow_cnt / len(vals) * 100, 1),
        })
    agents.sort(key=lambda x: -x["p95_ms"])

    tools = []
    for tkey, vals in per_tool.items():
        if len(vals) < 2:
            continue
        tools.append({
            "target": tkey,
            "calls": len(vals),
            "avg_ms": round(sum(vals) / len(vals), 1),
            "p95_ms": percentile(vals, 95),
            "max_ms": round(max(vals), 1),
        })
    tools.sort(key=lambda x: -x["p95_ms"])

    outliers.sort(key=lambda x: -x["latency_ms"])
    return {
        "slow_threshold_ms": slow_ms,
        "agents": agents,
        "tools": tools[:15],
        "outliers": outliers[:outlier_limit],
        "directions": _latency_directions(agents),
    }


def _latency_directions(agent_stats: List[dict]) -> List[dict]:
    """针对延迟最差的 Agent 给出优化方向 (基于真实分位数, 阈值判定)。"""
    out = []
    for a in agent_stats:
        p95 = a["p95_ms"]
        if p95 < slow_soft_threshold():
            continue
        actions = [
            "拆分长链路: 把「取全量再本地过滤」改为上游服务端过滤/分页收敛",
            "对稳定不变的上游查询结果加短 TTL 缓存，避免同问重复拉取",
        ]
        if a["slow_ratio"] >= 30:
            actions.append(f"慢调用占比 {a['slow_ratio']}%，优先并发化独立子查询并设并发上限")
        if p95 >= 120_000:
            actions.append("P95 超 2 分钟，考虑异步任务化 + 进度事件回传，避免请求侧长时间等待")
        out.append({
            "agent_id": a["agent_id"],
            "severity": "high" if p95 >= 120_000 else "medium",
            "title": f"{a['agent_id']} P95 {round(p95/1000, 1)}s 需收敛",
            "evidence": f"{a['calls']} 次调用 / 平均 {round(a['avg_ms']/1000, 1)}s / P95 {round(p95/1000, 1)}s / 慢调用 {a['slow_calls']} 次",
            "actions": actions,
        })
    return out


def slow_soft_threshold() -> int:
    """给出延迟方向的最低触发线 (毫秒)。"""
    return 20_000


# ─────────────────────────────────────────────────────────
# 优化台账 (真实优化落地 + 结果核算)
# ─────────────────────────────────────────────────────────

STATUS_ORDER = ["planned", "in_progress", "landed", "verified", "rejected", "rolled_back"]
STATUS_LABELS = {
    "planned": "待优化",
    "in_progress": "进行中",
    "landed": "已落地",
    "verified": "已验证",
    "rejected": "已放弃",
    "rolled_back": "已回滚",
}


def _file_fingerprint(path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def build_landed_anchor(files: List) -> dict:
    """标记落地时的代码锚定: git commit + 涉及文件指纹, 供漂移检测比对"""
    anchor: Dict[str, Any] = {"ts": time.time()}
    try:
        import subprocess
        out = subprocess.run(
            ["git", "-C", str(OPT_LEDGER_PATH.parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            anchor["git"] = out.stdout.strip()
    except Exception:
        pass
    fps = {}
    root = OPT_LEDGER_PATH.parent.parent  # data/optimizations.json → 项目根
    for f in files or []:
        p = str(f).split(":", 1)[0].strip()
        if not p:
            continue
        fp = Path(p) if p.startswith("/") else (root / p)
        try:
            if fp.is_file():
                fps[p] = _file_fingerprint(fp)
        except Exception:
            continue
    if fps:
        anchor["files"] = fps
    return anchor


def check_anchor_drift(item: dict) -> Optional[dict]:
    """落地后涉及文件再变更 → 台账可能与代码脱节 (记账滞后自动暴露)"""
    anchor = item.get("landed_anchor") or {}
    fps = anchor.get("files") or {}
    if not fps:
        return None
    changed, missing = [], []
    root = OPT_LEDGER_PATH.parent.parent  # data/optimizations.json → 项目根
    for p, h in fps.items():
        fp = Path(p) if p.startswith("/") else (root / p)
        try:
            if not fp.is_file():
                missing.append(p)
            elif _file_fingerprint(fp) != h:
                changed.append(p)
        except Exception:
            missing.append(p)
    if not changed and not missing:
        return None
    return {"changed": changed, "missing": missing,
            "anchored_at": anchor.get("ts"), "git": anchor.get("git", "")}


def load_ledger() -> List[dict]:
    if not OPT_LEDGER_PATH.exists():
        return []
    try:
        data = json.loads(OPT_LEDGER_PATH.read_text(encoding="utf-8"))
        items = data.get("optimizations", data) if isinstance(data, dict) else data
        return items if isinstance(items, list) else []
    except Exception as e:
        logger.warning(f"[obs_analyzer] 优化台账读取失败: {e}")
        return []


def save_ledger(items: List[dict]) -> None:
    OPT_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OPT_LEDGER_PATH.with_suffix(".json.tmp")
    payload = {"updated_at": time.time(), "optimizations": items}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OPT_LEDGER_PATH)


def evaluate_optimization(item: dict, failures: List[dict], records: List[dict],
                          now: Optional[float] = None,
                          guardrail: Optional[dict] = None) -> dict:
    """用真实执行数据核算一条优化的效果。

    metric.type:
      - error_signature : 统计该错误在观测起点前后的发生次数 (是否复现)
      - error_category  : 统计该错误分类在观测起点后的发生次数
      - latency         : 对比目标 Agent 当前 avg/P95 与立项时记录的基线
      - manual          : 无自动指标，仅展示代码/验证证据
    """
    now = now or time.time()
    metric = item.get("metric") or {}
    mtype = metric.get("type", "manual")
    landed_at = float(item.get("landed_at") or 0)

    result: Dict[str, Any] = {
        "metric_type": mtype,
        "verdict": "no_data",
        "verdict_label": "数据不足",
        "observed_days": round((now - landed_at) / 86400, 1) if landed_at else 0,
        "detail": "",
        "before": None,
        "after": None,
        "unit": "",
        "data_available": False,
    }

    # 尚未落地的优化只展示立项基线与当前实测值，不做效果判定
    if item.get("status") in ("planned", "in_progress"):
        result.update({"verdict": "pending", "verdict_label": "尚未落地"})
        if mtype == "latency":
            aid = metric.get("agent_id", "")
            vals = [float(r.get("latency_ms") or 0) for r in records or []
                    if r.get("agent_id") == aid]
            if vals:
                cur = round(sum(vals) / len(vals), 1)
                result.update({
                    "before": metric.get("baseline_avg_ms"), "after": cur, "unit": "ms",
                    "data_available": True, "sample_calls": len(vals),
                    "p95_after_ms": percentile(vals, 95),
                    "detail": f"当前实测平均 {round(cur/1000,1)}s / P95 "
                              f"{round(percentile(vals,95)/1000,1)}s（{len(vals)} 次调用），标记落地后将自动锚定并核算收益",
                })
                return result
        result["detail"] = item.get("verification") or "已立项；标记落地时自动锚定代码并开始效果核算"
        return result

    if mtype == "error_signature":
        match = (metric.get("match") or "").lower()
        before = after = 0
        last_hit = 0.0
        for rec in failures or []:
            err = str(rec.get("error") or rec.get("output_summary") or "").lower()
            if match and match in err:
                ts = _ts(rec)
                last_hit = max(last_hit, ts)
                # 观测起点取开区间: 以最后一次失败为起点时，该次失败本身算在落地前
                if landed_at and ts > landed_at:
                    after += 1
                else:
                    before += 1
        # 观测起点后该 Agent 实际跑了多少次 (说明观测样本量是否足够)
        target_agent = metric.get("agent_id", "")
        covered = sum(
            1 for r in records or []
            if _ts(r) > landed_at and (not target_agent or r.get("agent_id") == target_agent)
        )
        result.update({
            "before": before, "after": after, "unit": "次",
            "data_available": bool(before or after or covered),
            "covered_calls": covered,
            "last_occurrence": last_hit or None,
        })
        if after > 0:
            result.update({"verdict": "recurring", "verdict_label": "仍在复现",
                           "detail": f"落地后仍发生 {after} 次，需重新定位根因"})
        elif before > 0:
            result.update({"verdict": "resolved", "verdict_label": "已消除",
                           "detail": f"落地前 {before} 次 → 落地后 0 次，"
                                     f"观测 {result['observed_days']} 天"
                                     + (f"，其间目标 Agent 执行 {covered} 次" if covered else "")})
        else:
            result["detail"] = "台账中未匹配到历史失败样本，无法核算"

    elif mtype == "error_category":
        cat = metric.get("category", "")
        before = after = 0
        for rec in failures or []:
            err = rec.get("error") or rec.get("output_summary") or ""
            if classify_error(err)["category"] != cat:
                continue
            if landed_at and _ts(rec) > landed_at:
                after += 1
            else:
                before += 1
        covered = sum(1 for r in records or [] if _ts(r) > landed_at)
        result.update({
            "before": before, "after": after, "unit": "次",
            "data_available": True, "covered_calls": covered,
        })
        if after > 0:
            result.update({"verdict": "recurring", "verdict_label": "仍在复现",
                           "detail": f"落地后该类错误仍发生 {after} 次"})
        elif before > 0:
            result.update({"verdict": "resolved", "verdict_label": "已消除",
                           "detail": f"落地前失败池中 {before} 次 → 落地后 "
                                     f"{result['observed_days']} 天内 0 次"
                                     + (f"，覆盖 {covered} 次执行" if covered else "")})
        else:
            result.update({"verdict": "clean", "verdict_label": "无同类失败",
                           "detail": f"当前失败池（保留最近 {FAILURE_POOL_HINT} 条）中无该类错误，"
                                     f"落地后已观测 {result['observed_days']} 天"
                                     + (f"、{covered} 次执行" if covered else "")})

    elif mtype == "latency":
        aid = metric.get("agent_id", "")
        baseline = metric.get("baseline_avg_ms")
        vals = [
            float(r.get("latency_ms") or 0) for r in records or []
            if r.get("agent_id") == aid and (not landed_at or _ts(r) >= landed_at)
        ]
        if vals and baseline:
            cur = round(sum(vals) / len(vals), 1)
            delta = (cur - baseline) / baseline * 100
            result.update({
                "before": round(baseline, 1), "after": cur, "unit": "ms",
                "data_available": True,
                "delta_pct": round(delta, 1),
                "p95_after_ms": percentile(vals, 95),
                "sample_calls": len(vals),
            })
            if delta <= -10:
                result.update({"verdict": "improved", "verdict_label": "已改善",
                               "detail": f"平均耗时 {round(baseline/1000,1)}s → {round(cur/1000,1)}s"
                                         f" ({round(delta,1)}%)，样本 {len(vals)} 次"})
            elif delta >= 10:
                result.update({"verdict": "degraded", "verdict_label": "反而退化",
                               "detail": f"平均耗时 {round(baseline/1000,1)}s → {round(cur/1000,1)}s"
                                         f" (+{round(delta,1)}%)，需回滚复查"})
            else:
                result.update({"verdict": "flat", "verdict_label": "无显著变化",
                               "detail": f"平均耗时 {round(cur/1000,1)}s，与基线相差 {round(delta,1)}%"})
        elif vals:
            cur = round(sum(vals) / len(vals), 1)
            result.update({
                "after": cur, "unit": "ms", "data_available": True,
                "p95_after_ms": percentile(vals, 95), "sample_calls": len(vals),
                "verdict": "baseline_missing", "verdict_label": "待建基线",
                "detail": f"当前平均 {round(cur/1000,1)}s / P95 "
                          f"{round(percentile(vals,95)/1000,1)}s，台账未记录可比基线",
            })
        else:
            result["detail"] = "观测窗口内该 Agent 无执行记录"

    else:  # manual
        result.update({
            "verdict": "evidence_only", "verdict_label": "以证据为准",
            "detail": item.get("verification") or "以代码与验证记录为证据，无自动指标",
        })

    # 护栏核算 (质量回归探测): 延迟/错误收益之外, 副作用指标显著恶化则
    # 整体判为疑似回归并标红 (如: 提速 16s 但 KB 召回变差)
    if guardrail and guardrail.get("regressions"):
        gsig = guardrail.get("signals") or {}
        lines = []
        for sig in guardrail["regressions"]:
            s = gsig.get(sig) or {}
            if s.get("before_pct") is not None and s.get("after_pct") is not None:
                lines.append(f"{s.get('label', sig)} {s['before_pct']}% → {s['after_pct']}%")
        result.update({
            "verdict": "regression", "verdict_label": "疑似回归(护栏)",
            "guardrail": guardrail,
            "detail": "落地后护栏指标显著恶化: " + "；".join(lines)
                      + f"（原判定: {result.get('verdict_label')}）",
        })
    elif guardrail:
        result["guardrail"] = guardrail

    return result


def build_optimizations(failures: List[dict], records: List[dict],
                        now: Optional[float] = None,
                        guardrails: Optional[dict] = None) -> dict:
    """读取台账 + 真实数据核算，返回看板数据。

    guardrails: {item_id: 护栏核算结果} (API 层按每条优化各自的 landed_at 异步预取,
    core.guardrail), 用于质量回归标红; 已回滚归档的项不再核算回归。
    """
    now = now or time.time()
    items = load_ledger()
    enriched = []
    for it in items:
        e = dict(it)
        e["status_label"] = STATUS_LABELS.get(it.get("status", "planned"), it.get("status", ""))
        g = None
        if it.get("status") in ("landed", "verified") and it.get("landed_at"):
            g = (guardrails or {}).get(it.get("id") or "")
        e["result"] = evaluate_optimization(it, failures, records, now, guardrail=g)
        if it.get("status") in ("landed", "verified", "rolled_back"):
            e["drift"] = check_anchor_drift(it)
        enriched.append(e)

    # 达标自动升级 (Harness 只推进核算闭环): 已落地优化收益达标 (improved/resolved)
    # 持续 ≥ OPT_AUTO_VERIFY_DAYS 天且样本充足 → 自动升级“已验证”并追加证据。
    # 升级仅向成功方向归档, 无风险; 升级后 status=verified 不再命中条件 (幂等)。
    try:
        import config as _cfg
        _vd = int(getattr(_cfg, "OPT_AUTO_VERIFY_DAYS", 3))
        for e in enriched:
            if e.get("status") != "landed" or not e.get("landed_at"):
                continue
            res = e.get("result") or {}
            _sc = res.get("sample_calls")
            if (res.get("verdict") in ("improved", "resolved")
                    and (now - float(e["landed_at"])) >= _vd * 86400
                    and (_sc is None or int(_sc) >= 5)):
                _obs_days = round((now - float(e["landed_at"])) / 86400, 1)
                proof = (f"[harness auto] 收益达标({res.get('verdict_label')})持续 {_obs_days} 天"
                         + (f"、样本 {_sc} 次" if _sc is not None else "")
                         + "，自动升级已验证")
                old = (e.get("verification") or "").strip()
                upserted = upsert_optimization(
                    {"id": e.get("id"), "status": "verified",
                     "verification": f"{old}\n{proof}".strip()},
                    actor="harness")
                e.update(upserted)
                e["status_label"] = STATUS_LABELS["verified"]
    except Exception:
        pass

    order = {s: i for i, s in enumerate(STATUS_ORDER)}
    enriched.sort(key=lambda x: (order.get(x.get("status", "planned"), 9),
                                 -(x.get("landed_at") or 0)))
    board = {s: sum(1 for e in enriched if e.get("status") == s) for s in STATUS_ORDER}
    effective = sum(1 for e in enriched if e["result"]["verdict"] in ("resolved", "improved"))

    # 自动建议回滚 (Harness 判案不代改代码): 护栏回归持续 ≥ N 天置顶提示,
    # 由 OPT_AUTO_ROLLBACK_SUGGEST 开关控制, 默认关闭 (人工确认后才归档)
    auto_suggest = []
    try:
        import config as _cfg
        if getattr(_cfg, "OPT_AUTO_ROLLBACK_SUGGEST", False):
            _days = int(getattr(_cfg, "OPT_ROLLBACK_SUGGEST_DAYS", 3))
            for e in enriched:
                la = float(e.get("landed_at") or 0)
                if (e["result"].get("verdict") == "regression" and la > 0
                        and (now - la) >= _days * 86400):
                    auto_suggest.append({
                        "id": e.get("id", ""), "title": e.get("title", ""),
                        "days": round((now - la) / 86400, 1),
                        "reason": f"护栏指标回归已持续 {round((now - la) / 86400, 1)} 天 (≥{_days} 天)",
                    })
    except Exception:
        auto_suggest = []

    return {
        "items": enriched,
        "board": board,
        "board_labels": STATUS_LABELS,
        "total": len(enriched),
        "effective": effective,
        "updated_at": now,
        "auto_suggest": auto_suggest,
    }


def upsert_optimization(payload: dict, actor: str = "") -> dict:
    """新建或更新一条优化台账记录 (状态推进 / 从错误组立项)。"""
    items = load_ledger()
    now = time.time()
    oid = (payload.get("id") or "").strip() or f"opt-{int(now)}"

    idx = next((i for i, x in enumerate(items) if x.get("id") == oid), -1)
    base = dict(items[idx]) if idx >= 0 else {
        "id": oid, "created_at": now, "created_by": actor,
        "actions": [], "metric": {"type": "manual"},
    }

    for key in ("title", "problem", "root_cause", "direction", "status",
                "owner", "verification", "landed_at", "category", "severity",
                "rollback_reason"):
        if key in payload and payload[key] not in (None, ""):
            base[key] = payload[key]
    if isinstance(payload.get("actions"), list):
        base["actions"] = payload["actions"]
    if isinstance(payload.get("metric"), dict):
        base["metric"] = payload["metric"]
    if isinstance(payload.get("files"), list):
        base["files"] = payload["files"]

    # 推进到已落地/已验证时自动补落地时间 + 代码锚定 (git commit + 文件指纹)
    if base.get("status") in ("landed", "verified") and not base.get("landed_at"):
        base["landed_at"] = now
        base["landed_at_basis"] = f"{actor or 'system'} 在控制台标记落地"
        base["landed_anchor"] = build_landed_anchor(base.get("files") or [])
    # 回滚归档: 只记台账 (时间/操作人/原因), 不碰任何代码
    if base.get("status") == "rolled_back":
        if not (base.get("rollback_reason") or "").strip():
            base["rollback_reason"] = "未填写原因"
        if not base.get("rolled_back_at"):
            base["rolled_back_at"] = now
        base["rolled_back_by"] = actor
    base["updated_at"] = now
    base["updated_by"] = actor

    if idx >= 0:
        items[idx] = base
    else:
        items.append(base)
    save_ledger(items)
    return base