"""侦察 NMS Event Viewer 界面上与确认状态(ACK)相关的筛选控件。

目标：找出类似 SeverityButtons 的 ACK 筛选按钮（Acknowledged/Unacknowledged），
以便截图时叠加"仅未确认"筛选。
"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/zops")

import config
from agents.nms_agent import NmsAgent

# 枚举 frame 内所有 toolbar 根控件 + 所有 ToggleButton/按钮类 widget
ENUM_JS = """() => {
  const dijit = window.dijit;
  const out = {toolbars: [], toggles: [], buttons: []};
  if (!dijit) return {ok:false, err:'no dijit'};
  // 1) 所有 id 含 toolbar 的根
  document.querySelectorAll("[id*='toolbar']").forEach(el => {
    const w = dijit.getEnclosingWidget(el);
    out.toolbars.push({id: el.id, widget: w ? w.declaredClass : null, label: (w && w.get && w.get('label')) || null});
  });
  // 2) 所有 ToggleButton / 按钮类控件
  const all = dijit.registry ? dijit.registry.toArray() : [];
  all.forEach(w => {
    const cls = w.declaredClass || '';
    const dom = w.domNode ? w.domNode.id : null;
    if (cls.indexOf('ToggleButton') >= 0) {
      out.toggles.push({id: dom, cls: cls, label: (w.get && w.get('label')) || null, checked: (w.get && w.get('checked'))});
    }
    if (cls.indexOf('Button') >= 0 && dom) {
      const lbl = (w.get && (w.get('label') || w.get('title'))) || null;
      const txt = (w.domNode && w.domNode.textContent || '').trim().slice(0, 40);
      if (lbl || txt) out.buttons.push({id: w.domNode ? w.domNode.id : null, cls: cls, label: lbl, text: txt});
    }
  });
  // 3) 查找含 ack 关键字的元素
  out.ackEls = [];
  document.querySelectorAll("[id*='ack'], [id*='Ack'], [id*='ACK']").forEach(el => {
    out.ackEls.push({id: el.id, tag: el.tagName, text: (el.textContent || '').trim().slice(0, 60)});
  });
  return {ok:true, ...out};
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
        await agent._nms_select_filter(frame, "OPSCORE_use")
        await asyncio.sleep(3)
        res = await frame.evaluate(ENUM_JS)
        print("═" * 60)
        print("toolbars:")
        for t in res.get("toolbars", [])[:20]:
            print("  ", json.dumps(t, ensure_ascii=False))
        print("═" * 60)
        print("toggle 控件:")
        for t in res.get("toggles", [])[:30]:
            print("  ", json.dumps(t, ensure_ascii=False))
        print("═" * 60)
        print("按钮类控件:")
        for b in res.get("buttons", [])[:40]:
            print("  ", json.dumps(b, ensure_ascii=False))
        print("═" * 60)
        print("ack 相关元素:")
        for a in res.get("ackEls", [])[:20]:
            print("  ", json.dumps(a, ensure_ascii=False))
        await browser.close()


asyncio.run(main())
