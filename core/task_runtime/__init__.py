"""Z-OPS P1 任务运行时 (task_runtime) — 可靠任务、永久事件、Outbox

SPEC P1 落地（分阶段引入，不切换现有执行内核）:
  - 原子幂等登记 + 任务建立 + 入队（单一 Lua，可靠登记前不报告接收成功）;
  - 任务事件 append-only（任务内 sequence 单调递增），progress/delta/终态全持久;
  - lease/heartbeat/fencing 控制所有权（默认 lease 60s / 心跳 15s，失租禁止提交终态）;
  - 终态 + 终态事件 + Outbox 同一 Lua 原子提交，渠道按稳定 delivery ID 去重;
  - JSONL 永久事实归档（logs/tasks/{task_id}.jsonl）;
  - readiness gate: Redis ≥6.2 未达标时 Streams 消费模式不激活（账本/事件/游标照常）。

状态机: accepted → queued → running → (blocked | unknown) → succeeded | failed | cancelled
delivery 状态独立维护: pending → delivered | uncertain
"""

from .runtime import TaskRuntime, get_task_runtime, IdempotencyConflict  # noqa: F401
from .readiness import check_readiness  # noqa: F401
