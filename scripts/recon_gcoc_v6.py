"""侦察 v6：GCOC_use 响应内部 rows 的 Serial 列 vs identifierList 对齐性。"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/zops")

import config
from agents.nms_agent import NmsAgent


async def main():
    agent = NmsAgent()
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await agent._nms_login(page)

        filters = await agent._nms_fetch_filters_meta(page)
        fmap = {f["name"]: f for f in filters}
        f = fmap.get("GCOC_use")
        url = agent._nms_api_url(f["name"], "global", f["view"], "false")
        resp = await page.request.get(url, headers={"Accept-Encoding": "identity"})
        obj = json.loads(await resp.text())
        ds0 = obj["header"]["datasource"][0]
        gfields = [x.get("name") for x in ds0.get("field", [])]
        grows = obj["rows"][0] if obj.get("rows") else []
        gserials = [x for x in str(ds0.get("identifierList") or "").split(",")
                    if x.strip()]
        print(f"GCOC_use rows={len(grows)} identifierList={len(gserials)}")
        print(f"字段名: {gfields}")
        si = gfields.index("Serial") if "Serial" in gfields else None
        print(f"Serial 列索引: {si}")
        mism = 0
        n = min(len(grows), len(gserials))
        for i in range(n):
            row_serial = str(grows[i][si]).strip() if si is not None and si < len(grows[i]) else "?"
            if row_serial != str(gserials[i]).strip():
                mism += 1
                if mism <= 8:
                    print(f"  idx={i}: 行内Serial={row_serial} vs identifierList={gserials[i]}")
        print(f"错位行数: {mism} / {n}")
        # 行内 Serial 去重：rows 里的 serial 集合与 identifierList 集合的关系
        row_sers = {str(grows[i][si]).strip() for i in range(n)} if si is not None else set()
        list_sers = set(gserials)
        print(f"行内 Serial 集合大小: {len(row_sers)}; identifierList 集合大小: {len(list_sers)}")
        print(f"仅行内有: {sorted(list(row_sers - list_sers))[:8]}")
        print(f"仅列表有: {sorted(list(list_sers - row_sers))[:8]}")
        await browser.close()


asyncio.run(main())
