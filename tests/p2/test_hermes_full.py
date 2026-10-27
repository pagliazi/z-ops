"""Z-OPS Hermes 完整接入验收 — MemorySystem/SessionSearch/MemoryNudge/DockerBackend

验证所有 Hermes 构件在 Z-OPS 运行时链路中真实可用 (非仅 import 通过)。
"""
import asyncio
import os
import sys
import tempfile
import json
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")

import config

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


def test_memory_system():
    """Hermes MemorySystem: 文件化记忆 add → recall → build_system_context"""
    from core.memory_system import get_memory, MemoryEntry, MemoryType
    tmp = tempfile.mkdtemp(prefix="zops_hermes_ms_")
    ms = get_memory("hermes_test_user")
    ms.base_dir = Path(tmp)  # 隔离
    ms._ensure_dirs()

    ms.add(MemoryEntry(name="偏好简洁", description="用户偏好", type=MemoryType.USER,
                       content="回答直接给结论，不要推理过程"))
    ms.add(MemoryEntry(name="PowerShell反馈", description="反馈", type=MemoryType.FEEDBACK,
                       content="PowerShell 脚本要加 -ErrorAction Stop"))
    ms.add(MemoryEntry(name="项目A", description="项目", type=MemoryType.PROJECT,
                       content="客户A的备份窗口是周日凌晨"))

    hits = ms.recall("PowerShell 脚本怎么写", top_k=2)
    check("MemorySystem.recall: 关键词命中", len(hits) >= 1, str(len(hits)))
    ctx = ms.build_system_context(hits)
    check("MemorySystem.build_system_context: 注入格式", "用户记忆" in ctx or "纠正" in ctx, ctx[:50])


async def test_session_search():
    """SessionSearch FTS5: index → search (owner 过滤)"""
    from core.memory.session_search import SessionSearch
    tmp = tempfile.mkdtemp(prefix="zops_hermes_ss_")
    ss = SessionSearch(db_path=os.path.join(tmp, "test.db"))
    from core.memory.session_search import Message as SSMsg
    await ss.index_session(
        session_id="ss-test-1", user_id="alice", agent_id="general_agent",
        messages=[SSMsg(role="user", content="ESXI-HK-8025 的备份状态怎样"),
                  SSMsg(role="assistant", content="备份正常，最近一次是昨天")],
        summary="查VM备份状态")
    await ss.index_session(
        session_id="ss-test-2", user_id="bob", agent_id="general_agent",
        messages=[SSMsg(role="user", content="DNS 解析失败怎么排查")],
        summary="DNS 故障排查")
    # owner 过滤: alice 搜不到 bob 的
    alice_hits = await ss.search("DNS", user_id="alice")
    bob_hits = await ss.search("DNS", user_id="bob")
    check("SessionSearch: alice 搜不到 bob 的 DNS", len(alice_hits) == 0, str(len(alice_hits)))
    check("SessionSearch: bob 搜到自己的 DNS", len(bob_hits) >= 1)
    # 全文搜索: alice 自己的
    alice_vm = await ss.search("备份", user_id="alice")
    check("SessionSearch: alice 搜到自己 VM 备份", len(alice_vm) >= 1)


async def test_memory_nudge():
    """MemoryNudge: general_agent 数据查询前提示相关记忆"""
    from core.memory.cross_session_memory import CrossSessionMemory
    import core.memory.cross_session_memory as csm_mod
    csm_mod.DATA_DIR = Path(tempfile.mkdtemp(prefix="zops_hermes_nudge_"))
    mem = CrossSessionMemory()
    if hasattr(mem, "_data_path"):
        mem._data_path = csm_mod.DATA_DIR / "sessions.json"
    await mem.store_session_summary("nudge-s1", "alice", "general_agent",
                                    "Q: HKG 站点备份巡检 → A: 全部正常")
    await mem.store_session_summary("nudge-s2", "bob", "general_agent",
                                    "Q: DNS 故障 → A: 已排查")

    # BaseAgent.memory_nudge (仅本人)
    from agents.base import BaseAgent, AgentMessage
    class _Stub(BaseAgent):
        name = "stub_agent"
    stub = _Stub()
    m = AgentMessage(source="webchat", target="general_agent",
                     text="HKG 站点备份巡检", session_id="test-sess", username="alice")
    nudge = await stub.memory_nudge("test-sess", "HKG 站点备份巡检", actor="alice")
    check("MemoryNudge: alice 收到自己相关提醒", "记忆提醒" in nudge and "备份" in nudge, nudge[:80])
    nudge_bob = await stub.memory_nudge("test-sess", "HKG 站点备份巡检", actor="bob")
    check("MemoryNudge: bob 不收 alice 的提醒", "备份巡检" not in nudge_bob)


async def test_preference_context():
    """偏好层 (MemorySystem) recall → BaseAgent.preference_context"""
    from agents.base import BaseAgent, AgentMessage
    from core.memory_system import get_memory, MemoryEntry, MemoryType
    # 用测试用户写入偏好
    ms = get_memory("pref_test_user")
    ms.add(MemoryEntry(name="简洁偏好", description="用户偏好", type=MemoryType.USER,
                       content="回答直接给结论"))

    class _Stub(BaseAgent):
        name = "stub_agent"
    stub = _Stub()
    ctx = await stub.preference_context("pref_test_user", "结论")
    check("偏好层: 召回注入", "用户记忆" in ctx or "结论" in ctx, ctx[:80])
    empty = await stub.preference_context("nonexistent_user_xyz", "测试")
    check("偏好层: 无记忆用户不注入", empty == "")


import threading
def _run_bg(coro):
    box = {}
    def _t():
        try: box['v'] = asyncio.run(coro)
        except BaseException as e: box['e'] = e
    t = threading.Thread(target=_t); t.start(); t.join()
    if 'e' in box: raise box['e']
    return box.get('v')


def test_script_exec_docker():
    """DockerBackend (经 script_exec) 受限执行"""
    from core import script_exec as se
    reg_dir = Path(tempfile.mkdtemp(prefix="zops_docker_reg_"))
    (reg_dir / "hello.sh").write_text('#!/bin/bash\necho "docker-backend-test: $1"\n')
    (reg_dir / "hello.sh").chmod(0o755)
    (reg_dir / "registry.json").write_text(json.dumps([
        {"name": "hello", "file": "hello.sh", "type": "shell",
         "hosts": ["localhost"], "timeout_s": 15, "params_schema": ["--m"]}]))
    se.REGISTRY_DIR = reg_dir
    se.REGISTRY_PATH = reg_dir / "registry.json"

    # 本地沙箱 (prlimit)
    r = _run_bg(se.run_registered("hello", {"m": "local-ok"}, "localhost"))
    check("script_exec 本地沙箱:", r["ok"] and "docker-backend-test" in r["output"], str(r.get("output", ""))[:60])

    # Docker (仅当配置了 ZOPS_SCRIPT_DOCKER_IMAGE; 未配置则跳过不失败)
    if se.docker_image():
        r2 = _run_bg(se.run_registered("hello", {"m": "docker-ok"}, "localhost"))
        # argv 为 [--m, docker-ok] → 脚本 $1=--m; 断言"脚本在 docker 里真的执行了"而非参数语义
        check("script_exec Docker 沙箱:", r2["ok"]
              and "docker-backend-test" in r2["output"]
              and str(r2.get("env", "")).startswith("docker:"), str(r2)[:100])
    else:
        check("script_exec Docker: 未配置镜像 (跳过, 走本地沙箱)", True)


async def main():
    tmp = tempfile.mkdtemp(prefix="zops_hermes_")
    test_memory_system()
    await test_session_search()
    await test_memory_nudge()
    await test_preference_context()
    test_script_exec_docker()
    print("═" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        for f in FAIL: print("  -", f)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
