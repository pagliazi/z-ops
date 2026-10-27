"""
Z-OPS 飞书通道 — 监控域(NMS 告警)

  - _build_nms_card: 告警富卡片(级别统计/事件列表/截图内嵌/控制台+静默按钮)
  - _handle_nms_command: 群命令(状态/最新/详情/分组/截图/分析/推送控制/订阅)
  - _nms_nl_query: 自然语言免记指令(「截个 GCOC_use 的图」「3」编号查询)
  - _get_sub_channel/_upsert_sub_channel: 群订阅命令 ↔ 管理页多源通道表
    (Redis KEY_NMS_CHANNELS)的双向读写,保留级别过滤等服务端字段。

数据源:NMS 告警快照(Redis zops:nms_alarms:latest,由 nms_agent 周期抓取写入);
截图命令入队 NMS_CMD_KEY 并唤醒 nms_agent(约 15 分钟一轮)。
"""

import json
import re
import time
import uuid
import logging
from datetime import datetime
from typing import Dict

import config

logger = logging.getLogger("zops")


class NmsDomain:
    """监控域 mixin(组合进 feishu.bot.FeishuBot)"""

    _NMS_SEV_META = [
        ("critical", "🔴", "严重"), ("major", "🟠", "重要"),
        ("minor", "🟡", "次要"), ("warning", "🔵", "警告"),
        ("indeterminate", "⚪", "未知"),
    ]

    async def _build_nms_card(self, extra: dict):
        """构造 NMS 告警结构化卡片 payload（飞书 interactive）。
        截图上传飞书内嵌（image_key），解决内网域名公网不可达问题。"""
        elements = []

        # 标题行：时间 + 新增/活跃统计
        sev = extra.get("severity") or {}
        sev_txt = "　".join(
            f"{emoji} {cn} {sev.get(k, 0)}" for k, emoji, cn in self._NMS_SEV_META
            if sev.get(k)
        ) or "无"
        groups_label = "、".join(extra.get("push_groups") or [])
        head_md = (f"**时间**: {extra.get('time', '')}　"
                   f"新增 **{extra.get('new', 0)}** 条　活跃 **{extra.get('total', 0)}** 条\n"
                   f"{sev_txt}")
        if groups_label:
            head_md += f"\n**推送分组**: {groups_label}"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": head_md}})

        # 事件列表（incident 模式优先，否则逐条新增）
        incidents = extra.get("incidents") or []
        new_alarms = extra.get("new_alarms") or []
        if incidents:
            elements.append({"tag": "hr"})
            for i in incidents[:6]:
                s = i.get("severity", "")
                emoji = next((e for k, e, _ in self._NMS_SEV_META if k == s), "⚪")
                cn = next((c for k, _, c in self._NMS_SEV_META if k == s), s)
                cat = f"·{i.get('category')}" if i.get("category") else ""
                md = (f"{emoji} **[{cn}{cat}] {i.get('title', '')}**\n"
                      f"　成员 {i.get('members', 0)} 条 / 来源 {i.get('sources', 0)} 个 "
                      f"/ 未确认 {i.get('unacked', 0)} / 本轮新增 {i.get('new', 0)}")
                if i.get("last_time"):
                    md += f" | 最后: {i['last_time']}"
                elements.append({"tag": "div", "text": {"tag": "lark_md", "content": md}})
            if len(incidents) > 6:
                elements.append({"tag": "div", "text": {
                    "tag": "lark_md",
                    "content": f"　… 另有 {len(incidents) - 6} 个事件"}})
        elif new_alarms:
            elements.append({"tag": "hr"})
            for a in new_alarms[:8]:
                s = a.get("severity", "")
                emoji = next((e for k, e, _ in self._NMS_SEV_META if k == s), "⚪")
                cn = next((c for k, _, c in self._NMS_SEV_META if k == s), s)
                md = (f"{emoji} **[{cn}] [{a.get('group', '-')}] {a.get('summary', '')}**\n"
                      f"　来源: {a.get('source', '-')}")
                if a.get("last_time"):
                    md += f" | 最后: {a['last_time']}"
                elements.append({"tag": "div", "text": {"tag": "lark_md", "content": md}})
            if len(new_alarms) > 8:
                elements.append({"tag": "div", "text": {
                    "tag": "lark_md",
                    "content": f"　… 另有 {len(new_alarms) - 8} 条"}})

        # 截图：整组图上传飞书内嵌，分级图给链接
        shots = extra.get("shots") or []
        if shots:
            elements.append({"tag": "hr"})
            elements.append({"tag": "div", "text": {
                "tag": "lark_md", "content": "**分组告警截图**:"}})
            _shot_dir = config.BASE_DIR / "webchat_frontend" / "nms_shots"
            for s in shots:
                _label = s.get("group") or "视图"
                _sev_cn = next((c for k, _, c in self._NMS_SEV_META
                                if k == s.get("severity")), "")
                if _sev_cn:
                    _label = f"{_label}·{_sev_cn}告警"
                if s.get("unack"):
                    _label += "（未确认）"
                fname = s.get("file") or ""
                img_key = ""
                if fname:
                    img_key = await self._upload_image(str(_shot_dir / fname))
                if img_key:
                    elements.append({"tag": "img", "img_key": img_key, "alt": {
                        "tag": "plain_text", "content": _label}})
                elif s.get("url"):
                    elements.append({"tag": "div", "text": {
                        "tag": "lark_md",
                        "content": f"[{_label}]({s['url']})"}})

        # 预案命中/重点分组/分析摘要
        checklists = extra.get("checklists") or []
        top_groups = extra.get("top_groups") or []
        meta_md = ""
        if top_groups:
            meta_md += "**重点分组**: " + "、".join(
                f"{g['name']} {g['count']}" for g in top_groups[:5])
        if checklists:
            meta_md += ("\n" if meta_md else "") + "**预案命中**: " + "、".join(
                f"{c['name']} {c['count']}" for c in checklists[:6])
        analysis = (extra.get("analysis") or "").strip()
        if analysis:
            _lark = self._nms_md_to_lark(analysis)
            meta_md += ("\n" if meta_md else "") + f"**分析摘要**: {_lark[:500]}"
        if meta_md:
            elements.append({"tag": "hr"})
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": meta_md}})

        # 控制台/静默按钮（静默按钮仅应用群卡片：Webhook 自定义机器人卡片
        # 的按钮回调不会到达应用，加了也无效）
        _actions = []
        console_url = extra.get("console_url") or ""
        if console_url:
            _actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看 NMS 控制台"},
                "type": "primary",
                "url": console_url,
            })
        _ch_ctx = extra.get("channel") or {}
        if _ch_ctx.get("id") and _ch_ctx.get("type") != "webhook":
            _actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔇 静默 1 小时"},
                "type": "default",
                "value": {"type": "nms_silence",
                          "channel_id": _ch_ctx.get("id"), "hours": 1},
            })
        if _actions:
            elements.append({"tag": "action", "actions": _actions})

        elements.append({"tag": "note", "elements": [{
            "tag": "plain_text",
            "content": f"Z-OPS NMS 监控 | {extra.get('time', '')} | 默认仅未确认告警"
        }]})

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "🔔 NMS 告警监控"},
                    "template": "red",
                },
                "elements": elements,
            },
        }

    async def _nms_load_dyn(self, r) -> Dict:
        """读取 NMS 动态配置（Redis JSON），返回 dict。"""
        raw = await r.get(config.KEY_NMS_DYN_CFG)
        try:
            dyn = json.loads(raw) if raw else {}
        except Exception:
            dyn = {}
        return dyn if isinstance(dyn, dict) else {}

    async def _get_sub_channel(self, r, chat_id: str):
        """按 chat_id 查找本群的 app 型推送通道，找不到返回 None。"""
        if not chat_id:
            return None
        try:
            raw_all = await r.hgetall(config.KEY_NMS_CHANNELS)
        except Exception:
            return None
        for raw in (raw_all or {}).values():
            try:
                ch = json.loads(raw)
            except Exception:
                continue
            if isinstance(ch, dict) and ch.get("type") == "app" \
                    and ch.get("chat_id") == chat_id:
                return ch
        return None

    async def _upsert_sub_channel(self, r, chat_id: str, groups: list):
        """群命令订阅：upsert 本群的 app 型推送通道（保留级别过滤等既有配置）。"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        ch = await self._get_sub_channel(r, chat_id)
        if ch:
            ch["groups"] = groups
            ch["enabled"] = True
            ch["updated_at"] = now
        else:
            ch = {
                "id": f"ch_{uuid.uuid4().hex[:8]}",
                "name": f"群订阅 {chat_id[:12]}", "type": "app",
                "webhook_url": "", "chat_id": chat_id,
                "groups": groups, "severities": [],
                "enabled": True, "is_default": False,
                "created_at": now, "updated_at": now,
            }
        await r.hset(config.KEY_NMS_CHANNELS, ch["id"],
                     json.dumps(ch, ensure_ascii=False))

    async def _handle_nms_command(self, text: str, message_id: str = "",
                                  chat_id: str = "") -> str:
        """识别并处理 NMS 告警 bot 命令，返回回复文本；非命令返回空串。"""
        t = (text or "").strip()
        # 去掉 @提及（@_user_1 等）
        t = re.sub(r"@_?[A-Za-z0-9_-]+", "", t).strip()
        # 命令黏连规范化: nms截图GCOC_use → nms 截图 GCOC_use；nms状态 → nms 状态
        # （动词后紧跟参数时插空格，兼容无空格输入习惯；“推送间隔”放最前避免被“推送”吞掉）
        t = re.sub(r"^(/?)nms(推送间隔|截图|状态|分组|详情|分析|最新|静默|订阅|退订|帮助|推送)(?=[^\s，。])",
                   r"\1nms \2 ", t, flags=re.I)
        low = t.lower()
        if not (low.startswith("nms") or low.startswith("告警") or
                low.startswith("/nms")):
            return ""
        r = await self.get_redis()

        # ── 推送频率控制命令（无需告警快照，直接改动态配置，nms_agent 约 30 秒内生效） ──
        # nms 推送间隔 <分钟>：最小推送间隔，0=取消限制
        m = re.search(r"(?:nms\s+)?推送间隔\s*(\d+)", t, re.I)
        if m:
            mins = max(0, int(m.group(1)))
            dyn_cfg = await self._nms_load_dyn(r)
            dyn_cfg["push_min_interval"] = mins
            await r.set(config.KEY_NMS_DYN_CFG, json.dumps(dyn_cfg, ensure_ascii=False))
            if mins <= 0:
                return "✅ 推送间隔限制已取消：每轮抓取有新增告警时都会推送。"
            return (f"✅ 推送最小间隔已设为 **{mins} 分钟**：距上次推送不足该间隔将跳过。\n"
                    "约 30 秒内生效；`nms 推送间隔 0` 可恢复每轮都推。")
        # nms 静默 <小时>：临时静默，到期自动恢复
        m = re.search(r"(?:nms\s+)?静默\s*(\d+(?:\.\d+)?)", t, re.I)
        if m:
            hours = float(m.group(1))
            until = time.time() + hours * 3600
            dyn_cfg = await self._nms_load_dyn(r)
            dyn_cfg["push_silent_until"] = until
            await r.set(config.KEY_NMS_DYN_CFG, json.dumps(dyn_cfg, ensure_ascii=False))
            until_str = datetime.fromtimestamp(until).strftime("%m-%d %H:%M")
            return (f"🔕 飞书告警推送已静默 **{hours} 小时**（约至 {until_str} 前不再推送）。\n"
                    "`nms 推送 开` 可提前恢复。")
        # nms 推送 [开|关]：开关推送；不带参数则查询当前状态
        m = re.search(r"(?:nms\s+)?推送\s*(开|开启|关|关闭|on|off)?\s*$", t, re.I)
        if m:
            kw = (m.group(1) or "").lower()
            dyn_cfg = await self._nms_load_dyn(r)
            if kw in ("开", "开启", "on"):
                dyn_cfg["push_enabled"] = True
                dyn_cfg.pop("push_silent_until", None)  # 提前结束静默
                await r.set(config.KEY_NMS_DYN_CFG, json.dumps(dyn_cfg, ensure_ascii=False))
                return "✅ 飞书告警推送已开启（约 30 秒内生效）。"
            if kw in ("关", "关闭", "off"):
                dyn_cfg["push_enabled"] = False
                await r.set(config.KEY_NMS_DYN_CFG, json.dumps(dyn_cfg, ensure_ascii=False))
                return "🔴 飞书告警推送已关闭（约 30 秒内生效）。`nms 推送 开` 可恢复。"
            # 状态查询
            push_enabled = dyn_cfg.get("push_enabled", config.NMS_FEISHU_PUSH_ENABLED)
            min_interval = dyn_cfg.get("push_min_interval", config.NMS_PUSH_MIN_INTERVAL)
            silent_until = dyn_cfg.get("push_silent_until")
            lines = [
                "**NMS 飞书推送状态**",
                f"推送开关: {'🟢 开启' if push_enabled else '🔴 关闭'}",
                f"最小推送间隔: {int(min_interval) if isinstance(min_interval, (int, float)) and min_interval > 0 else 0} 分钟"
                + ("" if (isinstance(min_interval, (int, float)) and min_interval > 0) else "（每轮有新增都推）"),
            ]
            if isinstance(silent_until, (int, float)) and time.time() < silent_until:
                left = max(1, int((silent_until - time.time()) // 60))
                lines.append(f"静默中: 剩余约 {left} 分钟（`nms 推送 开` 提前恢复）")
            else:
                lines.append("静默: 无")
            last_raw = await r.get(config.KEY_NMS_LAST_PUSH_TS)
            if last_raw:
                try:
                    last_str = datetime.fromtimestamp(float(last_raw)).strftime("%m-%d %H:%M")
                    lines.append(f"上次推送: {last_str}")
                except Exception:
                    pass
            lines.append("命令: `nms 推送 开/关`、`nms 推送间隔 <分钟>`、`nms 静默 <小时>`")
            return "\n".join(lines)

        # ── 推送分组命令：nms 推送分组 [组,组...|全部]（默认 webhook 推送只含这些组的告警） ──
        m = re.search(r"(?:nms\s+)?推送分组\s*([^\s，。]*)", t, re.I)
        if m:
            arg = (m.group(1) or "").strip()
            dyn_cfg = await self._nms_load_dyn(r)
            if not arg or arg.lower() in ("全部", "all", "状态"):
                cur = dyn_cfg.get("push_groups") or ""
                return (f"当前推送分组: {'、'.join(g.strip() for g in cur.split(',') if g.strip()) or '全部（未过滤）'}\n"
                        "设置: `nms 推送分组 GCOC_use,GCOC_core`；`nms 推送分组 全部` 恢复全量。")
            dyn_cfg["push_groups"] = arg.replace("，", ",")
            await r.set(config.KEY_NMS_DYN_CFG, json.dumps(dyn_cfg, ensure_ascii=False))
            return f"✅ 推送分组已设为 **{arg}**（仅推这些组的告警，约 30 秒内生效）。"

        # ── 订阅命令：nms 订阅 <组...> / nms 退订 [全部] / nms 订阅列表 ──
        # 订阅后本群只收这些组的告警（写入多源推送通道表，按群分发）
        m = re.search(r"(?:nms\s+)?订阅\s*(.*)$", t, re.I)
        if m:
            arg = (m.group(1) or "").strip()
            if arg and not any(k in arg for k in ("列表", "状态")):
                groups = [g.strip() for g in re.split(r"[\s,，]+", arg) if g.strip()]
                if not groups:
                    return "用法: `nms 订阅 GCOC_use GCOC_core`（空格分隔多组）"
                if not chat_id:
                    return ("⚠️ 当前消息未携带群 chat_id（需应用机器人模式），"
                            "请把机器人拉入群后 @机器人 操作，或在管理页「飞书推送通道」配置。")
                await self._upsert_sub_channel(r, chat_id, groups)
                return (f"✅ 本群已订阅分组: **{'、'.join(groups)}**\n"
                        "此后本群只收这些组的告警推送；`nms 退订` 取消订阅恢复全量。")
            # 查询当前订阅
            ch = await self._get_sub_channel(r, chat_id)
            if ch:
                _gs = '、'.join(ch.get("groups") or [])
                return (f"本群当前订阅: **{_gs or '全部'}**（通道「{ch.get('name')}」）\n"
                        "`nms 退订` 取消订阅。")
            return ("本群未订阅特定分组（默认接收全量推送）。\n"
                    "`nms 订阅 <组名>...` 只收关注组的告警。")
        m = re.search(r"(?:nms\s+)?退订\s*(.*)$", t, re.I)
        if m:
            if not chat_id:
                return "⚠️ 当前消息未携带群 chat_id，无法退订。"
            ch = await self._get_sub_channel(r, chat_id)
            if ch and ch.get("id"):
                await r.hdel(config.KEY_NMS_CHANNELS, ch["id"])
            await r.hdel(config.KEY_NMS_SUBS, chat_id)  # 同步清理旧订阅表
            return "✅ 已取消本群订阅，恢复默认全量推送。"

        latest_raw = await r.get(f"{config.REDIS_PREFIX}:nms_alarms:latest")
        if not latest_raw:
            return "NMS 告警快照暂不可用，请稍后再试（抓取中）。"
        latest = json.loads(latest_raw)
        alarms = latest.get("alarms") or []
        ts = latest.get("timestamp") or ""

        def _sev(a):
            return a.get("severity", "unknown")

        _EMOJI = {"critical": "🔴", "major": "🟠", "minor": "🟡",
                  "warning": "🔵", "indeterminate": "⚪"}
        _CN = {"critical": "严重", "major": "重要", "minor": "次要",
               "warning": "警告", "indeterminate": "未知"}

        # ── 最新告警命令: nms 最新 [N] ──
        m = re.search(r"(?:nms\s+)?(?:最新|新增)\s*(\d{0,2})?\s*$", t, re.I)
        if m:
            n = min(int(m.group(1) or 8), 15)

            def _sort_key(a):
                # last_time 格式 "4/9/25, 3:37 PM"
                for f in ("last_time", "first_time"):
                    try:
                        return datetime.strptime(a.get(f) or "", "%m/%d/%y, %I:%M %p")
                    except Exception:
                        continue
                return datetime.min

            latest_n = sorted(alarms, key=_sort_key, reverse=True)[:n]
            lines = [f"**最新 {len(latest_n)} 条告警**（按最后发生时间排序）"]
            for a in latest_n:
                s = _sev(a)
                node = a.get("node_alias") or a.get("node") or "-"
                summary = (a.get("summary") or "")[:60].replace("\n", " ")
                lines.append(f"{_EMOJI.get(s, '⚪')} {summary}\n"
                             f"　节点 {node} · {a.get('last_time') or a.get('first_time') or '-'}")
            lines.append(f"快照: {ts}")
            return "\n".join(lines)

        # ── 告警详情搜索: nms 详情 <关键词> ──
        m = re.search(r"(?:nms\s+)?详情\s+(.+)", t, re.I)
        if m:
            kw = m.group(1).strip()
            kw_l = kw.lower()
            hits = [a for a in alarms
                    if kw_l in (a.get("summary") or "").lower()
                    or kw_l in (a.get("node") or "").lower()
                    or kw_l in (a.get("node_alias") or "").lower()
                    or kw_l in (a.get("source") or "").lower()
                    or kw_l in (a.get("alarm_type") or "").lower()]
            if not hits:
                return f"未找到与「{kw}」相关的告警。"
            _sev_rank = {"critical": 0, "major": 1, "minor": 2,
                         "warning": 3, "indeterminate": 4}
            hits.sort(key=lambda a: _sev_rank.get(_sev(a), 5))
            lines = [f"**「{kw}」匹配 {len(hits)} 条告警**（按级别排序，展示前 10）"]
            for a in hits[:10]:
                s = _sev(a)
                node = a.get("node_alias") or a.get("node") or a.get("source") or "-"
                grp = a.get("group") or "未分组"
                _sum = (a.get("summary") or "(无摘要)")[:70].replace("\n", " ")
                lines.append(f"{_EMOJI.get(s, '⚪')} {_sum}\n"
                             f"　节点 {node} · 分组 {grp} · Serial {a.get('serial') or '-'}"
                             f" · {a.get('last_time') or '-'}")
            lines.append(f"快照: {ts}")
            return "\n".join(lines)

        # ── 即时分析命令: nms 分析 → 入队由 nms_agent 下一轮强制执行 ──
        if re.search(r"(?:nms\s+)?分析\s*$", t, re.I):
            await r.rpush(config.NMS_CMD_KEY,
                          json.dumps({"cmd": "analyze", "ts": time.time()},
                                     ensure_ascii=False))
            await r.publish(config.NMS_WAKE_CHANNEL, "wake")  # 唤醒 nms_agent 立即开下一轮
            return ("✅ 已受理分析请求：采集进程已被唤醒，即将生成告警分析摘要并推送。\n"
                    "提示：一轮抓取含浏览器采集，预计 15 分钟内完成。")

        # 截图命令（倒装形式: nms GCOC_use 截图）
        m = re.search(r"^(?:nms\s+)?([^\s，。]{2,})\s+截图\s*$", t, re.I)
        if m:
            group = m.group(1)
            await r.rpush(config.NMS_CMD_KEY,
                          json.dumps({"cmd": "snapshot", "group": group,
                                      "ts": time.time()}, ensure_ascii=False))
            await r.publish(config.NMS_WAKE_CHANNEL, "wake")  # 唤醒 nms_agent 立即开下一轮
            return f"✅ 已受理截图请求：分组「{group}」。采集进程已被唤醒，预计 15 分钟内推送截图。"

        # 截图命令（无参数）→ 用法提示
        if re.search(r"(?:nms\s+)?截图\s*$", t, re.I):
            return ("📸 截图命令用法: `nms 截图「分组名」`\n"
                    "例如: `nms 截图 GCOC_use`（或倒装写法 `nms GCOC_use 截图`）\n"
                    "可先 `nms 分组 GCOC_use` 查看分组告警明细。")

        # 截图命令（正序: nms 截图 GCOC_use）
        m = re.search(r"(?:nms\s+)?截图\s+([^\s，。]+)", t, re.I)
        if m:
            group = m.group(1)
            await r.rpush(config.NMS_CMD_KEY,
                          json.dumps({"cmd": "snapshot", "group": group,
                                      "ts": time.time()}, ensure_ascii=False))
            await r.publish(config.NMS_WAKE_CHANNEL, "wake")  # 唤醒 nms_agent 立即开下一轮
            return f"✅ 已受理截图请求：分组「{group}」。采集进程已被唤醒，预计 15 分钟内推送截图。"

        # 分组明细命令: nms 分组 <name> / nms <分组名>
        m = re.search(r"(?:分组)\s+([^\s，。]+)", t, re.I)
        group_arg = m.group(1) if m else None
        if not group_arg:
            # "nms xxx" 的 xxx 视为分组名（若非状态关键词）
            rest = re.sub(r"^(?:nms|/nms)\s*", "", t, flags=re.I).strip()
            if rest and not any(k in rest for k in ("状态", "摘要", "报告", "统计",
                                                    "帮助", "help", "最新", "详情", "分析")):
                group_arg = rest
        if group_arg:
            grp_alarms = [a for a in alarms if group_arg in
                          (a.get("groups") or ([a.get("group")] if a.get("group") else []))]
            if not grp_alarms:
                return f"分组「{group_arg}」当前无活跃告警（或分组名不匹配）。"
            active = [a for a in grp_alarms if _sev(a) in ("critical", "major", "minor", "warning", "indeterminate")]
            sev_cnt: Dict[str, int] = {}
            for a in active:
                s = _sev(a)
                sev_cnt[s] = sev_cnt.get(s, 0) + 1
            sev_txt = "　".join(f"{_EMOJI.get(s, '⚪')} {_CN.get(s, s)} {n}"
                                for s, n in sorted(sev_cnt.items(),
                                                   key=lambda x: ["critical", "major", "minor", "warning", "indeterminate"].index(x[0])))
            lines = [
                f"**NMS 分组「{group_arg}」告警状态**",
                f"活跃告警: {len(active)} 条（总计 {len(grp_alarms)} 条）",
                f"级别分布: {sev_txt or '无'}",
            ]
            top = sorted(active, key=lambda a: (["critical", "major", "minor", "warning", "indeterminate"].index(_sev(a)), -(len(a.get('summary') or ''))))[:6]
            for a in top:
                cl = f"〔{a.get('checklist')}〕" if a.get("checklist") else ""
                lines.append(f"{_EMOJI.get(_sev(a), '⚪')} {cl} {(a.get('summary') or '')[:70]}")
            if ts:
                lines.append(f"快照: {ts}")
            return "\n".join(lines)

        # 状态命令: nms / nms 状态
        sev_cnt = {}
        active = [a for a in alarms if _sev(a) in ("critical", "major", "minor", "warning", "indeterminate")]
        for a in active:
            s = _sev(a)
            sev_cnt[s] = sev_cnt.get(s, 0) + 1
        sev_txt = "　".join(f"{_EMOJI.get(s, '⚪')} {_CN.get(s, s)} {n}"
                            for s, n in sorted(sev_cnt.items(),
                                               key=lambda x: ["critical", "major", "minor", "warning", "indeterminate"].index(x[0])))
        lines = [
            "**NMS 告警状态摘要**",
            f"活跃告警: {len(active)} 条（总计 {len(alarms)} 条）",
            f"级别分布: {sev_txt or '无'}",
        ]
        from collections import Counter
        gcount = Counter()
        for a in active:
            for g in (a.get("groups") or ([a.get("group")] if a.get("group") else ["未分组"])):
                gcount[g] += 1
        # 关注组置顶（默认组+订阅组）→ 其余业务组 → 排除系统视图组，避免业务组被淹没
        _sys = set(config.NMS_SYSTEM_VIEW_GROUPS)
        _pinned = list(config.NMS_DEFAULT_GROUPS)
        try:
            _chans_raw = await r.hgetall(config.KEY_NMS_CHANNELS)
            for _raw in (_chans_raw or {}).values():
                try:
                    _ch = json.loads(_raw)
                except Exception:
                    continue
                for _g in (_ch.get("groups") or []):
                    if _g and _g not in _pinned:
                        _pinned.append(_g)
        except Exception:
            pass
        _pinned_hit = [(g, gcount[g]) for g in _pinned if gcount.get(g)]
        _pinned_hit.sort(key=lambda x: -x[1])
        _biz = [(g, n) for g, n in gcount.most_common()
                if g not in _sys and g not in _pinned]
        lines.append("重点分组: " + "、".join(
            f"{g} {n}" for g, n in (_pinned_hit + _biz)[:5] or gcount.most_common(5)))
        lines.append("回复「有哪些分组」获取编号清单，回复编号即可看明细/截图")
        clcount = Counter(a.get("checklist") for a in active if a.get("checklist"))
        if clcount:
            lines.append("预案命中: " + "、".join(f"{t} {n}" for t, n in clcount.most_common(6)))
        # 最新告警示例：「有哪些告警」应给出具体条目，而不只是统计数字
        try:
            def _sort_key(a):
                for f in ("last_time", "first_time"):
                    try:
                        return datetime.strptime(a.get(f) or "", "%m/%d/%y, %I:%M %p")
                    except Exception:
                        continue
                return datetime.min

            _latest = sorted(active, key=_sort_key, reverse=True)[:3]
            lines.append("最新告警:")
            for a in _latest:
                s = _sev(a)
                node = a.get("node_alias") or a.get("node") or "-"
                summary = (a.get("summary") or "")[:50].replace("\n", " ")
                lines.append(f"{_EMOJI.get(s, '⚪')} {summary} · {node} · "
                             f"{a.get('last_time') or a.get('first_time') or '-'}")
        except Exception:
            pass
        if ts:
            lines.append(f"快照: {ts}")
        return "\n".join(lines)

    async def _nms_group_list(self, chat_id: str = "") -> str:
        """NMS 重点分组清单（活跃告警数降序 top 15，编号展示）。
        清单缓存到 Redis（每群一份，TTL 10 分钟），供后续编号查询「3」/「截图 3」。"""
        r = await self.get_redis()
        try:
            latest_raw = await r.get(f"{config.REDIS_PREFIX}:nms_alarms:latest")
            if not latest_raw:
                return "NMS 告警快照暂不可用，请稍后再试（抓取中）。"
            latest = json.loads(latest_raw)
            alarms = latest.get("alarms") or []
        except Exception:
            return "NMS 告警快照暂不可用，请稍后再试。"
        _ACTIVE = ("critical", "major", "minor", "warning", "indeterminate")
        gcount: Dict[str, int] = {}
        for a in alarms:
            if a.get("severity") not in _ACTIVE:
                continue
            for g in (a.get("groups") or ([a.get("group")] if a.get("group") else [])):
                if g:
                    gcount[g] = gcount.get(g, 0) + 1
        # 置顶用户关注组：默认组 + 各推送通道订阅组（告警数少也不被淹没）
        _pinned = list(config.NMS_DEFAULT_GROUPS)
        try:
            _chans_raw = await r.hgetall(config.KEY_NMS_CHANNELS)
            for _raw in (_chans_raw or {}).values():
                try:
                    _ch = json.loads(_raw)
                except Exception:
                    continue
                for _g in (_ch.get("groups") or []):
                    if _g and _g not in _pinned:
                        _pinned.append(_g)
        except Exception:
            pass
        _sys = set(config.NMS_SYSTEM_VIEW_GROUPS)
        _pinned_cnt = {g: gcount.get(g, 0) for g in _pinned}
        _pinned_s = sorted(((g, _pinned_cnt[g]) for g in _pinned if _pinned_cnt[g] > 0),
                           key=lambda x: -x[1])
        _biz = sorted(((g, n) for g, n in gcount.items()
                       if g not in _sys and g not in _pinned_cnt), key=lambda x: -x[1])
        _sysv = sorted(((g, n) for g, n in gcount.items() if g in _sys),
                       key=lambda x: -x[1])
        top = (_pinned_s + _biz + _sysv)[:15]
        if not top:
            return "暂无活跃告警分组。"
        # 缓存清单供编号查询（key 含 chat_id，不同群互不干扰）
        if chat_id:
            await r.set(f"{config.REDIS_PREFIX}:nms_groups:{chat_id}",
                        json.dumps([g for g, _ in top], ensure_ascii=False), ex=600)
        lines = [f"**NMS 分组清单**（业务组优先，共展示 {len(top)} 个）"]
        for i, (g, n) in enumerate(top, 1):
            lines.append(f"{i}. {g}（{n} 条）")
        lines.append("回复编号看该组明细（如「3」）；「截图 3」截第 3 组告警图。")
        return "\n".join(lines)

    async def _nms_group_by_no(self, no: int, chat_id: str, shot: bool = False) -> str:
        """按清单编号查询分组：no=编号，shot=True 时受理截图。"""
        r = await self.get_redis()
        try:
            raw = await r.get(f"{config.REDIS_PREFIX}:nms_groups:{chat_id}")
            groups = json.loads(raw) if raw else []
        except Exception:
            groups = []
        if not groups:
            return "请先问「有哪些分组」获取分组清单，再回复编号。"
        if not (1 <= no <= len(groups)):
            return f"编号超出范围（1-{len(groups)}），请重新选择。"
        g = groups[no - 1]
        if shot:
            return await self._handle_nms_command(f"nms 截图 {g}", "", chat_id)
        return await self._handle_nms_command(f"nms {g}", "", chat_id)

    async def _nms_nl_query(self, text: str, message_id: str = "",
                            chat_id: str = "") -> str:
        """自然语言 NMS 查询（免记指令）：
        - 「截个 GCOC_use 的图」/「给 GCOC_use 截张告警图」→ 受理截图
        - 「GCOC_use 组有什么告警」/「GCOC_use 怎么了」→ 分组告警明细
        从最新告警快照收集真实分组名做包含匹配（长度降序，避免 GCOC 先于 GCOC_use 命中），
        命中且含告警/截图语境词才执行，否则返回空串（交给下游路由）。"""
        t = (text or "").strip()
        if len(t) < 1 or t.lower().startswith(("nms", "告警状态")):
            return ""
        # ── 分组清单（免记分组名）：「有哪些分组」「分组列表」→ 编号清单 ──
        if re.search(r"分组列表|有哪些分组|哪些分组|什么分组|分组清单|列出分组|看看分组", t):
            return await self._nms_group_list(chat_id)
        # ── 清单编号查询：「3」/「第3个」→ 明细；「截图 3」/「截第3个的图」→ 截图 ──
        m = re.fullmatch(r"(?:第\s*)?(\d{1,2})\s*(?:个|组|号)?", t)
        if m:
            return await self._nms_group_by_no(int(m.group(1)), chat_id)
        # 「截图 3」（图在前）
        m = re.fullmatch(r"截图\s*(?:第\s*)?(\d{1,2})\s*$", t)
        if m:
            return await self._nms_group_by_no(int(m.group(1)), chat_id, shot=True)
        # 「截 3 的图」/「截第3个的图」（图在后）
        m = re.fullmatch(r"截(?:个|张|一下)?\s*(?:第\s*)?(\d{1,2})\s*(?:个|组)?(?:的)?图?", t)
        if m:
            return await self._nms_group_by_no(int(m.group(1)), chat_id, shot=True)
        if len(t) < 3:
            return ""
        # 语境门槛：含“告警/截/抓图”或疑问词才识别，避免误拦截普通对话
        if not re.search(r"告警|截|抓图|怎么|如何|什么|啥|多少|哪些|情况|状态|异常|有问题", t):
            return ""
        r = await self.get_redis()
        try:
            latest_raw = await r.get(f"{config.REDIS_PREFIX}:nms_alarms:latest")
            if not latest_raw:
                return ""
            latest = json.loads(latest_raw)
            alarms = latest.get("alarms") or []
        except Exception:
            return ""
        # 收集真实分组名（长度降序匹配，防 GCOC 先于 GCOC_use 命中）
        groups = set()
        for a in alarms:
            for g in (a.get("groups") or ([a.get("group")] if a.get("group") else [])):
                if g and len(g) >= 3:
                    groups.add(g)
        hit = None
        for g in sorted(groups, key=len, reverse=True):
            if g in t:
                hit = g
                break
        if not hit:
            # 组名未命中：提取疑似组名 token 做前后缀匹配，给出候选提示
            # （如用户说“GCOC”实际组名为“GCOC_use”，避免沉入 LLM 得不到告警信息）
            if re.search(r"告警|截", t):
                cands = set()
                for tok in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", t):
                    for g in groups:
                        if g.lower().startswith(tok.lower()) or tok.lower().startswith(g.lower()):
                            cands.add(g)
                if cands:
                    top = sorted(cands, key=len)[:6]
                    return ("🤔 未识别到准确分组名，您可能是指：\n"
                            + "\n".join(f"- {g}" for g in top)
                            + "\n请回复完整分组名（如「截个 GCOC_use 的图」）。")
            return ""
        # 截图意图: 截个 XX 的图 / 截图 / 抓图 / 告警图（允许“截”与“图”间隔组名）
        if re.search(r"截[图屏]|截(?:个|张|一下)?.{0,14}图|抓图|告警图", t):
            return await self._handle_nms_command(
                f"nms 截图 {hit}", message_id, chat_id)
        # 疑问/查询语境 → 分组明细
        if re.search(r"告警|明细|状态|多少|哪些|情况|活跃|严重|怎么|如何|什么|啥|异常|有问题", t):
            return await self._handle_nms_command(f"nms {hit}", message_id, chat_id)
        return ""
