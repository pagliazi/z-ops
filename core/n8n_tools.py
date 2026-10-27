"""
Z-OPS N8N 工具封装
实现 n8n 作为 Agent 执行器的功能
参考 n8n设计系统架构.md 的技能层解耦与外包方案
"""

import os
import json
import logging
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger("zops.n8n_tools")


class N8nToolConfig:
    """N8N 工具配置"""
    
    # 标准工作流映射
    WORKFLOW_REGISTRY = {
        # VM 管理类
        "create_vm": {
            "workflow_id": "create_vm_workflow",
            "description": "在 VMware/PVE 创建虚拟机",
            "params": ["name", "cpu", "memory", "disk", "template"],
            "require_approval": True,
            "category": "vm"
        },
        "vm_power": {
            "workflow_id": "vm_power_workflow",
            "description": "虚拟机电源操作 (开机/关机/重启)",
            "params": ["vm_id", "action"],
            "require_approval": True,
            "category": "vm"
        },
        "vm_snapshot": {
            "workflow_id": "vm_snapshot_workflow",
            "description": "虚拟机快照管理",
            "params": ["vm_id", "action", "snapshot_name"],
            "require_approval": True,
            "category": "vm"
        },
        
        # GCOC 平台类
        "backup_check": {
            "workflow_id": "backup_check_workflow",
            "description": "检查备份状态",
            "params": ["gid", "site"],
            "require_approval": False,
            "category": "gcoc"
        },
        "cmdb_sync": {
            "workflow_id": "cmdb_sync_workflow",
            "description": "同步 CMDB 主机信息",
            "params": [],
            "require_approval": False,
            "category": "gcoc"
        },
        
        # AD/LDAP 类
        "ad_user_create": {
            "workflow_id": "ad_user_workflow",
            "description": "创建 AD 域用户",
            "params": ["username", "display_name", "ou"],
            "require_approval": True,
            "category": "ad"
        },
        "ad_user_disable": {
            "workflow_id": "ad_user_disable_workflow",
            "description": "禁用 AD 域用户",
            "params": ["username"],
            "require_approval": True,
            "category": "ad"
        },
        
        # 通知类
        "notification": {
            "workflow_id": "notification_workflow",
            "description": "发送通知 (飞书/企微/邮件)",
            "params": ["title", "message", "channels"],
            "require_approval": False,
            "category": "notification"
        },
        "daily_health_report": {
            "workflow_id": "daily_health_report",
            "description": "生成每日健康报告",
            "params": [],
            "require_approval": False,
            "category": "report"
        },
        
        # 巡检类
        "host_health_check": {
            "workflow_id": "host_health_check",
            "description": "主机健康检查",
            "params": ["host_ip", "check_items"],
            "require_approval": False,
            "category": "ops"
        },
    }
    
    # 需要人工审批的操作
    APPROVAL_REQUIRED_ACTIONS = {
        "vm_power", "create_vm", "vm_snapshot", 
        "ad_user_create", "ad_user_disable"
    }


class N8nTool:
    """
    N8N 工具封装 - 作为 Agent 的执行器
    实现技能层的外包机制
    """
    
    def __init__(self, n8n_url: str = None, api_key: str = None):
        self.n8n_url = n8n_url or os.getenv("N8N_URL", "http://localhost:5678")
        self.api_key = api_key or os.getenv("N8N_API_KEY", "")
        self.config = N8nToolConfig()
    
    async def execute_tool(
        self, 
        tool_name: str, 
        params: Dict[str, Any],
        context: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        执行 N8N 工具
        
        Args:
            tool_name: 工具名称 (对应 WORKFLOW_REGISTRY 的 key)
            params: 工具参数
            context: 执行上下文 (包含 user 信息等)
            
        Returns:
            执行结果
        """
        # 检查工具是否存在
        if tool_name not in self.config.WORKFLOW_REGISTRY:
            return {
                "success": False,
                "error": f"未知工具: {tool_name}",
                "available_tools": list(self.config.WORKFLOW_REGISTRY.keys())
            }
        
        tool_info = self.config.WORKFLOW_REGISTRY[tool_name]
        
        # 检查是否需要审批
        if tool_info.get("require_approval", False):
            # 创建审批请求
            approval_result = await self._request_approval(tool_name, params, context)
            if not approval_result.get("approved"):
                return {
                    "success": False,
                    "error": "操作需要管理员审批",
                    "approval_id": approval_result.get("approval_id"),
                    "status": "pending_approval"
                }
        
        # 执行工作流
        return await self._trigger_workflow(
            tool_info["workflow_id"],
            params,
            context
        )
    
    async def _request_approval(
        self, 
        tool_name: str, 
        params: Dict[str, Any],
        context: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        请求人工审批
        通过飞书/企微发送审批卡片
        """
        approval_id = f"approval_{int(datetime.now().timestamp())}"
        
        # 构建审批请求消息
        message = {
            "approval_id": approval_id,
            "tool_name": tool_name,
            "description": self.config.WORKFLOW_REGISTRY[tool_name]["description"],
            "params": params,
            "requester": context.get("username", "unknown") if context else "unknown",
            "timestamp": datetime.now().isoformat()
        }
        
        # 发送审批请求到通知渠道
        logger.info(f"创建审批请求: {approval_id} - {tool_name}")
        
        # TODO: 集成飞书审批卡片
        # await send_approval_card(message)
        
        return {
            "approved": False,
            "approval_id": approval_id,
            "status": "waiting"
        }
    
    async def _trigger_workflow(
        self, 
        workflow_id: str, 
        params: Dict[str, Any],
        context: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        触发 N8N 工作流
        """
        import aiohttp
        
        payload = {
            "workflow_id": workflow_id,
            "params": params,
            "context": context or {},
            "triggered_by": "zops_agent",
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                webhook_url = f"{self.n8n_url}/webhook/{workflow_id}"
                async with session.post(
                    webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return {
                            "success": True,
                            "result": result,
                            "workflow_id": workflow_id
                        }
                    else:
                        return {
                            "success": False,
                            "error": f"工作流执行失败: HTTP {resp.status}",
                            "workflow_id": workflow_id
                        }
        except Exception as e:
            logger.error(f"触发 N8N 工作流失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "workflow_id": workflow_id
            }
    
    async def get_available_tools(self) -> List[Dict[str, Any]]:
        """获取可用工具列表"""
        tools = []
        for name, info in self.config.WORKFLOW_REGISTRY.items():
            tools.append({
                "name": name,
                "description": info["description"],
                "params": info["params"],
                "require_approval": info.get("require_approval", False),
                "category": info.get("category", "general")
            })
        return tools
    
    async def handle_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        处理 N8N Webhook 回调
        """
        webhook_type = payload.get("type")
        
        if webhook_type == "approval_response":
            # 处理审批响应
            approval_id = payload.get("approval_id")
            approved = payload.get("approved")
            logger.info(f"审批响应: {approval_id} - {'批准' if approved else '拒绝'}")
            return {"processed": True, "approval_id": approval_id}
        
        elif webhook_type == "workflow_result":
            # 处理工作流执行结果
            workflow_id = payload.get("workflow_id")
            result = payload.get("result")
            logger.info(f"工作流完成: {workflow_id}")
            return {"processed": True, "workflow_id": workflow_id, "result": result}
        
        elif webhook_type == "trigger":
            # 处理外部触发 (如告警)
            trigger_type = payload.get("trigger_type")
            data = payload.get("data")
            logger.info(f"外部触发: {trigger_type}")
            return {
                "processed": True, 
                "trigger_type": trigger_type, 
                "data": data,
                "next_action": "route_to_agent"
            }
        
        return {"processed": False, "error": "未知 webhook 类型"}


# 全局实例
_n8n_tool: Optional[N8nTool] = None


def get_n8n_tool() -> N8nTool:
    """获取 N8N 工具实例"""
    global _n8n_tool
    if _n8n_tool is None:
        _n8n_tool = N8nTool()
    return _n8n_tool