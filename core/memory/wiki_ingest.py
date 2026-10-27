"""
Z-OPS Wiki Ingest — 把真实的 Z-OPS 记忆数据编译到 wiki/ 下的 Markdown 页

数据源（真实存在，已盘点）：
- data/cross_session_memory.json  —— sessions + users
- data/user_profiles.json         —— Honcho 用户画像 + dialectic_notes
- data/skill_improvements.json    —— skill 失败反馈
- experiences/*.jsonl             —— skill 执行轨迹 + lessons_learned

设计原则（对照 Karpathy LLM-Wiki）：
- Ingest 时 "编译"，不是查询时解释
- 同一实体合并不覆盖，证据幂等追加
- 无 LLM 也能跑（规则式）；留出 llm_summarize() 钩子供未来注入
- 频次 ≥ 2 的 topic / key_learning 升级为 concept 页
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable

from .wiki_store import WikiStore, WikiPage, slugify, get_wiki_store

logger = logging.getLogger("zops.memory.wiki_ingest")

# 固定数据源路径
ZOPS_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ZOPS_ROOT / "data"
EXPERIENCES_DIR = ZOPS_ROOT / "experiences"

# 资产名识别：vm-xxx / host-xxx / esxi-xxx / 10.x.x.x
ASSET_PATTERNS = [
    re.compile(r"\b(vm-[a-z0-9_\-]+)\b", re.I),
    re.compile(r"\b(host-[a-z0-9_\-]+)\b", re.I),
    re.compile(r"\b(esxi-[a-z0-9_\-]+)\b", re.I),
    re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"),
]

# ---- LLM 钩子（可选注入） ----
LLMSummarizer = Optional[Callable[[str], str]]

# 全局 LLM summarizer（可通过 set_llm_summarizer 注入，或自动使用 OneAPI）
_global_llm_summarizer: Optional[Callable] = None


def set_llm_summarizer(fn: Callable) -> None:
    """外部注入 LLM summarizer 函数（同步/异步均可，接受 str 返回 str）"""
    global _global_llm_summarizer
    _global_llm_summarizer = fn


def llm_summarize(evidence_text: str, task: str = "synthesize") -> str:
    """
    LLM 编译钩子：用 LLM 将原始证据编译为结构化知识。

    设计理念（Karpathy LLM-Wiki）：
    - Ingest 时编译：用 LLM 提炼结构化知识，而非查询时解释
    - 失败时优雅降级：LLM 不可用时返回空字符串，上层回退到规则式
    - 支持注入：通过 set_llm_summarizer() 外部注入实现

    Args:
        evidence_text: 原始证据文本（多条证据拼接）
        task: 编译任务类型 ("synthesize" | "summarize" | "define")

    Returns:
        LLM 生成的结构化文本，失败时返回空字符串
    """
    global _global_llm_summarizer

    # 1. 优先使用外部注入的 summarizer
    if _global_llm_summarizer:
        try:
            result = _global_llm_summarizer(evidence_text)
            if result:
                return result
        except Exception as e:
            logger.warning(f"[llm_summarize] 注入 summarizer 失败: {e}")

    # 2. 自动模式：使用 OneAPI 网关
    try:
        import httpx
        import redis as sync_redis

        # 从 Redis 读取 LLM 配置（同步方式）
        r = sync_redis.Redis(host='localhost', port=6379, decode_responses=True)
        pref_raw = r.get("zops:llm_pref")
        if not pref_raw:
            logger.debug("[llm_summarize] zops:llm_pref 未配置，跳过 LLM 编译")
            return ""

        pref = json.loads(pref_raw)
        api_key = pref.get("oneapi_key") or pref.get("api_key", "")
        base_url = pref.get("oneapi_base_url") or pref.get("base_url", "")
        model = pref.get("model", "")

        if not api_key or not base_url or not model:
            logger.debug("[llm_summarize] LLM 配置不完整，跳过")
            return ""

        # 构造 prompt
        system_prompts = {
            "synthesize": (
                "你是 Z-OPS 运维知识编译器。请将以下多条原始证据编译为一段结构化的综合分析。"
                "输出格式：\n## 发现\n- 要点列表\n## 结论\n- 1-2 句总结\n## 建议\n- 行动建议列表"
            ),
            "summarize": (
                "你是 Z-OPS 运维知识编译器。请将以下原始会话数据编译为一段简洁的摘要（2-3 句话）。"
                "只输出摘要文本，不要加标题。"
            ),
            "define": (
                "你是 Z-OPS 运维知识编译器。请根据以下证据为这个运维概念写一段定义（2-3 句话），"
                "说明它是什么、为什么重要。只输出定义文本。"
            ),
        }
        system = system_prompts.get(task, system_prompts["summarize"])

        # 截断过长证据
        max_evidence = 2000
        if len(evidence_text) > max_evidence:
            evidence_text = evidence_text[:max_evidence] + "\n... (截断)"

        # 同步调用 OneAPI
        response = httpx.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": evidence_text},
                ],
                "temperature": 0.3,
                "max_tokens": 500,
                "stream": False,  # 强制非流式
            },
            timeout=60.0,  # 网关响应较慢
        )
        response.raise_for_status()
        result = response.json()
        choice = result.get("choices", [{}])[0] if result.get("choices") else {}
        msg = choice.get("message", {})
        # 兼容多种响应格式：content / reasoning_content / text
        text = msg.get("content") or msg.get("reasoning_content") or msg.get("text") or ""
        if text and isinstance(text, str):
            logger.info(f"[llm_summarize] LLM 编译成功 ({task}, {len(text)} chars)")
            return text.strip()

    except Exception as e:
        logger.debug(f"[llm_summarize] LLM 编译跳过 ({task}): {e}")

    return ""


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"read {path} failed: {e}")
        return {}


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _extract_assets(text: str) -> List[str]:
    found = []
    for pat in ASSET_PATTERNS:
        found.extend(pat.findall(text or ""))
    # 去重保序
    seen = set()
    result = []
    for x in found:
        xl = x.lower()
        if xl not in seen:
            seen.add(xl)
            result.append(xl)
    return result


# ==================== Ingestors ====================

def ingest_sessions(store: WikiStore) -> Dict[str, int]:
    """把 cross_session_memory.json 的 sessions/users 编译到 wiki。"""
    src_file = DATA_DIR / "cross_session_memory.json"
    data = _read_json(src_file)
    source_tag = "data/cross_session_memory.json"

    stats = {"summaries": 0, "users": 0, "agents": 0, "assets": 0}
    topic_counter: Counter = Counter()
    learning_counter: Counter = Counter()
    # 记录每个 topic/learning 的证据
    topic_evidence: Dict[str, List[tuple]] = defaultdict(list)
    learning_evidence: Dict[str, List[tuple]] = defaultdict(list)

    # --- sessions → summaries ---
    for sid, sess in data.get("sessions", {}).items():
        created = sess.get("created_at", "")
        date = created[:10] if created else datetime.now().date().isoformat()
        page_id = f"{date}-{sid}"
        page = store.get_or_create(
            "summaries",
            page_id,
            defaults_fm={
                "session_id": sid,
                "user_id": sess.get("user_id", ""),
                "agent_id": sess.get("agent_id", ""),
                "sentiment": sess.get("sentiment", "neutral"),
                "complexity": sess.get("complexity", "simple"),
                "follow_up_needed": sess.get("follow_up_needed", False),
                "sources": [source_tag],
                # P2 ACL: 会话摘要属用户私有 — 无此二字段的旧语义是 public, 造成跨用户泄漏
                "owner": sess.get("user_id", ""),
                "visibility": "private",
            },
        )
        # 兜底: 既有页 (含旧 public 页) 收紧为 private
        fm = page.frontmatter
        if str(fm.get("visibility") or "") != "private":
            fm["visibility"] = "private"
            fm.setdefault("owner", sess.get("user_id", ""))
        summary = sess.get("summary", "").strip()
        title = summary.split("。")[0][:60] if summary else sid
        # 重置 body（summary 是 1:1 原始源，可以整页重写）
        user_id = sess.get("user_id", "unknown")
        agent_id = sess.get("agent_id", "unknown")
        assets = _extract_assets(summary)
        lines = [
            f"# {title}",
            "",
            "## 用户",
            f"- [[user-{user_id}]]",
            "",
            "## Agent",
            f"- [[agent-{agent_id}]]",
            "",
        ]
        if assets:
            lines += ["## 涉及实体"] + [f"- [[asset-{a}]]" for a in assets] + [""]
        lines += ["## 摘要", summary or "_empty_", ""]
        if sess.get("key_learnings"):
            lines += ["## 关键学习"] + [f"- {l}" for l in sess["key_learnings"]] + [""]
        if sess.get("topics"):
            lines += ["## 话题"] + [f"- {t}" for t in sess["topics"]] + [""]
        lines += ["## Evidence", f"- `{source_tag}` @ `{created}` — session `{sid}`"]
        page.body = "\n".join(lines)
        store.add_source(page, source_tag)
        store.save(page)
        store.append_log("ingest", page.page_id, source_tag, f"session {sid}")
        stats["summaries"] += 1

        # 累计证据
        for t in sess.get("topics", []) or []:
            topic_counter[t] += 1
            topic_evidence[t].append((source_tag, created, f"session `{sid}`"))
        for l in sess.get("key_learnings", []) or []:
            learning_counter[l] += 1
            learning_evidence[l].append((source_tag, created, f"session `{sid}`"))

        # 创建/更新 asset entity
        for asset in assets:
            aid = f"asset-{asset}"
            ap = store.get_or_create(
                "entities", aid,
                defaults_fm={"subtype": "asset", "sources": [source_tag]},
                default_body=f"# {asset}\n\n## 概要\n运维资产 `{asset}`（由 session 摘要自动抽取）。\n\n## 关联\n- [[user-{user_id}]]\n- [[agent-{agent_id}]]\n",
            )
            ap.add_evidence(source_tag, created, f"session `{sid}` 涉及")
            store.add_source(ap, source_tag)
            store.save(ap)
            stats["assets"] += 1

        # agent entity
        aid = f"agent-{agent_id}"
        agent_page = store.get_or_create(
            "entities", aid,
            defaults_fm={"subtype": "agent", "sources": [source_tag]},
            default_body=f"# {agent_id}\n\n## 概要\nZ-OPS 智能体 `{agent_id}`。\n\n## 关联\n",
        )
        agent_page.add_bullet("## 关联", f"[[user-{user_id}]]")
        agent_page.add_evidence(source_tag, created, f"session `{sid}` 使用")
        store.add_source(agent_page, source_tag)
        store.save(agent_page)
        stats["agents"] += 1

    # --- users → entities ---
    for uid, u in data.get("users", {}).items():
        page_id = f"user-{uid}"
        page = store.get_or_create(
            "entities", page_id,
            defaults_fm={
                "subtype": "user",
                "sources": [source_tag],
            },
            default_body=f"# {u.get('name') or uid}\n\n## 概要\nZ-OPS 系统用户 `{uid}`。\n",
        )
        # 重建属性表
        attrs = {
            "user_id": uid,
            "role": u.get("role", ""),
            "communication_style": u.get("communication_style", ""),
            "session_count": u.get("session_count", 0),
            "last_active": u.get("last_active", ""),
        }
        if "## 关键属性" not in page.body:
            page.body = page.body.rstrip() + "\n\n## 关键属性\n" + \
                "| 字段 | 值 |\n|---|---|\n" + \
                "".join(f"| {k} | {v} |\n" for k, v in attrs.items() if v != "")
        # 常用任务 & 推荐 agent → 关联
        for topic in u.get("common_tasks", []) or []:
            page.add_bullet("## 常用任务", topic)
        for ag in u.get("preferred_agents", []) or []:
            page.add_bullet("## 关联", f"[[agent-{ag}]]")
        for note in u.get("notes", []) or []:
            # Honcho 笔记作为证据
            page.add_evidence(source_tag, u.get("last_active", ""), f"note: {note[:200]}")
        store.add_source(page, source_tag)
        store.save(page)
        store.append_log("ingest", page.page_id, source_tag, f"user {uid}")
        stats["users"] += 1

    # --- topics / key_learnings → concepts（频次≥2）---
    for topic, n in topic_counter.items():
        if n < 2:
            continue
        cid = f"topic-{slugify(topic)}"
        # LLM 编译定义仅在新页面创建时进行 — 页面已存在时编译结果会被
        # get_or_create 丢弃, 白耗一次 LLM 调用 (2026-10-22 前每次会话结束
        # 全量重编译所有话题, 全天空烧数百次调用)
        if store.get("concepts", cid) is None:
            evidence_lines = [f"- {det}" for _, _, det in topic_evidence[topic]]
            evidence_text = f"话题: {topic}\n出现频次: {n}\n证据:\n" + "\n".join(evidence_lines[:10])
            llm_def = llm_summarize(evidence_text, task="define")
            default_body = (
                f"# {topic}\n\n## 定义\n"
                f"{llm_def if llm_def else f'由多条会话 topic 聚合而成的运维关注点：**{topic}**。'}"
                f"\n\n## 证据\n" + "\n".join(evidence_lines[:5]) + "\n"
            )
            page = store.get_or_create(
                "concepts", cid,
                defaults_fm={"frequency": n, "confidence": min(0.5 + 0.1 * n, 0.95), "sources": [source_tag], "tags": ["session"]},
                default_body=default_body,
            )
        else:
            page = store.get("concepts", cid)
        page.frontmatter["frequency"] = max(page.frontmatter.get("frequency", 0), n)
        for src, ts, det in topic_evidence[topic]:
            page.add_evidence(src, ts, det)
        store.add_source(page, source_tag)
        store.save(page)

    for learning, n in learning_counter.items():
        if n < 2:
            continue
        cid = f"learning-{slugify(learning)}"
        page = store.get_or_create(
            "concepts", cid,
            defaults_fm={"frequency": n, "confidence": min(0.5 + 0.1 * n, 0.95), "sources": [source_tag], "tags": ["session", "learning"]},
            default_body=f"# {learning}\n\n## 定义\n来自多次会话 key_learnings 的经验结论。\n\n## 触发条件 / 识别信号\n待补充。\n\n## 处置方法\n{learning}\n",
        )
        page.frontmatter["frequency"] = max(page.frontmatter.get("frequency", 0), n)
        for src, ts, det in learning_evidence[learning]:
            page.add_evidence(src, ts, det)
        store.add_source(page, source_tag)
        store.save(page)

    return stats


def ingest_user_profiles(store: WikiStore) -> Dict[str, int]:
    """合并 user_profiles.json 到 user entity 页（添加 dialectic_notes 证据）。"""
    src_file = DATA_DIR / "user_profiles.json"
    data = _read_json(src_file)
    source_tag = "data/user_profiles.json"
    stats = {"users": 0, "notes": 0}

    for uid, p in data.get("profiles", {}).items():
        page_id = f"user-{uid}"
        page = store.get_or_create(
            "entities", page_id,
            defaults_fm={"subtype": "user", "sources": [source_tag],
                         # P2 ACL: 用户画像页私有 (SPEC: 画像不转授权/不外泄)
                         "owner": uid, "visibility": "private"},
            default_body=f"# {p.get('name') or uid}\n\n## 概要\nZ-OPS 系统用户 `{uid}`。\n",
        )
        fm_u = page.frontmatter
        if str(fm_u.get("visibility") or "") != "private":
            fm_u["visibility"] = "private"
            fm_u.setdefault("owner", uid)
        # role / expertise
        if p.get("role"):
            page.add_bullet("## 关键属性", f"role: {p['role']}")
        exp = p.get("expertise", {}) or {}
        for k in ("primary_domain", "skill_level"):
            if exp.get(k):
                page.add_bullet("## 关键属性", f"{k}: {exp[k]}")
        for vp in exp.get("vm_platforms", []) or []:
            page.add_bullet("## 关键属性", f"vm_platform: {vp}")
        # recommended agents
        for ag in p.get("recommended_agents", []) or []:
            page.add_bullet("## 关联", f"[[agent-{ag}]]")
        # dialectic notes → Evidence
        for note in p.get("dialectic_notes", []) or []:
            ts = note.get("timestamp", "")
            det = (
                f"observation: {note.get('observation','')}; "
                f"interpretation: {note.get('interpretation','')}; "
                f"outcome: {note.get('outcome','')} (conf={note.get('confidence','')})"
            )
            page.add_evidence(source_tag, ts, det)
            stats["notes"] += 1
        # metrics
        page.add_bullet("## 关键属性", f"total_interactions: {p.get('total_interactions', 0)}")
        page.add_bullet("## 关键属性", f"successful_interactions: {p.get('successful_interactions', 0)}")
        store.add_source(page, source_tag)
        store.save(page)
        store.append_log("ingest", page.page_id, source_tag, "merge profile")
        stats["users"] += 1

    return stats


def ingest_skill_feedbacks(store: WikiStore) -> Dict[str, int]:
    """skill_improvements.json → skill entity + failure synthesis。"""
    src_file = DATA_DIR / "skill_improvements.json"
    data = _read_json(src_file)
    source_tag = "data/skill_improvements.json"
    stats = {"skills": 0, "synthesis": 0}

    for skill_name, fbs in data.get("feedbacks", {}).items():
        if not fbs:
            continue
        page_id = f"skill-{skill_name}"
        page = store.get_or_create(
            "entities", page_id,
            defaults_fm={"subtype": "skill", "sources": [source_tag], "tags": ["skill"]},
            default_body=f"# {skill_name}\n\n## 概要\nZ-OPS 技能 `{skill_name}`。\n",
        )
        # 反馈证据（只取最新 5 条避免页面爆炸）
        latest = sorted(fbs, key=lambda x: x.get("timestamp", ""), reverse=True)[:5]
        for fb in latest:
            ts = fb.get("timestamp", "")
            det = (
                f"{'success' if fb.get('success') else 'failure'} "
                f"err_type={fb.get('error_type','')} msg={(fb.get('error_message') or '')[:80]}"
            )
            page.add_evidence(source_tag, ts, det)
        # 统计
        total = len(fbs)
        failed = sum(1 for x in fbs if not x.get("success"))
        page.add_bullet("## 关键属性", f"total_feedback: {total}")
        page.add_bullet("## 关键属性", f"failed: {failed}")
        page.add_bullet("## 关键属性", f"success_rate: {(total-failed)/total:.2%}" if total else "success_rate: n/a")
        store.add_source(page, source_tag)
        store.save(page)
        stats["skills"] += 1

        # --- synthesis：如果同类失败 ≥ 3 条，生成失败模式综合页 ---
        err_counter = Counter(x.get("error_type", "unknown") for x in fbs if not x.get("success"))
        for err_type, n in err_counter.items():
            if n < 3 or not err_type:
                continue
            syn_id = f"{skill_name}-{err_type}-pattern"

            # 已有综合页 → 不再重复 LLM 编译 (分析保留首次版本; 失败计数在页面属性中滚动更新)
            if store.get("synthesis", syn_id) is not None:
                continue

            # 收集证据用于 LLM 编译
            msgs = Counter((x.get("error_message") or "")[:80] for x in fbs if x.get("error_type") == err_type)
            top_msg, top_n = msgs.most_common(1)[0] if msgs else ("", 0)
            evidence_text = (
                f"技能: {skill_name}\n错误类型: {err_type}\n失败次数: {n}\n"
                f"主要错误消息: {top_msg} ({top_n}次)\n"
            )

            # 尝试 LLM 编译综合分析
            llm_analysis = llm_summarize(evidence_text, task="synthesize")

            if llm_analysis:
                body_content = "\n".join([
                    f"# {skill_name} 的 {err_type} 失败模式",
                    "",
                    "## LLM 编译分析",
                    llm_analysis,
                    "",
                    "## 数据支撑",
                    "| 维度 | 值 |",
                    "|---|---|",
                    f"| 失败次数 | {n} |",
                    f"| 失败类型 | {err_type} |",
                    f"| Top 错误消息 | {top_msg} |",
                    "",
                    "## 源页面",
                    f"- [[skill-{skill_name}]]",
                    "",
                    "## Evidence",
                    f"- `{source_tag}` @ `{datetime.now().isoformat()}` — 综合 {n} 条反馈",
                ])
            else:
                # 规则式后备
                body_content = "\n".join([
                    f"# {skill_name} 的 {err_type} 失败模式",
                    "",
                    "## 发现",
                    f"- 技能 [[skill-{skill_name}]] 共记录 {n} 次 `{err_type}` 类失败",
                    f"- 主导错误消息：`{top_msg}`（{top_n} 次）",
                    "",
                    "## 数据支撑",
                    "| 维度 | 值 |",
                    "|---|---|",
                    f"| 失败次数 | {n} |",
                    f"| 失败类型 | {err_type} |",
                    f"| Top 错误消息 | {top_msg} |",
                    "",
                    "## 结论与建议",
                    f"- 该技能对 `{err_type}` 敏感，建议增加重试 / 超时调整 / 前置连通性检查",
                    "",
                    "## 源页面",
                    f"- [[skill-{skill_name}]]",
                    "",
                    "## Evidence",
                    f"- `{source_tag}` @ `{datetime.now().isoformat()}` — 综合 {n} 条反馈",
                ])
            # 创建 synthesis 页面
            syn = store.get_or_create(
                "synthesis", syn_id,
                defaults_fm={
                    "window": "all_time",
                    "derived_from": [f"entities/skill-{skill_name}", source_tag],
                    "sources": [source_tag],
                    "tags": ["synthesis", "failure-pattern"],
                },
            )
            syn.body = body_content
            store.add_source(syn, source_tag)
            store.save(syn)
            store.append_log("synthesize", syn_id, source_tag, f"{n} x {err_type}")
            stats["synthesis"] += 1

    return stats


def ingest_skill_experiences(store: WikiStore) -> Dict[str, int]:
    """experiences/*.jsonl → 合并到 skill entity 页。"""
    stats = {"skills": 0, "lessons": 0}
    if not EXPERIENCES_DIR.exists():
        return stats

    for jsonl in sorted(EXPERIENCES_DIR.glob("*.jsonl")):
        rows = _read_jsonl(jsonl)
        if not rows:
            continue
        source_tag = f"experiences/{jsonl.name}"
        # 按 skill 聚合
        by_skill: Dict[str, List[dict]] = defaultdict(list)
        for r in rows:
            by_skill[r.get("skill_name", "unknown")].append(r)

        for skill_name, items in by_skill.items():
            page_id = f"skill-{skill_name}"
            page = store.get_or_create(
                "entities", page_id,
                defaults_fm={"subtype": "skill", "sources": [source_tag], "tags": ["skill"]},
                default_body=f"# {skill_name}\n\n## 概要\nZ-OPS 技能 `{skill_name}`（来自 experiences）。\n",
            )
            # 合并 lessons_learned 到 "## 经验累积"
            lesson_counter: Counter = Counter()
            for it in items:
                for l in it.get("lessons_learned", []) or []:
                    lesson_counter[l] += 1
            for lesson, n in lesson_counter.most_common(10):
                page.add_bullet("## 经验累积", f"{lesson}  _(x{n})_")
                stats["lessons"] += 1
            # 策略统计
            strat_counter = Counter(it.get("strategy_used", "") for it in items if it.get("strategy_used"))
            for strat, n in strat_counter.most_common():
                page.add_bullet("## 策略分布", f"`{strat}`: {n}")
            # 最近 3 条证据
            for it in sorted(items, key=lambda x: x.get("timestamp", 0), reverse=True)[:3]:
                ts = datetime.fromtimestamp(it.get("timestamp", 0)).isoformat() if it.get("timestamp") else ""
                det = f"action={it.get('action','')} success={it.get('success')} strategy={it.get('strategy_used','')} rc={it.get('root_cause','')}"
                page.add_evidence(source_tag, ts, det)
            store.add_source(page, source_tag)
            store.save(page)
            store.append_log("ingest", page.page_id, source_tag, f"{len(items)} trajectories")
            stats["skills"] += 1

    return stats


def ingest_optimizations(store: WikiStore) -> Dict[str, int]:
    """optimizations.json → 更正/优化学习 concept 页面。

    wiki 的核心内容是 Z-OPS 内部查询内容的更正与优化学习记录，
    optimizations.json 正是「问题→根因→方向→动作→验证」的结构化闭环。
    """
    src_file = DATA_DIR / "optimizations.json"
    data = _read_json(src_file)
    source_tag = "data/optimizations.json"
    stats = {"optimizations": 0}

    for rec in data.get("optimizations", []):
        opt_id = rec.get("id") or ""
        title = rec.get("title") or opt_id
        if not opt_id:
            continue
        tags = ["optimization", "learning"]
        if rec.get("category"):
            tags.append(rec["category"])
        page = store.get_or_create(
            "concepts", opt_id,
            defaults_fm={"subtype": "optimization", "sources": [source_tag], "tags": tags},
            default_body=f"# {title}\n",
        )
        body = [f"# {title}", ""]
        if rec.get("problem"):
            body += ["## 问题现象", rec["problem"], ""]
        if rec.get("root_cause"):
            body += ["## 根因分析", rec["root_cause"], ""]
        if rec.get("direction"):
            body += ["## 优化方向", rec["direction"], ""]
        actions = rec.get("actions") or []
        if actions:
            body += ["## 落实动作"] + [f"- {a}" for a in actions] + [""]
        verify_lines = []
        if rec.get("status"):
            verify_lines.append(f"- status: {rec['status']}")
        if rec.get("verification"):
            verify_lines.append(f"- verification: {rec['verification']}")
        if rec.get("metric"):
            verify_lines.append(f"- metric: {rec['metric']}")
        if verify_lines:
            body += ["## 验证与效果"] + verify_lines + [""]
        body += [
            "## 关键属性",
            f"- category: {rec.get('category', '')}",
            f"- severity: {rec.get('severity', '')}",
            f"- owner: {rec.get('owner', '')}",
        ]
        if rec.get("landed_at"):
            body.append(f"- landed_at: {rec['landed_at']}")
        body += [
            "",
            "## Evidence",
            f"- `{source_tag}` @ `{datetime.now().isoformat()}` — 优化记录 {opt_id}",
        ]
        page.body = "\n".join(body)
        store.add_source(page, source_tag)
        store.save(page)
        store.append_log("ingest", opt_id, source_tag, title[:60])
        stats["optimizations"] += 1
    return stats


# ==================== Orchestrator ====================

def run_full_ingest() -> Dict[str, Any]:
    store = get_wiki_store()
    result = {}
    result["sessions"] = ingest_sessions(store)
    result["profiles"] = ingest_user_profiles(store)
    result["skill_feedbacks"] = ingest_skill_feedbacks(store)
    result["skill_experiences"] = ingest_skill_experiences(store)
    result["optimizations"] = ingest_optimizations(store)
    store.write_index()
    store.append_log("full-ingest", "index.md", "", json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import pprint
    pprint.pprint(run_full_ingest())
