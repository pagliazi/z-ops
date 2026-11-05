"""
Z-OPS 飞书通道 — 对话域

群消息处理链路(_handle_feishu_message,由 gateway 的 WS/HTTP 接收器共用):
  ⓪ 帮助菜单 → 帮助卡片
  ① NMS 正则命令(监控域 _handle_nms_command)→ 秒回
  ①.2 自然语言 NMS 查询(_nms_nl_query)→ 秒回
  ①.5 告警类自然语言问句 → 实时状态摘要秒回
  ② 普通对话 → orchestrator(订阅 reply_channel 等回复,进度实时回送,
     回复写会话历史支持多轮追问)

会话基础设施:
  - _anchor_session: 推送/审批锚定群会话(assistant 历史条目,群内追问带上下文)
  - _save_session_msg: 对话历史写入会话引擎(与 webchat 同格式)
"""

import asyncio
import json
import re
import time
import logging

import httpx
import redis.asyncio as aioredis

import config

from .protocol import shared_bot

logger = logging.getLogger("zops")


class ConversationDomain:
    """对话域 mixin(组合进 feishu.bot.FeishuBot)"""

    async def _anchor_session(self, chat_id: str, content: str, source: str = "zops_push"):
        """写入群会话锚点（assistant 历史条目，与对话链路同格式）：
        推送/审批发生时留痕，群内后续追问经 orchestrator 路由时自带上下文。"""
        if not chat_id or not content:
            return
        try:
            r = await self.get_redis()
            sid = f"feishu:{chat_id}"
            key = config.key_session_msgs(sid)
            await r.rpush(key, json.dumps({
                "role": "assistant", "content": content[:600],
                "timestamp": time.time(), "session_id": sid, "source": source,
            }, ensure_ascii=False))
            if config.SESSION_MSG_MAX > 0:
                await r.ltrim(key, -config.SESSION_MSG_MAX, -1)
            if config.SESSION_TTL > 0:
                await r.expire(key, config.SESSION_TTL)
        except Exception as e:
            logger.debug(f"会话锚点写入失败: {e}")

    @staticmethod
    def _help_card() -> dict:
        """帮助菜单卡片（lark_md：粗体/列表/换行，不用标题与表格语法）"""
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🤖 Z-OPS 机器人使用指南"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content":
                    "**💬 免记指令，直接说人话**\n"
                    "- `有哪些分组`：列出全部重点分组（带编号）\n"
                    "- `3` / `截图 3`：看第 3 组明细 / 截第 3 组告警图\n"
                    "- `截个 OPSCORE_use 的图`：直接申请该分组告警截图\n"
                    "- `OPSCORE_use 组有什么告警` / `OPSCORE_use 怎么了`：分组告警明细\n"
                    "- `当前有哪些告警`：秒回 NMS 实时告警状态"}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content":
                    "**💬 直接对话**（智能路由到各运维助手，支持追问上下文）\n"
                    "- `XX故障如何排查`：知识库检索回答\n"
                    "- `GID 12345 有哪些备份`：查真实资源数据\n"
                    "- `介绍一下你的能力`：通用问答"}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content":
                    "**📟 NMS 高级命令**（可选，人话已覆盖常用场景）\n"
                    "- `nms` / `nms 状态`：告警状态摘要\n"
                    "- `nms 最新 [N]`：最近 N 条告警（默认 8，最多 15）\n"
                    "- `nms 详情「关键词」`：按节点/摘要搜索告警明细\n"
                    "- `nms 分组「组名」`：某分组告警明细\n"
                    "- `nms 截图「组名」`：申请该分组告警截图\n"
                    "- `nms 分析`：即时生成告警分析摘要\n"
                    "- `nms 推送 开/关`：开关推送\n"
                    "- `nms 推送间隔「分钟」`：设最小推送间隔\n"
                    "- `nms 静默「小时」`：临时静默推送\n"
                    "- `nms 订阅「组...」` / `nms 退订`：本群只收指定组告警（同步写入管理页「飞书推送通道」）\n"
                    "- `nms 推送分组「组,...」`：默认兜底推送的分组过滤"}},
                {"tag": "note", "elements": [{"tag": "plain_text",
                    "content": "群里 @机器人 直接发消息即可 | 输入“帮助”随时查看本菜单"}]},
            ],
        }


# 文本版帮助菜单（卡片降级时使用）
_HELP_TEXT = (
    "【Z-OPS 机器人使用指南】\n"
    "\n【免记指令，直接说人话】\n"
    "- 「有哪些分组」列出全部重点分组（带编号）\n"
    "- 「3」/「截图 3」看第 3 组明细 / 截第 3 组告警图\n"
    "- 「截个 OPSCORE_use 的图」申请该分组告警截图\n"
    "- 「OPSCORE_use 组有什么告警」/「OPSCORE_use 怎么了」分组告警明细\n"
    "- 「当前有哪些告警」秒回 NMS 实时告警状态\n"
    "\n【直接对话】智能路由到各运维助手，支持追问上下文\n"
    "- 「XX故障如何排查」知识库检索回答\n"
    "- 「GID 12345 有哪些备份」查真实资源数据\n"
    "- 「介绍一下你的能力」通用问答\n"
    "\n【NMS 高级命令（可选）】\n"
    "- nms / nms 状态：告警状态摘要\n"
    "- nms 最新 [N]：最近 N 条告警（默认 8，最多 15）\n"
    "- nms 详情 <关键词>：按节点/摘要搜索告警明细\n"
    "- nms 分组 <组名>：某分组告警明细\n"
    "- nms 截图 <组名>：申请该分组告警截图\n"
    "- nms 分析：手动触发 LLM 深度分析（即时执行并推送，定时采集本身不调 LLM）\n"
    "- nms 推送 开/关：开关推送\n"
    "- nms 推送间隔 <分钟>：设最小推送间隔\n"
    "- nms 静默 <小时>：临时静默推送\n"
    "- nms 订阅 <组...> / nms 退订：本群只收指定组告警\n"
    "- nms 推送分组 <组,...>：全局推送过滤\n"
    "\n群里 @机器人 直接发消息即可 | 输入「帮助」随时查看本菜单"
)


async def _auto_bind_feishu_user(sender_id: str) -> tuple:
    """通讯录自动绑定 (需飞书应用开通 contact:user.base:readonly 等权限):
    open_id → 用户详情 → 工号候选 (employee_no 优先, email 前缀 op\d+ 兕底)
    → 匹配 zops:users 真实账号 → 写映射, 返回 (username, display_name)。
    失败/无匹配/无权限 → 写负缓存 5 分钟 (防止权限未开通时高频无效调用)，
    返回 ("", "")，上层自动回落手工绑定引导。"""
    if not sender_id or not getattr(config, "FEISHU_AUTO_BIND", True):
        return "", ""
    _bot = shared_bot()
    try:
        r = await _bot.get_redis()
        if await r.exists(f"{config.REDIS_PREFIX}:feishu_bind_miss:{sender_id}"):
            return "", ""
    except Exception:
        pass
    try:
        token = await _bot._tenant_token()
        if not token:
            return "", ""
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/contact/v3/users/{sender_id}"
                "?user_id_type=open_id",
                headers={"Authorization": f"Bearer {token}"})
            data = resp.json()
        if data.get("code") != 0:
            logger.debug(f"feishu 通讯录自动绑定未命中 (code={data.get('code')}): {(data.get('msg') or '')[:60]}")
            raise LookupError(data.get("code"))
        u = ((data.get("data") or {}).get("user") or {})
        email = (u.get("email") or "").strip()
        emp_no = str(u.get("employee_no") or "").strip()
        # 工号候选优先级: 通讯录工号字段 → 邮箱前缀 (svc-opscore@xx → svc-opscore)
        candidates = [emp_no] if emp_no else []
        m = re.match(r"(op\d{3,6})", email, re.I)
        if m:
            candidates.append(m.group(1).lower())
        try:
            r = await _bot.get_redis()
            for cand in candidates:
                if not cand:
                    continue
                raw = await r.hget(config.KEY_USERS, cand) or ""
                if raw:
                    await r.hset(config.KEY_FEISHU_USER_MAP, sender_id, cand)
                    try:
                        display = (json.loads(raw).get("display_name") or "").strip()
                    except Exception:
                        display = ""
                    logger.info(f"feishu 通讯录自动绑定: {sender_id[:10]}… -> {cand}")
                    return cand, display
        except Exception as e:
            logger.debug(f"feishu 自动绑定写映射失败: {e}")
    except Exception:
        pass
    # 负缓存: 5 分钟内不再重试 (权限未开通/不在应用可见范围/无匹配均走此路径)
    try:
        r = await _bot.get_redis()
        await r.set(f"{config.REDIS_PREFIX}:feishu_bind_miss:{sender_id}", "1", ex=300)
    except Exception:
        pass
    return "", ""


async def _resolve_feishu_user(sender_id: str) -> tuple:
    """open_id → (系统用户名, display_name, 是否本次自动绑定)。查 zops:feishu_user_map 映射表 +
    zops:users 取展示名；映射未命中时尝试通讯录自动绑定；未绑定或映射失效返回 ("", "", False)。
    问答归户的身份源。"""
    if not sender_id:
        return "", "", False
    try:
        r = await shared_bot().get_redis()
        username = await r.hget(config.KEY_FEISHU_USER_MAP, sender_id) or ""
        if not username:
            # 映射未命中 → 通讯录自动绑定 (需开放平台权限, 未开通时负缓存回落手工引导)
            auto_u, auto_d = await _auto_bind_feishu_user(sender_id)
            return auto_u, auto_d, bool(auto_u)
        raw = await r.hget(config.KEY_USERS, username) or ""
        try:
            info = json.loads(raw) if raw else {}
        except Exception:
            info = {}
        # 账号已不存在则视为映射失效，清掉避免永久匿名占位
        if raw and info:
            return username, (info.get("display_name") or ""), False
        await r.hdel(config.KEY_FEISHU_USER_MAP, sender_id)
        return "", "", False
    except Exception as e:
        logger.debug(f"feishu 身份解析失败: {e}")
        return "", "", False


_BINDING_GUIDE = (
    "🔐 **首次使用需绑定账号**\n\n"
    "检测到您尚未绑定系统账号。请直接回复您的工号（如 `svc-opscore`）完成绑定，"
    "绑定后即可正常提问，问答记录将归入您的账号。\n\n"
    "如需换号：发送「重新绑定」")


async def _handle_binding(text: str, message_id: str, chat_id: str,
                          sender_id: str, bound: str) -> bool:
    """绑定交互处理（返回 True = 消息已被绑定流程消费，不再走对话链路）：
    - 「重新绑定」→ 清除映射，引导输入新工号（仅已绑定者可用）
    - 纯工号 (op\d{3,6}) → 校验系统账号存在后写入映射（首次/换号）
    绑定必须存在于 zops:users（绑定到系统中的真实用户，不接受任意字符串）"""
    if not sender_id:
        return False
    _bot = shared_bot()
    t = text.strip()
    # 重新绑定：清映射回到引导态
    if t in ("重新绑定", "换绑", "解绑"):
        if not bound:
            return False
        try:
            r = await _bot.get_redis()
            await r.hdel(config.KEY_FEISHU_USER_MAP, sender_id)
        except Exception as e:
            logger.debug(f"feishu 解绑失败: {e}")
        await _bot._reply_im(message_id, "已解除绑定。请回复新工号（如 `svc-opscore`）重新绑定。", chat_id)
        return True
    # 工号绑定：仅当文本就是工号格式才拦截（不误伤普通提问）
    if not re.fullmatch(r"op\d{3,6}", t):
        return False
    try:
        r = await _bot.get_redis()
        raw = await r.hget(config.KEY_USERS, t) or ""
    except Exception as e:
        logger.debug(f"feishu 绑定校验失败: {e}")
        raw = ""
    if not raw:
        await _bot._reply_im(
            message_id, f"⚠️ 系统中未找到账号 `{t}`，请确认工号后重新输入。", chat_id)
        return True
    try:
        display = (json.loads(raw).get("display_name") or "").strip()
    except Exception:
        display = ""
    try:
        await r.hset(config.KEY_FEISHU_USER_MAP, sender_id, t)
    except Exception as e:
        logger.warning(f"feishu 绑定写入失败: {e}")
    who = f"{t}（{display}）" if display else t
    await _bot._reply_im(
        message_id, f"✅ 绑定成功：**{who}**。现在直接提问即可，问答记录将归入该账号。", chat_id)
    logger.info(f"feishu 用户已绑定: {sender_id[:10]}… -> {t}")
    return True


async def _handle_feishu_message(text: str, message_id: str, chat_id: str,
                                 sender_id: str = "", chat_type: str = ""):
    """后台处理飞书文本消息：NMS 命令直接回复；普通对话转发 orchestrator，
    订阅 reply_channel 等待回复后回送飞书群（对话回复链路，保证有反馈）。
    多轮对话历史写入会话引擎（与 webchat 同格式），支持追问上下文。
    sender_id: 发送者 open_id，用于问答归户（首次对话自动引导绑定系统账号）。
    chat_type: group/p2p — P0 受众判定: 群聊 audience=feishu:group:{chat_id}，
    群历史/缓存/输出只含组共同可见内容，禁止注入发言者私有记忆或私有查询结果；
    chat_type 缺失时按群处理（fail-closed: 宁可少注入私有内容）。"""
    _bot = shared_bot()
    # P0 受众标记: 群聊 → 群受众; p2p → 私有 (仅发言者); 未知 → 按群 (fail-closed)
    is_group = str(chat_type or "").strip().lower() != "p2p"
    audience = f"feishu:group:{chat_id}" if (is_group and chat_id) else ""
    # 统一清理群聊 @提及（@_user_1 等内部格式），后续所有判断用干净文本
    text = re.sub(r"@_?[A-Za-z0-9_-]+[\s，,。]?", "", text or "").strip()
    if not text:
        return

    # ⓪ 帮助菜单：回复能力卡片（飞书交互卡片，支持按钮/列表）
    _low = text.lower().strip("？?")
    if _low in ("帮助", "help", "功能", "菜单", "命令", "你会什么", "你能做什么",
                "能做什么", "使用说明") or _low.startswith("帮助"):
        try:
            await _bot._reply_card(message_id, _bot._help_card(), chat_id,
                                   fallback_text=_HELP_TEXT)
            logger.info(f"飞书帮助菜单已回复: {text[:50]}")
        except Exception as e:
            logger.error(f"飞书帮助菜单回复异常: {e}")
        return

    # ① NMS 告警 bot 命令：直接处理并回复
    try:
        reply_text = await _bot._handle_nms_command(text, message_id, chat_id)
    except Exception as e:
        logger.error(f"飞书 NMS 命令处理异常: {e}")
        reply_text = ""
    if reply_text:
        # 命令回复用 lark_md 卡片渲染（markdown 清洗后粗体/列表正常显示）
        await _bot._reply_md(message_id, reply_text, chat_id, title="📟 NMS 告警")
        logger.info(f"飞书 NMS 命令已回复: {text[:50]}")
        return

    # ①.2 自然语言 NMS 查询（免记指令）：「截个 OPSCORE_use 的图」「OPSCORE_use 组有什么告警」
    #      命中真实分组名 + 告警/截图语境词才执行，秒回；否则返回空串交给下游
    try:
        nl_text = await _bot._nms_nl_query(text, message_id, chat_id)
    except Exception as e:
        logger.error(f"飞书自然语言 NMS 查询异常: {e}")
        nl_text = ""
    if nl_text:
        await _bot._reply_md(message_id, nl_text, chat_id, title="📟 NMS 告警")
        logger.info(f"飞书自然语言 NMS 已回复: {text[:50]}")
        return

    # ①.5 告警类自然语言问题（如“当前有哪些告警”）→ 实时告警状态摘要，秒回不绕知识库
    if "告警" in text and re.search(
            r"哪些|多少|几[条个]?|状态|情况|列表|数量|现在|当前|有没有|最新|怎么样", text):
        try:
            status_text = await _bot._handle_nms_command("nms 状态", message_id, chat_id)
        except Exception as e:
            logger.error(f"飞书告警状态查询异常: {e}")
            status_text = ""
        if status_text:
            await _bot._reply_md(message_id, status_text, chat_id, title="📟 NMS 告警")
            logger.info(f"飞书告警状态已回复: {text[:50]}")
            return

    # ①.6 问答归户门: 发送者身份解析 (sender 可得时启用)。
    # 解析顺序: 已有映射 → 通讯录自动绑定 (需开放平台权限) → 手工绑定引导。
    # 秒回类 NMS 命令不拦（本地查询不产生执行记录）；只有走 orchestrator
    # 的对话（产生日志/LLM 用量的路径）要求先绑定，未绑定自动引导。
    username, _display, _auto_bound = await _resolve_feishu_user(sender_id)
    if sender_id and username and _auto_bound:
        # 通讯录自动绑定首次成功: 告知本人并留纠正入口 (仅此一条, 后续直接命中映射)
        # 不拦截——用户本轮问题照常进入对话链路回答
        _who = f"{username}（{_display}）" if _display else username
        try:
            await _bot._reply_im(
                message_id,
                f"✅ 已通过通讯录自动绑定账号 **{_who}**，问答记录将归入该账号。"
                "如非本人或需换号：发送「重新绑定」", chat_id)
        except Exception:
            pass
    if sender_id and not username:
        if await _handle_binding(text, message_id, chat_id, sender_id, bound=""):
            return
        await _bot._reply_im(message_id, _BINDING_GUIDE, chat_id)
        logger.info(f"feishu 未绑定用户已引导绑定 (sender={sender_id[:10]}…)")
        return
    # 已绑定用户的换绑命令 (工号文本不拦，避免误伤讨论工号的普通提问)
    if sender_id and username and text.strip() in ("重新绑定", "换绑", "解绑"):
        if await _handle_binding(text, message_id, chat_id, sender_id, bound=username):
            return

    # ② 普通对话 → orchestrator（请求-响应，回复通过 reply_channel(msg_id) 回传）
    try:
        from agents.base import AgentMessage, AgentReply
        # 多轮对话上下文：同一飞书群/单聊共享会话，orchestrator 路由与 agent 回答可见历史
        session_id = f"feishu:{chat_id or message_id}"
        await _save_session_msg(session_id, "user", text, username=username)
        msg = AgentMessage(
            source="feishu",
            target="orchestrator",
            text=text,
            channel_source="feishu",
            session_id=session_id,
            username=username,
        )
        # P0: 受众随消息传播 (RequestContext.audience / 子任务继承 / wiki 检索过滤)
        msg.provenance = {**(msg.provenance or {}), "audience": audience}

        # ═══ P1: 飞书入口可靠任务登记 (TR_FEISHU_ENABLED, 默认开) ═══
        # message_id 天然幂等键 (重复回调不重复建任务); task 事件 JSONL 提供
        # 永久归档 —— 闭合“飞书仅写 Redis 截断 1000/4000、无 transcript 归档”缺口。
        # 登记失败/关闭时降级为纯对话 (可用性优先, 不阻断回复)。
        _tr = {"runtime": None, "task_id": "", "owner": "", "fencing": 0}
        if getattr(config, "TR_FEISHU_ENABLED", True):
            try:
                from core.task_runtime import get_task_runtime
                _rt = get_task_runtime()
                _tid, _created, _fenc, _own = await _rt.submit_for_chat(
                    actor=username or "", session_id=session_id,
                    client_key=message_id, message=text, target="orchestrator",
                    meta={"msg_id": msg.msg_id, "chat_id": chat_id,
                          "chat_type": chat_type or "", "audience": audience},
                    channel="feishu")
                _tr = {"runtime": _rt, "task_id": _tid, "owner": _own,
                       "fencing": _fenc}
                if _created:
                    await _rt.ledger.link_msg(_tid, msg.msg_id)
            except Exception as tr_e:
                logger.warning(f"飞书任务登记失败 (降级非持久, 不阻断): {tr_e}")

        async def _task_emit(ev_type: str, data: dict):
            """P1: 记录任务事件 (失租/未登记时静默降级)"""
            if not _tr["task_id"]:
                return
            try:
                await _tr["runtime"].emit(_tr["task_id"], _tr["owner"],
                                          _tr["fencing"], ev_type, data)
            except Exception as e:
                logger.debug(f"飞书任务事件失败 {_tr['task_id']}/{ev_type}: {e}")

        async def _task_complete(terminal: str, result: dict):
            """P1: 终态提交 (原子: 终态+事件+Outbox+归档确认); 失租被拒返回 None"""
            if not _tr["task_id"]:
                return None
            try:
                return await _tr["runtime"].complete(
                    _tr["task_id"], _tr["owner"], _tr["fencing"],
                    terminal, result)
            except Exception as e:
                logger.warning(f"飞书任务终态提交异常 {_tr['task_id']}: {e}")
                return None

        r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
        reply_ch = config.reply_channel(msg.msg_id)
        pubsub = r.pubsub()
        await pubsub.subscribe(reply_ch)
        # 先订阅激活再发布 (旧顺序发布早于订阅就绪, 快速回复会丢 reply 频道的存量竞态修复)
        await r.publish(config.channel("orchestrator"), msg.to_json())
        logger.info(f"飞书消息已转发 orchestrator: {text[:50]}")
        await _task_emit("dispatched", {"target": "orchestrator"})
        _last_hb = time.time()
        try:
            deadline = time.time() + 180
            progress_sent = 0
            _seen_progress = set()
            _route_acked = False
            while True:
                remaining = deadline - time.time()
                # P1: lease 心跳 (12s < 默认 15s); 内联节流, 失租后不再自动重试
                # (旧协调者禁止提交终态, 由 reclaim_loop 兜底闭合 UNKNOWN)
                if _tr["task_id"] and time.time() - _last_hb >= 12:
                    _last_hb = time.time()
                    await _tr["runtime"].heartbeat(
                        _tr["task_id"], _tr["owner"], _tr["fencing"])
                if remaining <= 0:
                    logger.warning(f"飞书对话等待回复超时: {text[:50]}")
                    await _bot._reply_im(message_id, "⚠️ 处理超时，请稍后再试。", chat_id)
                    await _task_complete("failed", {"error": "feishu_reply_timeout_180s"})
                    break
                # 轮询取消息：get_message 超时返回 None，保证 deadline 始终可触发
                raw_msg = await pubsub.get_message(timeout=min(remaining, 5.0))
                if raw_msg is None or raw_msg["type"] != "message":
                    continue
                try:
                    data = json.loads(raw_msg["data"])
                except Exception:
                    continue
                # 进度消息与最终回复共用 reply 频道：进度实时回送飞书（上限 3 条防刷屏，
                # 相同内容去重；orchestrator 的“正在分析/正在路由决策”过程性进度合并为一条）。
                # 必须跳过进度消息——否则被 AgentReply.from_json 解析成空回复导致提前退出
                if isinstance(data, dict) and data.get("_type") == "progress":
                    pt = (data.get("text") or "").strip()
                    if pt:
                        _route_noise = ("正在分析您的问题", "正在进行智能路由")
                        if any(k in pt for k in _route_noise):
                            if _route_acked:
                                continue
                            pt = "正在智能路由…"
                            _route_acked = True
                        if pt in _seen_progress:
                            continue
                        _seen_progress.add(pt)
                        await _task_emit("progress", {"text": pt[:500]})
                        if progress_sent < 3:
                            progress_sent += 1
                            # 进度文本自身可能已含 ⏳（如 kb_agent 生成等待提示），避免双图标
                            prefix = "" if pt.startswith("⏳") else "⏳ "
                            await _bot._reply_im(message_id, f"{prefix}{pt[:200]}", chat_id)
                            logger.info(f"飞书对话进度: {pt[:50]}")
                    continue
                try:
                    reply = AgentReply.from_json(raw_msg["data"])
                except Exception:
                    continue
                res = reply.result or {}
                answer = ""
                if isinstance(res, dict):
                    answer = res.get("text") or res.get("answer") or ""
                elif isinstance(res, str):
                    answer = res
                if not answer:
                    answer = reply.error or ""
                if not answer:
                    continue  # 空回复，继续等待真正的 AgentReply
                # 对话回复用 lark_md 卡片渲染（markdown 清洗后粗体/列表/链接正常显示）
                await _bot._reply_md(message_id, str(answer)[:4000], chat_id)
                logger.info(f"飞书对话已回复: {str(answer)[:50]}")
                # 回复写入会话历史（供下轮追问上下文）
                await _save_session_msg(session_id, "assistant", str(answer)[:1000],
                                        source=(reply.source or "agent"))
                # P1: 事件全量回复 + 终态 (complete 内含归档确认) → 发送成功即交付确认
                await _task_emit("reply", {
                    "text": str(answer)[:2000], "source": reply.source or "",
                    "truncated_in_feishu_card": len(str(answer)) > 4000})
                _dlv = await _task_complete("succeeded", {
                    "text": str(answer)[:2000], "source": reply.source or ""})
                if _dlv:
                    try:
                        await _tr["runtime"].ledger.mark_delivery(
                            _tr["task_id"], _dlv, "delivered")
                    except Exception:
                        pass
                break
        finally:
            await pubsub.unsubscribe(reply_ch)
            await r.aclose()
    except Exception as e:
        logger.error(f"飞书对话转发异常: {e}")


async def _save_session_msg(session_id: str, role: str, content: str,
                            source: str = "", username: str = ""):
    """把飞书对话写入会话引擎历史（与 webchat 同格式），
    供 orchestrator 路由决策与 agent 多轮回答的上下文使用。"""
    try:
        r = await shared_bot().get_redis()
        entry = {"role": role, "content": (content or "")[:1000],
                 "timestamp": time.time(), "session_id": session_id}
        if source:
            entry["source"] = source
        if username:
            entry["username"] = username
        key = config.key_session_msgs(session_id)
        await r.rpush(key, json.dumps(entry, ensure_ascii=False))
        if config.SESSION_MSG_MAX > 0:
            await r.ltrim(key, -config.SESSION_MSG_MAX, -1)
        if config.SESSION_TTL > 0:
            await r.expire(key, config.SESSION_TTL)
    except Exception as e:
        logger.warning(f"会话历史写入失败: {e}")


async def _send_welcome(chat_id: str):
    """机器人被拉入群后，主动发送能力介绍卡片（帮助菜单复用）。"""
    _bot = shared_bot()
    token = await _bot._tenant_token()
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages"
                "?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": chat_id, "msg_type": "interactive",
                      "content": json.dumps(_bot._help_card(), ensure_ascii=False)})
            data = resp.json()
            if data.get("code") == 0:
                logger.info(f"入群欢迎卡片已发送: {chat_id[:12]}")
            else:
                logger.warning(f"入群欢迎发送失败: {data.get('code')} {data.get('msg')}")
    except Exception as e:
        logger.error(f"入群欢迎发送异常: {e}")
