"""Agent ↔ Companion 集成: 无设备/离线即暂停, 不回退服务器, 停止不冒充已停止。

不连接真实 Redis、真实设备或真实模型; 只验证 Agent 的编排边界。
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from agents.base import AgentMessage
from agents.computer_agent import PAUSE_REASON_TEXT, ComputerAgent, CuApprovalRequired
from core.cu import devices as cudev

REF = "a" * 24
DEVICE = {
    "device_id": "dev_test_1", "name": "办公电脑", "platform": "windows",
    "online": True, "revoked": False, "version": "1.0.0",
    "capabilities": ["browser.goto", "browser.snapshot", "browser.act",
                     "browser.screenshot", "file.list", "file.read"],
}


def message() -> AgentMessage:
    return AgentMessage(source="webchat", target="computer_agent", action="computer_use",
                        text="整理工单", username="alice", session_id="s1", msg_id="m1")


class PauseReasonTextTests(unittest.TestCase):
    """桌面暂停码必须都有本机/小窗可读文案，新增的到期拒绝不能漏。"""

    CODES = ("desktop_control_not_approved", "desktop_approval_declined",
             "desktop_approval_timeout", "desktop_control_suspended",
             "desktop_focus_denied", "desktop_secure_desktop")

    def test_desktop_pause_codes_are_all_localized(self):
        for code in self.CODES:
            self.assertIn(code, PAUSE_REASON_TEXT)

    def test_approval_timeout_says_it_expired_not_declined(self):
        text = PAUSE_REASON_TEXT["desktop_approval_timeout"]
        self.assertIn("自动拒绝", text)
        self.assertNotEqual(text, PAUSE_REASON_TEXT["desktop_approval_declined"])


class AgentCompanionTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, decisions, cancel=False, prompts=None):
        agent = ComputerAgent.__new__(ComputerAgent)
        queue = list(decisions)
        progress = []

        async def send_progress(_msg, text):
            progress.append(text)

        async def llm_chat(**kwargs):
            if prompts is not None:
                prompts.append(str(kwargs.get("prompt") or ""))
            if not queue:
                return '{"action":"fail","reason":"测试决策耗尽"}'
            return json.dumps(queue.pop(0), ensure_ascii=False)

        async def cancel_requested(_task_id):
            return cancel

        agent.send_progress = send_progress
        agent.llm_chat = llm_chat
        agent._cu_cancel_requested = cancel_requested
        agent._take_approval = AsyncMock(return_value=True)
        agent.progress = progress
        return agent

    async def test_no_device_pauses_without_server_fallback(self):
        agent = self.make_agent([])
        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=None)), \
                patch.object(cudev, "start_task", AsyncMock()) as start:
            result = await asyncio.wait_for(agent._run_loop(message(), "整理工单"), 5)
        self.assertEqual(result["action"], "ask")
        self.assertEqual(result["reason"], "no_device")
        self.assertTrue(result["need_extension"])
        self.assertEqual(result["backend"], "companion")
        self.assertIn("不会改用服务器浏览器", result["question"])
        start.assert_not_called()

    async def test_offline_device_pauses_before_start(self):
        agent = self.make_agent([])
        offline = dict(DEVICE, online=False)
        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=offline)), \
                patch.object(cudev, "start_task", AsyncMock()) as start:
            result = await asyncio.wait_for(agent._run_loop(message(), "整理工单"), 5)
        self.assertEqual(result["reason"], "device_offline")
        self.assertIn("不会改用服务器浏览器", result["question"])
        start.assert_not_called()

    async def test_click_then_done_uses_bound_device_task(self):
        agent = self.make_agent([
            {"action": "click", "ref": REF},
            {"action": "done", "result": "已提交"},
        ])
        calls = []

        async def send_command(actor, device_id, session_id, task_id, method, params, timeout=45.0):
            calls.append((actor, device_id, session_id, task_id, method, params))
            if method == "browser.snapshot":
                return {"status": "ok", "data": {
                    "text": "工单列表", "url": "https://example.test/list", "title": "工单",
                    "items": [{"ref": REF, "tag": "button", "text": "提交", "pw": False}]}}
            if method == "browser.act":
                return {"status": "ok", "data": {"performed": True}}
            if method == "browser.screenshot":
                return {"status": "ok", "data": {"w": 800, "h": 600}}
            return {"status": "ok", "data": {}}

        start = AsyncMock(return_value={"task_id": "m1"})
        finish = AsyncMock(return_value={})
        renew = AsyncMock(return_value={})
        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=DEVICE)), \
                patch.object(cudev, "start_task", start), \
                patch.object(cudev, "send_command", side_effect=send_command), \
                patch.object(cudev, "finish_task", finish), \
                patch.object(cudev, "renew_task", renew):
            result = await asyncio.wait_for(agent._run_loop(message(), "整理工单"), 10)

        self.assertEqual(result["action"], "done")
        self.assertEqual(result["backend"], "companion")
        self.assertEqual(result["device_id"], DEVICE["device_id"])
        self.assertEqual(result["cu_task_id"], "m1")
        start.assert_awaited_once_with("alice", "s1", "m1")
        finish.assert_awaited_once_with("alice", "m1", "succeeded")
        self.assertTrue(renew.await_count >= 1)
        self.assertIn(("alice", "dev_test_1", "s1", "m1", "browser.act",
                       {"ref": REF, "verb": "click"}), calls)
        self.assertIn(("alice", "dev_test_1", "s1", "m1", "browser.snapshot", {}), calls)
        self.assertTrue(any("办公电脑" in text for text in agent.progress))

    async def test_paused_result_asks_for_local_confirmation_without_replay(self):
        agent = self.make_agent([{"action": "click", "ref": REF},
                                 {"action": "done", "result": "不应执行"}])
        calls = []

        async def send_command(actor, device_id, session_id, task_id, method, params, timeout=45.0):
            calls.append(method)
            if method == "browser.snapshot":
                return {"status": "ok", "data": {"text": "列表", "url": "https://example.test",
                        "title": "列表", "items": [{"ref": REF, "tag": "button",
                        "text": "提交", "pw": False}]}}
            if method == "browser.act":
                return {"status": "paused", "data": {}, "error": "local_authorization_required"}
            return {"status": "ok", "data": {}}

        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=DEVICE)), \
                patch.object(cudev, "start_task", AsyncMock(return_value={})), \
                patch.object(cudev, "send_command", side_effect=send_command), \
                patch.object(cudev, "finish_task", AsyncMock(return_value={})), \
                patch.object(cudev, "renew_task", AsyncMock(return_value={})):
            result = await asyncio.wait_for(agent._run_loop(message(), "整理工单"), 10)

        self.assertEqual(result["action"], "ask")
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(calls.count("browser.act"), 1)
        self.assertIn("Companion", result["question"])
        self.assertIn("checkpoint", result)

    async def test_paused_reason_is_localized_without_page_content(self):
        agent = self.make_agent([{"action": "click", "ref": REF},
                                 {"action": "done", "result": "不应执行"}])

        async def send_command(actor, device_id, session_id, task_id, method, params, timeout=45.0):
            if method == "browser.snapshot":
                return {"status": "ok", "data": {"text": "登录页", "url": "https://example.test/login",
                        "title": "登录", "items": [{"ref": REF, "tag": "button",
                        "text": "登录", "pw": False}]}}
            if method == "browser.act":
                return {"status": "paused", "data": {"pause_reason": "sensitive_or_login_page"},
                        "error": "local_authorization_required"}
            return {"status": "ok", "data": {}}

        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=DEVICE)), \
                patch.object(cudev, "start_task", AsyncMock(return_value={})), \
                patch.object(cudev, "send_command", side_effect=send_command), \
                patch.object(cudev, "finish_task", AsyncMock(return_value={})), \
                patch.object(cudev, "renew_task", AsyncMock(return_value={})):
            result = await asyncio.wait_for(agent._run_loop(message(), "整理工单"), 10)

        self.assertEqual(result["action"], "ask")
        self.assertIn("登录", result["question"])
        # 静态码只用于本机定位, 不原样回显给用户或模型。
        self.assertNotIn("sensitive_or_login_page", result["question"])

    async def test_unknown_result_stops_and_requires_device_check(self):
        agent = self.make_agent([{"action": "click", "ref": REF},
                                 {"action": "done", "result": "不应执行"}])
        calls = []

        async def send_command(actor, device_id, session_id, task_id, method, params, timeout=45.0):
            calls.append(method)
            if method == "browser.snapshot":
                return {"status": "ok", "data": {"text": "列表", "url": "https://example.test",
                        "title": "列表", "items": [{"ref": REF, "tag": "button",
                        "text": "提交", "pw": False}]}}
            if method == "browser.act":
                return {"status": "unknown", "data": {}, "error": "receipt_lost"}
            return {"status": "ok", "data": {}}

        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=DEVICE)), \
                patch.object(cudev, "start_task", AsyncMock(return_value={})), \
                patch.object(cudev, "send_command", side_effect=send_command), \
                patch.object(cudev, "get_task", AsyncMock(return_value={"state": "running"})), \
                patch.object(cudev, "finish_task", AsyncMock(return_value={})), \
                patch.object(cudev, "renew_task", AsyncMock(return_value={})):
            result = await asyncio.wait_for(agent._run_loop(message(), "整理工单"), 10)

        self.assertEqual(result["action"], "ask")
        self.assertEqual(result["reason"], "unknown")
        self.assertEqual(calls.count("browser.act"), 1)
        self.assertIn("不会重放", result["question"])

    async def test_device_without_browser_capability_pauses_before_start(self):
        agent = self.make_agent([])
        device = dict(DEVICE, capabilities=["file.read"])
        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=device)), \
                patch.object(cudev, "start_task", AsyncMock()) as start:
            result = await asyncio.wait_for(agent._run_loop(message(), "整理工单"), 5)
        self.assertEqual(result["action"], "ask")
        self.assertEqual(result["reason"], "capability_unavailable")
        self.assertIn("不会改用服务器", result["question"])
        start.assert_not_called()

    async def test_stop_request_never_claims_confirmed_stop(self):
        agent = self.make_agent([], cancel=True)
        cancel = AsyncMock(return_value={"state": "cancel_requested"})
        finish = AsyncMock(return_value={})
        send = AsyncMock(return_value={"status": "ok", "data": {}})
        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=DEVICE)), \
                patch.object(cudev, "start_task", AsyncMock(return_value={})), \
                patch.object(cudev, "cancel_task", cancel), \
                patch.object(cudev, "finish_task", finish), \
                patch.object(cudev, "send_command", send):
            result = await asyncio.wait_for(agent._run_loop(message(), "整理工单"), 10)
        cancel.assert_awaited_once_with("alice", "m1")
        send.assert_not_called()
        self.assertFalse(result["cancelled"])
        self.assertIn("尚未确认", result["reason"])

    async def test_rejected_command_after_stop_reports_device_receipt(self):
        calls = []

        async def send_command(actor, device_id, session_id, task_id, method, params, timeout=45.0):
            calls.append(method)
            if method == "browser.snapshot":
                return {"status": "ok", "data": {"text": "列表", "url": "https://example.test",
                        "title": "列表", "items": [{"ref": REF, "tag": "button",
                        "text": "提交", "pw": False}]}}
            return {"status": "rejected", "data": {}, "error": "device_busy_no_queue"}

        async def run(cancel_state):
            agent = self.make_agent([{"action": "click", "ref": REF}])
            cancel = AsyncMock(return_value={"state": cancel_state})
            with patch.object(cudev, "get_selected_device", AsyncMock(return_value=DEVICE)), \
                    patch.object(cudev, "start_task", AsyncMock(return_value={})), \
                    patch.object(cudev, "send_command", side_effect=send_command), \
                    patch.object(cudev, "get_task", AsyncMock(return_value={"state": "cancel_requested"})), \
                    patch.object(cudev, "cancel_task", cancel), \
                    patch.object(cudev, "finish_task", AsyncMock(return_value={})), \
                    patch.object(cudev, "renew_task", AsyncMock(return_value={})):
                return await asyncio.wait_for(agent._run_loop(message(), "整理工单"), 10)

        confirmed = await run("cancelled")
        self.assertEqual(confirmed["action"], "fail")
        self.assertTrue(confirmed["cancelled"])
        self.assertIn("已确认停止", confirmed["reason"])
        self.assertEqual(calls.count("browser.act"), 1)

        calls.clear()
        pending = await run("cancel_requested")
        self.assertEqual(pending["action"], "fail")
        self.assertFalse(pending["cancelled"])
        self.assertIn("尚未确认", pending["reason"])
        self.assertEqual(calls.count("browser.act"), 1)

    async def test_secret_question_becomes_local_login_handoff(self):
        agent = self.make_agent([
            {"action": "ask", "question": "请输入 AD 密码", "input": "密码"},
        ])
        send = AsyncMock(return_value={"status": "ok", "data": {}})
        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=DEVICE)), \
                patch.object(cudev, "start_task", AsyncMock(return_value={})), \
                patch.object(cudev, "send_command", send), \
                patch.object(cudev, "finish_task", AsyncMock(return_value={})), \
                patch.object(cudev, "renew_task", AsyncMock(return_value={})):
            result = await asyncio.wait_for(agent._run_loop(message(), "登录后整理工单"), 10)
        self.assertTrue(result["login_handoff"])
        self.assertNotIn("密码", result["question"].replace("不接触密码", ""))
        self.assertNotIn("browser.act", [c.args[4] for c in send.await_args_list])

    async def test_risky_click_requires_one_time_user_approval(self):
        async def run(approved):
            agent = self.make_agent([
                {"action": "click", "ref": REF},
                {"action": "done", "result": "已提交"},
            ])
            agent._take_approval = AsyncMock(return_value=approved)
            send = AsyncMock(side_effect=lambda *a, **k: {"status": "ok", "data": {
                "text": "工单", "url": "https://example.test/new", "title": "新建工单",
                "items": [{"ref": REF, "tag": "button", "text": "提交工单", "pw": False}]}}
                if a[4] == "browser.snapshot" else {"status": "ok", "data": {"performed": True}})
            with patch.object(cudev, "get_selected_device", AsyncMock(return_value=DEVICE)), \
                    patch.object(cudev, "start_task", AsyncMock(return_value={})), \
                    patch.object(cudev, "send_command", send), \
                    patch.object(cudev, "finish_task", AsyncMock(return_value={})), \
                    patch.object(cudev, "renew_task", AsyncMock(return_value={})):
                result = await asyncio.wait_for(agent._run_loop(message(), "提交工单"), 10)
            return result, [c.args[4] for c in send.await_args_list]

        blocked, methods = await run(False)
        self.assertTrue(blocked["approval_required"])
        self.assertEqual(blocked["action"], "ask")
        self.assertNotIn("browser.act", methods)
        allowed, methods = await run(True)
        self.assertEqual(allowed["action"], "done")
        self.assertIn("browser.act", methods)

    async def test_unsupported_shell_is_rejected_without_device_call(self):
        agent = self.make_agent([
            {"action": "shell", "command": "rm -rf /tmp/x"},
            {"action": "done", "result": "结束"},
        ])
        send = AsyncMock(return_value={"status": "ok", "data": {
            "text": "", "url": "https://example.test", "title": "t", "items": []}})
        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=DEVICE)), \
                patch.object(cudev, "start_task", AsyncMock(return_value={})), \
                patch.object(cudev, "send_command", send), \
                patch.object(cudev, "finish_task", AsyncMock(return_value={})), \
                patch.object(cudev, "renew_task", AsyncMock(return_value={})):
            await asyncio.wait_for(agent._run_loop(message(), "执行命令"), 10)
        methods = [c.args[4] for c in send.await_args_list]
        self.assertNotIn("script.run", methods)
        self.assertEqual(methods.count("browser.snapshot"), 2)

    async def test_write_receipt_is_checkpointed_and_reused_on_resume(self):
        path = "/tmp/work/report.docx"
        digest = "b" * 64
        prompts = []
        agent = self.make_agent([
            {"action": "write_document", "path": path, "text": "汇总"},
            {"action": "ask", "question": "继续吗", "options": ["继续"]},
        ], prompts=prompts)
        device = dict(DEVICE, capabilities=list(DEVICE["capabilities"]) + [
            "document.extract", "document.write_new", "file.write_new"])

        async def send_command(actor, device_id, session_id, task_id, method, params, timeout=45.0):
            if method == "browser.snapshot":
                return {"status": "ok", "data": {"text": "报表", "url": "https://example.test", "title": "t", "items": []}}
            if method == "document.write_new":
                return {"status": "ok", "data": {"created": True, "size": 6, "sha256": digest}}
            return {"status": "ok", "data": {}}

        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=device)), \
                patch.object(cudev, "start_task", AsyncMock(return_value={})), \
                patch.object(cudev, "send_command", side_effect=send_command), \
                patch.object(cudev, "finish_task", AsyncMock(return_value={})), \
                patch.object(cudev, "renew_task", AsyncMock(return_value={})):
            first = await asyncio.wait_for(agent._run_loop(message(), "生成汇总"), 10)
        self.assertEqual(first["action"], "ask")
        checkpoint = first["checkpoint"]
        self.assertEqual(checkpoint["confirmed_steps"], 1)
        self.assertEqual(checkpoint["receipts"][0]["path"], path)
        self.assertEqual(checkpoint["receipts"][0]["sha256"], digest)

        resumed = self.make_agent([{"action": "done", "result": "已完成"}], prompts=prompts)
        resumed.device_id = "dev_test_1"
        with patch.object(cudev, "get_selected_device", AsyncMock(return_value=device)), \
                patch.object(cudev, "start_task", AsyncMock(return_value={})), \
                patch.object(cudev, "send_command", side_effect=send_command), \
                patch.object(cudev, "finish_task", AsyncMock(return_value={})), \
                patch.object(cudev, "renew_task", AsyncMock(return_value={})):
            second = await asyncio.wait_for(resumed._run_loop(message(), "生成汇总", checkpoint), 10)
        self.assertEqual(second["action"], "done")
        self.assertTrue(any(digest in text and "已确认步骤: 1" in text for text in prompts))

    async def test_desktop_optin_setting_can_only_tighten_device_capabilities(self):
        from core import cu_bridge
        device = dict(DEVICE, capabilities=list(DEVICE["capabilities"]) + [
            "desktop.inspect", "desktop.request_control", "desktop.act"])

        async def run(optin):
            agent = self.make_agent([{"action": "desktop_inspect"},
                                     {"action": "done", "result": "结束"}])
            send = AsyncMock(side_effect=lambda *a, **k: {"status": "ok", "data": {
                "text": "", "url": "https://example.test", "title": "t", "items": []}}
                if a[4] == "browser.snapshot" else {"status": "ok", "data": {"windows": []}})
            prefs = AsyncMock(return_value={"push_frames": False, "desktop_optin": optin})
            with patch.object(cu_bridge, "get_prefs", prefs), \
                    patch.object(cudev, "get_selected_device", AsyncMock(return_value=device)), \
                    patch.object(cudev, "start_task", AsyncMock(return_value={})), \
                    patch.object(cudev, "send_command", send), \
                    patch.object(cudev, "finish_task", AsyncMock(return_value={})), \
                    patch.object(cudev, "renew_task", AsyncMock(return_value={})):
                await asyncio.wait_for(agent._run_loop(message(), "看桌面"), 10)
            return [c.args[4] for c in send.await_args_list]

        self.assertNotIn("desktop.inspect", await run(False))
        self.assertIn("desktop.inspect", await run(True))

    async def test_desktop_actions_require_capability_and_map_native_approval_params(self):
        agent = ComputerAgent.__new__(ComputerAgent)
        calls = []

        async def device_cmd(actor, device_id, session_id, task_id, method, params=None, timeout=45.0):
            calls.append((method, params or {}, timeout))
            return {"status": "ok", "data": {"approved": True}}

        agent._device_cmd = device_cmd
        args = ("alice", "dev", "s1", "t1", [])
        with self.assertRaises(ValueError):
            await agent._device_action(*args, {"action": "desktop_inspect"}, caps=set())
        self.assertEqual(calls, [])

        caps = {"desktop.inspect", "desktop.request_control", "desktop.act"}
        await agent._device_action(*args, {"action": "desktop_request_control", "window_id": 7,
                                          "scopes": ["click", "shell"], "reason": "核对报表"}, caps=caps)
        self.assertEqual(calls[-1][0], "desktop.request_control")
        self.assertEqual(calls[-1][1], {"window_id": 7, "scopes": ["click"], "reason": "核对报表"})
        self.assertEqual(calls[-1][2], 90)

        await agent._device_action(*args, {"action": "desktop_act", "desktop_action": "keys",
                                          "keys": ["CTRL", "S"]}, caps=caps)
        self.assertEqual(calls[-1][0], "desktop.act")
        self.assertEqual(calls[-1][1], {"action": "keys", "keys": ["CTRL", "S"]})

        # UIA: 带 window_id 的只读元素列表 + 元素级 invoke（参数白名单与类型校验）。
        await agent._device_action(*args, {"action": "desktop_inspect", "window_id": 7}, caps=caps)
        self.assertEqual(calls[-1][0], "desktop.inspect")
        self.assertEqual(calls[-1][1], {"limit": 30, "window_id": 7})
        await agent._device_action(*args, {"action": "desktop_act", "desktop_action": "invoke",
                                          "element": 2, "role": "button", "name": "保存"}, caps=caps)
        self.assertEqual(calls[-1][1], {"action": "invoke", "element": 2, "role": "button",
                                        "name": "保存"})
        for bad in ({"action": "desktop_act", "desktop_action": "invoke", "element": 2,
                     "role": "button", "name": ""},
                    {"action": "desktop_act", "desktop_action": "invoke", "element": 80,
                     "role": "button", "name": "x"},
                    {"action": "desktop_act", "desktop_action": "invoke", "role": "button",
                     "name": "x"},
                    {"action": "desktop_act", "desktop_action": "double_click", "x": 1, "y": 2}):
            with self.assertRaises(ValueError):
                await agent._device_action(*args, bad, caps=caps)

    async def test_destructive_file_actions_require_separate_approval(self):
        agent = ComputerAgent.__new__(ComputerAgent)
        calls = []

        async def device_cmd(actor, device_id, session_id, task_id, method, params=None, timeout=45.0):
            calls.append((method, params or {}))
            return {"status": "ok", "data": {"moved": True, "deleted": True, "replaced": True,
                                              "sha256": "a" * 64, "size": 3,
                                              **(params or {})}}

        agent._device_cmd = device_cmd
        args = ("alice", "dev", "s1", "t1", [])
        agent._take_approval = AsyncMock(return_value=False)
        with self.assertRaises(CuApprovalRequired):
            await agent._device_action(*args, {"action": "file_delete",
                                              "path": "C:\\Users\\alice\\secret.txt"})
        self.assertEqual(calls, [])
        agent._take_approval = AsyncMock(return_value=True)
        await agent._device_action(*args, {"action": "file_move", "source": "C:\\a.txt",
                                          "target": "C:\\b.txt"})
        self.assertEqual(calls[-1], ("file.move", {"source": "C:\\a.txt", "target": "C:\\b.txt"}))
        await agent._device_action(*args, {"action": "file_overwrite", "path": "C:\\b.txt",
                                          "text": "synthetic"})
        self.assertEqual(calls[-1], ("file.replace", {"path": "C:\\b.txt", "content": "synthetic"}))
        with self.assertRaises(ValueError):
            await agent._device_action(*args, {"action": "file_move", "source": "C:\\a.txt"})
        self.assertEqual(len(calls), 2)



if __name__ == "__main__":
    unittest.main()
