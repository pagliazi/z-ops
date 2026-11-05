"""告警闭环 L1 探针 — 备份 / DNS / TCP / NTP 只读实测

约束：
- 只做只读查询与连接测试：不触发备份、不改配置、不 ACK、不屏蔽告警；
- 探针异常一律降级为「健康未知」(ok=None)，调用方不得把未知当恢复依据；
- 目标解析与探测结果都是纯数据，便于单测注入与复现。
"""

import asyncio
import logging
import re
import socket
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("zops")

PROBE_KINDS = ("backup", "dns", "tcp", "ntp")
_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_DOMAIN = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})\b", re.I)


def _result(kind: str, results: List[Dict[str, Any]], unknown: List[str]) -> Dict[str, Any]:
    """统一结果体：ok=True 全健康 / False 有失败 / None 无法判定（无目标或全部未知）。"""
    checked = [r for r in results if r.get("status") in ("ok", "fail")]
    failed = [r for r in checked if r.get("status") == "fail"]
    if failed:
        ok: Optional[bool] = False
    elif checked:
        ok = True
    else:
        ok = None
    bits = [f"{len(checked)} 项实测"]
    details = [f"{r.get('target')}: {r.get('detail')}" for r in checked if r.get("detail")]
    if details:
        bits.append("；".join(details[:3]))
    if failed:
        bits.append("失败 " + "、".join(str(r.get("target")) for r in failed[:3]))
    if unknown:
        bits.append("未知 " + "、".join(unknown[:3]))
    return {
        "kind": kind, "ok": ok, "checked": len(checked),
        "failed": [r.get("target") for r in failed],
        "unknown": unknown, "results": results,
        "summary": ("无可测目标" if not results and not unknown
                    else "；".join(bits)),
        "probed_at": time.time(),
    }


# ═══ TCP ═══
def _tcp_once(host: str, port: int, timeout: float) -> Dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"status": "ok", "detail": "端口可达"}
    except socket.timeout:
        return {"status": "fail", "detail": "连接超时"}
    except ConnectionRefusedError:
        return {"status": "fail", "detail": "连接被拒绝"}
    except OSError as e:
        return {"status": "fail", "detail": str(e)[:60]}


async def probe_tcp(targets: List[str], timeout: float = 5) -> Dict[str, Any]:
    results, unknown = [], []
    for t in targets:
        host, _, port_s = str(t).rpartition(":")
        if not host or not port_s.isdigit():
            unknown.append(str(t))
            continue
        res = await asyncio.to_thread(_tcp_once, host, int(port_s), timeout)
        results.append(dict(res, target=t))
    return _result("tcp", results, unknown)


# ═══ DNS ═══
def _dns_once(domain: str, server: str, timeout: float) -> Dict[str, Any]:
    from agents.dns_api import get_dns_client
    client = get_dns_client()
    res = client.query(domain, "A", server or None)
    if res.get("success") is False:
        return {"status": "fail", "detail": str(res.get("error") or "解析失败")[:80]}
    answers = [r.get("result") for r in (res.get("successful_results") or []) if r.get("result")]
    if not answers:
        return {"status": "fail", "detail": "无解析结果"}
    return {"status": "ok", "detail": f"解析到 {len(answers)} 条记录"}


async def probe_dns(targets: List[str], timeout: float = 15) -> Dict[str, Any]:
    results, unknown = [], []
    for t in targets:
        domain, _, server = str(t).partition("|")
        if not domain:
            unknown.append(str(t))
            continue
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(_dns_once, domain, server, timeout), timeout=timeout)
        except Exception as e:
            unknown.append(f"{domain}({str(e)[:40]})")
            continue
        results.append(dict(res, target=t))
    return _result("dns", results, unknown)


# ═══ 备份作业（OPS-CORE-MGT 只读查询）═══
async def _backup_once(ip: str, timeout: float) -> Dict[str, Any]:
    """按前 3 段搜索后客户端按 server_url 精确过滤：
    OPS-CORE-MGT 的 search 不支持完整 IP 串，直接搜完整 IP 会得到 0 条。"""
    from agents.opscore_api import get_mgt_client
    from collections import Counter
    client = get_mgt_client()
    prefix = ".".join(ip.split(".")[:3])
    for ep, res_key in (("/api/brr/backups/", "job_last_result"),
                        ("/api/veeam-m365/backup-jobs/", "last_result")):
        try:
            data = await asyncio.wait_for(
                client.request("GET", ep, params={"search": prefix, "page_size": 50}),
                timeout=timeout)
        except Exception as e:
            logger.debug(f"[alert_probes] 备份查询失败 ({ip}, {ep}): {e}")
            continue
        rows = [r for r in ((data or {}).get("results") or [])
                if str(r.get("server_url") or r.get("server") or "").strip() == ip]
        if not rows:
            continue
        results = Counter(str(r.get(res_key) or "").strip() for r in rows)
        failed = results.get("Failed", 0)
        ok = results.get("Success", 0)
        if failed:
            return {"status": "fail", "detail": f"失败作业 {failed} 个（成功 {ok}）"}
        return {"status": "ok", "detail": f"作业全部成功（{ok} 个）"}
    return {"status": "unknown", "detail": "未查询到该主机的备份作业"}


async def probe_backup(targets: List[str], timeout: float = 30) -> Dict[str, Any]:
    results, unknown = [], []
    for t in targets:
        ip = str(t).split("|")[0].strip()
        if not _IPV4.fullmatch(ip):
            unknown.append(str(t))
            continue
        try:
            res = await _backup_once(ip, timeout)
        except Exception as e:
            unknown.append(f"{ip}({str(e)[:40]})")
            continue
        if res.get("status") == "unknown":
            unknown.append(ip)
            continue
        results.append(dict(res, target=ip))
    return _result("backup", results, unknown)


# ═══ NTP ═══
def _ntp_once(host: str, timeout: float) -> Dict[str, Any]:
    packet = b"\x1b" + 47 * b"\x00"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(packet, (host, 123))
        data, _ = sock.recvfrom(48)
    except socket.timeout:
        return {"status": "fail", "detail": "无响应（超时）"}
    except OSError as e:
        return {"status": "fail", "detail": str(e)[:60]}
    finally:
        sock.close()
    if len(data) < 48:
        return {"status": "fail", "detail": "响应过短"}
    stratum = data[1]
    if stratum == 0 or stratum >= 16:
        return {"status": "fail", "detail": f"未同步（stratum={stratum}）"}
    return {"status": "ok", "detail": f"stratum={stratum}"}


async def probe_ntp(targets: List[str], timeout: float = 5) -> Dict[str, Any]:
    results, unknown = [], []
    for t in targets:
        host = str(t).split("|")[0].strip()
        if not _IPV4.fullmatch(host):
            unknown.append(str(t))
            continue
        res = await asyncio.to_thread(_ntp_once, host, timeout)
        results.append(dict(res, target=host))
    return _result("ntp", results, unknown)


async def probe(kind: str, targets: List[str], timeout: float = 8) -> Dict[str, Any]:
    """按 kind 分发；无目标时返回 ok=None（未知），绝不返回健康。"""
    targets = [str(t) for t in (targets or []) if str(t).strip()][:6]
    if not targets:
        return _result(kind, [], [])
    if kind == "tcp":
        return await probe_tcp(targets, timeout)
    if kind == "dns":
        return await probe_dns(targets, max(timeout, 15))
    if kind == "backup":
        return await probe_backup(targets, max(timeout, 30))
    if kind == "ntp":
        return await probe_ntp(targets, timeout)
    return _result(kind, [], targets)


def extract_targets(kind: str, alarms: List[Dict[str, Any]], limit: int = 4) -> List[str]:
    """从告警里提取探针目标（确定性、去重、限流）。certificate 走 SSL 闭环，不在此提取。"""
    out: List[str] = []

    def _add(value: str) -> None:
        if value and value not in out and len(out) < limit:
            out.append(value)

    for a in alarms or []:
        text = " ".join([a.get("summary") or "", (a.get("fields") or {}).get("Summary") or ""])
        node = (a.get("node") or a.get("source") or "").strip()
        ips = _IPV4.findall(text)
        if kind == "tcp":
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})\D{1,4}(\d{2,5})", text)
            if m:
                _add(f"{m.group(1)}:{m.group(2)}")
            continue
        if kind == "backup" or kind == "ntp":
            _add(node if _IPV4.fullmatch(node) else (ips[0] if ips else ""))
            continue
        if kind == "dns":
            server = node if _IPV4.fullmatch(node) else (ips[0] if ips else "")
            for dom in _DOMAIN.findall(text):
                if dom.lower() in ("dns", "com", "net", "local", "lan"):
                    continue
                _add(f"{dom.lower()}|{server}")
            continue
    return out
