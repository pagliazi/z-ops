"""
Z-OPS v2.0.0 — 双层路由引擎
精确匹配 (bindings) 优先 → LLM 意图兜底

路由策略:
1. 精确匹配: 检查所有 Agent 的 bindings.keywords，关键词命中即路由
2. Action 直接路由: 如果消息指定了 action，按 COMMAND_ROUTES 路由
3. LLM 意图兜底: 以上均无匹配，调用 LLM 进行意图识别
"""

import logging
import re
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from core.registry import get_registry, AgentConfig, BindingRule

logger = logging.getLogger("zops")


@dataclass
class RouteResult:
    """路由结果"""
    agent_id: str = ""
    action: str = ""
    confidence: float = 0.0
    method: str = ""       # binding / command / llm / default
    reasoning: str = ""
    matched_keywords: List[str] = None

    def __post_init__(self):
        if self.matched_keywords is None:
            self.matched_keywords = []

    @property
    def matched(self) -> bool:
        return bool(self.agent_id)


class Router:
    """
    双层路由引擎

    Layer 1 — 声明式精确匹配:
        遍历 agents.yaml 中每个 Agent 的 bindings，
        将用户输入与 keywords 做关键词匹配
        命中则直接路由，不走 LLM

    Layer 2 — LLM 意图兜底:
        Layer 1 无匹配时，调用 LLM 意图识别
        (由 Orchestrator 负责调用，Router 仅返回 None)
    """

    def __init__(self):
        self._binding_index: List[Tuple[str, BindingRule, AgentConfig]] = []
        self._command_routes: Dict[str, Tuple[str, str]] = {}
        self._built = False

    def build_index(self):
        """从 Registry 构建路由索引"""
        registry = get_registry()
        agents = registry.get_enabled()

        self._binding_index = []
        self._command_routes = {}

        for agent_id, cfg in agents.items():
            for binding in cfg.bindings:
                self._binding_index.append((agent_id, binding, cfg))

                # 同时构建 action -> agent 的命令路由表
                if binding.action:
                    self._command_routes[binding.action] = (agent_id, binding.action)

        # 按 priority 排序 (值越小优先级越高)
        self._binding_index.sort(key=lambda x: x[1].priority)
        self._built = True
        logger.info(f"[router] 构建路由索引: {len(self._binding_index)} 条 bindings, "
                     f"{len(self._command_routes)} 条命令路由")

    def rebuild_if_needed(self):
        """如果 Registry 有变更，重新构建索引"""
        registry = get_registry()
        if not self._built or registry.is_config_changed():
            self.build_index()

    # ─── Layer 1: 精确匹配 ───

    def match_binding(self, text: str, action: str = "",
                      channel: str = "") -> Optional[RouteResult]:
        """
        精确匹配路由

        优先级:
        1. action 直接匹配 (如 msg.action == "host_check")
        2. 关键词匹配 (bindings.keywords)

        Args:
            text: 用户输入文本
            action: 指定的 action (如果有)
            channel: 消息来源通道

        Returns:
            RouteResult 或 None (无匹配)
        """
        self.rebuild_if_needed()

        # 1. Action 直接匹配
        if action and action in self._command_routes:
            agent_id, act = self._command_routes[action]
            return RouteResult(
                agent_id=agent_id,
                action=act,
                confidence=1.0,
                method="command",
                reasoning=f"Action 直接匹配: {action}"
            )

        # 2. 关键词匹配
        if text:
            text_lower = text.lower()
            best_match: Optional[RouteResult] = None
            best_score = 0

            for agent_id, binding, cfg in self._binding_index:
                # 通道过滤
                if binding.channel and channel and binding.channel != channel:
                    continue

                # 关键词匹配
                matched = []
                for kw in binding.keywords:
                    if kw.lower() in text_lower:
                        matched.append(kw)

                if matched:
                    # 得分 = 匹配关键词数 / 总关键词数 (越多越好)
                    score = len(matched) / max(len(binding.keywords), 1)
                    # 加上优先级加成 (priority 越小越好)
                    score += (100 - min(binding.priority, 100)) / 1000

                    if score > best_score:
                        best_score = score
                        best_match = RouteResult(
                            agent_id=agent_id,
                            action=binding.action,
                            confidence=min(score, 1.0),
                            method="binding",
                            reasoning=f"关键词匹配: {matched}",
                            matched_keywords=matched
                        )

            if best_match:
                logger.info(f"[router] 精确匹配: {best_match.agent_id}.{best_match.action} "
                            f"(keywords={best_match.matched_keywords})")
                return best_match

        return None

    # ─── Layer 2: 命令路由表兼容 ───

    def get_command_route(self, action: str) -> Optional[Tuple[str, str]]:
        """兼容旧版 COMMAND_ROUTES 查找"""
        self.rebuild_if_needed()
        return self._command_routes.get(action)

    def get_all_routes(self) -> Dict[str, Tuple[str, str]]:
        """获取全部命令路由"""
        self.rebuild_if_needed()
        return dict(self._command_routes)

    # ─── 辅助方法 ───

    def get_agent_for_text(self, text: str, action: str = "",
                           channel: str = "") -> RouteResult:
        """
        完整路由决策 (Layer 1 + fallback)

        如果 Layer 1 无匹配，返回 method="need_llm" 的 RouteResult，
        由调用方 (Orchestrator) 决定是否走 LLM 意图识别。
        """
        # Layer 1: 精确匹配
        result = self.match_binding(text, action, channel)
        if result:
            return result

        # 无精确匹配 — 需要 LLM 兜底
        return RouteResult(
            agent_id="",
            action="",
            confidence=0.0,
            method="need_llm",
            reasoning="无精确匹配，需要 LLM 意图识别"
        )


# ═══ 全局单例 ═══

_router: Optional[Router] = None


def get_router() -> Router:
    """获取全局 Router 单例"""
    global _router
    if _router is None:
        _router = Router()
        _router.build_index()
    return _router
