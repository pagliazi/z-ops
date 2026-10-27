#!/usr/bin/env python3
"""L1 探针测试：目标提取、结果判定、TCP 真实连通性、未知不得当健康。"""

import asyncio
import os
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

from core import alert_probes as probes  # noqa: E402


def alarm(summary, source="", node="", group="", checklist=""):
    return {"summary": summary, "source": source, "node": node,
            "event_group": group, "checklist": checklist, "fields": {}}


class TestExtractTargets(unittest.TestCase):
    def test_backup_uses_node_ip(self):
        targets = probes.extract_targets("backup", [alarm("VBR 备份失败", node="10.1.1.5")])
        self.assertEqual(targets, ["10.1.1.5"])

    def test_backup_falls_back_to_summary_ip(self):
        targets = probes.extract_targets("backup", [alarm("备份失败 10.9.9.9")])
        self.assertEqual(targets, ["10.9.9.9"])

    def test_tcp_requires_port(self):
        self.assertEqual(probes.extract_targets(
            "tcp", [alarm("PDNS 10.2.2.2:53 unreachable")]), ["10.2.2.2:53"])
        self.assertEqual(probes.extract_targets("tcp", [alarm("服务不可用 10.2.2.2")]), [])

    def test_dns_pairs_domain_with_server(self):
        targets = probes.extract_targets(
            "dns", [alarm("无法解析 app.example.com", source="10.3.3.3")])
        self.assertEqual(targets, ["app.example.com|10.3.3.3"])

    def test_limit_and_dedup(self):
        alarms = [alarm("BGP down 10.1.1.1")] * 5
        self.assertEqual(probes.extract_targets("ntp", alarms, limit=2), ["10.1.1.1"])


class TestResult(unittest.TestCase):
    def test_ok_only_when_checked(self):
        self.assertIsNone(probes._result("tcp", [], [])["ok"])
        self.assertTrue(probes._result("tcp", [{"status": "ok"}], [])["ok"])
        self.assertFalse(probes._result("tcp", [{"status": "ok"}, {"status": "fail"}], [])["ok"])
        # 只有 unknown：不能算健康
        self.assertIsNone(probes._result("backup", [], ["10.1.1.5"])["ok"])


class TestTcpProbe(unittest.IsolatedAsyncioTestCase):
    async def test_listening_port_is_ok(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            res = await probes.probe_tcp([f"127.0.0.1:{port}"], timeout=2)
        finally:
            server.close()
        self.assertTrue(res["ok"])
        self.assertEqual(res["checked"], 1)

    async def test_closed_port_is_fail(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        res = await probes.probe_tcp([f"127.0.0.1:{port}"], timeout=1)
        self.assertFalse(res["ok"])

    async def test_bad_target_is_unknown(self):
        res = await probes.probe_tcp(["not-a-target"], timeout=1)
        self.assertIsNone(res["ok"])
        self.assertEqual(res["unknown"], ["not-a-target"])


class TestDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_no_targets_never_healthy(self):
        res = await probes.probe("backup", [], timeout=3)
        self.assertIsNone(res["ok"])
        self.assertIn("无可测目标", res["summary"])

    async def test_unknown_kind_is_unknown(self):
        res = await probes.probe("network", ["sw-01"], timeout=3)
        self.assertIsNone(res["ok"])

    async def test_backup_unknown_when_no_jobs(self):
        async def _fake(ip, timeout):
            return {"status": "unknown", "detail": "未查询到备份作业"}
        with patch.object(probes, "_backup_once", _fake):
            res = await probes.probe_backup(["10.1.1.5"], timeout=3)
        self.assertIsNone(res["ok"])
        self.assertEqual(res["unknown"], ["10.1.1.5"])

    async def test_backup_failed_job_is_fail(self):
        async def _fake(ip, timeout):
            return {"status": "fail", "detail": "失败作业 2 个"}
        with patch.object(probes, "_backup_once", _fake):
            res = await probes.probe_backup(["10.1.1.5"], timeout=3)
        self.assertFalse(res["ok"])


class TestBackupFiltering(unittest.IsolatedAsyncioTestCase):
    """GCOC-MGT search 不支持完整 IP：必须按前 3 段搜索后客户端精确过滤。"""

    class _Client:
        def __init__(self, rows):
            self.rows, self.calls = rows, []

        async def request(self, method, path, params=None):
            self.calls.append((path, dict(params or {})))
            return {"results": self.rows}

    async def _run(self, ip, rows, ep_rows=None):
        client = self._Client(rows)
        with patch("agents.gcoc_api.get_mgt_client", lambda: client):
            status = await probes._backup_once(ip, 5)
        return status, client

    async def test_searches_prefix_and_filters_host(self):
        rows = [{"server_url": "10.1.1.9", "job_last_result": "Failed"},
                {"server_url": "10.1.1.5", "job_last_result": "Success"}]
        status, client = await self._run("10.1.1.5", rows)
        self.assertEqual(status["status"], "ok")
        self.assertEqual(client.calls[0][1]["search"], "10.1.1")

    async def test_other_host_failure_not_counted(self):
        rows = [{"server_url": "10.1.1.9", "job_last_result": "Failed"}]
        status, _ = await self._run("10.1.1.5", rows)
        self.assertEqual(status["status"], "unknown")

    async def test_own_failed_job_detected(self):
        rows = [{"server_url": "10.1.1.5", "job_last_result": "Failed"},
                {"server_url": "10.1.1.5", "job_last_result": "Success"}]
        status, _ = await self._run("10.1.1.5", rows)
        self.assertEqual(status["status"], "fail")


class TestNtpPacket(unittest.TestCase):
    def test_unsynchronized_is_not_ok(self):
        class _Sock:
            def __init__(self, payload):
                self.payload = payload

            def settimeout(self, _):
                pass

            def sendto(self, *_):
                pass

            def recvfrom(self, _):
                return self.payload, ("127.0.0.1", 123)

            def close(self):
                pass

        with patch.object(probes.socket, "socket", lambda *a, **k: _Sock(b"\x1b" + b"\x00" * 47)):
            self.assertFalse(probes._ntp_once("127.0.0.1", 1)["status"] == "ok")
        payload = bytearray(b"\x1b" + b"\x00" * 47)
        payload[1] = 3
        with patch.object(probes.socket, "socket", lambda *a, **k: _Sock(bytes(payload))):
            self.assertEqual(probes._ntp_once("127.0.0.1", 1)["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
