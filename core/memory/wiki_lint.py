"""
Z-OPS Wiki Lint — 健康检查

检查项：
- orphan：无反向链接且非自增长页（summary/query 允许孤立）
- broken_link：[[x]] 目标不存在
- stale：updated 距今 > 90 天且无反向链接 → 标记 archived
- duplicate：正文指纹重复
- missing_fm：缺必填 frontmatter
- empty_sources：sources 为空（手写页可设 [manual]）
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Dict, List

from .wiki_store import WikiStore, WikiPage, get_wiki_store

logger = logging.getLogger("zops.memory.wiki_lint")

REQUIRED_FM = ("id", "type", "created", "updated", "sources")
STALE_DAYS = 90


def _body_fingerprint(body: str) -> str:
    # 去掉时间戳类噪声再哈希
    cleaned = "\n".join(
        line for line in body.splitlines()
        if "Evidence" not in line and not line.startswith("- `")
    )
    return hashlib.md5(cleaned.encode("utf-8")).hexdigest()


def run_lint(store: WikiStore = None, mark_archived: bool = True) -> Dict[str, List[str]]:
    store = store or get_wiki_store()
    pages = list(store.all_pages())
    by_id = {p.page_id: p for p in pages}
    backlinks = store.backlink_map()

    issues: Dict[str, List[str]] = {
        "orphan": [],
        "broken_link": [],
        "stale": [],
        "duplicate": [],
        "missing_fm": [],
        "empty_sources": [],
    }

    # 指纹桶
    fp_bucket: Dict[str, List[str]] = {}
    for p in pages:
        fp = _body_fingerprint(p.body)
        fp_bucket.setdefault(fp, []).append(p.page_id)

    now = datetime.now()
    for p in pages:
        # frontmatter 完整性
        missing = [k for k in REQUIRED_FM if k not in p.frontmatter]
        if missing:
            issues["missing_fm"].append(f"{p.page_id}: missing {missing}")

        # sources 非空
        srcs = p.frontmatter.get("sources") or []
        if not srcs:
            issues["empty_sources"].append(p.page_id)

        # 断链
        for target in p.find_backlinks():
            if target not in by_id:
                issues["broken_link"].append(f"{p.page_id} -> [[{target}]]")

        # 孤儿：summary/query 不算
        if p.page_type in ("entities", "concepts", "synthesis"):
            if p.page_id not in backlinks:
                issues["orphan"].append(p.page_id)

        # 过期
        updated = p.frontmatter.get("updated", "")
        try:
            ut = datetime.fromisoformat(updated)
            if (now - ut) > timedelta(days=STALE_DAYS) and p.page_id not in backlinks:
                issues["stale"].append(p.page_id)
                if mark_archived:
                    tags = p.frontmatter.setdefault("tags", [])
                    if "archived" not in tags:
                        tags.append("archived")
                        store.save(p)
        except Exception:
            pass

    # 重复
    for fp, ids in fp_bucket.items():
        if len(ids) > 1:
            issues["duplicate"].append(" + ".join(ids))

    store.append_log("lint", "*", "", f"issues={ {k: len(v) for k,v in issues.items()} }")
    return issues


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import pprint
    pprint.pprint(run_lint())
