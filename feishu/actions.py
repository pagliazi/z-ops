"""
Z-OPS 飞书通道 — 卡片按钮回调分发(card.action.trigger 统一入口)

按钮 value 为结构化意图(不靠文本猜语义),长连接(WS)与 HTTP 接收器
(gateway)两条路径共用本入口,返回 (toast_type, toast_text) 供飞书客户端
即时反馈:

  {"type": "approval", "id": apr_xxx, "op": approve|reject}
      → 写回审批单(core.approval.decide_approval, approver=feishu:open_id),
        仅流程留痕 (审批只走流程, 系统不自动执行 destructive 操作)
  {"type": "nms_silence", "channel_id": ch_xxx, "hours": 1}
      → 通道写 silent_until,nms_agent 下轮分发跳过(告警降噪)
"""

import json
import time
import logging
from datetime import datetime

import config

from .protocol import shared_bot

logger = logging.getLogger("zops")


async def handle_card_action(value: dict, operator: dict):
    """卡片按钮回调统一处理。返回 (toast_type, toast_text)。"""
    _v = value or {}
    _t = str(_v.get("type") or "")
    _who = ((operator or {}).get("open_id")
            or (operator or {}).get("user_id") or "feishu_user")

    if _t == "approval":
        try:
            from core.approval import get_approval, decide_approval
        except Exception as e:
            logger.error(f"审批模块加载失败: {e}")
            return "error", "审批服务不可用，请到管理面板处理"
        aid = str(_v.get("id") or "")
        prev = get_approval(aid)
        if prev is None:
            return "error", "审批单不存在或已过期(24h)"
        if prev.get("status") != "pending":
            _st = {"approved": "已批准", "rejected": "已拒绝",
                   "timeout": "已超时"}.get(prev.get("status"), prev.get("status"))
            return "success", f"该审批单此前已处理（{_st}），无需重复操作"
        _op = "approve" if _v.get("op") in ("approve", "approved") else "reject"
        rec = decide_approval(aid, _op, approver=f"feishu:{_who}")
        if rec is None:
            return "error", "决策写入失败，请到管理面板处理"
        logger.info(f"卡片按钮审批决策: {aid} → {_op} (by {_who})")
        return ("success", "✅ 已批准（流程留痕），系统不会自动执行该操作" if _op == "approve"
                else "⛔ 已拒绝（流程留痕），已通知发起方")

    if _t == "nms_silence":
        ch_id = str(_v.get("channel_id") or "")
        try:
            hours = max(0.1, min(float(_v.get("hours") or 1), 24))
        except (TypeError, ValueError):
            hours = 1
        try:
            bot = shared_bot()
            r = await bot.get_redis()
            raw = await r.hget(config.KEY_NMS_CHANNELS, ch_id)
            if not raw:
                return "error", "通道不存在，可能已被删除"
            ch = json.loads(raw)
            ch["silent_until"] = time.time() + hours * 3600
            await r.hset(config.KEY_NMS_CHANNELS, ch_id,
                         json.dumps(ch, ensure_ascii=False))
            _until = datetime.fromtimestamp(ch["silent_until"]).strftime("%m-%d %H:%M")
            logger.info(f"[NMS] 通道「{ch.get('name')}」被 {_who} 静默至 {_until}")
            return "success", f"🔇 通道「{ch.get('name')}」已静默至 {_until}"
        except Exception as e:
            logger.error(f"通道静默写入失败: {e}")
            return "error", "静默写入失败，请稍后重试"

    return "error", f"未知操作类型: {_t or '-'}"
