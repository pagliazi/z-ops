"""
Z-OPS 飞书通道 — 审批域(destructive 操作人在环路, HITL)

审批卡片构建与发送:
  - 按钮值为结构化决策意图({"type":"approval","id":...,"op":approve|reject}),
    不靠文本猜语义;回调统一由 feishu.actions.handle_card_action 处理,
    写回审批单(core.approval.decide_approval)作为流程留痕 (审批只走流程,
    系统不自动执行 destructive 操作)。
  - 发送目标:默认(app 型)通道群 > 任一应用群;无凭据/无通道返回 False,
    由主循环降级 Webhook 纯文本(按钮回调仅应用卡片可达)。
  - 发送成功同步锚定群会话(对话域 _anchor_session,运行时绑定),
    群内追问"为什么被拦/进度"可带上下文。
"""

import json
import logging

import httpx

import config

logger = logging.getLogger("zops")


class ApprovalDomain:
    """审批域 mixin(组合进 feishu.bot.FeishuBot)"""

    def _build_approval_card(self, ap: dict) -> dict:
        """审批交互卡片：按区块渲染实质内容 (单据/对象/申请/影响/CITP/审批流)，
        按钮 value 携带结构化决策意图，回调统一由 handle_card_action 处理。"""
        from core.approval import APPROVAL_APPLY_TYPES, APPROVAL_CATEGORIES
        _aid = ap.get("approval_id", "")
        _cat = APPROVAL_CATEGORIES.get(ap.get("category") or "daily", "日常配置审批")
        _tool = APPROVAL_APPLY_TYPES.get(ap.get("tool_name") or "", ap.get("tool_name") or "-")
        _is_maint = (ap.get("category") == "maintenance")
        _pri = {"urgent": "🔴紧急", "high": "🟠高", "medium": "🟡中", "low": "⚪低"}.get(ap.get("priority") or "", "")

        def sec(title: str, *lines: str) -> dict:
            body = "\n".join(l for l in lines if l)
            return {"tag": "div", "text": {"tag": "lark_md",
                    "content": f"**{title}**\n{body}" if body else f"**{title}**"}}

        els = [
            sec("📋 单据",
                f"操作: **{_tool}**　归属: {ap.get('service_type') or '-'}",
                f"发起人: {ap.get('username') or '-'}　优先级: {_pri or '-'}",
                f"站点: {ap.get('site') or '-'}　Case: {ap.get('case_no') or '-'}",
                f"变更窗口: {ap.get('change_window') or '-'}"
                + (f"　计划: {str(ap.get('planned_ts') or '').replace('T', ' ')}" if ap.get("planned_ts") else "")),
            sec("🏢 对象",
                f"客户: {ap.get('customer') or '-'}"
                + (f"　GID: {ap.get('customer_gid')}" if ap.get("customer_gid") else "")
                + (f"　Order: {ap.get('customer_order_no')}" if ap.get("customer_order_no") else ""),
                *( [f"客户站点: {ap.get('customer_site_ids')}"] if ap.get("customer_site_ids") else [] ),
                f"目标设备: {ap.get('target_host') or '-'}"
                + (f"　关联告警: {ap.get('related_alerts_n')} 条" if ap.get("related_alerts_n") else "")),
            sec("📝 申请",
                f"{ap.get('summary') or '-'}"),
        ]
        if ap.get("impact_scope") or ap.get("rollback_plan") or ap.get("risk_note"):
            els.append(sec("⚠️ 影响与回退",
                           f"影响范围: {ap.get('impact_scope') or '-'}",
                           f"回退方案: {ap.get('rollback_plan') or '-'}",
                           f"风险说明: {ap.get('risk_note') or '-'}"))
        if _is_maint:
            els.append(sec("🔧 CITP",
                           f"故障类型: {ap.get('citp_fault') or '-'}　时间窗: {ap.get('citp_window') or '-'}",
                           f"设备: {ap.get('citp_devs') or ap.get('citp_devices') or '未匹配'}",
                           *( [f"影响: VM {ap.get('citp_vms')} 台 · 客户 {ap.get('citp_custs')} 家"]
                              if ap.get("citp_vms") or ap.get("citp_custs") else [] ),
                           f"编号: {ap.get('citp_no') or '-'}"))
        _lv = " → ".join(x for x in (
            f"DS复核:@{ap.get('approver_ds') or '-'}",
            f"技术:@{ap.get('approver_tech') or '跳过'}",
            f"行政:@{ap.get('approver_admin') or '跳过'}") )
        els.append(sec("✅ 审批流", _lv
                       + (f"\n指定审批人: @{ap.get('approvers')}" if ap.get("approvers") else "")))
        els += [
            {"tag": "hr"},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "✅ 同意"},
                 "type": "primary",
                 "value": {"type": "approval", "id": _aid, "op": "approve"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "⛔ 拒绝"},
                 "type": "danger",
                 "value": {"type": "approval", "id": _aid, "op": "reject"}},
            ]},
            {"tag": "note", "elements": [{"tag": "plain_text",
                "content": f"{_aid} · 审批仅流程留痕, 系统不会自动执行 · 审批中心可查看完整单据/轨迹/CITP"}]},
        ]
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"[{_cat}] {_tool} · {ap.get('site') or '-'}"},
                "template": "orange" if _is_maint else "blue",
            },
            "elements": els,
        }

    async def _send_approval_card(self, ap: dict) -> bool:
        """发送审批卡片：优先默认(app 型)通道群，其次任一应用群；
        成功返回 True；未配置/失败返回 False（降级 Webhook 纯文本）。"""
        token = await self._tenant_token()
        if not token:
            return False
        chat_id = ""
        try:
            r = await self.get_redis()
            for raw in (await r.hgetall(config.KEY_NMS_CHANNELS)).values():
                try:
                    c = json.loads(raw)
                except Exception:
                    continue
                if isinstance(c, dict) and c.get("enabled", True) \
                        and c.get("type") == "app" and c.get("chat_id"):
                    if c.get("is_default"):
                        chat_id = c["chat_id"]
                        break
                    chat_id = chat_id or c["chat_id"]
        except Exception as e:
            logger.warning(f"审批目标通道读取失败: {e}")
        if not chat_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages"
                    "?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"receive_id": chat_id, "msg_type": "interactive",
                          "content": json.dumps(self._build_approval_card(ap),
                                                 ensure_ascii=False)})
                data = resp.json()
            if data.get("code") == 0:
                logger.info(f"审批卡片已发送到 chat {chat_id[:12]}... "
                            f"({ap.get('approval_id')})")
                # 审批请求同步锚定群会话：群内追问“为什么被拦/进度”可带上下文
                await self._anchor_session(chat_id, (
                    f"【审批请求锚点】{ap.get('agent') or '-'} 发起 "
                    f"`{ap.get('tool_name', '-')}`（发起人 {ap.get('username') or '-'}），"
                    f"属 destructive 操作已被拦截（审批仅流程留痕），单号 {ap.get('approval_id')}。"))
                return True
            logger.error(f"审批卡片发送失败: {data.get('code')} {data.get('msg')}")
        except Exception as e:
            logger.error(f"审批卡片发送异常: {e}")
        return False
