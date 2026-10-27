# Z-OPS P1 上线前置核查报告 — task-c17 / 2026-10-13

SPEC P1「技术选择与上线前置条件」要求的只读核查（未改动任何生产 Redis 配置）。

## 核查结果

| 项 | 要求 | 实际 | 结论 |
|---|---|---|---|
| Redis 版本 | ≥ 6.2 | **6.0.16** | ❌ **阻塞** |
| 淘汰策略 | noeviction | noeviction | ✅ |
| 持久化 | AOF + RDB 可用 | AOF enabled / appendfsync=everysec / aof_last_write_status=ok / rdb_last_bgsave_status=ok | ✅ |
| 拓扑 | 单实例（不将未验证原子操作用于 Cluster） | role=master, 无副本/无集群 | ✅ |
| 容量 | 余量充足 | used 103.5M / maxmemory 0(不限) / 磁盘 223G 可用(20%) | ✅ |
| 备份恢复 | 具备 | AOF+RDB 双文件在 /var/lib/redis；恢复演练依赖备份工具（另有待修项） | ⚠️ 演练随 P1 验收补做 |

## 阻塞项处置（按 SPEC：不自行升级或重配生产 Redis）

- **Redis 6.0.16 → ≥6.2 升级需管理员另行安排**。注意：2026-10-08 曾发生 Redis 升级引发的生产事故（zops-agent 自愈架构记忆），升级已列入黑名单——必须先在隔离环境演练、选变更窗口、按 zops-agent 双向自愈预案执行，禁止直接 apt 升级。
- 影响：6.2 的 XAUTOCLAIM 等 Streams 特性不可用。task_runtime 实现按 6.0 兼容子集编写（XPENDING+XCLAIM 轮询回收，不依赖 XAUTOCLAIM），在隔离测试 Redis 上全量验证；**生产激活由 readiness gate 硬性拦截，版本达标前 Streams 消费模式保持未激活**。
- P1 中不依赖 Streams 的部分照常落地：任务账本/幂等登记/事件模型/Outbox（Redis hash+list+Lua，6.0 原生支持）、JSONL 归档强化、SSE 游标与兼容迁移、前端稳定事件 ID。

## 故障边界（SPEC 要求如实声明）

- AOF everysec 存在至多 ~1s 丢失窗口；AOF+RDB 双存储不承诺零丢失。
- 不承诺任意外部操作 exactly-once；写操作超时/崩溃进入 UNKNOWN 由人工核查（task_runtime 状态机实现）。
