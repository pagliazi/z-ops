"""
Z-OPS Wiki × 前期学习引用导入通道

将外部经验（如 IDE/Agent 平台侧的记忆内容）作为**前期学习引用**沉淀到
Z-OPS wiki/concepts/，供 LLM 检索参考。

定位约束：wiki 的核心内容是 Z-OPS 内部查询内容的更正与优化学习记录
（见 wiki_ingest.ingest_optimizations）；外部经验仅作参考，统一归属
`prior_learning_reference`，不得在页面中标注外部平台名称。

使用方式：
    from core.memory.qoder_memory_bridge import import_prior_references
    import_prior_references([
        {
            "title": "Z-OPS服务干净重启标准流程",
            "category": "task_flow_experience",
            "keywords": ["Z-OPS", "干净重启"],
            "content": "...",           # 具体经验正文
            "scope": "workspace",        # 可选
        },
        ...
    ])
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Dict, Optional, Any

from .wiki_store import WikiStore, slugify, get_wiki_store

logger = logging.getLogger("zops.memory.prior_ref_bridge")

# category → tag 映射（让 wiki 侧也能按类别检索；首标签统一为前期学习引用归属）
CATEGORY_TAGS = {
    "user_info": ["prior_learning_reference", "user"],
    "user_behavior": ["prior_learning_reference", "behavior"],
    "project_tech_stack": ["prior_learning_reference", "tech-stack"],
    "project_build_configuration": ["prior_learning_reference", "build"],
    "project_environment_configuration": ["prior_learning_reference", "env"],
    "project_scm_configuration": ["prior_learning_reference", "scm"],
    "project_introduction": ["prior_learning_reference", "project"],
    "project_configuration": ["prior_learning_reference", "config"],
    "development_code_specification": ["prior_learning_reference", "code-spec"],
    "development_practice_specification": ["prior_learning_reference", "practice"],
    "development_test_specification": ["prior_learning_reference", "test-spec"],
    "development_comment_specification": ["prior_learning_reference", "comment-spec"],
    "expert_experience": ["prior_learning_reference", "expert"],
    "common_pitfalls_experience": ["prior_learning_reference", "pitfall"],
    "task_flow_experience": ["prior_learning_reference", "task-flow"],
    "task_summary_experience": ["prior_learning_reference", "task-summary"],
    "learned_skill_experience": ["prior_learning_reference", "skill"],
    "plan_experience": ["prior_learning_reference", "plan"],
    "history_task_reference_files": ["prior_learning_reference", "reference-file"],
}

_LINK_HINT = re.compile(r"[a-z_]+-[a-z0-9_\-]+", re.I)


def _extract_related_entities(content: str) -> List[str]:
    """扫描正文中可能的 wiki 实体 id（启发式）"""
    ids = set()
    # 显式 wiki 引用格式
    for m in re.finditer(r"\[\[([a-z0-9\-_]+)\]\]", content.lower()):
        ids.add(m.group(1))
    # skill-xxx / agent-xxx / user-xxx / asset-xxx 形式
    for m in _LINK_HINT.finditer(content.lower()):
        tok = m.group(0)
        if tok.startswith(("skill-", "agent-", "user-", "asset-", "topic-", "learning-")):
            ids.add(tok)
    return sorted(ids)


def import_prior_references(
    memories: List[Dict[str, Any]],
    store: Optional[WikiStore] = None,
    source_tag: str = "prior_learning_reference",
) -> Dict[str, int]:
    """
    导入一批前期学习引用条目（外部经验，仅作参考）。

    每条 memory 必须包含：title, category, content
    可选：keywords, scope, memory_id
    """
    store = store or get_wiki_store()
    stats = {"created": 0, "updated": 0, "skipped": 0}

    for m in memories:
        title = (m.get("title") or "").strip()
        category = (m.get("category") or "").strip()
        content = (m.get("content") or "").strip()
        if not title or not content:
            stats["skipped"] += 1
            continue

        slug = slugify(title)
        page_id = slug
        tags = list(CATEGORY_TAGS.get(category, ["prior_learning_reference"]))
        if m.get("scope"):
            tags.append(f"scope-{m['scope']}")

        keywords = m.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [k.strip() for k in re.split(r"[,，]", keywords) if k.strip()]

        existed = store.get("concepts", page_id) is not None
        page = store.get_or_create(
            "concepts", page_id,
            defaults_fm={
                "sources": [source_tag],
                "tags": tags,
                # P2 (task-c17): 外部经验标记为 reference — 检索时标注"外部参考",
                # 不冒充已验证的 Z-OPS 内部纠错经验 (SPEC)
                "origin": "reference",
                "reference_category": category,
                "reference_id": m.get("memory_id", ""),
                "frequency": 1,
                "confidence": 0.9,
            },
        )

        # 重写正文（Qoder memory 本身就是已编译内容，不需要增量合并）
        related = _extract_related_entities(content)
        body_lines = [
            f"# {title}",
            "",
            "## 定义",
            content,
            "",
        ]
        if keywords:
            body_lines += ["## 关键词", ", ".join(f"`{k}`" for k in keywords), ""]
        if related:
            body_lines += ["## 关联实体"] + [f"- [[{r}]]" for r in related] + [""]
        body_lines += [
            "## 来源",
            f"- 前期学习引用 category: `{category}`",
            "- 外部经验导入，仅作参考",
            "",
            "## Evidence",
            f"- `{source_tag}` @ `{datetime.now().isoformat()}` — 作为前期学习引用导入（外部经验）",
        ]
        page.body = "\n".join(body_lines)

        # 合并 tags 去重
        cur_tags = set(page.frontmatter.get("tags") or [])
        cur_tags.update(tags)
        page.frontmatter["tags"] = sorted(cur_tags)
        store.add_source(page, source_tag)
        store.save(page)
        store.append_log(
            "prior_ref_import",
            page_id,
            source_tag,
            f"category={category} title='{title[:40]}'",
        )
        stats["updated" if existed else "created"] += 1

    store.write_index()
    return stats


# 兼容旧调用名（历史 bootstrap 脚本引用）
import_qoder_memories = import_prior_references


if __name__ == "__main__":
    # 自测：导入一条假 memory
    import json
    r = import_prior_references([{
        "title": "Bridge Self Test",
        "category": "task_flow_experience",
        "keywords": ["test"],
        "content": "This is a bridge self-test. Relates to [[skill-vm_backup_check]].",
    }])
    print(json.dumps(r, indent=2))
