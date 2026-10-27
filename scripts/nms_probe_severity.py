"""验证 NMS severity ToggleButton 切换是否生效：只勾选指定级别，对比表格内容变化。"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/zops")

import config
from agents.nms_agent import NmsAgent

SEV_BTN_JS = """async (severities) => {
  const dijit = window.dijit;
  if (!dijit) return {ok:false, err:'no dijit'};
  const root = document.querySelector("[id*='toolbar_severityButtons']");
  if (!root) return {ok:false, err:'no severityButtons root'};
  const titles = ['Critical','Major','Minor','Warning','Indeterminate'];
  const states = [];
  for (let i = 0; i < titles.length; i++) {
    const el = document.getElementById(root.id + '_severityButton' + i);
    if (!el) continue;
    const b = dijit.getEnclosingWidget(el) || dijit.byId(root.id + '_severityButton' + i);
    if (!b) continue;
    const want = severities.indexOf(titles[i]) >= 0;
    if (b.get('checked') !== want) b.set('checked', want);
    states.push({t: titles[i], checked: b.get('checked')});
  }
  return {ok:true, states: states};
}"""


async def main():
    agent = NmsAgent()
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(ignore_https_errors=True)
        await agent._nms_login(page, config.NMS_USERNAME, config.NMS_PASSWORD)
        frame = await agent._nms_get_content_frame(page)
        if not frame:
            print("FAIL: 未找到 content frame")
            await browser.close()
            return
        r = await agent._nms_select_filter(frame, "GCOC_use")
        print("切换分组:", json.dumps(r, ensure_ascii=False))
        await asyncio.sleep(3)
        before = await agent._nms_table_snapshot(frame)
        print("切换前表格指纹长度:", len(before))

        # 只勾选 Critical（严重）
        r2 = await frame.evaluate(SEV_BTN_JS, ["Critical"])
        print("severity 按钮状态:", json.dumps(r2, ensure_ascii=False))
        await agent._nms_wait_table_changed(frame, before, max_wait=10)
        after = await agent._nms_table_snapshot(frame)
        print("切换后表格指纹长度:", len(after))
        print("表格已变化:", before != after)
        # 截一张图供人工核对
        await page.screenshot(path="/opt/zops/screenshots/probe_sev_critical.png")
        print("已保存: screenshots/probe_sev_critical.png")

        # 再勾选 Major + Minor（三级别）
        r3 = await frame.evaluate(SEV_BTN_JS, ["Critical", "Major", "Minor"])
        print("三级别按钮状态:", json.dumps(r3, ensure_ascii=False))
        await asyncio.sleep(3)
        after3 = await agent._nms_table_snapshot(frame)
        print("三级别后表格指纹长度:", len(after3))
        await page.screenshot(path="/opt/zops/screenshots/probe_sev_cmm.png")
        print("已保存: screenshots/probe_sev_cmm.png")
        await browser.close()


asyncio.run(main())
