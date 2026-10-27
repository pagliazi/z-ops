"""
Z-OPS v2.0.0 — SOUL.md 加载器
每个 Agent 的 system prompt 从独立的 SOUL.md 文件加载
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("zops")


def load_soul(soul_path: str) -> str:
    """
    加载 SOUL.md 文件内容作为 Agent 的 system prompt

    Args:
        soul_path: SOUL.md 文件路径

    Returns:
        SOUL.md 内容字符串，加载失败返回空字符串
    """
    if not soul_path:
        return ""

    p = Path(soul_path)
    if not p.exists():
        logger.debug(f"[soul] SOUL.md 不存在: {soul_path}")
        return ""

    try:
        content = p.read_text(encoding="utf-8").strip()
        if content:
            logger.info(f"[soul] 加载 SOUL.md: {soul_path} ({len(content)} 字符)")
        return content
    except Exception as e:
        logger.error(f"[soul] 加载 SOUL.md 失败 ({soul_path}): {e}")
        return ""


def ensure_soul_file(soul_path: str, default_content: str = "") -> bool:
    """
    确保 SOUL.md 文件存在，不存在则创建默认内容

    Args:
        soul_path: SOUL.md 文件路径
        default_content: 默认内容

    Returns:
        文件是否存在 (包括新创建的)
    """
    if not soul_path:
        return False

    p = Path(soul_path)
    if p.exists():
        return True

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(default_content or "# Agent SOUL\n\n请在此配置 Agent 的 system prompt。\n",
                     encoding="utf-8")
        logger.info(f"[soul] 创建默认 SOUL.md: {soul_path}")
        return True
    except Exception as e:
        logger.error(f"[soul] 创建 SOUL.md 失败: {e}")
        return False
