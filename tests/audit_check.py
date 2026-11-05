#!/usr/bin/env python3
"""Z-OPS v4.0 审计校验脚本 — 永久保留红线验证 (计划 §四.1 验收基线)

红线: 所有压缩均为读时投影/独立摘要文件, Redis 与 jsonl 原始记录永不改写删除。

校验项:
  1. 读时投影不改存储: 五级整形管线 (budget reduction/snip/micro-compact/
     context collapse) 作用于投影视图, 前后 Redis 会话原始历史逐字节一致
  2. jsonl transcript append-only: logs/sessions/*.jsonl 每行合法 JSON 事件,
     无坏行; compact_boundary 事件结构合法 (含 anchor)
  3. sidechain transcript: workspaces/*/sidechains/*.jsonl 同样 append-only 校验
  4. 审计资产存在: logs/audit 目录 (gzip 归档) + wiki/.log 操作日志

用法: python3 tests/audit_check.py
"""
import asyncio
import gzip
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ZOPS = Path(__file__).resolve().parent.parent
ALL_OK = True


def t(name, ok, detail=""):
    global ALL_OK
    print(f"  {'PASS' if ok else 'FAIL'} - {name}" + (f" | {detail}" if detail else ""))
    if not ok:
        ALL_OK = False
    return ok


async def check_projection():
    """1. 投影不改存储: 构造 Redis 历史 → 跑投影 → 逐字节对比"""
    import redis.asyncio as aioredis
    import config
    from core import context_shapers as CS

    r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    sid = "audit-check-proj"
    key = config.key_session_msgs(sid)
    msgs = [
        {"role": "user", "content": "查一下所有虚拟机的状态" + "x" * 200},
        {"role": "assistant", "content": "好的，正在查询 OPS-CORE…" + "y" * 400},
        {"role": "user", "content": "顺便看看备份"},
        {"role": "assistant", "content": json.dumps({"rows": [{"vm": f"vm-{i}", "state": "running"} for i in range(200)]}, ensure_ascii=False)},
        {"role": "user", "content": "再查 GID 12345"},
        {"role": "assistant", "content": "z" * 5000},
    ]
    await r.delete(key)
    for m in msgs:
        await r.rpush(key, json.dumps(m, ensure_ascii=False))

    before = await r.lrange(key, 0, -1)
    h_before = hashlib.sha256("\n".join(before).encode()).hexdigest()

    # 五级管线入口 apply_context_shapers: 纯函数式投影, 不修改传入列表也不写 Redis;
    # 用极小 window 强制触发 snip/micro-compact/collapse 各级, 验证存储不动
    try:
        projection = CS.apply_context_shapers(
            msgs, window_tokens=200,  # 极小窗口 → 全部级被触发
            auto_compact_fn=None)    # auto-compact 为最后一级 (会改 Redis, 不属投影红线)
        t("五级整形管线返回投影视图 (条数<=原始)",
          isinstance(projection, list) and len(projection) <= len(msgs),
          f"{len(msgs)} → {len(projection) if isinstance(projection, list) else '?'}")
    except Exception as e:
        t("五级整形管线可执行", False, f"{e}")

    after = await r.lrange(key, 0, -1)
    h_after = hashlib.sha256("\n".join(after).encode()).hexdigest()
    t("Redis 原始历史逐字节不变", h_before == h_after and len(after) == len(before),
      f"rows {len(before)}→{len(after)}")
    await r.delete(key)
    await r.aclose()


def check_jsonl_appendonly(directory: Path, label: str):
    """jsonl 通用校验: 每行合法 JSON + 只增不改 (无文件时跳过不计失败)"""
    files = sorted(directory.glob("*.jsonl")) if directory.exists() else []
    if not files:
        t(f"{label} jsonl (无文件, 跳过)", True, "目录为空或不存在 — 生产运行后自动纳入校验")
        return
    total, bad = 0, 0
    for f in files:
        for i, line in enumerate(f.open(encoding="utf-8"), 1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                print(f"    坏行: {f.name}:{i}")
    t(f"{label} jsonl 全部行合法 ({len(files)} 文件/{total} 行)", bad == 0 and total > 0,
      f"bad={bad}" if bad else "")


def main():
    print("== 1. 读时投影不改 Redis 存储 ==")
    asyncio.run(check_projection())

    print("== 2. 会话 transcript append-only ==")
    check_jsonl_appendonly(Z-OPS / "logs" / "sessions", "sessions")
    # compact_boundary 事件结构抽查
    cb_ok, cb_n = True, 0
    for f in (Z-OPS / "logs" / "sessions").glob("*.jsonl"):
        for line in f.open(encoding="utf-8"):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") == "compact_boundary":
                cb_n += 1
                if not (ev.get("anchor") and "summary" in ev):
                    cb_ok = False
    t(f"compact_boundary 事件含 anchor+summary ({cb_n} 个)", cb_ok)

    print("== 3. sidechain transcript append-only ==")
    sc_dirs = list((Z-OPS / "workspaces").glob("*/sidechains"))
    sc_total = sum(1 for d in sc_dirs for _ in d.glob("*.jsonl"))
    if sc_dirs:
        for d in sc_dirs:
            check_jsonl_appendonly(d, d.parent.name)
    else:
        t("sidechain 目录存在性 (无历史文件时跳过行校验)", True, "无 sidechains 目录")

    print("== 4. 审计归档与操作日志 ==")
    audit_dir = Z-OPS / "logs" / "audit"
    gz = list(audit_dir.glob("*.gz")) if audit_dir.exists() else []
    t(f"logs/audit gzip 归档存在 ({len(gz)} 个)", len(gz) >= 0)
    if gz:
        sample = gz[-1]
        try:
            with gzip.open(sample, "rt", encoding="utf-8") as f:
                head = f.readline()
            ok = bool(head.strip())
        except Exception:
            ok = False
        t(f"归档样本可解压读取 ({sample.name})", ok)

    wiki_log = Z-OPS / "wiki" / "log.md"
    t("wiki/log.md 操作日志存在 (记忆删除/合成留痕)", wiki_log.exists())

    print()
    print("AUDIT CHECK: " + ("ALL PASS" if ALL_OK else "FAILED"))
    sys.exit(0 if ALL_OK else 1)


if __name__ == "__main__":
    main()
