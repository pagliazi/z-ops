"""P1 任务账本 — 原子操作 (Lua)

所有多键操作单脚本原子执行（单实例 Redis 语义）:
  submit   : 幂等登记 + 任务建立 + (可选)入队 — 可靠登记前调用方不得报告接收成功
  claim    : lease 获取/失租回收 (fencing 递增, 旧 worker 终态提交将被拒)
  emit     : 事件追加 (seq 单调) + 隐式续租
  heartbeat: 显式续租
  complete : 终态 + 终态事件 + Outbox 同一原子 (+可选 XACK)
  stop     : 协作取消请求 (记事件, 不保证已发出动作撤销)

键布局 (前缀 {zops}:tr:):
  idem:{hash}    Hash  {task_id, payload_hash}  幂等键绑定 actor/channel/session/client_key
  task:{task_id} Hash  状态机字段
  events:{id}    List  事件 JSON (LPUSH, 读侧反转 → seq 升序)
  outbox         List  待投递 {delivery_id, task_id, channel, session}
  queue          Stream (Streams 模式, readiness 达标才激活消费)
"""

import hashlib
import json
import logging
import time
import uuid
from typing import Optional

import redis.asyncio as aioredis

import config

logger = logging.getLogger("zops")

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "unknown")
ACTIVE_STATUSES = ("accepted", "queued", "running", "blocked")

DEFAULT_LEASE_S = 60          # SPEC: 默认 lease 60 秒
DEFAULT_HEARTBEAT_S = 15      # SPEC: 心跳 15 秒

PROTOCOL_VERSION = 1          # SPEC: 新任务带协议版本


def _prefix() -> str:
    return f"{config.REDIS_PREFIX}:tr:"


def k_task(task_id: str) -> str:
    return f"{_prefix()}task:{task_id}"


def k_events(task_id: str) -> str:
    return f"{_prefix()}events:{task_id}"


def k_idem(idem_hash: str) -> str:
    return f"{_prefix()}idem:{idem_hash}"


def idem_hash(actor: str, channel: str, session_id: str, client_key: str) -> str:
    raw = "\x00".join([(actor or ""), (channel or ""), (session_id or ""), (client_key or "")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# ─────────────────────────── Lua 脚本 ───────────────────────────

SUBMIT_LUA = """
-- KEYS[1]=idem KEYS[2]=task KEYS[3]=queue('' 跳过)
-- ARGV: idem_key, payload_hash, task_id, actor, channel, session, protocol_ver, now_ts, enqueue, message, target
local existing = redis.call('HGET', KEYS[1], 'task_id')
if existing and existing ~= '' then
  if redis.call('HGET', KEYS[1], 'payload_hash') == ARGV[2] then
    return {existing, 'duplicate'}
  end
  return {'', 'conflict'}
end
redis.call('HSET', KEYS[1], 'task_id', ARGV[3], 'payload_hash', ARGV[2],
           'actor', ARGV[4], 'created_ts', ARGV[8])
redis.call('HSET', KEYS[2],
  'status', 'queued', 'seq', '0',
  'idem_key', ARGV[1], 'payload_hash', ARGV[2],
  'actor', ARGV[4], 'channel', ARGV[5], 'session_id', ARGV[6],
  'protocol_ver', ARGV[7], 'created_ts', ARGV[8],
  'fencing', '0', 'attempts', '0', 'cancel_requested', '0',
  'delivery_status', '',
  'message', string.sub(ARGV[10], 1, 4000), 'target', ARGV[11])
if KEYS[3] ~= '' and ARGV[9] == '1' then
  redis.call('XADD', KEYS[3], '*', 'task_id', ARGV[3])
end
return {ARGV[3], 'created'}
"""

CLAIM_LUA = """
-- KEYS[1]=task ARGV: owner, now_ts, lease_s
local status = redis.call('HGET', KEYS[1], 'status')
if not status or status == '' then return {'err_missing', ''} end
if status == 'succeeded' or status == 'failed' or status == 'cancelled' or status == 'unknown' then
  return {'err_terminal', status}
end
if status ~= 'queued' and status ~= 'running' and status ~= 'blocked' then
  return {'err_status', status}
end
local owner = redis.call('HGET', KEYS[1], 'lease_owner')
local expires = tonumber(redis.call('HGET', KEYS[1], 'lease_expires_ts') or '0')
local now = tonumber(ARGV[2])
if owner and owner ~= '' and expires > now then
  return {'err_busy', owner}
end
local fencing = tonumber(redis.call('HGET', KEYS[1], 'fencing') or '0') + 1
local attempts = tonumber(redis.call('HGET', KEYS[1], 'attempts') or '0') + 1
redis.call('HSET', KEYS[1],
  'lease_owner', ARGV[1], 'lease_expires_ts', tostring(now + tonumber(ARGV[3])),
  'fencing', tostring(fencing), 'attempts', tostring(attempts), 'status', 'running')
return {'ok', tostring(fencing), tostring(attempts)}
"""

EMIT_LUA = """
-- KEYS[1]=task KEYS[2]=events
-- ARGV: owner, fencing, ev_type, data_json, ts, event_id, new_expiry_ts
local owner = redis.call('HGET', KEYS[1], 'lease_owner')
local fencing = redis.call('HGET', KEYS[1], 'fencing')
if owner ~= ARGV[1] or fencing ~= ARGV[2] then
  return {'err_fenced', owner or '', fencing or ''}
end
local status = redis.call('HGET', KEYS[1], 'status')
if status == 'succeeded' or status == 'failed' or status == 'cancelled' then
  return {'err_terminal', status}
end
local seq = tonumber(redis.call('HGET', KEYS[1], 'seq') or '0') + 1
redis.call('HSET', KEYS[1], 'seq', tostring(seq), 'lease_expires_ts', ARGV[7])
redis.call('LPUSH', KEYS[2], cjson.encode({
  event_id = ARGV[6], seq = seq, type = ARGV[3], ts = tonumber(ARGV[5]),
  data = cjson.decode(ARGV[4])}))
return {'ok', tostring(seq)}
"""

HEARTBEAT_LUA = """
-- KEYS[1]=task ARGV: owner, fencing, new_expiry_ts
local owner = redis.call('HGET', KEYS[1], 'lease_owner')
local fencing = redis.call('HGET', KEYS[1], 'fencing')
if owner ~= ARGV[1] or fencing ~= ARGV[2] then
  return {'err_fenced', owner or '', fencing or ''}
end
redis.call('HSET', KEYS[1], 'lease_expires_ts', ARGV[3])
return {'ok'}
"""

COMPLETE_LUA = """
-- KEYS[1]=task KEYS[2]=events KEYS[3]=outbox KEYS[4]=queue('' 跳过)
-- ARGV: owner, fencing, terminal_status, result_json, ts, delivery_id, event_id,
--       stream_msg_id, group, task_id, channel, session
local status = redis.call('HGET', KEYS[1], 'status')
if not status or status == '' then return {'err_missing', ''} end
if status == 'succeeded' or status == 'failed' or status == 'cancelled' or status == 'unknown' then
  return {'err_already', status}
end
local owner = redis.call('HGET', KEYS[1], 'lease_owner')
local fencing = redis.call('HGET', KEYS[1], 'fencing')
-- unknown 终态允许 janitor 无 lease 提交 (写操作超时/崩溃进 UNKNOWN); 其余须持有效 lease
if ARGV[3] ~= 'unknown' and (owner ~= ARGV[1] or fencing ~= ARGV[2]) then
  return {'err_fenced', owner or '', fencing or ''}
end
local seq = tonumber(redis.call('HGET', KEYS[1], 'seq') or '0') + 1
redis.call('HSET', KEYS[1], 'seq', tostring(seq), 'status', ARGV[3],
  'terminal_ts', ARGV[5], 'delivery_id', ARGV[6], 'delivery_status', 'pending',
  'lease_owner', '', 'lease_expires_ts', '0')
redis.call('LPUSH', KEYS[2], cjson.encode({
  event_id = ARGV[7], seq = seq, type = 'task_terminal', ts = tonumber(ARGV[5]),
  terminal = ARGV[3], data = cjson.decode(ARGV[4])}))
redis.call('LPUSH', KEYS[3], cjson.encode({
  delivery_id = ARGV[6], task_id = ARGV[10], channel = ARGV[11], session = ARGV[12],
  ts = tonumber(ARGV[5])}))
if KEYS[4] ~= '' and ARGV[8] ~= '' then
  redis.call('XACK', KEYS[4], ARGV[9], ARGV[8])
end
return {'ok', tostring(seq)}
"""

STOP_LUA = """
-- KEYS[1]=task KEYS[2]=events ARGV: actor, ts, event_id
local status = redis.call('HGET', KEYS[1], 'status')
if not status or status == '' then return {'err_missing', ''} end
if status == 'succeeded' or status == 'failed' or status == 'cancelled' or status == 'unknown' then
  return {'err_terminal', status}
end
redis.call('HSET', KEYS[1], 'cancel_requested', '1', 'cancel_actor', ARGV[1])
local seq = tonumber(redis.call('HGET', KEYS[1], 'seq') or '0') + 1
redis.call('HSET', KEYS[1], 'seq', tostring(seq))
redis.call('LPUSH', KEYS[2], cjson.encode({
  event_id = ARGV[3], seq = seq, type = 'stop_requested', ts = tonumber(ARGV[2]),
  data = {actor = ARGV[1]}}))
return {'ok'}
"""


class LedgerError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class Ledger:
    """任务账本 (薄封装; Lua 原子性由脚本保证)"""

    def __init__(self, r: aioredis.Redis = None, lease_s: int = DEFAULT_LEASE_S):
        self._r = r
        self.lease_s = lease_s
        self._scripts: dict = {}

    async def _redis(self) -> aioredis.Redis:
        if self._r is None:
            self._r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
        return self._r

    async def _eval(self, name: str, script: str, keys: list, args: list):
        r = await self._redis()
        if name not in self._scripts:
            self._scripts[name] = r.register_script(script)
        return await self._scripts[name](keys=keys, args=args)

    # ── 公共操作 ──

    async def submit(self, actor: str, channel: str, session_id: str,
                     client_key: str, payload_hash: str,
                     enqueue: bool = False,
                     message: str = "", target: str = "") -> tuple:
        """幂等登记 + 任务建立 (+可选入队) — 原子。

        Returns: (task_id, "created"|"duplicate")
        Raises:  LedgerError(code="conflict") — 同键不同内容 (调用方返回 409)
        """
        task_id = f"tsk_{uuid.uuid4().hex[:12]}"
        ih = idem_hash(actor, channel, session_id, client_key)
        res = await self._eval("submit", SUBMIT_LUA,
                               [k_idem(ih), k_task(task_id),
                                f"{_prefix()}queue" if enqueue else ""],
                               [ih, payload_hash, task_id, actor or "", channel or "",
                                session_id or "", str(PROTOCOL_VERSION),
                                f"{time.time():.3f}", "1" if enqueue else "0",
                                message or "", target or ""])
        # redis-py 返回 bytes; decode
        res = [x.decode() if isinstance(x, bytes) else x for x in res]
        if res[1] == "conflict":
            raise LedgerError("conflict", "同幂等键不同内容")
        return res[0], res[1]

    async def claim(self, task_id: str, owner: str, lease_s: int = 0) -> int:
        """获取/失租回收 lease。Returns: fencing token。"""
        res = await self._eval("claim", CLAIM_LUA, [k_task(task_id)],
                               [owner, f"{time.time():.3f}", str(lease_s or self.lease_s)])
        res = [x.decode() if isinstance(x, bytes) else x for x in res]
        if res[0] != "ok":
            raise LedgerError(res[0], res[1] if len(res) > 1 else "")
        return int(res[1])

    async def emit(self, task_id: str, owner: str, fencing: int,
                   ev_type: str, data: dict) -> int:
        """追加事件 (seq 单调) + 隐式续租。Returns: seq。"""
        now = time.time()
        res = await self._eval("emit", EMIT_LUA,
                               [k_task(task_id), k_events(task_id)],
                               [owner, str(fencing), ev_type,
                                json.dumps(data or {}, ensure_ascii=False),
                                f"{now:.3f}", f"evt_{uuid.uuid4().hex[:12]}",
                                f"{now + self.lease_s:.3f}"])
        res = [x.decode() if isinstance(x, bytes) else x for x in res]
        if res[0] != "ok":
            raise LedgerError(res[0], res[1] if len(res) > 1 else "")
        return int(res[1])

    async def heartbeat(self, task_id: str, owner: str, fencing: int) -> None:
        now = time.time()
        res = await self._eval("heartbeat", HEARTBEAT_LUA, [k_task(task_id)],
                               [owner, str(fencing), f"{now + self.lease_s:.3f}"])
        res = [x.decode() if isinstance(x, bytes) else x for x in res]
        if res[0] != "ok":
            raise LedgerError(res[0], res[1] if len(res) > 1 else "")

    async def complete(self, task_id: str, owner: str, fencing: int,
                       terminal: str, result: dict,
                       stream_msg_id: str = "", group: str = "") -> str:
        """终态 + 终态事件 + Outbox 原子提交。Returns: delivery_id。"""
        if terminal not in TERMINAL_STATUSES:
            raise ValueError(f"非法终态: {terminal}")
        now = time.time()
        delivery_id = f"dlv_{uuid.uuid4().hex[:12]}"
        res = await self._eval("complete", COMPLETE_LUA,
                               [k_task(task_id), k_events(task_id),
                                f"{_prefix()}outbox", f"{_prefix()}queue"],
                               [owner, str(fencing), terminal,
                                json.dumps(result or {}, ensure_ascii=False),
                                f"{now:.3f}", delivery_id,
                                f"evt_{uuid.uuid4().hex[:12]}",
                                stream_msg_id or "", group or "",
                                task_id,
                                await self._field(task_id, "channel") or "",
                                await self._field(task_id, "session_id") or ""])
        res = [x.decode() if isinstance(x, bytes) else x for x in res]
        if res[0] != "ok":
            raise LedgerError(res[0], res[1] if len(res) > 1 else "")
        return delivery_id

    async def complete_unknown(self, task_id: str, result: dict) -> str:
        """UNKNOWN 终态 (janitor/崩溃恢复路径; 无 lease 要求, 人工核查)"""
        return await self.complete(task_id, "", -1, "unknown", result)

    async def request_stop(self, task_id: str, actor: str) -> None:
        """协作取消请求 (校验 actor 由调用方完成; 不宣称已撤销外部动作)"""
        res = await self._eval("stop", STOP_LUA,
                               [k_task(task_id), k_events(task_id)],
                               [actor or "", f"{time.time():.3f}",
                                f"evt_{uuid.uuid4().hex[:12]}"])
        res = [x.decode() if isinstance(x, bytes) else x for x in res]
        if res[0] != "ok":
            raise LedgerError(res[0], res[1] if len(res) > 1 else "")

    # ── 读 ──

    async def get_task(self, task_id: str) -> Optional[dict]:
        r = await self._redis()
        data = await r.hgetall(k_task(task_id))
        return data if data else None

    async def link_msg(self, task_id: str, msg_id: str) -> None:
        """关联触发消息 id (兼容映射 msg_id/trace; 非原子, 非关键路径)"""
        r = await self._redis()
        await r.hset(k_task(task_id), "msg_id", msg_id)

    async def _field(self, task_id: str, field: str) -> Optional[str]:
        r = await self._redis()
        return await r.hget(k_task(task_id), field)

    async def read_events(self, task_id: str, after_seq: int = 0) -> list:
        """读 seq > after_seq 的事件 (升序)。游标错误 (after_seq 超出且非 0) 由调用方处理。

        性能 (全面体检优化): LPUSH 使列表按 新→旧 排列且 seq 随推送单调递增,
        正序遍历遇到第一个 seq <= after_seq 即可早停 — 长会话轮询从 O(全量) 降到 O(增量)。
        """
        r = await self._redis()
        raw = await r.lrange(k_events(task_id), 0, -1)   # 新→旧 (LPUSH)
        newest_first = []
        for line in raw:
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if int(ev.get("seq") or 0) <= after_seq:
                break   # 早停: 列表按时间倒序, 首个 <= after 后全是旧事件
            newest_first.append(ev)
        newest_first.reverse()   # → 升序
        return newest_first

    async def is_terminal(self, task_id: str) -> Optional[str]:
        status = await self._field(task_id, "status")
        return status if status in TERMINAL_STATUSES else None

    # ── Outbox 投递 ──

    async def pop_delivery(self) -> Optional[dict]:
        r = await self._redis()
        raw = await r.lpop(f"{_prefix()}outbox")
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return None

    async def pushback_delivery(self, rec: dict) -> None:
        r = await self._redis()
        await r.lpush(f"{_prefix()}outbox", json.dumps(rec, ensure_ascii=False))

    async def mark_delivery(self, task_id: str, delivery_id: str,
                            status: str) -> None:
        """delivered / uncertain (无法确认远端送达时标注不确定性, 不重跑业务)"""
        r = await self._redis()
        await r.hset(k_task(task_id), "delivery_status", status)
        await r.set(f"{_prefix()}delivery:{delivery_id}", status,
                    ex=86400 * 7)   # 渠道按稳定 delivery ID 去重

    async def delivery_seen(self, delivery_id: str) -> bool:
        r = await self._redis()
        return bool(await r.exists(f"{_prefix()}delivery:{delivery_id}"))
