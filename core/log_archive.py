"""审计日志归档模块

将 Redis 中的每日日志 (zops:daily_log:YYYY-MM-DD) 与用户操作日志
(zops:user_ops:YYYY-MM-DD) 归档为本地 gzip JSONL 文件，实现长期审计保存。

设计:
- 归档目录: <项目根>/logs/audit/YYYY-MM-DD.jsonl.gz
- 幂等: 已存在归档文件的日期跳过；当日日志不归档（仍在写入）
- 后台循环: 每小时扫描一次未归档的历史日期
- Redis 原始数据保留策略由 config.DAILY_LOG_TTL 控制 (0=永不过期)，归档是额外的持久副本
"""
import asyncio
import gzip
import json
import os
import re
from datetime import datetime

import config

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE_DIR = os.path.join(_BASE_DIR, "logs", "audit")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _archive_path(date_str: str) -> str:
    return os.path.join(ARCHIVE_DIR, f"{date_str}.jsonl.gz")


async def archive_date(r, date_str: str) -> int:
    """归档指定日期的 daily_log + user_ops，返回写入条数 (0=已归档或无数据)"""
    if not _DATE_RE.match(date_str):
        return 0
    path = _archive_path(date_str)
    if os.path.exists(path):
        return 0
    entries = []
    for raw in await r.lrange(config.daily_log_key(date_str), 0, -1):
        entries.append(("daily_log", raw))
    for raw in await r.lrange(config.user_ops_key(date_str), 0, -1):
        entries.append(("user_ops", raw))
    if not entries:
        return 0
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    tmp_path = path + ".tmp"
    try:
        with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
            for source, raw in entries:
                try:
                    obj = json.loads(raw)
                except Exception:
                    obj = {"raw": raw}
                obj["_source"] = source
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)  # 原子落盘，避免半截文件
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return len(entries)


async def archive_pending(r) -> int:
    """扫描 Redis 中所有日志日期，归档除今天以外的全部未归档日期"""
    today = datetime.now().strftime("%Y-%m-%d")
    dates = set()
    async for k in r.scan_iter(f"{config.REDIS_PREFIX}:daily_log:*"):
        d = k.split(":", 2)[-1]
        if _DATE_RE.match(d):
            dates.add(d)
    async for k in r.scan_iter(f"{config.REDIS_PREFIX}:user_ops:*"):
        d = k.split(":", 2)[-1]
        if _DATE_RE.match(d):
            dates.add(d)
    total = 0
    for d in sorted(dates):
        if d >= today:
            continue  # 当天日志仍在写入，次日再归档
        try:
            total += await archive_date(r, d)
        except Exception as e:
            print(f"[log_archive] 归档 {d} 失败: {e}")
    return total


async def archive_loop(interval_s: int = 3600):
    """后台常驻: 启动时补归档历史，之后每小时增量归档"""
    import redis.asyncio as aioredis
    r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    try:
        while True:
            try:
                n = await archive_pending(r)
                if n:
                    print(f"[log_archive] 审计归档完成: {n} 条 → {ARCHIVE_DIR}")
            except Exception as e:
                print(f"[log_archive] 归档循环异常: {e}")
            # 休眠到下一个整点附近，简化为固定间隔
            await asyncio.sleep(interval_s)
    finally:
        await r.aclose()
