"""
Z-OPS v4.0 Phase 3.1 — wiki 混合检索向量层 (借鉴 mem0 两阶段召回 + ChatGPT Dreaming 语义召回)

设计:
- 写入时 (WikiStore.save 后) 同步生成 `<page_id>.vec.json` sidecar — Markdown 纯文本不被污染
- 查询时: BM25 top-K ∪ 向量 top-K → RRF (Reciprocal Rank Fusion) 融合 → 返回整页
- embedding 失败 / 无 sidecar / 服务不可用 → 自动降级纯 BM25 (wiki_query 加权逻辑不变)
- 向量检索自研余弦 top-K (wiki 规模 <1 万页, 无需向量库); sidecar 进程内 mtime 缓存
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import config

logger = logging.getLogger("zops.memory.wiki_vector")

# RRF 融合常数 (标准 k=60, 平滑排名差异)
RRF_K = 60
# 单页送 embedding 的文本上限 (字符)
MAX_PAGE_CHARS = 6000

# sidecar 进程内缓存: path -> (mtime, page_type, page_id, vec)
_sidecar_cache: Dict[Path, Tuple[float, str, str, List[float]]] = {}
_cache_loaded_at = 0.0


def _http():
    import httpx
    return httpx.Client(timeout=10)


def embed_texts_sync(texts: List[str]) -> Optional[List[List[float]]]:
    """调用 OneAPI /embeddings (同步, 供 wiki_query 同步检索链路).

    Returns:
        向量列表; None = 服务不可用/失败 (调用方降级纯 BM25)
    """
    if not texts:
        return []
    if not config.ONEAPI_API_KEY or not config.ONEAPI_BASE_URL:
        logger.debug("[wiki_vector] ONEAPI 未配置, 跳过向量召回")
        return None
    try:
        # P3: 统一模型调用层 (同步变体; embeddings 也受 endpoint 级治理)
        from core.model_gateway import get_model_gateway
        resp = get_model_gateway().request_chat_sync(
            {"provider": "oneapi", "api_key": config.ONEAPI_API_KEY,
             "base_url": config.ONEAPI_BASE_URL.rstrip('/')},
            {"model": config.EMBEDDING_MODEL, "input": texts},
            path="/embeddings", timeout=30.0)
        if resp.status_code != 200:
            logger.warning(f"[wiki_vector] embeddings HTTP {resp.status_code}: {resp.text[:120]}")
            return None
        data = resp.json().get("data") or []
        vecs = [item.get("embedding") for item in data]
        if len(vecs) != len(texts) or any(v is None for v in vecs):
            logger.warning(f"[wiki_vector] embeddings 返回数量不符: {len(vecs)}/{len(texts)}")
            return None
        return vecs
    except Exception as e:
        logger.warning(f"[wiki_vector] embeddings 调用失败 (降级 BM25): {e}")
        return None


def page_text_for_embedding(page) -> str:
    """页面 embedding 文本 = 标题 + 标签 + 正文 (截断)"""
    title = ""
    for line in page.body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    tags = " ".join(page.frontmatter.get("tags") or [])
    return f"{title}\n{tags}\n{page.body}"[:MAX_PAGE_CHARS]


def sidecar_path(page) -> Path:
    # 与 Markdown 同目录的 <page_id>.vec.json (page.path 形如 wiki/entities/xxx.md)
    return page.path.with_suffix(".vec.json")


def write_sidecar(page) -> bool:
    """为页面生成向量 sidecar (ingest 写入时调用; 失败仅告警不阻断)"""
    try:
        vecs = embed_texts_sync([page_text_for_embedding(page)])
        if vecs is None:
            return False
        sc = sidecar_path(page)
        sc.write_text(json.dumps({
            "page_id": page.page_id,
            "page_type": page.page_type,
            "model": config.EMBEDDING_MODEL,
            "dim": len(vecs[0]),
            "vec": vecs[0],
            "updated": time.time(),
        }, ensure_ascii=False), encoding="utf-8")
        _sidecar_cache.pop(sc, None)
        return True
    except Exception as e:
        logger.warning(f"[wiki_vector] sidecar 写入失败 {page.page_id}: {e}")
        return False


def load_sidecars(root: Path, page_types: Optional[List[str]] = None
                  ) -> List[Tuple[str, str, List[float]]]:
    """扫描全部 sidecar (mtime 缓存增量读取) → [(page_type, page_id, vec)]"""
    global _cache_loaded_at
    out = []
    scanned = 0
    try:
        for sc in root.rglob("*.vec.json"):
            scanned += 1
            ptype = sc.parent.name
            if page_types and ptype not in page_types:
                continue
            try:
                mtime = sc.stat().st_mtime
                cached = _sidecar_cache.get(sc)
                if cached and cached[0] == mtime:
                    _, pt, pid, vec = cached
                    out.append((pt, pid, vec))
                    continue
                data = json.loads(sc.read_text(encoding="utf-8"))
                vec = data.get("vec") or []
                if not vec:
                    continue
                _sidecar_cache[sc] = (mtime, ptype, data.get("page_id", sc.stem), vec)
                out.append((ptype, data.get("page_id", sc.stem), vec))
            except Exception:
                continue
        _cache_loaded_at = time.time()
    except Exception as e:
        logger.debug(f"[wiki_vector] sidecar 扫描失败: {e}")
    if scanned:
        logger.debug(f"[wiki_vector] sidecar 扫描: {scanned} 文件, 载入 {len(out)} 向量")
    return out


def _cosine(a: List[float], b: List[float]) -> float:
    if len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(x * x for x in b) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def vector_search(query: str, root: Path, top_k: int,
                  page_types: Optional[List[str]] = None
                  ) -> Optional[List[Tuple[str, str, float]]]:
    """向量召回 top-K → [(page_type, page_id, cosine_score)]

    Returns None = embedding 服务不可用 (调用方降级); [] = 无 sidecar/无命中
    """
    qvec = embed_texts_sync([query[:2000]])
    if qvec is None:
        return None
    if not qvec or not qvec[0]:
        return []
    q = qvec[0]
    scored = []
    for ptype, pid, vec in load_sidecars(root, page_types):
        s = _cosine(q, vec)
        if s > 0.05:  # 噪声下限
            scored.append((ptype, pid, s))
    scored.sort(key=lambda x: x[2], reverse=True)
    return scored[:top_k]


def rrf_fuse(bm25_ranked: List[str], vec_ranked: List[str],
             top_k: int) -> List[str]:
    """Reciprocal Rank Fusion: 两路排名融合 (无参数尺度问题, k=60 标准值)"""
    scores: Dict[str, float] = {}
    for rank, pid in enumerate(bm25_ranked):
        scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, pid in enumerate(vec_ranked):
        scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
    fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [pid for pid, _ in fused[:top_k]]
