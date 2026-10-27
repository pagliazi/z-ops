"""侦察 v7：不同 filter 的 identifierList 是否相同（会话视图状态假说）。"""
import asyncio
import json
import re
import sys

sys.path.insert(0, "/opt/zops")

import config
from agents.nms_agent import NmsAgent


async def fetch_idl(page, agent, f):
    url = agent._nms_api_url(f["name"], "global", f["view"], "false")
    resp = await page.request.get(url, headers={"Accept-Encoding": "identity"})
    body = await resp.text()
    m = re.search(r'"identifierList":"([^"]*)"', body)
    if not m:
        return [], resp.status, 0
    ids = [x for x in m.group(1).split(",") if x.strip()]
    return ids, resp.status, len(body)


async def main():
    agent = NmsAgent()
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await agent._nms_login(page)

        filters = await agent._nms_fetch_filters_meta(page)
        fmap = {f["name"]: f for f in filters}
        names = ["GCOC_use", "GCOC_need_confirm", "BGPStatus", "PSC_use", "EU_CS_use"]
        results = {}
        for nm in names:
            f = fmap.get(nm)
            if not f:
                print(f"{nm}: 无此 filter")
                continue
            ids, status, blen = await fetch_idl(page, agent, f)
            results[nm] = set(ids)
            print(f"{nm}: {len(ids)} 条 (status={status}, body={blen})")

        # 两两比较
        ks = list(results.keys())
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                a, b = ks[i], ks[j]
                inter = len(results[a] & results[b])
                print(f"{a} ∩ {b} = {inter} (|a|={len(results[a])}, |b|={len(results[b])})")

        # 同一 filter 连抓 2 次
        f = fmap.get("GCOC_use")
        ids1, _, _ = await fetch_idl(page, agent, f)
        ids2, _, _ = await fetch_idl(page, agent, f)
        s1, s2 = set(ids1), set(ids2)
        print(f"\nGCOC_use 连续2次: {len(s1)} vs {len(s2)}, "
              f"仅第1次有={len(s1 - s2)}, 仅第2次有={len(s2 - s1)}")
        if s1 - s2:
            print(f"  仅第1次样本: {sorted(s1 - s2)[:5]}")
        if s2 - s1:
            print(f"  仅第2次样本: {sorted(s2 - s1)[:5]}")

        await browser.close()


asyncio.run(main())
