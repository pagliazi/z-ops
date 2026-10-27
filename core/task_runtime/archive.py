"""P1 任务事件 JSONL 永久事实归档

SPEC: JSONL 采用单 writer/跨进程锁、fsync、event_id 去重、投影 offset 和补偿扫描。
- 路径 logs/tasks/{task_id}.jsonl (append-only, 永久保留, 无自动清理)
- event_id 去重: 已归档 event_id 不重复落盘
- 补偿扫描: reconcile(task_id) 把 Redis 事件中未归档的补写 (Redis↔JSONL 不宣称跨存储原子)
- 旧数据丢失部分标明不可恢复 (missing_marker)
"""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import config

logger = logging.getLogger("zops")

TASK_ARCHIVE_DIR = Path(__file__).resolve().parent.parent.parent / "logs" / "tasks"

_write_locks: dict = {}


def _archive_path(task_id: str) -> Path:
    safe = "".join(c for c in task_id if c.isalnum() or c in "-_.")
    return TASK_ARCHIVE_DIR / f"{safe}.jsonl"


def _get_lock(task_id: str) -> asyncio.Lock:
    if task_id not in _write_locks:
        _write_locks[task_id] = asyncio.Lock()
    return _write_locks[task_id]


def load_archived(task_id: str) -> list:
    """读取已归档事件 (升序); 文件不存在返回 []"""
    path = _archive_path(task_id)
    if not path.exists():
        return []
    events = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning(f"[task_archive] 读取失败 task={task_id}: {e}")
    return events


def archived_event_ids(task_id: str) -> set:
    return {str(e.get("event_id")) for e in load_archived(task_id) if e.get("event_id")}


async def archive_events(task_id: str, events: list, fsync: bool = True) -> int:
    """追加事件到 JSONL (event_id 去重; 单 writer 锁 + fsync)。Returns: 新写入条数"""
    if not events:
        return 0
    seen = archived_event_ids(task_id)
    fresh = [e for e in events if str(e.get("event_id") or "") not in seen]
    if not fresh:
        return 0
    lines = []
    for e in sorted(fresh, key=lambda x: int(x.get("seq") or 0)):
        rec = {"ts_archived": None, **e}
        rec.pop("ts_archived", None)
        lines.append(json.dumps(rec, ensure_ascii=False))
    async with _get_lock(task_id):
        def _write():
            TASK_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            with open(_archive_path(task_id), "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                if fsync:
                    os.fsync(f.fileno())
        try:
            await asyncio.get_running_loop().run_in_executor(None, _write)
        except OSError as e:
            logger.error(f"[task_archive] 写入失败 task={task_id}: {e}")
            raise
    return len(fresh)


async def mark_unrecoverable(task_id: str, note: str) -> None:
    """旧数据已丢失部分标明不可恢复 (占位事件, 不虚构内容)"""
    marker = {
        "event_id": f"evt_missing_{uuid.uuid4().hex[:8]}",
        "seq": -1,
        "type": "archive_gap",
        "ts": 0,
        "data": {"note": note, "recoverable": False},
    }
    async with _get_lock(task_id):
        def _write():
            TASK_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            with open(_archive_path(task_id), "a", encoding="utf-8") as f:
                f.write(json.dumps(marker, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        await asyncio.get_running_loop().run_in_executor(None, _write)


async def reconcile(ledger, task_id: str) -> int:
    """补偿扫描: Redis 事件 → 补写未归档部分 (归档已有但 Redis 缺失时不重放副作用, 由 runtime 处理)"""
    redis_events = await ledger.read_events(task_id, after_seq=0)
    return await archive_events(task_id, redis_events)
