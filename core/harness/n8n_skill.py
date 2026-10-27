"""
Z-OPS Harness - N8N 技能封装基类
提供统一的 N8N Workflow 调用接口，Agent 通过此类调用标准化操作
"""

import logging
from typing import Dict, Optional, Any, Callable
from dataclasses import dataclass
from datetime import datetime
import asyncio
import aiohttp

logger = logging.getLogger("zops.harness.n8n_skill")

# N8N 配置
N8N_BASE_URL = "http://localhost:5678"
N8N_WEBHOOK_URL = f"{N8N_BASE_URL}/webhook"
N8N_API_URL = f"{N8N_BASE_URL}/api/v1"


@dataclass
class SkillExecutionResult:
    """技能执行结果"""
    success: bool
    workflow_id: str
    execution_id: Optional[str] = None
    data: Optional[Dict] = None
    error: Optional[str] = None
    timestamp: str = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


class N8nBaseSkill:
    """
    N8N 技能基类
    
    使用方式:
    class VMPowerSkill(N8nBaseSkill):
        WORKFLOW_ID = "vm_power"
        REQUIRED_PARAMS = ["vm_name", "action"]
        
    skill = VMPowerSkill()
    result = await skill.execute(vm_name="web-server-01", action="restart")
    """
    
    WORKFLOW_ID: str = ""  # N8N Workflow ID
    REQUIRED_PARAMS: list = []  # 必需参数列表
    OPTIONAL_PARAMS: dict = {}  # 可选参数及默认值
    
    def __init__(self, n8n_url: str = None, api_key: str = None):
        self.n8n_url = n8n_url or N8N_BASE_URL
        self.api_key = api_key
        self.webhook_url = f"{self.n8n_url}/webhook/{self.WORKFLOW_ID}"
        self._execution_history: list = []
    
    def validate_params(self, params: Dict) -> tuple[bool, str]:
        """验证参数"""
        for param in self.REQUIRED_PARAMS:
            if param not in params:
                return False, f"缺少必需参数: {param}"
        return True, ""
    
    def build_payload(self, params: Dict, context: Dict = None) -> Dict:
        """构建请求载荷"""
        payload = {
            "skill": self.__class__.__name__,
            "workflow_id": self.WORKFLOW_ID,
            "params": params,
            "context": context or {},
            "timestamp": datetime.now().isoformat(),
            "source": "zops-agent",
        }
        return payload
    
    async def execute(
        self, 
        params: Dict, 
        context: Dict = None,
        wait_for_result: bool = True,
        timeout: int = 60
    ) -> SkillExecutionResult:
        """
        执行技能
        
        Args:
            params: 技能参数
            context: 上下文信息
            wait_for_result: 是否等待 N8N 执行结果
            timeout: 超时时间(秒)
            
        Returns:
            SkillExecutionResult: 执行结果
        """
        # 参数验证
        valid, error = self.validate_params(params)
        if not valid:
            return SkillExecutionResult(
                success=False,
                workflow_id=self.WORKFLOW_ID,
                error=error
            )
        
        payload = self.build_payload(params, context)
        
        try:
            async with aiohttp.ClientSession() as session:
                # 触发 Webhook
                async with session.post(
                    self.webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as response:
                    if response.status == 200:
                        result_data = await response.json()
                        execution_record = {
                            "success": True,
                            "workflow_id": self.WORKFLOW_ID,
                            "params": params,
                            "result": result_data,
                            "timestamp": datetime.now().isoformat()
                        }
                        self._execution_history.append(execution_record)
                        
                        return SkillExecutionResult(
                            success=True,
                            workflow_id=self.WORKFLOW_ID,
                            execution_id=result_data.get("execution_id"),
                            data=result_data
                        )
                    else:
                        error_text = await response.text()
                        logger.warning(f"N8N Webhook 调用失败: {response.status} - {error_text}")
                        return SkillExecutionResult(
                            success=False,
                            workflow_id=self.WORKFLOW_ID,
                            error=f"HTTP {response.status}: {error_text}"
                        )
                        
        except asyncio.TimeoutError:
            logger.error(f"N8N 执行超时: {self.WORKFLOW_ID}")
            return SkillExecutionResult(
                success=False,
                workflow_id=self.WORKFLOW_ID,
                error="执行超时"
            )
        except Exception as e:
            logger.error(f"N8N 执行异常: {e}")
            return SkillExecutionResult(
                success=False,
                workflow_id=self.WORKFLOW_ID,
                error=str(e)
            )
    
    def get_execution_history(self, limit: int = 10) -> list:
        """获取执行历史"""
        return self._execution_history[-limit:]
    
    async def health_check(self) -> Dict:
        """检查技能/工作流健康状态"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{N8N_API_URL}/workflows",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    return {
                        "skill": self.__class__.__name__,
                        "workflow_id": self.WORKFLOW_ID,
                        "n8n_status": "online" if response.status == 200 else "error",
                        "timestamp": datetime.now().isoformat()
                    }
        except Exception as e:
            return {
                "skill": self.__class__.__name__,
                "workflow_id": self.WORKFLOW_ID,
                "n8n_status": "offline",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }


# ========== 常用技能实现 ==========

class VMPowerSkill(N8nBaseSkill):
    """虚拟机电源管理技能"""
    WORKFLOW_ID = "vm_power"
    REQUIRED_PARAMS = ["vm_name", "action"]
    
    def validate_params(self, params: Dict) -> tuple[bool, str]:
        valid, error = super().validate_params(params)
        if not valid:
            return valid, error
        if params["action"] not in ["start", "stop", "restart", "shutdown"]:
            return False, f"无效的操作: {params['action']}"
        return True, ""


class VMBackupCheckSkill(N8nBaseSkill):
    """虚拟机备份检查技能"""
    WORKFLOW_ID = "backup_check"
    REQUIRED_PARAMS = ["vm_name"]
    OPTIONAL_PARAMS = {"check_type": "all"}


class SystemHealthCheckSkill(N8nBaseSkill):
    """系统健康检查技能"""
    WORKFLOW_ID = "health_check"
    REQUIRED_PARAMS = []


class NotificationSkill(N8nBaseSkill):
    """通知技能"""
    WORKFLOW_ID = "notification"
    REQUIRED_PARAMS = ["message", "channel"]
    OPTIONAL_PARAMS = {"priority": "normal", "to": ""}


class ApprovalRequestSkill(N8nBaseSkill):
    """人工审批请求技能"""
    WORKFLOW_ID = "approval_request"
    REQUIRED_PARAMS = ["action", "reason", "requester"]
    OPTIONAL_PARAMS = {"timeout": 300}


class DailyHealthReportSkill(N8nBaseSkill):
    """每日健康报告技能"""
    WORKFLOW_ID = "daily_health_report"
    REQUIRED_PARAMS = []


# ========== 技能工厂 ==========

SKILL_REGISTRY = {
    "vm_power": VMPowerSkill,
    "vm_backup_check": VMBackupCheckSkill,
    "backup_check": VMBackupCheckSkill,
    "health_check": SystemHealthCheckSkill,
    "notification": NotificationSkill,
    "approval_request": ApprovalRequestSkill,
    "daily_health_report": DailyHealthReportSkill,
}


def get_skill(skill_name: str) -> Optional[N8nBaseSkill]:
    """获取技能实例"""
    skill_class = SKILL_REGISTRY.get(skill_name)
    if skill_class:
        return skill_class()
    return None


async def execute_skill(
    skill_name: str, 
    params: Dict, 
    context: Dict = None
) -> SkillExecutionResult:
    """执行技能的统一入口"""
    skill = get_skill(skill_name)
    if not skill:
        return SkillExecutionResult(
            success=False,
            workflow_id=skill_name,
            error=f"未知的技能: {skill_name}"
        )
    return await skill.execute(params, context)


# ========== 便捷函数 ==========

async def vm_power(vm_name: str, action: str) -> SkillExecutionResult:
    """虚拟机电源操作"""
    skill = VMPowerSkill()
    return await skill.execute({"vm_name": vm_name, "action": action})


async def check_backup(vm_name: str) -> SkillExecutionResult:
    """检查虚拟机备份"""
    skill = VMBackupCheckSkill()
    return await skill.execute({"vm_name": vm_name})


async def send_notification(message: str, channel: str = "feishu") -> SkillExecutionResult:
    """发送通知"""
    skill = NotificationSkill()
    return await skill.execute({"message": message, "channel": channel})


async def request_approval(action: str, reason: str, requester: str = "system") -> SkillExecutionResult:
    """请求人工审批"""
    skill = ApprovalRequestSkill()
    return await skill.execute({"action": action, "reason": reason, "requester": requester})