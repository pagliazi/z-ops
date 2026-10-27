"""
Z-OPS v2.0.0 — Browser Session Manager
浏览器会话管理：支持 Attach 到现有浏览器会话

核心功能:
1. Attach 到用户的真实浏览器会话 (userDataDir)
2. 保留登录态和 Cookie
3. 多会话管理
4. 浏览器连接池
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger("zops")


@dataclass
class BrowserSession:
    """浏览器会话"""
    session_id: str
    user_data_dir: str
    context = None  # BrowserContext
    page = None  # Page
    created_at: float = 0.0
    last_used: float = 0.0


class BrowserSessionManager:
    """
    浏览器会话管理器
    
    使用方式:
    1. 创建新会话：manager.create_session(session_id, user_data_dir)
    2. Attach 现有会话：manager.attach_session(session_id, user_data_dir)
    3. 获取页面：manager.get_page(session_id)
    4. 关闭会话：manager.close_session(session_id)
    """
    
    def __init__(self):
        self._playwright = None
        self._sessions: Dict[str, BrowserSession] = {}
    
    async def initialize(self):
        """初始化 Playwright"""
        if self._playwright is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            logger.info("[browser_session] Playwright 已初始化")
    
    async def create_session(
        self,
        session_id: str = "default",
        user_data_dir: str = None,
        headless: bool = True
    ) -> BrowserSession:
        """
        创建新的浏览器会话
        
        Args:
            session_id: 会话 ID
            user_data_dir: 用户数据目录（用于持久化）
            headless: 是否无头模式
            
        Returns:
            BrowserSession
        """
        await self.initialize()
        
        import time
        
        # 如果有 user_data_dir，使用持久化上下文
        if user_data_dir:
            logger.info(f"📎 创建持久化浏览器会话：{session_id} (userDataDir={user_data_dir})")
            context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-default-apps',
                    '--disable-sync',
                    '--no-first-run',
                ],
                viewport={'width': 1920, 'height': 1080}
            )
        else:
            # 创建新的无痕会话
            logger.info(f"🆕 创建新的浏览器会话：{session_id}")
            browser = await self._playwright.chromium.launch(
                headless=headless,
                args=['--disable-blink-features=AutomationControlled']
            )
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080}
            )
        
        session = BrowserSession(
            session_id=session_id,
            user_data_dir=user_data_dir or "",
            context=context,
            page=None,
            created_at=time.time(),
            last_used=time.time()
        )
        
        self._sessions[session_id] = session
        logger.info(f"[browser_session] 会话已创建：{session_id}")
        return session
    
    async def attach_session(
        self,
        session_id: str,
        user_data_dir: str,
        headless: bool = False
    ) -> BrowserSession:
        """
        Attach 到现有的浏览器会话
        
        Args:
            session_id: 会话 ID
            user_data_dir: 用户数据目录（必须存在）
            headless: 是否无头模式（Attach 时建议可见）
            
        Returns:
            BrowserSession
        """
        if not os.path.exists(user_data_dir):
            raise FileNotFoundError(f"用户数据目录不存在：{user_data_dir}")
        
        logger.info(f"📎 Attach 到现有浏览器会话：{session_id}")
        return await self.create_session(session_id, user_data_dir, headless)
    
    async def get_page(self, session_id: str = "default") -> any:
        """
        获取或创建页面
        
        Args:
            session_id: 会话 ID
            
        Returns:
            Page
        """
        if session_id not in self._sessions:
            logger.warning(f"会话不存在，创建新会话：{session_id}")
            await self.create_session(session_id)
        
        session = self._sessions[session_id]
        
        # 如果还没有页面，创建一个
        if session.page is None:
            pages = session.context.pages
            if len(pages) > 0:
                session.page = pages[0]
            else:
                session.page = await session.context.new_page()
                logger.info(f"[browser_session] 创建新页面：{session_id}")
        
        import time
        session.last_used = time.time()
        return session.page
    
    async def close_session(self, session_id: str):
        """关闭会话"""
        if session_id in self._sessions:
            session = self._sessions[session_id]
            if session.context:
                await session.context.close()
            del self._sessions[session_id]
            logger.info(f"[browser_session] 会话已关闭：{session_id}")
    
    async def cleanup(self):
        """清理所有会话"""
        logger.info(f"[browser_session] 清理 {len(self._sessions)} 个会话")
        for session_id in list(self._sessions.keys()):
            await self.close_session(session_id)
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
    
    def list_sessions(self) -> list:
        """列出所有活跃会话"""
        return list(self._sessions.keys())
    
    def get_session_info(self, session_id: str) -> dict:
        """获取会话信息"""
        if session_id not in self._sessions:
            return None
        
        session = self._sessions[session_id]
        import time
        return {
            "session_id": session.session_id,
            "user_data_dir": session.user_data_dir,
            "created_at": session.created_at,
            "last_used": session.last_used,
            "age_seconds": time.time() - session.created_at,
            "idle_seconds": time.time() - session.last_used
        }


# 全局单例
_session_manager: Optional[BrowserSessionManager] = None


def get_browser_session_manager() -> BrowserSessionManager:
    """获取全局浏览器会话管理器"""
    global _session_manager
    if _session_manager is None:
        _session_manager = BrowserSessionManager()
    return _session_manager


async def create_browser_session(
    session_id: str = "default",
    user_data_dir: str = None,
    headless: bool = True
) -> BrowserSession:
    """创建浏览器会话（便捷函数）"""
    manager = get_browser_session_manager()
    return await manager.create_session(session_id, user_data_dir, headless)


async def attach_browser_session(
    session_id: str,
    user_data_dir: str,
    headless: bool = False
) -> BrowserSession:
    """Attach 到现有浏览器会话（便捷函数）"""
    manager = get_browser_session_manager()
    return await manager.attach_session(session_id, user_data_dir, headless)


async def get_browser_page(session_id: str = "default") -> any:
    """获取浏览器页面（便捷函数）"""
    manager = get_browser_session_manager()
    return await manager.get_page(session_id)


async def close_browser_session(session_id: str):
    """关闭浏览器会话（便捷函数）"""
    manager = get_browser_session_manager()
    await manager.close_session(session_id)
