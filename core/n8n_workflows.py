"""
N8N 工作流定义
基于 n8n设计系统架构.md 的执行中枢架构
包含：VM管理、GCOC备份、健康检查、通知推送、AD管理、审批流
"""

# 工作流分类
WORKFLOW_CATEGORIES = {
    "vm": "虚拟机管理",
    "gcoc": "GCOC平台",
    "ad": "AD/LDAP管理",
    "notification": "通知推送",
    "report": "报告生成",
    "ops": "运维巡检",
    "approval": "审批流程",
    "event": "事件驱动(主动运维)",
}

# 完整工作流注册表
WORKFLOW_REGISTRY = {
    # ========== VM 管理类 ==========
    "vm_power": {
        "workflow_id": "vm_power",
        "name": "VM电源操作",
        "description": "虚拟机开关机重启操作",
        "category": "vm",
        "params": ["vm_id", "vm_name", "action"],  # action: start/stop/restart
        "require_approval": True,
        "approval_role": "admin",
        "webhook_path": "webhook/vm-power"
    },
    "vm_snapshot": {
        "workflow_id": "vm_snapshot",
        "name": "VM快照管理",
        "description": "创建/恢复/删除虚拟机快照",
        "category": "vm",
        "params": ["vm_id", "action", "snapshot_name"],
        "require_approval": True,
        "approval_role": "admin",
        "webhook_path": "webhook/vm-snapshot"
    },
    "create_vm": {
        "workflow_id": "create_vm",
        "name": "创建虚拟机",
        "description": "在VMware/PVE创建新虚拟机",
        "category": "vm",
        "params": ["name", "cpu", "memory", "disk", "template", "network"],
        "require_approval": True,
        "approval_role": "admin",
        "webhook_path": "webhook/create-vm"
    },

    # ========== GCOC 平台类 ==========
    "backup_check": {
        "workflow_id": "backup_check",
        "name": "备份状态检查",
        "description": "检查GID备份状态",
        "category": "gcoc",
        "params": ["gid", "site"],
        "require_approval": False,
        "webhook_path": "webhook/backup-check"
    },
    "backup_restore": {
        "workflow_id": "backup_restore",
        "name": "备份恢复",
        "description": "从备份恢复GID数据",
        "category": "gcoc",
        "params": ["gid", "backup_id", "target_date"],
        "require_approval": True,
        "approval_role": "admin",
        "webhook_path": "webhook/backup-restore"
    },
    "cmdb_sync": {
        "workflow_id": "cmdb_sync",
        "name": "CMDB同步",
        "description": "同步CMDB主机信息到Z-OPS",
        "category": "gcoc",
        "params": ["sync_mode"],  # full/incremental
        "require_approval": False,
        "webhook_path": "webhook/cmdb-sync"
    },

    # ========== AD/LDAP 类 ==========
    "ad_user_create": {
        "workflow_id": "ad_user_create",
        "name": "创建AD用户",
        "description": "在AD域创建新用户",
        "category": "ad",
        "params": ["username", "display_name", "ou", "email", "phone"],
        "require_approval": True,
        "approval_role": "admin",
        "webhook_path": "webhook/ad-user-create"
    },
    "ad_user_disable": {
        "workflow_id": "ad_user_disable",
        "name": "禁用AD用户",
        "description": "禁用AD域用户账户",
        "category": "ad",
        "params": ["username"],
        "require_approval": True,
        "approval_role": "admin",
        "webhook_path": "webhook/ad-user-disable"
    },
    "ad_group_add": {
        "workflow_id": "ad_group_add",
        "name": "添加AD组成员",
        "description": "将用户添加到AD安全组",
        "category": "ad",
        "params": ["username", "group_name"],
        "require_approval": True,
        "approval_role": "admin",
        "webhook_path": "webhook/ad-group-add"
    },

    # ========== 通知类 ==========
    "notification": {
        "workflow_id": "notification",
        "name": "发送通知",
        "description": "通过飞书/企微/邮件发送通知",
        "category": "notification",
        "params": ["title", "message", "channels", "receivers"],
        "require_approval": False,
        "webhook_path": "webhook/notification"
    },
    "feishu_card": {
        "workflow_id": "feishu_card",
        "name": "飞书交互卡片",
        "description": "发送飞书交互式卡片消息",
        "category": "notification",
        "params": ["card_type", "title", "content", "action_buttons"],
        "require_approval": False,
        "webhook_path": "webhook/feishu-card"
    },

    # ========== 报告类 ==========
    "daily_health_report": {
        "workflow_id": "daily_health_report",
        "name": "每日健康报告",
        "description": "生成系统每日健康状态报告",
        "category": "report",
        "params": [],
        "require_approval": False,
        "schedule": "0 9 * * *",  # 每天早上9点
        "webhook_path": "webhook/daily-health-report"
    },
    "weekly_usage_report": {
        "workflow_id": "weekly_usage_report",
        "name": "每周用量报告",
        "description": "生成LLM每周调用统计报告",
        "category": "report",
        "params": [],
        "require_approval": False,
        "schedule": "0 9 * * 1",  # 每周一早上9点
        "webhook_path": "webhook/weekly-usage-report"
    },

    # ========== 运维巡检类 ==========
    "host_health_check": {
        "workflow_id": "host_health_check",
        "name": "主机健康检查",
        "description": "检查主机网络/磁盘/CPU/内存状态",
        "category": "ops",
        "params": ["host_ip", "check_items"],
        "require_approval": False,
        "webhook_path": "webhook/host-health-check"
    },
    "cluster_health_check": {
        "workflow_id": "cluster_health_check",
        "name": "集群健康检查",
        "description": "检查整个集群健康状态",
        "category": "ops",
        "params": ["cluster_name"],
        "require_approval": False,
        "schedule": "0 */2 * * *",  # 每2小时
        "webhook_path": "webhook/cluster-health-check"
    },
    "disk_space_check": {
        "workflow_id": "disk_space_check",
        "name": "磁盘空间检查",
        "description": "检查存储空间并告警",
        "category": "ops",
        "params": ["threshold"],
        "require_approval": False,
        "schedule": "0 */4 * * *",  # 每4小时
        "webhook_path": "webhook/disk-space-check"
    },

    # ========== 审批流 ==========
    "approval_request": {
        "workflow_id": "approval_request",
        "name": "审批请求",
        "description": "通用审批流程",
        "category": "approval",
        "params": ["request_type", "request_title", "request_content", "approver"],
        "require_approval": True,
        "approval_role": "admin",
        "webhook_path": "webhook/approval-request"
    },
    "emergency_reboot": {
        "workflow_id": "emergency_reboot",
        "name": "紧急重启",
        "description": "紧急情况下重启服务/主机",
        "category": "approval",
        "params": ["target", "reason", "force"],
        "require_approval": True,
        "approval_role": "admin",
        "webhook_path": "webhook/emergency-reboot"
    },

    # ========== 事件驱动类 (Proactive) ==========
    "alert_receiver": {
        "workflow_id": "alert_receiver",
        "name": "告警接收器",
        "description": "统一接收 Zabbix/Prometheus/自定义告警，高级别告警自动转发 Agent 诊断",
        "category": "event",
        "params": ["source", "level", "title", "message", "host"],
        "require_approval": False,
        "webhook_path": "webhook/alert-receiver"
    },
    "auto_diagnosis": {
        "workflow_id": "auto_diagnosis",
        "name": "自动诊断SOP",
        "description": "根据告警症状自动选择 SOP 并请求 Agent 诊断",
        "category": "event",
        "params": ["target", "symptom", "sop_type"],
        "require_approval": False,
        "webhook_path": "webhook/auto-diagnosis"
    },
}

# 审批状态
APPROVAL_STATUS = {
    "PENDING": "待审批",
    "APPROVED": "已批准",
    "REJECTED": "已拒绝",
    "CANCELLED": "已取消",
    "EXPIRED": "已过期"
}

# 通知渠道
NOTIFICATION_CHANNELS = {
    "feishu": "飞书",
    "wecom": "企业微信",
    "email": "邮件",
    "webhook": "自定义Webhook"
}

def get_workflow_by_category(category: str) -> list:
    """获取指定分类的工作流列表"""
    return [
        {**wf, "id": wf_id} 
        for wf_id, wf in WORKFLOW_REGISTRY.items() 
        if wf.get("category") == category
    ]

def get_workflow(workflow_id: str) -> dict:
    """获取工作流定义"""
    return WORKFLOW_REGISTRY.get(workflow_id)

def get_all_workflows() -> list:
    """获取所有工作流"""
    return [
        {**wf, "id": wf_id} 
        for wf_id, wf in WORKFLOW_REGISTRY.items()
    ]

def get_require_approval_workflows() -> list:
    """获取需要审批的工作流"""
    return [
        {**wf, "id": wf_id} 
        for wf_id, wf in WORKFLOW_REGISTRY.items() 
        if wf.get("require_approval")
    ]

def get_scheduled_workflows() -> list:
    """获取定时执行的工作流"""
    return [
        {**wf, "id": wf_id} 
        for wf_id, wf in WORKFLOW_REGISTRY.items() 
        if wf.get("schedule")
    ]