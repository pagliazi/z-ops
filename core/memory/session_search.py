"""
Z-OPS Session Search - FTS5 Full-Text Search
会话全文搜索系统 - 参考 Hermes Agent 的 FTS5 会话搜索

使用 SQLite FTS5 实现高效的全文搜索:
- 历史会话检索
- 跨会话知识回忆
- LLM 摘要增强搜索
"""

import logging
import sqlite3
import json
import asyncio
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
import re

logger = logging.getLogger("zops.memory.search")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
FTS_DB_PATH = DATA_DIR / "session_fts.db"


@dataclass
class Message:
    """消息记录"""
    role: str  # user / assistant / system
    content: str
    timestamp: str = ""
    agent_id: str = ""
    skill_used: str = ""


@dataclass
class IndexedSession:
    """已索引的会话"""
    session_id: str
    user_id: str
    agent_id: str
    created_at: str
    messages: List[Message] = field(default_factory=list)
    summary: str = ""
    topics: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SearchResult:
    """搜索结果"""
    session_id: str
    user_id: str
    agent_id: str
    created_at: str
    summary: str
    matched_snippet: str  # 匹配的文本片段
    relevance_score: float  # 相关性分数
    topics: List[str] = field(default_factory=list)


class SessionSearch:
    """
    FTS5 全文会话搜索
    
    参考 Hermes Agent 的会话搜索设计:
    1. 使用 SQLite FTS5 实现高效全文索引
    2. 支持会话摘要和消息内容搜索
    3. 可选 LLM 摘要增强
    """
    
    def __init__(self, db_path: str = None, llm_client=None):
        self.db_path = db_path or str(FTS_DB_PATH)
        self.llm = llm_client
        self._init_db()
    
    def _init_db(self):
        """初始化数据库和 FTS5 表"""
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
            
        try:
            # 创建会话元数据表
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    agent_id TEXT,
                    created_at TEXT,
                    summary TEXT,
                    topics TEXT,
                    message_count INTEGER DEFAULT 0
                )
            """)
                
            # 创建消息内容表
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT,
                    agent_id TEXT,
                    skill_used TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
            """)
                
            self.db.commit()
                
            # 尝试创建 FTS5 表（如果支持）
            try:
                self.db.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                        content,
                        content='messages',
                        content_rowid='id'
                    )
                """)
                    
                # 创建触发器：自动同步 FTS
                self.db.execute("""
                    CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                        INSERT INTO messages_fts(rowid, content)
                        VALUES (new.id, new.content);
                    END
                """)
                    
                self.db.execute("""
                    CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                        INSERT INTO messages_fts(messages_fts, rowid, content)
                        VALUES('delete', old.id, old.content);
                    END
                """)
                self.fts_enabled = True
            except sqlite3.OperationalError:
                logger.warning("FTS5 不可用，使用 LIKE 搜索")
                self.fts_enabled = False
                
            # 创建索引
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")
                
            self.db.commit()
            logger.info(f"搜索数据库初始化: {self.db_path} (FTS5: {self.fts_enabled})")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")
            raise
    
    async def index_session(
        self,
        session_id: str,
        user_id: str,
        agent_id: str,
        messages: List[Message],
        summary: str = "",
        topics: List[str] = None
    ):
        """
        索引会话内容
        
        将会话消息存入数据库，自动触发 FTS5 索引
        """
        topics_str = json.dumps(topics or [], ensure_ascii=False)
        
        # 插入会话元数据
        self.db.execute("""
            INSERT OR REPLACE INTO sessions 
            (session_id, user_id, agent_id, created_at, summary, topics, message_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id, user_id, agent_id,
            datetime.now().isoformat(),
            summary, topics_str, len(messages)
        ))
        
        # 插入消息
        for msg in messages:
            self.db.execute("""
                INSERT INTO messages 
                (session_id, role, content, timestamp, agent_id, skill_used)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                session_id, msg.role, msg.content,
                msg.timestamp or datetime.now().isoformat(),
                msg.agent_id or agent_id,
                msg.skill_used
            ))
        
        self.db.commit()
        logger.info(f"索引会话: {session_id} ({len(messages)} 消息)")
    
    async def search(
        self,
        query: str,
        user_id: str = None,
        agent_id: str = None,
        limit: int = 10,
        offset: int = 0
    ) -> List[SearchResult]:
        """
        全文搜索
        """
        results = []
        
        # CJK 查询走 LIKE (FTS5 默认 tokenizer 不支持中文分词)
        _has_cjk = any('\u4e00' <= c <= '\u9fff' for c in (query or ''))
        if self.fts_enabled and not _has_cjk:
            # 使用 FTS5 搜索
            clean_query = self._clean_fts_query(query)
            conditions = []
            params = []
            
            if user_id:
                conditions.append("s.user_id = ?")
                params.append(user_id)
            
            if agent_id:
                conditions.append("s.agent_id = ?")
                params.append(agent_id)
            
            where_clause = " AND " + " AND ".join(conditions) if conditions else ""
            
            try:
                sql = f"""
                    SELECT 
                        m.session_id,
                        s.user_id,
                        s.agent_id,
                        s.created_at,
                        s.summary,
                        s.topics,
                        m.content as matched_snippet
                    FROM messages_fts fts
                    JOIN messages m ON fts.rowid = m.id
                    JOIN sessions s ON m.session_id = s.session_id
                    WHERE messages_fts MATCH ?
                    {where_clause}
                    ORDER BY rank
                    LIMIT ? OFFSET ?
                """
                
                params.insert(0, clean_query)
                params.extend([limit, offset])
                
                cursor = self.db.execute(sql, params)
                
                for row in cursor.fetchall():
                    topics_list = []
                    if row["topics"]:
                        try:
                            topics_list = json.loads(row["topics"])
                        except:
                            pass
                    
                    results.append(SearchResult(
                        session_id=row["session_id"],
                        user_id=row["user_id"],
                        agent_id=row["agent_id"],
                        created_at=row["created_at"],
                        summary=row["summary"] or "",
                        matched_snippet=row["matched_snippet"] or "",
                        relevance_score=1.0,
                        topics=topics_list
                    ))
            except Exception as e:
                logger.warning(f"FTS5 搜索失败，降级到 LIKE: {e}")
                results = await self._search_like(query, user_id, agent_id, limit, offset)
        else:
            # 使用 LIKE 搜索
            results = await self._search_like(query, user_id, agent_id, limit, offset)
        
        logger.debug(f"搜索 '{query}': {len(results)} 结果")
        return results
    
    async def _search_like(
        self,
        query: str,
        user_id: str = None,
        agent_id: str = None,
        limit: int = 10,
        offset: int = 0
    ) -> List[SearchResult]:
        """LIKE 搜索（FTS5 不可用时的降级方案）"""
        conditions = ["m.content LIKE ?"]
        params = [f"%{query}%"]
        
        if user_id:
            conditions.append("s.user_id = ?")
            params.append(user_id)
        
        if agent_id:
            conditions.append("s.agent_id = ?")
            params.append(agent_id)
        
        sql = f"""
            SELECT 
                m.session_id,
                s.user_id,
                s.agent_id,
                s.created_at,
                s.summary,
                s.topics,
                m.content as matched_snippet
            FROM messages m
            JOIN sessions s ON m.session_id = s.session_id
            WHERE {' AND '.join(conditions)}
            ORDER BY s.created_at DESC
            LIMIT ? OFFSET ?
        """
        
        params.extend([limit, offset])
        
        cursor = self.db.execute(sql, params)
        results = []
        
        for row in cursor.fetchall():
            topics_list = []
            if row["topics"]:
                try:
                    topics_list = json.loads(row["topics"])
                except:
                    pass
            
            results.append(SearchResult(
                session_id=row["session_id"],
                user_id=row["user_id"],
                agent_id=row["agent_id"],
                created_at=row["created_at"],
                summary=row["summary"] or "",
                matched_snippet=row["matched_snippet"] or "",
                relevance_score=1.0,
                topics=topics_list
            ))
        
        return results
    
    def _clean_fts_query(self, query: str) -> str:
        """清理 FTS5 查询字符串"""
        # 移除特殊字符
        query = re.sub(r'[^\w\s\u4e00-\u9fff\-]', ' ', query)
        
        # 处理空查询
        words = query.strip().split()
        if not words:
            return "*"
        
        # 对中文词进行特殊处理
        cleaned = []
        for w in words:
            if len(w) > 1:
                cleaned.append(w)
        
        return " ".join(cleaned) if cleaned else "*"
    
    async def get_session_context(self, session_id: str) -> Optional[IndexedSession]:
        """获取会话完整上下文"""
        # 获取会话元数据
        cursor = self.db.execute(
            "SELECT * FROM sessions WHERE session_id = ?", 
            (session_id,)
        )
        row = cursor.fetchone()
        
        if not row:
            return None
        
        # 获取消息
        cursor = self.db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,)
        )
        messages = []
        for mrow in cursor.fetchall():
            messages.append(Message(
                role=mrow["role"],
                content=mrow["content"],
                timestamp=mrow["timestamp"],
                agent_id=mrow["agent_id"],
                skill_used=mrow["skill_used"]
            ))
        
        topics = []
        if row["topics"]:
            try:
                topics = json.loads(row["topics"])
            except:
                pass
        
        return IndexedSession(
            session_id=row["session_id"],
            user_id=row["user_id"],
            agent_id=row["agent_id"],
            created_at=row["created_at"],
            messages=messages,
            summary=row["summary"] or "",
            topics=topics
        )
    
    async def get_recent_sessions(self, user_id: str = None, limit: int = 20) -> List[IndexedSession]:
        """获取最近会话"""
        if user_id:
            cursor = self.db.execute(
                "SELECT * FROM sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit)
            )
        else:
            cursor = self.db.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
        
        sessions = []
        for row in cursor.fetchall():
            sessions.append(IndexedSession(
                session_id=row["session_id"],
                user_id=row["user_id"],
                agent_id=row["agent_id"],
                created_at=row["created_at"],
                summary=row["summary"] or "",
                topics=json.loads(row["topics"] or "[]")
            ))
        
        return sessions
    
    async def get_stats(self) -> Dict:
        """获取统计信息"""
        cursor = self.db.execute("SELECT COUNT(*) FROM sessions")
        session_count = cursor.fetchone()[0]
        
        cursor = self.db.execute("SELECT COUNT(*) FROM messages")
        message_count = cursor.fetchone()[0]
        
        return {
            "total_sessions": session_count,
            "total_messages": message_count,
            "db_path": self.db_path
        }
    
    async def delete_session(self, session_id: str):
        """删除会话及其消息"""
        self.db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        self.db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        self.db.commit()
        logger.info(f"删除会话: {session_id}")
    
    def close(self):
        """关闭数据库连接"""
        self.db.close()


# 单例
_search_instance: Optional[SessionSearch] = None


async def get_session_search() -> SessionSearch:
    """获取搜索实例"""
    global _search_instance
    if _search_instance is None:
        _search_instance = SessionSearch()
    return _search_instance


def get_session_search_sync() -> SessionSearch:
    """同步获取搜索实例"""
    global _search_instance
    if _search_instance is None:
        _search_instance = SessionSearch()
    return _search_instance