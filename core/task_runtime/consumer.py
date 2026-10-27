"""P1 Streams 消费者 — 至少一次 + claim/lease/heartbeat/fencing

Redis 6.0 兼容子集: XREADGROUP + XPENDING/XCLAIM 轮询回收 (不依赖 6.2 XAUTOCLAIM)。
激活条件: readiness.streams_allowed (Redis ≥6.2 等 P1 门槛全通过) —
未达标时本模块不启动消费循环 (SPEC: 条件不满足则阻止 P1 上线, 不自行升级生产 Redis)。

只读查询重试: 默认最多 3 次, 指数退避, 受总 deadline 约束 (RetryPolicy)。
写操作: 先记意图事件; 外部系统明确支持幂等+状态核验才自动重试, 否则 UNKNOWN 人工核查。
"""

import asyncio
import logging
import time
import uuid
from typing import Callable, Optional

import config
from .ledger import Ledger, LedgerError

logger = logging.getLogger("zops")


def queue_key() -> str:
    return f"{config.REDIS_PREFIX}:tr:queue"


GROUP = "workers"

MAX_DELIVERIES_READ = 3          # SPEC: 只读查询默认最多三次尝试
RETRY_BACKOFF_S = (1.0, 3.0, 9.0)  # 指数退避


class RetryPolicy:
    """只读重试: n 次尝试 + 指数退避 + 总 deadline"""

    def __init__(self, max_attempts: int = MAX_DELIVERIES_READ,
                 backoffs=RETRY_BACKOFF_S, deadline_ts: float = 0.0):
        self.max_attempts = max_attempts
        self.backoffs = backoffs
        self.deadline_ts = deadline_ts

    def allowed(self, attempt: int) -> bool:
        if attempt >= self.max_attempts:
            return False
        delay = self.backoffs[min(attempt, len(self.backoffs) - 1)]
        if self.deadline_ts and time.time() + delay > self.deadline_ts:
            return False
        return delay


class TaskConsumer:
    """Streams 消费者 (readiness 达标后由 runtime 启动)"""

    def __init__(self, ledger: Ledger, handler: Callable,
                 owner: str = "", lease_s: int = 60):
        self.ledger = ledger
        self.handler = handler          # async (task_id, msg_id, consumer, fencing) -> None
        self.owner = owner or f"worker-{uuid.uuid4().hex[:8]}"
        self.lease_s = lease_s
        self._running = False
        self._inflight: dict = {}       # task_id -> (owner, fencing, hb_task)

    async def ensure_group(self):
        r = await self.ledger._redis()
        try:
            await r.xgroup_create(queue_key(), GROUP, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def start(self):
        await self.ensure_group()
        self._running = True
        asyncio.create_task(self._consume_loop())
        asyncio.create_task(self._janitor_loop())
        logger.info(f"[task_consumer] 启动 ({self.owner}, lease={self.lease_s}s)")

    async def stop(self):
        self._running = False

    async def _consume_loop(self):
        r = await self.ledger._redis()
        while self._running:
            try:
                resp = await r.xreadgroup(
                    GROUP, self.owner, {queue_key(): ">"}, count=4, block=5000)
                for _stream, entries in (resp or []):
                    for msg_id, fields in entries:
                        task_id = (fields or {}).get("task_id") or ""
                        if not task_id:
                            await r.xack(queue_key(), GROUP, msg_id)
                            continue
                        asyncio.create_task(self._handle_entry(task_id, msg_id))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[task_consumer] XREADGROUP 异常: {e}")
                await asyncio.sleep(2)

    async def _handle_entry(self, task_id: str, msg_id: str):
        """至少一次消费: claim → handler (带心跳) → complete(XACK 同原子)"""
        r = await self.ledger._redis()
        try:
            fencing = await self.ledger.claim(task_id, self.owner, self.lease_s)
        except LedgerError as e:
            if e.code in ("err_busy",):
                # 他人持有 lease: 消息留在 PEL 由持有者处理, 不重复投递
                return
            if e.code == "err_terminal":
                await r.xack(queue_key(), GROUP, msg_id)  # 已终态: 纯 ACK
                return
            logger.warning(f"[task_consumer] claim 失败 {task_id}: {e.code}")
            return
        hb = asyncio.create_task(self._heartbeat_loop(task_id, fencing))
        self._inflight[task_id] = (self.owner, fencing, hb)
        try:
            await self.handler(task_id, msg_id, self, fencing)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[task_consumer] handler 异常 {task_id}: {e}")
            try:
                await self.ledger.complete(task_id, self.owner, fencing,
                                           "failed", {"error": str(e)[:300]},
                                           stream_msg_id=msg_id, group=GROUP)
            except LedgerError as le:
                logger.warning(f"[task_consumer] 终态失败 {task_id}: {le.code}")
        finally:
            hb.cancel()
            self._inflight.pop(task_id, None)

    async def _heartbeat_loop(self, task_id: str, fencing: int):
        interval = max(3, self.lease_s // 4)   # 60s lease → 15s 心跳 (SPEC)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.ledger.heartbeat(task_id, self.owner, fencing)
            except LedgerError as e:
                if e.code == "err_fenced":
                    logger.warning(f"[task_consumer] {task_id} 失租 (fenced), 停止心跳")
                    return
            except Exception as e:
                logger.debug(f"[task_consumer] 心跳异常 {task_id}: {e}")

    async def _janitor_loop(self):
        """失租回收: pending 中 idle > 2×lease → XCLAIM 归还重投;
        超过只读重试上限 → UNKNOWN (写操作不自动重做, 人工核查)。
        注: XPENDING IDLE 过滤是 6.2 特性, 此处全量取回按 time_since_delivered
        客户端过滤 (6.0 兼容; readiness 达标前不激活)。"""
        r = await self.ledger._redis()
        while self._running:
            try:
                pend = await r.xpending_range(
                    queue_key(), GROUP, min="-", max="+", count=50)
                for p in (pend or []):
                    idle_ms = int(p.get("time_since_delivered") or 0)
                    if idle_ms < self.lease_s * 2 * 1000:
                        continue
                    msg_id = p.get("message_id") or ""
                    msg_id = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                    times = int(p.get("times_delivered") or 0)
                    # 全面体检修复: 已终态任务的残留消息只 ACK 出队, 绝不重新派发
                    # (claim 会重置 lease/fencing, 若任务已 succeeded 属重复执行风险)
                    _tid_probe = None
                    try:
                        _probe = await r.xrange(queue_key(), min=msg_id, max=msg_id, count=1)
                        if _probe:
                            _tid_probe = (_probe[0][1] or {}).get("task_id") or ""
                    except Exception:
                        pass
                    if _tid_probe:
                        _st = await self.ledger._field(_tid_probe, "status")
                        if _st in ("succeeded", "failed", "cancelled", "unknown"):
                            await r.xack(queue_key(), GROUP, msg_id)
                            logger.info(f"[task_consumer] 终态残留 ACK: {_tid_probe} ({_st})")
                            continue
                    claimed = await r.xclaim(queue_key(), GROUP, self.owner,
                                             min_idle_time=self.lease_s * 1000,
                                             message_ids=[msg_id])
                    if not claimed:
                        continue
                    if times >= MAX_DELIVERIES_READ + 1:
                        # 超重试上限: 定位 task 置 UNKNOWN, 仅 ACK 出队
                        for _mid, fields in claimed:
                            tid = (fields or {}).get("task_id") or ""
                            if tid:
                                try:
                                    await self.ledger.complete_unknown(
                                        tid, {"reason": "exceeded_read_retries",
                                              "deliveries": times})
                                except LedgerError:
                                    pass
                            await r.xack(queue_key(), GROUP, msg_id)
                    else:
                        # 重新投递 (claim 重置 lease → 原 handler 的 fencing 失效)
                        for _mid, fields in claimed:
                            tid = (fields or {}).get("task_id") or ""
                            if tid:
                                asyncio.create_task(self._handle_entry(tid, msg_id))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[task_consumer] janitor 异常: {e}")
            await asyncio.sleep(self.lease_s)
