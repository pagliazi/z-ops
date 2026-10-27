"""
Z-OPS N8N 集成模块 (v2)
参考 claw-code 工具调用模式 + claude-code ToolUse 架构
支持：Webhook 触发、动态工作流、重试机制、执行追踪、审批流
"""

import os
import json
import logging
import asyncio
import time
from typing import Optional, Dict, Any, List
from datetime import datetime

import aiohttp

logger = logging.getLogger("zops.n8n")

# ═══ 配置 ═══
N8N_ENABLED = os.getenv("N8N_ENABLED", "false").lower() == "true"
N8N_URL = os.getenv("N8N_URL", "http://localhost:5678")
N8N_API_KEY = os.getenv("N8N_API_KEY", "")
N8N_WEBHOOK_KEY = os.getenv("N8N_WEBHOOK_KEY", "n8n_webhook_secret_2024")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "http://localhost:7789/api/n8n/webhook")

# 重试配置
MAX_RETRIES = 3
RETRY_DELAY = 2  # 秒
REQUEST_TIMEOUT = 30  # 秒


class N8NClient:
    """
    N8N API 客户端 (v2)
    - 连接复用 (单 session)
    - 指数退避重试
    - 执行追踪 (execution_id → status)
    """

    def __init__(self, base_url: str = None, api_key: str = None):
        self.base_url = (base_url or N8N_URL).rstrip("/")
        self.api_key = api_key or N8N_API_KEY
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-N8N-API-Key"] = self.api_key
        return h

    async def _get_session(self) -> aiohttp.ClientSession:
        """复用 aiohttp session"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            self._session = aiohttp.ClientSession(
                headers=self.headers, timeout=timeout
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ───── 基础 API ─────

    async def _request(
        self, method: str, url: str, json_data: dict = None, retries: int = MAX_RETRIES
    ) -> Dict[str, Any]:
        """带重试的 HTTP 请求"""
        session = await self._get_session()
        last_error = None

        for attempt in range(retries):
            try:
                async with session.request(method, url, json=json_data) as resp:
                    body = await resp.text()
                    if resp.status < 300:
                        try:
                            return {"success": True, "status": resp.status, "data": json.loads(body)}
                        except json.JSONDecodeError:
                            return {"success": True, "status": resp.status, "data": body}
                    else:
                        last_error = f"HTTP {resp.status}: {body[:500]}"
                        logger.warning(f"[N8N] 请求失败 (attempt {attempt+1}/{retries}): {last_error}")
            except asyncio.TimeoutError:
                last_error = f"请求超时 ({REQUEST_TIMEOUT}s)"
                logger.warning(f"[N8N] 超时 (attempt {attempt+1}/{retries}): {url}")
            except aiohttp.ClientError as e:
                last_error = str(e)
                logger.warning(f"[N8N] 连接错误 (attempt {attempt+1}/{retries}): {e}")
            except Exception as e:
                last_error = str(e)
                logger.error(f"[N8N] 未知错误: {e}")
                break

            if attempt < retries - 1:
                delay = RETRY_DELAY * (2 ** attempt)
                await asyncio.sleep(delay)

        return {"success": False, "error": last_error}

    # ───── 健康检查 ─────

    async def get_status(self) -> Dict[str, Any]:
        """获取 N8N 服务状态"""
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}/healthz",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    return {
                        "enabled": True, "status": "online",
                        "url": self.base_url, "latency_ms": 0
                    }
                return {
                    "enabled": N8N_ENABLED, "status": "error",
                    "url": self.base_url, "http_status": resp.status
                }
        except Exception as e:
            logger.debug(f"N8N 健康检查失败：{e}")
            return {
                "enabled": N8N_ENABLED, "status": "offline",
                "url": self.base_url, "error": str(e)
            }

    # ───── 工作流管理 ─────

    async def get_workflows(self) -> List[Dict[str, Any]]:
        """获取所有工作流"""
        result = await self._request("GET", f"{self.base_url}/api/v1/workflows")
        if result["success"]:
            data = result["data"]
            # n8n API 返回 {data: [...]} 或直接 [...]
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            if isinstance(data, list):
                return data
        return []

    async def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """获取单个工作流详情"""
        result = await self._request("GET", f"{self.base_url}/api/v1/workflows/{workflow_id}")
        if result["success"]:
            return result["data"]
        return None

    async def create_workflow(self, workflow_data: dict) -> Optional[Dict[str, Any]]:
        """创建工作流"""
        result = await self._request("POST", f"{self.base_url}/api/v1/workflows", workflow_data)
        if result["success"]:
            logger.info(f"[N8N] 创建工作流成功: {workflow_data.get('name', 'unknown')}")
            return result["data"]
        logger.error(f"[N8N] 创建工作流失败: {result.get('error')}")
        return None

    async def update_workflow(self, workflow_id: str, workflow_data: dict) -> Optional[Dict[str, Any]]:
        """更新工作流"""
        result = await self._request("PATCH", f"{self.base_url}/api/v1/workflows/{workflow_id}", workflow_data)
        if result["success"]:
            return result["data"]
        return None

    async def activate_workflow(self, workflow_id: str, active: bool = True) -> bool:
        """激活/停用工作流"""
        result = await self._request(
            "PATCH",
            f"{self.base_url}/api/v1/workflows/{workflow_id}",
            {"active": active}
        )
        return result["success"]

    # ───── 触发工作流 (Webhook) ─────

    async def trigger_workflow(self, webhook_path: str, data: Dict[str, Any] = None) -> Dict[str, Any]:
        """通过 Webhook 触发工作流"""
        url = f"{self.base_url}/{webhook_path.lstrip('/')}"
        t0 = time.time()
        result = await self._request("POST", url, data or {})
        elapsed = time.time() - t0

        log_data = {
            "webhook": webhook_path,
            "success": result["success"],
            "elapsed_s": round(elapsed, 2),
        }
        if result["success"]:
            logger.info(f"[N8N] 触发工作流成功: {log_data}")
        else:
            log_data["error"] = result.get("error", "unknown")
            logger.error(f"[N8N] 触发工作流失败: {log_data}")

        return {**result, "elapsed_s": round(elapsed, 2)}

    async def trigger_by_registry(self, workflow_name: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        通过 WORKFLOW_REGISTRY 名称触发工作流
        自动查找 webhook_path 并注入参数
        """
        from core.n8n_workflows import get_workflow
        wf_def = get_workflow(workflow_name)
        if not wf_def:
            return {"success": False, "error": f"未知工作流: {workflow_name}"}

        webhook_path = wf_def.get("webhook_path", f"webhook/{workflow_name}")

        # 验证必要参数
        required_params = wf_def.get("params", [])
        if params and required_params:
            missing = [p for p in required_params if p not in params and p not in ("check_items", "sync_mode")]
            if missing:
                logger.warning(f"[N8N] 缺少参数: {missing} (workflow={workflow_name})")

        # 检查是否需要审批
        if wf_def.get("require_approval"):
            return await self._trigger_with_approval(workflow_name, wf_def, params or {})

        return await self.trigger_workflow(webhook_path, {
            "workflow": workflow_name,
            "params": params or {},
            "triggered_by": "zops_api",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    async def _trigger_with_approval(
        self, workflow_name: str, wf_def: dict, params: dict
    ) -> Dict[str, Any]:
        """触发需要审批的工作流 — 先创建审批请求"""
        import uuid
        approval_id = f"apr_{uuid.uuid4().hex[:12]}"

        # 发送审批请求到审批工作流
        approval_data = {
            "approval_id": approval_id,
            "workflow": workflow_name,
            "workflow_name": wf_def["name"],
            "params": params,
            "require_role": wf_def.get("approval_role", "admin"),
            "callback_url": f"{N8N_WEBHOOK_URL.replace('/api/n8n/webhook', '/api/webhook/n8n_callback')}",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        result = await self.trigger_workflow("webhook/approval-request", approval_data)

        if result["success"]:
            logger.info(f"[N8N] 审批请求已发送: {approval_id} ({workflow_name})")
            return {
                "success": True,
                "status": "pending_approval",
                "approval_id": approval_id,
                "message": f"操作 [{wf_def['name']}] 需要管理员审批，审批ID: {approval_id}",
            }
        return result

    # ───── 快捷触发方法 ─────

    async def trigger_backup_check(self, gid: str = None, site: str = None) -> Dict[str, Any]:
        """触发备份检查"""
        params = {}
        if gid:
            params["gid"] = gid
        if site:
            params["site"] = site
        return await self.trigger_by_registry("backup_check", params)

    async def trigger_health_check(self, cluster_name: str = None) -> Dict[str, Any]:
        """触发健康检查"""
        params = {"cluster_name": cluster_name} if cluster_name else {}
        return await self.trigger_by_registry("host_health_check", params)

    async def trigger_daily_report(self, date: str = None) -> Dict[str, Any]:
        """触发每日健康报告"""
        params = {"date": date or datetime.now().strftime("%Y-%m-%d")}
        return await self.trigger_by_registry("daily_health_report", params)

    async def trigger_vm_power(self, vm_id: str, action: str, vm_name: str = "") -> Dict[str, Any]:
        """触发 VM 电源操作 (需审批)"""
        return await self.trigger_by_registry("vm_power", {
            "vm_id": vm_id, "action": action, "vm_name": vm_name
        })

    # ───── 通知推送 ─────

    async def send_notification(
        self, title: str, message: str, channels: List[str] = None
    ) -> Dict[str, Any]:
        """通过邮件发送通知 (优先邮件方案)"""
        channels = channels or ["email"]
        # 邮件优先: 直接调用后端邮件发送
        if "email" in channels:
            try:
                import sys, os
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                from webchat_api import send_notify_email
                import config
                html_body = f"<h3>{title}</h3><p>{message}</p><p style='color:#888;font-size:12px'>发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
                result = send_notify_email(f"[Z-OPS] {title}", html_body, config.ADMIN_NOTIFY_EMAIL)
                return {"success": result.get("success", False), "channel": "email", "to": config.ADMIN_NOTIFY_EMAIL}
            except Exception as e:
                logger.error(f"[N8N] 邮件通知发送失败: {e}")
                return {"success": False, "error": str(e)}
        # 回退: 通过 N8N 工作流发送
        return await self.trigger_workflow("webhook/notification", {
            "title": title,
            "message": message,
            "channels": channels,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    async def send_feishu_card(
        self, title: str, content: str, card_type: str = "info",
        action_buttons: List[dict] = None
    ) -> Dict[str, Any]:
        """发送飞书交互卡片"""
        return await self.trigger_workflow("webhook/feishu-card", {
            "card_type": card_type,
            "title": title,
            "content": content,
            "action_buttons": action_buttons or [],
        })

    # ───── Webhook 处理 ─────

    async def process_webhook(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """
        处理 N8N 发送过来的 Webhook 回调
        支持类型: workflow_result, health_report, alert, approval_result
        """
        action = body.get("action", body.get("callback_type", "unknown"))
        logger.info(f"[N8N] 处理 Webhook 回调: action={action}")

        if action == "health_report":
            return await self._handle_health_report(body)
        elif action == "alert":
            return await self._handle_alert(body)
        elif action == "backup_result":
            return await self._handle_backup_result(body)
        elif action == "workflow_result":
            return {"processed": True, "action": action, "data": body}
        else:
            logger.warning(f"[N8N] 未知 Webhook 动作: {action}")
            return {"processed": False, "action": action, "reason": "unknown_action"}

    async def _handle_health_report(self, body: dict) -> dict:
        """处理健康报告回调"""
        report = body.get("report", {})
        status = report.get("overall_status", "unknown")
        logger.info(f"[N8N] 健康报告: 整体状态={status}")

        # 如果有异常，推送告警通知
        if status in ("warning", "error", "critical"):
            issues = report.get("issues", [])
            await self._publish_notification(
                topic="health",
                text=f"健康检查异常: {status}\n" + "\n".join(f"- {i}" for i in issues[:5]),
                source="n8n_health_check"
            )
        return {"processed": True, "status": status}

    async def _handle_alert(self, body: dict) -> dict:
        """处理告警回调"""
        alert_level = body.get("level", "warning")
        alert_msg = body.get("message", "")
        logger.warning(f"[N8N] 收到告警: level={alert_level}, msg={alert_msg[:100]}")

        await self._publish_notification(
            topic="alert",
            text=f"[{alert_level.upper()}] {alert_msg}",
            source=body.get("source", "n8n_alert")
        )
        return {"processed": True, "level": alert_level}

    async def _handle_backup_result(self, body: dict) -> dict:
        """处理备份结果回调"""
        gid = body.get("gid", "")
        status = body.get("status", "unknown")
        site = body.get("site", "")
        logger.info(f"[N8N] 备份结果: GID={gid}, site={site}, status={status}")

        if status != "success":
            await self._publish_notification(
                topic="ops",
                text=f"备份异常 GID={gid} (站点:{site}): {body.get('error', status)}",
                source="n8n_backup_check"
            )
        return {"processed": True, "gid": gid, "status": status}

    async def _publish_notification(self, topic: str, text: str, source: str):
        """通过 Redis 发布通知到飞书等渠道"""
        try:
            import redis.asyncio as aioredis
            import config
            r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
            notify_ch = config.channel("notify:feishu")
            await r.publish(notify_ch, json.dumps({
                "topic": topic, "text": text, "source": source,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }))
            await r.aclose()
        except Exception as e:
            logger.error(f"[N8N] 发布通知失败: {e}")

    # ───── 执行记录 ─────

    async def get_executions(self, workflow_id: str = None, limit: int = 20) -> List[Dict[str, Any]]:
        """获取执行记录"""
        url = f"{self.base_url}/api/v1/executions?limit={limit}"
        if workflow_id:
            url += f"&workflowId={workflow_id}"
        result = await self._request("GET", url)
        if result["success"]:
            data = result["data"]
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            if isinstance(data, list):
                return data
        return []


# ═══ 单例管理 ═══

_n8n_client: Optional[N8NClient] = None


async def get_n8n_integration() -> N8NClient:
    """获取 N8N 集成实例 (单例)"""
    global _n8n_client
    if _n8n_client is None:
        _n8n_client = N8NClient()
    return _n8n_client


def get_n8n_config() -> Dict[str, Any]:
    """获取 N8N 配置信息"""
    return {
        "enabled": N8N_ENABLED,
        "url": N8N_URL,
        "webhook_url": N8N_WEBHOOK_URL,
        "api_key_configured": bool(N8N_API_KEY),
        "max_retries": MAX_RETRIES,
        "timeout_s": REQUEST_TIMEOUT,
    }
