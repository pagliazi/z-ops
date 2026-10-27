#!/usr/bin/env python3
"""严格 SSL 取证单元测试（不依赖真实 NMS / 外部网络）。

覆盖：目标解析与白名单、地址安全策略（SSRF/元数据/内网）、
证书有效期门槛（29/30 天边界）、多地址聚合、未知绝不等于健康、
子进程硬超时；CA 信任库解析（配置 > certifi > 系统包）。
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, '/opt/zops')
os.chdir('/opt/zops')

from agents import ssl_check  # noqa: E402


def _alarm(text, **extra):
    return {"fields": {"Summary": text}, **extra}


class TestExtractStrictTarget(unittest.TestCase):
    def test_normalizes_case_and_idna(self):
        t = ssl_check.extract_strict_target(
            _alarm("ssl证书可用天数: 0 (app=a, position=AMS.Example.COM.:443)"))
        self.assertEqual(t, {"host": "ams.example.com", "port": 443})

    def test_default_port_is_443(self):
        self.assertEqual(
            ssl_check.extract_strict_target(_alarm("position=www.example.com"))["port"], 443)

    def test_rejects_ip_literal(self):
        with self.assertRaises(ValueError):
            ssl_check.extract_strict_target(_alarm("position=10.1.1.1:443"))

    def test_rejects_uri_and_credentials(self):
        for bad in ("position=https://www.example.com:443/x",
                    "position=user@www.example.com:443"):
            with self.assertRaises(ValueError):
                ssl_check.extract_strict_target(_alarm(bad))

    def test_rejects_port_outside_allowlist(self):
        with self.assertRaises(ValueError):
            ssl_check.extract_strict_target(_alarm("position=www.example.com:8443"))
        self.assertEqual(
            ssl_check.extract_strict_target(
                _alarm("position=www.example.com:8443"), allowed_ports=(443, 8443))["port"],
            8443)

    def test_rejects_multiple_targets(self):
        with self.assertRaises(ValueError):
            ssl_check.extract_strict_target(
                _alarm("position=www.example.com:443 position=api.example.net:443"))

    def test_rejects_missing_summary(self):
        with self.assertRaises(ValueError):
            ssl_check.extract_strict_target({"summary": "position=www.example.com:443"})

    def test_uses_full_summary_not_truncated(self):
        # 截断摘要会丢掉 position → 必须依赖 fields.Summary 的完整值
        alarm = _alarm("x" * 300 + " position=www.example.com:443",
                       summary="x" * 300)
        self.assertEqual(ssl_check.extract_strict_target(alarm)["host"], "www.example.com")


class TestAddressPolicy(unittest.TestCase):
    def test_allows_rfc1918_and_public(self):
        for ip in ("10.1.2.3", "172.20.1.1", "192.168.10.9", "8.8.8.8", "2606:4700:4700::1111"):
            self.assertEqual(ssl_check._check_address(ip), ip)

    def test_blocks_loopback_linklocal_multicast_metadata(self):
        for ip in ("127.0.0.1", "::1", "169.254.169.254", "fe80::1", "224.0.0.1",
                   "0.0.0.0", "100.100.100.200", "fd00:ec2::254"):
            with self.assertRaises(ValueError, msg=ip):
                ssl_check._check_address(ip)

    def test_blocks_ipv4_mapped_bypass(self):
        with self.assertRaises(ValueError):
            ssl_check._check_address("::ffff:127.0.0.1")
        with self.assertRaises(ValueError):
            ssl_check._check_address("::ffff:169.254.169.254")

    def test_blocks_shared_and_reserved(self):
        for ip in ("198.18.0.1", "240.0.0.1"):
            with self.assertRaises(ValueError, msg=ip):
                ssl_check._check_address(ip)


class TestCertificateVerdict(unittest.TestCase):
    def test_30_day_boundary_by_seconds(self):
        self.assertEqual(ssl_check._certificate_verdict(True, 30 * 86400, 30)[:2], (True, "ok"))
        ok, defect, _ = ssl_check._certificate_verdict(True, 30 * 86400 - 1, 30)
        self.assertFalse(ok)
        self.assertEqual(defect, "expiring_soon")

    def test_expired(self):
        ok, defect, _ = ssl_check._certificate_verdict(True, -5, 30)
        self.assertFalse(ok)
        self.assertEqual(defect, "expired")

    def test_unverified_never_returns_ok(self):
        self.assertEqual(ssl_check._certificate_verdict(False, 10 ** 8, 30), (False, "", ""))


class TestProbeAggregation(unittest.TestCase):
    def _run(self, addresses, entries, policy=None):
        with patch.object(ssl_check, "_resolve_addresses",
                          return_value=[(2, a) for a in addresses]), \
             patch.object(ssl_check, "_tls_probe",
                          side_effect=lambda ip, *a, **k: entries[ip]):
            return ssl_check._probe_sync({"host": "www.example.com", "port": 443},
                                         policy or {"min_days": 30, "max_addresses": 4})

    def test_all_healthy(self):
        r = self._run(["10.0.0.1", "10.0.0.2"], {
            "10.0.0.1": {"ip": "10.0.0.1", "verified": True, "fingerprint": "aa"},
            "10.0.0.2": {"ip": "10.0.0.2", "verified": True, "fingerprint": "aa"},
        })
        self.assertEqual(r["diagnosis"], "healthy")
        self.assertEqual(r["defect_type"], "ok")

    def test_one_unhealthy_makes_round_unhealthy(self):
        r = self._run(["10.0.0.1", "10.0.0.2"], {
            "10.0.0.1": {"ip": "10.0.0.1", "verified": True, "fingerprint": "aa"},
            "10.0.0.2": {"ip": "10.0.0.2", "verified": False,
                         "defect_type": "hostname_mismatch", "error": "Hostname mismatch"},
        })
        self.assertEqual(r["diagnosis"], "unhealthy")
        self.assertEqual(r["defect_type"], "hostname_mismatch")

    def test_connect_failure_is_unknown(self):
        r = self._run(["10.0.0.1"], {
            "10.0.0.1": {"ip": "10.0.0.1", "verified": False,
                         "defect_type": "timeout", "error": "连接超时"},
        })
        self.assertEqual(r["diagnosis"], "unknown")

    def test_too_many_addresses_is_unknown(self):
        r = self._run(["10.0.0.1", "10.0.0.2", "10.0.0.3"], {}, {"max_addresses": 2})
        self.assertEqual(r["diagnosis"], "unknown")
        self.assertEqual(r["defect_type"], "too_many_addresses")

    def test_blocked_address_stops_round(self):
        r = self._run(["127.0.0.1"], {})
        self.assertEqual(r["diagnosis"], "unknown")
        self.assertEqual(r["defect_type"], "address_blocked")

    def test_inconsistent_certificates_not_healthy(self):
        r = self._run(["10.0.0.1", "10.0.0.2"], {
            "10.0.0.1": {"ip": "10.0.0.1", "verified": True, "fingerprint": "aa"},
            "10.0.0.2": {"ip": "10.0.0.2", "verified": True, "fingerprint": "bb"},
        })
        self.assertEqual(r["diagnosis"], "unknown")
        self.assertEqual(r["defect_type"], "inconsistent_certificates")

    def test_resolution_failure_is_unknown(self):
        with patch.object(ssl_check, "_resolve_addresses", side_effect=OSError("boom")):
            r = ssl_check._probe_sync({"host": "www.example.com", "port": 443}, {})
        self.assertEqual(r["diagnosis"], "unknown")
        self.assertEqual(r["defect_type"], "resolve_failed")


class TestAsyncProbe(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_returns_unknown(self):
        async def _boom(payload, timeout):
            raise ssl_check._ProbeTimeout()
        with patch.object(ssl_check, "_spawn_probe", _boom):
            r = await ssl_check.probe_cert({"host": "www.example.com", "port": 443},
                                           {"total_timeout": 1})
        self.assertEqual(r["diagnosis"], "unknown")
        self.assertEqual(r["defect_type"], "timeout")
        self.assertTrue(r["probe_id"])

    async def test_subprocess_roundtrip_blocks_localhost(self):
        r = await ssl_check.probe_cert({"host": "localhost", "port": 443},
                                       {"total_timeout": 20})
        self.assertEqual(r["diagnosis"], "unknown")
        self.assertIn(r["defect_type"], ("address_blocked", "connect_failed", "resolve_failed"))

    async def test_cancel_kills_subprocess(self):
        """调用方取消（如失去租约）时必须回收子进程，不得遗留阻塞任务。"""
        killed = {"v": False}

        class FakeProc:
            returncode = None

            async def communicate(self):
                await asyncio.sleep(30)

            def kill(self):
                killed["v"] = True

            async def wait(self):
                return 0

        async def fake_exec(*a, **k):
            return FakeProc()

        with patch.object(asyncio, "create_subprocess_exec", fake_exec):
            task = asyncio.create_task(ssl_check._spawn_probe({}, 30))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(killed["v"], "取消探测必须 kill 子进程")


class TestCaBundle(unittest.TestCase):
    """容器内 Python 默认 CA 路径可能不存在，必须解析到真实可用的信任库。"""

    def test_configured_path_wins(self):
        with patch.object(ssl_check, "config", create=True) as cfg:
            cfg.NMS_SSL_CA_FILE = "/tmp/custom-ca.pem"
            self.assertEqual(ssl_check._default_ca_file(), "/tmp/custom-ca.pem")

    def test_falls_back_to_existing_bundle(self):
        with patch.object(ssl_check, "config", create=True) as cfg:
            cfg.NMS_SSL_CA_FILE = ""
            ca = ssl_check._default_ca_file()
        self.assertTrue(ca and os.path.isfile(ca), ca)
        self.assertGreater(os.path.getsize(ca), 1024)

    def test_system_candidates_when_certifi_missing(self):
        with patch.object(ssl_check, "config", create=True) as cfg, \
                patch("certifi.where", return_value="/nonexistent/ca.pem"):
            cfg.NMS_SSL_CA_FILE = ""
            ca = ssl_check._default_ca_file()
        self.assertIn(ca, [p for p in ssl_check._CA_CANDIDATES if os.path.isfile(p)])

    def test_strict_probe_uses_resolved_bundle(self):
        captured = {}
        real = ssl_check.ssl.create_default_context

        def spy(*args, **kwargs):
            captured["cafile"] = kwargs.get("cafile")
            return real(*args, **kwargs)

        with patch.object(ssl_check.ssl, "create_default_context", spy):
            ssl_check._tls_probe("192.0.2.1", 2, "www.example.com", 443,
                                 {"connect_timeout": 0.01, "min_days": 30})
        self.assertTrue(captured["cafile"] and os.path.isfile(captured["cafile"]))


if __name__ == "__main__":
    unittest.main()
