"""
Z-OPS — ssl_check: SSL 证书实测工具（供 nms_agent 告警分析自动核验 + 处理分析引用）

对告警中的网页地址（host:port）做 TLS 握手，提取对端证书计算剩余天数，
实现 checklist「浏览器输入告警网页地址，检查SSL证书过期时间」步骤的自动实测。
校验模式：仅取证书信息不做链校验（告警目标可能是自签/过期证书，校验必然失败）。

供 nms_agent（SSL证书过期预案的 tool_checks 实测）使用。
"""

import asyncio
import contextlib
import datetime
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import sys
import time
import uuid
from typing import Dict, List, Optional, Tuple

try:  # 本模块可被独立调用，缺失配置时回退默认 CA 解析
    import config
except Exception:  # pragma: no cover
    config = None  # type: ignore[assignment]

logger = logging.getLogger("zops")

# 容器内 Python 可能编译自 /usr/local/ssl，默认 CA 路径不存在（capath 为空），
# 不显式指定会退化为"无信任库"，把所有公网证书误判为 untrusted。
_CA_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
)


def _default_ca_file() -> str:
    """解析实际可用的 CA 包：配置优先，其次 certifi，再次常见系统路径。

    全部缺失时返回空串，交给 OpenSSL 默认路径（此时严格校验会失败，
    但不会静默关闭校验）。
    """
    configured = str(getattr(config, "NMS_SSL_CA_FILE", "") or "").strip() if config else ""
    if configured:
        return configured
    try:
        import certifi  # 项目依赖，指向发行版 CA 包

        path = certifi.where()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    for cand in _CA_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return ""


# 从告警摘要提取网页地址: position=app.example.internal:443 → host=app.example.internal, port=443
_POSITION_RE = re.compile(r"position[=:]\s*([^,\s)()]+)")
_HOST_PORT_RE = re.compile(
    r"([A-Za-z0-9](?:[A-Za-z0-9-]*\.)+[A-Za-z]{2,})(?::(\d{1,5}))?"
)


def extract_cert_targets(text: str) -> List[Dict]:
    """从告警文本提取证书检查目标 [{host, port}]（position 中的域名:端口，默认 443）。

    识别 NMS 实际告警格式: ssl证书可用天数: 0 (app=..., ip=..., position=域名:443)
    """
    targets: List[Dict] = []
    seen = set()
    for m in _POSITION_RE.finditer(text or ""):
        pos = m.group(1)
        hm = _HOST_PORT_RE.search(pos)
        if not hm:
            continue
        host, port = hm.group(1), int(hm.group(2)) if hm.group(2) else 443
        if not (1 <= port <= 65535):
            continue
        key = f"{host.lower()}|{port}"
        if key not in seen:
            seen.add(key)
            targets.append({"host": host, "port": port})
    return targets


def check_cert(host: str, port: int = 443, timeout: int = 8) -> Dict:
    """TLS 握手提取对端证书并计算剩余天数。

    返回 {success, days_left, expires, subject, issuer} 或 {success: False, error}。
    """
    try:
        ctx = ssl.create_default_context()
        # 仅取证书信息，不做主机名/链校验（告警目标可能是自签/过期证书）
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                der = ssock.getpeercert(binary_form=True)
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        pem = ssl.DER_cert_to_PEM_cert(der)
        cert = x509.load_pem_x509_certificate(pem.encode(), default_backend())
        now = datetime.datetime.utcnow()
        expires = cert.not_valid_after  # naive UTC
        days = (expires - now).days
        return {
            "success": True,
            "days_left": days,
            "expires": expires.date().isoformat(),
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
        }
    except socket.timeout:
        return {"success": False, "error": "连接超时"}
    except socket.gaierror as e:
        return {"success": False, "error": f"域名解析失败: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)[:80]}


# ═══════════════════════════════════════════════════════════════════════════
# 严格取证（SSL 调查闭环专用）
#
# 与 check_cert 的区别：本段做**完整证书验证**（系统信任库 + 主机名 + 有效期），
# 只有全部地址均通过才算 healthy；任何无法取证的路径一律 unknown，绝不把
# “握手成功、能读证书”当成“证书健康”。全部探测在可终止的短生命周期子进程中
# 执行，父进程超时直接 kill，避免取消协程后遗留阻塞任务。
# ═══════════════════════════════════════════════════════════════════════════

# 原始 Summary 里的 position=域名:端口（完整值，不做截断）
_POSITION_FULL_RE = re.compile(r"position\s*[=:]\s*([^\s,;)]+)")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_IP_LITERAL_RE = re.compile(r"^[0-9.]+$|^\[?[0-9a-fA-F:]+\]?$")

# 允许的 RFC1918 私网段（真实内部域名解析到这些地址是正常的）
_ALLOWED_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
# 云元数据地址（部分不在 link-local 判定内，单独列出）
_METADATA_IPS = {"169.254.169.254", "fd00:ec2::254", "100.100.100.200"}


def _normalize_host(raw: str) -> str:
    """域名规范化：IDNA + 小写 + 去末尾点。IP 字面量/非法标签一律拒绝。"""
    host = (raw or "").strip()
    if not host or len(host) > 253:
        raise ValueError("域名缺失或过长")
    if any(ch in host for ch in "/@?#\\ \t"):
        raise ValueError("目标不是纯域名")
    if _IP_LITERAL_RE.match(host):
        raise ValueError("不接受 IP 字面量目标")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except Exception:
        raise ValueError("域名 IDNA 规范化失败")
    labels = ascii_host.split(".")
    if len(labels) < 2 or any(not _LABEL_RE.match(x) for x in labels):
        raise ValueError("域名格式非法")
    if len(labels[-1]) < 2:
        raise ValueError("顶级域非法")
    return ascii_host


def _parse_targets(alarm: Dict) -> Tuple[str, int]:
    """从原始告警 ``fields.Summary`` 解析唯一目标（不校验端口白名单）。"""
    fields = alarm.get("fields")
    text = ""
    if isinstance(fields, dict):
        text = str(fields.get("Summary") or fields.get("summary") or "")
    if not text:
        raise ValueError("缺少原始告警字段 Summary，无法确定取证目标")
    raw_positions = [m.group(1).strip() for m in _POSITION_FULL_RE.finditer(text)]
    if not raw_positions:
        raise ValueError("告警中未找到 position 目标")
    targets = set()
    for pos in raw_positions:
        if "://" in pos:
            raise ValueError("不接受 URI 形式的目标")
        host_part, _, port_part = pos.rpartition(":")
        if not host_part:            # 没有端口
            host_part, port_part = pos, ""
        elif not port_part.isdigit():
            raise ValueError("目标端口格式非法")
        if "@" in host_part:
            raise ValueError("不接受携带凭据的目标")
        host = _normalize_host(host_part)
        port = int(port_part) if port_part else 443
        if not (1 <= port <= 65535):
            raise ValueError("目标端口越界")
        targets.add((host, port))
    if len(targets) != 1:
        raise ValueError("告警包含多个取证目标，身份歧义")
    return next(iter(targets))


def extract_target_lenient(alarm: Dict) -> Dict:
    """宽松解析目标：仅保证唯一、格式合法，不做端口白名单校验。

    仅用于「人工核验模式」的调查身份与 NMS 行比对（该模式不发起 TLS 探测），
    绝不能作为自动取证的入参。
    """
    host, port = _parse_targets(alarm)
    return {"host": host, "port": port}


def extract_strict_target(alarm: Dict, allowed_ports: Tuple[int, ...] = (443,)) -> Dict:
    """从原始告警 ``fields.Summary`` 的完整 position 解析唯一取证目标。

    只接受服务端可控的原始字段，不接受前端传入的 host/port/URI；多目标、
    身份歧义、IP 字面量、非白名单端口全部抛 ValueError（API 层转 422）。
    返回 ``{"host": <idna 小写域名>, "port": <int>}``。
    """
    target = extract_target_lenient(alarm)
    allowed = tuple(int(p) for p in (allowed_ports or (443,)))
    if target["port"] not in allowed:
        raise ValueError(f"端口 {target['port']} 不在允许取证范围 {list(allowed)}")
    return target


def _check_address(ip_text: str) -> str:
    """地址安全策略：放行 RFC1918 与公网，拒绝回环/链路本地/组播/保留/元数据。"""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        raise ValueError("地址不可解析")
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # ::ffff:127.0.0.1 之类的映射绕过按内层 IPv4 重新判定
        return _check_address(str(ip.ipv4_mapped))
    if str(ip) in _METADATA_IPS:
        raise ValueError("拒绝云元数据地址")
    if (ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_unspecified or ip.is_reserved):
        raise ValueError("拒绝回环/链路本地/组播/保留地址")
    if any(ip in net for net in _ALLOWED_PRIVATE_NETS):
        return str(ip)
    if ip.is_global:
        return str(ip)
    raise ValueError("拒绝非公网且非内网段的地址")


def _resolve_addresses(host: str, port: int) -> List[Tuple[int, str]]:
    """解析一次并去重（保留 IPv4 优先顺序），每轮只解析一次。"""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: List[Tuple[int, str]] = []
    seen = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        if ip in seen:
            continue
        seen.add(ip)
        out.append((family, ip))
    return out


def _load_cert(der: Optional[bytes]) -> Optional[Dict]:
    if not der:
        return None
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        import hashlib

        cert = x509.load_der_x509_certificate(der, default_backend())
        # cryptography >= 42 提供 tz-aware 属性；旧版按 UTC 补齐
        nb = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before.replace(
            tzinfo=datetime.timezone.utc)
        na = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(
            tzinfo=datetime.timezone.utc)
        try:
            san = list(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
                       .value.get_values_for_type(x509.DNSName))
        except Exception:
            san = []
        return {
            "fingerprint": hashlib.sha256(der).hexdigest(),
            "not_before": nb.astimezone(datetime.timezone.utc).isoformat(),
            "not_after": na.astimezone(datetime.timezone.utc).isoformat(),
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "san": san,
            "_not_after_dt": na,
        }
    except Exception as e:
        logger.debug(f"[ssl_check] 证书解析失败: {e}")
        return None


def _certificate_verdict(verified: bool, remaining_seconds: Optional[int],
                         min_days: int) -> Tuple[bool, str, str]:
    """证书有效期门槛判定（按秒比较，避免整数天舍入造成 30 天边界误判）。"""
    if not verified or remaining_seconds is None:
        return verified, "", ""
    if remaining_seconds <= 0:
        return False, "expired", f"证书已过期 {abs(remaining_seconds) // 86400} 天"
    if remaining_seconds < int(min_days) * 86400:
        return False, "expiring_soon", f"剩余 {remaining_seconds // 86400} 天，低于 {int(min_days)} 天观察门槛"
    return True, "ok", ""


def _tls_probe(ip: str, family: int, host: str, port: int, policy: Dict) -> Dict:
    """对固定 IP 做严格 TLS 握手（保留原域名 SNI），返回单地址证据。"""
    entry: Dict = {"ip": ip, "verified": False, "error": ""}
    sock = None
    ssock = None
    der: Optional[bytes] = None
    try:
        ctx = ssl.create_default_context(
            cafile=policy.get("ca_file") or _default_ca_file() or None)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except Exception:
            pass
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(float(policy.get("connect_timeout") or 8))
        sock.connect((ip, port))
        ssock = ctx.wrap_socket(sock, server_hostname=host)
        der = ssock.getpeercert(binary_form=True)
        entry["verified"] = True
    except ssl.SSLCertVerificationError as e:
        entry["error"] = (getattr(e, "verify_message", "") or str(e))[:200]
        # 严格握手失败后仍读一次证书用于解释（该证据永远不能证明健康）
        with contextlib.suppress(Exception):
            raw = socket.socket(family, socket.SOCK_STREAM)
            raw.settimeout(float(policy.get("connect_timeout") or 8))
            raw.connect((ip, port))
            unauth = ssl._create_unverified_context()
            wrapped = unauth.wrap_socket(raw, server_hostname=host)
            try:
                der = wrapped.getpeercert(binary_form=True)
            finally:
                wrapped.close()
        entry["defect_type"] = "expired" if "expired" in entry["error"].lower() else (
            "hostname_mismatch" if "hostname" in entry["error"].lower()
            or "ip address mismatch" in entry["error"].lower() else "untrusted")
    except socket.timeout:
        entry["error"] = "连接超时"
        entry["defect_type"] = "timeout"
    except socket.gaierror as e:
        entry["error"] = f"域名解析失败: {e}"[:120]
        entry["defect_type"] = "resolve_failed"
    except (ConnectionError, OSError) as e:
        entry["error"] = f"连接失败: {e}"[:120]
        entry["defect_type"] = "connect_failed"
    except Exception as e:
        entry["error"] = str(e)[:120]
        entry["defect_type"] = "probe_error"
    finally:
        for s in (ssock, sock):
            with contextlib.suppress(Exception):
                s.close()

    cert = _load_cert(der)
    if cert:
        na = cert.pop("_not_after_dt")
        remaining = (na - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        entry.update({k: v for k, v in cert.items()})
        entry["remaining_seconds"] = int(remaining)
        ok, defect, msg = _certificate_verdict(
            bool(entry["verified"]), entry["remaining_seconds"],
            int(policy.get("min_days", 30)))
        if not ok and defect:
            entry["verified"] = False
            entry["defect_type"] = defect
            entry["error"] = entry["error"] or msg
    elif entry["verified"]:
        entry["verified"] = False
        entry["defect_type"] = entry.get("defect_type") or "incomplete"
        entry["error"] = entry["error"] or "无法解析证书"
    return entry


def _probe_sync(target: Dict, policy: Dict) -> Dict:
    """同步执行一轮完整取证（在子进程中调用）。"""
    host = target["host"]
    port = int(target["port"])
    observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result: Dict = {
        "diagnosis": "unknown", "defect_type": "", "observed_at": observed_at,
        "addresses": [], "certificates": [], "error": "",
        "target": {"host": host, "port": port},
        "min_days": int(policy.get("min_days", 30)),
    }
    try:
        resolved = _resolve_addresses(host, port)
    except Exception as e:
        result["defect_type"] = "resolve_failed"
        result["error"] = f"域名解析失败: {e}"[:160]
        return result
    if not resolved:
        result["defect_type"] = "resolve_failed"
        result["error"] = "域名无可用地址"
        return result
    max_addr = int(policy.get("max_addresses") or 4)
    if len(resolved) > max_addr:
        result["addresses"] = [ip for _f, ip in resolved]
        result["defect_type"] = "too_many_addresses"
        result["error"] = f"解析到 {len(resolved)} 个地址，超过单轮核验上限 {max_addr}"
        return result
    try:
        checked = [(fam, _check_address(ip)) for fam, ip in resolved]
    except ValueError as e:
        result["addresses"] = [ip for _f, ip in resolved]
        result["defect_type"] = "address_blocked"
        result["error"] = str(e)
        return result

    entries: List[Dict] = []
    for fam, ip in checked:
        entries.append(_tls_probe(ip, fam, host, port, policy))
    result["addresses"] = [e["ip"] for e in entries]
    result["certificates"] = entries

    defects = [e.get("defect_type") for e in entries if not e.get("verified")]
    if defects:
        hard = [d for d in defects if d in (
            "expired", "expiring_soon", "hostname_mismatch", "untrusted")]
        result["defect_type"] = hard[0] if hard else (defects[0] or "unknown")
        result["diagnosis"] = "unhealthy" if hard else "unknown"
        result["error"] = "; ".join(
            f"{e['ip']}: {e.get('error', '')}" for e in entries if not e.get("verified"))[:300]
        return result
    fps = {e.get("fingerprint") for e in entries}
    if len(fps) > 1:
        result["defect_type"] = "inconsistent_certificates"
        result["error"] = "同一轮内不同地址返回了不同证书"
        return result
    result["diagnosis"] = "healthy"
    result["defect_type"] = "ok"
    return result


class _ProbeTimeout(Exception):
    pass


async def _spawn_probe(payload: Dict, timeout: float) -> Dict:
    """短生命周期子进程执行阻塞探测；超时直接 kill，保证调用方一定返回。"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, os.path.abspath(__file__), "--probe", json.dumps(payload, ensure_ascii=False),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=max(1.0, timeout))
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise _ProbeTimeout()
    except asyncio.CancelledError:
        # 调用方取消（租约丢失 / 服务停止）时同样回收子进程，避免遗留阻塞任务
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(BaseException):
            await proc.wait()
        raise
    if proc.returncode != 0 or not out:
        raise RuntimeError((err or b"").decode("utf-8", "ignore")[:200] or "探测子进程失败")
    return json.loads(out.decode("utf-8"))


async def probe_cert(target: Dict, policy: Optional[Dict] = None) -> Dict:
    """严格证书核验（异步入口）。

    target: ``{"host": 域名, "port": 443}``（必须来自 extract_strict_target）
    policy: min_days / allowed_ports / ca_file / dns_timeout / connect_timeout /
            total_timeout / max_addresses
    返回 ``{diagnosis, defect_type, observed_at, addresses, certificates, error?}``；
    healthy 表示所有地址的证书链可信、域名匹配、有效期满足门槛。
    """
    pol = {
        "min_days": int((policy or {}).get("min_days", 30)),
        # 子进程不继承解释器默认路径问题，必须显式传可用 CA 包
        "ca_file": (policy or {}).get("ca_file", "") or _default_ca_file(),
        "dns_timeout": float((policy or {}).get("dns_timeout", 5)),
        "connect_timeout": float((policy or {}).get("connect_timeout", 8)),
        "total_timeout": float((policy or {}).get("total_timeout", 40)),
        "max_addresses": int((policy or {}).get("max_addresses", 4)),
    }
    host = (target or {}).get("host") or ""
    port = int((target or {}).get("port") or 443)
    started = time.monotonic()
    try:
        result = await _spawn_probe(
            {"target": {"host": host, "port": port}, "policy": pol},
            pol["total_timeout"])
    except _ProbeTimeout:
        result = {
            "diagnosis": "unknown", "defect_type": "timeout",
            "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "addresses": [], "certificates": [],
            "error": f"核验超时（>{int(pol['total_timeout'])}s）",
        }
    except Exception as e:
        result = {
            "diagnosis": "unknown", "defect_type": "probe_error",
            "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "addresses": [], "certificates": [],
            "error": str(e)[:200],
        }
    result["target"] = {"host": host, "port": port}
    result["min_days"] = pol["min_days"]
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    result["probe_id"] = uuid.uuid4().hex
    return result


def _main(argv: List[str]) -> int:
    """子进程入口: python ssl_check.py --probe '<json>'"""
    if len(argv) >= 3 and argv[1] == "--probe":
        payload = json.loads(argv[2])
        out = _probe_sync(payload["target"], payload.get("policy") or {})
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()
        return 0
    sys.stderr.write("usage: ssl_check.py --probe '<json>'\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
