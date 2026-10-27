"""Z-OPS P3 — 子任务共享预算预留/结算 (SPEC「子任务与预算」)

规则:
  - task + children 共享同一预算账本 (Redis hash: reserved / used / log);
  - 子任务开始前预留 (reserve), 结束按实际用量结算 (settle);
  - 预留不足 → 拒绝派发 (budget_exceeded), 不静默超支;
  - 重试/摘要/复盘的消耗也计入 (调用方 settle 时累加 kind 标记);
  - 进程故障后核对未结算预留 (reconcile): 超过宽限期的 stale 预留回收并告警记录,
    不凭空清零 — 回收写日志, 用量按已结算事实保留。

账本键: {prefix}:tr:budget:{key}  (key 由父任务标识: sc:{agent}:{request_id|batch})
"""

import json
import logging
import time
from typing import Optional

import config

logger = logging.getLogger("zops")

STALE_GRACE_S = 1800   # 预留宽限: 超过此时长未结算视为崩溃残留


def _redis():
    import redis.asyncio as aioredis
    return aioredis.from_url(config.REDIS_URL, decode_responses=True)


class SidechainBudget:
    def __init__(self, key: str, cap_tokens: Optional[int] = None):
        self.key = f"{config.REDIS_PREFIX}:tr:budget:{key}"
        self.cap_tokens = cap_tokens    # None = 不限 (仅记账)

    async def _r(self):
        return _redis()

    async def snapshot(self) -> dict:
        r = await self._r()
        try:
            data = await r.hgetall(self.key)
            return {
                "reserved": int(data.get("reserved") or 0),
                "used": int(data.get("used") or 0),
                "cap": self.cap_tokens or 0,
                "entries": json.loads(data.get("entries") or "{}"),
            }
        finally:
            await r.aclose()

    async def reserve(self, task_id: str, amount: int) -> bool:
        """预留额度; 超上限拒绝 (fail-fast)。同一 task_id 重复预留幂等。"""
        if amount <= 0:
            return True
        r = await self._r()
        try:
            entries = json.loads((await r.hget(self.key, "entries")) or "{}")
            if task_id in entries:
                return True   # 幂等重预留
            reserved = int((await r.hget(self.key, "reserved")) or 0)
            used = int((await r.hget(self.key, "used")) or 0)
            if self.cap_tokens and reserved + used + amount > self.cap_tokens:
                logger.warning(f"[budget] {self.key} 预留拒绝 {task_id}: "
                               f"reserved={reserved} used={used} +{amount} > cap={self.cap_tokens}")
                return False
            entries[task_id] = {"amount": amount, "ts": time.time()}
            await r.hset(self.key, mapping={
                "reserved": str(reserved + amount), "entries": json.dumps(entries)})
            return True
        finally:
            await r.aclose()

    async def settle(self, task_id: str, actual: int, kind: str = "child") -> None:
        """结算: 预留释放 + 实际用量入账 (重试/摘要/复盘以 kind 区分, 均计入)"""
        if actual < 0:
            actual = 0
        r = await self._r()
        try:
            entries = json.loads((await r.hget(self.key, "entries")) or "{}")
            entry = entries.pop(task_id, None)
            reserved = int((await r.hget(self.key, "reserved")) or 0)
            used = int((await r.hget(self.key, "used")) or 0)
            back = entry["amount"] if entry else 0
            await r.hset(self.key, mapping={
                "reserved": str(max(0, reserved - back)),
                "used": str(used + actual),
                "entries": json.dumps(entries),
                "last_settle": json.dumps(
                    {"task_id": task_id, "actual": actual, "kind": kind,
                     "ts": time.time()}, ensure_ascii=False)})
        finally:
            await r.aclose()

    async def reconcile(self) -> dict:
        """崩溃核对: 宽限期外的未结算预留回收 (记日志, 不清 used)。
        Returns: {"reclaimed": n, "amount": total}"""
        r = await self._r()
        try:
            entries = json.loads((await r.hget(self.key, "entries")) or "{}")
            now = time.time()
            stale = {k: v for k, v in entries.items()
                     if now - float(v.get("ts") or 0) > STALE_GRACE_S}
            if not stale:
                return {"reclaimed": 0, "amount": 0}
            for k in stale:
                entries.pop(k)
            reserved = int((await r.hget(self.key, "reserved")) or 0)
            amount = sum(int(v.get("amount") or 0) for v in stale.values())
            await r.hset(self.key, mapping={
                "reserved": str(max(0, reserved - amount)),
                "entries": json.dumps(entries)})
            logger.warning(f"[budget] {self.key} 回收崩溃残留预留: "
                           f"{len(stale)} 项 / {amount} tokens {list(stale)[:5]}")
            return {"reclaimed": len(stale), "amount": amount}
        finally:
            await r.aclose()
