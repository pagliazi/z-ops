"""侦察 v5：同 serial 在主集 Default 视图 vs GCOC_use GroupByDevice 视图的内容对比。"""
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

        targets = ["1295022955", "1295022956", "1295398255", "1295511030"]

        # 1) 主集 Default 视图
        master = fmap.get("AllEvents")
        mf = dict(master)
        mf["view"] = "Default"
        fields, rows, serials = await agent._nms_api_get_full(page, mf)
        print(f"主集 rows={len(rows)} serials={len(serials)} fields={fields}")
        mrow = {}
        for i, s in enumerate(serials):
            if s in targets and i < len(rows):
                mrow[s] = rows[i]
        for s in targets:
            r = mrow.get(s)
            print(f"\n主集 serial={s}: {'FOUND' if r else 'MISSING'}")
            if r:
                d = dict(zip(fields, r))
                for k in ("EventGroup", "AlertGroup", "Summary", "Node", "NodeAlias"):
                    print(f"    {k} = {str(d.get(k))[:60]!r}")

        # 2) GCOC_use GroupByDevice 视图
        f = fmap.get("GCOC_use")
        url = agent._nms_api_url(f["name"], "global", f["view"], "false")
        resp = await page.request.get(url, headers={"Accept-Encoding": "identity"})
        obj = json.loads(await resp.text())
        ds0 = obj["header"]["datasource"][0]
        gfields = [x.get("name") for x in ds0.get("field", [])]
        grows = obj["rows"][0] if obj.get("rows") else []
        gserials = [x for x in str(ds0.get("identifierList") or "").split(",")
                    if x.strip()]
        print(f"\nGCOC_use rows={len(grows)} serials={len(gserials)} fields={gfields}")
        for s in targets:
            if s in gserials:
                idx = gserials.index(s)
                row = grows[idx] if idx < len(grows) else None
                print(f"\nGCOC_use serial={s} @idx={idx}: {'ROW' if row else 'NO ROW'}")
                if row:
                    d = dict(zip(gfields, row))
                    for k in ("EventGroup", "AlertGroup", "Summary", "Node", "NodeAlias", "Service", "Customer"):
                        if k in d:
                            print(f"    {k} = {str(d[k])[:60]!r}")
            else:
                print(f"\nGCOC_use serial={s}: NOT IN LIST")

        await browser.close()


asyncio.run(main())
