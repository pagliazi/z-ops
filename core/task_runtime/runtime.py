"""P1 任务运行时门面 — chat/飞书/n8n/NMS 入口接入层

职责:
  - submit_for_chat: 幂等登记 + 直接 claim (协调者模式, 旧执行路径不变);
  - 事件读写 + 游标 (SSE Last-Event-ID / 断线补读);
  - 终态提交 + Outbox 投递 (delivery 去重);
  - 协作停止 (actor 校验);
  - 归档: 终态交付前 fsync 归档确认; 补偿扫描 reconcile;
  - readiness gate: Streams 消费仅在门槛全通过时可启动。
"""

import asyncio
import hashlib
import json
import logging
import time
from typing import Optional

import config
from .ledger import Ledger, LedgerError, DEFAULT_LEASE_S, DEFAULT_HEARTBEAT_S
from . import archive as task_archive
from .readiness import check_readiness, ReadinessReport

logger = logging.getLogger("zops")


class IdempotencyConflict(Exception):
    """同幂等键不同内容 — API 层返回 409"""


def payload_hash(message: str, target: str = "", session_id: str = "") -> str:
    raw = "\x00".join([(message or ""), (target or ""), (session_id or "")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TaskRuntime:
    # P1 容量监测与受控拒收 (SPEC: 不用删除历史解决容量问题)
    MAX_PENDING_TASKS = 1000      # 活跃 (非终态) 任务上限
    MAX_MEMORY_USED_MB = 8192     # Redis 已用内存上限 (受控拒收线)

    def __init__(self, lease_s: int = DEFAULT_LEASE_S):
        self.ledger = Ledger(lease_s=lease_s)
        self.lease_s = lease_s
        self._readiness: Optional[ReadinessReport] = None

    async def capacity_check(self) -> tuple:
        """容量检查: (ok, reason)。超限受控拒收 (不删历史), 返回明确原因。"""
        try:
            r = await self.ledger._redis()
            mem = await r.info("memory")
            used_mb = int(mem.get("used_memory") or 0) / (1024 * 1024)
            if used_mb > self.MAX_MEMORY_USED_MB:
                return False, f"Redis 内存超限 ({used_mb:.0f}MB > {self.MAX_MEMORY_USED_MB}MB), 受控拒收"
            pend = await r.zcard(f"{config.REDIS_PREFIX}:tr:active")
            # active 索引由 submit/complete 维护 (见 _track_active)
            if pend > self.MAX_PENDING_TASKS:
                return False, f"活跃任务超限 ({pend} > {self.MAX_PENDING_TASKS}), 受控拒收"
            return True, ""
        except Exception:
            return True, ""   # 容量检查失败不阻断 (宁可放行不误杀)

    # ── readiness ──

    async def readiness(self, refresh: bool = False) -> ReadinessReport:
        if self._readiness is None or refresh:
            self._readiness = await check_readiness()
        return self._readiness

    # ── 提交 (chat 等同步协调入口) ──

    async def submit_stream(self, actor: str, session_id: str,
                            client_key: str, message: str,
                            target: str = "") -> tuple:
        """Streams 模式提交 (P1 试点): 原子 幂等登记+建任务+入队; 消费者认领执行。

        Returns: (task_id, created_flag)
        Raises:  IdempotencyConflict / LedgerError("capacity")
        """
        ok, why = await self.capacity_check()
        if not ok:
            raise LedgerError("capacity", why)
        try:
            task_id, created = await self.ledger.submit(
                actor=actor, channel="webchat", session_id=session_id,
                client_key=client_key,
                payload_hash=payload_hash(message, target, session_id),
                enqueue=True, message=message, target=target)
        except LedgerError as e:
            if e.code == "conflict":
                raise IdempotencyConflict(e.detail) from e
            raise
        if created == "created":
            try:
                r = await self.ledger._redis()
                await r.zadd(f"{config.REDIS_PREFIX}:tr:active", {task_id: time.time()})
            except Exception:
                pass
        return task_id, (created == "created")

    async def streams_enabled(self) -> bool:
        """Streams 消费激活 = readiness 达标 (≥6.2 等) 且 ZOPS_TR_STREAMS 开关开。"""
        try:
            if not getattr(config, "TR_STREAMS_ENABLED", False):
                return False
            rep = await self.readiness()
            return rep.streams_allowed
        except Exception:
            return False

    async def submit_for_chat(self, actor: str, session_id: str,
                              client_key: str, message: str,
                              target: str = "", meta: dict = None,
                              channel: str = "webchat") -> tuple:
        """幂等登记 + 建立 + 立即 claim (协调者=后端/飞书进程, 旧执行路径不变)。
        创建成功时记录 task_started 事件 (重复请求不重复记)。

        channel: 入口渠道 (webchat/feishu/...); 决定 Outbox 投递归属与归档受众。

        Returns: (task_id, created_flag, fencing, owner)
        Raises:  IdempotencyConflict (同键不同内容 → 409); LedgerError("capacity", reason) (受控拒收)
        """
        ok, why = await self.capacity_check()
        if not ok:
            raise LedgerError("capacity", why)
        try:
            task_id, created = await self.ledger.submit(
                actor=actor, channel=channel, session_id=session_id,
                client_key=client_key,
                payload_hash=payload_hash(message, target, session_id),
                enqueue=False)
        except LedgerError as e:
            if e.code == "conflict":
                raise IdempotencyConflict(e.detail) from e
            raise
        owner = f"coord-{task_id[:12]}"   # 确定性 owner: 重复 POST 复用同一协调者身份
        try:
            fencing = await self.ledger.claim(task_id, owner, self.lease_s)
        except LedgerError as e:
            if e.code == "err_busy" and (e.detail or "") == owner:
                # 重复请求且本协调者仍持有 lease → 复用 fencing (不重复递增)
                task = await self.ledger.get_task(task_id)
                fencing = int((task or {}).get("fencing") or 0)
            elif e.code == "err_terminal" and created != "created":
                # 已终态任务的重复请求: 纯重放 (不发事件不提交终态), 无需租约
                fencing = 0
            else:
                raise
        if created == "created":
            await self.ledger.emit(task_id, owner, fencing, "task_started", {
                "target": target, "idempotency_key": client_key,
                "session_id": session_id, **(meta or {})})
            try:
                r = await self.ledger._redis()
                await r.zadd(f"{config.REDIS_PREFIX}:tr:active", {task_id: time.time()})
            except Exception:
                pass
        return task_id, (created == "created"), fencing, owner

    async def emit(self, task_id: str, owner: str, fencing: int,
                   ev_type: str, data: dict, archive_now: bool = False) -> int:
        """追加事件 (隐式续租)。archive_now=True 时同步归档 (fsync) 后返回。"""
        seq = await self.ledger.emit(task_id, owner, fencing, ev_type, data)
        if archive_now:
            evs = [e for e in await self.ledger.read_events(task_id, seq - 1)
                   if int(e.get("seq") or 0) == seq]
            await task_archive.archive_events(task_id, evs, fsync=True)
        return seq

    async def heartbeat(self, task_id: str, owner: str, fencing: int) -> bool:
        try:
            await self.ledger.heartbeat(task_id, owner, fencing)
            return True
        except LedgerError as e:
            if e.code == "err_fenced":
                logger.warning(f"[task_runtime] {task_id} 失租: 心跳被拒")
            return False

    async def complete(self, task_id: str, owner: str, fencing: int,
                       terminal: str, result: dict) -> Optional[str]:
        """终态提交 (原子: 终态+事件+Outbox) → 归档确认 → 投递。

        归档异常时阻塞交付并告警 (SPEC), 终态已落 Redis 不回滚。
        Returns: delivery_id (None=失租被拒或已终态)
        """
        try:
            delivery_id = await self.ledger.complete(
                task_id, owner, fencing, terminal, result)
        except LedgerError as e:
            if e.code == "err_fenced":
                logger.warning(f"[task_runtime] {task_id} 失租 worker 终态被拒 (fencing)")
            elif e.code != "err_already":
                raise
            return None
        # 活跃索引出队
        try:
            r = await self.ledger._redis()
            await r.zrem(f"{config.REDIS_PREFIX}:tr:active", task_id)
        except Exception:
            pass
        # 终态事件归档确认 (补偿扫描兜底)
        try:
            await task_archive.reconcile(self.ledger, task_id)
        except Exception as e:
            logger.error(f"[task_runtime] {task_id} 归档确认失败 (阻塞交付, 告警): {e}")
            await self.ledger.mark_delivery(task_id, delivery_id, "uncertain")
            return delivery_id
        return delivery_id

    # ── 孤儿任务回收 (P1: 进程崩溃后 running+失租任务闭合为 UNKNOWN, 人工核查) ──

    async def reclaim_orphans_once(self) -> int:
        """扫描活跃任务: running 且 lease 过期超 3×lease → complete_unknown。

        语义: 协调者崩溃 (backend 重启) 后任务不得悬死; 写操作进入 UNKNOWN 由
        人工核查, 绝不自动重做 (SPEC)。幂等: 终态任务已被移出活跃索引。
        """
        try:
            r = await self.ledger._redis()
            actives = await r.zrange(f"{config.REDIS_PREFIX}:tr:active", 0, -1)
        except Exception:
            return 0
        now = time.time()
        n = 0
        for task_id in actives[:200]:
            try:
                t = await self.ledger.get_task(task_id)
                if not t or t.get("status") not in ("running", "queued", "blocked"):
                    await r.zrem(f"{config.REDIS_PREFIX}:tr:active", task_id)
                    continue
                expires = float(t.get("lease_expires_ts") or 0)
                if expires and now - expires < self.lease_s * 3:
                    continue   # 失租未超宽限, 可能正在被接管
                await self.ledger.complete_unknown(
                    task_id, {"reason": "coordinator_lease_expired",
                              "last_status": t.get("status")})
                await r.zrem(f"{config.REDIS_PREFIX}:tr:active", task_id)
                # 永久事实归档确认 (与 complete 同口径): 崩溃类任务最需审计,
                # 不得仅存 Redis 事件 (旧实现此处未归档 → UNKNOWN 任务 JSONL 缺失)。
                try:
                    await task_archive.reconcile(self.ledger, task_id)
                except Exception as ae:
                    logger.error(f"[task_runtime] {task_id} 孤儿闭合后归档确认失败 (待补扫): {ae}")
                logger.warning(f"[task_runtime] 孤儿任务闭合为 UNKNOWN: {task_id}")
                n += 1
            except LedgerError:
                continue
            except Exception as e:
                logger.debug(f"[task_runtime] 孤儿扫描异常 {task_id}: {e}")
        return n

    async def _drain_outbox_webchat(self) -> int:
        """Outbox 台账闭合: webchat 渠道交付=回复已落会话 (persist/dispatch 负责),
        终态任务的待办记录补标 delivered — 幂等, 只处理已确认事实, 不虚报。"""
        n = 0
        for _ in range(50):
            try:
                rec = await self.ledger.pop_delivery()
            except Exception:
                break
            if not rec:
                break
            task = await self.ledger.get_task(rec.get("task_id") or "")
            status = (task or {}).get("status")
            if status in ("succeeded", "cancelled"):
                await self.ledger.mark_delivery(rec["task_id"], rec["delivery_id"], "delivered")
                n += 1
            elif status in ("failed", "unknown"):
                # unknown: 协调者崩溃闭合, 交付确实不确定 — 标注不虚报
                await self.ledger.mark_delivery(rec["task_id"], rec["delivery_id"], "uncertain")
                n += 1
            elif status is None:
                break   # 空记录异常, 停止本轮
            else:
                # 仍在途 (running 等): 推回队尾等下轮
                await self.ledger.pushback_delivery(rec)
                break
        return n

    async def archive_sweep_once(self, max_tasks: int = 200) -> dict:
        """归档补偿扫描: 把仅在 Redis 的事件补写 JSONL (SPEC: 未归档事件补写)。

        覆盖 complete()/孤儿闭合之外的缺口来源 (归档时 Redis 抖动、进程被 SIGKILL、
        历史遗留未归档任务)。幂等 (按 event_id 去重), 只补写不删除, 不回滚已落事实。
        """
        res = {"scanned": 0, "repaired": 0, "lag_tasks": 0, "error": ""}
        try:
            r = await self.ledger._redis()
        except Exception as e:
            res["error"] = f"redis: {e}"
            return res
        cursor = 0
        while True:
            try:
                cursor, batch = await r.scan(cursor=cursor,
                                             match=f"{config.REDIS_PREFIX}:tr:task:*", count=200)
            except Exception as e:
                res["error"] = f"scan: {e}"
                return res
            for key in batch:
                if res["scanned"] >= max_tasks:
                    return res
                tid = key.split(":")[-1]
                res["scanned"] += 1
                try:
                    st = await r.hget(key, "status")
                    if st not in ("succeeded", "failed", "cancelled", "unknown"):
                        continue
                    n = await task_archive.reconcile(self.ledger, tid)
                    if n:
                        res["repaired"] += n
                        res["lag_tasks"] += 1
                        logger.info(f"[task_runtime] 归档补写 {tid}: {n} 条事件")
                except Exception as e:
                    logger.warning(f"[task_runtime] 归档补扫失败 {tid}: {e}")
            if cursor == 0:
                return res

    async def reclaim_loop(self):
        """后台回收循环 (backend 启动时拉起; 60s 一轮: 孤儿回收 + outbox + 容量告警)"""
        _capacity_alert_ts = 0
        _cycle = 0
        while True:
            await asyncio.sleep(60)
            _cycle += 1
            try:
                await self.reclaim_orphans_once()
            except Exception as e:
                logger.debug(f"[task_runtime] 回收循环异常: {e}")
            try:
                await self._drain_outbox_webchat()
            except Exception as e:
                logger.debug(f"[task_runtime] outbox 台账异常: {e}")
            # 归档补偿扫描 (10min 一轮; 每轮上限 200 个任务, 不删历史只补写)
            if _cycle % 10 == 0:
                try:
                    s = await self.archive_sweep_once()
                    if s.get("repaired") or s.get("error"):
                        logger.info(f"[task_runtime] 归档补扫: {s}")
                except Exception as e:
                    logger.debug(f"[task_runtime] 归档补扫异常: {e}")
            # P1 容量监控: 键总数/内存超阈值时告警 (SPEC: 不删历史, 只告警+受控拒收)
            try:
                import time as _time
                if _time.time() - _capacity_alert_ts < 3600:   # 1h 最多告警一次
                    continue
                r = await self.ledger._redis()
                info = await r.info("memory")
                used_mb = int(info.get("used_memory") or 0) / (1024 * 1024)
                dbsize = await r.dbsize()
                if used_mb > self.MAX_MEMORY_USED_MB * 0.8:
                    await self._alert_capacity(
                        f"Redis 内存 {used_mb:.0f}MB 超预警线 "
                        f"({self.MAX_MEMORY_USED_MB * 0.8:.0f}MB/{self.MAX_MEMORY_USED_MB}MB)")
                    _capacity_alert_ts = _time.time()
                elif dbsize > 5000:
                    await self._alert_capacity(
                        f"Redis 键数 {dbsize} 超预警 (任务/事件键持续增长, 无自动清理)")
                    _capacity_alert_ts = _time.time()
            except Exception as e:
                logger.debug(f"[task_runtime] 容量监控异常: {e}")

    async def _alert_capacity(self, message: str) -> None:
        """容量告警: 发飞书通知 + 日志 (不阻断, 不删历史)"""
        logger.warning(f"[task_runtime] ⚠️ 容量告警: {message}")
        try:
            r = await self.ledger._redis()
            await r.publish(config.channel("notify:feishu"), json.dumps({
                "text": f"⚠️ **Z-OPS 容量预警**\n{message}\n\n"
                        f"任务/事件/幂等键按规范无自动清理, 持续增长属预期。\n"
                        f"如需控制增长, 请评估归档策略 (不删历史, 可加 TTL 到幂等键)。",
                "topic": "capacity_alert", "source": "task_runtime",
            }, ensure_ascii=False))
        except Exception:
            pass

    # ── 读/游标 ──

    async def get_task(self, task_id: str) -> Optional[dict]:
        return await self.ledger.get_task(task_id)

    async def read_events_after(self, task_id: str, after_seq: int) -> list:
        events = await self.ledger.read_events(task_id, after_seq=after_seq)
        return events

    # ── 停止 ──

    async def request_stop(self, task_id: str, actor: str,
                           is_admin: bool = False) -> tuple:
        """协作取消: 校验 actor/task 归属; 断线不取消; 不宣称撤销已发出动作"""
        task = await self.ledger.get_task(task_id)
        if not task:
            return False, "任务不存在"
        if not is_admin and (task.get("actor") or "") != (actor or ""):
            return False, "仅任务发起人 (或管理员) 可停止"
        try:
            await self.ledger.request_stop(task_id, actor)
            return True, ""
        except LedgerError as e:
            if e.code == "err_terminal":
                return False, f"任务已终态 ({e.detail})"
            return False, e.code

    def cancel_requested(self, task: dict) -> bool:
        return str(task.get("cancel_requested") or "0") == "1"

    # ── Outbox 投递 ──

    async def drain_outbox_once(self, deliver_fn) -> int:
        """投递一轮: pop → deliver → 标记; 失败推回 (重试上限后 uncertain)。
        deliver_fn 兼容 sync/async (返回 truthy=已送达)。"""
        import inspect
        n = 0
        for _ in range(20):
            rec = await self.ledger.pop_delivery()
            if not rec:
                break
            try:
                ok = deliver_fn(rec)
                if inspect.isawaitable(ok):
                    ok = await ok
                status = "delivered" if ok else "uncertain"
                await self.ledger.mark_delivery(rec["task_id"], rec["delivery_id"], status)
                n += 1
            except Exception as e:
                logger.warning(f"[task_runtime] 投递失败 (推回): {e}")
                await self.ledger.pushback_delivery(rec)
                break
        return n


_runtime: Optional[TaskRuntime] = None


def get_task_runtime() -> TaskRuntime:
    global _runtime
    if _runtime is None:
        _runtime = TaskRuntime()
    return _runtime
