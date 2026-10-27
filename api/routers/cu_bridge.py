"""Computer Use 桥路由 — Chrome 扩展接入 / 前端实时视图 / 状态查询

- WS /api/cu/bridge/ext?token=  扩展 (background service worker) 接入
- WS /api/cu/bridge/view?token=  前端实时小窗查看者接入
- GET /api/cu/bridge/status     我的浏览器是否已连接 (芯片/面板状态)
- GET /api/cu/extension/download  扩展 zip (免翻墙安装)
"""

import json
import logging
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

import config
from webchat_api import get_redis, verify_token
from core import cu_bridge

logger = logging.getLogger("zops")

router = APIRouter()

EXT_DIR = Path(__file__).resolve().parent.parent.parent / "extension"


async def _ws_actor(ws: WebSocket) -> str:
    """WS 鉴权: query token → username; 失败关闭 (code 4401)"""
    token = (ws.query_params.get("token") or "").strip()
    if token:
        r = await get_redis()
        username = await r.get(config.key_token(token))
        if username:
            return username
    await ws.close(code=4401)
    raise WebSocketDisconnect(code=4401)


@router.websocket("/api/cu/bridge/ext")
async def bridge_ext_ws(ws: WebSocket):
    """旧版服务器直连通道: 只保留诊断, 不再接受任何任务命令。"""
    await ws.accept()
    try:
        await _ws_actor(ws)
    except WebSocketDisconnect:
        return
    try:
        await ws.send_text(json.dumps({"type": "legacy_disabled", "protocol": 2,
            "message": "服务器浏览器执行已停用；请安装 Companion 并在「设置 → 电脑操作」配对设备。"},
            ensure_ascii=False))
    finally:
        await ws.close(code=4400)


@router.websocket("/api/cu/bridge/view")
async def bridge_view_ws(ws: WebSocket):
    """旧版服务器浏览器查看通道: 只保留诊断, 画面改由设备任务接口提供。"""
    await ws.accept()
    try:
        await _ws_actor(ws)
    except WebSocketDisconnect:
        return
    try:
        await ws.send_text(json.dumps({"type": "legacy_disabled", "protocol": 2,
            "message": "服务器浏览器画面已停用；请在小窗查看本机设备任务。"}, ensure_ascii=False))
    finally:
        await ws.close(code=4400)


@router.get("/api/cu/bridge/status")
async def bridge_status(session_id: str = "", current_user: str = Depends(verify_token)):
    st = cu_bridge.get_status(current_user)
    st["server_live"] = await cu_bridge.get_server_live(current_user)
    st["prefs"] = await cu_bridge.get_prefs(current_user)
    # 登录一律在用户本机完成; 服务端不再提供凭据输入。
    st["login_wall"] = False
    return st


@router.get("/api/cu/bridge/stats")
async def bridge_stats(current_user: str = Depends(verify_token)):
    from webchat_api import require_admin
    await require_admin(current_user)
    return cu_bridge.all_stats()


# ═══ 旧版扩展入口 (已停用, 保留路由给出明确错误) ═══

@router.post("/api/cu/bridge/pair")
async def bridge_pair(current_user: str = Depends(verify_token)):
    """旧版扩展连接码已停用; 设备配对码见 /api/cu/devices/pair-code。"""
    raise HTTPException(410, "旧版扩展连接码已停用；请在「设置 → 电脑操作」生成设备配对码")


@router.post("/api/cu/bridge/use-my-browser")
async def bridge_use_my_browser(current_user: str = Depends(verify_token)):
    """已停用: 不存在“这一次改用本机浏览器”的临时回退, 执行位置只由设备选择决定。"""
    raise HTTPException(410, "该临时切换已停用；请在本机安装并选择 Companion 设备")


# ═══ 用户级 Computer Use 设置 (设置页读写) ═══

@router.get("/api/cu/prefs")
async def cu_prefs_get(current_user: str = Depends(verify_token)):
    return await cu_bridge.get_prefs(current_user)


@router.put("/api/cu/prefs")
async def cu_prefs_set(request: Request, current_user: str = Depends(verify_token)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求格式错误")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求格式错误")
    return await cu_bridge.set_prefs(current_user, body)


# ═══ 小窗内一次性凭据输入 (已停用) ═══


@router.post("/api/cu/manual-login")
async def cu_manual_login(request: Request, current_user: str = Depends(verify_token)):
    """已停用: 新 CU 不在本服务中转密码。

    登录只能发生在用户操作端 Companion 的浏览器里；服务端不接收凭据。
    保留路由是为了让旧前端按明确错误失败，而不是静默回退。
    """
    raise HTTPException(
        410, "服务端凭据输入已停用；请在「设置 → 电脑操作」连接本机设备，在本机浏览器登录")


@router.post("/api/cu/bridge/pair/claim")
async def bridge_pair_claim(request: Request):
    """旧版扩展凭码换 token 已停用; 设备身份改由 Ed25519 + 一次性配对码绑定。"""
    raise HTTPException(410, "旧版扩展配对已停用；请使用 Companion 设备配对")


@router.get("/api/cu/extension/download")
async def extension_download():
    """打包 extension/ 目录为 zip 供安装 (无需认证 — 目录内无敏感信息)"""
    if not EXT_DIR.exists():
        raise HTTPException(404, "扩展目录不存在")
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(EXT_DIR.rglob("*")):
            if f.is_file() and "__pycache__" not in f.name:
                zf.write(f, f.relative_to(EXT_DIR.parent))
    buf.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="zops-browser-bridge.zip"'})
