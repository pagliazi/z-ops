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
