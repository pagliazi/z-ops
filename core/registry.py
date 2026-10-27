"""
Z-OPS v2.0.0 — AgentRegistry
声明式 Agent 注册表，从 agents.yaml 加载配置
支持:
  - YAML 配置加载与校验
  - Agent 注册/发现/心跳
  - 热加载 (运行时增减 Agent)
"""

import os
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field

import yaml

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

# 配置文件路径
DEFAULT_AGENTS_YAML = config.BASE_DIR / "agents.yaml"


@dataclass
class ToolsPolicy:
    """工具权限策略"""
    allow: List[str] = field(default_factory=lambda: ["*"])
    deny: List[str] = field(default_factory=list)

    def is_allowed(self, tool_name: str) -> bool:
        """检查工具是否被允许"""
        if tool_name in self.deny:
            return False
        if "*" in self.allow or tool_name in self.allow:
            return True
        return False


@dataclass
class BindingRule:
    """路由绑定规则"""
    action: str = ""
    keywords: List[str] = field(default_factory=list)
    channel: str = ""  # 限定通道 (空=所有)
    priority: int = 10


@dataclass
class AgentConfig:
    """Agent 声明式配置"""
    id: str = ""
    name: str = ""
    description: str = ""
    workspace: str = ""
    model: str = ""
    skills: List[str] = field(default_factory=list)
    tools: ToolsPolicy = field(default_factory=ToolsPolicy)
    soul: str = ""  # SOUL.md 路径
    soul_content: str = ""  # 加载后的 SOUL.md 内容
    bindings: List[BindingRule] = field(default_factory=list)
    enabled: bool = True
    # v4.0 (Phase 2.2 路由知识单一事实源):
    #   capability_summary — LLM function_calling 路由的 tool 描述 (能力边界三句话,
    #                        替代 orchestrator 内 338 行硬编码 AGENT_TOOL_DEFINITIONS;
    #                        具体 case 补丁已移入 tests/routing/eval_cases.yaml 评测集守护)
    #   routing_hints     — 高置信快速路由关键词 (orchestrator 内置规则之后的补充层)
    capability_summary: str = ""
    routing_hints: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict, base_dir: Path = None) -> "AgentConfig":
        """从字典构建 AgentConfig"""
        tools_data = data.get("tools", {})
        tools = ToolsPolicy(
            allow=tools_data.get("allow", ["*"]),
            deny=tools_data.get("deny", [])
        )

        bindings = []
        for b in data.get("bindings", []):
            bindings.append(BindingRule(
                action=b.get("action", ""),
                keywords=b.get("keywords", []),
                channel=b.get("channel", ""),
                priority=b.get("priority", 10)
            ))

        cfg = cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            workspace=data.get("workspace", ""),
            model=data.get("model", ""),
            skills=data.get("skills", []),
            tools=tools,
            soul=data.get("soul", ""),
            bindings=bindings,
            enabled=data.get("enabled", True),
            capability_summary=data.get("capability_summary", ""),
            routing_hints=[h.lower() for h in data.get("routing_hints", [])],
        )

        # 解析相对路径
        if base_dir and cfg.workspace and not os.path.isabs(cfg.workspace):
            cfg.workspace = str(base_dir / cfg.workspace)
        if base_dir and cfg.soul and not os.path.isabs(cfg.soul):
            cfg.soul = str(base_dir / cfg.soul)

        return cfg


class AgentRegistry:
    """
    Agent 注册表 — 声明式配置管理中心

    功能:
    1. 从 agents.yaml 加载全部 Agent 配置
    2. 提供 Agent 发现和查询
    3. 支持运行时热加载
    """

    def __init__(self, config_path: str = None):
        self._config_path = Path(config_path) if config_path else DEFAULT_AGENTS_YAML
        self._agents: Dict[str, AgentConfig] = {}
        self._load_time: float = 0.0

    def load(self) -> bool:
        """加载 agents.yaml 配置"""
        if not self._config_path.exists():
            logger.error(f"[registry] agents.yaml 不存在: {self._config_path}")
            return False

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)

            if not data or "agents" not in data:
                logger.error("[registry] agents.yaml 格式错误: 缺少 'agents' 键")
                return False

            base_dir = self._config_path.parent
            agents = {}
            for agent_data in data["agents"]:
                cfg = AgentConfig.from_dict(agent_data, base_dir)
                if not cfg.id:
                    logger.warning(f"[registry] 跳过无 id 的 Agent: {agent_data}")
                    continue
                agents[cfg.id] = cfg

                # 确保 workspace 目录存在
                if cfg.workspace:
                    Path(cfg.workspace).mkdir(parents=True, exist_ok=True)

            self._agents = agents
            self._load_time = time.time()
            logger.info(f"[registry] 加载 {len(agents)} 个 Agent 配置: {list(agents.keys())}")
            return True

        except yaml.YAMLError as e:
            logger.error(f"[registry] YAML 解析失败: {e}")
            return False
        except Exception as e:
            logger.error(f"[registry] 加载失败: {e}")
            return False

    def reload(self) -> bool:
        """热加载: 重新加载配置"""
        old_agents = set(self._agents.keys())
        success = self.load()
        if success:
            new_agents = set(self._agents.keys())
            added = new_agents - old_agents
            removed = old_agents - new_agents
            if added:
                logger.info(f"[registry] 新增 Agent: {added}")
            if removed:
                logger.info(f"[registry] 移除 Agent: {removed}")
        return success

    def get(self, agent_id: str) -> Optional[AgentConfig]:
        """获取指定 Agent 的配置"""
        return self._agents.get(agent_id)

    def get_all(self) -> Dict[str, AgentConfig]:
        """获取全部 Agent 配置"""
        return dict(self._agents)

    def get_enabled(self) -> Dict[str, AgentConfig]:
        """获取全部启用的 Agent 配置"""
        return {k: v for k, v in self._agents.items() if v.enabled}

    def list_ids(self) -> List[str]:
        """列出所有 Agent ID"""
        return list(self._agents.keys())

    def is_config_changed(self) -> bool:
        """检查配置文件是否已变更"""
        if not self._config_path.exists():
            return False
        mtime = self._config_path.stat().st_mtime
        return mtime > self._load_time

    @property
    def count(self) -> int:
        return len(self._agents)


# ═══ 全局单例 ═══

_registry: Optional[AgentRegistry] = None


def get_registry(config_path: str = None) -> AgentRegistry:
    """获取全局 AgentRegistry 单例"""
    global _registry
    if _registry is None:
        _registry = AgentRegistry(config_path)
        _registry.load()
    return _registry
