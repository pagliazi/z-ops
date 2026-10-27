# Z-OPS 运维智能体系统架构

> 更新时间: 2026-04-17
> 版本: v2.2.0 (Self-Improving Advanced)

---

## 一、系统概览

Z-OPS (Operation Claw) 是一个基于 LLM 的智能运维辅助系统，通过 AI Agent 集群实现运维自动化、知识问答、和平台数据集成。

### v2.2.0 Self-Improving Advanced 核心能力

在 v2.1.0 Hermes Enhancement 基础上，重点升级了自我进化子系统：

| 能力 | 模块 | 说明 |
|------|------|------|
| **根因分析** | `core/self_improving.py` | 9种错误分类 + 规则匹配 + LLM深度分析 |
| **LLM 反思** | `core/self_improving.py` | 连续失败自动触发 LLM 深度反思 |
| **经验衰减** | `core/self_improving.py` | 指数衰减，7天半衰期，近期经验权重更高 |
| **跨技能迁移** | `core/self_improving.py` | 基于标签匹配的相似技能经验共享 |
| **执行规划** | `core/self_improving.py` | 基于历史的预执行规划与风险评估 |
| **策略自适应** | `core/self_improving.py` | 动态选择历史最优执行策略 |
| **LLM 改进生成** | `core/harness/skill_improver.py` | LLM 驱动的技能改进方案生成 |
| **A/B 测试** | `core/harness/skill_improver.py` | 改进前后指标快照对比验证 |
| **自动回滚** | `core/harness/skill_improver.py` | 改进后指标恶化自动回滚 |

```
┌─────────────────────────────────────────────────────────────────┐
│              Self-Improving v2.2 架构                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐   ┌────────────┐   ┌─────────────┐      │
│  │ BaseSkill   │   │  执行规划  │   │ 策略自适应 │      │
│  │ (v2.2)     │──▶│  Planning │──▶│  Strategy  │      │
│  └─────────────┘   └────────────┘   └─────────────┘      │
│         │                                                │
│         ▼                                                │
│  ┌───────────────────────────────────────────┐      │
│  │        SelfImprovingEngine v2.2              │      │
│  ├───────────────────────────────────────────┤      │
│  │ RootCauseAnalyzer  │ ReflectionEngine       │      │
│  │ (根因分析)         │ (LLM 反思)              │      │
│  ├───────────────────────────────────────────┤      │
│  │ ExperienceDB       │ Strategy Scores        │      │
│  │ (时间衰减+指纹)    │ (策略自适应)            │      │
│  ├───────────────────────────────────────────┤      │
│  │ CrossSkillTransfer │ ExecutionPlanner       │      │
│  │ (跨技能迁移)       │ (执行规划)              │      │
│  └───────────────────────────────────────────┘      │
│         │                                                │
│         ▼                                                │
│  ┌───────────────────────────────────────────┐      │
│  │        SkillImprover v2.2                   │      │
│  ├───────────────────────────────────────────┤      │
│  │ LLM 驱动改进     │ A/B 测试验证            │      │
│  │ 自动回滚         │ 指标快照对比            │      │
│  └───────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

---

## 二、核心组件

### 2.1 Agent 集群

| Agent | 功能描述 | 状态 | 技能 |
|-------|---------|------|------|
| **orchestrator** | 意图识别 & 任务路由 | 🟢 online | route, intent_classify |
| **general_agent** | 通用问答 / 知识库检索 | 🟢 online | chat, doc_gen, summarize, rag |
| **ops_agent** | 系统巡检 / 故障诊断 | 🟢 online | host_check, log_analyze, batch_exec, health_report |
| **network_agent** | 网络运维 / 连通性检测 | 🟢 online | ping_sweep, port_scan, route_trace, config_backup |
| **vm_agent** | 虚拟化管理 (VMware/KVM/PVE) | 🟢 online | vm_list, vm_power, vm_snapshot, vm_migrate |
| **dev_agent** | 脚本开发 / SSH 批量执行 | 🟢 online | script_gen, script_exec, ansible_run |
| **browser_agent** | 浏览器自动化 | 🟢 online | web_screenshot, web_action, browser_attach |
| **gcoc_agent** | GCOC 平台数据集成 | 🟢 online | cmdb, dns, backup, snapshot, ha, overview, brr, smartops |
| **kb_agent** | 知识库 RAG | 🟢 online | search, chat, rag_enhance |

### 2.2 消息通道

| 通道 | 状态 | 说明 |
|-----|------|------|
| **webchat** | 🟢 online | Web 聊天界面 (Next.js :3000) |
| **feishu** | 🟢 online | 飞书机器人 |
| **wecom** | 🟢 online | 企业微信机器人 |

### 2.3 外部集成

| 服务 | 状态 | 说明 |
|-----|------|------|
| **Redis** | 🟢 online | 状态存储 |
| **LLM** | 🟢 online | DeepSeek API (多模型支持) |
| **N8N** | 🟢 online | 工作流自动化 (:5678) - 执行中枢 |
| **LDAP/AD** | 🟢 online | AD 域认证 (198.19.5.191:3268) |
| **GCOC API** | 🟢 online | 信息管理系统 + 资源管理平台 (117端点) |
| **KB API** | 🟢 online | 知识库 RAG |

### 2.4 N8N 集成 (执行中枢)

N8N 在 Z-OPS 架构中扮演三大角色：

| 角色 | 功能 | 示例 |
|:---|:---|:---|
| **执行器 (Tool)** | Agent 将复杂操作外包给 n8n | VM 电源操作、备份检查 |
| **触发器 (Trigger)** | 告警/定时任务主动唤醒 Agent | 每日健康报告、告警诊断 |
| **审批流 (Human-in-loop)** | 高危操作需要人工审批 | 关机/重启/创建用户 |

**N8N 工作流列表**:
- `vm_power_workflow`: VM 电源操作 (需审批)
- `backup_check_workflow`: 备份状态检查
- `health_check_workflow`: 定时健康检查
- `notification_workflow`: 统一通知渠道
- `ad_user_workflow`: AD 用户管理 (需审批)

---

## 三、前端架构 (Next.js)

```
web-next/
├── src/
│   ├── app/                    # 页面路由
│   │   ├── page.tsx            # 对话首页
│   │   ├── admin/page.tsx      # 管理后台
│   │   ├── agents/page.tsx     # Agent集群
│   │   ├── logs/page.tsx       # 运维日志
│   │   ├── usage/page.tsx      # LLM用量
│   │   ├── n8n/page.tsx       # N8N工作流
│   │   └── layout.tsx          # 根布局
│   │
│   ├── components/             # UI组件
│   │   ├── Dashboard.tsx       # 主仪表盘
│   │   ├── Header.tsx          # 顶部导航
│   │   ├── SessionList.tsx     # 会话列表
│   │   ├── ChatMessage.tsx     # 消息组件
│   │   ├── ChatInput.tsx       # 输入框
│   │   └── LoginPage.tsx       # 登录页
│   │
│   └── lib/
│       └── api.ts              # API客户端
│
└── next.config.ts              # Next.js 配置
```

### 页面功能

| 页面 | 路径 | 功能 |
|-----|------|------|
| 对话 | `/` | AI 对话界面 |
| Agent集群 | `/agents` | 架构可视化 |
| 运维日志 | `/logs` | 日志查询 |
| LLM用量 | `/usage` | 调用统计 |
| 管理后台 | `/admin` | 用户/模型/脱敏 |
| N8N | `/n8n` | 工作流管理 |

---

### 4.2 N8N 集成架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Z-OPS + N8N 集成架构                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌─────────────┐    技能调用    ┌─────────────┐               │
│   │  Agent集群  │ ─────────────► │   N8N       │               │
│   │ (脑力劳动)  │               │  (执行中枢) │               │
│   └──────┬──────┘               └──────┬──────┘               │
│          │                             │                       │
│          │    ┌────────────────────────┼──────────────┐        │
│          ▼    ▼                        ▼               ▼        │
│   ┌─────────────┐              ┌─────────────┐ ┌─────────┐    │
│   │  LLM        │              │ 标准化工作流 │ │ 触发器  │    │
│   │  意图识别   │              │(VM/GCOC/AD) │ │(告警/定时)│    │
│   └─────────────┘              └─────────────┘ └─────────┘    │
│                                              │                │
│                                              ▼                │
│                                       ┌─────────────┐         │
│                                       │  外部系统   │         │
│                                       │(VM/GCOC/DNS)│         │
│                                       └─────────────┘         │
└─────────────────────────────────────────────────────────────────┘
```

**N8N 角色定位**:
- **执行器 (Tool)**: Agent 将复杂跨系统操作外包给 n8n
- **触发器 (Trigger)**: 告警/定时任务主动唤醒 Agent
- **审批流 (Human-in-loop)**: 高危操作需要人工审批

### 4.3 核心模块

```
zops/
├── agents/                    # Agent 实现
│   ├── base.py               # Agent 基类
│   ├── orchestrator.py       # 任务编排器
│   ├── general_agent.py      # 通用问答
│   ├── ops_agent.py          # 运维专家
│   ├── network_agent.py      # 网络专家
│   ├── vm_agent.py           # 虚拟化专家
│   ├── gcoc_agent.py         # GCOC 集成
│   ├── kb_agent.py           # 知识库
│   ├── browser_agent.py      # 浏览器自动化
│   └── dev_agent.py          # 开发助手
│
├── core/                      # 核心引擎
│   ├── router.py             # 消息路由
│   ├── registry.py           # Agent 注册
│   ├── skill_base.py         # 技能基类
│   ├── skill_registry.py     # 技能注册
│   ├── sandbox.py            # 沙箱隔离
│   ├── browser_session.py    # 浏览器会话
│   ├── secret_ref.py         # 敏感配置
│   ├── self_improving.py     # 自我进化
│   │
│   ├── memory/               # [v2.1] 跨会话记忆系统
│   │   ├── cross_session_memory.py  # 跨会话记忆
│   │   ├── user_profile.py          # Honcho 用户画像
│   │   ├── session_search.py        # FTS5 全文搜索
│   │   └── memory_nudge.py          # 周期性记忆推送
│   │
│   ├── harness/              # Harness Engineering
│   │   ├── skill_schema.py          # 技能数据模型
│   │   ├── skill_registry.py        # 技能注册表
│   │   ├── skill_generator.py       # 技能生成器
│   │   ├── skill_improver.py        # [v2.1] 技能改进器
│   │   ├── memory.py                # Harness 记忆
│   │   ├── n8n_bridge.py            # N8N 桥接
│   │   ├── n8n_skill.py             # N8N 技能封装
│   │   └── scheduler.py             # 任务调度
│   │
│   ├── delegation/           # [v2.1] 子代理委派
│   │   ├── subagent_manager.py      # 子代理管理器
│   │   ├── rpc_bridge.py            # RPC 桥接
│   │   └── parallel_executor.py     # 并行执行器
│   │
│   ├── backends/             # [v2.1] 多终端后端
│   │   ├── backend_factory.py       # 后端工厂
│   │   ├── local_backend.py         # 本地执行
│   │   ├── docker_backend.py        # Docker 执行
│   │   ├── ssh_backend.py           # SSH 远程执行
│   │   └── modal_backend.py         # Modal 无服务器
│   │
│   └── training/             # [v2.1] RL 训练数据
│       └── trajectory_recorder.py   # 轨迹记录器
│
├── channels/                  # 消息通道
│   ├── feishu.py            # 飞书
│   ├── wecom.py             # 企业微信
│   └── telegram.py          # Telegram
│
├── skills/                    # 技能实现
│   ├── ops/                 # 运维技能
│   ├── network/             # 网络技能
│   ├── gcoc/                # GCOC技能
│   ├── vm/                  # 虚拟化技能
│   ├── dev/                 # 开发技能
│   ├── browser/             # 浏览器技能
│   ├── kb/                  # 知识库技能
│   ├── general/             # 通用技能
│   └── zops_generated/       # 自动生成的技能
│
├── workspaces/               # Agent 工作空间
│   ├── orchestrator/
│   ├── general_agent/
│   ├── ops_agent/
│   └── ...
│
├── webchat_api.py           # FastAPI 服务
├── config.py                # 配置管理
└── agents.yaml              # Agent 声明配置
```

### 4.2 API 端点

| 路径 | 方法 | 功能 |
|-----|------|------|
| `/api/auth/login` | POST | 用户登录 |
| `/api/auth/users` | GET | 用户列表 |
| `/api/auth/register` | POST | 注册用户 |
| `/api/auth/authorize-ad` | POST | 授权AD用户 |
| `/api/chat/sessions` | GET/POST | 会话管理 |
| `/api/chat/messages/{id}` | GET | 消息历史 |
| `/api/chat` | POST | 发送消息(SSE) |
| `/api/overview` | GET | 系统概览 |
| `/api/agents/detail` | GET | Agent详情 |
| `/api/llm/config` | GET/POST | LLM配置 |
| `/api/usage` | GET | 用量统计 |
| `/api/daily-log` | GET | 运维日志 |
| `/api/audit/recent` | GET | 审计日志 |
| `/api/sanitize/*` | GET/POST | 数据脱敏 |
| `/api/oneapi/keys` | GET/POST | API Keys |
| `/api/n8n/*` | GET/POST | N8N集成 |
| `/api/n8n/trigger/{workflow}` | POST | 触发工作流 |
| `/api/n8n/webhook` | POST | Webhook回调 |
| `/api/health` | GET | 健康检查 |

---

## 五、数据流

### 5.1 用户消息处理流程

```
用户输入
    │
    ▼
┌─────────────────┐
│  Web/FeiShu/   │  消息入口
│  Wecom         │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  webchat_api   │  认证 + 会话管理
│  (:7789)       │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Orchestrator  │  意图识别 + 路由
│  Agent         │
└────────┬────────┘
         │
    ┌────┴────┬────────┬────────┐
    ▼         ▼        ▼        ▼
┌───────┐ ┌───────┐ ┌───────┐ ┌──────┐
│ Ops   │ │Network│ │ GCOC  │ │ ...  │
│ Agent │ │ Agent │ │ Agent │ │      │
└───┬───┘ └───┬───┘ └───┬───┘ └──┬───┘
    │         │         │         │
    └─────────┴────┬────┴─────────┘
                   │
                   ▼
            ┌─────────────┐
            │  Redis      │  持久化
            │  状态存储   │
            └─────────────┘
                   │
                   ▼
            ┌─────────────┐
            │  SSE       │  流式响应
            │  返回结果  │
            └─────────────┘
```

### 5.2 Redis 数据结构

```
zops:users                    # 用户列表 (Hash)
zops:token:{token}            # Token映射 (String)
zops:session:{id}             # 会话元数据 (Hash)
zops:messages:{id}            # 会话消息 (List)
zops:sessions:{user}          # 用户会话列表 (ZSet)
zops:heartbeats              # Agent心跳 (Hash)
zops:channel_heartbeats       # 通道心跳 (Hash)
zops:chat_history            # 聊天历史 (List)
zops:llm_usage               # 用量统计 (List)
zops:llm_pref                # LLM配置 (String)
zops:daily_log:{date}        # 每日日志 (List)
zops:user_ops:{date}         # 用户操作日志 (List)
```

---

## 六、认证与安全

### 6.1 认证方式

| 方式 | 状态 | 说明 |
|-----|------|------|
| **本地认证** | 🟢 | 用户名+密码 (SHA256) |
| **LDAP/AD** | 🟢 | AD 域账号认证 |
| **Bearer Token** | 🟢 | JWT Token (24h有效) |
| **API Key** | 🟢 | OneAPI 兼容 |

### 6.2 当前用户

```
总用户数: 9
├── 本地用户: admin (管理员), temp (临时用户)
└── AD域用户: 
    ├── svc-gcoc (Cyril Zhong) - 管理员
    ├── user-2141 (User A) - 已授权
    ├── user-1069 (User B) - 已授权
    ├── user-1744, user-2136, user-2002, user-1983 - 待授权
```

---

## 七、部署配置

### 7.1 服务端口

| 服务 | 端口 | 说明 |
|-----|------|------|
| webchat_api | 7789 | FastAPI 后端 |
| web-next | 3000 | Next.js 前端 |
| N8N | 5678 | 工作流引擎 |
| Redis | 6379 | 状态存储 |

### 7.2 环境配置

```
# 核心配置 (zops.env)
APP_NAME=Z-OPS
REDIS_PREFIX=zops
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat

# 认证配置
WEBCHAT_AUTH_USER=admin
WEBCHAT_AUTH_PASS=zops_admin_2024

# LDAP 配置
LDAP_ENABLED=true
LDAP_SERVER_URI=ldap://198.19.5.191:3268
```

---

## 八、监控与运维

### 8.1 健康检查

```bash
# API 健康检查
curl http://localhost:7789/api/health

# Agent 状态
curl http://localhost:7789/api/overview -H "Authorization: Bearer {token}"
```

### 8.2 日志位置

```
zops/logs/
├── orchestrator.log      # 编排器日志
├── gcoc_agent.log        # GCOC Agent
├── ops_agent.log         # 运维 Agent
├── network_agent.log     # 网络 Agent
├── vm_agent.log          # 虚拟机 Agent
├── webchat.log           # Web服务
├── frontend_stdout.log   # 前端日志
└── audit/                # 审计日志
```

---

## 九、版本信息

| 组件 | 版本 | 说明 |
|-----|------|------|
| Z-OPS | v2.2.0 | Self-Improving Advanced |
| Next.js | 16.2.1 | React 19 前端 |
| FastAPI | 0.110+ | Python 后端 |
| Redis | 7.x | 状态存储 |
| N8N | 1.x | 工作流引擎 |
| SQLite FTS5 | 3.x | 全文搜索 |

---

## 十、相关文档

- [Z-OPS v2.0 更新总结](./Z-OPS_v2_Summary.md)
- [Z-OPS 升级指南](./Z-OPS_v2_Upgrade_Guide.md)
- [快速开始指南](./QUICKSTART.md)
- [服务部署指南](./SERVICES_GUIDE.md)
- [Agent 配置](./agents.yaml)
- [N8N 设计架构](./n8n设计系统架构.md)

---

## 十一、v2.1.0 新增模块详解

### 11.1 跨会话记忆系统 (core/memory/)

参考 Hermes Agent 的 Honcho 用户建模，实现跨会话知识持久化。

| 模块 | 文件 | 功能 |
|------|------|------|
| 跨会话记忆 | `cross_session_memory.py` | 会话摘要存储、历史检索 |
| 用户画像 | `user_profile.py` | Honcho dialectic 用户建模 |
| 全文搜索 | `session_search.py` | SQLite FTS5 会话搜索 |
| 记忆推送 | `memory_nudge.py` | 周期性记忆提醒 |

**核心类**:
- `CrossSessionMemory`: 跨会话记忆管理
- `HonchoProfileManager`: 用户画像管理 (dialectic notes)
- `SessionSearch`: FTS5 全文索引搜索
- `MemoryNudgeSystem`: 智能记忆推送

**使用示例**:
```python
from core.memory import get_cross_session_memory, get_profile_manager

# 存储会话摘要
memory = await get_cross_session_memory()
await memory.store_session_summary(
    session_id="sess_001",
    user_id="user_001",
    agent_id="ops_agent",
    summary="执行了服务器健康检查",
    key_learnings=["需要检查网络连通性"]
)

# 搜索历史会话
results = await memory.search_past_sessions("健康检查")

# 获取用户画像
profile_manager = await get_profile_manager()
prompt = await profile_manager.get_personalized_prompt("user_001", "ops_agent")
```

### 11.2 技能自我改进循环 (core/harness/skill_improver.py)

实现从"技能生成"到"生成 + 反馈 + 改进"的完整闭环。

**改进触发条件**:
1. 成功率低于 80%
2. 相同错误重复 3 次
3. 用户负面反馈达到阈值

**核心类**:
- `SkillImprover`: 技能改进器
- `Feedback`: 使用反馈记录
- `UsageAnalysis`: 使用模式分析
- `Improvement`: 改进建议

**工作流程**:
```
失败记录 → Feedback → 分析触发 → Improvement 生成 → 应用改进 → 版本升级
```

**使用示例**:
```python
from core.harness import get_skill_improver

improver = await get_skill_improver()

# 记录失败反馈
await improver.record_feedback(
    skill_name="vm_backup_check",
    session_id="sess_001",
    user_id="user_001",
    success=False,
    error_type="timeout",
    error_message="VMware API 连接超时"
)

# 获取待处理改进
pending = await improver.get_pending_improvements("vm_backup_check")

# 应用改进
await improver.apply_improvement(pending[0].improvement_id)
```

### 11.3 子代理委派机制 (core/delegation/)

实现 Hermes Agent 的 spawn subagents + RPC 调用机制。

| 模块 | 功能 |
|------|------|
| `subagent_manager.py` | 子代理创建、隔离、生命周期管理 |
| `rpc_bridge.py` | RPC 通信桥接 |
| `parallel_executor.py` | 并行任务执行器 |

**核心类**:
- `SubagentManager`: 子代理管理器
- `SubagentHandle`: 子代理句柄
- `RPCBridge`: RPC 通信桥接
- `ParallelExecutor`: 并行执行器

**使用示例**:
```python
from core.delegation import get_subagent_manager, get_parallel_executor

# 创建子代理
manager = await get_subagent_manager()
handle = await manager.spawn_subagent(
    task="检查网络连通性",
    agent_type="network_agent",
    isolated=True
)

# 并行执行多个任务
executor = get_parallel_executor()
results = await executor.execute_parallel([
    ParallelTask(task_id="t1", task_func=check_network, args=("192.168.1.1",)),
    ParallelTask(task_id="t2", task_func=check_vm, args=("vm-001",)),
])
```

### 11.4 多终端后端 (core/backends/)

支持多种执行环境，统一接口调用。

| 后端 | 功能 | 适用场景 |
|------|------|----------|
| `LocalBackend` | 本地执行 | 开发测试 |
| `DockerBackend` | Docker 容器执行 | 隔离环境 |
| `SSHBackend` | SSH 远程执行 | 远程服务器 |
| `ModalBackend` | Modal 无服务器 | 弹性计算/GPU |

**使用示例**:
```python
from core.backends import get_backend

# 本地执行
local = get_backend("local")
result = await local.execute("ls -la", timeout=30)

# Docker 执行
docker = get_backend("docker", {"image": "python:3.11"})
result = await docker.execute("python -c 'print(1+1)'")

# SSH 执行
ssh = get_backend("ssh", {"host": "192.168.1.100", "username": "root"})
result = await ssh.execute("systemctl status nginx")
```

### 11.5 agentskills.io 标准兼容

技能格式兼容开放标准，便于技能共享。

**标准字段**:
```yaml
name: vm_backup_check
version: 1.0.0
description: 检查虚拟机备份状态
author: zops-system
platforms: [linux, vmware]

when_to_use: |
  当需要检查虚拟机备份状态时
procedure: |
  1. 连接 VMware API
  2. 查询备份任务状态
  3. 返回结果摘要
pitfalls: |
  - 注意 API 超时问题
verification: |
  确认返回了备份状态列表

# Z-OPS 扩展
metadata:
  zops:
    tags: [vmware, backup]
    category: ops
    n8n_workflow: backup_check_workflow
```

**导出格式**:
```python
from core.harness.skill_schema import Skill

skill = Skill.from_yaml(yaml_content)

# 导出为 agentskills.io 格式
agentskills_data = skill.to_agentskills_io()

# 导出为 OpenAI Tools 格式
openai_tool = skill.to_openai_tool()
```

### 11.6 RL 训练数据生成 (core/training/)

支持 Atropos RL 环境和轨迹压缩。

**核心类**:
- `TrajectoryRecorder`: 轨迹记录器
- `Trajectory`: 执行轨迹
- `CompressedTrajectory`: 压缩轨迹

**使用示例**:
```python
from core.training import get_trajectory_recorder

recorder = get_trajectory_recorder()

# 开始轨迹
traj_id = recorder.start_trajectory(
    session_id="sess_001",
    user_id="user_001",
    agent_id="ops_agent",
    task="执行服务器健康检查"
)

# 记录步骤
recorder.record_step(
    trajectory_id=traj_id,
    action="host_check",
    action_type="tool_call",
    skill_used="host_check",
    success=True
)

# 完成轨迹
trajectory = recorder.complete_trajectory(
    trajectory_id=traj_id,
    success=True,
    final_response="服务器状态正常"
)

# 导出为 Atropos 格式
atropos_data = recorder.export_for_atropos(trajectory)
```

### 11.7 Self-Improving Advanced (v2.2.0)

v2.2 重点升级自我进化子系统，从简单的经验记录+规则匹配，进化为具备**深度思考能力**的智能系统。

#### 11.7.1 根因分析器 (RootCauseAnalyzer)

9 种结构化错误分类，三层分析（规则→历史→LLM）：

| 分类 | 说明 | 触发关键词 |
|------|------|------------|
| `timeout` | 超时 | timeout, timed out, deadline |
| `connection` | 连接失败 | refused, unreachable, network |
| `auth` | 认证/权限 | permission, denied, 401, 403 |
| `param_invalid` | 参数错误 | invalid, missing, validation |
| `resource` | 资源不存在 | not found, 404 |
| `dependency` | 依赖故障 | 502, 503, upstream |
| `rate_limit` | 限流 | rate limit, 429, throttle |
| `data` | 数据异常 | decode, parse, json |
| `logic` | 业务逻辑 | 自定义匹配 |

```python
from core.self_improving import get_improving_engine

engine = get_improving_engine()
rca = engine.rca

# 快速分析 (规则+历史)
result = rca.analyze("Connection refused to 192.168.1.100:443")
print(result.category)       # ErrorCategory.CONNECTION
print(result.suggested_fix)  # "检查网络连通性和目标服务状态"

# LLM 深度分析 (可选)
result = await rca.deep_analyze(
    error_message="VMware API timeout after 30s",
    skill_name="vm_backup_check",
    input_params={"host": "vcenter.local"},
    llm_func=my_llm_call
)
```

#### 11.7.2 LLM 反思引擎 (ReflectionEngine)

连续失败 ≥3 次自动触发 LLM 深度反思：

```python
engine = get_improving_engine()

# 自动触发 (连续失败 >= 3 次)
reflection = await engine.maybe_reflect("vm_backup_check")
if reflection:
    print(reflection.summary)          # "VMware API 连接不稳定"
    print(reflection.what_to_improve)   # "增加重试机制和超时配置"
    print(reflection.pattern_detected)  # "网络层面间歇性故障"

# 手动触发
result = await engine.reflector.reflect_on_failure(
    skill_name="vm_backup_check",
    recent_failures=failures_list,
    recent_successes=successes_list
)
```

#### 11.7.3 执行规划与策略自适应

```python
engine = get_improving_engine()

# 生成执行规划
plan = engine.plan_execution("vm_backup_check", {"host": "vcenter.local"})
print(plan.strategy)          # "retry_with_backoff"
print(plan.confidence)        # 0.72
print(plan.risk_level)        # "medium"
print(plan.warnings)          # ["⚠️ 已连续失败 2 次", "💡 历史最常见错误: timeout"]
print(plan.param_adjustments) # {"timeout": 45}  (自动建议增加超时)
print(plan.fallback_strategy) # "direct_api"  (备选策略)

# 策略分数查看
scores = engine.get_strategy_scores("vm_backup_check")
# {"default": 0.45, "retry_with_backoff": 0.78, "direct_api": 0.62}
```

**策略自适应算法**: 指数移动平均 (EMA)
```
new_score = old_score × 0.8 + (0.5 + reward × 0.25) × 0.2
reward = +1.0 (成功) / -1.0 (失败) / +0.2 (快速执行奖励)
```

#### 11.7.4 跨技能迁移学习

基于标签匹配的相似技能经验共享：

```python
engine = get_improving_engine()

# 注册技能标签
engine.register_skill_tags("vm_backup_check", ["vmware", "backup", "api"])
engine.register_skill_tags("vm_snapshot", ["vmware", "snapshot", "api"])
engine.register_skill_tags("gcoc_backup", ["gcoc", "backup", "api"])

# 获取迁移洞察
insights = engine.get_transfer_insights("vm_backup_check", error_category="timeout")
# ["[迁移自 vm_snapshot] 修复建议: 增加超时时间或检查目标服务响应",
#  "[迁移自 gcoc_backup] 有效策略: retry_with_backoff"]
```

#### 11.7.5 A/B 测试与自动回滚

```python
from core.harness import get_skill_improver
improver = await get_skill_improver()

# 应用改进 (自动记录 pre_apply_metrics 快照)
await improver.apply_improvement("imp_abc123")

# 后续每次反馈自动触发验证检查
# 10 次执行后自动比较前后指标:
# - improved:  成功率提升 ≥5%  → ✅ 验证通过
# - degraded:  成功率下降 >10% → ❌ 自动回滚
# - neutral:   变化不大         → ➖ 保持观察

# 查看改进历史
history = await improver.get_skill_history("vm_backup_check")
print(history["improved_count"])   # 已验证有效的改进数
print(history["degraded_count"])   # 已回滚的改进数
```

#### 11.7.6 增强的 BaseSkill 钩子

```python
from core.skill_base import BaseSkill, SkillResult

class MySkill(BaseSkill):
    name = "my_custom_skill"
    description = "自定义技能"
    skill_tags = ["network", "check"]      # 启用跨技能迁移
    enable_reflection = True                # 启用 LLM 反思
    enable_param_adjustment = True          # 启用参数自调整

    async def execute(self, params, context=None):
        # self._current_strategy 已自动设置为历史最优策略
        # self.confidence_score 已自动计算
        return SkillResult(success=True, text="done")

# 推荐使用 execute_with_learning 调用
result = await skill.execute_with_learning(params)
# 自动流程:
# 1. on_before_execute → 规划 + 策略 + 参数调整 + 迁移洞察
# 2. execute → 实际执行
# 3. on_after_execute → 经验记录 + 根因分析 + 反思触发
```

**Self-Improving 完整数据流**:

```
┌─────────────────────────────────────────────────────────────┐
│                Self-Improving v2.2 数据流                    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  [执行前]                                                  │
│  params ──▶ ExecutionPlanner ──▶ plan.strategy              │
│            ├── 历史统计查询      ├── 参数自调整              │
│            ├── 策略自适应        ├── 风险评估                │
│            └── 跨技能迁移        └── 信心评估                │
│                                                             │
│  [执行中]                                                  │
│  skill.execute(adjusted_params)                             │
│                                                             │
│  [执行后]                                                  │
│  result ──▶ ExperienceDB ──▶ 持久化                        │
│            ├── RootCauseAnalyzer (根因分析)                 │
│            ├── 策略分数更新 (EMA)                           │
│            └── 连续失败? ──▶ ReflectionEngine (LLM反思)     │
│                                                             │
│  [改进闭环]                                                │
│  SkillImprover ──▶ LLM改进生成 ──▶ 应用改进                │
│                    ├── 规则回退      ├── 指标快照            │
│                    └── 反思联动      └── A/B验证 → 回滚?    │
└─────────────────────────────────────────────────────────────┘
```

#### 11.7.7 经验衰减机制

采用指数衰减算法，7天半衰期：

```
decay_factor = exp(-0.693 × age / HALF_LIFE)
HALF_LIFE = 7 × 24 × 3600 (7天)
```

| 经验年龄 | 衰减因子 | 说明 |
|----------|----------|------|
| 0天 | 1.00 | 最新经验，权重最高 |
| 7天 | 0.50 | 半衰期，权重减半 |
| 14天 | 0.25 | 两个半衰期 |
| 30天 | 0.05 | 几乎不影响决策 |

#### 11.7.8 多维信心评估

```
confidence = base_rate + trend_factor + sim_bonus - fail_penalty + strategy_bonus

其中:
- base_rate     = 整体成功率
- trend_factor  = +0.05 (上升趋势) / -0.10 (下降趋势)
- sim_bonus     = (相似经验成功率 - 0.5) × 0.15
- fail_penalty  = min(0.3, consecutive_failures × 0.08)
- strategy_bonus = (best_strategy_score - 0.5) × 0.1
```

---

## 十二、快速验证

运行验证脚本检查所有新模块：

```bash
cd /opt/zops
python3 verify_hermes_enhancement.py
```

预期输出：
```
============================================================
Z-OPS v2.1.0 Hermes Enhancement 功能验证
============================================================
[1] 跨会话记忆系统测试         ✓ 通过
[2] Honcho 用户画像测试       ✓ 通过
[3] FTS5 全文搜索测试         ✓ 通过
[4] 记忆推送系统测试          ✓ 通过
[5] 技能自我改进测试          ✓ 通过
[6] 子代理委派测试            ✓ 通过
[7] 并行执行器测试            ✓ 通过
[8] 多终端后端测试            ✓ 通过
[9] RL 训练数据生成测试       ✓ 通过
[10] agentskills.io 兼容测试  ✓ 通过

总计: 10/10 测试通过
============================================================
```