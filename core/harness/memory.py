"""
Z-OPS Harness 记忆系统
"""

import logging
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger("zops.harness.memory")


class MemoryEntry:
    def __init__(self, entry_type: str, content: str, task: str = "", agent_id: str = "", session_id: str = ""):
        self.id = f"{entry_type}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self.type = entry_type
        self.content = content
        self.task = task
        self.agent_id = agent_id
        self.session_id = session_id
        self.created_at = datetime.now().isoformat()
        self.access_count = 0


class HarnessMemory:
    def __init__(self, redis_client=None, llm_client=None):
        self.redis = redis_client
        self.llm = llm_client
        self._memory: List[MemoryEntry] = []
    
    async def store_failure(self, task: str, error: str, solution: str, context: dict, agent_id: str = "", session_id: str = "") -> MemoryEntry:
        content = f"任务: {task}\n错误: {error}\n解决方案: {solution}"
        entry = MemoryEntry("failure", content, task, agent_id, session_id)
        self._memory.append(entry)
        logger.debug(f"存储失败记忆: {entry.id}")
        return entry
    
    async def store_success(self, task: str, solution: str, result: str, context: dict, agent_id: str = "", session_id: str = "") -> MemoryEntry:
        content = f"任务: {task}\n解决方案: {solution}\n结果: {result}"
        entry = MemoryEntry("success", content, task, agent_id, session_id)
        self._memory.append(entry)
        logger.debug(f"存储成功记忆: {entry.id}")
        return entry
    
    async def search(self, query: str, limit: int = 10) -> List[MemoryEntry]:
        query_lower = query.lower()
        results = [m for m in self._memory if query_lower in m.content.lower() or query_lower in m.task.lower()]
        for m in results:
            m.access_count += 1
        results.sort(key=lambda x: x.access_count, reverse=True)
        return results[:limit]
    
    async def get_recent(self, memory_type: str = None, limit: int = 20) -> List[MemoryEntry]:
        filtered = self._memory if not memory_type else [m for m in self._memory if m.type == memory_type]
        return sorted(filtered, key=lambda x: x.created_at, reverse=True)[:limit]
    
    async def get_stats(self) -> Dict:
        return {"total": len(self._memory), "by_type": {"failure": len([m for m in self._memory if m.type == "failure"]), "success": len([m for m in self._memory if m.type == "success"])}}


_memory_instance = None

async def get_harness_memory() -> HarnessMemory:
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = HarnessMemory()
    return _memory_instance
