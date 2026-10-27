"""
Z-OPS Cross-Session Memory Module
跨会话记忆模块 - 参考 Hermes Agent 的记忆系统

核心模块:
- cross_session_memory: 跨会话记忆管理
- user_profile: Honcho 用户画像建模
- session_search: FTS5 全文搜索
- memory_nudge: 周期性记忆推送
"""

from .cross_session_memory import (
    CrossSessionMemory,
    SessionSummary,
    UserProfile,
    get_cross_session_memory,
    get_cross_session_memory_sync
)

from .user_profile import (
    HonchoProfileManager,
    UserProfileModel,
    UserPreferences,
    UserExpertise,
    DialecticNote,
    get_profile_manager
)

from .session_search import (
    SessionSearch,
    IndexedSession,
    Message,
    SearchResult,
    get_session_search,
    get_session_search_sync
)

from .memory_nudge import (
    MemoryNudgeSystem,
    MemoryNudge,
    NudgeRule,
    get_memory_nudge_system
)

# --- Karpathy LLM-Wiki ---
from .wiki_store import WikiStore, WikiPage, get_wiki_store, slugify
from .wiki_ingest import run_full_ingest
from .wiki_query import search_wiki, fetch_pages, QueryHit
from .wiki_lint import run_lint
from .qoder_memory_bridge import import_prior_references, import_qoder_memories

__all__ = [
    # 跨会话记忆
    "CrossSessionMemory",
    "SessionSummary",
    "UserProfile",
    "get_cross_session_memory",
    "get_cross_session_memory_sync",
    
    # 用户画像
    "HonchoProfileManager",
    "UserProfileModel",
    "UserPreferences",
    "UserExpertise",
    "DialecticNote",
    "get_profile_manager",
    
    # 会话搜索
    "SessionSearch",
    "IndexedSession",
    "Message",
    "SearchResult",
    "get_session_search",
    "get_session_search_sync",
    
    # 记忆推送
    "MemoryNudgeSystem",
    "MemoryNudge",
    "NudgeRule",
    "get_memory_nudge_system",

    # LLM-Wiki（Karpathy 思想）
    "WikiStore",
    "WikiPage",
    "get_wiki_store",
    "slugify",
    "run_full_ingest",
    "search_wiki",
    "fetch_pages",
    "QueryHit",
    "run_lint",
    "import_prior_references",
    "import_qoder_memories",
]

__version__ = "1.0.0"