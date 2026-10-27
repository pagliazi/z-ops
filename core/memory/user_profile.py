"""
Z-OPS User Profile - Honcho Dialectic User Modeling
用户画像建模系统 - 参考 Hermes Agent 的 Honcho dialectic 方法

核心概念:
- Dialectic: 通过对话逐步构建用户模型
- 用户偏好、专长、沟通风格的学习
- 动态调整 Agent 行为以适应用户
"""

import logging
import json
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("zops.memory.profile")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
PROFILE_DB_PATH = DATA_DIR / "user_profiles.json"


@dataclass
class DialecticNote:
    """Honcho dialectic note - 用户行为的观察记录"""
    timestamp: str
    observation: str  # 观察到的用户行为
    interpretation: str  # 对行为的解读
    action_taken: str = ""  # Agent 采取的响应动作
    outcome: str = ""  # 结果反馈
    confidence: float = 0.5  # 信心度 0-1


@dataclass 
class UserPreferences:
    """用户偏好模型"""
    # 沟通偏好
    detail_level: str = "medium"  # brief / medium / detailed
    response_style: str = "professional"  # casual / professional / technical
    language_preference: str = "zh-CN"
    
    # 任务偏好
    preferred_tools: List[str] = field(default_factory=list)
    task_priorities: Dict[str, int] = field(default_factory=dict)
    
    # 时间偏好
    active_hours: List[int] = field(default_factory=list)  # 0-23
    preferred_response_time: int = 30  # seconds
    
    # 通知偏好
    notification_channels: List[str] = field(default_factory=list)
    notification_frequency: str = "normal"  # sparse / normal / frequent


@dataclass
class UserExpertise:
    """用户专长模型"""
    primary_domain: str = "general"  # ops / dev / network / vm / general
    skill_level: str = "intermediate"  # beginner / intermediate / expert
    known_systems: List[str] = field(default_factory=list)
    known_tools: List[str] = field(default_factory=list)
    
    # 运维领域细分
    vm_platforms: List[str] = field(default_factory=list)  # vmware, kvm, pve
    network_knowledge: List[str] = field(default_factory=list)
    script_languages: List[str] = field(default_factory=list)  # python, bash, ansible


@dataclass
class UserProfileModel:
    """
    用户画像模型 (Honcho Dialectic)
    
    通过持续的对话观察，逐步构建对用户的理解:
    1. 观察用户行为 -> DialecticNote
    2. 解读行为意图
    3. 调整响应策略
    4. 验证调整效果
    """
    user_id: str
    name: str = ""
    department: str = ""
    role: str = "user"  # admin / ops / dev / user
    
    # 偏好模型
    preferences: UserPreferences = field(default_factory=UserPreferences)
    
    # 专长模型
    expertise: UserExpertise = field(default_factory=UserExpertise)
    
    # Dialectic notes (用户行为观察记录)
    dialectic_notes: List[DialecticNote] = field(default_factory=list)
    
    # 统计数据
    total_interactions: int = 0
    successful_interactions: int = 0
    last_interaction: str = ""
    
    # 动态调整
    recommended_agents: List[str] = field(default_factory=list)
    personalized_prompts: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "department": self.department,
            "role": self.role,
            "preferences": asdict(self.preferences),
            "expertise": asdict(self.expertise),
            "dialectic_notes": [asdict(n) for n in self.dialectic_notes],
            "total_interactions": self.total_interactions,
            "successful_interactions": self.successful_interactions,
            "last_interaction": self.last_interaction,
            "recommended_agents": self.recommended_agents,
            "personalized_prompts": self.personalized_prompts,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'UserProfileModel':
        prefs_data = data.get("preferences", {})
        prefs = UserPreferences(**prefs_data)
        
        exp_data = data.get("expertise", {})
        exp = UserExpertise(**exp_data)
        
        notes_data = data.get("dialectic_notes", [])
        notes = [DialecticNote(**n) for n in notes_data]
        
        return cls(
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            department=data.get("department", ""),
            role=data.get("role", "user"),
            preferences=prefs,
            expertise=exp,
            dialectic_notes=notes,
            total_interactions=data.get("total_interactions", 0),
            successful_interactions=data.get("successful_interactions", 0),
            last_interaction=data.get("last_interaction", ""),
            recommended_agents=data.get("recommended_agents", []),
            personalized_prompts=data.get("personalized_prompts", {}),
        )


class HonchoProfileManager:
    """
    Honcho 用户画像管理器
    
    实现 dialectic 用户建模方法:
    1. 观察 -> 记录用户行为
    2. 解读 -> 分析行为意图
    3. 行动 -> 调整响应策略
    4. 反思 -> 验证效果，修正模型
    """
    
    def __init__(self, redis_client=None):
        self.redis = redis_client
        self._profiles: Dict[str, UserProfileModel] = {}
        self._load_from_disk()
    
    def _load_from_disk(self):
        """加载用户画像"""
        if PROFILE_DB_PATH.exists():
            try:
                data = json.loads(PROFILE_DB_PATH.read_text())
                for uid, pdata in data.get("profiles", {}).items():
                    self._profiles[uid] = UserProfileModel.from_dict(pdata)
                logger.info(f"加载用户画像: {len(self._profiles)} 个")
            except Exception as e:
                logger.warning(f"加载画像失败: {e}")
    
    def _save_to_disk(self):
        """保存到磁盘"""
        try:
            data = {
                "profiles": {uid: p.to_dict() for uid, p in self._profiles.items()},
                "updated_at": datetime.now().isoformat()
            }
            PROFILE_DB_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.error(f"保存画像失败: {e}")
    
    async def get_profile(self, user_id: str) -> UserProfileModel:
        """获取用户画像"""
        if user_id not in self._profiles:
            self._profiles[user_id] = UserProfileModel(user_id=user_id)
        return self._profiles[user_id]
    
    async def observe(
        self,
        user_id: str,
        observation: str,
        interpretation: str,
        action_taken: str = "",
        outcome: str = "",
        confidence: float = 0.5
    ) -> DialecticNote:
        """
        Honcho dialectic: 观察用户行为
        
        在每次交互后调用，记录用户行为和解读
        """
        profile = await self.get_profile(user_id)
        
        note = DialecticNote(
            timestamp=datetime.now().isoformat(),
            observation=observation,
            interpretation=interpretation,
            action_taken=action_taken,
            outcome=outcome,
            confidence=confidence
        )
        
        profile.dialectic_notes.append(note)
        
        # 限制 notes 数量
        if len(profile.dialectic_notes) > 100:
            profile.dialectic_notes = profile.dialectic_notes[-100:]
        
        profile.total_interactions += 1
        if outcome == "success":
            profile.successful_interactions += 1
        profile.last_interaction = note.timestamp
        
        # 基于 observation 更新模型
        await self._update_model_from_observation(user_id, observation, interpretation)
        
        self._save_to_disk()
        logger.debug(f"添加 dialectic note: {user_id}")
        
        return note
    
    async def _update_model_from_observation(
        self, 
        user_id: str, 
        observation: str, 
        interpretation: str
    ):
        """从观察中更新用户模型"""
        profile = self._profiles[user_id]
        
        # 检测专长领域
        if any(kw in observation.lower() for kw in ["vmware", "vm", "虚拟机", "esxi", "vcenter"]):
            if "vmware" not in profile.expertise.vm_platforms:
                profile.expertise.vm_platforms.append("vmware")
            if "vm_agent" not in profile.recommended_agents:
                profile.recommended_agents.append("vm_agent")
        
        if any(kw in observation.lower() for kw in ["网络", "network", "dns", "ip", "ping"]):
            if "network_agent" not in profile.recommended_agents:
                profile.recommended_agents.append("network_agent")
        
        if any(kw in observation.lower() for kw in ["脚本", "script", "bash", "python", "ansible"]):
            if "dev_agent" not in profile.recommended_agents:
                profile.recommended_agents.append("dev_agent")
        
        # 检测偏好
        if "详细" in interpretation or "detailed" in interpretation.lower():
            profile.preferences.detail_level = "detailed"
        
        if "简单" in interpretation or "brief" in interpretation.lower():
            profile.preferences.detail_level = "brief"
        
        # 检测角色
        if any(kw in observation.lower() for kw in ["运维", "ops", "巡检", "告警"]):
            profile.role = "ops"
            profile.expertise.primary_domain = "ops"
        
        if any(kw in observation.lower() for kw in ["开发", "dev", "代码", "git"]):
            profile.role = "dev"
            profile.expertise.primary_domain = "dev"
    
    async def get_personalized_prompt(self, user_id: str, agent_id: str) -> str:
        """
        获取个性化提示词
        
        基于用户画像，为特定 Agent 生成个性化提示词
        """
        profile = await self.get_profile(user_id)
        
        base_prompt = f"用户 {user_id} "
        
        if profile.name:
            base_prompt += f"({profile.name}) "
        
        if profile.role != "user":
            base_prompt += f"角色: {profile.role}, "
        
        if profile.expertise.primary_domain != "general":
            base_prompt += f"专长领域: {profile.expertise.primary_domain}, "
        
        if profile.preferences.detail_level != "medium":
            base_prompt += f"偏好回复详细程度: {profile.preferences.detail_level}, "
        
        if profile.recommended_agents:
            base_prompt += f"常用代理: {', '.join(profile.recommended_agents[:3])}"
        
        return base_prompt
    
    async def add_explicit_note(self, user_id: str, content: str) -> None:
        """P2 收尾 (task-c17): 显式记忆写入画像 — 只记录用户主动「记住」的内容。

        SPEC: 画像推断不转授权 — 本方法不接受任何推断来源, 调用方必须传用户显式
        指令原文; 观察型 dialectic 仍由会话摘要流程产生但不参与授权判定。
        """
        profile = await self.get_profile(user_id)
        from datetime import datetime as _dt
        profile.dialectic_notes.append(DialecticNote(
            timestamp=_dt.now().isoformat(),
            observation=f"[explicit] {content[:200]}",
            interpretation="用户显式要求长期记住 (非推断)",
            action_taken="recorded", outcome="", confidence=1.0))
        profile.dialectic_notes = profile.dialectic_notes[-100:]
        self._save_to_disk()

    async def update_preference(self, user_id: str, key: str, value: Any):
        """更新用户偏好"""
        profile = await self.get_profile(user_id)
        
        # 支持嵌套更新
        if "." in key:
            parts = key.split(".")
            obj = profile.preferences
            for p in parts[:-1]:
                obj = getattr(obj, p)
            setattr(obj, parts[-1], value)
        else:
            if hasattr(profile.preferences, key):
                setattr(profile.preferences, key, value)
        
        self._save_to_disk()
    
    async def get_dialectic_history(self, user_id: str, limit: int = 20) -> List[DialecticNote]:
        """获取用户的行为观察历史"""
        profile = await self.get_profile(user_id)
        return profile.dialectic_notes[-limit:]
    
    async def get_stats(self) -> Dict:
        """获取统计"""
        return {
            "total_users": len(self._profiles),
            "users_by_role": {
                role: len([p for p in self._profiles.values() if p.role == role])
                for role in ["admin", "ops", "dev", "user"]
            }
        }


# 单例
_profile_manager: Optional[HonchoProfileManager] = None


async def get_profile_manager() -> HonchoProfileManager:
    """获取画像管理器实例"""
    global _profile_manager
    if _profile_manager is None:
        _profile_manager = HonchoProfileManager()
    return _profile_manager