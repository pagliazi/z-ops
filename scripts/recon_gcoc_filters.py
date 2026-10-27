"""侦察：NMS filter 定义——GCOC_use 的 SQL 与哪些 filter 含 BGP。"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/zops")

import config
from agents.nms_agent import NmsAgent


async def main():
    agent = NmsAgent()
    from playwright.async_api import async_playwright
    from collections import Counter
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await agent._nms_login(page)

        filters = await agent._nms_fetch_filters_meta(page)
        fmap = {f["name"]: f for f in filters}

        # 1) 主集 AllEvents（全量串行 + 字段）
        master = fmap.get("AllEvents")
        mf = dict(master)
        mf["view"] = "Default"
        fields, rows, serials = await agent._nms_api_get_full(page, mf)
        merged = {}
        n = min(len(rows), len(serials))
        for i in range(n):
            merged[serials[i]] = agent._nms_row_to_alarm(fields, rows[i], serials[i])
        print(f"主集 AllEvents: {len(merged)} 条")

        # 2) BGPStatus 匹配集
        bgp_serials, _ = await agent._nms_api_serials(page, fmap.get("BGPStatus"))
        bgp_set = set(bgp_serials)
        print(f"BGPStatus 匹配: {len(bgp_set)} 条")

        # 3) GCOC_use 匹配集
        gcoc_serials, fell_back = await agent._nms_api_serials(page, fmap.get("GCOC_use"))
        gcoc_set = set(gcoc_serials)
        print(f"GCOC_use 匹配: {len(gcoc_set)} 条 (fallback={fell_back})")

        # 4) 交集：GCOC_use 里的 BGP 告警
        inter = gcoc_set & bgp_set
        print(f"\n交集 (GCOC_use ∩ BGPStatus): {len(inter)} 条")
        if inter:
            print("\n=== 交集样本（前 10 条）===")
            for srl in list(inter)[:10]:
                a = merged.get(srl)
                if not a:
                    continue
                print(f"  EventGroup={a.get('event_group')!r} AlertGroup={a.get('alarm_type')!r}")
                print(f"    Summary={str(a.get('summary'))[:110]!r}")
            eg = Counter(merged[s].get("event_group") for s in inter if s in merged)
            print("\n=== 交集 EventGroup 分布 ===")
            for k, v in eg.most_common(15):
                print(f"  {k!r}: {v}")
            non_bgp = gcoc_set - bgp_set
            eg2 = Counter(merged[s].get("event_group") for s in non_bgp if s in merged)
            print(f"\n=== GCOC_use 非BGP EventGroup Top 10 (共 {len(non_bgp)}) ===")
            for k, v in eg2.most_common(10):
                print(f"  {k!r}: {v}")

        await browser.close()


asyncio.run(main())
