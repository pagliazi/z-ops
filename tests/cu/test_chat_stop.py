"""聊天停止键贯通本机设备: 请求 ≠ 已停止, 只认设备回执。"""

import unittest
from unittest.mock import AsyncMock, patch

import webchat_api  # noqa: F401  先初始化应用, 避免路由循环导入
from api.routers.chat import _cu_cancel
from core.cu import devices as cudev


def service(entries, cancel_state=None, cancel_error=None):
    """entries: {task_id: 任务记录}; 未命中抛 ValueError, 与真实服务一致。"""
    svc = AsyncMock()

    async def task_record(actor, task_id, check=True):
        if task_id in entries:
            return ("task-key", dict(entries[task_id], task_id=task_id))
        raise ValueError("任务不存在或无权访问")

    svc.task_record = AsyncMock(side_effect=task_record)
    svc.cancel_task = (AsyncMock(side_effect=cancel_error) if cancel_error
                       else AsyncMock(return_value={"state": cancel_state or ""}))
    return svc


class ChatStopForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_task_forwards_cancel_and_stays_unconfirmed(self):
        svc = service({"m1": {"state": "running"}}, cancel_state="cancel_requested")
        with patch.object(cudev, "get_service", AsyncMock(return_value=svc)):
            cu = await _cu_cancel({"msg_id": "m1"}, "rt-1", "alice")
        svc.task_record.assert_awaited_once_with("alice", "m1", check=False)
        svc.cancel_task.assert_awaited_once_with("alice", "m1")
        self.assertTrue(cu["requested"])
        self.assertFalse(cu["confirmed"])
        self.assertFalse(cu["unknown"])

    async def test_device_confirmed_stop_is_reported_as_confirmed(self):
        svc = service({"m1": {"state": "running"}}, cancel_state="cancelled")
        with patch.object(cudev, "get_service", AsyncMock(return_value=svc)):
            cu = await _cu_cancel({"msg_id": "m1"}, "rt-1", "alice")
        self.assertTrue(cu["confirmed"])

    async def test_unknown_result_is_not_treated_as_stopped(self):
        svc = service({"m1": {"state": "running"}}, cancel_state="unknown")
        with patch.object(cudev, "get_service", AsyncMock(return_value=svc)):
            cu = await _cu_cancel({"msg_id": "m1"}, "rt-1", "alice")
        self.assertEqual(cu["state"], "unknown")
        self.assertFalse(cu["confirmed"])

    async def test_already_terminal_task_is_not_recontrolled(self):
        svc = service({"m1": {"state": "cancelled"}})
        with patch.object(cudev, "get_service", AsyncMock(return_value=svc)):
            cu = await _cu_cancel({"msg_id": "m1"}, "rt-1", "alice")
        svc.cancel_task.assert_not_called()
        self.assertTrue(cu["confirmed"])

    async def test_runtime_task_id_is_fallback_when_message_id_missing(self):
        svc = service({"rt-1": {"state": "cancelled"}})
        with patch.object(cudev, "get_service", AsyncMock(return_value=svc)):
            cu = await _cu_cancel({}, "rt-1", "alice")
        self.assertTrue(cu["confirmed"])

    async def test_no_device_task_returns_none(self):
        svc = service({})
        with patch.object(cudev, "get_service", AsyncMock(return_value=svc)):
            self.assertIsNone(await _cu_cancel({"msg_id": "m1"}, "rt-1", "alice"))
        svc.cancel_task.assert_not_called()

    async def test_control_failure_never_claims_confirmation(self):
        svc = service({"m1": {"state": "running"}},
                      cancel_error=ValueError("任务已结束或控制请求无效"))
        with patch.object(cudev, "get_service", AsyncMock(return_value=svc)):
            cu = await _cu_cancel({"msg_id": "m1"}, "rt-1", "alice")
        self.assertEqual(cu["state"], "")
        self.assertFalse(cu["confirmed"])


if __name__ == "__main__":
    unittest.main()
