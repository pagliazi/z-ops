"""
Z-OPS Memory Synthesizer — 后台记忆合成 + 显式记忆指令 (v4.0 Phase 3.2)

借鉴 ChatGPT Dreaming V3 (空闲期后台合成) + saved memories (用户可控记忆面板)。

三条路径:
1. 后台合成: 每日 02:00 扫描近 24h 会话 transcript (logs/sessions/*.jsonl),
   LLM 提炼「应长期记住的事实/偏好/故障模式」→ 编译进 wiki concepts
   (保持 ingest-time compilation 差异化: 写入时即编译为结构化页面, 查询零额外开销)
2. 显式指令: "记住: XX 客户的备份窗口是周日凌晨" → 直接落 wiki concepts 并回复确认
3. 记忆面板: /api/admin/memory 数据源 (wiki 五类页面计数 + 最近合成条目 + 删除)

审计: 合成/显式/删除操作全部写 wiki append-only .log; 页面 sources 标注来源;
LLM 调用复用 zops:llm_pref OneAPI 通道 (与 wiki_ingest.llm_summarize 同源)。
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import config

logger = logging.getLogger("zops.memory.synthesizer")

from core.memory.wiki_store import get_wiki_store, slugify, PAGE_TYPES

# ── 合成参数 (config 可覆盖: MEMORY_SYNTH_* 前缀) ──
SYNTH_HOUR = int(getattr(config, "MEMORY_SYNTH_HOUR", 2))      # 每日合成时刻
LOOKBACK_HOURS = int(getattr(config, "MEMORY_SYNTH_LOOKBACK_HOURS", 24))
MAX_SESSIONS = int(getattr(config, "MEMORY_SYNTH_MAX_SESSIONS", 60))
MAX_MSGS_PER_SESSION = 40        # 每会话注入 LLM 的最大对话条数 (防爆 prompt)
MSG_CHAR_LIMIT = 400             # 单条消息截断
MAX_EXTRACTIONS = 8              # 单次合成最多提炼条数
STATE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "memory_synthesis.json"

# 显式记忆指令: 行首「(请|帮我|麻烦你?)记住(这条|这个|一下)?[:：,，]? + 内容」
_EXPLICIT_RE = re.compile(
    r"^(?:请|帮我|麻烦你?)?\s*记住(?:这条|这个|一下)?\s*[:：,，]?\s*(?P<content>.{2,200})$",
    re.S,
)

# 提炼条目 kind → (page_id 前缀, wiki tags)
_KIND_PREFIX = {
    "fact": ("fact-", ["synth", "fact"]),
    "preference": ("pref-", ["synth", "preference"]),
    "failure_pattern": ("fp-", ["synth", "failure-pattern"]),
}


# ═══ 1. 显式记忆指令 (用户可控, 计划原文: 直接落 wiki 并回复确认) ═══

def capture_explicit_memory(text: str) -> str:
    """识别显式记忆指令, 返回记忆内容 (无匹配返回空串)

    匹配: "记住: XX 客户的备份窗口是周日凌晨" / "帮我记住一下，GID 12345 是数据库主机组"
    不匹配: "不要记住" / "忘记了密码" (不以记住指令开头)
    """
    if not text:
        return ""
    m = _EXPLICIT_RE.match(text.strip())
    if not m:
        return ""
    content = (m.group("content") or "").strip().strip("\"'“”")
    return content if len(content) >= 2 else ""


def save_explicit_memory(content: str, username: str = "user",
                         audience: str = "") -> Optional[str]:
    """显式记忆直接落 wiki concepts (幂等 page_id, 同内容只记一页)

    P0 受众/归属: 私聊 (audience 空) 记忆 visibility=private 仅本人检索可见;
    群聊记忆 visibility=group 仅该群受众可见 — 私有内容不再流入全局检索/
    群回复 (wiki_query.page_visible 检索前过滤)。
    """
    if not content:
        return None
    store = get_wiki_store()
    pid = f"memory-{slugify(content, max_len=40)}"
    source = f"explicit:{username}"
    now = datetime.now().isoformat()
    if audience:
        visibility, aud_fm = "group", audience
    else:
        visibility, aud_fm = "private", ""
    page = store.get_or_create(
        "concepts", pid,
        defaults_fm={"confidence": 0.98, "tags": ["explicit-memory"],
                     "sources": [source],
                     "owner": username, "visibility": visibility,
                     **({"audience": aud_fm} if aud_fm else {})},
        default_body=(f"# {content[:60]}\n\n## 定义\n用户显式要求长期记住的信息。\n\n"
                      f"## 内容\n{content}\n\n## 证据\n"),
    )
    # 既有页 (幂等命中): 归属/可见性只收紧不放宽 — 已是 private/group 则不改;
    # 无 visibility 的旧页升级为 private (P0: 存量显式记忆按私有处理)
    fm = page.frontmatter
    fm.setdefault("owner", username)
    cur_vis = str(fm.get("visibility") or "").strip().lower()
    if cur_vis not in ("private",) and not (cur_vis.startswith("group")):
        fm["visibility"] = visibility
        if aud_fm:
            fm["audience"] = aud_fm
    page.add_evidence(source, now, content[:300])
    store.add_source(page, source)
    store.save(page)
    store.append_log("explicit_memory", pid, source, content[:100])
    logger.info(f"[synthesizer] 显式记忆已落 wiki: {pid} (user={username}, "
                f"visibility={fm.get('visibility')})")
    return pid


# ═══ 2. 后台合成: 近 N 小时会话 → LLM 提炼 → wiki (Dreaming V3 式) ═══

def _collect_recent_dialogs(hours: int) -> List[str]:
    """扫描 logs/sessions/*.jsonl, 收集近 N 小时的对话文本块 (每会话一块)"""
    from core import transcript
    cutoff = time.time() - hours * 3600
    root: Path = transcript.TRANSCRIPT_DIR
    if not root.exists():
        return []
    chunks: List[str] = []
    for f in sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.stat().st_mtime < cutoff or len(chunks) >= MAX_SESSIONS:
            break
        try:
            events = transcript.load_transcript(f.stem)
        except Exception as e:
            logger.debug(f"[synthesizer] transcript 读取失败 {f.name}: {e}")
            continue
        lines = []
        for ev in events:
            if ev.get("type") != "message" or (ev.get("ts") or 0) < cutoff:
                continue
            role, content = ev.get("role", ""), ev.get("content") or ""
            if role not in ("user", "assistant") or not content.strip():
                continue
            lines.append(f"{role}: {content[:MSG_CHAR_LIMIT]}")
        if lines:
            chunks.append("\n".join(lines[-MAX_MSGS_PER_SESSION:]))
    return chunks


def _llm_extract_memories(dialog_text: str) -> List[Dict]:
    """LLM 提炼应长期记住的条目 (JSON list), 失败/未配置返回 [] (优雅降级)"""
    try:
        import httpx
        import redis as sync_redis
        r = sync_redis.Redis(host="localhost", port=6379, decode_responses=True)
        pref_raw = r.get("zops:llm_pref")
        if not pref_raw:
            return []
        pref = json.loads(pref_raw)
        api_key = pref.get("oneapi_key") or pref.get("api_key", "")
        base_url = pref.get("oneapi_base_url") or pref.get("base_url", "")
        model = pref.get("model", "")
        if not (api_key and base_url and model):
            return []
        if len(dialog_text) > 12000:
            dialog_text = dialog_text[:12000] + "\n...(截断)"
        system = (
            "你是 Z-OPS 运维记忆合成器。从以下会话对话中提炼「值得长期记住」的条目, "
            "严格输出 JSON 数组 (不要输出其他文字):\n"
            '[{"kind": "fact|preference|failure_pattern", "title": "短语标题(<=20字)", '
            '"detail": "具体内容(<=120字)"}]\n'
            "kind 判定: fact=客观事实(客户环境/备份窗口/资产归属等); "
            "preference=用户偏好(称呼/汇报格式/关注点); failure_pattern=故障模式(症状→根因→解法)。\n"
            "只提炼跨会话仍有价值的稳定信息, 忽略一次性操作与寒暄; 最多 "
            f"{MAX_EXTRACTIONS} 条; 无值得记住的内容输出 []。"
        )
        # P3: 统一模型调用层 (同步变体 — 限流/退避/熔断与主链路同一状态机)
        from core.model_gateway import get_model_gateway
        resp = get_model_gateway().request_chat_sync(
            {"provider": pref.get("provider", "oneapi"), "api_key": api_key,
             "base_url": base_url},
            {"model": model,
             "messages": [{"role": "system", "content": system},
                          {"role": "user", "content": dialog_text}],
             "temperature": 0.2, "max_tokens": 1500, "stream": False},
            timeout=90.0)
        resp.raise_for_status()
        msg = (resp.json().get("choices") or [{}])[0].get("message", {})
        text = msg.get("content") or msg.get("reasoning_content") or ""
        # 容错解析: 剥离 ```json 围栏 / 前后杂文字
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return []
        items = json.loads(m.group(0))
        out = []
        for it in items[:MAX_EXTRACTIONS]:
            if not isinstance(it, dict):
                continue
            kind = it.get("kind") if it.get("kind") in _KIND_PREFIX else "fact"
            title = str(it.get("title") or "").strip()[:40]
            detail = str(it.get("detail") or "").strip()[:300]
            if title and detail:
                out.append({"kind": kind, "title": title, "detail": detail})
        return out
    except Exception as e:
        logger.debug(f"[synthesizer] LLM 提炼跳过: {e}")
        return []


def _persist_extraction(item: Dict, source_tag: str) -> str:
    """单条提炼结果 → wiki concepts 页面 (幂等 page_id, 证据追加)"""
    store = get_wiki_store()
    prefix, tags = _KIND_PREFIX[item["kind"]]
    pid = f"{prefix}{slugify(item['title'], max_len=40)}"
    now = datetime.now().isoformat()
    page = store.get_or_create(
        "concepts", pid,
        defaults_fm={"confidence": 0.85, "tags": tags, "sources": [source_tag]},
        default_body=(f"# {item['title']}\n\n## 定义\n{item['detail']}\n\n"
                      "## 触发条件 / 识别信号\n待补充。\n\n## 处置方法\n待补充。\n\n## 证据\n"),
    )
    page.add_evidence(source_tag, now, item["detail"])
    store.add_source(page, source_tag)
    store.save(page)
    return pid


def _load_state() -> Dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: Dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception as e:
        logger.warning(f"[synthesizer] 状态写入失败: {e}")


def synthesize_recent_sessions(hours: int = LOOKBACK_HOURS) -> Dict:
    """后台合成入口: 扫描近 N 小时会话 → LLM 提炼 → wiki concepts

    幂等保护: 同一自然日不重复全量合成 (state.last_run_date 记账)。
    Returns: {sessions, messages, extracted, pages}
    """
    state = _load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_run_date") == today:
        logger.info("[synthesizer] 今日已完成合成, 跳过")
        return {**state.get("last_stats", {}), "skipped": True}

    chunks = _collect_recent_dialogs(hours)
    n_msgs = sum(c.count("\n") + 1 for c in chunks)
    pages: List[str] = []
    if chunks:
        dialog_text = "\n\n".join(chunks)
        source_tag = f"synthesizer:{today}"
        for item in _llm_extract_memories(dialog_text):
            try:
                pages.append(_persist_extraction(item, source_tag))
            except Exception as e:
                logger.warning(f"[synthesizer] 条目落盘失败: {e}")
    stats = {"date": today, "sessions": len(chunks), "messages": n_msgs,
             "extracted": len(pages), "pages": pages}
    _save_state({"last_run_date": today,
                 "last_run_at": datetime.now().isoformat(),
                 "last_stats": stats})
    logger.info(f"[synthesizer] 后台合成完成: {stats}")
    return stats


def _seconds_until_next_run() -> float:
    """现在 → 下一个 SYNTH_HOUR 点的秒数 (最小 60s 防紧密循环)"""
    now = datetime.now()
    target = now.replace(hour=SYNTH_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        import datetime as _dt
        target = target + _dt.timedelta(days=1)
    return max(60.0, (target - now).total_seconds())


async def run_daily_synthesis():
    """后台常驻: 每日 SYNTH_HOUR 点触发一次合成 (config.MEMORY_SYNTHESIS_ENABLED 可关)

    启动点: webchat_api.lifespan asyncio.create_task (与 log_archive 同模式)
    """
    logger.info(f"[synthesizer] 后台合成调度已启动 (每日 {SYNTH_HOUR:02d}:00)")
    while True:
        try:
            await asyncio.sleep(_seconds_until_next_run())
            if not getattr(config, "MEMORY_SYNTHESIS_ENABLED", True):
                logger.debug("[synthesizer] MEMORY_SYNTHESIS_ENABLED=false, 跳过")
                continue
            # 同步 IO + 同步 LLM 调用 → 线程池, 不阻塞事件循环
            stats = await asyncio.to_thread(synthesize_recent_sessions)
            if stats and not stats.get("skipped"):
                logger.info(f"[synthesizer] 每日合成: 提炼 {stats.get('extracted', 0)} 条")
        except asyncio.CancelledError:
            logger.info("[synthesizer] 后台合成调度已停止")
            raise
        except Exception as e:
            logger.warning(f"[synthesizer] 合成循环异常 (1h 后重试): {e}")
            await asyncio.sleep(3600)


# ═══ 3. 记忆面板数据源 (用户可控, 同 ChatGPT saved memories) ═══

def memory_panel_data(limit: int = 20) -> Dict:
    """wiki 五类页面计数 + 最近合成条目 + 最近显式记忆 (admin 面板用)"""
    store = get_wiki_store()
    counts = {t: 0 for t in PAGE_TYPES}
    synth_items, explicit_items = [], []
    for page in store.all_pages():
        counts[page.page_type] = counts.get(page.page_type, 0) + 1
        tags = page.frontmatter.get("tags") or []
        item = {
            "page_type": page.page_type,
            "page_id": page.page_id,
            "title": page.frontmatter.get("id", page.page_id),
            "updated": page.frontmatter.get("updated", ""),
            "tags": tags,
        }
        if "synth" in tags:
            synth_items.append(item)
        elif "explicit-memory" in tags:
            explicit_items.append(item)
    key = lambda it: it.get("updated") or ""
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "recent_synthesized": sorted(synth_items, key=key, reverse=True)[:limit],
        "recent_explicit": sorted(explicit_items, key=key, reverse=True)[:limit],
        "last_synthesis": _load_state().get("last_stats", {}),
    }


def delete_memory_page(page_type: str, page_id: str, operator: str = "admin") -> bool:
    """删除 wiki 记忆页面 (用户可控); 审计: append_log 永久记录 who/what

    对话历史与日志不受影响 (审计红线仅约束 transcripts/logs, wiki 记忆页
    属用户可控资产, 同 ChatGPT saved memories 的删除语义)。
    """
    store = get_wiki_store()
    page = store.get(page_type, page_id)
    if not page:
        return False
    title = page.frontmatter.get("id", page_id)
    try:
        page.path.unlink(missing_ok=True)
        # 向量 sidecar 一并清理 (Phase 3.1)
        (page.path.parent / (page.path.stem + ".vec.json")).unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"[synthesizer] 页面文件删除失败 {page_id}: {e}")
        return False
    store.append_log("memory_delete", page_id, f"admin:{operator}",
                     f"type={page_type} title={title}")
    logger.info(f"[synthesizer] 记忆页面已删除: {page_type}/{page_id} (by {operator})")
    return True

