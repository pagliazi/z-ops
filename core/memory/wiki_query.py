"""
Z-OPS Wiki Query — 分层检索（先 wiki 后 FTS5 后 trajectories）

实现轻量 BM25：
- 标题加权 3x
- 标签加权 2x
- Evidence 行降权 0.5x
无外部依赖，纯 Python。
"""
from __future__ import annotations

import math
import re
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

from .wiki_store import WikiStore, WikiPage, get_wiki_store

logger = logging.getLogger("zops.memory.wiki_query")

_TOK_RE = re.compile(r"[\w\-_]+", re.UNICODE)

# BM25 参数
K1 = 1.5
B = 0.75
TITLE_BOOST = 3.0
TAG_BOOST = 2.0
EVIDENCE_WEIGHT = 0.5


def tokenize(text: str) -> List[str]:
    # 中文按字切（简化）；英文按 \w+
    tokens = []
    for m in _TOK_RE.finditer(text.lower()):
        w = m.group(0)
        if all(ord(c) < 128 for c in w):
            tokens.append(w)
        else:
            # 中文：单字 + 二字窗口
            for i, c in enumerate(w):
                tokens.append(c)
                if i + 1 < len(w):
                    tokens.append(w[i:i+2])
    return tokens


@dataclass
class QueryHit:
    page_id: str
    page_type: str
    score: float
    title: str
    snippet: str
    sources: List[str] = field(default_factory=list)
    origin: str = ""   # P2: internal(缺省)/reference — 外部参考检索时显式标注

    def to_dict(self) -> dict:
        return {
            "page_id": self.page_id,
            "page_type": self.page_type,
            "score": round(self.score, 3),
            "title": self.title,
            "snippet": self.snippet,
            "sources": self.sources,
        }


def _extract_title_and_tags(page: WikiPage) -> Tuple[str, List[str]]:
    title = ""
    for line in page.body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    tags = page.frontmatter.get("tags") or []
    return title, list(tags)


def _score_page(
    page: WikiPage,
    query_tokens: List[str],
    idf: Dict[str, float],
    avgdl: float,
) -> Tuple[float, str]:
    body = page.body
    title, tags = _extract_title_and_tags(page)

    # 分段 tokens
    body_lines = body.splitlines()
    evidence_lines = [l for l in body_lines if l.startswith("- `")]
    nonevid_body = "\n".join(l for l in body_lines if l not in evidence_lines)

    body_tokens = tokenize(nonevid_body)
    evid_tokens = tokenize(" ".join(evidence_lines))
    title_tokens = tokenize(title)
    tag_tokens = tokenize(" ".join(tags))

    tf = Counter(body_tokens)
    tf_title = Counter(title_tokens)
    tf_tag = Counter(tag_tokens)
    tf_evid = Counter(evid_tokens)

    dl = len(body_tokens) + len(evid_tokens) * EVIDENCE_WEIGHT
    if avgdl <= 0:
        avgdl = 1.0

    score = 0.0
    best_line = ""
    for q in set(query_tokens):
        if q not in idf:
            continue
        freq = tf.get(q, 0) + tf_evid.get(q, 0) * EVIDENCE_WEIGHT
        norm = (freq * (K1 + 1)) / (freq + K1 * (1 - B + B * dl / avgdl)) if freq else 0
        s = idf[q] * norm
        if q in tf_title:
            s += idf[q] * TITLE_BOOST
        if q in tf_tag:
            s += idf[q] * TAG_BOOST
        score += s

    # 取包含最多命中词的一行做 snippet
    if score > 0:
        q_set = set(query_tokens)
        best_count = 0
        for line in body_lines:
            if not line.strip() or line.startswith("#"):
                continue
            lt = tokenize(line)
            c = sum(1 for t in lt if t in q_set)
            if c > best_count:
                best_count = c
                best_line = line.strip()
    return score, best_line or (title[:80] if title else "")


def _page_origin(page) -> str:
    """P2: 页面来源标记 — 外部参考 (Hermes/IDE 记忆等) 不冒充内部验证经验"""
    fm = getattr(page, "frontmatter", None) or {}
    if str(fm.get("origin") or "").lower() == "reference":
        return "reference"
    if "prior_learning_reference" in (fm.get("tags") or []):
        return "reference"
    return "internal"


def page_visible(page, actor: str = "", audience: str = "") -> bool:
    """P0 受众/归属过滤 (检索前判定; SPEC: 群输出不得包含发言者私有记忆)

    visibility 语义 (frontmatter, 缺省 = public — 存量内部经验页不受影响):
      public        所有人可见 (Z-OPS 内部纠错经验默认)
      group:<aud>   仅该受众群可见 (audience 形如 feishu:group:{chat_id})
      private       仅 owner 可见 (显式记忆/私聊内容)
    匹配规则:
      - private: actor == owner 才可见
      - group: audience 精确等于页面 audience 才可见
      - 未知 visibility 值 → 不可见 (fail-closed, 未知不解释为公开)
    """
    fm = getattr(page, "frontmatter", None) or {}
    vis = str(fm.get("visibility") or "public").strip().lower()
    if vis in ("", "public"):
        return True
    if vis == "private":
        owner = str(fm.get("owner") or "").strip()
        return bool(actor) and actor == owner
    if vis.startswith("group"):
        page_aud = str(fm.get("audience") or "").strip()
        return bool(audience) and audience == page_aud
    return False  # 未知 visibility → 拒绝


def search_wiki(
    query: str,
    store: WikiStore = None,
    top_k: int = 10,
    page_types: Optional[List[str]] = None,
    actor: str = "",
    audience: str = "",
) -> List[QueryHit]:
    store = store or get_wiki_store()
    pages = list(store.all_pages())
    if page_types:
        pages = [p for p in pages if p.page_type in page_types]
    # P0: 检索前过滤 — 私有页/他群页不进入打分 (返回内容也必在过滤后集合内)
    pages = [p for p in pages if page_visible(p, actor=actor, audience=audience)]

    query_tokens = tokenize(query)
    if not query_tokens or not pages:
        return []

    # 计算 IDF
    N = len(pages)
    doc_freq: Counter = Counter()
    tokens_per_page = []
    for p in pages:
        toks = set(tokenize(p.body) + tokenize(" ".join(p.frontmatter.get("tags") or [])))
        tokens_per_page.append(toks)
        for t in toks:
            doc_freq[t] += 1

    idf = {
        t: math.log((N - df + 0.5) / (df + 0.5) + 1.0)
        for t, df in doc_freq.items()
    }
    avgdl = sum(len(tokenize(p.body)) for p in pages) / max(N, 1)

    hits: List[QueryHit] = []
    for page in pages:
        score, snippet = _score_page(page, query_tokens, idf, avgdl)
        if score <= 0:
            continue
        title, _ = _extract_title_and_tags(page)
        hits.append(QueryHit(
            page_id=page.page_id,
            page_type=page.page_type,
            score=score,
            title=title,
            snippet=snippet[:200],
            sources=list(page.frontmatter.get("sources") or []),
            origin=_page_origin(page),
        ))

    hits.sort(key=lambda h: h.score, reverse=True)

    # v4.0 (Phase 3.1): 混合检索 — BM25 top-K ∪ 向量 top-K → RRF 融合
    # 向量召回可命中同义不同词 (BM25 必 miss 的场景); embedding 不可用/无 sidecar
    # 时自动降级纯 BM25 (上方加权逻辑不变)
    try:
        import config as _cfg
        from . import wiki_vector
        bm25_ids = [h.page_id for h in hits[:_cfg.HYBRID_RECALL_TOPK]]
        vec_res = wiki_vector.vector_search(
            query, store.root, _cfg.HYBRID_RECALL_TOPK, page_types)
        if vec_res:  # None=服务不可用 → 纯 BM25; [] → 无 sidecar/无命中
            vec_ids = [pid for _, pid, _ in vec_res]
            fused = wiki_vector.rrf_fuse(bm25_ids, vec_ids, top_k)
            by_id = {h.page_id: h for h in hits}
            page_by_id: Dict[str, WikiPage] = {p.page_id: p for p in pages}
            final: List[QueryHit] = []
            for pid in fused:
                if pid in by_id:
                    final.append(by_id[pid])
                elif pid in page_by_id:
                    # 向量独有命中 (BM25 零分页) — 构造 QueryHit 取整页
                    p = page_by_id[pid]
                    title, _ = _extract_title_and_tags(p)
                    v_score = next((s for _, i, s in vec_res if i == pid), 0.0)
                    final.append(QueryHit(
                        page_id=pid, page_type=p.page_type, score=v_score,
                        title=title, snippet=(title or pid)[:200],
                        sources=list(p.frontmatter.get("sources") or []),
                        origin=_page_origin(p),
                    ))
            if final:
                # P2 返回前复核: 融合结果再次按可见性过滤 (防御向量旁路)
                final = [h for h in final
                         if page_visible(page_by_id.get(h.page_id), actor=actor,
                                         audience=audience)]
                hits = final
                store.append_log("query", "*", "",
                                 f"q='{query[:50]}' hybrid hits={len(hits)} "
                                 f"(bm25={len(bm25_ids)}, vec={len(vec_res)})")
                return hits[:top_k]
    except Exception as e:
        logger.debug(f"[wiki_query] 混合检索降级 BM25: {e}")

    store.append_log("query", "*", "", f"q='{query[:50]}' hits={len(hits)}")
    return hits[:top_k]


def fetch_pages(page_ids: List[str], store: WikiStore = None) -> List[Dict]:
    """按 id 拉取整页用于喂给 LLM。"""
    store = store or get_wiki_store()
    result = []
    for pid in page_ids:
        for t in ("entities", "concepts", "synthesis", "summaries", "queries"):
            p = store.get(t, pid)
            if p:
                result.append({
                    "page_id": p.page_id,
                    "page_type": p.page_type,
                    "frontmatter": p.frontmatter,
                    "body": p.body,
                })
                break
    return result


if __name__ == "__main__":
    import sys, json
    q = " ".join(sys.argv[1:]) or "vm_backup_check"
    hits = search_wiki(q)
    print(json.dumps([h.to_dict() for h in hits], ensure_ascii=False, indent=2))
