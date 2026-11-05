"""
Z-OPS — opscore_api: OPS-CORE 平台 API 共享模块
提供统一的 API 客户端、完整端点目录、智能查询函数
供 opscore_agent / vm_agent / ops_agent / network_agent 等共享使用

两个平台:
  - 信息管理系统 (Token 鉴权, Bearer): 145 / opscore-info.example.com
  - 管理平台 (Django REST Framework, 账号密码换 Token): 198.18.18.146

2026-10-27 起 145 已关闭 AD 账号本地密码兜底: 信息平台只走 Token,
代码里不再保留 /api/auth/login/ 调用; 401/403 直接失败, 不回退账号密码。
迁移说明: https://opscore-info.example.com/handoff/OPSCORE_AI_TOKEN_MIGRATION.md
"""

import json
import logging
import asyncio
import re
import time
from typing import Optional, Dict, Any, List, Callable, Tuple

import httpx

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")


# ═══════════════════════════════════════════════
#  API 端点目录 (从实际平台探测)
# ═══════════════════════════════════════════════

# ── 信息管理系统 (Token 鉴权) ──
# 注: auth_* 端点已移除 — 脚本不再登录, 401/403 一律上报管理员
INFO_API_CATALOG = {
    # 监控
    "monitor_info":     "/api/monitor_info/",
    "alarm_info":       "/api/alarm_info/",
    "site_info":        "/api/site_info/",
    "datacenter":       "/api/datacenter/",
    # 信息目录
    "opscore_access":      "/api/opscore_access/",
    "idc_info":         "/api/idc_info/",
    "vendor_info":      "/api/vendor_info/",
    "temp_password":    "/api/temp_password/",
    "local_password":   "/api/local_password/",
    # DNS
    "dns_query":        "/api/dns/query/",
    "dns_spf":          "/api/dns/spf/",
    "dns_dmarc":        "/api/dns/dmarc/",
    "dns_zone":         "/api/dns/zone/",
    # CMDB
    "cmdb_info":        "/api/cmdb_info/",
    "rack_info":        "/api/rack_info/",
    "snapshot_info":    "/api/snapshot_info/",
    "case_info":        "/api/case_info/",
}

# ── 管理平台 (198.18.18.146) 88个端点 ──
MGT_API_CATALOG = {
    # BRR 备份
    "brr_backups":          "/api/brr/backups/",
    "brr_backup_objects":   "/api/brr/backup-objects/",
    "brr_jobs":             "/api/brr/jobs/",
    "brr_gid_jobs":         "/api/brr/gid-jobs/",
    "brr_job_status_stats": "/api/brr/job-status-stats/",
    "brr_repositories":     "/api/brr/repositories/",
    "brr_repository_stats": "/api/brr/repository-stats/",
    "brr_restore_points":   "/api/brr/restore-points/",
    "brr_server_health":    "/api/brr/server-health/",
    "brr_servers":          "/api/brr/servers/",
    "brr_sessions":         "/api/brr/sessions/",
    "brr_session_stats":    "/api/brr/session-stats/",
    "brr_stats":            "/api/brr/stats/",
    # Smart-Ops 虚拟机
    "smartops_vms":             "/api/smart-ops/vms/",
    "smartops_vms_summary":     "/api/smart-ops/vms/summary/",
    # Smart-Ops 事件
    "smartops_events":              "/api/smart-ops/events/",
    "smartops_events_critical":     "/api/smart-ops/events/critical/",
    "smartops_events_unack":        "/api/smart-ops/events/unacknowledged/",
    # Smart-Ops 告警器
    "smartops_alerters":        "/api/smart-ops/alerters/",
    "smartops_alerters_active": "/api/smart-ops/alerters/active/",
    # Smart-Ops 状态
    "smartops_status":          "/api/smart-ops/status/",
    "smartops_status_latest":   "/api/smart-ops/status/latest/",
    "smartops_status_summary":  "/api/smart-ops/status/summary/",
    # Smart-Ops 日志
    "smartops_logs_alerts":         "/api/smart-ops/logs/alerts/",
    "smartops_logs_alerts_summary": "/api/smart-ops/logs/alerts/summary/",
    "smartops_logs_events":         "/api/smart-ops/logs/events/",
    "smartops_logs_tasks":          "/api/smart-ops/logs/tasks/",
    # Aria
    "aria_dashboard":       "/api/smart-ops/aria/aria-dashboard/",
    "aria_logs_alerts":     "/api/smart-ops/aria/aria-logs/alerts/",
    "aria_logs_events":     "/api/smart-ops/aria/aria-logs/events/",
    "aria_logs_servers":    "/api/smart-ops/aria/aria-logs/servers/",
    "aria_logs_tasks":      "/api/smart-ops/aria/aria-logs/tasks/",
    "aria_ops_capacity":    "/api/smart-ops/aria/aria-ops/capacity/",
    "aria_ops_inventory":   "/api/smart-ops/aria/aria-ops/inventory/",
    "aria_ops_servers":     "/api/smart-ops/aria/aria-ops/servers/",
    "aria_ops_statistics":  "/api/smart-ops/aria/aria-ops/statistics/",
    "aria_ops_vms":         "/api/smart-ops/aria/aria-ops/virtual-machines/",
    "aria_ha_events":       "/api/smart-ops/aria/ha-events/",
    "aria_vm_search":       "/api/smart-ops/aria/vm/search/",
    # 快照
    "snapshot_list":        "/api/snapshot/snapshots/",
    "snapshot_vms":         "/api/snapshot/vms/",
    "snapshot_check":       "/api/snapshot/check-problems/",
    "snapshot_check_vcd":   "/api/snapshot/check-vcd/",
    "snapshot_check_veeam": "/api/snapshot/check-veeam/",
    "snapshot_summary":     "/api/snapshot/data-summary/",
    # VCD
    "vcd_organizations":    "/api/vcd/organizations/",
    "vcd_resources":        "/api/vcd/resources/",
    # CRM 客户管理
    "crm_customers":        "/api/crm/customers/",
    "crm_customer_detail":  "/api/crm/customer-detail/",
    "crm_dashboard_stats":  "/api/crm/dashboard-stats/",
    "crm_storage_mappings": "/api/storage/volumes/with-crm-mappings/",
    # M365
    "m365_dashboard":       "/api/veeam-m365/dashboard/",
    "m365_backup_jobs":     "/api/veeam-m365/backup-jobs/",
    "m365_backup_sessions": "/api/veeam-m365/backup-sessions/",
    "m365_license":         "/api/veeam-m365/license-info/",
    "m365_organizations":   "/api/veeam-m365/organizations/",
}


# ═══ 按领域分组的端点映射 (供 smart_query 使用) ═══
DOMAIN_ENDPOINTS = {
    "ha": {
        "desc": "HA 高可用 (VM列表/搜索/站点状态)",
        "endpoints": [
            ("ha_vms", "HA VM列表（缓存）"),
            ("ha_vms_search", "HA VM 搜索"),
            ("ha_sites", "站点状态"),
        ]
    },
    "vm": {
        "desc": "虚拟机查询 (Smart-Ops VM / Aria Ops VM / 快照VM / VCD资源)",
        "endpoints": [
            ("smartops_vms", "Smart-Ops 虚拟机列表"),
            ("smartops_vms_summary", "Smart-Ops 虚拟机汇总"),
            ("aria_ops_vms", "Aria Ops 虚拟机"),
            ("aria_vm_search", "Aria VM 搜索"),
            ("snapshot_vms", "快照虚拟机列表"),
            ("vcd_resources", "VCD 资源"),
        ]
    },
    "backup": {
        "desc": "备份管理 (BRR备份作业/对象/仓库/会话/统计)",
        "endpoints": [
            ("brr_backups", "备份作业列表"),
            ("brr_backup_objects", "备份对象列表"),
            ("brr_jobs", "作业列表"),
            ("brr_repositories", "备份仓库"),
            ("brr_sessions", "备份会话"),
            ("brr_stats", "备份统计"),
            ("brr_server_health", "备份服务器健康"),
            ("brr_restore_points", "还原点"),
        ]
    },
    "snapshot": {
        "desc": "快照管理 (列表/汇总/问题检查)",
        "endpoints": [
            ("snapshot_list", "快照列表"),
            ("snapshot_summary", "快照汇总"),
            ("snapshot_check", "快照问题检查"),
            ("snapshot_check_vcd", "VCD快照检查"),
            ("snapshot_check_veeam", "Veeam快照检查"),
        ]
    },
    "monitoring": {
        "desc": "监控告警 (监控数据/告警/运维事件/日志告警)",
        "endpoints": [
            ("monitor_info", "监控数据"),
            ("alarm_info", "告警信息"),
            ("smartops_events", "运维事件"),
            ("smartops_events_critical", "严重事件"),
            ("smartops_events_unack", "未确认事件"),
            ("smartops_logs_alerts", "日志告警"),
            ("smartops_logs_alerts_summary", "告警汇总"),
            ("smartops_alerters_active", "活跃告警器"),
        ]
    },
    "infra": {
        "desc": "基础设施 (CMDB/机柜/IDC/站点/数据中心/厂商)",
        "endpoints": [
            ("cmdb_info", "CMDB 主机信息"),
            ("rack_info", "机柜信息"),
            ("idc_info", "IDC 机房"),
            ("site_info", "站点信息"),
            ("datacenter", "数据中心"),
            ("vendor_info", "厂商信息"),
            ("case_info", "案例/工单"),
        ]
    },
    "ha": {
        "desc": "HA 高可用 (VM列表/搜索/站点状态)",
        "endpoints": [
            ("ha_vms", "HA VM列表（缓存）"),
            ("ha_vms_search", "HA VM 搜索"),
            ("ha_sites", "站点状态"),
        ]
    },
    "network_dns": {
        "desc": "DNS 记录查询 (域名/SPF/DMARC/Zone)",
        "endpoints": [
            ("dns_query", "DNS 记录查询"),
            ("dns_spf", "SPF 记录"),
            ("dns_dmarc", "DMARC 记录"),
            ("dns_zone", "Zone 传送"),
        ]
    },
    "aria": {
        "desc": "Aria Ops/Logs (仪表盘/容量/统计/服务器/HA事件)",
        "endpoints": [
            ("aria_dashboard", "Aria 仪表盘"),
            ("aria_ops_capacity", "Aria 容量"),
            ("aria_ops_statistics", "Aria 统计"),
            ("aria_ops_servers", "Aria 服务器"),
            ("aria_ops_inventory", "Aria 清单"),
            ("aria_ha_events", "HA 事件"),
            ("aria_logs_alerts", "Aria 日志告警"),
            ("aria_logs_events", "Aria 日志事件"),
        ]
    },
    "m365": {
        "desc": "M365 备份 (作业/会话/许可证/仪表盘)",
        "endpoints": [
            ("m365_dashboard", "M365 仪表盘"),
            ("m365_backup_jobs", "M365 备份作业"),
            ("m365_backup_sessions", "M365 备份会话"),
            ("m365_license", "M365 许可证"),
        ]
    },
    "vcd": {
        "desc": "vCloud Director (组织/资源)",
        "endpoints": [
            ("vcd_organizations", "VCD 组织"),
            ("vcd_resources", "VCD 资源"),
        ]
    },
    "crm": {
        "desc": "CRM 客户管理 (客户列表/详情/统计/存储映射)",
        "endpoints": [
            ("crm_customers", "CRM 客户列表"),
            ("crm_customer_detail", "CRM 客户详情"),
            ("crm_dashboard_stats", "CRM 仪表盘统计"),
            ("crm_storage_mappings", "CRM 存储卷映射"),
        ]
    },
    "status": {
        "desc": "运维状态汇总 (Smart-Ops 状态/最新/汇总)",
        "endpoints": [
            ("smartops_status_summary", "运维状态汇总"),
            ("smartops_status_latest", "最新运维状态"),
            ("smartops_logs_tasks", "运维任务"),
        ]
    },
}


def get_domain_api_desc(domain: str) -> str:
    """获取某个领域的 API 描述"""
    if domain in DOMAIN_ENDPOINTS:
        d = DOMAIN_ENDPOINTS[domain]
        eps = ", ".join(f"{name}({desc})" for name, desc in d["endpoints"])
        return f"{d['desc']}: {eps}"
    return ""


# ═══════════════════════════════════════════════
#  API 客户端
# ═══════════════════════════════════════════════

class OpscoreApiClient:
    """OPS-CORE 平台 API 客户端

    两种鉴权:
      - 信息平台: 静态 Token (Authorization: Bearer), 无登录/重登路径
      - 管理平台: 账号密码换 DRF Token (header 前缀 Token)
    """

    def __init__(self, base_url: str, username: str = "", password: str = "",
                 catalog: dict = None, token: str = "", token_scheme: str = "Bearer"):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.catalog = catalog or {}
        self._token = (token or "").strip()
        self._token_scheme = token_scheme
        self._session_ok = bool(self._token)  # 静态 Token 视为已认证
        self._session_cookie = ""
        self._login_fails = 0            # 连续登录失败次数 (指数退避)
        self._last_login_fail_ts = 0.0   # 上次登录失败时间戳
        self._auth_warned = False        # 401/403 只告警一次, 避免刷日志
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30, verify=False, follow_redirects=True
            )
        return self._client

    def _login_cooldown(self) -> float:
        """登录失败冷却: 60s 起指数退避, 封顶 30 分钟。

        同一个 AD 账号被多个服务共用, 密码失效/账号被锁时若每个 API 请求都
        重登一次, 会把失败次数放大到锁号。
        """
        if self._login_fails <= 0:
            return 0.0
        return min(60.0 * (2 ** (self._login_fails - 1)), 1800.0)

    async def login(self) -> bool:
        """尝试登录获取 Token (仅管理平台: 信息平台已配置静态 Token, 直接返回 True)"""
        if self._token:
            return True
        if self._token_scheme == "Bearer":
            # 信息平台只认 Token: 145 已关闭密码兜底, 发登录请求只会白送一次 AD 绑定
            return False
        if not self.username or not self.password:
            return False
        if self._last_login_fail_ts and \
                time.time() - self._last_login_fail_ts < self._login_cooldown():
            return False  # 失败冷却期内不再试密
        try:
            client = await self._get_client()
            resp = await client.post(
                f"{self.base_url}/api/auth/login/",
                json={"username": self.username, "password": self.password},
                headers={"Content-Type": "application/json"}
            )
            if resp.status_code == 200:
                data = resp.json()
                self._token = data.get("token", data.get("key", ""))
                # 145 信息平台无 token 字段, 认证靠 sessionid cookie (client 自动携带)
                if resp.cookies:
                    self._session_cookie = str(resp.cookies)
                self._session_ok = True
                self._login_fails = 0
                logger.info(f"[opscore_api] 登录成功: {self.base_url} "
                            f"({'token' if self._token else 'session-cookie'})")
                return True
            else:
                self._login_fails += 1
                self._last_login_fail_ts = time.time()
                logger.warning(f"[opscore_api] 登录失败 {self.base_url}: {resp.status_code} "
                               f"(连续 {self._login_fails} 次, 退避 {int(self._login_cooldown())}s)")
                return False
        except Exception as e:
            self._login_fails += 1
            self._last_login_fail_ts = time.time()
            logger.warning(f"[opscore_api] 登录异常 {self.base_url}: {e}")
            return False

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"{self._token_scheme} {self._token}"
        return h

    async def get(self, endpoint_key: str, params: dict = None) -> dict:
        """通过端点键名发起 GET 请求"""
        path = self.catalog.get(endpoint_key)
        if not path:
            return {"error": f"未知端点: {endpoint_key}"}
        return await self.request("GET", path, params=params)

    async def request(self, method: str, path: str,
                      params: dict = None, data: dict = None) -> dict:
        """发起 API 请求"""
        url = f"{self.base_url}{path}"
        try:
            # 先尝试登录（如果还没认证且有凭据; token 或 session cookie 均算已认证）
            if not self._token and not self._session_ok and self.username:
                await self.login()

            client = await self._get_client()
            if method.upper() == "GET":
                resp = await client.get(url, headers=self._headers(), params=params)
            elif method.upper() == "POST":
                resp = await client.post(url, headers=self._headers(), json=data, params=params)
            else:
                resp = await client.get(url, headers=self._headers(), params=params)

            if resp.status_code in (401, 403) and self._token_scheme == "Bearer":
                # 静态 Token 鉴权失败: 告警并直接失败, 禁止回退账号密码登录
                if not self._auth_warned:
                    reason = "Token 无效/被吊销" if resp.status_code == 401 else \
                        "Token 缺 scope 或来源 IP 不在白名单(198.18.18.147)"
                    logger.error(f"[opscore_api] OPS-CORE Token 鉴权失败 {resp.status_code}: {url} — {reason}")
                    self._auth_warned = True
                return {"error": f"HTTP {resp.status_code}: OPS-CORE Token 鉴权失败", "url": url}
            if resp.status_code == 401:
                # 管理平台 DRF Token 过期，重新登录
                if await self.login():
                    if method.upper() == "GET":
                        resp = await client.get(url, headers=self._headers(), params=params)
                    else:
                        resp = await client.post(url, headers=self._headers(), json=data, params=params)

            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text[:2000]}
            else:
                return {"error": f"HTTP {resp.status_code}", "url": url}

        except httpx.TimeoutException:
            return {"error": f"请求超时: {url}"}
        except Exception as e:
            return {"error": f"请求失败: {str(e)}"}

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


# ═══ 单例客户端 ═══

_info_client: Optional[OpscoreApiClient] = None
_mgt_client: Optional[OpscoreApiClient] = None


def get_info_client() -> OpscoreApiClient:
    """获取信息管理系统客户端 (Token 鉴权; 145 已禁用密码兜底, 无登录路径)"""
    global _info_client
    if _info_client is None:
        _info_client = OpscoreApiClient(
            base_url=config.OPSCORE_INFO_URL,
            catalog=INFO_API_CATALOG,
            token=config.OPSCORE_API_TOKEN or "",
            token_scheme="Bearer",
        )
        if not config.OPSCORE_API_TOKEN:
            logger.warning("[opscore_api] 未配置 OPSCORE_API_TOKEN, 信息平台接口将全部 401 "
                           "(禁止回退账号密码登录)")
    return _info_client


def get_mgt_client() -> OpscoreApiClient:
    """获取管理平台 (146) 客户端"""
    global _mgt_client
    if _mgt_client is None:
        _mgt_client = OpscoreApiClient(
            base_url=config.OPSCORE_MGT_URL,
            username=config.OPSCORE_MGT_USER,
            password=config.OPSCORE_MGT_PASS,
            catalog=MGT_API_CATALOG,
            token_scheme="Token",
        )
    return _mgt_client


def _get_client_for_endpoint(endpoint_key: str) -> OpscoreApiClient:
    """根据端点名自动选择对应平台客户端"""
    if endpoint_key in INFO_API_CATALOG:
        return get_info_client()
    elif endpoint_key in MGT_API_CATALOG:
        return get_mgt_client()
    else:
        raise ValueError(f"未知端点: {endpoint_key}")


# ═══════════════════════════════════════════════
#  便捷查询函数
# ═══════════════════════════════════════════════

async def query_api(endpoint_key: str, params: dict = None) -> dict:
    """
    统一查询接口: 按端点键名查询，自动路由到正确的平台
    用法: data = await query_api("cmdb_info", {"search": "web-server"})
    """
    try:
        client = _get_client_for_endpoint(endpoint_key)
        return await client.get(endpoint_key, params)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"查询异常: {str(e)}"}


async def smart_query(question: str, domains: List[str],
                      llm_func: Callable = None, max_queries: int = 3) -> str:
    """
    智能查询: 根据用户问题自动选择相关 API 端点查询数据
    
    1. 用 LLM 分析问题 → 确定最相关的端点
    2. 并发查询多个端点
    3. 汇总返回原始数据文本
    
    Args:
        question: 用户问题
        domains: 要搜索的领域列表 (如 ["vm", "backup", "monitoring"])
        llm_func: LLM 调用函数 (async, 用于智能端点选择)
        max_queries: 最大并发查询数
    
    Returns:
        查询到的数据文本 (JSON)，空字符串表示无数据
    """
    # 收集所有候选端点
    candidate_endpoints = []
    for domain in domains:
        if domain in DOMAIN_ENDPOINTS:
            for ep_key, ep_desc in DOMAIN_ENDPOINTS[domain]["endpoints"]:
                candidate_endpoints.append((ep_key, ep_desc))

    if not candidate_endpoints:
        return ""

    # 如果候选端点少于等于 max_queries，直接全查
    if len(candidate_endpoints) <= max_queries:
        selected = [ep[0] for ep in candidate_endpoints]
    elif llm_func:
        # 用 LLM 选择最相关的端点
        ep_list = "\n".join(f"- {key}: {desc}" for key, desc in candidate_endpoints)
        try:
            selection = await llm_func(
                prompt=(
                    f"用户问题: {question}\n\n"
                    f"可用 API 端点:\n{ep_list}\n\n"
                    f"请选择最多 {max_queries} 个最相关的端点来回答用户问题。\n"
                    f"只返回端点 key 列表，用逗号分隔，不要其他内容。"
                ),
                system="你是 API 路由选择器。根据用户问题选择最相关的 API 端点。只返回 key 列表。",
                temperature=0.1
            )
            # 解析选择结果
            selected = []
            for key in selection.replace("\n", ",").split(","):
                key = key.strip().strip("`").strip('"').strip("'").strip("-").strip()
                if key in INFO_API_CATALOG or key in MGT_API_CATALOG:
                    selected.append(key)
            if not selected:
                # 降级: 取前 max_queries 个
                selected = [ep[0] for ep in candidate_endpoints[:max_queries]]
        except Exception:
            selected = [ep[0] for ep in candidate_endpoints[:max_queries]]
    else:
        selected = [ep[0] for ep in candidate_endpoints[:max_queries]]

    # 并发查询
    async def _fetch(key: str) -> tuple:
        data = await query_api(key, {"page_size": 20})
        return (key, data)

    tasks = [_fetch(key) for key in selected[:max_queries]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 汇总结果
    output_parts = []
    for item in results:
        if isinstance(item, Exception):
            continue
        key, data = item
        if isinstance(data, dict) and "error" not in data:
            # 截取有效数据
            text = json.dumps(data, ensure_ascii=False, indent=1)
            if len(text) > 4000:
                text = text[:4000] + "\n... (截断)"
            output_parts.append(f"=== {key} ===\n{text}")

    return "\n\n".join(output_parts) if output_parts else ""


# ═══════════════════════════════════════════════
#  CRM 客户管理专用查询函数
# ═══════════════════════════════════════════════

# ═══ 工具函数 ═══

def normalize_gid(gid: str) -> str:
    """
    标准化 GID 格式
    
    Args:
        gid: 用户输入的 GID (可能是 "19934", "TICKET-19934", "gid19934")
    
    Returns:
        标准化格式："TICKET-19934"
    """
    if not gid:
        return ""
    gid = gid.upper().strip()
    # 移除所有非字母数字字符
    gid = ''.join(c for c in gid if c.isalnum())
    # 提取数字部分
    import re
    numbers = re.findall(r'\d+', gid)
    if not numbers:
        return gid  # 没有数字，返回原值
    
    # 提取字母前缀
    letters = re.match(r'([A-Z]*)\d+', gid)
    prefix = letters.group(1) if letters else ""
    
    # 确保前缀是 GID 开头
    if not prefix.startswith('GID'):
        prefix = 'GID'
    
    # 取最后 5 位数字
    last_5_digits = numbers[-1][-5:]
    
    return f"{prefix}{last_5_digits}"


def bytes_to_gb(size_bytes: int) -> float:
    """
    字节转 GB (保留 2 位小数)
    
    Args:
        size_bytes: 字节数
    
    Returns:
        GB 值 (保留 2 位小数)
    """
    if not isinstance(size_bytes, (int, float)):
        return 0.0
    return round(size_bytes / (1024 ** 3), 2)


# ═══ 高级查询函数 ═══

async def query_all_pages(endpoint_key: str, params: dict = None, 
                          max_pages: int = 100) -> list:
    """
    分页获取全部数据
    
    Args:
        endpoint_key: 端点键名
        params: 查询参数
        max_pages: 最大页数 (防止无限循环)
    
    Returns:
        所有结果列表
    """
    all_results = []
    page = 1
    page_size = (params or {}).get("page_size", 100)
    
    while page <= max_pages:
        p = (params or {}).copy()
        p["page"] = page
        p["page_size"] = page_size
        
        data = await query_api(endpoint_key, p)
        
        if "error" in data:
            logger.warning(f"[query_all_pages] 第{page}页查询失败：{data['error']}")
            break
        
        results = data.get("results", [])
        all_results.extend(results)
        
        # 如果返回结果少于 page_size，说明已经是最后一页
        if len(results) < page_size:
            break
        
        page += 1
    
    logger.info(f"[query_all_pages] {endpoint_key}: 获取{len(all_results)}条数据")
    return all_results


async def query_by_server_url(endpoint_key: str, server_urls: list,
                               params: dict = None) -> dict:
    """
    按 server_url 分组查询，返回每台服务器的数据
    
    Args:
        endpoint_key: 端点键名
        server_urls: 服务器 URL 列表
        params: 查询参数
    
    Returns:
        字典：{server_url: 数据}
    """
    results = {}
    
    async def _fetch(server_url: str) -> tuple:
        p = (params or {}).copy()
        p["server_url"] = server_url
        data = await query_api(endpoint_key, p)
        return (server_url, data)
    
    tasks = [_fetch(url) for url in server_urls]
    fetched = await asyncio.gather(*tasks, return_exceptions=True)
    
    for item in fetched:
        if isinstance(item, Exception):
            continue
        server_url, data = item
        results[server_url] = data
    
    logger.info(f"[query_by_server_url] {endpoint_key}: 查询{len(results)}台服务器")
    return results


async def robust_query(endpoint_key: str, params: dict = None,
                       max_retries: int = 3, timeout: int = 30) -> dict:
    """
    带重试的健壮查询
    
    Args:
        endpoint_key: 端点键名
        params: 查询参数
        max_retries: 最大重试次数
        timeout: 超时时间 (秒)
    
    Returns:
        查询结果
    """
    for attempt in range(max_retries):
        try:
            # 添加超时参数
            p = (params or {}).copy()
            if "timeout" not in p:
                p["timeout"] = timeout
            
            data = await query_api(endpoint_key, p)
            
            # 检查 HTTP 错误
            if isinstance(data, dict) and data.get("error", "").startswith("HTTP"):
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # 指数退避
                    logger.warning(f"[robust_query] HTTP 错误，{wait_time}秒后重试 ({attempt+1}/{max_retries})")
                    await asyncio.sleep(wait_time)
                    continue
            
            return data
            
        except Exception as e:
            if attempt == max_retries - 1:
                error_msg = f"查询失败 (已重试{max_retries}次): {str(e)}"
                logger.error(f"[robust_query] {error_msg}")
                return {"error": error_msg}
            
            wait_time = 2 ** attempt
            logger.warning(f"[robust_query] 查询异常，{wait_time}秒后重试 ({attempt+1}/{max_retries}): {str(e)}")
            await asyncio.sleep(wait_time)
    
    return {"error": "查询超时"}


async def query_with_concurrency_limit(endpoint_key: str, 
                                        items: list,
                                        params: dict = None,
                                        max_concurrent: int = 5) -> list:
    """
    限制并发数的批量查询
    
    Args:
        endpoint_key: 端点键名
        items: 查询项列表
        params: 查询参数
        max_concurrent: 最大并发数
    
    Returns:
        查询结果列表
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def _fetch_with_limit(item: str) -> dict:
        async with semaphore:
            p = (params or {}).copy()
            p["search"] = item
            return await query_api(endpoint_key, p)
    
    tasks = [_fetch_with_limit(item) for item in items]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 过滤异常
    valid_results = []
    for r in results:
        if isinstance(r, Exception):
            continue
        valid_results.append(r)
    
    logger.info(f"[query_with_concurrency_limit] {endpoint_key}: 并发查询{len(valid_results)}项")
    return valid_results


# ═══ 缓存机制 ═══

from functools import lru_cache
import hashlib

# 简单内存缓存
_query_cache = {}
_cache_ttl = 300  # 5 分钟


def _make_cache_key(endpoint_key: str, params: dict = None) -> str:
    """生成缓存键"""
    key_str = f"{endpoint_key}:{json.dumps(params or {}, sort_keys=True)}"
    return hashlib.md5(key_str.encode()).hexdigest()


async def cached_query_api(endpoint_key: str, params: dict = None,
                           ttl: int = None, use_cache: bool = True) -> dict:
    """
    带缓存的 API 查询
    
    Args:
        endpoint_key: 端点键名
        params: 查询参数
        ttl: 缓存时间 (秒)，默认使用全局设置
        use_cache: 是否使用缓存
    
    Returns:
        查询结果
    """
    if not use_cache:
        return await query_api(endpoint_key, params)
    
    cache_key = _make_cache_key(endpoint_key, params)
    current_time = time.time()
    
    # 检查缓存
    if cache_key in _query_cache:
        data, timestamp, cache_ttl = _query_cache[cache_key]
        if current_time - timestamp < cache_ttl:
            logger.debug(f"[cached_query_api] 缓存命中：{endpoint_key}")
            return data
        else:
            # 缓存过期，删除
            del _query_cache[cache_key]
    
    # 查询并缓存 (error 结果不缓存: 平台恢复后立即可用, 无需等 TTL 过期)
    data = await query_api(endpoint_key, params)
    if isinstance(data, dict) and "error" in data:
        return data
    cache_ttl = ttl if ttl is not None else _cache_ttl
    _query_cache[cache_key] = (data, current_time, cache_ttl)
    
    logger.debug(f"[cached_query_api] 缓存并返回：{endpoint_key}")
    return data


def clear_cache(pattern: str = None):
    """
    清除缓存
    
    Args:
        pattern: 匹配模式 (支持通配符)，为 None 时清空所有缓存
    """
    global _query_cache
    
    if pattern is None:
        _query_cache.clear()
        logger.info("[clear_cache] 已清空所有缓存")
        return
    
    import fnmatch
    keys_to_delete = [k for k in _query_cache.keys() if fnmatch.fnmatch(k, pattern)]
    for key in keys_to_delete:
        del _query_cache[key]
    
    logger.info(f"[clear_cache] 清除{len(keys_to_delete)}条缓存")


async def query_crm_customers(search: str = None, page_size: int = 50) -> dict:
    """
    查询 CRM 客户列表
    
    Args:
        search: 搜索关键词 (GID/公司名称/订单号)
        page_size: 每页数量
    
    Returns:
        客户列表数据，包含 count/results 等字段
    """
    params = {"page_size": page_size}
    if search:
        params["search"] = search
    return await query_api("crm_customers", params)


async def query_crm_customer_by_gid(gid: str, use_cache: bool = True) -> dict:
    """
    根据 GID 查询客户详情 (带缓存)
    
    Args:
        gid: 客户 GID (如 "19503", "47141", "TW48915")
        use_cache: 是否使用缓存
    
    Returns:
        客户详情，包含 resource_pools, storage_mapping_count, vcd_vm_count 等
    """
    # 提取纯数字/原始 GID (CRM 存储格式无 GID 前缀，如 "47141", "TW43727")
    import re as _re
    raw_gid = gid.strip()
    # 去掉 "GID"/"gid" 前缀，保留原始值 (如 "TICKET-47141" → "47141")
    raw_gid_no_prefix = _re.sub(r'^(?:GID|gid)', '', raw_gid, flags=_re.IGNORECASE).strip()
    
    # 策略1: 用原始 GID (无前缀) 搜索 — CRM 通常存储为 "47141", "TW43727"
    search_terms = [raw_gid_no_prefix, raw_gid]
    # 去重
    search_terms = list(dict.fromkeys(search_terms))
    
    for search_term in search_terms:
        customer_resp = await cached_query_api(
            "crm_customers", 
            {"search": search_term, "page_size": 100},
            use_cache=use_cache
        )
        if "error" in customer_resp:
            continue
        
        results = customer_resp.get("results", [])
        # 精确匹配 GID
        for customer in results:
            if customer.get("gid") == search_term or customer.get("gid") == raw_gid_no_prefix:
                return customer
        # 部分匹配
        for customer in results:
            if raw_gid_no_prefix in str(customer.get("gid", "")):
                return customer
    
    # 还是没找到，返回错误
    return {"error": f"GID {raw_gid} 未找到匹配客户", "search_results": []}


async def query_crm_customer_resources(gid: str) -> dict:
    """
    查询客户的资源信息 (Resource Pools, Storage Mappings, VMs)
    支持多GID客户：从storage_mappings中提取所有关联的GID
    
    Args:
        gid: 客户 GID
    
    Returns:
        客户资源汇总信息，包含所有关联GID
    """
    # 标准化 GID 格式
    normalized_gid = normalize_gid(gid)
    
    customer = await query_crm_customer_by_gid(normalized_gid, use_cache=True)
    if "error" in customer:
        return customer
    
    # 确保是单个客户对象，不是列表
    if "results" in customer:
        return {"error": f"GID {normalized_gid} 返回多个匹配结果，无法确定唯一客户"}
        
    # 获取客户 ID 查询存储映射
    customer_id = customer.get("id")
    storage_mappings = []
    related_gids = set([normalized_gid])  # 包含主 GID
    
    if customer_id:
        # 查询该客户的存储卷映射
        client = get_mgt_client()
        mappings_resp = await client.request(
            "GET", 
            f"/api/crm/customers/{customer_id}/storage_mappings/"
        )
        if "error" not in mappings_resp:
            storage_mappings = mappings_resp if isinstance(mappings_resp, list) else mappings_resp.get("results", [])
            # 从 resource_pool 字段提取所有关联的GID
            import re
            for mapping in storage_mappings:
                pool_name = mapping.get("resource_pool", "")
                # 匹配 GID 格式：GID + 数字 或 GID + 2字母 + 数字
                matches = re.findall(r'GID[A-Z]{0,2}\d{5}', pool_name.upper())
                related_gids.update(matches)
    
    return {
        "customer": {
            "id": customer.get("id"),
            "gid": customer.get("gid"),
            "company_name": customer.get("company_name"),
            "service_id": customer.get("service_id"),
            "order_no": customer.get("order_no"),
            "current_service": customer.get("current_service"),
        },
        "resources": {
            "resource_pools_count": customer.get("resource_pools_count", 0),
            "storage_mapping_count": customer.get("storage_mapping_count", 0),
            "vcd_vm_count": customer.get("vcd_vm_count", 0),
            "snapshot_vm_count": customer.get("snapshot_vm_count", 0),
            "brr_job_count": customer.get("brr_job_count", 0),
            "brr_vm_count": customer.get("brr_vm_count", 0),
        },
        "resource_pools": customer.get("resource_pools", []),
        "storage_mappings": storage_mappings,
        "related_gids": list(related_gids),  # 所有关联的GID
        "primary_gid": normalized_gid,
    }


async def query_crm_dashboard() -> dict:
    """
    查询 CRM 仪表盘统计
    
    Returns:
        仪表盘统计数据，包含 total_customers, recent_imports 等
    """
    return await query_api("crm_dashboard_stats", {})


async def query_crm_storage_mappings() -> dict:
    """
    查询存储卷与 CRM 客户的映射关系
    
    Returns:
        存储卷映射列表
    """
    return await query_api("crm_storage_mappings", {"page_size": 100})


# ═══════════════════════════════════════════════
#  MCP REST 网关直连 (smartops_vms 全量缓存 / esxi_host 索引)
#
#  背景: REST /api/smart-ops/vms/ 无过滤大页查询在 Z-OPS→146 链路上单页 20s+、
#  page_size>=500 直接 30s 超时 (VMStatus 聚合序列化开销)，是 opscore_agent
#  主机 VM 查询平均 77s 的根因。MCP 网关 (opscore-mgt /api/snapshot/mcp/tools/)
#  返回精简字段、无 VMStatus 聚合，全量 3 页并发热态 <2s。
#  主机查询链路: MCP 全量缓存建 esxi_host 索引 → 短名解析完整主机 →
#  REST host=<完整值> 服务端精确过滤 (索引命中 vm_host_idx, 0.2s)。
# ═══════════════════════════════════════════════

_mcp_gw_client: Optional[httpx.AsyncClient] = None
_mcp_bg_tasks: set = set()


async def _get_mcp_gateway_client() -> httpx.AsyncClient:
    """MCP REST 网关共享 httpx 客户端 (连接复用, 避免每次 TLS 握手)"""
    global _mcp_gw_client
    if _mcp_gw_client is None or _mcp_gw_client.is_closed:
        _mcp_gw_client = httpx.AsyncClient(
            timeout=config.MCP_TIMEOUT, verify=config.MCP_VERIFY_SSL,
            follow_redirects=True,
        )
    return _mcp_gw_client


async def mcp_call_tool(tool: str, payload: dict = None) -> dict:
    """调用 OPS-CORE MCP REST 网关工具: POST {MCP_REST_BASE}/{tool}/

    Returns:
        工具 data 字段 (dict)；失败时 {"error": ...}
    """
    headers = {"X-API-Key": config.MCP_API_KEY} if config.MCP_API_KEY else {}
    try:
        client = await _get_mcp_gateway_client()
        resp = await client.post(
            f"{config.MCP_REST_BASE}/{tool}/",
            json=payload or {}, headers=headers,
        )
        if resp.status_code != 200:
            return {"error": f"MCP HTTP {resp.status_code}: {resp.text[:120]}"}
        body = resp.json()
        if body.get("status") != "success":
            return {"error": f"MCP {body.get('status')}: {str(body)[:120]}"}
        return body.get("data") or {}
    except Exception as e:
        return {"error": f"MCP 调用失败: {e}"}


# ── smartops_vms 全量缓存 (stale-while-revalidate) ──

_MCP_VMS_PAGE_SIZE = 1000
_MCP_VMS_TTL = 300        # 新鲜期 (秒): 直接用缓存
_MCP_VMS_STALE = 900      # 宽限期 (秒): 返回旧值 + 后台刷新

_mcp_vms_cache: Dict[str, Any] = {"vms": [], "ts": 0.0, "esxi_hosts": []}
_mcp_vms_lock = asyncio.Lock()


async def _fetch_mcp_vms_all() -> list:
    """MCP 全量分页拉取: 第 1 页拿 total 算页数, 其余页并发"""
    d1 = await mcp_call_tool("smartops_vms",
                             {"page": 1, "page_size": _MCP_VMS_PAGE_SIZE})
    if "error" in d1:
        raise RuntimeError(d1["error"])
    total = int(d1.get("total") or 0)
    vms = list(d1.get("vms") or [])
    pages = max(1, -(-total // _MCP_VMS_PAGE_SIZE))
    if pages > 1:
        results = await asyncio.gather(*[
            mcp_call_tool("smartops_vms",
                          {"page": p, "page_size": _MCP_VMS_PAGE_SIZE})
            for p in range(2, pages + 1)
        ], return_exceptions=True)
        for r in results:
            if isinstance(r, dict) and "error" not in r:
                vms.extend(r.get("vms") or [])
    return vms


async def _refresh_mcp_vms_cache(force: bool = False) -> None:
    """刷新全量缓存 (锁内 double-check, 避免过期瞬间重复全量拉取)"""
    async with _mcp_vms_lock:
        if not force and _mcp_vms_cache["vms"] \
                and (time.time() - _mcp_vms_cache["ts"]) < _MCP_VMS_TTL:
            return
        vms = await _fetch_mcp_vms_all()
        esxi_hosts = sorted({
            str(v["esxi_host"]) for v in vms
            if v.get("esxi_host") and str(v["esxi_host"]).lower() != "none"
        })
        _mcp_vms_cache.update(vms=vms, esxi_hosts=esxi_hosts, ts=time.time())
        logger.info(f"[mcp_vms] 全量缓存刷新: {len(vms)} 台 VM, "
                    f"{len(esxi_hosts)} 台 ESXi 宿主机")


async def mcp_smartops_vms_all() -> list:
    """smartops_vms 全量列表 (stale-while-revalidate 内存缓存)

    - 年龄 < TTL: 直接返回缓存 (0 网络开销)
    - TTL ~ TTL+STALE: 返回旧值并后台刷新 (不阻塞查询)
    - 超过宽限期: 同步刷新; 刷新失败时退回旧值
    """
    if _mcp_vms_cache["vms"]:
        age = time.time() - _mcp_vms_cache["ts"]
        if age < _MCP_VMS_TTL:
            return _mcp_vms_cache["vms"]
        if age < _MCP_VMS_TTL + _MCP_VMS_STALE:
            task = asyncio.create_task(_refresh_mcp_vms_cache())
            _mcp_bg_tasks.add(task)
            task.add_done_callback(_mcp_bg_tasks.discard)
            return _mcp_vms_cache["vms"]
        try:
            await _refresh_mcp_vms_cache(force=True)
        except Exception as e:
            logger.warning(f"[mcp_vms] 同步刷新失败, 使用旧缓存: {e}")
        return _mcp_vms_cache["vms"]
    await _refresh_mcp_vms_cache(force=True)
    return _mcp_vms_cache["vms"]


def resolve_esxi_host_candidates(host_code: str) -> List[str]:
    """短名 (如 ESXI-02) → 完整 esxi_host 候选 (本地子串匹配缓存索引)

    覆盖 FQDN (esxi-02.example.internal) 与 IP 两种形态。
    """
    code = (host_code or "").strip().lower()
    if not code:
        return []
    return [h for h in _mcp_vms_cache["esxi_hosts"] if code in h.lower()]


# ── 主机 VM 查询 (opscore_agent / vm_agent 共用核心) ──

_HOST_CODE_RE = re.compile(r'^([A-Za-z]{2,8})(\d{3,5})([A-Za-z]{0,4})$')
_HOST_SKIP_PREFIXES = {'GID', 'HTTP', 'DNS', 'API', 'URL', 'SSH', 'TCP', 'UDP'}
_HOST_SNAPSHOT_COVERAGE_MIN = 0.6   # snapshot 站点搜索命中率阈值, 达标即早停


def expand_host_code(search: str) -> Optional[str]:
    """站点设备代码归一化: 'ESXI-02' → 'ESXI-02'; 非主机代码返回 None"""
    m = _HOST_CODE_RE.match((search or "").strip())
    if not m:
        return None
    prefix, num, suffix = m.groups()
    if prefix.upper() in _HOST_SKIP_PREFIXES:
        return None
    return f"{prefix}{num}{suffix}".lower()


async def query_vms_on_esxi_host(search: str) -> Optional[dict]:
    """主机 VM 查询核心: 将 'ESXI-02' 视为 ESXi 主机名, 返回该主机上全部 VM

    链路 (opscore_agent._expand_site_search / vm_agent._expand_site_vm_search 共用):
      1. MCP 全量缓存建 esxi_host 索引, 短名子串解析出完整宿主机 (FQDN/IP)
      2. REST smartops_vms?host=<完整值> 服务端精确过滤拿丰富字段明细 (索引命中, ~0.2s)
         REST 不可用时退回 MCP 缓存内同宿主机 VM (精简字段)
      3. snapshot_vms 站点前缀合并 (site/resource_pool/guest_ip/GID);
         命中率达标即早停, 未达标仅对缺失 VM 受限并发精确补查, 不做全量兜底

    Returns:
        None (非主机代码或未命中宿主机) 或
        {host_code, esxi_hosts, site_hints, vms, snapshot_matched, source, elapsed}
    """
    t0 = time.time()
    host_code = expand_host_code(search)
    if not host_code:
        return None

    # 1) esxi_host 索引解析 (需要时触发全量缓存加载)
    try:
        all_vms = await mcp_smartops_vms_all()
    except Exception as e:
        logger.warning(f"[host_vm] MCP 全量缓存不可用: {e}")
        all_vms = []
    candidates = resolve_esxi_host_candidates(host_code)
    if not candidates:
        return None
    logger.info(f"[host_vm] '{search}' → 宿主机候选: {candidates}")

    # 2) REST 服务端精确过滤 (host= 命中 vm_host_idx; 页级缓存, 同问不重复拉取)
    cand_lower = {c.lower() for c in candidates}
    mcp_filtered = [v for v in all_vms
                    if str(v.get("esxi_host", "")).lower() in cand_lower]
    detail_results = await asyncio.gather(*[
        cached_query_api("smartops_vms", {"host": c, "page_size": 500}, ttl=300)
        for c in candidates
    ], return_exceptions=True)
    filtered, source = [], "smartops_vms(host=服务端过滤)"
    for d in detail_results:
        if isinstance(d, dict) and "error" not in d:
            filtered.extend(d.get("results", []))
    if not filtered:
        # REST 全部失败 → MCP 缓存兜底 (精简字段)
        filtered = [dict(v) for v in mcp_filtered]
        source = f"smartops_vms(MCP全量缓存, {len(mcp_filtered)}台)"
    if not filtered:
        return None

    # 3) snapshot_vms 合并 — 站点前缀从完整 esxi_host 提取 (FQDN 去域取前 8 位)
    snap_lookup: Dict[str, dict] = {}
    site_hints: List[str] = []
    for c in candidates:
        base = str(c).split(".")[0]
        hint = base[:8].upper()
        if hint and hint[0].isalpha() and hint not in site_hints:
            site_hints.append(hint)
    for hint in site_hints:
        try:
            sd = await cached_query_api(
                "snapshot_vms", {"page_size": 1000, "search": hint}, ttl=300)
            if isinstance(sd, dict) and "error" not in sd:
                for vm in sd.get("results", []):
                    snap_lookup.setdefault(vm.get("name", ""), vm)
        except Exception as e:
            logger.warning(f"[host_vm] snapshot 站点搜索失败 ({hint}): {e}")

    # 早停: 命中率达标跳过补查; 未达标仅对缺失 VM 受限并发精确补查 (替代全量兜底)
    names = [v.get("name", "") for v in filtered]
    missing = [n for n in names if n and n not in snap_lookup]
    coverage = 1 - len(missing) / len(names) if names else 1.0
    if missing and coverage < _HOST_SNAPSHOT_COVERAGE_MIN:
        sem = asyncio.Semaphore(8)

        async def _lookup_one(n: str) -> Optional[dict]:
            async with sem:
                d = await cached_query_api(
                    "snapshot_vms", {"page_size": 5, "search": n}, ttl=300)
                if isinstance(d, dict) and "error" not in d:
                    for vm in d.get("results", []):
                        if vm.get("name") == n:
                            return vm
            return None

        found = await asyncio.gather(
            *[_lookup_one(n) for n in missing], return_exceptions=True)
        for n, f in zip(missing, found):
            if isinstance(f, dict):
                snap_lookup[n] = f

    # 合并 snapshot 字段 (不覆盖 REST 原有值) + 提取客户 GID
    _gid_re = re.compile(r'GID(?:TW)?(\d{4,6})', re.IGNORECASE)
    _snap_fields = ("site", "site_display", "resource_pool", "biz_type",
                    "guest_ip", "guest_os")
    for vm in filtered:
        snap = snap_lookup.get(vm.get("name", ""))
        if not snap:
            continue
        for k in _snap_fields:
            if snap.get(k) and not vm.get(k):
                vm[k] = snap[k]
        gid_m = _gid_re.search(snap.get("resource_pool", "") or "")
        if gid_m:
            vm.setdefault("customer_gid", gid_m.group(1))

    elapsed = time.time() - t0
    matched = sum(1 for n in names if n in snap_lookup)
    logger.info(f"[host_vm] '{search}': {len(filtered)} 台 VM @ {candidates} "
                f"({elapsed:.1f}s, snapshot 命中 {matched}/{len(names)}, {source})")
    return {
        "host_code": host_code,
        "esxi_hosts": candidates,
        "site_hints": site_hints,
        "vms": filtered,
        "snapshot_matched": matched,
        "source": source,
        "elapsed": round(elapsed, 2),
    }
