"""
Z-OPS Cross-Session Memory System
跨会话记忆系统 - 参考 Hermes Agent 的 Honcho 用户建模

核心功能:
1. 存储会话摘要和关键学习点
2. 跨会话知识检索
3. 用户画像建模 (Honcho dialectic)
4. 与 FTS5 搜索集成
"""

import logging
import json
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
import asyncio
from pathlib import Path

logger = logging.getLogger("zops.memory.cross_session")

# 数据存储路径
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MEMORY_DB_PATH = DATA_DIR / "cross_session_memory.json"


@dataclass
class SessionSummary:
    """会话摘要"""
    session_id: str
    user_id: str
    agent_id: str
    created_at: str
    summary: str
    key_learnings: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)
    sentiment: str = "neutral"  # positive / neutral / negative
    complexity: str = "simple"  # simple / moderate / complex
    follow_up_needed: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'SessionSummary':
        return cls(**data)


@dataclass
class UserProfile:
    """用户画像 - Honcho dialectic 模型"""
    user_id: str
    name: str = ""
    role: str = ""  # admin / ops / dev / user
    preferences: Dict[str, Any] = field(default_factory=dict)
    expertise_areas: List[str] = field(default_factory=list)
    common_tasks: List[str] = field(default_factory=list)
    communication_style: str = "direct"  # direct / detailed / casual
    preferred_agents: List[str] = field(default_factory=list)
    session_count: int = 0
    last_active: str = ""
    notes: List[str] = field(default_factory=list)  # Honcho: dialectic notes
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'UserProfile':
        return cls(**data)


class CrossSessionMemory:
    """
    跨会话记忆系统
    参考 Hermes Agent 的 Honcho dialectic 用户建模
    
    功能:
    - 存储会话摘要
    - 用户画像管理
    - 跨会话知识检索
    - 与 FTS5 搜索集成
    """
    
    def __init__(self, redis_client=None, llm_client=None):
        self.redis = redis_client
        self.llm = llm_client
        self._sessions: Dict[str, SessionSummary] = {}
        self._users: Dict[str, UserProfile] = {}
        self._load_from_disk()
    
    def _load_from_disk(self):
        """从磁盘加载记忆数据"""
        if MEMORY_DB_PATH.exists():
            try:
                data = json.loads(MEMORY_DB_PATH.read_text())
                for sid, sdata in data.get("sessions", {}).items():
                    self._sessions[sid] = SessionSummary.from_dict(sdata)
                for uid, udata in data.get("users", {}).items():
                    self._users[uid] = UserProfile.from_dict(udata)
                logger.info(f"加载跨会话记忆: {len(self._sessions)} 会话, {len(self._users)} 用户")
            except Exception as e:
                logger.warning(f"加载记忆数据失败: {e}")
    
    def _save_to_disk(self):
        """保存到磁盘"""
        try:
            data = {
                "sessions": {sid: s.to_dict() for sid, s in self._sessions.items()},
                "users": {uid: u.to_dict() for uid, u in self._users.items()},
                "updated_at": datetime.now().isoformat()
            }
            MEMORY_DB_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            logger.debug(f"保存跨会话记忆到磁盘")
        except Exception as e:
            logger.error(f"保存记忆数据失败: {e}")
    
    async def store_session_summary(
        self, 
        session_id: str, 
        user_id: str,
        agent_id: str,
        summary: str,
        key_learnings: List[str] = None,
        topics: List[str] = None,
        metadata: Dict = None
    ) -> SessionSummary:
        """
        存储会话摘要
        在会话结束时调用，记录关键信息供后续检索
        """
        ss = SessionSummary(
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            created_at=datetime.now().isoformat(),
            summary=summary,
            key_learnings=key_learnings or [],
            topics=topics or [],
            metadata=metadata or {}
        )
        
        self._sessions[session_id] = ss
        
        # 更新用户画像
        await self._update_user_profile(user_id, ss)
        
        self._save_to_disk()
        logger.info(f"存储会话摘要: {session_id}")
        
        # --- 后处理走插件缝 (dsh "一切皆插件"): wiki 增量编译等由插件订阅,
        # 管理页可停用; 内联逻辑已迁至 plugins/wiki_compile (2026-10-22)
        try:
            import asyncio as _aio
            from core.plugin_system import get_event_bus
            _aio.get_event_loop().create_task(get_event_bus().emit(
                "session/summary_stored", {
                    "session_id": session_id, "user_id": user_id,
                    "summary": summary, "topics": topics or [],
                    "key_learnings": key_learnings or [],
                    "agent_id": agent_id, "ts": time.time()}))
        except Exception as e:
            logger.warning(f"session/summary_stored 事件发布失败 (不阻断): {e}")

        return ss
    
    async def _update_user_profile(self, user_id: str, session: SessionSummary):
        """更新用户画像"""
        if user_id not in self._users:
            self._users[user_id] = UserProfile(user_id=user_id)
        
        profile = self._users[user_id]
        profile.session_count += 1
        profile.last_active = session.created_at
        
        # 更新常用任务
        for topic in session.topics:
            if topic not in profile.common_tasks:
                profile.common_tasks.append(topic)
        
        # 更新偏好代理
        if session.agent_id not in profile.preferred_agents:
            profile.preferred_agents.append(session.agent_id)
        
        # Honcho dialectic note
        if session.key_learnings:
            note = f"[{session.created_at[:10]}] {session.summary[:100]}"
            profile.notes.append(note)
            if len(profile.notes) > 50:
                profile.notes = profile.notes[-50:]
    
    async def delete_session_summary(self, session_id: str, user_id: str) -> bool:
        """删除指定会话记忆 (owner 校验由调用方完成; 这里再复核一次)"""
        ss = self._sessions.get(session_id)
        if not ss or (ss.user_id or "") != (user_id or ""):
            return False
        del self._sessions[session_id]
        self._save_to_disk()
        logger.info(f"删除会话记忆: {session_id} (user={user_id})")
        return True

    async def clear_user_memories(self, user_id: str) -> int:
        """清除该用户全部会话记忆; 同步清理 wiki 私有 summaries 页 (owner 隔离)"""
        n = 0
        for sid in [k for k, v in self._sessions.items()
                    if (v.user_id or "") == (user_id or "")]:
            del self._sessions[sid]
            n += 1
        self._save_to_disk()
        try:
            from .wiki_store import get_wiki_store
            store = get_wiki_store()
            for page in list(store.all_pages()):
                if page.page_type == "summaries" and \
                        str(page.frontmatter.get("owner") or "") == (user_id or ""):
                    try:
                        store.delete("summaries", page.page_id)
                    except Exception:
                        p = store.page_path("summaries", page.page_id)
                        if p.exists():
                            p.unlink()
            store.write_index()
        except Exception as e:
            logger.warning(f"清除用户 wiki summaries 失败 (记忆已清): {e}")
        logger.info(f"清除用户全部记忆: {user_id} ({n} 条)")
        return n

    async def search_past_sessions(
        self, 
        query: str, 
        user_id: str = None,
        limit: int = 10
    ) -> List[SessionSummary]:
        """
        搜索历史会话
        支持按用户过滤和全文搜索
        """
        results = []
        query_lower = query.lower()
        
        for session in self._sessions.values():
            # 用户过滤
            if user_id and session.user_id != user_id:
                continue
            
            # 全文匹配
            if query_lower in session.summary.lower() or \
               any(query_lower in t.lower() for t in session.topics) or \
               any(query_lower in l.lower() for l in session.key_learnings):
                results.append(session)
        
        # 按时间排序
        results.sort(key=lambda x: x.created_at, reverse=True)
        return results[:limit]
    
    async def get_user_model(self, user_id: str) -> UserProfile:
        """
        获取用户画像 (Honcho dialectic)
        返回用户的偏好、专长、常用任务等信息
        """
        if user_id not in self._users:
            self._users[user_id] = UserProfile(user_id=user_id)
        return self._users[user_id]
    
    async def update_user_preference(
        self, 
        user_id: str, 
        key: str, 
        value: Any
    ):
        """更新用户偏好"""
        profile = await self.get_user_model(user_id)
        profile.preferences[key] = value
        self._save_to_disk()
    
    async def get_recent_sessions(
        self, 
        user_id: str = None,
        limit: int = 20
    ) -> List[SessionSummary]:
        """获取最近的会话"""
        sessions = list(self._sessions.values())
        if user_id:
            sessions = [s for s in sessions if s.user_id == user_id]
        sessions.sort(key=lambda x: x.created_at, reverse=True)
        return sessions[:limit]
    
    async def get_session_context(self, session_id: str) -> Optional[SessionSummary]:
        """获取指定会话的上下文"""
        return self._sessions.get(session_id)
    
    async def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            "total_sessions": len(self._sessions),
            "total_users": len(self._users),
            "sessions_per_user": {
                uid: self._users[uid].session_count 
                for uid in self._users
            }
        }


# 单例实例
_memory_instance: Optional[CrossSessionMemory] = None


async def get_cross_session_memory() -> CrossSessionMemory:
    """获取跨会话记忆实例"""
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = CrossSessionMemory()
    return _memory_instance


def get_cross_session_memory_sync() -> CrossSessionMemory:
    """同步获取实例"""
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = CrossSessionMemory()
    return _memory_instance