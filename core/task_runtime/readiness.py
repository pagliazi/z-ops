"""P1 上线前置核查（只读，不改任何配置）

SPEC: 只读核实实际 Redis ≥6.2、持久化、noeviction、容量。
条件不满足则阻止 P1 Streams 消费激活，不自行升级或重配生产 Redis。
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import redis.asyncio as aioredis

import config

logger = logging.getLogger("zops")

REQUIRED_REDIS_VERSION = (6, 2)


@dataclass
class ReadinessReport:
    ok: bool = False
    streams_allowed: bool = False          # Streams 消费模式是否可激活
    redis_version: str = ""
    blockers: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "streams_allowed": self.streams_allowed,
            "redis_version": self.redis_version,
            "blockers": self.blockers, "notes": self.notes,
        }


def _version_tuple(v: str):
    try:
        return tuple(int(x) for x in v.split(".")[:2])
    except (ValueError, AttributeError):
        return (0, 0)


async def check_readiness(redis_client: Optional[aioredis.Redis] = None) -> ReadinessReport:
    """只读核查；任何一项异常都进入 blockers/notes，不抛出、不修改。"""
    rep = ReadinessReport()
    r = redis_client or aioredis.from_url(config.REDIS_URL, decode_responses=True)
    own_client = redis_client is None
    try:
        info = await r.info("server")
        rep.redis_version = str(info.get("redis_version") or "")
        ver = _version_tuple(rep.redis_version)
        if ver < REQUIRED_REDIS_VERSION:
            rep.blockers.append(
                f"Redis {rep.redis_version} < 6.2 (SPEC P1 门槛; 需管理员按变更流程升级, "
                "参考 2026-10-08 升级事故教训, 不自行升级)")
        # 淘汰策略 (noeviction)
        pol = await r.config_get("maxmemory-policy")
        pol = (pol or {}).get("maxmemory-policy", "")
        if pol != "noeviction":
            rep.blockers.append(f"maxmemory-policy={pol} (要求 noeviction)")
        # 持久化 (分节查询: r.info() 扁平返回会覆盖节键)
        pers = await r.info("persistence")
        if not pers.get("aof_enabled"):
            rep.blockers.append("AOF 未启用")
        aof_status = str(pers.get("aof_last_write_status") or "")
        rdb_status = str(pers.get("rdb_last_bgsave_status") or "")
        if "err" in aof_status.lower():
            rep.blockers.append(f"AOF 写入异常: {aof_status}")
        if "err" in rdb_status.lower():
            rep.notes.append(f"RDB 最近后台保存异常: {rdb_status}")
        fsync = (await r.config_get("appendfsync") or {}).get("appendfsync", "")
        rep.notes.append(f"appendfsync={fsync} (everysec 存在 ~1s 丢失窗口, 不承诺零丢失)")
        # 拓扑 (单实例)
        repl = await r.info("replication")
        if repl.get("role") != "master":
            rep.blockers.append(f"非 master (role={repl.get('role')})")
        # 容量
        mem = await r.info("memory")
        used = int(mem.get("used_memory") or 0)
        maxm = int(mem.get("maxmemory") or 0)
        if maxm and used > maxm * 0.8:
            rep.blockers.append(f"内存占用超 80% ({used}/{maxm})")
        if not maxm:
            rep.notes.append("maxmemory 未设 (依赖宿主内存; 容量监测项)")
        rep.ok = not rep.blockers
        rep.streams_allowed = rep.ok and ver >= REQUIRED_REDIS_VERSION
        if not rep.ok:
            logger.warning(f"[task_runtime] readiness 未通过: {rep.blockers}")
        return rep
    finally:
        if own_client:
            try:
                await r.aclose()
            except Exception:
                pass
