"""NMS CMDB 资产与客户影响查询 — 从 api/routers/agents.py 抽出, 解除循环导入。

依赖: redis + agents.opscore_api.get_mgt_client (懒加载), 无 FastAPI / webchat_api 依赖。
被 nms_agent.prompt_builder 与 api/routers/agents.py 的 checklist 端点共用。
"""
import json
import logging
import re
from typing import List, Dict, Optional

import config

logger = logging.getLogger("zops")

_IP4_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')


# ── 设备归一化 (各厂商 raw record → 统一 device dict) ──
def _nms_compellent_to_device(a: dict, key: str) -> dict:
    return {'key': key, 'name': a.get('name', ''), 'hostname': a.get('name', ''),
            'ip': a.get('ip', ''), 'sn': str(a.get('serial_number') or ''),
            'vendor': 'Dell Compellent', 'model': a.get('model') or '未采集',
            'firmware': a.get('firmware_version') or '未采集',
            'site': a.get('system_name') or '', 'status': 'ok'}


def _nms_netapp_to_device(a: dict, key: str) -> dict:
    return {'key': key, 'name': a.get('name', ''), 'hostname': a.get('name', ''),
            'ip': a.get('ip', ''), 'sn': str(a.get('serial_number') or ''),
            'vendor': 'NetApp', 'model': a.get('model') or '未采集',
            'firmware': a.get('ontap_version') or '未采集',
            'site': a.get('cluster_name') or '', 'status': 'ok'}


def _nms_nimble_to_device(a: dict, key: str) -> dict:
    return {'key': key, 'name': a.get('name', ''), 'hostname': a.get('name', ''),
            'ip': a.get('ip', ''), 'sn': str(a.get('serial_number') or ''),
            'vendor': 'HPE Nimble', 'model': a.get('model') or '未采集',
            'firmware': a.get('firmware_version') or '未采集',
            'site': a.get('system_name') or '', 'status': 'ok'}


# ── 通用 OPS-CORE-MGT 列表端点缓存 (10 分钟) ──
async def _nms_mgt_list_cached(r, ck: str, path: str):
    try:
        cached = await r.get(ck)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    try:
        from agents.opscore_api import get_mgt_client
        data = await get_mgt_client().request(
            "GET", path, params={"page_size": 200})
        if isinstance(data, dict) and 'error' not in data:
            items = data.get('results') or []
            await r.set(ck, json.dumps(items, ensure_ascii=False), ex=600)
            return items
    except Exception:
        pass
    return None


def _short_upper(name) -> str:
    """主机名短名大写形态 (FQDN → 设备标签样式)"""
    return str(name or "").strip().split(".")[0].upper()


def _host_name_eq(name, ku) -> bool:
    """主机名等价: 去域去大小写比较 (FQDN esxi-18.example.internal == ESXI-HK-3118)"""
    a = str(name or "").strip().lower().split(".")[0]
    b = str(ku or "").strip().lower().split(".")[0]
    return bool(a) and a == b


# ── ESXi 主机精确匹配 (走 smartops_vms) ──
async def _nms_query_esxi_host(r, name: str):
    ku = name.strip()
    if not ku or _IP4_RE.fullmatch(ku):
        return None
    ck = f"{config.REDIS_PREFIX}:nms:esxi_host:{ku.lower()}"
    try:
        cached = await r.get(ck)
        if cached is not None:
            return json.loads(cached) or None
    except Exception:
        pass
    dev = None
    try:
        from agents.opscore_api import get_mgt_client
        data = await get_mgt_client().request(
            "GET", "/api/smart-ops/vms/", params={"search": ku, "page_size": 50})
        if isinstance(data, dict) and 'error' not in data:
            for vm in data.get('results') or []:
                if _host_name_eq(vm.get('host_name'), ku):
                    loc = ' / '.join(x for x in (vm.get('vcenter_server'),
                                                 vm.get('datacenter'),
                                                 vm.get('cluster')) if x)
                    _short = str(vm.get('host_name') or '').split('.')[0].upper()
                    dev = {'key': name, 'name': _short,
                           'hostname': _short, 'ip': '',
                           'sn': '', 'vendor': 'VMware ESXi', 'model': '未采集',
                           'firmware': '未采集', 'site': loc, 'status': 'ok'}
                    break
    except Exception:
        pass
    try:
        await r.set(ck, json.dumps(dev, ensure_ascii=False) if dev else 'null', ex=600)
    except Exception:
        pass
    return dev


# ── vCenter 虚机按 IP 查询 (含客户归属) ──
async def _nms_vms_by_ip(r, ip: str) -> List[Dict]:
    ck = f"{config.REDIS_PREFIX}:nms:vms_ip:{ip}"
    try:
        cached = await r.get(ck)
        if cached is not None:
            return json.loads(cached)
    except Exception:
        pass
    vms = []
    try:
        from agents.opscore_api import get_mgt_client
        data = await get_mgt_client().request(
            "GET", "/api/vcenter/vms/", params={"search": ip, "page_size": 100})
        if isinstance(data, dict) and 'error' not in data:
            for vm in data.get('results') or []:
                if vm.get('guest_ip') == ip:
                    vms.append({'name': vm.get('name', ''),
                                'customer': str(vm.get('customer_name')
                                                or vm.get('customer') or ''),
                                'vcenter': vm.get('vcenter_ip') or '',
                                'cluster': vm.get('cluster') or ''})
    except Exception:
        pass
    try:
        await r.set(ck, json.dumps(vms, ensure_ascii=False), ex=600)
    except Exception:
        pass
    return vms


# ── 单设备资产查询 (IP/主机名/SN) ──
async def _nms_query_cmdb_device(r, key: str):
    ku = key.strip()
    _sources_ok = 0
    comps = await _nms_mgt_list_cached(
        r, f"{config.REDIS_PREFIX}:nms:mgt_compellent",
        "/api/storage/compellent-arrays/")
    if comps is not None:
        _sources_ok += 1
    for a in comps or []:
        if (a.get('ip') == ku or (a.get('name') or '').lower() == ku.lower()
                or str(a.get('serial_number') or '').lower() == ku.lower()):
            return _nms_compellent_to_device(a, key)
    netapps = await _nms_mgt_list_cached(
        r, f"{config.REDIS_PREFIX}:nms:mgt_netapp",
        "/api/storage/netapp-arrays/")
    if netapps is not None:
        _sources_ok += 1
    for a in netapps or []:
        if (a.get('ip') == ku or (a.get('name') or '').lower() == ku.lower()
                or str(a.get('serial_number') or '').lower() == ku.lower()):
            return _nms_netapp_to_device(a, key)
    nimbles = await _nms_mgt_list_cached(
        r, f"{config.REDIS_PREFIX}:nms:mgt_nimble", "/api/storage/nimble/")
    if nimbles is not None:
        _sources_ok += 1
    for a in nimbles or []:
        if (a.get('ip') == ku or (a.get('name') or '').lower() == ku.lower()
                or str(a.get('serial_number') or '').lower() == ku.lower()):
            return _nms_nimble_to_device(a, key)
    vhosts = await _nms_mgt_list_cached(
        r, f"{config.REDIS_PREFIX}:nms:mgt_vhosts", "/api/vcenter/hosts/")
    if vhosts is not None:
        _sources_ok += 1
    for h in vhosts or []:
        if h.get('ip_address') == ku or _host_name_eq(h.get('name'), ku):
            loc = ' / '.join(x for x in (h.get('datacenter'), h.get('cluster')) if x)
            fw = ' / '.join(x for x in (h.get('version'), h.get('build')) if x)
            _short2 = str(h.get('name') or '').split('.')[0].upper()
            return {'key': key, 'name': _short2,
                    'hostname': _short2, 'ip': h.get('ip_address', ''),
                    'sn': '', 'vendor': h.get('manufacturer') or 'VMware ESXi',
                    'model': h.get('model') or '未采集',
                    'firmware': fw or '未采集', 'site': loc, 'status': 'ok'}
    dev = await _nms_query_esxi_host(r, ku)
    if dev:
        return dev
    # 容错: 精确未命中 → 短名包含匹配 (去域大写; 输入少写字符/大小写混用也能命中)
    ku_u = ku.strip().upper()
    for pool, to_dev in ((comps, _nms_compellent_to_device),
                         (netapps, _nms_netapp_to_device),
                         (nimbles, _nms_nimble_to_device)):
        for a in (pool or []):
            if ku_u in str(a.get('name') or '').upper() or ku_u in str(a.get('ip') or '').upper():
                return to_dev(a, key)
    for h in (vhosts or []):
        if ku_u in _short_upper(h.get('name')):
            loc = ' / '.join(x for x in (h.get('datacenter'), h.get('cluster')) if x)
            fw = ' / '.join(x for x in (h.get('version'), h.get('build')) if x)
            return {'key': key, 'name': _short_upper(h.get('name')),
                    'hostname': _short_upper(h.get('name')), 'ip': h.get('ip_address', ''),
                    'sn': '', 'vendor': h.get('manufacturer') or 'VMware ESXi',
                    'model': h.get('model') or '未采集',
                    'firmware': fw or '未采集', 'site': loc, 'status': 'ok'}
    if _sources_ok == 0:
        return {'key': key, 'status': 'OPS-CORE-MGT 资产查询失败'}
    return {'key': key, 'status': 'not_found'}


async def _nms_lookup_cmdb_devices(r, targets) -> List[Dict]:
    """并发查询设备资产 (最多 8 个目标, 去重保序)"""
    keys = list(dict.fromkeys(t for t in targets if t))[:8]
    if not keys:
        return []
    return await __import__("asyncio").gather(
        *[_nms_query_cmdb_device(r, k) for k in keys])


# ── 客户影响 (存储卷映射 + vCenter 虚机客户归属) ──
async def _nms_mcp_customer_impact(r, ips: List[str]) -> Dict:
    import asyncio
    ips = list(dict.fromkeys(i for i in ips if i))[:8]
    if not ips:
        return {'vms': 0, 'customers': {}, 'src': ''}
    customers, vm_count, srcs = {}, 0, []
    maps = await _nms_mgt_list_cached(
        r, f"{config.REDIS_PREFIX}:nms:mgt_mappings",
        "/api/storage/mappings/")
    hit_arrays = set()
    for m in maps or []:
        if m.get('storage_array_ip') in ips:
            hit_arrays.add(m.get('storage_array_ip'))
            vm_count += 1
            c = str(m.get('company_name') or m.get('customer') or '').strip()
            if c:
                customers[c] = customers.get(c, 0) + 1
    if hit_arrays:
        srcs.append(f'存储卷映射命中 {len(hit_arrays)} 台阵列')
    vm_results = await asyncio.gather(*[_nms_vms_by_ip(r, ip) for ip in ips])
    for vms in vm_results:
        if vms:
            vm_count += len(vms)
            srcs.append(f'vCenter 虚机 {len(vms)} 台')
            for v in vms:
                if v['customer']:
                    customers[v['customer']] = customers.get(v['customer'], 0) + 1
    return {'vms': vm_count, 'customers': customers, 'src': '；'.join(srcs)}