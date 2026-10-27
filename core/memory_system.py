"""
Z-OPS v4 — Memory System (文件持久化 + LLM 召回)

参考 Claude Code 的记忆系统设计:
- 文件存储 (非数据库)，用户可直接查看/编辑
- 四层记忆分类: user / feedback / project / reference
- LLM 召回: session 启动时用小模型挑选相关记忆注入 system prompt
- 信任通过透明: .md 文件，带 frontmatter 元数据

文件结构:
    ~/.zops/memory/
    ├── MEMORY.md          # 索引 (≤200 行, ≤25KB)
    ├── user/
    │   └── prefs.md       # 用户偏好
    ├── feedback/
    │   └── corrections.md # 纠正记录
    ├── projects/
    │   └── zops-migration.md
    └── team/              # 团队共享 (symlink)
"""

import os
import time
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("zops")


# ═══ 记忆类型 ═══

class MemoryType:
    USER = "user"          # 用户偏好、角色、专业知识
    FEEDBACK = "feedback"  # 纠正记录 + 验证方法
    PROJECT = "project"    # 活跃工作上下文
    REFERENCE = "reference" # 外部系统指针


# ═══ 记忆条目 ═══

@dataclass
class MemoryEntry:
    """单条记忆"""
    name: str                      # 标题
    description: str               # 一行摘要 (LLM 召回索引)
    type: str                      # user / feedback / project / reference
    content: str                   # 正文
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    file_path: str = ""            # 文件路径
    
    def to_frontmatter(self) -> str:
        """生成 frontmatter + body 格式"""
        fm = (
            f"---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            f"type: {self.type}\n"
            f"created: {self.created_at}\n"
            f"updated: {self.updated_at}\n"
            f"---\n\n"
            f"{self.content}"
        )
        return fm
    
    @classmethod
    def from_file(cls, file_path: str) -> Optional["MemoryEntry"]:
        """从文件加载记忆"""
        try:
            content = Path(file_path).read_text(encoding="utf-8")
            # 解析 frontmatter
            if not content.startswith("---"):
                return None
            
            parts = content.split("---", 2)
            if len(parts) < 3:
                return None
            
            fm_text = parts[1].strip()
            body = parts[2].strip()
            
            fm = {}
            for line in fm_text.split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    fm[key.strip()] = val.strip()
            
            return cls(
                name=fm.get("name", Path(file_path).stem),
                description=fm.get("description", ""),
                type=fm.get("type", MemoryType.USER),
                content=body,
                created_at=float(fm.get("created", time.time())),
                updated_at=float(fm.get("updated", time.time())),
                file_path=file_path,
            )
        except Exception as e:
            logger.warning(f"[memory] 加载文件失败 {file_path}: {e}")
            return None


# ═══ Memory 系统 ═══

class MemorySystem:
    """
    Memory 文件化系统
    
    核心功能:
    1. 文件持久化 (增删改查)
    2. LLM 召回 (挑选相关记忆)
    3. 索引维护 (MEMORY.md)
    4. 自动学习 (feedback 写入)
    
    使用方式:
        mem = MemorySystem(user="admin")
        
        # 添加记忆
        mem.add(MemoryEntry(
            name="用户偏好",
            description="喜欢简洁回答，不需要详细解释",
            type=MemoryType.USER,
            content="回答时直接给出结论，省略推理过程。"
        ))
        
        # 召回相关记忆
        memories = mem.recall("PowerShell 故障排查", top_k=3)
        
        # 注入 system prompt
        prompt = mem.build_system_context(memories)
    """
    
    def __init__(self, user: str, base_dir: str = None):
        self.user = user
        self.base_dir = Path(base_dir or self._default_dir())
        self._ensure_dirs()
    
    def _default_dir(self) -> str:
        """默认记忆目录"""
        return os.path.expanduser(f"~/.zops/memory/{self.user}")
    
    def _ensure_dirs(self):
        """确保目录结构存在"""
        for subdir in ["", "user", "feedback", "projects", "reference", "team"]:
            (self.base_dir / subdir).mkdir(parents=True, exist_ok=True)
    
    def _type_dir(self, mem_type: str) -> Path:
        """获取类型对应目录 (自动创建 — Hermes 原版 singular/plural 不一致修复)"""
        d = self.base_dir / mem_type
        d.mkdir(parents=True, exist_ok=True)
        return d
    
    def _slug(self, name: str) -> str:
        """生成文件名 (slugify)"""
        import re
        slug = re.sub(r'[^\w\s-]', '', name.lower())
        slug = re.sub(r'[\s_]+', '-', slug).strip('-')
        return slug or "untitled"
    
    def add(self, entry: MemoryEntry) -> str:
        """
        添加记忆到文件
        
        返回: 文件路径
        """
        entry.updated_at = time.time()
        
        # 确定目录
        type_dir = self._type_dir(entry.type)
        filename = self._slug(entry.name) + ".md"
        file_path = type_dir / filename
        
        # 写入文件
        file_path.write_text(entry.to_frontmatter(), encoding="utf-8")
        entry.file_path = str(file_path)
        
        logger.info(f"[memory] 添加记忆: {entry.name} ({entry.type}) → {file_path}")
        
        # 更新索引
        self._update_index()
        
        return str(file_path)
    
    def get(self, name: str, mem_type: str = None) -> Optional[MemoryEntry]:
        """获取单条记忆"""
        if mem_type:
            file_path = self._type_dir(mem_type) / (self._slug(name) + ".md")
        else:
            # 搜索所有类型
            for t in [MemoryType.USER, MemoryType.FEEDBACK, MemoryType.PROJECT, MemoryType.REFERENCE]:
                file_path = self._type_dir(t) / (self._slug(name) + ".md")
                if file_path.exists():
                    return MemoryEntry.from_file(str(file_path))
            return None
        
        if not file_path.exists():
            return None
        return MemoryEntry.from_file(str(file_path))
    
    def list_memories(self, mem_type: str = None) -> List[MemoryEntry]:
        """列出所有记忆"""
        entries = []
        types = [mem_type] if mem_type else [MemoryType.USER, MemoryType.FEEDBACK, MemoryType.PROJECT, MemoryType.REFERENCE]
        
        for t in types:
            type_dir = self._type_dir(t)
            if not type_dir.exists():
                continue
            for f in type_dir.glob("*.md"):
                if f.name == "MEMORY.md":
                    continue
                entry = MemoryEntry.from_file(str(f))
                if entry:
                    entries.append(entry)
        
        return entries
    
    def delete(self, name: str, mem_type: str) -> bool:
        """删除记忆"""
        file_path = self._type_dir(mem_type) / (self._slug(name) + ".md")
        if file_path.exists():
            file_path.unlink()
            self._update_index()
            logger.info(f"[memory] 删除记忆: {name} ({mem_type})")
            return True
        return False
    
    def recall(self, query: str, top_k: int = 5) -> List[MemoryEntry]:
        """
        召回相关记忆 (简单关键词匹配，Phase 3 后续接入 LLM)
        
        TODO: 用小模型 (Haiku) 做语义召回
        """
        all_memories = self.list_memories()
        
        # 简单评分: 关键词匹配数
        # 中文兼容: 空格分词对中文无效 (整句=1 token) → 加字符 bigram
        _q = query.lower()
        query_words = set(_q.split())
        if any('\u4e00' <= c <= '\u9fff' for c in _q):
            # 中文: 逐字符 + bigram
            query_words.update(_q[i] for i in range(len(_q)))
            query_words.update(_q[i:i+2] for i in range(len(_q) - 1))
        scored = []
        for mem in all_memories:
            text = f"{mem.name} {mem.description} {mem.content}".lower()
            score = sum(1 for w in query_words if w in text)
            if score > 0:
                scored.append((score, mem))
        
        # 按评分排序，取 top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        return [mem for _, mem in scored[:top_k]]
    
    def recall_semantic(self, query: str, top_k: int = 3,
                        llm_func=None) -> List[MemoryEntry]:
        """LLM 语义召回 (Hermes TODO: 用小模型做语义匹配)

        策略: 把所有记忆的 name+description 拼成编号清单交给 LLM,
        让 LLM 选出与 query 语义最相关的 top_k 条 (解决 bigram 无法
        处理同义词/跨领域匹配的问题, 如「虚拟机怎么搬迁」命中「VM 迁移方案」)。
        LLM 不可用时降级到关键词 recall。
        """
        all_memories = self.list_memories()
        if not all_memories or not query:
            return []
        if not llm_func:
            return self.recall(query, top_k)

        # 编号清单
        lines = ["从以下用户记忆中选择与问题最相关的 #{top_k} 条 (只输出编号, 逗号分隔):"]
        for i, m in enumerate(all_memories):
            lines.append(f"[{i}] ({m.type}) {m.name}: {m.description[:60]}")
        lines.append(f"\n问题: {query}")
        prompt = "\n".join(lines)
        try:
            # llm_func 是同步或异步均可
            import asyncio
            if asyncio.iscoroutinefunction(llm_func):
                result = asyncio.get_event_loop().run_until_complete(
                    llm_func(prompt=prompt))
            else:
                result = llm_func(prompt=prompt)
            # 解析编号
            import re
            nums = [int(x) for x in re.findall(r'\d+', str(result))[:top_k]]
            hits = [all_memories[i] for i in nums if 0 <= i < len(all_memories)]
            return hits if hits else self.recall(query, top_k)
        except Exception:
            return self.recall(query, top_k)

    def build_system_context(self, memories: List[MemoryEntry]) -> str:
        """
        构建 system prompt 上下文 (注入到 LLM 调用)
        
        格式:
            === 用户记忆 ===
            [用户偏好]
            - 喜欢简洁回答...
            
            [反馈记录]
            - 不要用 rm -rf，改用 mv 备份...
        """
        if not memories:
            return ""
        
        parts = ["=== 用户记忆 (自动加载) ===\n"]
        
        by_type = {}
        for mem in memories:
            by_type.setdefault(mem.type, []).append(mem)
        
        type_labels = {
            MemoryType.USER: "用户偏好",
            MemoryType.FEEDBACK: "纠正记录",
            MemoryType.PROJECT: "项目上下文",
            MemoryType.REFERENCE: "外部参考",
        }
        
        for mem_type, entries in by_type.items():
            label = type_labels.get(mem_type, mem_type)
            parts.append(f"\n[{label}]")
            for mem in entries:
                parts.append(f"- {mem.content[:200]}")
        
        parts.append("\n=== 记忆结束 ===\n")
        return "\n".join(parts)
    
    def add_feedback(self, query: str, correction: str, rule: str = "") -> str:
        """
        添加反馈记录 (自动学习)
        
        格式:
            规则 → **Why:** → **How to apply:**
        """
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        content = f"**触发**: {query}\n\n"
        if rule:
            content += f"**规则**: {rule}\n\n"
        content += f"**纠正**: {correction}\n\n"
        content += f"**时间**: {timestamp}"
        
        entry = MemoryEntry(
            name=f"feedback-{int(time.time())}",
            description=f"纠正: {query[:50]}",
            type=MemoryType.FEEDBACK,
            content=content,
        )
        return self.add(entry)
    
    def _update_index(self):
        """更新 MEMORY.md 索引"""
        all_memories = self.list_memories()
        if not all_memories:
            return
        
        # 按类型分组
        by_type = {}
        for mem in all_memories:
            by_type.setdefault(mem.type, []).append(mem)
        
        # 生成索引
        lines = [
            "# MEMORY.md — 记忆索引",
            f"用户: {self.user}",
            f"更新: {time.strftime('%Y-%m-%d %H:%M')}",
            f"总计: {len(all_memories)} 条记忆",
            "",
        ]
        
        type_labels = {
            MemoryType.USER: "用户偏好",
            MemoryType.FEEDBACK: "纠正记录",
            MemoryType.PROJECT: "项目上下文",
            MemoryType.REFERENCE: "外部参考",
        }
        
        for mem_type, label in type_labels.items():
            entries = by_type.get(mem_type, [])
            if not entries:
                continue
            lines.append(f"\n## {label} ({len(entries)})")
            for mem in entries:
                lines.append(f"- **{mem.name}**: {mem.description}")
        
        # 检查是否超出限制
        content = "\n".join(lines)
        if len(content.split("\n")) > 200:
            lines = lines[:200]
            lines.append("\n...(索引过长，已截断)")
            content = "\n".join(lines)
        
        (self.base_dir / "MEMORY.md").write_text(content, encoding="utf-8")
    
    def stats(self) -> Dict[str, Any]:
        """统计信息"""
        all_memories = self.list_memories()
        by_type = {}
        for mem in all_memories:
            by_type[mem.type] = by_type.get(mem.type, 0) + 1
        
        return {
            "user": self.user,
            "base_dir": str(self.base_dir),
            "total": len(all_memories),
            "by_type": by_type,
        }


# ═══ 便捷函数 ═══

def get_memory(user: str) -> MemorySystem:
    """获取用户记忆系统"""
    return MemorySystem(user=user)


# ═══ 测试 ═══

if __name__ == "__main__":
    mem = MemorySystem(user="test")
    
    # 添加记忆
    mem.add(MemoryEntry(
        name="回答风格",
        description="喜欢简洁直接的回答",
        type=MemoryType.USER,
        content="回答时直接给出结论，省略推理过程。关键命令用代码块展示。"
    ))
    
    mem.add(MemoryEntry(
        name="PowerShell教训",
        description="不要用 rm -rf 删除 PowerShell 模块",
        type=MemoryType.FEEDBACK,
        content="删除 PowerShell 模块时不要用 rm -rf，改用 Uninstall-Module 命令。"
    ))
    
    # 召回
    memories = mem.recall("PowerShell", top_k=3)
    print(f"召回 {len(memories)} 条记忆")
    for m in memories:
        print(f"  - {m.name}: {m.description}")
    
    # System context
    context = mem.build_system_context(memories)
    print(f"\nSystem Context:\n{context}")
    
    # 统计
    print(f"\n统计: {mem.stats()}")
