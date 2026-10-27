"""[已由 synthesizer 取代 2026-10-18] 主动记忆推送由每日记忆合成 (run_daily_synthesis)
承担, 本模块保留实现不再接线; 如需启用须经用户显式允许 (SPEC P2: 推送也是记忆暴露面)。

Z-OPS Memory Nudge System - Periodic Memory Push
周期性记忆推送系统 - 参考 Hermes Agent 的记忆 nudges

核心功能:
1. 周期性提醒 Agent 回顾重要记忆
2. 主动推送相关历史信息到用户
3. 智能选择推送时机和内容
"""

import logging
import json
import asyncio
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
import random

logger = logging.getLogger("zops.memory.nudge")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
NUDGE_DB_PATH = DATA_DIR / "memory_nudges.json"


@dataclass
class MemoryNudge:
    """记忆推送"""
    nudge_id: str
    user_id: str
    trigger_type: str  # time_based / task_related / failure_related / user_request
    content: str
    related_sessions: List[str] = field(default_factory=list)
    related_skills: List[str] = field(default_factory=list)
    priority: int = 5  # 1-10, higher is more important
    created_at: str = ""
    delivered: bool = False
    delivered_at: str = ""
    user_feedback: str = ""  # useful / not_useful / ignored
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NudgeRule:
    """推送规则"""
    rule_id: str
    name: str
    description: str
    trigger_condition: Dict[str, Any]  # 触发条件
    content_template: str  # 内容模板
    priority: int = 5
    enabled: bool = True
    last_triggered: str = ""
    trigger_count: int = 0


class MemoryNudgeSystem:
    """
    记忆推送系统
    
    参考 Hermes Agent 的 nudges 设计:
    1. 周期性推送重要记忆
    2. 任务相关时推送历史解决方案
    3. 失败发生时推送相关经验
    """
    
    # 默认推送规则
    DEFAULT_RULES = [
        {
            "rule_id": "daily_summary",
            "name": "每日摘要",
            "description": "每天推送前一天的关键交互",
            "trigger_condition": {"type": "time_based", "interval_hours": 24},
            "content_template": "昨天您进行了 {count} 次运维操作，主要涉及 {topics}。",
            "priority": 3
        },
        {
            "rule_id": "failure_recall",
            "name": "失败回顾",
            "description": "相似失败发生时推送历史解决方案",
            "trigger_condition": {"type": "failure_related", "min_similarity": 0.7},
            "content_template": "之前在处理 '{task}' 时遇到类似问题，解决方案是: {solution}",
            "priority": 8
        },
        {
            "rule_id": "skill_improvement",
            "name": "技能改进提醒",
            "description": "技能成功率下降时推送改进建议",
            "trigger_condition": {"type": "skill_related", "success_rate_threshold": 0.8},
            "content_template": "技能 '{skill}' 近期成功率较低 ({rate}%)，建议: {suggestions}",
            "priority": 7
        },
        {
            "rule_id": "weekly_insight",
            "name": "每周洞察",
            "description": "每周推送用户行为分析洞察",
            "trigger_condition": {"type": "time_based", "interval_hours": 168},
            "content_template": "本周您最常使用 {top_agent}，主要任务是 {top_tasks}。",
            "priority": 4
        }
    ]
    
    def __init__(self, redis_client=None, llm_client=None):
        self.redis = redis_client
        self.llm = llm_client
        self._nudges: Dict[str, MemoryNudge] = {}
        self._rules: Dict[str, NudgeRule] = {}
        self._load_rules()
        self._load_from_disk()
    
    def _load_rules(self):
        """加载默认规则"""
        for rule_data in self.DEFAULT_RULES:
            rule = NudgeRule(
                rule_id=rule_data["rule_id"],
                name=rule_data["name"],
                description=rule_data["description"],
                trigger_condition=rule_data["trigger_condition"],
                content_template=rule_data["content_template"],
                priority=rule_data["priority"]
            )
            self._rules[rule.rule_id] = rule
    
    def _load_from_disk(self):
        """加载推送历史"""
        if NUDGE_DB_PATH.exists():
            try:
                data = json.loads(NUDGE_DB_PATH.read_text())
                for nid, ndata in data.get("nudges", {}).items():
                    self._nudges[nid] = MemoryNudge(**ndata)
                logger.info(f"加载推送历史: {len(self._nudges)} 条")
            except Exception as e:
                logger.warning(f"加载推送历史失败: {e}")
    
    def _save_to_disk(self):
        """保存到磁盘"""
        try:
            data = {
                "nudges": {nid: n.to_dict() for nid, n in self._nudges.items()},
                "updated_at": datetime.now().isoformat()
            }
            NUDGE_DB_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.error(f"保存推送历史失败: {e}")
    
    async def check_and_generate_nudges(
        self,
        user_id: str,
        context: Dict[str, Any] = None
    ) -> List[MemoryNudge]:
        """
        检查并生成推送
        
        根据规则和上下文，决定是否生成推送
        """
        new_nudges = []
        
        for rule in self._rules.values():
            if not rule.enabled:
                continue
            
            should_trigger = await self._check_trigger_condition(rule, user_id, context)
            
            if should_trigger:
                nudge = await self._create_nudge(rule, user_id, context)
                if nudge:
                    self._nudges[nudge.nudge_id] = nudge
                    new_nudges.append(nudge)
                    rule.trigger_count += 1
                    rule.last_triggered = datetime.now().isoformat()
        
        if new_nudges:
            self._save_to_disk()
        
        return new_nudges
    
    async def _check_trigger_condition(
        self,
        rule: NudgeRule,
        user_id: str,
        context: Dict[str, Any]
    ) -> bool:
        """检查触发条件"""
        condition = rule.trigger_condition
        trigger_type = condition.get("type", "unknown")
        
        if trigger_type == "time_based":
            # 时间触发
            interval_hours = condition.get("interval_hours", 24)
            last_triggered = rule.last_triggered
            
            if not last_triggered:
                return True
            
            last_time = datetime.fromisoformat(last_triggered)
            elapsed = datetime.now() - last_time
            
            if elapsed.total_seconds() >= interval_hours * 3600:
                return True
        
        elif trigger_type == "failure_related":
            # 失败触发
            if context and context.get("has_failure"):
                return True
        
        elif trigger_type == "skill_related":
            # 技能触发
            if context:
                success_rate = context.get("skill_success_rate", 1.0)
                threshold = condition.get("success_rate_threshold", 0.8)
                if success_rate < threshold:
                    return True
        
        elif trigger_type == "task_related":
            # 任务触发
            if context and context.get("current_task"):
                return True
        
        return False
    
    async def _create_nudge(
        self,
        rule: NudgeRule,
        user_id: str,
        context: Dict[str, Any]
    ) -> Optional[MemoryNudge]:
        """创建推送"""
        import uuid
        
        nudge_id = f"nudge_{uuid.uuid4().hex[:8]}"
        
        # 渲染内容模板
        content = await self._render_template(rule.content_template, user_id, context)
        
        if not content:
            return None
        
        nudge = MemoryNudge(
            nudge_id=nudge_id,
            user_id=user_id,
            trigger_type=rule.trigger_condition.get("type", "unknown"),
            content=content,
            priority=rule.priority,
            created_at=datetime.now().isoformat(),
            related_sessions=context.get("related_sessions", []) if context else [],
            related_skills=context.get("related_skills", []) if context else []
        )
        
        logger.info(f"生成推送: {nudge_id} ({rule.name})")
        return nudge
    
    async def _render_template(
        self,
        template: str,
        user_id: str,
        context: Dict[str, Any]
    ) -> str:
        """渲染内容模板"""
        # 简单变量替换
        content = template
        
        # 基础替换
        content = content.replace("{user_id}", user_id)
        content = content.replace("{date}", datetime.now().strftime("%Y-%m-%d"))
        
        # 上下文替换
        if context:
            for key, value in context.items():
                if isinstance(value, (str, int, float)):
                    content = content.replace(f"{{{key}}}", str(value))
                elif isinstance(value, list):
                    content = content.replace(f"{{{key}}}", ", ".join(str(v) for v in value[:5]))
        
        # 检查是否有未替换的变量
        if "{" in content and "}" in content:
            # 有变量未填充，可能需要更多上下文
            logger.debug(f"模板部分未填充: {content}")
        
        return content
    
    async def get_pending_nudges(self, user_id: str = None) -> List[MemoryNudge]:
        """获取待推送的记忆"""
        pending = [n for n in self._nudges.values() if not n.delivered]
        
        if user_id:
            pending = [n for n in pending if n.user_id == user_id]
        
        # 按优先级排序
        pending.sort(key=lambda x: x.priority, reverse=True)
        return pending
    
    async def deliver_nudge(self, nudge_id: str) -> bool:
        """标记推送已发送"""
        if nudge_id not in self._nudges:
            return False
        
        nudge = self._nudges[nudge_id]
        nudge.delivered = True
        nudge.delivered_at = datetime.now().isoformat()
        
        self._save_to_disk()
        logger.info(f"推送已发送: {nudge_id}")
        return True
    
    async def record_feedback(self, nudge_id: str, feedback: str):
        """记录用户反馈"""
        if nudge_id not in self._nudges:
            return
        
        self._nudges[nudge_id].user_feedback = feedback
        self._save_to_disk()
        
        # 根据反馈调整规则
        if feedback == "not_useful":
            logger.info(f"用户标记推送无用: {nudge_id}")
        
        elif feedback == "useful":
            logger.info(f"用户标记推送有用: {nudge_id}")
    
    async def add_custom_rule(self, rule: NudgeRule):
        """添加自定义规则"""
        self._rules[rule.rule_id] = rule
        logger.info(f"添加推送规则: {rule.rule_id}")
    
    async def disable_rule(self, rule_id: str):
        """禁用规则"""
        if rule_id in self._rules:
            self._rules[rule_id].enabled = False
            logger.info(f"禁用推送规则: {rule_id}")
    
    async def get_nudge_history(self, user_id: str, limit: int = 20) -> List[MemoryNudge]:
        """获取推送历史"""
        history = [n for n in self._nudges.values() if n.user_id == user_id]
        history.sort(key=lambda x: x.created_at, reverse=True)
        return history[:limit]
    
    async def get_stats(self) -> Dict:
        """获取统计"""
        total = len(self._nudges)
        delivered = len([n for n in self._nudges.values() if n.delivered])
        useful = len([n for n in self._nudges.values() if n.user_feedback == "useful"])
        
        return {
            "total_nudges": total,
            "delivered": delivered,
            "useful": useful,
            "delivery_rate": delivered / total if total else 0,
            "useful_rate": useful / delivered if delivered else 0,
            "active_rules": len([r for r in self._rules.values() if r.enabled])
        }


# 单例
_nudge_instance: Optional[MemoryNudgeSystem] = None


async def get_memory_nudge_system() -> MemoryNudgeSystem:
    """获取推送系统实例"""
    global _nudge_instance
    if _nudge_instance is None:
        _nudge_instance = MemoryNudgeSystem()
    return _nudge_instance