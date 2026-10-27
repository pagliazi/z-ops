"""
Z-OPS Harness - 技能数据格式定义
兼容 agentskills.io 开放标准

agentskills.io 标准字段:
- name, version, description, author, platforms
- when_to_use, procedure, pitfalls, verification

Z-OPS 扩展字段:
- metadata.zops: tags, category, requires_tools, requires_agent, n8n_workflow
- quick_reference: 快速参考
- auto_generated: 是否自动生成
"""

from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
import yaml
import json


class SkillExportFormat:
    """技能导出格式"""
    AGENTSKILLS_IO = "agentskills.io"
    ZOPS_NATIVE = "zops_native"
    OPENAI_TOOLS = "openai_tools"


@dataclass
class SkillMetadata:
    """技能元数据 (Z-OPS 扩展)"""
    tags: List[str] = field(default_factory=list)
    category: str = "general"  # ops / network / vm / dev / general
    requires_tools: List[str] = field(default_factory=list)  # 需要的工具
    requires_agent: List[str] = field(default_factory=list)  # 需要的 Agent
    n8n_workflow: Optional[str] = None  # 关联的 N8N 工作流
    min_zops_version: str = "2.0.0"  # 最低 Z-OPS 版本要求
    examples: List[str] = field(default_factory=list)  # 使用示例
    related_skills: List[str] = field(default_factory=list)  # 相关技能


@dataclass
class Skill:
    """
    技能定义 - 兼容 agentskills.io 标准
    
    标准字段 (agentskills.io):
    - name: 技能名称
    - version: 版本号
    - description: 描述
    - author: 作者
    - platforms: 支持的平台
    - when_to_use: 使用场景
    - procedure: 执行步骤
    - pitfalls: 注意事项
    - verification: 验证方法
    
    Z-OPS 扩展:
    - quick_reference: 快速参考
    - metadata: 元数据
    - 使用统计和成功率
    """
    # 标准字段
    name: str
    description: str
    version: str = "1.0.0"
    author: str = "zops-system"
    platforms: List[str] = field(default_factory=lambda: ["linux"])
    when_to_use: str = ""
    procedure: str = ""
    pitfalls: str = ""
    verification: str = ""
    
    # Z-OPS 扩展字段
    quick_reference: str = ""
    metadata: SkillMetadata = field(default_factory=SkillMetadata)
    
    # 运行时数据
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    usage_count: int = 0
    success_rate: float = 1.0
    auto_generated: bool = False
    
    def to_json(self) -> Dict:
        """转换为 JSON 格式"""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "platforms": self.platforms,
            "when_to_use": self.when_to_use,
            "procedure": self.procedure,
            "pitfalls": self.pitfalls,
            "verification": self.verification,
            "quick_reference": self.quick_reference,
            "metadata": {
                "zops": {
                    "tags": self.metadata.tags,
                    "category": self.metadata.category,
                    "requires_tools": self.metadata.requires_tools,
                    "requires_agent": self.metadata.requires_agent,
                    "n8n_workflow": self.metadata.n8n_workflow,
                    "min_zops_version": self.metadata.min_zops_version,
                    "examples": self.metadata.examples,
                    "related_skills": self.metadata.related_skills,
                }
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "usage_count": self.usage_count,
            "success_rate": self.success_rate,
            "auto_generated": self.auto_generated,
        }
    
    def to_yaml(self) -> str:
        """转换为 YAML 格式 (agentskills.io 兼容)"""
        return yaml.dump(self.to_json(), allow_unicode=True, default_flow_style=False)
    
    def to_agentskills_io(self) -> Dict:
        """导出为 agentskills.io 标准格式"""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "platforms": self.platforms,
            "when_to_use": self.when_to_use,
            "procedure": self.procedure,
            "pitfalls": self.pitfalls,
            "verification": self.verification,
        }
    
    def to_openai_tool(self) -> Dict:
        """导出为 OpenAI Tools 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},  # 需要从 procedure 中解析
                    "required": []
                }
            }
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'Skill':
        """从字典创建"""
        m = SkillMetadata()
        if "metadata" in data and "zops" in data["metadata"]:
            z = data["metadata"]["zops"]
            m.tags = z.get("tags", [])
            m.category = z.get("category", "general")
            m.requires_tools = z.get("requires_tools", [])
            m.requires_agent = z.get("requires_agent", [])
            m.n8n_workflow = z.get("n8n_workflow")
            m.min_zops_version = z.get("min_zops_version", "2.0.0")
            m.examples = z.get("examples", [])
            m.related_skills = z.get("related_skills", [])
        
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            author=data.get("author", "zops-system"),
            platforms=data.get("platforms", ["linux"]),
            when_to_use=data.get("when_to_use", ""),
            procedure=data.get("procedure", ""),
            pitfalls=data.get("pitfalls", ""),
            verification=data.get("verification", ""),
            quick_reference=data.get("quick_reference", ""),
            metadata=m,
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            usage_count=data.get("usage_count", 0),
            success_rate=data.get("success_rate", 1.0),
            auto_generated=data.get("auto_generated", False),
        )
    
    @classmethod
    def from_yaml(cls, yaml_content: str) -> 'Skill':
        """从 YAML 创建"""
        data = yaml.safe_load(yaml_content)
        return cls.from_dict(data)

    def to_prompt(self) -> str:
        """转换为 Prompt 格式"""
        prompt = f"""# {self.name}
{self.description}

## When to Use
{self.when_to_use}

## Procedure
{self.procedure}"""
        if self.pitfalls:
            prompt += f"\n## Pitfalls\n{self.pitfalls}"
        if self.verification:
            prompt += f"\n## Verification\n{self.verification}"
        if self.quick_reference:
            prompt += f"\n## Quick Reference\n{self.quick_reference}"
        return prompt


def import_skill_from_agentskills_io(skill_data: Dict) -> Skill:
    """从 agentskills.io 格式导入技能"""
    return Skill.from_dict(skill_data)


def export_skill_to_agentskills_io(skill: Skill) -> Dict:
    """导出技能到 agentskills.io 格式"""
    return skill.to_agentskills_io()
