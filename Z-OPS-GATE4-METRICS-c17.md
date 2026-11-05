# Z-OPS 上线门 4 指标台账 — task-c17

`python3 scripts/c17_gate4_metrics.py --days 7 --write` 生成 (只读统计生产账本)。每轮追加一节, 不覆盖历史数值。

## 窗口 7 天 · 生成 2026-11-01T23:25:55+08:00

| 门 4 指标 | 实测值 | 口径与说明 |
|---|---|---|
| 任务协议覆盖 | 144 个任务 (终态 144) | 渠道分布 {'webchat': 144} |
| 任务成功率 | 95.8% | succeeded/终态; UNKNOWN=3 个 (写崩溃人工核查, 不计成功也不计失败重试) |
| 任务端到端延迟 | P50 19.2s / P95 225.8s / avg 54.7s / max 865.9s | created_ts→terminal_ts |
| 执行明细量 | 525 条 (按日 {'2026-11-01': 85, '2026-10-31': 70, '2026-10-30': 70, '2026-10-29': 67, '2026-10-28': 63, '2026-10-27': 87, '2026-10-26': 83}) | 成功 519 / 失败 0 → 100.0% |
| 执行分项 · tool_call | 507 次 (ok 501 / fail 0) | P50 327539.9ms / P95 733246.4ms |
| 执行分项 · agent_message | 14 次 (ok 14 / fail 0) | P50 14745.4ms / P95 105508.2ms |
| 执行分项 · agent_forward | 4 次 (ok 4 / fail 0) | P50 0.0ms / P95 0.0ms |
| 模型调用量 | 277 次 | 模型分布 {'deepseek-v3-0324': 277} (窗口内清单口径) |
| Token 用量 | in 3,142,880 / out 704,626 | 估算标记 268 条; 窗口内累计调用 2457 次 (全局计数器) |
| 单次模型耗时 | P50 146.5s / P95 1113.1s / max 1524.9s | llm_usage.elapsed |
| 队列与投递 | stream_len=6 group_pending=0 active=0 outbox_pending=0 | 消费组 ['workers']; delivery 状态 {'delivered': 10} |
| 归档水位 | Redis 事件 38,741 / 已归档 38,741 | 滞后 0 条 (涉及 0 个任务), 核对 144 个终态任务 |
| 恢复能力 | redis 7.2.10 / AOF=1 / aof_bgrewrite=ok / rdb_bgsave=ok | loading=0 (重启恢复中=1) |

- 归档目录: `/opt/zops/logs/tasks`
- 口径提示: executions 列表按日上限裁剪, 数值为可见窗口内事实 (非全量真值); zops:llm_usage 仅保留最近 1000 条 (清单口径), 故窗口 token 为可见清单内值; 调用次数全量真值取 :total 计数器。
- 本轮为协议上线后首个可测窗口, 无同口径历史值 → 只记录当前值作为后续对比基线, 不推算提升比例或工期。

## 本轮实测暴露的缺口与修复（2026-11-01）

| 发现 | 根因 | 修复 | 复核 |
|---|---|---|---|
| 3 个 UNKNOWN 任务的 82 条事件只在 Redis，`logs/tasks/*.jsonl` 无文件（永久事实存在审计盲区，且正是崩溃类最需要审计的任务） | `reclaim_orphans_once` 调 `ledger.complete_unknown` 后**未做归档确认**；60s 回收循环也**没有归档补偿扫描**（只有 `complete()` 路径会 reconcile） | `runtime.py`：孤儿闭合同口径调用 `task_archive.reconcile`；新增 `archive_sweep_once()`（幂等按 event_id 去重、只补写不删除、跳过未终态），reclaim_loop 每 10 分钟一轮（每轮上限 200 任务） | 一次性补扫 `{scanned:144, repaired:82, lag_tasks:3}`；三个任务归档行数 2/69/11 与 Redis 事件数一致；本轮统计 **Redis 事件 38,741 / 已归档 38,741 / 滞后 0** |
| 生产 Redis 实际 7.2.10、`ZOPS_TR_STREAMS=true`、消费组 `workers` 已就绪（旧 readiness 文档记为 6.0.16 阻塞） | 文档过期（管理员已于 10-27 前后升级） | `Z-OPS-P1-READINESS-c17.md` 追加"状态更新"小节（保留原核查结论作为历史事实，不覆盖） | 诊断端点 `feature_flags.streams_consumer=true`；队列 `group_pending=0 active=0 outbox_pending=0` |
| 反向核对（归档 → 账本）发现 `logs/tasks/` 164 文件 vs 账本 144 任务，20 个归档无任何账本投影（永久归档被测试写脏，且当时无人可见） | 只做正向水位（账本→归档）不构成完整闭环；`test_p1_feishu_runtime.py::test_channel_passthrough` 未经 `install_fakes`，而归档目录隔离写在 `install_fakes` 内 → 首个用例把测试事件写进生产永久归档 | 测试 `main()` 在任何用例前隔离 `TASK_ARCHIVE_DIR`；脏文件**不删历史**，按 `archive.mark_unrecoverable` 各追加 1 条 `archive_gap [test_artifact]` 标明来源；`diagnostics.py` 新增 `archive_reverse` 反向盲区核查（无投影且未标明 → blocker，已标明 → note） | 复跑测试 19/19 且生产文件数 **164 → 164 不再增长**、双标记 0；生产诊断 `{files:164, checked:164, orphan_files:20, unexplained:0}`，`ready=true / blockers=[]`（20 个孤儿转为可解释 note） |

- 数据口径提示：`tool_call` 的 P50≈327s 主要由 NMS 后台长任务（`nms_scheduled_round` 单轮 400s+）计入，不是交互请求延迟；交互口径请看"任务端到端延迟"（P50 19.2s / P95 225.8s）。
- 未纳入本表的项：飞书/n8n 入口尚无真实流量（渠道分布 100% webchat），故其成功率/P95 无可测样本，不臆测。
