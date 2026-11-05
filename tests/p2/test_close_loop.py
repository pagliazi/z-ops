"""Z-OPS 收尾批验收 — 技能消费闭环 / 跨会话记忆 ACL / 受限脚本执行

SPEC 对应: P2 技能 closed loop + 用户记忆 ACL; P0 登记脚本受限执行。
隔离: 测试 Redis 6399 + 临时生命周期目录 + 登记表样本。
"""
import asyncio
import json
import threading
import os
import sys
import tempfile
from pathlib import Path

TEST_REDIS = os.environ.get("ZOPS_P0_TEST_REDIS", "redis://127.0.0.1:6399/0")
os.environ["REDIS_URL"] = TEST_REDIS
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import config  # noqa: E402

PASS, FAIL = [], []


def _run(coro):
    box = {}
    def _t():
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as e:
            box["e"] = e
    t = threading.Thread(target=_t)
    t.start(); t.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f" {detail}"))


def test_skill_consumption(tmp):
    """closed loop: published 注入 / 非 published 不注入 / 结果回写 / usage 口径"""
    from core.harness import skill_lifecycle as sl

    lc = sl.SkillLifecycle(base=Path(tmp) / "lc", live_dir=Path(tmp) / "live")
    admin = lambda: True
    # 三个版本: v1 published; v2 candidate (不注入); v3 approved (不注入)
    s1 = {"name": "vm_backup_v2", "description": "备份核对", "version": "1.0.0",
          "summary": "核对虚拟机备份覆盖", "procedure": "查 OPS-CORE 备份接口",
          "metadata": {"zops": {"tags": ["备份"]}}}
    v1 = lc.submit_candidate(dict(s1), author="retro")
    lc.evaluate("vm_backup_v2", v1, True, tests=["t"])
    lc.approve("vm_backup_v2", v1, "admin", is_admin=admin)
    lc.publish("vm_backup_v2", v1, "admin", is_admin=admin)
    s2 = dict(s1); s2["procedure"] = "v2 改"
    v2 = lc.submit_candidate(s2, author="retro")
    lc.evaluate("vm_backup_v2", v2, True, tests=["t"])
    lc.approve("vm_backup_v2", v2, "admin", is_admin=admin)   # approved 未发布
    s3 = dict(s1); s3["procedure"] = "v3 草"
    lc.submit_candidate(s3, author="retro")                    # candidate

    block, names = lc.prompt_for_task("帮我检查一下 HKG 站点虚拟机备份 状态")
    check("消费端: published 技能按关键词注入", names == ["vm_backup_v2"] and "参考步骤" in block,
          f"{names} {block[:60]}")
    check("消费端: 注入的是 published 内容 (v1 procedure)",
          "查 OPS-CORE 备份接口" in block and "v2 改" not in block and "v3 草" not in block)
    check("消费端: 无关任务不注入", lc.prompt_for_task("今天天气如何") == ([], "")[1] or
          lc.prompt_for_task("今天天气如何")[0] == "")

    # 结果回写 → usage 统计; verified 口径
    lc.record_skill_outcomes(names, success=True)
    lc.record_skill_outcomes(names, success=True)
    lc.record_skill_outcomes(names, success=False)
    st = lc.usage_stats("vm_backup_v2")
    check("回写: usage=3, 成功率仍 null (未验证不计)", st["usage"] == 3
          and st["success_rate"] is None and st["verified"] == 0, str(st))
    lc.record_outcome("vm_backup_v2", success=True, verified=True)
    st2 = lc.usage_stats("vm_backup_v2")
    check("回写: verified 证据才进成功率", st2["verified"] == 1 and st2["success_rate"] == 1.0)


async def test_user_memory_acl(tmp):
    """跨会话记忆: 存储后仅本人检索可见 (owner ACL)"""
    from core.memory.cross_session_memory import CrossSessionMemory
    import core.memory.cross_session_memory as csm_mod
    # 数据目录隔离
    csm_mod.DATA_DIR = Path(tmp) / "memdata"
    try:
        csm_mod.DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    mem = CrossSessionMemory()
    if hasattr(mem, "_data_path"):
        mem._data_path = csm_mod.DATA_DIR / "sessions.json"
    await mem.store_session_summary(session_id="s-a1", user_id="alice",
                                    agent_id="general_agent",
                                    summary="Q: 备份窗口几点 → A: 周日凌晨 2 点")
    await mem.store_session_summary(session_id="s-b1", user_id="bob",
                                    agent_id="general_agent",
                                    summary="Q: 备份窗口几点 → A: 保密值")
    hits_a = await mem.search_past_sessions("备份", user_id="alice")
    owners = {h.user_id for h in hits_a}
    check("记忆 ACL: alice 只见自己的会话", all(o == "alice" for o in owners)
          and any("周日凌晨" in h.summary for h in hits_a), str(owners))
    hits_b = await mem.search_past_sessions("备份", user_id="bob")
    check("记忆 ACL: bob 只见自己的会话", all(h.user_id == "bob" for h in hits_b)
          and not any("周日凌晨" in h.summary for h in hits_b))
    # BaseAgent.user_memory_context 复核 (无 actor 不注入)
    from agents.base import BaseAgent, AgentMessage
    m_no_user = AgentMessage(source="webchat", target="general_agent", text="备份")
    class _Stub(BaseAgent):
        name = "stub_agent"
    stub = _Stub()
    check("记忆注入: 无 actor 不注入", await stub.user_memory_context(m_no_user) == "")
    m_alice = AgentMessage(source="webchat", target="general_agent", text="备份", username="alice")
    ctx = await stub.user_memory_context(m_alice)
    check("记忆注入: 本人上下文带标记且只见本人内容",
          "该用户的历史会话记忆" in ctx and "保密值" not in ctx, ctx[:80])


def test_script_registry(tmp):
    """登记脚本受限执行: 注册表隔离 + 全拒绝路径 + 沙箱执行"""
    from core import script_exec as se
    reg_dir = Path(tmp) / "reg"
    reg_dir.mkdir(parents=True)
    (reg_dir / "hello.sh").write_text('#!/bin/bash\necho "out: $1 $2"\n')
    (reg_dir / "hello.sh").chmod(0o755)
    (reg_dir / "registry.json").write_text(json.dumps([
        {"name": "hello", "file": "hello.sh", "type": "shell",
         "hosts": ["localhost"], "timeout_s": 5, "risk": "read",
         "params_schema": ["--msg"]}]))
    se.REGISTRY_DIR = reg_dir
    se.REGISTRY_PATH = reg_dir / "registry.json"

    async def _go():
        return await se.run_registered("hello", {"msg": "测试"}, "localhost")
    r = _run(_go())
    # env: docker 优先 (生产配置 ZOPS_SCRIPT_DOCKER_IMAGE), prlimit 为回退 — 两者都是有效沙箱
    _sandboxed = r["env"] == "local-sandbox" or r["env"].startswith("docker:")
    check("脚本: 登记脚本沙箱执行成功", r["ok"] and "out: --msg 测试" in r["output"]
          and _sandboxed, str(r)[:100])
    check("脚本: 输出含环境标记", _sandboxed, r["env"])

    async def _deny_unregistered():
        return await se.run_registered("rm_rf", {})
    try:
        _run(_deny_unregistered())
        check("脚本: 未登记拒绝", False)
    except se.ScriptRegistryError as e:
        check("脚本: 未登记拒绝", e.code == "not_registered")

    async def _deny_inject():
        return await se.run_registered("hello", {"msg": "x; rm -rf /"})
    try:
        _run(_deny_inject())
        check("脚本: 参数注入拒绝", False)
    except se.ScriptRegistryError as e:
        check("脚本: 参数注入拒绝", e.code == "param_invalid")

    async def _deny_host():
        return await se.run_registered("hello", {}, host="10.0.0.9")
    try:
        _run(_deny_host())
        check("脚本: 未登记 host 拒绝", False)
    except se.ScriptRegistryError as e:
        check("脚本: 未登记 host 拒绝", e.code == "host_not_registered")

    async def _deny_raw():
        return await se.run_registered("#!/bin/bash\ncurl evil\n", {})
    try:
        _run(_deny_raw())
        check("脚本: 原始脚本内容拒绝 (非登记名)", False)
    except se.ScriptRegistryError as e:
        check("脚本: 原始脚本内容拒绝 (非登记名)", e.code == "not_registered")


async def main():
    tmp = tempfile.mkdtemp(prefix="zops_close_")
    try:
        test_skill_consumption(tmp)
        await test_user_memory_acl(tmp)
        test_script_registry(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("═" * 50)
    print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  -", f)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
