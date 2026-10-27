"""
Z-OPS 飞书通道 — 事件接收网关(入站)

两条接收路径,共用同一条消息处理链路(feishu.conversation._handle_feishu_message)
与卡片回调分发(feishu.actions.handle_card_action):

  ① WS 长连接(_start_ws_receiver):内网环境无需公网回调地址,lark ws 客户端
     独立线程运行,事件经 run_coroutine_threadsafe 桥接主事件循环;
     含 _patch_ws_card_support —— 补齐 lark-oapi(≤1.7.3)对 CARD 帧直接丢弃、
     不回帧的 SDK 缺口(否则群内点按钮报 200341/200671 系交互错误)。
  ② HTTP 接收器(create_feishu_receiver):FastAPI /feishu/event,
     处理 URL 验证(challenge)/v2 卡片回调/文本消息事件(公网回调模式备选)。

出站推送不走本模块 —— 见 feishu.bot(通知消费主循环)。
"""

import asyncio
import json
import time
import logging
from concurrent.futures import TimeoutError as FuturesTimeoutError

import config

from .protocol import shared_bot
from .conversation import _handle_feishu_message, _send_welcome
from .actions import handle_card_action

logger = logging.getLogger("zops")


def _patch_ws_card_support():
    """补齐 lark-oapi(≤1.7.3) ws 客户端的卡片回调缺口。

    SDK 的 _handle_data_frame 对 MessageType.CARD 直接 return 不回帧，
    飞书收不到响应 → 群内点按钮报 200341/200671 系卡片交互错误。
    此 patch 让 CARD 帧与 EVENT 帧同路径分发（dispatcher 的
    _do_without_validation 已支持 p2.card.action.trigger 回调表），
    并把 P2CardActionTriggerResponse 序列化回帧（toast 即时反馈）。"""
    import base64 as _b64
    import http as _http
    import time as _time
    import lark_oapi.ws.client as _ws_mod
    from lark_oapi.ws.enum import MessageType
    from lark_oapi.ws.const import (HEADER_MESSAGE_ID, HEADER_TRACE_ID, HEADER_SUM,
                                    HEADER_SEQ, HEADER_TYPE, HEADER_BIZ_RT)
    from lark_oapi.ws.model import Response
    from lark_oapi.core.const import UTF_8
    from lark_oapi.core.json import JSON as _JSON

    if getattr(_ws_mod.Client._handle_data_frame, "_zops_card_patched", False):
        return

    async def _handle_data_frame(self, frame):
        try:
            hs = frame.headers
            msg_id = _ws_mod._get_by_key(hs, HEADER_MESSAGE_ID)
            trace_id = _ws_mod._get_by_key(hs, HEADER_TRACE_ID)
            sum_ = _ws_mod._get_by_key(hs, HEADER_SUM)
            seq = _ws_mod._get_by_key(hs, HEADER_SEQ)
            type_ = _ws_mod._get_by_key(hs, HEADER_TYPE)

            pl = frame.payload
            if int(sum_) > 1:
                pl = self._combine(msg_id, int(sum_), int(seq), pl)
                if pl is None:
                    return

            message_type = MessageType(type_)
            # CARD（卡片回调）与 EVENT 同路径分发——SDK 原版在此直接丢弃 CARD 帧
            if message_type not in (MessageType.EVENT, MessageType.CARD):
                return
            resp = Response(code=_http.HTTPStatus.OK)
            try:
                start = int(round(_time.time() * 1000))
                result = self._event_handler._do_without_validation(pl)
                end = int(round(_time.time() * 1000))
                header = hs.add()
                header.key = HEADER_BIZ_RT
                header.value = str(end - start)
                if result is not None:
                    resp.data = _b64.b64encode(_JSON.marshal(result).encode(UTF_8))
            except Exception as e:
                logger.error(f"ws CARD/EVENT 帧分发异常 (trace={trace_id}): {e}")
                resp = Response(code=_http.HTTPStatus.INTERNAL_SERVER_ERROR)
            frame.payload = _JSON.marshal(resp).encode(UTF_8)
            await self._write_message(frame.SerializeToString())
        except Exception as e:
            logger.error(f"ws 数据帧处理异常: {e}")

    _handle_data_frame._zops_card_patched = True
    _ws_mod.Client._handle_data_frame = _handle_data_frame
    logger.info("lark ws 客户端 CARD 帧支持已补齐 (monkey-patch)")


def _start_ws_receiver(app_id: str, app_secret: str, loop):
    """飞书长连接模式事件接收（内网环境无需公网回调地址）：
    独立线程运行 lark ws 客户端，事件通过 run_coroutine_threadsafe 桥接到主事件循环。
    消息处理与 HTTP 回调共用同一条 _handle_feishu_message 链路（含对话回复闭环）。"""
    import lark_oapi as lark
    _patch_ws_card_support()

    def _on_message(data) -> None:
        try:
            ev = data.event
            if ev is None or ev.message is None:
                return
            msg = ev.message
            if msg.message_type != "text":
                return
            # 发送者身份 (问答归户): 事件原生带 sender.open_id, 入口不取后续链路就丢
            _sid = ev.sender
            _sender_id = ""
            if _sid is not None and getattr(_sid, "sender_type", "") != "app":
                _srid = getattr(_sid, "sender_id", None)
                _sender_id = (getattr(_srid, "open_id", "") or "") if _srid is not None else ""
            content = json.loads(msg.content or "{}")
            text = (content.get("text") or "").strip()
            if not text:
                return
            logger.info(f"飞书长连接事件已接收 (chat={msg.chat_id[:12] if msg.chat_id else ''}): {text[:50]}")
            # 桥接到主事件循环后台处理（飞书要求 3 秒内响应，LLM 对话不可阻塞 ws 线程）
            # P0: 透传 chat_type (group/p2p) 供受众判定 — 群输出不含发言者私有内容
            asyncio.run_coroutine_threadsafe(
                _handle_feishu_message(text, msg.message_id or "", msg.chat_id or "",
                                       sender_id=_sender_id,
                                       chat_type=getattr(msg, "chat_type", "") or ""),
                loop)
        except Exception as e:
            logger.error(f"飞书长连接事件处理异常: {e}")

    def _on_bot_added(data) -> None:
        try:
            ev = data.event
            if ev is None or not ev.chat_id:
                return
            logger.info(f"机器人已加入群聊: {ev.chat_id[:12]}")
            asyncio.run_coroutine_threadsafe(_send_welcome(ev.chat_id), loop)
        except Exception as e:
            logger.error(f"入群欢迎处理异常: {e}")

    def _on_card_action(data):
        """卡片按钮回调（card.action.trigger）：同步返回 toast 响应。
        注意：飞书要求 3 秒内响应；模型构造只接受 dict（kwargs 会 TypeError）——
        这两者都曾在真机上导致 200671 系交互错误。"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse)

        def _resp(ttype: str, text: str):
            return P2CardActionTriggerResponse({"toast": {
                "type": ttype if ttype in ("success", "error") else "success",
                "content": text or "已处理"}})

        try:
            ev = data.event
            if ev is None or ev.action is None:
                return P2CardActionTriggerResponse()
            value = ev.action.value or {}
            operator = ({"open_id": getattr(ev.operator, "open_id", "") or "",
                         "user_id": getattr(ev.operator, "user_id", "") or ""}
                        if ev.operator else {})
            logger.info(f"飞书卡片按钮回调: value={json.dumps(value, ensure_ascii=False)[:120]}")
            # 桥接主事件循环执行（决策写 Redis 毫秒级）；飞书限 3s，超时提示到面板确认
            fut = asyncio.run_coroutine_threadsafe(
                handle_card_action(value, operator), loop)
            ttype, text = fut.result(timeout=2.5)
            return _resp(ttype, text)
        except FuturesTimeoutError:
            logger.warning("卡片回调处理超 2.5s，已回提示（决策可能已写入，重复点击有幂等提示）")
            return _resp("error", "处理超时，请稍后在审批面板确认结果")
        except Exception as e:
            logger.error(f"飞书卡片按钮回调处理异常: {e}")
            return _resp("error", "处理异常，请稍后重试或到管理面板操作")

    while True:
        try:
            handler = lark.EventDispatcherHandler.builder("", "") \
                .register_p2_im_message_receive_v1(_on_message) \
                .register_p2_im_chat_member_bot_added_v1(_on_bot_added) \
                .register_p2_card_action_trigger(_on_card_action) \
                .build()
            cli = lark.ws.Client(app_id, app_secret,
                                 event_handler=handler,
                                 log_level=lark.LogLevel.INFO)
            cli.start()  # 阻塞：连接建立后持续接收事件
        except Exception as e:
            logger.error(f"飞书长连接客户端异常退出: {e}，10 秒后重试")
            time.sleep(10)


async def create_feishu_receiver():
    """
    创建飞书事件接收服务 (应用机器人交互, HTTP 公网回调备选路径)
    服务常驻启动：APP 凭据可在前端管理页配置后即时生效（无需重启）
    """
    from fastapi import FastAPI, Request

    feishu_app = FastAPI(title="Z-OPS Feishu Receiver")

    async def _verify(body: dict) -> bool:
        """事件订阅请求校验：配置了 VERIFICATION_TOKEN 时要求请求体 token 匹配
        （v1 事件 token 在顶层，v2 事件/卡片回调 token 在 header 内）。"""
        token = config.FEISHU_VERIFICATION_TOKEN
        try:
            _dyn = await shared_bot()._dyn_cfg()
            token = _dyn.get("feishu_verification_token") or token
        except Exception:
            pass
        if not token:
            return True
        _tok = body.get("token") or (body.get("header") or {}).get("token")
        return _tok == token

    @feishu_app.post("/feishu/event")
    async def feishu_event(request: Request):
        body = await request.json()

        # 请求校验（明文事件订阅模式，body.token 为校验 token）
        if not await _verify(body):
            logger.warning("飞书事件回调 token 校验失败，已拒绝")
            return {"code": 1, "msg": "invalid token"}

        # URL 验证
        if "challenge" in body:
            return {"challenge": body["challenge"]}

        # v2 卡片按钮回调 (card.action.trigger)：与长连接模式共用 handle_card_action
        if (body.get("header") or {}).get("event_type") == "card.action.trigger":
            ev = body.get("event") or {}
            action = ev.get("action") or {}
            try:
                ttype, text = await handle_card_action(action.get("value") or {},
                                                       ev.get("operator") or {})
            except Exception as e:
                logger.error(f"飞书卡片回调处理异常: {e}")
                ttype, text = "error", "处理异常，请稍后重试或到管理面板操作"
            return {"toast": {"type": ttype if ttype in ("success", "error") else "success",
                              "content": text or "已处理"}}

        # 消息事件
        event = body.get("event", {})
        message = event.get("message", {})
        msg_type = message.get("message_type", "")
        message_id = message.get("message_id", "")
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "")   # P0: group/p2p 受众判定
        # 发送者身份 (问答归户): 事件原生带 sender.sender_id.open_id
        _snd = event.get("sender") or {}
        sender_id = ""
        if _snd.get("sender_type", "") != "app":
            sender_id = ((_snd.get("sender_id") or {}).get("open_id")) or ""
        if msg_type == "text":
            try:
                content = json.loads(message.get("content") or "{}")
            except Exception:
                content = {}
            text = (content.get("text") or "").strip()
            if text:
                logger.info(f"飞书消息事件已接收 (chat={chat_id[:12] if chat_id else ''}): {text[:50]}")
                # 后台任务处理（飞书回调要求 3 秒内响应，LLM 对话耗时长不可阻塞）
                asyncio.create_task(
                    _handle_feishu_message(text, message_id, chat_id,
                                           sender_id=sender_id, chat_type=chat_type))

        return {"code": 0}

    return feishu_app
