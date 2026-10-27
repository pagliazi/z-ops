"""
Z-OPS v2.0.0 — 工具权限策略引擎
基于 agents.yaml 的 tools.allow / tools.deny 控制工具访问

策略规则:
- deny 优先于 allow (黑名单优先)
- allow 含 "*" 表示允许所有 (仍受 deny 约束)
- 被拒绝的工具返回 PolicyDenied
"""

import logging
import time
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from core.registry import get_registry, AgentConfig, ToolsPolicy

logger = logging.getLogger("zops")


@dataclass
class PolicyDecision:
    """权限决策结果"""
    allowed: bool = True
    agent_id: str = ""
    tool_name: str = ""
    reason: str = ""
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


class PolicyEngine:
    """
    工具权限策略引擎

    每个 Agent 在 agents.yaml 中可配置:
      tools:
        allow: [ssh_exec, gcoc_api]   # 允许列表 (白名单)
        deny: [script_exec]           # 禁止列表 (黑名单)

    校验逻辑:
    1. 如果工具在 deny 列表中 → 拒绝
    2. 如果 allow 含 "*" → 允许
    3. 如果工具在 allow 列表中 → 允许
    4. 其他 → 拒绝 (默认拒绝原则)
    """

    def __init__(self):
        self._policies: Dict[str, ToolsPolicy] = {}
        self._audit_log: List[PolicyDecision] = []
        self._max_audit = 1000

    def load_policies(self):
        """从 Registry 加载所有 Agent 的工具策略"""
        registry = get_registry()
        self._policies = {}
        for agent_id, cfg in registry.get_all().items():
            self._policies[agent_id] = cfg.tools
        logger.info(f"[policy] 加载 {len(self._policies)} 个 Agent 的工具策略")

    def check(self, agent_id: str, tool_name: str) -> PolicyDecision:
        """
        检查指定 Agent 是否允许使用某工具

        Args:
            agent_id: Agent ID
            tool_name: 工具名称

        Returns:
            PolicyDecision
        """
        policy = self._policies.get(agent_id)

        # 未找到策略 — 默认允许 (向下兼容)
        if policy is None:
            decision = PolicyDecision(
                allowed=True, agent_id=agent_id, tool_name=tool_name,
                reason="无策略配置，默认允许"
            )
            self._record(decision)
            return decision

        allowed = policy.is_allowed(tool_name)

        if tool_name in policy.deny:
            reason = f"工具 '{tool_name}' 在 deny 列表中"
        elif "*" in policy.allow:
            reason = "allow: [*] 全局允许"
        elif tool_name in policy.allow:
            reason = f"工具 '{tool_name}' 在 allow 列表中"
        else:
            reason = f"工具 '{tool_name}' 不在 allow 列表中"

        decision = PolicyDecision(
            allowed=allowed, agent_id=agent_id, tool_name=tool_name,
            reason=reason
        )
        self._record(decision)

        if not allowed:
            logger.warning(f"[policy] 拒绝: {agent_id} 使用 {tool_name} — {reason}")

        return decision

    def check_batch(self, agent_id: str, tool_names: List[str]) -> Dict[str, PolicyDecision]:
        """批量检查工具权限"""
        return {name: self.check(agent_id, name) for name in tool_names}

    def get_allowed_tools(self, agent_id: str) -> List[str]:
        """获取 Agent 允许使用的工具列表"""
        policy = self._policies.get(agent_id)
        if policy is None:
            return ["*"]
        if "*" in policy.allow:
            return ["*"]
        return [t for t in policy.allow if t not in policy.deny]

    def get_denied_tools(self, agent_id: str) -> List[str]:
        """获取 Agent 禁止使用的工具列表"""
        policy = self._policies.get(agent_id)
        if policy is None:
            return []
        return list(policy.deny)

    def _record(self, decision: PolicyDecision):
        """记录审计日志"""
        self._audit_log.append(decision)
        if len(self._audit_log) > self._max_audit:
            self._audit_log = self._audit_log[-self._max_audit:]

    def get_recent_decisions(self, count: int = 50) -> List[PolicyDecision]:
        """获取最近的权限决策记录"""
        return self._audit_log[-count:]

    def get_agent_decisions(self, agent_id: str, count: int = 20) -> List[PolicyDecision]:
        """获取指定 Agent 的权限决策记录"""
        return [d for d in self._audit_log if d.agent_id == agent_id][-count:]


# ═══ 全局单例 ═══

_policy_engine: Optional[PolicyEngine] = None


def get_policy_engine() -> PolicyEngine:
    """获取全局 PolicyEngine 单例"""
    global _policy_engine
    if _policy_engine is None:
        _policy_engine = PolicyEngine()
        _policy_engine.load_policies()
    return _policy_engine
