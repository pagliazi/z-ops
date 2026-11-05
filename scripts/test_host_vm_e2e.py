#!/usr/bin/env python3
"""端到端验证: query_vms_on_esxi_host 新链路 (冷/热/边界/字段完整性)"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from agents.opscore_api import (
    query_vms_on_esxi_host, expand_host_code, mcp_smartops_vms_all,
    get_mgt_client, _get_mcp_gateway_client,
)


async def main():
    print("=== 0. expand_host_code 边界 ===")
    for s in ["ESXI-02", "ESXI-02", "GDS3103BKP", "TICKET-47915", "web-server", "198.18.64.104", ""]:
        print(f"  {s!r} -> {expand_host_code(s)!r}")

    print("=== 1. 冷启动: 首次查询 ESXI-02 (含 MCP 全量缓存加载) ===")
    t0 = time.time()
    info = await query_vms_on_esxi_host("ESXI-02")
    dt = time.time() - t0
    assert info, "ESXI-02 应命中宿主机"
    print(f"  冷启动总耗时: {dt:.2f}s (内部 elapsed={info['elapsed']}s)")
    print(f"  宿主机: {info['esxi_hosts']}  站点: {info['site_hints']}  来源: {info['source']}")
    print(f"  VM 数: {info['count'] if 'count' in info else len(info['vms'])}  snapshot 命中: {info['snapshot_matched']}")
    vms = info["vms"]
    assert len(vms) >= 15, f"应 >=15 台, 实际 {len(vms)}"
    # 字段完整性抽查
    with_snap = [v for v in vms if v.get("site")]
    with_gid = [v for v in vms if v.get("customer_gid")]
    with_ip = [v for v in vms if v.get("guest_ip")]
    print(f"  含site字段: {len(with_snap)}/{len(vms)}  含GID: {len(with_gid)}  含guest_ip: {len(with_ip)}")
    sample = sorted(vms, key=lambda x: x.get('name',''))[0]
    print(f"  样本: name={sample.get('name')} power={sample.get('power_state')} site={sample.get('site')} rp={sample.get('resource_pool')} ip={sample.get('guest_ip')} gid={sample.get('customer_gid')}")

    print("=== 2. 热态: 全缓存命中 ===")
    t0 = time.time()
    info2 = await query_vms_on_esxi_host("ESXI-02")
    print(f"  热态耗时: {time.time()-t0:.2f}s (elapsed={info2['elapsed']}s) VM={len(info2['vms'])}")

    print("=== 3. 热态换主机: 198.18.64.104 (IP 形态宿主机) ===")
    t0 = time.time()
    info3 = await query_vms_on_esxi_host("198.18.64.104")
    print(f"  耗时: {time.time()-t0:.2f}s", end=" ")
    if info3:
        print(f"宿主机={info3['esxi_hosts']} VM={len(info3['vms'])} snapshot命中={info3['snapshot_matched']}")
        assert len(info3["vms"]) >= 5, f"应 >=5 台, 实际 {len(info3['vms'])}"
    else:
        print("未命中 (IP 输入正则不匹配是预期, expand_host_code 对纯IP返回 None)")

    print("=== 4. 非主机代码/不存在主机 ===")
    t0 = time.time()
    r = await query_vms_on_esxi_host("TICKET-47915")
    print(f"  TICKET-47915 -> {r} ({time.time()-t0:.2f}s, 应为 None)")
    t0 = time.time()
    r = await query_vms_on_esxi_host("ZZZ9999HT")
    print(f"  ZZZ9999HT -> {r} ({time.time()-t0:.2f}s, 不存在主机应为 None)")

    print("=== 5. vm_agent 路径 (_expand_site_vm_search) ===")
    from agents.vm_agent import VMAgent
    agent = VMAgent.__new__(VMAgent)  # 不走 __init__ (避免注册副作用)
    t0 = time.time()
    expanded = await agent._expand_site_vm_search("ESXI-02")
    print(f"  vm_agent 耗时: {time.time()-t0:.2f}s count={expanded['count']} source={expanded['_source']}")
    assert expanded["count"] >= 15

    print("=== 6. opscore_agent 路径 (_expand_site_search) ===")
    from agents.opscore_agent import OpscoreAgent
    g = OpscoreAgent.__new__(OpscoreAgent)
    t0 = time.time()
    items, text = await g._expand_site_search("ESXI-02")
    print(f"  opscore_agent 耗时: {time.time()-t0:.2f}s items={len(items)}")
    print("  输出预览:")
    for line in text.split("\n")[:5]:
        print(f"    {line[:150]}")

    print("\n✅ 全部通过" if len(vms) >= 15 else "❌ 有断言失败")

    await get_mgt_client().close()
    mc = await _get_mcp_gateway_client()
    await mc.aclose()


if __name__ == "__main__":
    asyncio.run(main())
