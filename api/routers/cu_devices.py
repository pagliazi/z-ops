"""本机 Computer Use v2 路由。由主应用显式注册，不兼容旧扩展 token。"""

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket

from core.cu import protocol as p
from core.cu.connection import DeviceConnection
from core.cu.devices import DeviceService
from core.cu.logging import install_ticket_redaction

install_ticket_redaction()
router = APIRouter()


def origins():
    return tuple(x.strip() for x in os.environ.get("ZOPS_CU_ALLOWED_ORIGINS", "").split(",") if x.strip())


def check_transport(request, require_origin=False):
    """TLS + 可选 Origin 白名单。

    浏览器路径必须带合法 Origin（防跨站伪造）；Companion 设备通道不用任何浏览器凭据
    （无 cookie/Authorization，鉴权只靠一次性票据 + 设备签名），若强制 Origin 会让
    真实网络里的本机客户端被 403 拒绝。自带 Origin 时仍按白名单拒绝非法值。
    """
    if not p.transport_ok(request.scope, request.headers, origins(), require_origin=require_origin):
        raise HTTPException(403, "需要受信任的 Origin 和 TLS")


async def get_redis():
    # 延迟导入避免 router 注册时循环导入；复用旧登录/Redis，不依赖新 UI 身份模型。
    from webchat_api import get_redis as existing_get_redis
    return await existing_get_redis()


async def verify_token(request: Request):
    from webchat_api import verify_token as existing_verify_token
    return await existing_verify_token(request)


async def authenticated(request: Request):
    check_transport(request)
    return await verify_token(request)


async def service():
    import config
    return DeviceService(await get_redis(), prefix=f"{config.REDIS_PREFIX}:cu2",
                         ceiling=p.admin_ceiling(
                             desktop=bool(getattr(config, "CU_DESKTOP_CEILING", False)),
                             scripts=bool(getattr(config, "CU_SCRIPT_CEILING", True))))


async def body(request, fields):
    # 调用方必须先认证/授权/限流；不用 Body 模型，避免 FastAPI 预先读取大载荷。
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) < 0 or int(length) > p.MAX_HTTP:
                raise HTTPException(413, "请求体过大")
        except ValueError:
            raise HTTPException(400, "请求长度格式错误") from None
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "需要 JSON 请求")

    async def read():
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > p.MAX_HTTP:
                raise HTTPException(413, "请求体过大")
            data.extend(chunk)
        return data

    try:
        data = await asyncio.wait_for(read(), timeout=5)
        obj = p.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "JSON 格式错误") from None
    except asyncio.TimeoutError:
        raise HTTPException(408, "请求体读取超时") from None
    if not isinstance(obj, dict) or set(obj) != set(fields):
        raise HTTPException(400, "请求字段不匹配")
    return obj


async def invoke(operation, status=400):
    try:
        return await operation
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(status, "设备凭据、任务绑定或请求无效") from None


async def anonymous(request, category):
    check_transport(request)
    svc = await service()
    identity = request.client.host if request.client else "unknown"
    if not await svc.rate_limit(category, identity, 20):
        raise HTTPException(429, "请求过于频繁")
    return svc


@router.post("/api/cu/devices/pair-code")
async def pair_code(actor: str = Depends(authenticated)):
    return await invoke((await service()).create_pair_code(actor))


@router.post("/api/cu/devices/enroll")
async def enroll(request: Request):
    svc = await anonymous(request, "enroll")
    obj = await body(request, {"code", "name", "platform", "public_key"})
    return await invoke(svc.enroll(**obj))


@router.post("/api/cu/devices/challenge")
async def challenge(request: Request):
    svc = await anonymous(request, "challenge")
    obj = await body(request, {"device_id"})
    return await invoke(svc.challenge(**obj))


@router.post("/api/cu/devices/ticket")
async def ticket(request: Request):
    svc = await anonymous(request, "ticket")
    obj = await body(request, {"device_id", "challenge_id", "signature"})
    return await invoke(svc.ticket(**obj))


@router.websocket("/api/cu/devices/connect")
async def connect(ws: WebSocket):
    try:
        check_transport(ws)
        # 明确拒绝 token、重复票据参数、旧扩展及用户登录凭据直连。
        pairs = list(ws.query_params.multi_items())
        if len(pairs) != 1 or pairs[0][0] != "ticket" or ws.headers.get("authorization"):
            raise ValueError("只能使用设备短票据")
        svc = await service()
        if not await svc.rate_limit("connect", ws.client.host if ws.client else "unknown", 30):
            raise ValueError("连接过于频繁")
        device = await svc.connect_ticket(pairs[0][1])
    except (HTTPException, ValueError, TypeError):
        await ws.close(code=4403)
        return
    await DeviceConnection(svc, ws, device).run()


@router.get("/api/cu/devices")
async def devices(actor: str = Depends(authenticated)):
    return await (await service()).list_devices(actor)


@router.put("/api/cu/devices/selection")
async def selection(request: Request, actor: str = Depends(authenticated)):
    obj = await body(request, {"device_id"})
    return await invoke((await service()).select(actor, obj["device_id"]), 404)


@router.delete("/api/cu/devices/{device_id}")
async def revoke(device_id: str, actor: str = Depends(authenticated)):
    await invoke((await service()).detach(device_id, "", revoked_by=actor), 404)
    return {"device_id": device_id, "revoked": True}


@router.get("/api/cu/tasks")
async def tasks(session_id: str, actor: str = Depends(authenticated)):
    return await invoke((await service()).list_tasks(actor, session_id), 404)


@router.get("/api/cu/tasks/{task_id}/status")
async def task_status(task_id: str, actor: str = Depends(authenticated)):
    return await invoke((await service()).get_task(actor, task_id), 404)


@router.post("/api/cu/tasks/{task_id}/control")
async def task_control(task_id: str, request: Request, actor: str = Depends(authenticated)):
    svc = await service()
    # 即使 Content-Length 极大或是 chunked，也先校验 actor 和 session。
    await invoke(svc.task_record(actor, task_id), 404)
    obj = await body(request, {"action"})
    return await invoke(svc.control_task(actor, task_id, obj["action"]), 409)
