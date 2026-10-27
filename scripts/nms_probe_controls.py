"""NMS 页面控件侦察：登录后在告警 frame 内枚举所有 input/select 控件 id，
用于定位 severity（级别筛选）控件。用法: python3 scripts/nms_probe_controls.py [filter_name]
"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/zops")

import config
from agents.nms_agent import NmsAgent

PROBE_JS = """() => {
  const out = [];
  document.querySelectorAll("input, select").forEach(el => {
    out.push({
      tag: el.tagName,
      id: el.id || '',
      name: el.name || '',
      type: el.type || '',
      title: el.title || '',
      cls: (el.className || '').toString().slice(0, 80),
      visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
    });
  });
  // 也列出常见 severity 相关 widget
  const dijitW = window.dijit;
  let widgets = [];
  if (dijitW && dijitW.registry) {
    try {
      dijitW.registry.forEach(w => {
        if (/sever|filter|ack/i.test((w.id || '') + (w.class || ''))) {
          widgets.push({id: w.id, cls: w.declaredClass || w.class, title: w.title || ''});
        }
      });
    } catch (e) { widgets.push({err: String(e)}); }
  }
  return {controls: out, widgets: widgets};
}"""


async def main():
    filter_name = sys.argv[1] if len(sys.argv) > 1 else ""
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
        if filter_name:
            r = await agent._nms_select_filter(frame, filter_name)
            print("切换分组结果:", json.dumps(r, ensure_ascii=False))
            await asyncio.sleep(3)
        res = await frame.evaluate(PROBE_JS)
        print("=== 控件列表 (id | tag | name | title) ===")
        for c in res.get("controls") or []:
            if not c["visible"]:
                continue
            print(f"  {c['tag']:6} id={c['id']!r:45} name={c['name']!r:25} title={c['title']!r}")
        print("=== severity/filter/ack 相关 widget ===")
        for w in res.get("widgets") or []:
            print(" ", json.dumps(w, ensure_ascii=False))
        await browser.close()


asyncio.run(main())
