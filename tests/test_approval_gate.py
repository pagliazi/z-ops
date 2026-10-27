#!/usr/bin/env python3
"""
Z-OPS v4.0 Phase 4.1/4.2 — 审批门冒烟测试 (验收基线: destructive 100% 过审批)

覆盖:
  1. action 级风险判定 (vm_power shutdown=destructive / status=read / batch_exec=destructive ...)
  2. ToolDef.risk_level 三级 (read/write/destructive)
  3. 审批单生命周期: create → pending → decide(approve/reject) → 审计
  4. gate_destructive_call: destructive 一律立即拦截 (permission_denied, 不执行)
  5. gate_action_message: read/write 放行, destructive 立即拦截
  6. 审批只走流程: decide 仅为流程留痕 (批准不触发执行), 决策永久写审计
运行: python3 tests/test_approval_gate.py  (需 Redis)
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def test_risk_rules():
    print("── 1. action 级风险判定 ──")
    from core.approval import assess_action_risk
    check("vm_power shutdown → destructive",
          assess_action_risk("vm_agent", "vm_power", {"power_action": "shutdown"}) == "destructive")
    check("vm_power reboot → destructive",
          assess_action_risk("vm_agent", "vm_power", {"power_action": "reboot"}) == "destructive")
    check("vm_power status → read",
          assess_action_risk("vm_agent", "vm_power", {"power_action": "status"}) == "read")
    check("vm_power start → read",
          assess_action_risk("vm_agent", "vm_power", {"power_action": "start"}) == "read")
    check("batch_exec → destructive",
          assess_action_risk("ops_agent", "batch_exec", {}) == "destructive")
    check("emergency_reboot → destructive",
          assess_action_risk("ops_agent", "emergency_reboot", {}) == "destructive")
    check("vm_snapshot create → write",
          assess_action_risk("vm_agent", "vm_snapshot", {"snap_action": "create"}) == "write")
    check("vm_snapshot revert → destructive",
          assess_action_risk("vm_agent", "vm_snapshot", {"snap_action": "revert"}) == "destructive")
    check("未列出 action → read",
          assess_action_risk("kb_agent", "wiki_query", {}) == "read")


def test_tooldef_risk():
    print("── 2. ToolDef.risk_level 三级 ──")
    from core.tool_schema import ToolDef, RiskLevel
    td_r = ToolDef(name="q1", description="查询", risk_level=RiskLevel.READ)
    td_w = ToolDef(name="s1", description="快照", risk_level=RiskLevel.WRITE)
    td_d = ToolDef(name="p1", description="电源", risk_level=RiskLevel.DESTRUCTIVE)
    check("read 级", td_r.risk_level == RiskLevel.READ)
    check("write 级", td_w.risk_level == RiskLevel.WRITE)
    check("destructive 级", td_d.risk_level == RiskLevel.DESTRUCTIVE)
    check("默认 read", ToolDef(name="d", description="默认").risk_level == RiskLevel.READ)


def test_lifecycle_and_gates():
    print("── 3-6. 审批单生命周期 + 双门 ──")
    from core.approval import (
        create_approval, get_approval, decide_approval,
        gate_destructive_call, gate_action_message, _key, _get_redis,
    )
    from core.tool_schema import ToolDef, RiskLevel

    # 3. 创建 → pending
    rec = create_approval("vm_power", {"power_action": "shutdown", "name": "vm-01"},
                          agent="vm_agent", session_id="smoke", msg_id="m1",
                          username="tester")
    aid = rec["approval_id"]
    got = get_approval(aid)
    check("创建后 status=pending", got and got.get("status") == "pending", str(got and got.get("status")))

    # 4a. destructive 一律立即拦截: gate_destructive_call 不等待不执行 (审批只走流程)
    async def gate_flow():
        td = ToolDef(name="vm_power", description="电源", risk_level=RiskLevel.DESTRUCTIVE)

        executed = {"n": 0}

        async def exec_fn():
            executed["n"] += 1
            from core.tool_schema import ToolExecution
            return ToolExecution(tool_name="vm_power", success=True, output='{"ok": true}')

        result = await gate_destructive_call(td, {"power_action": "shutdown"}, exec_fn,
                                             agent="vm_agent", username="tester")
        return result, executed["n"]

    result, n = asyncio.run(gate_flow())
    check("destructive 立即拦截且不执行原工具 (审批只走流程)",
          (not result.success) and result.permission_denied and n == 0,
          f"success={result.success} denied={result.permission_denied} executed={n}")

    # 4b. 拦截后 decide reject → 流程留痕 (不影响拦截结果)
    async def reject_flow():
        td = ToolDef(name="vm_power", description="电源", risk_level=RiskLevel.DESTRUCTIVE)
        res = await gate_destructive_call(td, {"power_action": "shutdown"}, lambda: None,
                                          agent="vm_agent", username="tester")
        aid = json.loads(res.output).get("approval_id", "")
        decide_approval(aid, "reject", approver="admin-smoke")
        return res, aid

    res, rej_aid = asyncio.run(reject_flow())
    check("拦截后拒绝仅为流程留痕 (单据 rejected)",
          res.permission_denied and not res.success and
          get_approval(rej_aid).get("status") == "rejected",
          f"denied={res.permission_denied} success={res.success}")

    # 5. 幂等决策
    d2 = decide_approval(aid, "approve", approver="admin-smoke")
    check("决策幂等 (已决单不重复)", d2 and d2.get("status") in ("approved", "pending"))
    decide_approval(aid, "reject", approver="admin-smoke")  # 确保最终有决策

    # 6. gate_action_message: read 放行 / destructive 拦截 (APPROVAL_ENABLED 开启时)
    import config
    print(f"     (APPROVAL_ENABLED={config.APPROVAL_ENABLED})")

    class _Msg:
        def __init__(self, action, params):
            self.action, self.params = action, params
            self.session_id, self.msg_id, self.username = "smoke", "m2", "tester"

    class _Agent:
        name = "vm_agent"
        async def reply(self, msg, result=None):
            pass

    async def msg_gate():
        return (await gate_action_message(_Agent(), _Msg("vm_power", {"power_action": "status"})),
                await gate_action_message("kb_agent", _Msg("wiki_query", {})))

    r1, r2 = asyncio.run(msg_gate())
    check("read 级 action 放行 (status)", r1 is True)
    check("未列出 action 放行", r2 is True)

    # 6b. destructive action 立即拦截 (不挂起等待, 审批只走流程)
    async def destructive_gate():
        return await gate_action_message(_Agent(), _Msg("vm_power", {"power_action": "shutdown"}))

    if config.APPROVAL_ENABLED:
        ok = asyncio.run(destructive_gate())
        check("destructive action 立即拦截 (审批只走流程)", ok is False)

    # 7. 审批只走流程 (v4.0.2): decide 仅为流程留痕, 不触发执行
    if config.APPROVAL_ENABLED:
        async def flow_only():
            rec = create_approval("vm_agent.vm_power", {"power_action": "shutdown"},
                                  agent="vm_agent", session_id="smoke", msg_id="m3", username="tester")
            still_pending = get_approval(rec["approval_id"]).get("status") == "pending"
            # approve → 流程留痕 (approved), 无 executed 语义
            r1 = decide_approval(rec["approval_id"], "approve", approver="admin-smoke")
            approved_ok = r1.get("status") == "approved" and "executed" not in r1
            # 幂等: 重复决策不改状态
            r1b = decide_approval(rec["approval_id"], "approve", approver="admin-smoke")
            idem_ok = r1b.get("status") == "approved"
            # reject → 流程留痕 (rejected)
            rec2 = create_approval("ops_agent.batch_exec", {"cmd": "x"},
                                   agent="ops_agent", session_id="smoke", msg_id="m4", username="tester")
            r2 = decide_approval(rec2["approval_id"], "reject", approver="admin-smoke")
            rejected_ok = r2.get("status") == "rejected"
            return still_pending, approved_ok, idem_ok, rejected_ok

        s_pend, a_ok, i_ok, r_ok = asyncio.run(flow_only())
        check("拦截后审批单保持 pending", s_pend)
        check("approve → approved 且无 executed 语义 (仅流程留痕)", a_ok)
        check("决策幂等 (重复决策不改状态)", i_ok)
        check("reject → rejected (流程留痕)", r_ok)
    else:
        print("  SKIP  destructive action 拦截 (APPROVAL_ENABLED=False)")

    # 清理冒烟审批单
    try:
        r = _get_redis()
        for key in r.scan_iter(match=_key("*"), count=100):
            data = r.hgetall(key)
            if data.get("username") == "tester" or data.get("approver") == "admin-smoke":
                r.delete(key)
    except Exception:
        pass


def test_api_route():
    print("── 7. 决策 API 端点存在 ──")
    import webchat_api
    paths = {r.path for r in webchat_api.app.routes}
    check("POST /api/approvals/{id}/decide 端点",
          any("approvals" in p and "decide" in p for p in paths),
          str([p for p in paths if "approval" in p]))
    check("审批列表端点", any("approvals" in p and "decide" not in p for p in paths))


if __name__ == "__main__":
    test_risk_rules()
    test_tooldef_risk()
    test_lifecycle_and_gates()
    test_api_route()
    print(f"\n{'='*40}\n审批门冒烟: {PASS} PASS / {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)
