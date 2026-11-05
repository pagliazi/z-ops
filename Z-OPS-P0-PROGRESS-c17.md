# Z-OPS P0 落地进度 — task-c17 (2026-10-13)

对应 SPEC: `ZOPS_全阶段_架构_落地_task-c17.md`（P0—P3 全阶段计划）。
**P0 全部工作项已落地并部署上线**（2026-10-13 21:02-21:11 分批重启）。
轮换清单见 `Z-OPS-P0-ROTATION-CHECKLIST-c17.md`。

## 基线与回退

- `backups/c17-baseline/`：`HEAD.txt`（git HEAD + stash）、`git-status.txt`（133 项改动快照，先于本次编辑）、`manifest.md5`（8418 文件 hash）、`preedit/`（本次每个被改文件的编辑前副本）。
- 未提交 Git、未 reset/clean、未覆盖用户既有改动。
- 未轮换任何密钥（SPEC 红线：本轮封闭凭据暴露）。

## P0-A 请求隔离与会话访问 ✅

| 修复 | 位置 | 说明 |
|---|---|---|
| 请求上下文 | `core/request_context.py`（新增） | 不可变 `RequestContext`（actor/service_principal/audience/scope/delegation 等，无凭据）+ ContextVar 承载 + token/finally reset + 线程显式传播 `run_with_context` |
| 并发身份错位 | `agents/base.py` `_safe_handle` | 单例 Agent 并发消息在 await 点互相覆盖 `_current_username`（A 的请求记到 B 头上）→ ContextVar 按任务隔离；实例属性仅作兼容回退 |
| 主体丢失 | `agents/base.py` `send_to/send_and_wait` | 新建消息不透传 username/session/channel/父关联 → 下游 write 权限门看到空用户直接放行。现已自动透传 + provenance 带父 request_id |
| 会话归属 | `core/session_access.py`（新增）+ `api/routers/sessions.py`、`chat.py` | `resume_context` 此前**无任何归属校验**（任意用户可读他人会话全文）；`/api/chat` 传入 session_id 无校验（可向他人会话注入消息）。现在读取/追加/restore/update/delete/stats 全部走 `ensure_session_access`（Redis meta → transcript 首条用户消息兜底 → 无法证明即 403） |
| 403 恢复绕过 | `webchat_stable.html` | API.get 非 2xx 显式抛错（带 status）；403 时不再调用 resume_context 重试 |

## P0-B 统一执行与审批 ✅

| 修复 | 位置 | 说明 |
|---|---|---|
| scope 空交集=不限 | `core/bizperms.py` | 个人与组 scope 均非空、交集为空时，旧 `_restrict_merge` 返回 `[]` 被「空=不限」语义吞掉 → 越权放大。现引入 `_modes`（all/scoped/**none**），none 一律拒绝 |
| 资源缺失跳过限制 | `core/bizperms.py` `scope_allows` | 单据缺 site/customer + 决策人 scoped → 旧放行，现拒绝（提示补字段） |
| write 工具无权限门 | `core/bizperms.py` `check_write_allowed` + `WRITE_TOOL_PERM_MAP`；`core/query_engine.py` `enforce_tool_risk` | 工具循环 write 级此前仅记日志放行。现校验权限点+资源范围；无映射（缺授权元数据）/无法确认主体 → 拒绝；admin 不自动获得业务写权限 |
| 高风险绕过开关 | `core/query_engine.py`、`core/approval.py` | `APPROVAL_ENABLED=false` 旧版直接执行 destructive → 现所有配置组合下均不调用 executor（停用时返回拦截+指引） |
| 审批 fail-open | `core/approval.py` `gate_action_message` | 审批门组件异常旧版放行（fail-open）→ 现 fail-closed 拦截并回复原因 |
| 虚报审批单 | `core/approval.py` | Redis 写失败旧版照常返回单号 → 现 `_persist_failed` 标记，gate 明确回复「未登记成功」 |
| 决策并发覆盖 | `core/approval.py` `decide_approval`/`advance_approval_level` | WATCH/MULTI 版本 CAS + `decision_ver` 单调递增；并发决策只有一个生效，终态幂等 |
| RiskLevel 字符串漏判 | `core/tool_schema.py` | ToolDef 不做类型强制，字符串 risk_level 绕过枚举比较 → `__post_init__` 归一化（别名/大小写；未知值→WRITE 倾向进门） |
| 全文引用越权 | `core/query_engine.py` | `_REF_STORE` 全局递增无权限对象，任意会话可 read_ref 他人原文 → 引用绑定 actor+session，跨用户/跨会话/无归属一律拒绝 |

## P0 凭据 ✅（部分）

| 修复 | 位置 | 说明 |
|---|---|---|
| 模型配置密钥回显 | `api/routers/admin.py` | GET `/api/llm/config` 向**任意登录用户**返回原始 api_key/anthropic_key/oneapi_key → 收归管理员；仅返回 `__KEEP__` 掩码 + `has_*` 标志 |
| PATCH 保留语义 | `api/routers/admin.py` POST | 未提交密钥（空/掩码）保留原值；清除必须显式 `clear_keys`；`/api/llm/test` 解析掩码为存量密钥 |

## 测试（全部隔离：独立 Redis 6399、临时目录、fake 接口、无真实外部调用）

- `tests/p0/test_p0_acceptance.py` — **50/50 通过**：并发身份隔离（await 交错不错位）、线程传播、空 scope、跨用户会话/引用、审批存储故障（executor 未调用）、决策 CAS、fail-closed、write 门矩阵、send_to 透传、risk 归一。
- `tests/p0/test_llm_config_mask.py` — **13/13 通过**：非管理员 403、掩码不泄漏原文、PATCH 保留、显式清除。
- 既有白名单回归：`test_approval_gate.py` 26/26、`test_lazy_tools.py` 全过、`test_synthesizer.py` 全过、`test_sidechain.py` 过（首次失败为机器负载下 fsync 计时抖动，复跑 3/3 过）。
- `test_nms_analyzer.py` **排除**：主流程硬编码生产 Redis 6379/0（SPEC 禁止直接全量运行既有测试的原因之一）。
- webchat_api 应用导入验证：170 路由加载正常；agents（vm/kb/orchestrator/general/ops）导入正常。

## P0 剩余项 → 已全部完成（2026-10-13 晚批次）

| 项 | 位置 | 说明 |
|---|---|---|
| 飞书群受众 (P0-D) | `feishu/gateway.py`、`conversation.py`、`core/memory/wiki_query.py`、`synthesizer.py`、`orchestrator.py` | 入口透传 chat_type；群聊 audience=`feishu:group:{chat_id}`（缺失按群 fail-closed）；**私聊显式记忆 visibility=private、群记忆 group+audience，检索前过滤**——修复「私聊『记住…』落全局 wiki 后被注入任何人/群回复」的真实泄漏链；公共经验页不受影响；orchestrator 保存记忆带 audience 并在回复注明可见范围 |
| NMS 服务主体 (P0-E) | `agents/nms_agent.py`、`api/routers/agents.py` | **移除跨用户 SSO 会话借用**（周期任务只认服务账号会话 → zops.env 显式服务凭据）；无会话且无服务凭据 → 暂停并推送原因（4h 去重）；全局 NMS 快照 API 收权（`/api/nms/groups` admin-only、`/api/nms/channels` admin/notify.manage）；会话失效告警恢复指引改为服务账号 |
| n8n 回调签名 (P0-F) | `core/n8n_callback_auth.py`（新增）、`api/routers/n8n.py`、`config.py` | HMAC-SHA256(方法+路径+原始body+ts+nonce)、±5min 窗口、nonce 防重放、事件持久幂等去重（7d）、approval 绑定真实审批单、workflow 绑定 execution_id；**密钥未配置 → 503 暂停不开放匿名回退**；回调主体为 n8n 服务（approver 不再作为自报 username）；`zops.env` 已生成 `N8N_CALLBACK_SECRET`——**未升级签名的 n8n 工作流回调将被 401 拒绝**，升级配方见轮换清单 |
| 硬编码凭据 (P0-G) | 全仓扫描 | 可执行代码（py/sh/yaml）无硬编码凭据字面量（env 受控引用机制已在 `core/secret_ref.py`）；`zops.env.bak-20260805` 含 13+ 真实凭据且全局可读 → 已收 600（内容未动）；轮换清单已出（JWT_SECRET/LLM keys/VCENTER/OPS-CORE/NMS 等 13 项，管理员执行） |

## 部署记录（2026-10-13）

- 流量静默确认（最后用户消息 5 小时前）→ 分批重启：zops-backend（21:02）→ zops-feishu（21:02）→ 10 个 zops-agent@*（21:03-21:11）。zops-frontend 无改动未重启。
- nms_agent 在 stop 阶段因 chromium 子进程超时被 SIGKILL 后未自动拉起 → 手动 `systemctl start` 恢复（心跳 online；后续可考虑 unit 加 `KillMode=control-group`）。
- **生产验证（全部通过）**：13/13 服务 active、0 failed；10 agent 心跳在线；铸临时 token 实测——非管理员 GET llm/config **403**、他人 resume_context/messages/追加消息 **403**、本人 **200**、无归属会话 **403**、未签名 n8n 回调 **401**；首页 HTML 已带新前端代码（no-cache）；端到端对话完整链路正常（start→progress→delta×98→reply→done）；飞书 wss 长连接 21:02:57 重建成功；NMS 监控循环已启用（interval=1800s）。

## 测试汇总（隔离 Redis 6399，已关闭）

- `tests/p0/test_p0_acceptance.py` — 50/50
- `tests/p0/test_llm_config_mask.py` — 13/13
- `tests/p0/test_p0_def.py` — 30/30（D/E/F）
- 既有白名单回归：`test_approval_gate.py` 26/26、`test_lazy_tools.py`、`test_synthesizer.py` 全过、`test_sidechain.py` 过（计时抖动复跑 3/3）。
- `test_nms_analyzer.py` 排除（硬编码生产 Redis）。

## 遗留与下一步

1. **n8n 工作流签名升级**（3 个工作流引用回调 URL；未升级前相关回调暂停——按 SPEC 预期行为）。
2. **凭据轮换**：按 `Z-OPS-P0-ROTATION-CHECKLIST-c17.md` 执行（13 项，管理员）。
3. 浏览器截图级验证（SPEC 部署项的补充证据；API 级已覆盖同路径）。
4. P0 验收后进入 **P1**：Redis Streams 任务运行时（`core/task_runtime/`）+ 原子任务账本/Outbox + JSONL 永久归档统一 + SSE 游标兼容迁移。

## 行为变更提示（灰度时关注）

- scoped 审批人/申请人遇到缺 site/customer 的单据会被拒（提示补字段）——SPEC 要求的 fail-closed，注意存量单据习惯。
- `/api/llm/config` 非管理员从可读变为 403（admin 页正常）。
- `APPROVAL_ENABLED=false` 不再是 destructive 直通开关。
- 前端 API.get 对非 2xx 抛错——所有调用点已有 catch，个别空态提示更明确。
- **私聊「记住: …」的记忆不再进入他人/群检索**（本人可见；群聊记忆仅该群可见）。
- **NMS 周期任务不再借用任意登录用户的会话**；服务账号无会话且 zops.env 无密码时会暂停并推送原因。
- **n8n 回调必须带签名**；未升级的工作流回调被 401/503 拒绝（升级配方见轮换清单）。
- `/api/nms/groups`（admin 页配置用）收为 admin-only；`/api/nms/channels` 收为 admin/notify.manage。
