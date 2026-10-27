"""
Z-OPS 飞书通道 — 协议原语层(所有域共用)

飞书 API 的无业务语义封装:
  - tenant_access_token 获取/缓存(应用机器人凭据,前端管理页动态配置优先)
  - 图片上传(im/v1/images → image_key,解决内网截图公网不可达)
  - 消息发送:自定义机器人 Webhook(默认/通道直发)/ 应用消息 im/v1/messages
  - 消息回复:reply 接口(文本/卡片),无凭据或失败时逐级降级
  - 文本格式转换:markdown → lark_md(卡片)/ 纯文本(text 降级)
  - 共享 Bot 单例(shared_bot):网关/回调/对话链路复用 Redis 连接

域模块(nms/approval/conversation)以 mixin 形式在组合根 feishu.bot.FeishuBot
上运行时绑定协作(如 _send_webhook 遇 nms_alarm 主题时调用 NMS 域的
_build_nms_card),本层不感知业务语义。
"""

import json
import re
import time
import logging
from typing import Optional

import httpx
import redis.asyncio as aioredis

import config

logger = logging.getLogger("zops")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [feishu] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


class FeishuProtocol:
    """飞书协议原语 mixin(组合进 feishu.bot.FeishuBot)"""

    async def get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(config.REDIS_URL, decode_responses=True)
        return self._redis

    async def _dyn_cfg(self) -> dict:
        """读取前端管理页动态配置（Redis KEY_NMS_DYN_CFG），无则返回 {}。
        优先级：Redis 动态配置 > config.py 环境变量静态默认。"""
        try:
            r = await self.get_redis()
            raw = await r.get(config.KEY_NMS_DYN_CFG)
            cfg = json.loads(raw) if raw else {}
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    async def _tenant_token(self) -> str:
        """获取飞书 tenant_access_token（应用机器人模式），失败返回空串。"""
        app_id = config.FEISHU_APP_ID
        app_secret = config.FEISHU_APP_SECRET
        # 前端管理页动态配置优先
        try:
            _dyn = await self._dyn_cfg()
            app_id = _dyn.get("feishu_app_id") or app_id
            app_secret = _dyn.get("feishu_app_secret") or app_secret
        except Exception:
            pass
        if not app_id or not app_secret:
            return ""
        # 缓存有效则直接复用（飞书 token 有效期 2 小时且有获取频率限制）
        if self._ta_token and time.time() < self._ta_token_expires:
            return self._ta_token
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id,
                          "app_secret": app_secret})
                data = resp.json()
                if data.get("code") == 0 and data.get("tenant_access_token"):
                    self._ta_token = data["tenant_access_token"]
                    expire = int(data.get("expire") or 7200)
                    self._ta_token_expires = time.time() + expire - 300
                    return self._ta_token
                logger.warning(f"获取 tenant_access_token 失败: {data.get('code')} {data.get('msg')}")
        except Exception as e:
            logger.error(f"获取 tenant_access_token 失败: {e}")
        return ""

    async def _upload_image(self, file_path: str) -> str:
        """上传本地图片到飞书（im/v1/images），返回 image_key；失败返回空串。"""
        token = await self._tenant_token()
        if not token:
            return ""
        try:
            with open(file_path, "rb") as f:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        "https://open.feishu.cn/open-apis/im/v1/images",
                        headers={"Authorization": f"Bearer {token}"},
                        data={"image_type": "message"},
                        files={"image": ("shot.png", f, "image/png")})
                data = resp.json()
                if data.get("code") == 0:
                    return data["data"]["image_key"]
                logger.warning(f"飞书图片上传失败: {data.get('code')} {data.get('msg')}")
        except Exception as e:
            logger.warning(f"飞书图片上传异常: {e}")
        return ""

    async def _send_webhook(self, text: str, topic: str = "", source: str = "",
                            extra: dict = None):
        """发送飞书 Webhook 消息(默认 webhook)。extra 存在时按主题渲染富卡片
        （nms_alarm 主题由监控域的 _build_nms_card 构建,运行时绑定）。"""
        webhook_url = config.FEISHU_WEBHOOK_URL
        # 前端管理页动态配置优先
        try:
            _dyn = await self._dyn_cfg()
            if _dyn.get("feishu_webhook_url"):
                webhook_url = _dyn["feishu_webhook_url"]
        except Exception:
            pass
        if not webhook_url or "YOUR_HOOK_ID" in webhook_url:
            logger.warning("飞书 Webhook URL 未配置，跳过推送")
            return

        # NMS 告警：结构化富卡片（级别统计/事件列表/截图内嵌/控制台按钮）
        if topic == "nms_alarm" and extra:
            payload = await self._build_nms_card(extra)
            if not payload:
                return
        else:
            # 构造通用卡片
            title = f"Z-OPS 运维通知"
            if topic:
                title += f" [{topic}]"
            payload = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"content": title, "tag": "plain_text"},
                        "template": self._topic_color(topic)
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {"content": text[:2000], "tag": "lark_md"}
                        },
                        {
                            "tag": "note",
                            "elements": [
                                {"tag": "plain_text", "content": f"来源: {source} | {time.strftime('%Y-%m-%d %H:%M:%S')}"}
                            ]
                        }
                    ]
                }
            }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(webhook_url, json=payload)
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("code") == 0:
                        logger.info(f"飞书推送成功: {text[:50]}...")
                    else:
                        logger.error(f"飞书推送失败: {result}")
                else:
                    logger.error(f"飞书推送 HTTP 错误: {resp.status_code}")
        except Exception as e:
            logger.error(f"飞书推送异常: {e}")

    async def _send_webhook_card(self, url: str, text: str, topic: str,
                                 source: str, extra: dict = None):
        """Webhook 型通道直发：NMS 告警发富卡片（截图优先上传内嵌 image_key，
        无应用凭据时自动降级为链接，由 _build_nms_card 内部处理），其余发通用卡片。"""
        if not url or "YOUR_HOOK_ID" in url:
            logger.warning("通道 Webhook URL 无效，跳过推送")
            return
        if topic == "nms_alarm" and extra:
            payload = await self._build_nms_card(extra)
            if not payload:
                return
        else:
            title = "Z-OPS 运维通知"
            if topic:
                title += f" [{topic}]"
            payload = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"content": title, "tag": "plain_text"},
                        "template": self._topic_color(topic)
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {"content": text[:2000], "tag": "lark_md"}
                        },
                        {
                            "tag": "note",
                            "elements": [
                                {"tag": "plain_text", "content": f"来源: {source} | {time.strftime('%Y-%m-%d %H:%M:%S')}"}
                            ]
                        }
                    ]
                }
            }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("code") == 0:
                        logger.info(f"通道 Webhook 推送成功: {text[:50]}...")
                    else:
                        logger.error(f"通道 Webhook 推送失败: {result}")
                else:
                    logger.error(f"通道 Webhook 推送 HTTP 错误: {resp.status_code}")
        except Exception as e:
            logger.error(f"通道 Webhook 推送异常: {e}")

    async def _send_im_card(self, chat_id: str, topic: str, extra: dict):
        """应用机器人模式：给指定群/用户发送卡片消息（订阅分发,截图内嵌）。"""
        token = await self._tenant_token()
        if not token:
            logger.error("订阅分发失败：无应用凭据 tenant_access_token")
            return
        payload = await self._build_nms_card(extra)
        if not payload:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages"
                    "?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"receive_id": chat_id,
                          "msg_type": "interactive",
                          "content": json.dumps(payload["card"],
                                               ensure_ascii=False)})
                data = resp.json()
                if data.get("code") == 0:
                    logger.info(f"飞书订阅卡片已发送到 chat {chat_id[:12]}...")
                else:
                    logger.error(f"飞书订阅卡片发送失败: {data.get('code')} {data.get('msg')}")
        except Exception as e:
            logger.error(f"飞书订阅卡片发送异常: {e}")

    async def _reply_im(self, message_id: str, text: str, chat_id: str = ""):
        """回复飞书消息：应用机器人模式用 reply 接口，无凭据降级为 webhook 群发。"""
        token = await self._tenant_token()
        if token:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"msg_type": "text",
                              "content": json.dumps({"text": text})})
                    if resp.status_code == 200 and resp.json().get("code") == 0:
                        logger.info(f"飞书回复成功: {text[:40]}")
                        return
            except Exception as e:
                logger.error(f"飞书回复异常: {e}")
        # 降级：webhook 群发（标注为 bot 命令响应）
        await self._send_webhook(text, topic="nms_cmd", source="bot_command")

    async def _reply_card(self, message_id: str, payload: dict, chat_id: str = "",
                          fallback_text: str = ""):
        """回复飞书消息（interactive 卡片）；失败降级为文本回复（带实际内容）。"""
        token = await self._tenant_token()
        if token:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"msg_type": "interactive",
                              "content": json.dumps(payload, ensure_ascii=False)})
                    if resp.status_code == 200 and resp.json().get("code") == 0:
                        logger.info("飞书卡片回复成功")
                        return
            except Exception as e:
                logger.error(f"飞书卡片回复异常: {e}")
        # 降级：文本回复（卡片失败时仍把内容发给用户）
        await self._reply_im(message_id, fallback_text or "（卡片发送失败）", chat_id)

    async def _reply_md(self, message_id: str, text: str, chat_id: str = "",
                        title: str = "🤖 Z-OPS 智能体"):
        """以交互卡片回复对话内容：markdown 清洗为 lark_md（粗体/列表/链接正常渲染），
        解决 text 消息里 **/#/[链接] 标记原样显示的样式问题。失败降级纯文本。"""
        _lark = self._nms_md_to_lark(text or "")
        payload = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": _lark[:1800]}},
                {"tag": "note", "elements": [{"tag": "plain_text",
                    "content": f"Z-OPS 运维智能体 | {time.strftime('%Y-%m-%d %H:%M:%S')}"}]},
            ],
        }
        await self._reply_card(message_id, payload, chat_id,
                               fallback_text=self._md_to_plain(text))

    def _topic_color(self, topic: str) -> str:
        """根据主题返回飞书卡片颜色"""
        colors = {
            "ops": "blue",
            "network": "green",
            "vm": "purple",
            "alert": "red",
            "nms_alarm": "red",
            "nms_cmd": "blue",
            "nms_snapshot": "turquoise",
            "report": "turquoise",
            "system": "grey",
        }
        return colors.get(topic, "blue")

    @staticmethod
    def _nms_md_to_lark(text: str) -> str:
        """把 LLM 分析摘要的 markdown 转成飞书 lark_md 兼容语法。
        lark_md 不支持标题(#)与表格(|)，否则井号/管道符原样显示显得样式缺失：
        标题→粗体行；表格→列表行(key: value / 其余列)。"""
        import re as _re
        lines = []
        in_table = False
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                in_table = False
                lines.append("")
                continue
            m = _re.match(r"^#{1,6}\s+(.*)", line)
            if m:
                in_table = False
                lines.append("**" + m.group(1).strip() + "**")
                continue
            if line.startswith("|") and line.endswith("|"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if all(_re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    in_table = True  # 表格分隔行，跳过
                    continue
                if in_table:
                    # 表格数据行 → 列表项
                    if len(cells) >= 2:
                        lines.append(f"- **{cells[0]}**: " + " / ".join(cells[1:]))
                    else:
                        lines.append("- " + " / ".join(cells))
                else:
                    in_table = True  # 表头行 → 粗体一行
                    lines.append("**" + " / ".join(cells) + "**")
                continue
            in_table = False
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _md_to_plain(text: str) -> str:
        """markdown → 纯文本（用于飞书 text 消息降级）：去粗体/标题/代码标记，
        链接展平为「文字(链接)」，表格转为列表行。"""
        out = []
        for line in (text or "").splitlines():
            line = re.sub(r"^#{1,6}\s+", "", line.strip())
            line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
            line = re.sub(r"`(.+?)`", r"\1", line)
            line = re.sub(r"\[(.+?)\]\((https?://\S+)\)", r"\1(\2)", line)
            if line.startswith("|") and line.endswith("|"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    continue
                line = " · ".join(cells)
            out.append(line)
        return "\n".join(out)


# ═══ 共享 Bot 单例(网关/回调/对话链路复用 Redis 连接) ═══

_shared: Optional["object"] = None


def shared_bot():
    """获取全局共享 Bot 实例(组合根 feishu.bot.FeishuBot,惰性创建)。
    延迟导入避免 protocol ↔ bot 循环依赖。"""
    global _shared
    if _shared is None:
        from .bot import FeishuBot
        _shared = FeishuBot()
    return _shared
