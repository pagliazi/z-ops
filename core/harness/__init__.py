"""
Z-OPS Harness Engineering 模块 v2.2
基于 Hermes Agent 理念的自我进化系统（进阶版）

核心模块:
- skill_schema: 技能数据模型 (agentskills.io 兼容)
- skill_registry: 技能注册表管理 (版本管理)
- skill_generator: 基于失败记录的自动技能生成
- skill_improver: 技能持续改进循环 (v2.2: LLM驱动 + A/B测试 + 自动回滚)
- memory: 跨会话记忆系统
- n8n_bridge: N8N 工作流集成
- n8n_skill: N8N 技能封装基类
- scheduler: 自动化任务调度
"""

from .skill_schema import Skill, SkillMetadata
from .skill_registry import SkillRegistry, get_skill_registry
from .skill_generator import SkillGenerator, HarnessController, FailureRecord
from .skill_improver import (
    SkillImprover, 
    get_skill_improver, 
    Feedback, 
    UsageAnalysis, 
    Improvement
)
from .memory import HarnessMemory, get_harness_memory, MemoryEntry
from .n8n_bridge import N8nHarnessBridge, HarnessN8nExecutor, get_n8n_bridge, get_harness_executor
from .n8n_skill import (
    N8nBaseSkill, 
    get_skill, 
    execute_skill,
    VMPowerSkill, 
    VMBackupCheckSkill,
    SystemHealthCheckSkill,
    NotificationSkill,
    ApprovalRequestSkill,
    DailyHealthReportSkill
)
from .scheduler import TaskScheduler, ScheduledTask, get_task_scheduler, start_scheduler

__all__ = [
    # 技能模型
    "Skill", "SkillMetadata",
    "SkillRegistry", "get_skill_registry",
    # 技能生成
    "SkillGenerator", "HarnessController", "FailureRecord",
    # 技能改进
    "SkillImprover", "get_skill_improver",
    "Feedback", "UsageAnalysis", "Improvement",
    # 记忆系统
    "HarnessMemory", "get_harness_memory", "MemoryEntry",
    # N8N 集成
    "N8nHarnessBridge", "HarnessN8nExecutor", "get_n8n_bridge", "get_harness_executor",
    # N8N 技能封装
    "N8nBaseSkill", "get_skill", "execute_skill",
    "VMPowerSkill", "VMBackupCheckSkill", "SystemHealthCheckSkill",
    "NotificationSkill", "ApprovalRequestSkill", "DailyHealthReportSkill",
    # 任务调度
    "TaskScheduler", "ScheduledTask", "get_task_scheduler", "start_scheduler",
]

__version__ = "2.2.0"
