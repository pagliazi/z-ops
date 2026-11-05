# Z-OPS P1 落地进度 — task-c17 (2026-10-13 晚, 第二批更新 23:5x)

对应 SPEC P1「可靠任务、永久事件与断线恢复」。**P1 全部核心+尾项已落地部署**，
含 Redis 升级至 7.2.10 与 Streams 消费激活（OPS-CORE 试点已生产验证）。

## 第二批更新（2026-10-13 深夜）

| 项 | 结果 |
|---|---|
| **Redis 6.0.16 → 7.2.10** | 源码构建+二进制替换；隔离演练通过；修复两个升级坑（无 sd_notify → Type=simple drop-in；旧 ACL 行致 `resetchannels` 全员无法 SUBSCRIBE → `&*` 修复, agents 5 秒自愈）。readiness 全绿 `streams_allowed=True`。详见 `Z-OPS-REDIS-UPGRADE-20261013.md`（含回退手册） |
| **Streams 消费激活（OPS-CORE 试点）** | `ZOPS_TR_STREAMS=true`；backend lifespan 启动 TaskConsumer + AgentDispatch；opscore_agent 路由的 chat 走 `submit_stream`（原子幂等+入队）→ 消费者 claim → 派发 agent → progress/delta 进事件 → 回复落会话+终态+交付。SSE 走事件跟随（单一执行方）。**生产验证**：OPS-CORE 只读查询经队列全链路 succeeded/delivered/归档 |
| **容量监测+受控拒收** | `capacity_check`：内存 8G 线 / 活跃任务 1000 线，超限拒收不删历史；活跃索引 `tr:active` 由提交/终态维护 |
| **孤儿任务回收** | `reclaim_loop`（backend 常驻）：running+失租超 3×lease → **UNKNOWN 闭合**（写操作绝不自动重做，人工核查）——backend 崩溃后任务不再悬死 |
| **NMS 命令解耦** | 修复「告警无变化→跳过整轮」挡在命令消费之前的 bug：飞书「nms 分析/截图」在告警静默期现在必然执行（队列有命令/强制分析不跳过） |
| **web-next 迁移** | `api.ts`：幂等键 + `catchUpTaskEvents` 补读；`Dashboard.tsx`：流中断→游标补读至终态（不重新 POST）。离线构建修复（Geist→本地字体栈）。已 build+部署 :3000 |
| **P2 首项** | 审批/权限拦截从 `success=True` 改为 `success=False + manual_required`（不再计入业务成功率） |

## 验收汇总

`tests/p1/` **51/51**（新增 OPS-CORE 试点 7 项：队列提交/consumer→agent→回复全链路/事件链/会话落库/幂等重提/容量守卫）；P0 三套 93/93；审批门 26/26；服务 0 failed。

## 仍未完（P1 收尾 → P2）

1. 飞书/n8n 入口迁移到 task_runtime（webchat 已迁移+OPS-CORE 试点）。
2. P2 主体：技能生命周期（candidate→published+内容 hash 版本）、可核验成功率证据、复盘队列隔离、知识 ACL 全量（wiki 已有 visibility 基础）、Hermes reference 标签。
3. P3：MCP SDK stdio/HTTP transport、子任务预算结算、统一模型调用层。


## 前置核查结论（只读）

- **Redis 6.0.16 < 6.2 → Streams 消费模式不激活**（SPEC 硬门槛；升级需管理员按变更流程
  执行，注意 2026-10-08 升级事故教训，不自行升级）。
- 其余达标：noeviction ✅ / AOF everysec+RDB 双存储 ✅ / 单实例 ✅ / 容量充足 ✅。
- 实现层按 **6.0 兼容子集**编写（XPENDING 全量+客户端 idle 过滤、XCLAIM 轮询回收，
  不依赖 6.2 的 XAUTOCLAIM/IDLE 过滤），隔离环境全量验证通过；readiness gate 达标后可激活。

## 已落地

### core/task_runtime/（新增包）

| 模块 | 内容 |
|---|---|
| `ledger.py` | 原子 Lua 账本：**幂等登记+任务建立+入队同一脚本**；claim/emit/heartbeat/complete/stop 五个原子操作；lease 60s（fencing 单调递增，**失租 worker 禁止提交终态**）；**终态+终态事件+Outbox 同一原子提交**；同幂等键同内容返回原任务、不同内容 conflict(409) |
| `events.py`（并入 ledger） | 任务事件 append-only，任务内 seq 单调；progress/delta/reply/终态全持久（SPEC: 初期全存） |
| `outbox.py`（并入 ledger） | 投递记录原子入队；稳定 delivery_id 去重；delivered/uncertain 独立维护 |
| `consumer.py` | Streams 消费者组 + **janitor 失租回收（XCLAIM）+ 超重试上限→UNKNOWN**（写操作不自动重做）；只读默认 3 次指数退避（RetryPolicy） |
| `archive.py` | JSONL 永久归档 `logs/tasks/{task_id}.jsonl`：单 writer 锁+fsync+**event_id 去重**+补偿扫描 reconcile 幂等 + 不可恢复缺口标记 |
| `runtime.py` | 门面：submit_for_chat（幂等+立即 claim 协调者模式）/ emit / heartbeat / complete（**归档异常→阻塞交付并标 uncertain，不虚报**）/ request_stop（actor 校验，断线不取消）/ drain_outbox |
| `readiness.py` | 只读核查 Redis 版本/淘汰策略/AOF/拓扑/容量 → `streams_allowed` gate |

### chat 入口接入（api/routers/chat.py，POST 流完全兼容）

- `Idempotency-Key`（缺省自动生成并随响应返回；**幂等键绑定客户端会话标识**，自动建会话的随机 id 不参与）；响应头 `X-Task-Id`。
- start 事件带 task_id/protocol_ver=1；**重复 POST → 重放全部事件+跟随至终态，不重新提交**（不发 orchestrator、无新 LLM 调用）。
- **事件记录与心跳的单一 writer = `_persist_reply` 任务**（独立于 SSE 连接）：客户端断流后事件仍持续记录、终态仍闭合（生产实测：断流后 seq 68→108 持续增长，最终 succeeded+157 事件归档）。
- 新端点：`GET /api/chat/tasks/{task_id}/events`（JSON 游标 / SSE+Last-Event-ID；**超前游标 400 明确报错**；归属校验 403）、`POST /api/chat/tasks/{task_id}/stop`（协作取消+审计事件，不宣称撤销外部动作）。
- 超时 → failed 终态闭合（原子+Outbox）。

### 前端（webchat_stable.html）

- 发送自动携带 Idempotency-Key；按事件 seq 跟踪游标；
- **断线/流中断 → `GET events?after=游标` 补读直至终态，不重新 POST**。

## 验收（SPEC P1 清单对应）

`tests/p1/test_p1_task_runtime.py` — **44/44**：请求重复(原任务/409)、快速回复、lease 回收+ fencing 递增、**旧 worker 提交全路径被拒**、进程重启恢复接管、Outbox 出队/去重/归档失败→uncertain、游标语义项、停止与终态竞争、**并发 emit seq 无空洞无重复**、Streams 崩溃→janitor 回收重投→完成+XACK。
P0 回归：50+13+30 全绿；审批门 26/26。
**生产 e2e**：真实 LLM 对话全链路（幂等键→delta×82→reply→done→JSONL 归档 157 事件→delivery delivered）；断流补读实测；同键重放实测（秒回、replay 标记、零新任务）。

## 遗留（P1 尾项 → 下一批）

1. **Redis ≥6.2 升级**（管理员变更流程）→ 激活 Streams 消费（consumer 已就绪）。
2. 入口迁移：OPS-CORE 多对象只读试点（agent 侧消费 runtime）→ 飞书/n8n/NMS 入口。
3. web-next（api.ts/Dashboard.tsx）迁移到事件游标（webchat_stable.html 已完成）。
4. 容量监测与受控拒收；session_engine 上下文投影 CAS；NMS 命令与告警变化解耦。
5. 恢复演练报告（Redis 恢复/归档重建）随 P1 验收补做。

## 运维提示

- 任务/事件/幂等事实**无自动清理**（`zops:tr:*` 持久增长；容量监测项）；`logs/tasks/*.jsonl` 永久保留。
- 幂等键混入会话随机 id 的历史任务（3 个 e2e 产物）已终态，无需处理；后续键统一。

---

## 收尾批次（2026-11-01，deploy-c17/c18/c19）：入口全覆盖 + MCP 可用化 + 上线门诊断

### 1) 飞书入口接入可靠任务（`feishu/conversation.py` step ② + `TR_FEISHU_ENABLED`）

- 以 **message_id 为幂等键**、`channel="feishu"`、`session_id="feishu:{chat_id}"` 登记任务，meta 留痕 msg_id/chat_id/chat_type/audience；`link_msg` 绑定触发消息。
- 180s 等待循环内：≥12s **内联心跳**（lease 续租）、progress 事件去重后落账、终局 reply 事件带**全量文本**（≤2000 截断标记注明飞书卡片实际长度）。
- `succeeded` → Outbox 交付 → `mark_delivery(delivered)`；超时 → `complete("failed")` 且**不虚报交付**。
- **降级优先**：登记/emit/终态任一异常只记 warning，对话链路照常回复（可用性不被账本绑死）。
- 顺带修复存量竞态：`_handle_feishu_message` 改为**先 subscribe 再 publish**（旧顺序在 agent 秒回时丢 reply 频道存量）。
- 验收：`tests/p1/test_p1_feishu_runtime.py` **19/19**（channel 透传、webchat 向后兼容、幂等复用、五类事件全落、delivered 归属、JSONL 永久归档、audience 留痕、降级不建任务、超时 failed）。

### 2) n8n 回调可靠登记与去重（`core/task_runtime/entry_guard.py` + `TR_N8N_ENABLED`）

- 背景：n8n 是 at-least-once，`/api/n8n/webhook` 旧路径**无去重**，重发即重复发布通知/重复告警。
- 语义（保守，不改处理逻辑）：首次登记+处理+succeeded 留痕；**仅前次 succeeded 判重复**并跳过副作用；前次 failed/unknown/在途 → 以 `#retry-{ts}` **另记新尝试**（保留历史终态，绝不覆盖，符合"UNKNOWN 不自动重做"）；开关关闭或账本不可用 → **降级不登记**，处理链路完全不受影响。
- 端点侧：鉴权失败不登记不处理；下游异常 `finish("failed")` 后再抛 500（不虚报成功）；`unknown_action`（processed=False）记 failed，因此重发不会被误判重复。
- 验收：`tests/p1/test_p1_n8n_entry.py` **34/34**（含副作用次数断言、幂等键优先级 execution_id>dedupe_key>payload 摘要且键顺序无关、降级/异常安全）。

### 3) MCP REST 发现链路修复（`core/mcp_adapter.py`）

- 原判断"外部 REST 不可达"被证据推翻：带 `X-API-Key` 直连网关 200 + **38 个工具**；生产 `opscore_api.mcp_call_tool`（httpx）一直在正常刷新（3036 VM/168 ESXi）。休眠的只是通用适配器 discover 路径。
- 三处根因修复：SSE 非 200/连接异常**均降级 REST 发现**（旧实现直接返回空）；响应形状 `normalize_tools()` 归一（list / `{tools:[]}` / `{data:[]}`，params 合成 JSON Schema，脏条目丢弃）；aiohttp `ssl` 显式使用 **certifi 上下文**（旧 `ssl=True` 走系统 CA，对中间链不完整的网关 CERTIFICATE_VERIFY_FAILED；verify 关闭才传 False，不自默放宽）。
- 白名单 fail-closed 门控不变：未登记 `transport=rest` server 一律拒绝发现。
- 验收：`tests/p3/test_mcp_rest_discovery.py` **13/13**（离线打桩，不触网）。

### 4) 上线门诊断端点（SPEC"需认证的 diagnostic readiness"首次落地）

- `GET /api/observability/task-runtime-readiness`（`observability.detail` 权限门）→ `core/task_runtime/diagnostics.py::collect()`，只读聚合：
  Redis 前置条件（版本/AOF/noeviction/拓扑/容量，非 ping）、入口开关、队列 XLEN + 活跃租约 ZCARD + Outbox LLEN、delivery 状态计数、任务 status/channel 分布、**租约过期未回收**、**归档水位与 lag**（Redis 事件 event_id vs JSONL）、服务主体与入口鉴权配置。
- `ready = not blockers`（fail-closed 表述）；uncertain 投递 / 归档缺失 / 事件滞后 / 租约停摆 均升级为 blocker。
- **凭据只报"是否配置"**，诊断体不含任何密钥值（测试用哨兵值断言）。
- 验收：`tests/p1/test_p1_diagnostics.py` **20/20**（含未认证 401、伪造令牌 401）。

### 5) 回归与部署

- 全量回归 `scripts/run_c17_regression.sh`（隔离 Redis 6399）：**P0 全通过 + 30 + 38 / P1 51+19+34+20 / P2 25+15 / P3 13+10+10+15 + 插件/CU 桥** → ALL GREEN（≈310 断言）。
- 受控部署：备份 `backups/deploy-c17-feishu-tr-20261101`、`deploy-c18-n8n-mcp-20261101`、`deploy-c19-diag-n8n-20261101`（含 MANIFEST.md5）→ `systemctl restart zops-feishu` / `zops-backend`；health 200、task_consumer/Streams 正常、启动日志无 error；新端点已在 OpenAPI 注册且未认证 401；错误 Webhook key 实测 401 且**不产生任务**（`zops:tr:task:*` 计数不变）。

### 6) 待真实流量/管理员收尾（不属于实现缺口）

- **飞书入口生产验证**：生产账本 `channel` 分布目前全为 `webchat`（144 条），需一条真实飞书消息触发后核对 channel=feishu 任务与 delivered。
- **n8n 入口生产验证**：`/api/n8n/webhook` 无真实流量（日志零条成功回调），去重收益待首个回调；`/api/webhook/n8n_callback`（HMAC+nonce）已有幂等且不受本次改动影响。
- **ops1 凭据轮换 13 项**：管理员动作，见 `Z-OPS-P0-ROTATION-CHECKLIST-c17.md`（本次仅封闭暴露，未轮换）。
- 门 4 指标基线对比：需累积真实流量窗口后统计成功率/P95/token/归档延迟，不用未测量数字背书。

### 7) 上线门 4 指标台账 + 实测暴露的归档盲区修复

- 新增 `scripts/c17_gate4_metrics.py`（**只读**统计生产账本，`--write` 追加 `Z-OPS-GATE4-METRICS-c17.md`）：任务成功率/端到端延迟分位、执行明细按 `exec_type` 分段、模型调用量与 token（`zops:llm_usage` 清单 + `:total` 计数器口径区分）、队列 `XPENDING`/active/outbox、**归档水位（Redis 事件 event_id ↔ JSONL 集合差）**、delivery 状态、恢复能力（版本/AOF/RDB/loading）。
- 首个窗口实测（7 天，144 任务）：成功率 95.8%（UNKNOWN 3 个单列，不计成功也不计重试）、任务 P50 19.2s / P95 225.8s、模型 277 次调用 in 3.14M / out 0.70M（估算标记 268 条）、group_pending=0 / active=0 / outbox=0、AOF+RDB ok、Redis 7.2.10。
- **诊断暴露真实缺陷**：3 个 UNKNOWN 任务的 82 条事件只在 Redis，JSONL 无文件——`reclaim_orphans_once` 闭合孤儿后未做归档确认，且无补偿扫描（仅 `complete()` 路径 reconcile）。崩溃类任务恰是最需审计的。
- 修复（`core/task_runtime/runtime.py`）：孤儿闭合改为与 `complete` 同口径 `task_archive.reconcile`；新增 `archive_sweep_once()`（幂等按 event_id 去重、只补写不删除、跳过未终态、每轮上限 200），`reclaim_loop` 每 10 分钟一轮。
- 生产修复核验：一次性补扫 `{scanned:144, repaired:82, lag_tasks:3}`，三个任务归档行数 2/69/11 与 Redis 事件数一致；再统计 **38,741 / 38,741 / 滞后 0**。
- 验收：`tests/p1/test_p1_archive_sweep.py` **10/10**（孤儿闭合即归档、补扫幂等不重写、跳过在途、补扫后诊断归档 blocker 清除）。
- 全量回归 `scripts/run_c17_regression.sh`：P0 全通过 + 30 + 38 / P1 51+19+34+20+10 / P2 25+15 / P3 13+10+10+15 + 插件/CU 桥 → **ALL GREEN**（≈330 断言）。
- 受控部署：`backups/deploy-c20-archive-sweep-20261101`（runtime.py + MANIFEST.md5）→ restart zops-backend（active / health 200 / 启动无 error）。

### 8) 反向核对暴露的测试污染（永久归档被写脏）

- 门 4 脚本核对"归档 ↔ 账本投影"反向差异时发现：生产 `logs/tasks/` 有 **164 个文件 / 账本仅 144 个任务**，20 个孤儿归档的 `idempotency_key` 是 `pt-web1…`/`om_pt1…`、`session_id=s2|feishu:oc_x` → 全部来自 `tests/p1/test_p1_feishu_runtime.py::test_channel_passthrough`（该用例不经过 `install_fakes`，而归档目录隔离写在 `install_fakes` 里 → 先跑的用例把测试事件写进了生产永久归档）。
- 修复：`main()` 进入任何用例前即隔离 `archive.TASK_ARCHIVE_DIR`（注释说明顺序陷阱）。复跑验证：测试仍 19/19，生产归档文件数 **164 → 164 不再增长**。
- 既有 20 个脏文件**不删除**（红线：历史不删），按 `archive.mark_unrecoverable` 既有占位机制各追加一条 `archive_gap` 标记事件（`[test_artifact]` 说明来源、无对应投影、不代表真实请求），逐文件恰好 1 条（双标记数 0），原有事件行完整保留。

### 9) 诊断补齐反向盲区核查（deploy-c21，本轮闭环）

- 缺口：诊断只做"账本 → 归档"正向水位（有任务未归档 = blocker），反方向"归档有文件 / 账本无投影"从未核对——正是这一盲区让测试污染在生产归档里存在多日无人可见（见第 8 节）。永久保留不删除的红线，意味着**盲区必须靠"可解释"来兜，而不是靠删**。
- 实现（`core/task_runtime/diagnostics.py`）：新增 `archive_reverse` 分节（`_REVERSE_SAMPLE=300`）遍历 `TASK_ARCHIVE_DIR/*.jsonl`，逐 tid 查 `k_task(tid)` 是否存在投影；无投影者读文件末 8KB 找 `type in (archive_gap, archive_note)`：
  - 未标明 → `blockers += "N 个归档文件无账本投影且未标明来源 (审计盲区, 需核实后用 mark_unrecoverable 标明)"`（fail-closed，`ready=false`）；
  - 已标明 → 转 note「N 个无投影归档文件均已标明来源 (历史/测试产物, 不删历史)」；
  - 抽样未覆盖全量时另加提示 note，不假装查全。
- 验收：`tests/p1/test_p1_diagnostics.py` 新增 `case_reverse_blindspot`（写入无标记孤儿 → 出 blocker；补 `archive_gap` → blocker 解除且转 note），**24/24**（原 20 条全保留）。
- 全量回归（含本节 24 断言版 + 第 7 节 10 断言补扫版）：16 套件 **ALL GREEN**，P1 由 51+19+34+20 提升为 51+19+34+**24**+10。
- 受控部署：`backups/deploy-c21-diag-reverse-20261101`（diagnostics.py 回滚快照 + MANIFEST.md5，记录旧 `baa09e50…` / 新 `15d6a1cf…`）→ `systemctl restart zops-backend`（23:38:33→23:38:45）；核验 `/api/health` **200**、新端点未认证 **401**、路由在 OpenAPI、启动日志无 traceback、`[lifespan] P1 Streams 消费已启动` 23:38:36 重现。
- **生产核对（关键结论）**：`ready=true` / `blockers=[]`；`archive_reverse = {files:164, checked:164, orphan_files:20, unexplained:0}`；20 个已标明孤儿如预期降级为可解释 note，不再构成盲区 blocker；正向水位同步干净（`terminal_tasks:144, sampled:25, events_not_archived:0, tasks_without_archive:[]`）；`authz` 全真且 `credential_values_returned=false`。
- 诚实边界：**认证态 HTTP 实测未做**——需要持有 `observability.detail` 的真实会话凭据，按红线不借用个人凭据、不临时造权。已验证的是同族明细端点共用的 `require_admin_or_biz_perm` 门（与 obs.py 其余 12 处一致）+ 401 拒绝 + 诊断模块对生产数据的进程内全量执行（即真实数据路径）。
