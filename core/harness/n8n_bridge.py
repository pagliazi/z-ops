"""
Z-OPS Harness N8N 集成
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger("zops.harness.n8n")

WORKFLOW_SKILL_MAP = {"backup_check": "vm_backup_check", "health_check": "system_health_check", "notification": "send_notification"}


class N8nHarnessBridge:
    def __init__(self, n8n_client=None):
        self.n8n = n8n_client
    
    async def execute_skill_as_workflow(self, skill_name: str, params: Dict, context: Dict = None) -> Dict:
        workflow_id = WORKFLOW_SKILL_MAP.get(skill_name)
        if not workflow_id:
            return {"success": False, "error": f"未找到技能 {skill_name} 对应的工作流"}
        payload = {"skill": skill_name, "params": params, "context": context or {}}
        if self.n8n:
            try:
                result = await self.n8n.trigger_workflow(workflow_id, payload)
                return {"success": True, "workflow_id": workflow_id, "result": result}
            except Exception as e:
                logger.error(f"触发工作流失败: {e}")
                return {"success": False, "error": str(e)}
        return {"success": False, "error": "N8N 客户端未初始化"}
    
    async def check_skill_health(self, skill_name: str) -> Dict:
        workflow_id = WORKFLOW_SKILL_MAP.get(skill_name)
        return {"skill": skill_name, "workflow_id": workflow_id, "status": "active" if workflow_id else "no_workflow"}


class HarnessN8nExecutor:
    def __init__(self, n8n_bridge: N8nHarnessBridge = None):
        self.n8n_bridge = n8n_bridge
    
    async def execute(self, skill_name: str, params: Dict, context: Dict = None, prefer_n8n: bool = False) -> Dict:
        should_use_n8n = prefer_n8n or (self.n8n_bridge and WORKFLOW_SKILL_MAP.get(skill_name))
        if should_use_n8n and self.n8n_bridge:
            return await self.n8n_bridge.execute_skill_as_workflow(skill_name, params, context)
        return {"success": True, "mode": "local", "skill": skill_name, "params": params, "message": "本地执行"}


async def get_n8n_bridge() -> N8nHarnessBridge:
    try:
        from core.n8n_integration import get_n8n_integration
        n8n = await get_n8n_integration()
        return N8nHarnessBridge(n8n)
    except Exception as e:
        logger.warning(f"N8N 未连接: {e}")
        return N8nHarnessBridge(None)


async def get_harness_executor() -> HarnessN8nExecutor:
    bridge = await get_n8n_bridge()
    return HarnessN8nExecutor(bridge)
