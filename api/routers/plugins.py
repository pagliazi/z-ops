"""插件管理 API — 列表 / 启停 / 重载 / 最近动态 (仅管理员)

设计对齐 dsh plugin-manager: 启停写 patch 层 (plugins/patch.yaml),
enable = 移除 patch 项恢复默认, disable = 写 disabled 并逆序撤销副作用。
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

import config
from webchat_api import get_redis, require_admin, verify_token
from core.plugin_system import (get_plugin_manager, get_event_bus,
                                PluginState, PATCH_FILE)

logger = logging.getLogger("zops")

router = APIRouter()


@router.get("/api/admin/plugins")
async def plugins_list(current_user: str = Depends(verify_token)):
    """插件清单 (含状态/贡献/错误)"""
    await require_admin(current_user)
    mgr = get_plugin_manager()
    return {
        "plugins": mgr.list(),
        "events": [{"event": e, "mode": mgr.ctx.bus.mode_of(e),
                    "listeners": len(mgr.ctx.bus.listeners_of(e))}
                   for e in mgr.ctx.bus._modes],
        "patch_file": str(PATCH_FILE),
    }


@router.post("/api/admin/plugins/{pid}/enable")
async def plugin_enable(pid: str, current_user: str = Depends(verify_token)):
    await require_admin(current_user)
    mgr = get_plugin_manager()
    r = await mgr.enable(pid)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or f"启用失败: {r.get('state')}")
    from webchat_api import _log_user_op
    await _log_user_op(current_user, "plugin_enable", f"plugin={pid}")
    return r


@router.post("/api/admin/plugins/{pid}/disable")
async def plugin_disable(pid: str, current_user: str = Depends(verify_token)):
    await require_admin(current_user)
    mgr = get_plugin_manager()
    r = await mgr.disable(pid)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "禁用失败")
    from webchat_api import _log_user_op
    await _log_user_op(current_user, "plugin_disable", f"plugin={pid}")
    return r


@router.post("/api/admin/plugins/reload")
async def plugins_reload(request: Request, current_user: str = Depends(verify_token)):
    await require_admin(current_user)
    body = await request.json() if request.headers.get("content-length", 0) not in (b"", 0, None) else {}
    pid = str((body or {}).get("id", ""))
    mgr = get_plugin_manager()
    r = await mgr.reload(pid or "")
    from webchat_api import _log_user_op
    await _log_user_op(current_user, "plugin_reload", f"plugin={pid or 'ALL'}")
    return r


@router.get("/api/admin/plugins/recap")
async def plugins_recap(current_user: str = Depends(verify_token)):
    """session_recap 插件的最近动态 (zops:plugins:recap)"""
    await require_admin(current_user)
    r = await get_redis()
    items = await r.lrange(f"{config.REDIS_PREFIX}:plugins:recap", 0, 49)
    out = []
    for raw in items or []:
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return {"recap": out}
