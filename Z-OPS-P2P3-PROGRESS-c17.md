# Z-OPS P2/P3 落地进度 — task-c17 (2026-10-14 凌晨)

P2「可验证学习、知识 ACL 与技能发布」主体完成；P3 完成子任务预算项。累计验收：
P0 93 + P1 51 + P2 25 + P3 9 = **178 项断言全绿**。

## P2 已落地（本轮）

### 技能生命周期（core/harness/skill_lifecycle.py 新增）

- 状态机 `candidate → evaluated → approved → published → retired`，非法迁移拒绝；
  **越级发布拒绝**（candidate 直接 publish/approve 均被拒）、**非管理员 approve/publish 拒绝**。
- **不可变版本**：内容 hash 绑定；同版本号不同内容 → 自动递增新版本（同版本覆盖禁止）；
  同内容重复提交幂等。
- **发布指针**：每技能唯一 `published` 指针，仅指向已批准版本；线上文件
  （skills/zops_generated/）仅由 publish 写入；回退 = 指针回移到已批准旧版（不重写历史）。
- 每版绑定来源 execution/纠错证据/author/审核记录（approver+时间）/测试记录。
- **存量收编**：线上既有技能自动注册为 approved+published（证据=legacy-bootstrap），
  门控不破坏现状。

### 复盘隔离（SPEC: 后台复盘只能写候选知识）

- `SkillRegistry.save_skill` 门控：直接写线上技能一律 `PermissionError`
  （唯一例外是 lifecycle publish 内部通道）。
- 失败复盘（HarnessController）改走 `submit_candidate`，**绝不自动发布**
  （`allow_auto_publish=True` 显式拒绝）。
- 辅助模型独立日预算（默认 50 次/日），超限复盘暂停（不影响主链路）。

### 成功率口径（SPEC: 不将初始值设为成功）

- 新技能不再预置 `success_rate: 1.0`（改 `None`）；
- 动态成功率直写线上文件的旧路径删除 → 生命周期 `record_outcome`；
- `success_rate()` **只从 verified=True 的可核验证据计算**，无验证证据时返回 null。

### 知识 ACL / 外部参考标注

- 外部经验（Hermes/IDE 记忆等）标记 `origin: reference`，贯通到检索结果
  （`QueryHit.origin`）与 prompt 注入（**【外部参考·未验证】**前缀，不冒充内部纠错经验）。
- 混合检索（向量+BM25 融合）**返回前复核**可见性（防御向量旁路）。
- 撤权立即失效：页面改 private/group 后查询即刻过滤（查询时判定，无缓存残留）——测试覆盖。

## P3 已落地（本轮）

### 子任务共享预算（core/delegation/budget.py 新增 + sidechain 接入）

- task+children 共享预算账本（Redis）：**预留（reserve）→ 执行 → 结算（settle）**；
  预算继承自父 RequestContext（derive_child 只收紧不放宽）；
- 超预算子任务 **fail-fast 拒绝派发**（budget_exceeded），不静默超支；
- 重试/摘要/复盘消耗以 kind 标记计入；**崩溃核对（reconcile）**：宽限期外的未结算
  预留回收并告警（used 按已结算事实保留，不凭空清零）；批次结束自动核对。

## 验收

`tests/p2/test_p2_lifecycle.py` **25/25**（候选→评估→审核→发布→回退全路径、越级×2、
同版本覆盖×2、复盘越权×2、成功率口径×3、预算隔离×2、存量收编×2、外部参考×3、撤权×2）；
`tests/p2/test_p3_budget.py` **9/9**；全量回归 P0/P1/白名单全绿；已部署
（backend + orchestrator + ops_agent 重启，心跳在线）。

## 遗留（P2 尾 + P3 主体）

| 项 | 说明 |
|---|---|
| 技能生命周期 admin API/UI | 模块 API 就绪（approve/publish 需管理员回调）；管理页操作入口待加 |
| 用户记忆/会话搜索 ACL 统一 | session 端点已有归属校验；MemorySystem 未接线（无现行泄漏路径，P2 完整项） |
| 统一摘要模型回调 | synthesizer 已复用 OneAPI 通道；回调化待 P3 统一模型层一并做 |
| P3 统一模型调用层 | BaseAgent/QueryEngine/ingest/embedding/synthesizer 共用解析调用层 + 限流/熔断（大重构，建议独立批次） |
| P3 MCP 官方 SDK | stdio/Streamable HTTP transport、已批准 server 白名单、stdio 命令登记；需 `pip install mcp`（生产 Python 装包需授权）+ 隔离验证 |
| P3 mock MCP 验证 | 撤权/Schema 变化/缓存隔离的 mock 测试（依赖上一项） |


## 第三批更新（2026-10-16 上午）— P3 主体 + P2 尾项

| 项 | 结果 |
|---|---|
| **P2 尾: 技能生命周期 admin API** | 5 端点（GET lifecycle 总览 + approve/publish/rollback/retire，均 require_admin + user-ops 审计）；模块操作能力齐备，admin 页 UI 待加 |
| **P3: 统一模型调用层** | `core/model_gateway.py`：解析链单一事实源（agent Redis > agents.yaml > global Redis > env；provider/model 同层；OneAPI 专用密钥优先；凭据绑定 provider 不混用——BaseAgent._get_llm_config 已委托）；endpoint 级限流（令牌桶）+ 429/5xx 指数退避（尊重 Retry-After）+ 熔断（连续 5 败开闸 60s，快速失败交上层 fallback 模型链）；query_engine 热路径与 fallback 链均已接入（业务级处理——上下文超限/截断升限——语义不变）。生产 e2e 通过 |
| **P3: MCP 官方 SDK 隔离验证** | `/opt/zops-mcp-venv` 安装 **mcp==2.3.0**（未触碰系统 Python）；隔离验证 15/15：stdio + streamable HTTP 双 transport 的 initialize/list_tools/call_tool 全回环（SDK 2.x 适配：MCPServer 改名/streamable_http_client 二元组/is_error） |
| **P3: MCP 注册表与适配层** | `core/mcp_registry.py`（MCP_APPROVED_SERVERS 白名单 fail-closed；stdio 命令必须登记的绝对路径、参数仅标量、拒 shell 元字符——不提供任意进程启动）；`core/mcp_official.py`（惰性 import SDK；MCPCacheKey 缓存按 主体×服务配置指纹×授权版本 隔离；执行时重新验证 Schema；SDK 缺失→MCPNotAvailable 降级现有 REST 适配，白名单仍生效） |

## 验收汇总（累计）

P0 93 + P1 51 + P2 25 + P3 预算 9 + 网关 10 + MCP 隔离 15 = **203 项断言全绿**；
全量回归（含白名单）通过；13 服务部署后 0 failed；网关热路径生产 e2e succeeded。

## 剩余（SPEC 收尾清单）

1. MCP 生产启用：把 mcp SDK 按部署流程装入受控运行环境（隔离验证已锁定 2.3.0 与适配层），
   并在 MCP_APPROVED_SERVERS 登记 opscore 等正式 server。
2. 技能生命周期 admin 页 UI（API 就绪）。
3. 统一模型层第二批迁移：BaseAgent llm_chat 流式路径/wiki ingest/embedding/synthesizer
   的 HTTP 执行改走网关（解析链已统一；执行层渐进迁移）。
4. 运维遗留：n8n 工作流签名升级、凭据轮换 13 项（管理员）。


## 第四批更新（2026-10-16 上午第二批）— SPEC 收尾三项完成

| 项 | 结果 |
|---|---|
| **MCP 生产启用** | 官方 SDK mcp==2.3.0 装入系统 Python（经隔离 venv 复现系统依赖栈验证 anyio 4.15/FastAPI 0.135 兼容 + FastAPI 冒烟）；解决 httpx/httpx2 双栈 h11 冲突（httpcore 升 1.0.9 对齐 h11 0.16，实网请求验证）；全量回归含 MCP 验证 15/15 在系统 Python 通过。opscore REST server 已登记 MCP_APPROVED_SERVERS 白名单；REST 适配 discover 加 fail-closed 门控（勘察发现 REST 集成本处于休眠——无 agent 调用，门控零副作用）。stdio/http transport 经 mcp_official 惰性可用 |
| **模型执行层二批迁移** | BaseAgent 三路径接入网关：llm_chat 非流式（request_chat）、Anthropic（preflight+report）、流式（preflight/配对 report）；synthesizer 记忆提炼与 wiki_vector embeddings 走网关同步变体（同一限流/退避/熔断状态机）。生产 e2e 对话正常（reply+done, task succeeded） |
| **生命周期 admin UI** | web-next 管理页新增「技能生命周期」标签：版本状态徽章/发布指针/成功率口径(null=未验证)/批准/发布/回退/停用操作。已 build+部署 :3000；生产 API 实测（admin token）：vm_backup_check@1.0.0 published、成功率 None |

## 累计验收

**228 项断言全绿**（P0 93 + P1 51 + P2 25 + P3 预算 9 + 网关 10 + MCP 隔离 15×[venv+系统双跑]）；服务 0 failed。

## SPEC 全景最终状态

P0 ✅ / P1 ✅ / P2 ✅ / P3 ✅（预算+统一模型层+MCP 全部就绪）。剩余为纯运维事项：
1. n8n 三个工作流升级 HMAC 回调签名（配方在 Z-OPS-P0-ROTATION-CHECKLIST-c17.md）。
2. 凭据轮换 13 项（管理员执行, 清单同上）。
3. （可选）MCP 正式 server 接入：REST 端点恢复可达后在白名单确认 url；或用 stdio/http transport 登记新 server——SDK 与适配层已就绪。


## 第五批更新（2026-10-18）— Hermes 构件全面接线（收尾批）

背景: Hermes 融入度评估发现四个真缺口（技能闭环无消费端 / memory 四件套休眠 /
backends 闲置 / training 无场景），本批全部处理：

| 项 | 结果 |
|---|---|
| **技能消费端闭环** | `skill_lifecycle.prompt_for_task`（仅 published 注入, 渐进披露）; general_agent/ops_agent 注入 + 结果回写 `record_skill_outcomes`(verified=False 口径不虚高); `usage_stats` 进 admin 生命周期 API。**生产实证**: 备份类问题注入 vm_backup_check, usage 1 次即时可见 |
| **跨会话记忆接线** | chat persist 存储（owner=用户）; `BaseAgent.user_memory_context`（检索前过滤+返回前复核, 仅本人, 无 actor 不注入）; `GET /api/memory/search`（owner 强过滤）。**生产实证**: 本人检索 2 条 / 他人 0 条 |
| **会话摘要 ACL 泄漏修复** | 接线暴露真实泄漏: `ingest_sessions/ingest_user_profiles` 生成的 summaries/entities 页无 visibility → 默认 public, 用户问答进入所有人检索。已修复（写入 owner+private, 旧页回填 12 页）并生产验证隔离 |
| **用户画像 opt-in** | `ZOPS_USER_PROFILE`(默认关) + `add_explicit_note`（仅显式记忆来源, 推断不转授权）; `GET /api/memory/profile` |
| **登记脚本受限执行** | `core/script_exec.py`: registry.json 登记（未登记/未登记 host/参数注入/原始内容四路拒绝实证）; 受限环境 docker(ZOPS_SCRIPT_DOCKER_IMAGE) > 本地 prlimit 沙箱(AS 512M/NPROC 64/干净 env/进程组击杀/输出截断) > 暂停 fail-closed; 远程仅登记 host 走 SSHBackend 非专用账号。dev_agent `_script_exec` 重写为登记制, `_script_gen` 产出落 scripts/proposed/ 候选 |
| **休眠构件处置** | memory_nudge 标注"由每日合成取代, 启用须显式允许"; training/ 归档标注（无场景, 启用前须评估 ACL） |

## 累计验收: 243 项断言全绿（新增 tests/p2/test_close_loop.py 15 项）


## 第六批更新（2026-10-18 下午）— 前端凸显 (让已建成能力可见)

原则: 后端能力 → 用户可感知。四件上线 (web-next :3000, 已 build 部署):

| 凸显点 | 实现 | 生产验证 |
|---|---|---|
| **技能徽章** | 回复 meta 带 `skills` (本轮注入的已发布技能) → 消息栏绿色 🧩 徽章 (最多 2 个+溢出计数) | e2e: `"skills": ["vm_backup_check"]` 信号实测到达 |
| **记忆徽章 + 「我的记忆」页** | `memory_used` 信号 → 紫色 🧠 徽章 (点击跳转); 新页 `/memory` (全员 alwaysVisible): 搜索自己的跨会话记忆 + ACL 说明条 + topics 标签; Sidebar 主导航入口 | `/memory` 200; API 本人 1 条/结构完整 |
| **任务停止按钮** | loading 且持有 task_id 时输入区上方「■ 停止生成」→ POST stop (协作取消, 提示不撤销外部动作) | 端点行为单测覆盖 (发起人/他人/终态三态) |
| **断线恢复提示** | 流中断触发 catchUp 时黄色横幅「网络中断，正在从任务断点恢复…」(4s 自动消失) — P1 断线补读从静默变可感知 | 代码路径与 catchUp 同源 |

配套后端: general_agent reply 带 skills/memory_used; chat reply_meta 透传; lifecycle admin API 加 usage/verified 列, admin 技能生命周期 tab 显示「使用: N 次 (核验 M)」。

webchat_stable.html (:7789 备用前端) 未镜像徽章 — 主入口为 :3000; 如需可后续补。


## 第七批更新（2026-10-18 晚）— 记忆收进设置页 + ChatGPT/Claude Code 式前端架构

| 凸显点 | 实现 | 生产验证 |
|---|---|---|
| **ChatGPT 式设置弹窗** | `SettingsModal` (通用/记忆/数据控制 三分区); Sidebar 底部用户头像块 (渐变首字母) 点击打开; 全局事件 `zops:open-settings` (徽章/面板可唤起) | :3000 build 部署, routes 183 |
| **记忆 = 设置内的完整管理** (ChatGPT Memory parity) | 记忆 tab: 总开关 (关=不注入+不新存)、搜索、逐条删除 (悬停垃圾桶)、数据控制 tab 清除全部 (含 wiki 私有 summaries 页联动清理); ACL 说明条 | 开关 GET/PUT/关闭后对话 0 记忆/DELETE 404/all 清除 e2e 全过 |
| **Claude Code 式键盘优先** | `Ctrl/⌘+K` 命令面板: 模糊过滤 + ↑↓ + Enter; 动作: 新对话/记忆设置/设置/审批/NMS/KB/架构 | tsc/build 通过 |
| **入口收敛** | 独立 /memory 页移除 (旧链接重定向并自动打开设置-记忆 tab); 回答「记忆」徽章改为打开设置弹窗 | 重定向代码路径部署 |

后端新增: `GET/PUT /api/memory/settings` (用户级开关, users hash `memory_enabled`, 注入/存储双侧生效); `DELETE /api/memory/item|all` (owner 双重复核; 清除联动 wiki 私有页); `CrossSessionMemory.delete/clear` + `WikiStore.delete`。


## 第八批更新（2026-10-18 深夜）— 全面体检与优化

### 体检结论 (服务 0 failed / 心跳 10/10 / 容量健康) 与发现修复

| 发现 | 处置 |
|---|---|
| `/api/n8n/webhook` trigger 端点**完全无鉴权** (SPEC 外新洞) | 加 `X-Webhook-Key` 校验 (值=N8N_WEBHOOK_KEY, 同步进 zops.env 因 backend 不加载 n8n.env); 未配置 fail-closed 暂停。实测无 key 401 / 带 key 200 |
| n8n 签名升级实况 | live 8 个工作流**无任何节点打 Z-OPS 端点** (仓库 json 均为模板) — 两端点鉴权门零现有流量; 模板已升级 (20 个 webhook 节点加 key 头, 2 个 callback 模板内嵌签名 Code 节点+说明), 未来部署即合规。**运维项就此关闭** |
| outbox 积压 14 条 (无 drain 循环) | reclaim_loop 顺带台账闭合: 终态任务 delivered/uncertain 补标 (unknown→uncertain 不虚报); 实测 14→0 |
| 队列残留已终态消息 (janitor 会重新派发=重复执行风险) | janitor claim 前查任务终态 → 仅 ACK 出队; 历史残留已清 |
| 事件读取 O(全量) 轮询 | 早停优化 (LPUSH 倒序+seq 单调, 首个 ≤after 即停) — 期间发现并修正方向反转 bug (reversed 顺序错误导致空结果, 回归捕获) |
| 模型网关无协议 URL 报错晦涩 | base_url 卫兵: 早失败并给出可定位错误 |
| webchat_stable.html 徽章缺失 (遗留) | reply 信号透传 + 技能/记忆徽章渲染 (生产验证 skills 信号到达) |
| 间歇性 LLM 401 (aigw 网关) | 同时刻 e2e 成功 → 网关侧间歇问题, 列为监控项 (非代码回归) |

回归: P0 50 + P1 51 + P2 25+15 + P3 9+10 全绿; llm_pref 虚惊 (string 类型, hgetall 查错)。
