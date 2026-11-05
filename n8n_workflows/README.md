# Z-OPS N8N 工作流示例

> 参考 `n8n设计系统架构.md` 文档创建的工作流示例

## 工作流结构

```
n8n_workflows/
├── backup_check_workflow.json    # 备份检查工作流
├── vm_power_workflow.json         # VM 电源操作工作流
├── notification_workflow.json     # 通知工作流
├── health_check_workflow.json     # 健康检查工作流
└── README.md                      # 本文件
```

## 快速开始

### 1. 导入工作流

1. 打开 N8N 界面: http://localhost:5678
2. 点击 "Import from File" 
3. 选择对应的 `.json` 文件

### 2. 配置 Webhook

每个工作流的 Webhook 节点配置：

```
Webhook URLs:
- Test:  http://localhost:5678/webhook-test/{workflow_id}
- Production: http://localhost:5678/webhook/{workflow_id}
```

### 3. 配置触发器

#### 定时触发 (Cron)
```
Expression: {{ $json.time }}
- 备份检查: @every 1h
- 健康报告: 0 9 * * * (每天 9 点)
```

#### Webhook 触发
```
URL: http://localhost:7789/api/n8n/webhook
Method: POST
Authentication: Header (X-N8N-Key: n8n_webhook_secret_2024)
```

## 核心工作流说明

### 1. 备份检查工作流 (backup_check_workflow)

**用途**: 检查 GID 备份状态

**触发方式**: 
- 定时 (每小时)
- API 调用: `POST /api/n8n/trigger/backup_check`

**流程**:
```
[Webhook/Manual] → [HTTP Request: OPS-CORE BRR API] → [Transform Data] → [Slack/Feishu Notification]
```

**参数**:
- `gid`: GID 号
- `site`: 站点名称

### 2. VM 电源操作工作流 (vm_power_workflow)

**用途**: VM 开机/关机/重启 (需要审批)

**触发方式**: Z-OPS Agent 调用

**流程**:
```
[Webhook] → [IF: 需要审批?] → [Wait for Approval] → [HTTP Request: vCenter API] → [Notification]
                              → [Direct Execute (dev mode)]
```

**参数**:
- `vm_id`: 虚拟机 ID
- `action`: powerOn | powerOff | restart

### 3. 通知工作流 (notification_workflow)

**用途**: 统一通知渠道

**触发方式**: 
- Agent 调用
- 定时任务
- 外部 Webhook

**流程**:
```
[Webhook] → [Switch: Channel] → [Feishu] / [Wecom] / [Email] / [Telegram]
```

**参数**:
- `title`: 通知标题
- `message`: 通知内容
- `channels`: 目标渠道数组

### 4. 健康检查工作流 (health_check_workflow)

**用途**: 定时巡检系统健康状态

**触发方式**: Cron (每天 9:00, 15:00, 21:00)

**流程**:
```
[Cron] → [HTTP: Z-OPS /api/overview] → [Parse Agents Status] → [Format Report] → [Notification]
```

## Z-OPS 集成配置

### 环境变量

在 `n8n.env` 中配置:

```bash
# N8N 服务
N8N_ENABLED=true
N8N_URL=http://localhost:5678
N8N_API_KEY=your_api_key

# Z-OPS 回调
N8N_WEBHOOK_URL=http://localhost:7789/api/n8n/webhook
N8N_WEBHOOK_KEY=n8n_webhook_secret_2024

# 允许触发的工
N8N_TRIGGER_WORKFLOW_IDS=backup_check,vm_power,notification,health_check
```

### API 端点

| 端点 | 方法 | 说明 |
|-----|------|------|
| `/api/n8n/workflows` | GET | 获取工作流列表 |
| `/api/n8n/trigger/{workflow}` | POST | 触发工作流 |
| `/api/n8n/webhook` | POST | N8N 回调 |
| `/api/n8n/status` | GET | N8N 状态 |

## 高级特性

### Human-in-the-loop (人工审批流)

高危操作需要管理员审批:

1. Agent 调用 n8n 触发 VM 关机
2. n8n 发送飞书审批卡片给管理员
3. 管理员点击"同意"或"拒绝"
4. n8n 根据响应继续执行或取消

### 主动运维 (Proactive)

通过告警触发自动诊断:

1. 监控系统产生告警 → n8n Webhook
2. n8n 预处理并提取关键信息
3. n8n 调用 Z-OPS `/api/chat` 触发 ops_agent
4. ops_agent 分析并返回诊断结果
5. n8n 根据结果发送通知或执行自愈

### 定时任务示例

```bash
# 每日健康报告 (早上 9 点)
0 9 * * * curl -X POST http://localhost:5678/webhook/daily_health_report

# 备份状态检查 (每小时)
0 * * * * curl -X POST http://localhost:5678/webhook/backup_check
```

## 故障排查

### 工作流不触发

1. 检查 Webhook URL 是否正确
2. 确认 N8N 已激活工作流 (Activate toggle)
3. 查看 N8N 执行日志

### Z-OPS 无法调用

1. 检查 `N8N_API_KEY` 配置
2. 确认防火墙允许 5678 端口
3. 验证 `core/n8n_integration.py` 连接

### 审批流不工作

1. 检查飞书/企微机器人配置
2. 确认 Webhook URL 可访问
3. 验证审批卡片模板