"""
Z-OPS Wiki Store — Karpathy LLM-Wiki 思想的 Markdown 存储层

职责：
- 读写带 YAML frontmatter 的 Markdown 页面
- 幂等追加证据（同一 evidence 不重复写入）
- 扫描双向链接 [[page-id]]
- 生成 index.md
- 追加 log.md 操作流水

不依赖 LLM，可无缝退化为纯规则运行。
"""
from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Iterator

logger = logging.getLogger("zops.memory.wiki_store")

WIKI_ROOT = Path(__file__).resolve().parent.parent.parent / "wiki"
PAGE_TYPES = ("entities", "concepts", "summaries", "synthesis", "queries")

# 目录名 → 单数 type 值（去复数时避免 entitie/summarie/synthesi 这种机械切尾 's'）
_TYPE_SINGULAR = {
    "entities": "entity",
    "concepts": "concept",
    "summaries": "summary",
    "synthesis": "synthesis",
    "queries": "query",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_LINK_RE = re.compile(r"\[\[([a-z0-9\-_]+)\]\]")
_FM_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def slugify(text: str, max_len: int = 60) -> str:
    """把任意字符串变成 kebab-case 的 slug。中文会被音译为拼音前缀 zh-<hash>。"""
    if not text:
        return "unnamed"
    t = text.strip().lower()
    # 中文 / 非 ASCII → 用 zh-<hash8>
    if not all(ord(c) < 128 for c in t):
        import hashlib
        h = hashlib.md5(t.encode("utf-8")).hexdigest()[:8]
        # 抽取 ASCII 部分作前缀
        ascii_part = _SLUG_RE.sub("-", "".join(c if ord(c) < 128 else " " for c in t)).strip("-")
        prefix = ascii_part[:20].strip("-") or "zh"
        return f"{prefix}-{h}"[:max_len]
    s = _SLUG_RE.sub("-", t).strip("-")
    return s[:max_len] or "unnamed"


def _simple_yaml_dump(data: Dict[str, Any]) -> str:
    """极简 YAML 输出（只支持 str/int/float/bool/list/dict 一层嵌套，避开 PyYAML 依赖）"""
    lines = []
    for k, v in data.items():
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f"  - {_yaml_scalar(item)}")
        elif isinstance(v, dict):
            if not v:
                lines.append(f"{k}: {{}}")
            else:
                lines.append(f"{k}:")
                for kk, vv in v.items():
                    lines.append(f"  {kk}: {_yaml_scalar(vv)}")
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    return "\n".join(lines)


def _yaml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # 需要加引号的场景
    if any(ch in s for ch in [":", "#", "[", "]", "{", "}", ",", "&", "*", "!", "|", ">", "'", '"', "\n"]) or s != s.strip():
        return json.dumps(s, ensure_ascii=False)
    return s


def _simple_yaml_load(text: str) -> Dict[str, Any]:
    """极简 YAML 解析，与 _simple_yaml_dump 对称。"""
    result: Dict[str, Any] = {}
    current_key = None
    current_kind = None  # 'list' | 'dict' | None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith("  - ") and current_kind == "list":
            result[current_key].append(_parse_scalar(raw[4:].strip()))
            continue
        if raw.startswith("  ") and current_kind == "dict":
            kk, _, vv = raw[2:].partition(":")
            result[current_key][kk.strip()] = _parse_scalar(vv.strip())
            continue
        if ":" in raw:
            k, _, v = raw.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "":
                # 下一行决定是 list 还是 dict；先存 list，遇到 dict 再转换
                result[k] = []
                current_key = k
                current_kind = "list"  # 猜测；_dict 的情况走下面分支
            elif v == "[]":
                result[k] = []
                current_key = None
                current_kind = None
            elif v == "{}":
                result[k] = {}
                current_key = None
                current_kind = None
            else:
                result[k] = _parse_scalar(v)
                current_key = None
                current_kind = None
    return result


def _parse_scalar(s: str) -> Any:
    if s == "" or s == "null":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if s.startswith('"') and s.endswith('"'):
        try:
            return json.loads(s)
        except Exception:
            return s[1:-1]
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


@dataclass
class WikiPage:
    page_type: str          # 'entities' / 'concepts' / ...
    page_id: str            # 如 'user-ops_user_001'
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    body: str = ""
    path: Optional[Path] = None

    @classmethod
    def load(cls, path: Path) -> "WikiPage":
        text = path.read_text(encoding="utf-8")
        m = _FM_RE.match(text)
        if m:
            fm = _simple_yaml_load(m.group(1))
            body = m.group(2)
        else:
            fm, body = {}, text
        return cls(
            page_type=path.parent.name,
            page_id=path.stem,
            frontmatter=fm,
            body=body,
            path=path,
        )

    def dump(self) -> str:
        fm_text = _simple_yaml_dump(self.frontmatter)
        return f"---\n{fm_text}\n---\n{self.body}"

    def save(self):
        assert self.path is not None, "path not set"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.dump(), encoding="utf-8")

    def find_backlinks(self) -> List[str]:
        return list(set(_LINK_RE.findall(self.body)))

    def ensure_section(self, header: str) -> int:
        """确保存在某个 section，返回 section 结束位置（下一个 section 前）。"""
        idx = self.body.find(f"\n{header}\n")
        if idx < 0:
            if not self.body.endswith("\n"):
                self.body += "\n"
            self.body += f"\n{header}\n"
            return len(self.body)
        # 找下一个 ## 标题
        after = idx + len(header) + 2
        next_h = self.body.find("\n## ", after)
        return next_h if next_h > 0 else len(self.body)

    def add_evidence(self, source: str, timestamp: str, detail: str):
        """幂等追加证据。按 (source, timestamp, detail) 去重。"""
        line = f"- `{source}` @ `{timestamp}` — {detail}"
        if line in self.body:
            return
        end = self.ensure_section("## Evidence")
        # 在 section 末尾追加
        self.body = self.body[:end].rstrip() + "\n" + line + "\n" + self.body[end:]

    def add_bullet(self, section_header: str, line: str):
        """幂等向某 section 追加列表项。"""
        bullet = f"- {line}" if not line.startswith("- ") else line
        if bullet in self.body:
            return
        end = self.ensure_section(section_header)
        self.body = self.body[:end].rstrip() + "\n" + bullet + "\n" + self.body[end:]

    def bump_updated(self):
        self.frontmatter["updated"] = datetime.now().isoformat()


class WikiStore:
    def __init__(self, root: Path = WIKI_ROOT):
        self.root = root
        for t in PAGE_TYPES:
            (self.root / t).mkdir(parents=True, exist_ok=True)

    # ---------- CRUD ----------

    def page_path(self, page_type: str, page_id: str) -> Path:
        assert page_type in PAGE_TYPES, f"unknown page_type: {page_type}"
        return self.root / page_type / f"{page_id}.md"

    def get(self, page_type: str, page_id: str) -> Optional[WikiPage]:
        p = self.page_path(page_type, page_id)
        return WikiPage.load(p) if p.exists() else None

    def get_or_create(
        self,
        page_type: str,
        page_id: str,
        defaults_fm: Optional[Dict[str, Any]] = None,
        default_body: str = "",
    ) -> WikiPage:
        page = self.get(page_type, page_id)
        if page:
            return page
        now = datetime.now().isoformat()
        fm = {
            "id": page_id,
            "type": _TYPE_SINGULAR.get(page_type, page_type),
            "created": now,
            "updated": now,
            "sources": [],
            "tags": [],
        }
        if defaults_fm:
            fm.update(defaults_fm)
        page = WikiPage(
            page_type=page_type,
            page_id=page_id,
            frontmatter=fm,
            body=default_body,
            path=self.page_path(page_type, page_id),
        )
        return page

    def save(self, page: WikiPage):
        page.bump_updated()
        page.save()
        # v4.0 (Phase 3.1): 同步生成向量 sidecar (<page_id>.vec.json, Markdown 不被污染;
        # embedding 服务不可用时静默降级 — 下次查询自动回退纯 BM25)
        try:
            from . import wiki_vector
            wiki_vector.write_sidecar(page)
        except Exception:
            pass

    def all_pages(self) -> Iterator[WikiPage]:
        for t in PAGE_TYPES:
            d = self.root / t
            if not d.exists():
                continue
            for p in sorted(d.glob("*.md")):
                yield WikiPage.load(p)

    # ---------- sources 合并 ----------

    @staticmethod
    def add_source(page: WikiPage, source: str):
        srcs = page.frontmatter.setdefault("sources", [])
        if source not in srcs:
            srcs.append(source)

    def delete(self, page_type: str, page_id: str) -> bool:
        """删除页面 (记忆清除等场景; 文件移除 + 索引重写)"""
        p = self.page_path(page_type, page_id)
        if not p.exists():
            return False
        p.unlink()
        try:
            self.write_index()
        except Exception:
            pass
        return True

    # ---------- index / log ----------

    def write_index(self):
        lines = [
            "# Z-OPS Wiki Index",
            "",
            f"_auto-generated at {datetime.now().isoformat()}_",
            "",
        ]
        for t in PAGE_TYPES:
            d = self.root / t
            pages = sorted(d.glob("*.md"))
            if not pages:
                continue
            lines.append(f"## {t} ({len(pages)})")
            lines.append("")
            for p in pages:
                page = WikiPage.load(p)
                title = _extract_title(page.body) or page.page_id
                lines.append(f"- [[{page.page_id}]] — {title}")
            lines.append("")
        (self.root / "index.md").write_text("\n".join(lines), encoding="utf-8")

    def append_log(self, op: str, page_id: str, source: str = "", details: str = ""):
        line = f"{datetime.now().isoformat()} | {op} | {page_id} | {source} | {details}\n"
        log_path = self.root / "log.md"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)

    # ---------- backlinks ----------

    def backlink_map(self) -> Dict[str, List[str]]:
        """返回 target_id -> [source_page_id]"""
        result: Dict[str, List[str]] = {}
        for page in self.all_pages():
            for target in page.find_backlinks():
                result.setdefault(target, []).append(page.page_id)
        return result


def _extract_title(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# 单例
_store: Optional[WikiStore] = None


def get_wiki_store() -> WikiStore:
    global _store
    if _store is None:
        _store = WikiStore()
    return _store
