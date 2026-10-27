"""GCOC 对外 API 客户端（Token 版）。

用途：替代 gcoc-ai 上「用 op 个人 AD 账号登录 → 拿 sessionid → 带 Cookie 调接口」的实现。
那个做法有两个硬伤：每天重复登录会在 AD 侧累计失败绑定次数把账号锁死；
session 只有 24 小时，过期就中断调用（401 后再登录，循环往复）。

用法：
    export GCOC_BASE_URL='https://gcoc-info.example.com'
    export GCOC_API_TOKEN='<向 GCOC 管理员索取，145 上 manage.py dns_token list 可查>'

    python3 gcoc_ai_client.py        # 自检：逐个接口各打一发

    from gcoc_ai_client import gcoc
    page = gcoc.cmdb_page(search='TWTCHFET3112CEPH', page_size=500)
    ans = gcoc.dns_query(['qq.com'], 'A')

已封装 GCOC 全部对外只读接口（gcoc-ai 实际用到的 7 个）：
    cmdb_page / site_info / alarm_info / monitor_info / case_info / dns_query / dns_compare
未封装的接口用 gcoc.get('/api/xxx/', **params) 或 gcoc.post(path, payload) 直调。

依赖：httpx（gcoc-ai 现有 0.26.x 即可）
错误码约定：
    401 Token 缺失/无效/被吊销     -> 不要重试，更不要改用账号密码登录
    403 Token 无该 scope 或来源 IP 不在白名单 -> 找 GCOC 管理员调 Token
    429 超过 30 次/分钟            -> 退避后重试（本客户端自动做 3 次退避）
"""
import os
import time

import httpx

DEFAULT_BASE_URL = 'https://gcoc-info.example.com'


class GCOCError(RuntimeError):
    """GCOC 接口调用失败（鉴权类错误不重试）。"""

    def __init__(self, status_code, message, payload=None):
        super().__init__(f'[{status_code}] {message}')
        self.status_code = status_code
        self.payload = payload


class GCOCClient:
    def __init__(self, token=None, base_url=None, timeout=60.0, max_retries=3):
        token = token or os.environ.get('GCOC_API_TOKEN', '').strip()
        if not token:
            raise GCOCError(0, '缺少 GCOC_API_TOKEN 环境变量或 token 参数')
        self.base_url = (base_url or os.environ.get('GCOC_BASE_URL', DEFAULT_BASE_URL)).rstrip('/')
        self.max_retries = max_retries
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/json',
            },
        )

    # ---------- 底层请求 ----------
    def _request(self, method, path, **kwargs):
        last_exc = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:            # 网络抖动/超时：退避重试
                last_exc = exc
                time.sleep(2 * (attempt + 1))
                continue

            if resp.status_code == 429:               # 限流：按窗口退避
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code >= 500:               # 服务端错误：退避重试
                last_exc = GCOCError(resp.status_code, resp.text[:200])
                time.sleep(2 * (attempt + 1))
                continue

            if resp.status_code in (401, 403):        # 鉴权类错误：立即失败，绝不重试
                try:
                    detail = resp.json().get('error', '')
                except ValueError:
                    detail = resp.text[:200]
                hint = '请检查 Token 是否有效' if resp.status_code == 401 else \
                    '请确认 Token 已授予对应 scope 且来源 IP 在白名单'
                raise GCOCError(resp.status_code, f'{detail}（{hint}）')

            try:
                body = resp.json()
            except ValueError:
                raise GCOCError(resp.status_code, f'响应非 JSON: {resp.text[:200]}')

            if resp.status_code >= 400 or body.get('success') is False:
                raise GCOCError(resp.status_code, body.get('error', '接口返回失败'), body)
            return body

        if isinstance(last_exc, GCOCError):
            raise last_exc
        raise GCOCError(0, f'请求 {path} 重试 {self.max_retries} 次仍失败: {last_exc}')

    # ---------- 业务接口 ----------
    def get(self, path, **params):
        """通用 GET（path 形如 /api/site_info/），用于客户端未封装的新接口。"""
        return self._request('GET', path, params=params)

    def post(self, path, payload):
        """通用 POST JSON。"""
        return self._request('POST', path, json=payload)

    def cmdb_page(self, search='', page=1, page_size=500):
        """CMDB 资产查询（scope=cmdb）。返回 dict：results/count/page/total_pages。"""
        return self._request('GET', '/api/cmdb_info/', params={
            'page': page, 'page_size': page_size, 'search': search,
        })

    def site_info(self):
        """站点/机房信息（scope=site），无参数。"""
        return self._request('GET', '/api/site_info/')

    def alarm_info(self):
        """告警信息（scope=alarm），无参数。"""
        return self._request('GET', '/api/alarm_info/')

    def monitor_info(self, page=1, page_size=20, search='', max_items_per_ip=100):
        """监控信息（scope=monitor），按监控项分组分页。"""
        return self._request('GET', '/api/monitor_info/', params={
            'page': page, 'page_size': page_size,
            'search': search, 'max_items_per_ip': max_items_per_ip,
        })

    def case_info(self, page=1, page_size=20, **filters):
        """案例信息（scope=case）。filters 支持 caseid/status/severity/assignee_group。"""
        params = {'page': page, 'page_size': page_size}
        params.update({k: v for k, v in filters.items() if v not in (None, '')})
        return self._request('GET', '/api/case_info/', params=params)

    def dns_query(self, domains, record_type='A', server=None, expected_results=None):
        """DNS 解析查询（scope=dns）。

        domains: 字符串或列表，多域名会自动按行拼接批量查询。
        server : 指定 DNS 服务器 IP，默认 GCOC 侧 223.5.5.5。
        """
        if isinstance(domains, (list, tuple)):
            domains = '\n'.join(domains)
        payload = {'domain': domains, 'record_type': record_type}
        if server:
            payload['server'] = server
        if expected_results:
            payload['expected_results'] = expected_results
        return self._request('POST', '/api/dns/query/', json=payload)

    def dns_compare(self, domains, record_type='A', servers=None, expected_results=None):
        """多 DNS 服务器对比查询（scope=dns）。servers 缺省为 223.5.5.5, 8.8.8.8。

        返回 dict 关键字段：domains（每个域名一组，内含按 server 的 results）、consistent。
        """
        if isinstance(domains, (list, tuple)):
            domains = '\n'.join(domains)
        if isinstance(servers, (list, tuple)):
            servers = ','.join(servers)
        payload = {'domain': domains, 'record_type': record_type, 'servers': servers or ''}
        if expected_results:
            payload['expected_results'] = expected_results
        return self._request('POST', '/api/dns/compare/', json=payload)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# 进程内单例：147 现有代码直接 from gcoc_ai_client import gcoc 即可复用连接
gcoc = None
if os.environ.get('GCOC_API_TOKEN'):
    gcoc = GCOCClient()


if __name__ == '__main__':
    client = gcoc or GCOCClient()
    print(f'BASE = {client.base_url}')

    page = client.cmdb_page(page_size=1)
    print(f'[OK] cmdb    共 {page.get("count")} 条资产')
    print(f'[OK] site    {len(client.site_info().get("results", []))} 个站点')
    print(f'[OK] alarm   {len(client.alarm_info().get("results", []))} 条告警')
    print(f'[OK] monitor {client.monitor_info(page_size=1).get("count", "-")} 组监控项')
    print(f'[OK] case    {client.case_info(page_size=1).get("count", "-")} 条案例')
    ans = client.dns_query(['qq.com'], 'A')
    print(f'[OK] dns     qq.com A 记录 {len(ans.get("successful_results", []))} 条')
    cmp = client.dns_compare(['qq.com'], 'A', servers=['223.5.5.5'])
    print(f'[OK] compare {len(cmp.get("domains", []))} 个域名对比完成, consistent={cmp.get("consistent")}')
    client.close()
