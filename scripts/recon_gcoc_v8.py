"""侦察 v8：用行内 Serial 字段映射，看污染串行在 GCOC_use 响应里的真实行内容。"""
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
        targets = {"1295022955", "1295022956", "1295398255", "1295511030"}

        # GCOC_use 响应
        f = fmap.get("GCOC_use")
        url = agent._nms_api_url(f["name"], "global", f["view"], "false")
        resp = await page.request.get(url, headers={"Accept-Encoding": "identity"})
        obj = json.loads(await resp.text())
        ds0 = obj["header"]["datasource"][0]
        gfields = [x.get("name") for x in ds0.get("field", [])]
        grows = obj["rows"][0] if obj.get("rows") else []
        si = gfields.index("Serial") if "Serial" in gfields else None
        print(f"GCOC_use: rows={len(grows)}, Serial列={si}")
        by_serial = {}
        for row in grows:
            if si is not None and si < len(row):
                by_serial[str(row[si]).strip()] = row
        for s in sorted(targets):
            row = by_serial.get(s)
            if row:
                d = dict(zip(gfields, row))
                print(f"\nGCOC_use 真实行 serial={s}:")
                for k in ("EventGroup", "AlertGroup", "Summary", "Node", "NodeAlias"):
                    print(f"    {k} = {str(d.get(k))[:70]!r}")
            else:
                print(f"\nGCOC_use serial={s}: 不在 rows 中")

        # 主集 Default
        master = fmap.get("AllEvents")
        mf = dict(master)
        mf["view"] = "Default"
        mfields, mrows, mserials = await agent._nms_api_get_full(page, mf)
        msi = mfields.index("Serial") if "Serial" in mfields else None
        m_by_serial = {}
        for row in mrows:
            if msi is not None and msi < len(row):
                m_by_serial[str(row[msi]).strip()] = row
        print(f"\n主集: rows={len(mrows)}")
        for s in sorted(targets):
            row = m_by_serial.get(s)
            if row:
                d = dict(zip(mfields, row))
                print(f"\n主集真实行 serial={s}:")
                for k in ("EventGroup", "AlertGroup", "Summary", "Node", "NodeAlias"):
                    print(f"    {k} = {str(d.get(k))[:70]!r}")
            else:
                print(f"\n主集 serial={s}: 不在 rows 中")

        await browser.close()


asyncio.run(main())
