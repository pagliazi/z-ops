"""
Z-OPS — dns_api: OPS-CORE 信息管理系统 DNS API 客户端
参考 https://opscore-info.example.com/dns/api-doc (Swagger: /api/docs/ → /api/openapi.json)

认证: Authorization: Bearer <OPSCORE_API_TOKEN>
  (2026-10-27 起 145 关闭 AD 账号本地密码兜底, 脚本不再走 /api/auth/login/ 登录)
端点 (全部 POST JSON):
  /api/dns/query/    DNS 查询 (支持多行批量域名 + PTR 反向, record_type: A/AAAA/CNAME/MX/TXT/NS/SOA/PTR/SRV)
  /api/dns/compare/  多 DNS 服务器对比查询 (批量域名, 最多 20 域名 / 10 服务器)
  /api/dns/dkim/     DKIM 记录检查 (domain + selector)
  /api/dns/trace/    dig +trace 全链路解析
  /api/dns/spf/      SPF 记录检查
  /api/dns/dmarc/    DMARC 记录检查
  /api/dns/zone/     Zone Transfer (AXFR)

供 opscore_agent (chat DNS 工具) 与 nms_agent (告警分析自动解析) 共享使用。
"""

import asyncio
import logging
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

import httpx

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

# record_type 枚举 (来自 OpenAPI schema)
RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "SOA", "PTR", "SRV")

# IP / 域名提取正则
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6_RE = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:]{0,20}(?![0-9A-Fa-f:])")
_DOMAIN_RE = re.compile(
    r"(?<![a-zA-Z0-9_-])"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"(?:com|net|org|cn|com\.cn|net\.cn|org\.cn|hk|tw|gov\.cn|edu\.cn|"
    r"io|co|info|biz|name|me|cloud|online|site|top|vip|shop|club|xyz|"
    r"enterprise|corp|local|intra|corp|lan|internal)"
    r"(?![a-zA-Z0-9_-])"
)
# 过滤常见伪域名 (告警文本中的版本号/时间戳等)
_DOMAIN_BLACKLIST = {"http", "https", "www.com", "com.cn", "v1.0", "v2.0", "v1.1"}

# ── DNS 告警专项提取正则 (来自 NMS 实际告警格式) ──
# RelayDNS 告警: position=198.18.168.3_customer-a.example.com → server=198.18.168.3, domain=customer-a.example.com
_POSITION_SRV_DOMAIN_RE = re.compile(
    r"position[=:]\s*(\d{1,3}(?:\.\d{1,3}){3})_"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)"
)
# 显式 server 字段: server=1.2.3.4 / dns_server=1.2.3.4 / nameserver=1.2.3.4
_SRV_FIELD_RE = re.compile(r"(?:dns[_-]?|name)?server[=:]\s*(\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE)
# 告警级记录类型: record_type=MX.
_RT_FIELD_RE = re.compile(r"record_type[=:]\s*([A-Za-z]+)", re.IGNORECASE)
# Zabbix dns_query 监控项: __dns_query__homecrbcncn__no_data / __dns_query_portalcrccomcn_no_data
# (单/双下划线变体, 非贪婪提取编码域名)
_DNS_QUERY_ITEM_RE = re.compile(r"dns_query[_]{0,2}([A-Za-z0-9_.-]+?)[_]{0,2}no_data")


def extract_targets(text: str, max_items: int = 20) -> Tuple[List[str], List[str]]:
    """从文本提取 IP 与域名。

    Returns:
        (ips, domains) 元组，均去重保序、各不超过 max_items。
    """
    if not text:
        return [], []
    ips, domains = [], []
    seen_ip, seen_dom = set(), set()

    for m in _IPV6_RE.finditer(text):
        ip = m.group(0).strip(":")
        # IPv6 至少含 2 个冒号才可信
        if ip not in seen_ip and ip.count(":") >= 2:
            seen_ip.add(ip)
            ips.append(ip)
    for m in _IPV4_RE.finditer(text):
        ip = m.group(0)
        parts = ip.split(".")
        # 过滤明显非法段 (如 999.1.1.1 / 0.0.0.0)
        if any(int(p) > 255 for p in parts):
            continue
        if ip == "0.0.0.0" or ip.startswith("255."):
            continue
        if ip not in seen_ip:
            seen_ip.add(ip)
            ips.append(ip)

    for m in _DOMAIN_RE.finditer(text):
        d = m.group(0).lower().rstrip(".")
        if d in _DOMAIN_BLACKLIST or d in seen_dom:
            continue
        # 域名各标签必须以字母开头 (排除纯数字 ip 形态被误匹配)
        if not re.match(r"^[a-zA-Z]", d):
            continue
        seen_dom.add(d)
        domains.append(d)

    return ips[:max_items], domains[:max_items]


def restore_encoded_domain(name: str) -> List[str]:
    """尝试还原 Zabbix 监控项编码域名 (点号被去除的编码串): 返回候选列表, 原串在前。

    例如 portalcrccomcn → [portalcrccomcn, portalcrc.com.cn]
    仅还原常见 TLD 后缀, 宁缺勿滥——无法可靠还原时不猜。
    """
    cands = [name]
    for pat, rep in (("comcn", "com.cn"), ("netcn", "net.cn"), ("orgcn", "org.cn"),
                     ("comhk", "com.hk"), ("eduhk", "edu.hk")):
        if name.endswith(pat) and name != pat:
            cands.append(name[:-len(pat)] + rep)
    return cands


def extract_dns_test_targets(text: str) -> List[Dict]:
    """从 DNS 告警文本提取解析测试目标 [{domain, server, record_type}]。

    识别 NMS 实际告警格式 (准确提取，宁缺勿滥):
    - position=<dns_server_ip>_<domain> → 用告警指定的 server 解析该域名 (RelayDNS 告警)
    - __dns_query__<name>__           → 监控项域名 (Zabbix dns_query, 无指定 server)
    - record_type=<TYPE>.             → 告警级记录类型 (默认 A)
    - server=<ip> / dns_server=<ip>   → 显式 DNS 服务器
    - 回退: 以上模式均未命中时，提取文本中的域名用默认 server 解析
    """
    targets: List[Dict] = []
    seen = set()

    def add(domain: str, server: str = "", rt: str = "A"):
        domain = (domain or "").strip().rstrip(".")
        rt = (rt or "A").upper().rstrip(".")
        if rt not in RECORD_TYPES:
            rt = "A"
        key = f"{domain}|{server}|{rt}"
        if domain and key not in seen:
            seen.add(key)
            targets.append({"domain": domain, "server": server, "record_type": rt})

    m_rt = _RT_FIELD_RE.search(text)
    alert_rt = m_rt.group(1) if m_rt else "A"
    m_srv = _SRV_FIELD_RE.search(text)
    alert_srv = m_srv.group(1) if m_srv else ""

    # 1) position=<ip>_<domain> 主模式 (server 与域名成对出现)
    for m in _POSITION_SRV_DOMAIN_RE.finditer(text):
        add(m.group(2), m.group(1), alert_rt)
    # 2) __dns_query__<name>__ 监控项域名
    for m in _DNS_QUERY_ITEM_RE.finditer(text):
        add(m.group(1), alert_srv, alert_rt)
    # 3) 回退: 未命中任何模式时，提取文本中的域名用默认/告警 server 解析
    if not targets:
        _, domains = extract_targets(text, max_items=5)
        for d in domains:
            add(d, alert_srv, alert_rt)
    return targets


def format_dns_test_result(domain: str, record_type: str, server: str,
                           data: dict, label: str = "") -> str:
    """把单次 DNS 查询结果格式化为一行报告 (简化但准确，供 LLM 分析引用)。

    示例: "RelayDNS critical: customer-a.example.com [MX, server=198.18.168.3] → 10.1.1.1"
    """
    srv = f"server={server}" if server else "默认server"
    prefix = f"{label} " if label else ""
    restored = data.get("_restored_from")
    if restored:
        domain = f"{domain}(还原自监控项编码 {restored})"
    if data.get("success") is False:
        return f"- {prefix}{domain} [{record_type}, {srv}] → 解析失败: {data.get('error', '未知错误')}"
    sres = [r.get("result") for r in (data.get("successful_results") or []) if r.get("result")]
    eres = [r.get("result") for r in (data.get("error_results") or []) if r.get("result")]
    if sres:
        return f"- {prefix}{domain} [{record_type}, {srv}] → {'; '.join(sres[:3])}"
    if eres:
        return f"- {prefix}{domain} [{record_type}, {srv}] → {eres[0]}"
    return f"- {prefix}{domain} [{record_type}, {srv}] → 无记录"


class DnsApiClient:
    """OPS-CORE DNS API 客户端 (Token 鉴权 + 内存缓存)

    Token 是无状态凭据: 没有 24h 过期、没有并发 session 竞态, 因此不再需要
    登录/重登/请求串行化; 401/403 属鉴权类错误, 直接失败并告警,
    绝不回退账号密码登录 (每次试密都是一次 AD 绑定, 会把 svc-opscore 锁死)。
    """

    def __init__(self, base_url: str = None, token: str = ""):
        self.base_url = (base_url or config.DNS_API_URL).rstrip("/")
        self.token = (token or config.OPSCORE_API_TOKEN or "").strip()
        self._client: Optional[httpx.Client] = None
        self._auth_warned = False   # 401/403 只告警一次, 避免刷日志
        # 解析结果缓存: (endpoint, body_key) -> (ts, data)
        self._cache: Dict[str, Tuple[float, dict]] = {}
        self._cache_lock = threading.Lock()

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=config.DNS_API_TIMEOUT, verify=False, follow_redirects=True
            )
        return self._client

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    def _cache_get(self, cache_key: str) -> Optional[dict]:
        with self._cache_lock:
            hit = self._cache.get(cache_key)
        if hit and time.time() - hit[0] < config.DNS_API_CACHE_TTL:
            return hit[1]
        return None

    def _cache_put(self, cache_key: str, data: dict):
        with self._cache_lock:
            self._cache[cache_key] = (time.time(), data)
            # 简单防膨胀: 缓存超 512 条时清一半
            if len(self._cache) > 512:
                for k in list(self._cache)[:256]:
                    self._cache.pop(k, None)

    def _auth_error(self, status_code: int, detail: str) -> dict:
        """鉴权类错误: 告警一次并直接失败, 不回退账号密码登录"""
        if status_code == 0:
            hint = "未配置 OPSCORE_API_TOKEN (见 zops.env)"
        elif status_code == 401:
            hint = "OPS-CORE Token 无效/被吊销, 找 OPS-CORE 管理员核对"
        else:
            hint = "Token 缺 scope 或来源 IP 不在白名单(198.18.18.147), 找 OPS-CORE 管理员"
        if not self._auth_warned:
            logger.error(f"[dns_api] OPS-CORE 鉴权失败 ({status_code}): {detail[:120]} — {hint}")
            self._auth_warned = True
        return {"success": False, "error": f"鉴权失败 HTTP {status_code}: {hint}"}

    def _call(self, endpoint: str, body: dict, use_cache: bool = True) -> dict:
        """调用 DNS 端点 (POST JSON + Bearer Token)；429/5xx 退避重试, 401/403 直接失败"""
        url = f"{self.base_url}/api/dns/{endpoint}/"
        cache_key = f"{endpoint}:{json_dumps(body)}"
        if use_cache:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
        if not self.token:
            return self._auth_error(0, "未配置 OPSCORE_API_TOKEN (zops.env)")
        data = self._post_with_retry(url, body)
        if use_cache and data.get("success") is not False:
            self._cache_put(cache_key, data)
        return data

    def _post_with_retry(self, url: str, body: dict) -> dict:
        """单次调用 (限流 429 / 服务端 5xx 退避重试, 最多 3 次)"""
        last_error = f"请求失败: {url}"
        for attempt in range(3):
            try:
                resp = self._get_client().post(url, json=body, headers=self._headers())
            except httpx.TimeoutException:
                last_error = f"请求超时: {url}"
                time.sleep(2 * (attempt + 1))
                continue
            except Exception as e:
                return {"success": False, "error": f"请求失败: {str(e)}"}

            if resp.status_code in (401, 403):
                # 鉴权类错误立即失败: 重试只会浪费配额, 回退密码登录会锁 AD 账号
                return self._auth_error(resp.status_code, resp.text[:200])
            if resp.status_code == 429:
                # 服务端限流 (每分钟 30 次): 5/10/15 秒退避
                last_error = "HTTP 429: 查询过于频繁（限每分钟 30 次）"
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    return {"success": False, "error": f"响应非 JSON: {resp.text[:200]}"}
            try:
                err = resp.json().get("error", resp.text[:120])
            except Exception:
                err = resp.text[:120]
            return {"success": False, "error": f"HTTP {resp.status_code}: {err}"}
        return {"success": False, "error": last_error}

    # ── 端点封装 ──

    def query(self, domain: str, record_type: str = "A", server: str = "",
              expected_results: str = "") -> dict:
        """DNS 查询 (支持多行批量域名、PTR 反向)"""
        body = {"domain": domain, "record_type": record_type}
        if server:
            body["server"] = server
        if expected_results:
            body["expected_results"] = expected_results
        return self._call("query", body)

    def compare(self, domain: str, record_type: str = "A", servers: str = "",
                expected_results: str = "") -> dict:
        """多 DNS 服务器对比查询"""
        body = {"domain": domain, "record_type": record_type}
        if servers:
            body["servers"] = servers
        if expected_results:
            body["expected_results"] = expected_results
        return self._call("compare", body)

    def dkim(self, domain: str, selector: str, server: str = "") -> dict:
        """DKIM 记录检查"""
        body = {"domain": domain, "selector": selector}
        if server:
            body["server"] = server
        return self._call("dkim", body)

    def trace(self, domain: str, record_type: str = "A") -> dict:
        """dig +trace 全链路解析"""
        return self._call("trace", {"domain": domain, "record_type": record_type})

    def spf(self, domain: str) -> dict:
        """SPF 记录检查"""
        return self._call("spf", {"domain": domain})

    def dmarc(self, domain: str) -> dict:
        """DMARC 记录检查"""
        return self._call("dmarc", {"domain": domain})

    def zone(self, domain: str, server: str = "") -> dict:
        """Zone Transfer (AXFR)"""
        body = {"domain": domain}
        if server:
            body["server"] = server
        return self._call("zone", body)

    def close(self):
        if self._client and not self._client.is_closed:
            self._client.close()
            self._client = None


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


# ═══ 单例 ═══
_dns_client: Optional[DnsApiClient] = None


def get_dns_client() -> DnsApiClient:
    """获取 DNS API 客户端单例 (Token 鉴权)"""
    global _dns_client
    if _dns_client is None:
        _dns_client = DnsApiClient()
    return _dns_client


# ═══════════════════════════════════════════════
#  批量解析辅助函数 (供 chat 工具 / NMS 告警分析共用)
# ═══════════════════════════════════════════════

async def resolve_targets(ips: List[str], domains: List[str],
                          max_targets: int = None) -> dict:
    """批量解析 IP 与域名 (并发, 带缓存)。

    - IP    → PTR 反向解析 (得到主机名)
    - 域名  → A 正向解析 (得到 IP)

    Args:
        ips: IP 列表 (IPv4/IPv6)
        domains: 域名列表
        max_targets: 总数上限 (None 用 config.NMS_DNS_RESOLVE_MAX)

    Returns:
        {"resolved": {target: [results]}, "errors": {target: err}, "skipped": [target]}
    """
    limit = max_targets if max_targets is not None else config.NMS_DNS_RESOLVE_MAX
    ips = list(dict.fromkeys(ips))[:limit]
    domains = list(dict.fromkeys(domains))[: max(0, limit - len(ips))]

    resolved: Dict[str, List[str]] = {}
    errors: Dict[str, str] = {}

    if not ips and not domains:
        return {"resolved": resolved, "errors": errors, "skipped": []}

    client = get_dns_client()

    async def _resolve_ip(ip: str):
        try:
            rt = "AAAA" if ":" in ip else "PTR"
            # 反向解析: PTR 需要 in-addr.arpa 名，API 端已支持直接传 IP
            data = await asyncio.to_thread(client.query, ip, rt)
            if data.get("success") is False:
                errors[ip] = str(data.get("error", "解析失败"))
                return
            results = [r.get("result", "") for r in data.get("successful_results", [])
                       if r.get("result")]
            if results:
                resolved[ip] = results
            elif not data.get("successful_results"):
                errors[ip] = "无 PTR 记录"
        except Exception as e:
            errors[ip] = str(e)

    async def _resolve_domain(dom: str):
        try:
            data = await asyncio.to_thread(client.query, dom, "A")
            if data.get("success") is False:
                errors[dom] = str(data.get("error", "解析失败"))
                return
            results = [r.get("result", "") for r in data.get("successful_results", [])
                       if r.get("result")]
            if results:
                resolved[dom] = results
            elif not data.get("successful_results"):
                errors[dom] = "无 A 记录"
        except Exception as e:
            errors[dom] = str(e)

    # 并发但限量 (避免告警风暴时打爆 API)
    sem = asyncio.Semaphore(5)

    async def _guarded(coro):
        async with sem:
            await coro

    tasks = [_guarded(_resolve_ip(ip)) for ip in ips]
    tasks += [_guarded(_resolve_domain(d)) for d in domains]
    await asyncio.gather(*tasks, return_exceptions=True)

    return {"resolved": resolved, "errors": errors, "skipped": []}


def format_resolve_report(result: dict, max_lines: int = 40) -> str:
    """把 resolve_targets 的结果格式化为 LLM 可读文本块"""
    resolved = result.get("resolved", {})
    errors = result.get("errors", {})
    lines = []
    for target, vals in resolved.items():
        lines.append(f"- {target} → {', '.join(vals[:4])}")
    if errors:
        lines.append("（以下目标解析失败/无记录，属正常现象）")
        for target, err in list(errors.items())[:10]:
            lines.append(f"- {target}: {err}")
    if not lines:
        return ""
    return "\n".join(lines[:max_lines])
