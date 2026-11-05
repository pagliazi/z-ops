"""
Z-OPS — ha_api: HA VM 高可用信息 API 客户端
通过 http://198.18.18.166:8088 获取 vSphere HA 重启的 VM列表数据

功能:
- 查询近期 HA 报告（含 VM 列表、客户 GID、源/目标主机）
- 查询实时 HA 告警
- 查询所有 vCenter 站点状态
- 跨站点搜索 HA VM
- 手动触发 HA 数据采集
"""

import logging
from typing import Optional, Dict, Any, List
import httpx

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")


# ═══════════════════════════════════════════════
#  HA API 端点目录
# ═══════════════════════════════════════════════

HA_API_BASE_URL = config.HA_API_URL
HA_API_TOKEN = config.HA_API_TOKEN or "opscore-ha-vm-query"  # 默认 API Key


class HaApiClient:
    """HA VM API 客户端 (支持 API Key 认证)"""

    def __init__(self, base_url: str = HA_API_BASE_URL, api_key: str = HA_API_TOKEN):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20, connect=5), verify=False, follow_redirects=True
            )
        return self._client

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": self.api_key
        }

    async def health_check(self) -> dict:
        """健康检查"""
        return await self.request("GET", "/api/v1/health")

    async def get_ha_vms(self, site: str, vm_name: str = None, include_expired: bool = False) -> dict:
        """
        获取 HA VM列表（从缓存读取）
        
        Args:
            site: 站点 ID（如 SCC）
            vm_name: VM 名称过滤（模糊匹配，可选）
            include_expired: 是否包含过期数据
            
        Returns:
            HA VM列表响应
        """
        params = {"site": site, "include_expired": include_expired}
        if vm_name:
            params["vm_name"] = vm_name
        return await self.request("GET", "/api/v1/ha-vms", params=params)

    async def search_ha_vms(self, vm_name: str = None, site: str = None) -> dict:
        """
        跨站点搜索HA VM
        
        Args:
            vm_name: VM 名称（模糊匹配）
            site: 限定站点（可选）
            
        Returns:
            HA VM 搜索结果
        """
        params = {}
        if vm_name:
            params["vm_name"] = vm_name
        if site:
            params["site"] = site
        return await self.request("GET", "/api/v1/ha-vms/search", params=params)

    async def get_sites_status(self) -> dict:
        """获取所有站点状态（旧版端点，已废弃）"""
        return await self.request("GET", "/api/v1/sites")

    # ═══════════════════════════════════════════════
    #  新版 HA Reports API（推荐）
    # ═══════════════════════════════════════════════

    async def get_ha_reports(self, limit: int = 10, site: str = None) -> dict:
        """
        获取 HA 报告列表（新版端点）
        
        Args:
            limit: 返回报告数量限制
            site: 过滤特定站点（可选）
            
        Returns:
            HA 报告列表
        """
        params = {"limit": limit}
        if site:
            params["site"] = site
        return await self.request("GET", "/api/v1/ha-reports", params=params)

    async def get_ha_report(self, report_id: str) -> dict:
        """
        获取单个 HA 报告详情
        
        Args:
            report_id: 报告 ID
            
        Returns:
            HA 报告详情（包含 VM 列表、客户信息等）
        """
        return await self.request("GET", f"/api/v1/ha-reports/{report_id}")

    async def collect_ha_report(self, site: str, days: int = 30, trigger: str = "manual", host_filter: str = None) -> dict:
        """
        触发 HA 数据采集并生成报告
        
        Args:
            site: 站点 ID（如 HKHKGBBB）
            days: 回溯天数
            trigger: 触发类型（manual/auto）
            host_filter: 主机过滤（可选）
            
        Returns:
            采集结果
        """
        data = {
            "site": site,
            "days": days,
            "trigger": trigger
        }
        if host_filter:
            data["host_filter"] = host_filter
        return await self.request("POST", "/api/v1/ha-reports/collect", data=data)

    async def delete_ha_report(self, report_id: str) -> dict:
        """删除 HA 报告"""
        return await self.request("DELETE", f"/api/v1/ha-reports/{report_id}")

    # ═══════════════════════════════════════════════
    #  HA Monitor API（实时监控）
    # ═══════════════════════════════════════════════

    async def get_ha_monitor_status(self) -> dict:
        """获取 HA 监控器状态（Prometheus 连接、采集器配置等）"""
        return await self.request("GET", "/api/v1/ha-monitor/status")

    async def get_ha_monitor_alerts(self) -> dict:
        """获取当前 HA 告警列表"""
        return await self.request("GET", "/api/v1/ha-monitor/alerts")

    async def get_ha_monitor_dashboard(self) -> dict:
        """获取 HA 监控仪表盘（概览 + 近期报告 + 站点告警分布）"""
        return await self.request("GET", "/api/v1/ha-monitor/dashboard")

    async def get_ha_monitor_sites(self) -> dict:
        """获取所有 vCenter 站点列表（新版）"""
        return await self.request("GET", "/api/v1/ha-monitor/sites")

    async def alert_trigger(self, site: str, alert_type: str = "HA", force_refresh: bool = False) -> dict:
        """
        告警触发刷新缓存
        
        Args:
            site: 站点 ID
            alert_type: 告警类型
            force_refresh: 是否强制刷新
            
        Returns:
            刷新结果
        """
        data = {
            "site": site,
            "alert_type": alert_type,
            "force_refresh": force_refresh
        }
        return await self.request("POST", "/api/v1/alert-trigger", data=data)

    async def refresh_site(self, site: str, force: bool = False) -> dict:
        """
        手动刷新站点缓存
        
        Args:
            site: 站点 ID
            force: 是否强制刷新
            
        Returns:
            刷新结果
        """
        return await self.request("POST", f"/api/v1/refresh/{site}", 
                                 params={"force": force})

    async def clear_cache(self, site: str) -> dict:
        """
        清除站点缓存
        
        Args:
            site: 站点 ID
            
        Returns:
            操作结果
        """
        return await self.request("DELETE", f"/api/v1/cache/{site}")

    async def request(self, method: str, path: str, 
                     params: dict = None, data: dict = None) -> dict:
        """发起 API 请求"""
        url = f"{self.base_url}{path}"
        try:
            client = await self._get_client()
            headers = self._headers()
            
            if method.upper() == "GET":
                resp = await client.get(url, headers=headers, params=params)
            elif method.upper() == "POST":
                resp = await client.post(url, headers=headers, json=data, params=params)
            elif method.upper() == "DELETE":
                resp = await client.delete(url, headers=headers, params=params)
            else:
                resp = await client.get(url, headers=headers, params=params)

            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text[:2000]}
            elif resp.status_code == 401:
                return {"error": "认证失败：API Key 无效"}
            elif resp.status_code == 404:
                return {"error": f"端点不存在：{url}"}
            else:
                return {"error": f"HTTP {resp.status_code}", "url": url}

        except httpx.TimeoutException:
            return {"error": f"请求超时：{url}"}
        except Exception as e:
            return {"error": f"请求失败：{str(e)}"}

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


# ═══ 单例客户端 ═══
_ha_client: Optional[HaApiClient] = None


def get_ha_client(api_key: str = HA_API_TOKEN) -> HaApiClient:
    """获取 HA API 客户端"""
    global _ha_client
    if _ha_client is None:
        _ha_client = HaApiClient(api_key=api_key)
    return _ha_client


async def query_ha_vms(site: str, vm_name: str = None, include_expired: bool = False) -> dict:
    """
    查询 HA VM列表
    
    用法: result = await query_ha_vms("SCC", vm_name="web-server")
    """
    client = get_ha_client()
    return await client.get_ha_vms(site, vm_name, include_expired)


async def search_ha_vms(vm_name: str = None, site: str = None) -> dict:
    """
    搜索HA VM
    
    用法: result = await search_ha_vms(vm_name="db", site="SCC")
    """
    client = get_ha_client()
    return await client.search_ha_vms(vm_name, site)


async def get_all_sites() -> dict:
    """
    获取所有站点状态（旧版，已废弃）
    
    用法: sites = await get_all_sites()
    """
    client = get_ha_client()
    return await client.get_sites_status()


# ═══ 新版 HA Reports 便捷函数 ═══

async def get_recent_ha_reports(limit: int = 10, site: str = None) -> dict:
    """
    获取近期 HA 报告列表
    
    用法: reports = await get_recent_ha_reports(limit=5, site="HKHKGBBB")
    """
    client = get_ha_client()
    return await client.get_ha_reports(limit=limit, site=site)


async def get_ha_report_detail(report_id: str) -> dict:
    """
    获取 HA 报告详情
    
    用法: detail = await get_ha_report_detail("c38ec131")
    """
    client = get_ha_client()
    return await client.get_ha_report(report_id)


async def trigger_ha_collection(site: str, days: int = 30) -> dict:
    """
    触发 HA 数据采集
    
    用法: result = await trigger_ha_collection("HKHKGBBB", days=60)
    """
    client = get_ha_client()
    return await client.collect_ha_report(site, days=days)


async def get_ha_dashboard() -> dict:
    """
    获取 HA 监控仪表盘
    
    用法: dashboard = await get_ha_dashboard()
    """
    client = get_ha_client()
    return await client.get_ha_monitor_dashboard()
