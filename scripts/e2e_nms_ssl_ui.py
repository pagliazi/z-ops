#!/usr/bin/env python3
"""NMS SSL 证书调查闭环 — 浏览器端到端验收脚本（Playwright / 无头 Chromium）。

不使用 mock：真实打开 Next.js 页面、真实登录、真实调用后端。测试数据来自
专用账号 ssl_e2e（个人快照由验收脚本预置）与 ssl_e2e_admin（案例审核）。

用法：
    python3 scripts/e2e_nms_ssl_ui.py flow1        # 入口 / 面板 / 证据 / 页签切换 / 记录
    python3 scripts/e2e_nms_ssl_ui.py manual       # 人工处置（进入 observing）
    python3 scripts/e2e_nms_ssl_ui.py observe      # 等待 3 次健康 + 截图（waiting_nms）
    python3 scripts/e2e_nms_ssl_ui.py case         # 提交案例
    python3 scripts/e2e_nms_ssl_ui.py approve      # 管理员审核通过
    python3 scripts/e2e_nms_ssl_ui.py cite         # 新调查引用已批准案例
    python3 scripts/e2e_nms_ssl_ui.py cleared      # 告警消失后仍可访问记录
"""

import argparse
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("ZOPS_WEB_BASE", "http://127.0.0.1:3000")
SHOT_DIR = "/opt/zops/screenshots"
USER = "ssl_e2e"
PWD = "E2e#Ssl2026"
ADMIN = "ssl_e2e_admin"
HOST = "s3.example.internal"

REPORT = {"steps": [], "texts": {}, "screenshots": []}


def log(step, detail=""):
    line = f"[step] {step}" + (f" | {detail}" if detail else "")
    print(line, flush=True)
    REPORT["steps"].append({"step": step, "detail": detail, "at": time.strftime("%H:%M:%S")})


def shot(page, name):
    os.makedirs(SHOT_DIR, exist_ok=True)
    path = os.path.join(SHOT_DIR, name)
    page.screenshot(path=path, full_page=True)
    REPORT["screenshots"].append(path)
    print(f"[shot] {path}", flush=True)
    return path


def login(page, username, password):
    page.goto(BASE + "/", wait_until="networkidle", timeout=60000)
    if page.locator("input[type=password]").count() == 0:
        log("login", "已有会话，跳过登录")
        return
    page.locator("input").first.fill(username)
    page.locator("input[type=password]").first.fill(password)
    page.locator("button[type=submit]").first.click()
    try:
        page.wait_for_selector("input[type=password]", state="detached", timeout=20000)
    except Exception:
        shot(page, "login-failed.png")
        dump("login-failed", page.locator("body").inner_text())
        raise RuntimeError("登录失败：" + page.locator("body").inner_text()[:300])
    log("login", f"{username} 登录成功")


def open_nms(page, need_row=True):
    page.goto(BASE + "/nms", wait_until="networkidle", timeout=60000)
    page.wait_for_selector("text=当前告警", timeout=40000)
    page.locator("button:has-text('当前告警')").first.click()
    page.wait_for_selector("text=证书", timeout=30000)
    page.wait_for_timeout(2000)
    if not need_row:
        return
    try:
        page.wait_for_selector(f'tr:has-text("{HOST}")', timeout=25000)
    except Exception:
        shot(page, "debug-alarms.png")
        dump("debug-alarms", page.locator("body").inner_text())
        raise


def open_records(page):
    page.locator("button:has-text('事件视图')").first.click()
    page.wait_for_timeout(1000)
    page.locator("button:has-text('调查记录')").first.click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=ssl-panel]')?.innerText.includes('我的调查')",
        timeout=20000)
    page.wait_for_timeout(1200)


def open_inv(page):
    panel(page).locator(f'button:has-text("{HOST}")').first.click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=ssl-panel]')?.innerText.includes('证书调查')",
        timeout=20000)
    page.wait_for_timeout(1800)


def ssl_row(page):
    return page.locator(f'tr:has-text("{HOST}")').first


def open_panel(page):
    row = ssl_row(page)
    row.wait_for(timeout=30000)
    btn = row.locator('button:has-text("调查")').first
    btn.wait_for(timeout=15000)
    btn.click()
    page.wait_for_function(
        "() => { const el = document.querySelector('[data-testid=ssl-dock]');"
        " return el && el.getBoundingClientRect().width > 300; }", timeout=15000)
    page.wait_for_timeout(3000)
    log("停靠区宽度", f"{dock_width(page)}px")


def panel(page):
    return page.locator('[data-testid="ssl-panel"]')


def dock_width(page):
    try:
        box = page.locator('[data-testid="ssl-dock"]').bounding_box(timeout=5000)
        return round(box["width"]) if box else 0
    except Exception:
        return 0


def panel_text(page):
    try:
        return panel(page).inner_text(timeout=5000)
    except Exception:
        return ""


def dump(name, text):
    path = os.path.join(SHOT_DIR, f"{name}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    REPORT["texts"][name] = text[:1200]
    print(f"[text] {path} ({len(text)} chars)", flush=True)


def flow1(page):
    open_nms(page)
    body = page.locator("body").inner_text()
    log("列表加载", f"snapshot rows={page.locator('tbody tr').count()}，含目标行={HOST in body}")
    log("表头含证书列", str("证书" in body))
    shot(page, "nms-1-list.png")

    row = ssl_row(page)
    row.wait_for(timeout=30000)
    row_text = row.inner_text()
    log("SSL 行", row_text.replace("\n", " · ")[:300])
    row_btn = row.locator('button:has-text("调查")').count()
    log("调查按钮数量", f"全部={page.locator('button', has_text='调查').count()}，本行={row_btn}")
    shot(page, "nms-2-entry.png")

    open_panel(page)
    text = panel_text(page)
    dump("nms-3-panel", text)
    log("面板已打开", f"url={page.url}")
    log("出现下钻字样", str("下钻" in page.locator("body").inner_text()))
    for key in ("待人工处置", "TLS", "校验", "剩余", "时间线", "恢复条件"):
        log(f"面板包含 {key}", str(key in text))
    shot(page, "nms-3-panel.png")

    # 页签切换：面板必须立即收起
    page.locator("button:has-text('事件视图')").first.click()
    page.wait_for_timeout(1200)
    gone = dock_width(page) < 50
    log("切到事件视图后停靠区收起", f"{gone}（宽度={dock_width(page)}px）")
    shot(page, "nms-4-tabswitch.png")

    page.locator("button:has-text('调查记录')").first.click()
    page.wait_for_selector("text=我的调查", timeout=20000)
    page.wait_for_timeout(1500)
    rec = panel_text(page)
    dump("nms-5-records", rec)
    log("记录列表含目标", str(HOST in rec))
    log("记录列表状态", " / ".join([s for s in ("待人工处置", "观察中", "等待 NMS", "已恢复", "已暂停") if s in rec]) or "未识别")
    shot(page, "nms-5-records.png")


def manual(page):
    open_nms(page)
    open_panel(page)
    page.locator("button:has-text('标记处置完成')").first.click()
    page.wait_for_timeout(800)
    sel = page.locator("select").first
    opts = sel.locator("option").all_inner_texts()
    log("处置类别选项", ", ".join(opts))
    sel.select_option(index=1)
    page.locator("textarea").first.fill("已续期并重载网关证书（浏览器验收）")
    page.locator("button:has-text('确认已处置')").first.click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=ssl-panel]')?.innerText.includes('自动跟踪中')",
        timeout=20000)
    page.wait_for_timeout(1500)
    text = panel_text(page)
    dump("nms-6-manual", text)
    log("状态进入观察", str("自动跟踪中" in text))
    shot(page, "nms-6-manual.png")


def observe(page, timeout_s=420):
    open_nms(page)
    open_panel(page)
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        text = panel_text(page)
        for key in ("等待 NMS 确认", "已恢复"):
            if key in text:
                dump("nms-7-waiting", text)
                shot(page, f"nms-7-{'waiting' if key == '等待 NMS 确认' else 'recovered'}.png")
                log("观察结果", key)
                REPORT["status"] = key
                return key
        last = text
        page.wait_for_timeout(5000)
    dump("nms-7-timeout", last)
    shot(page, "nms-7-timeout.png")
    log("观察超时", "未达到等待 NMS")
    return "timeout"


def recovered(page):
    open_nms(page, need_row=False)
    open_records(page)
    open_inv(page)
    text = panel_text(page)
    dump("nms-7-recovered", text)
    log("状态已恢复", str("已恢复" in text))
    log("恢复条件全部满足", str("TLS 连续 3 次严格校验健康" in text and "NMS 明确清除原告警" in text))
    shot(page, "nms-7-recovered.png")


def case(page):
    open_nms(page, need_row=False)
    open_records(page)
    open_inv(page)
    page.locator("button:has-text('提交成功案例')").first.click()
    page.wait_for_timeout(600)
    page.locator('input[placeholder="实际原因（必填）"]').first.fill("证书到期前已续期")
    page.locator('input[placeholder="实际操作（必填）"]').first.fill("更新网关证书并重载")
    page.locator('input[placeholder="适用条件（可选）"]').first.fill("仅适用于 443 端口自有证书")
    page.locator("button:has-text('提交审核')").first.click()
    page.wait_for_timeout(3500)
    text = panel_text(page)
    dump("nms-8-case", text)
    log("案例提交结果", "待审核" if "待审核" in text else ("已提交" if "案例已提交" in text else "未知"))
    shot(page, "nms-8-case.png")


def approve(page):
    open_nms(page, need_row=False)
    open_records(page)
    panel(page).locator("button:has-text('案例')").first.click()
    page.wait_for_timeout(2000)
    text = panel_text(page)
    dump("nms-9-cases", text)
    log("案例列表", "待审核" if "待审核" in text else "未发现待审核")
    log("提交人可见", "ssl_e2e" if "ssl_e2e" in text else "未显示")
    shot(page, "nms-9-cases.png")
    review = panel(page).locator("button:text-is('审核')").first
    if review.count() == 0:
        log("审核入口", "未找到（创建人自审应被隐藏，请确认使用管理员账号）")
        return
    review.click()
    page.wait_for_timeout(500)
    panel(page).locator('input[placeholder="审核说明（退回必填）"]').fill("已核对证据，批准（验收）")
    panel(page).locator("button:text-is('批准')").first.click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=ssl-panel]')?.innerText.includes('已批准')",
        timeout=20000)
    page.wait_for_timeout(1500)
    text = panel_text(page)
    dump("nms-10-approved", text)
    log("审核结果", "已批准" if "已批准" in text else "未识别")
    shot(page, "nms-10-approved.png")


def cite(page):
    open_nms(page)
    open_panel(page)
    page.wait_for_timeout(2500)
    text = panel_text(page)
    dump("nms-11-cite", text)
    log("引用区出现", str("同目标已批准案例" in text))
    log("引用案例内容", "证书到期前已续期" if "证书到期前已续期" in text else "未引用")
    shot(page, "nms-11-cite.png")


def cleared(page):
    open_nms(page, need_row=False)
    body = page.locator("body").inner_text()
    log("原告警是否仍在活跃列表", str(HOST in body))
    open_records(page)
    text = panel_text(page)
    dump("nms-12-cleared", text)
    log("记录仍可访问", str(HOST in text))
    shot(page, "nms-12-cleared.png")


def error_banner(page):
    """采集异常提示：nms_last_result=error 时面板必须明确提示且不作清除证据。"""
    open_nms(page, need_row=False)
    open_records(page)
    open_inv(page)
    text = panel_text(page)
    dump("nms-14-error", text)
    log("采集异常提示", str("采集异常" in text))
    log("不得声称恢复", str("已恢复" not in text))
    shot(page, "nms-14-error.png")


def polling(page):
    open_nms(page)
    open_panel(page)
    hits = {"detail": 0}

    def on_resp(resp):
        if "/api/nms/ssl-investigations/" in resp.url and resp.request.method == "GET":
            hits["detail"] += 1

    page.on("response", on_resp)
    base = hits["detail"]
    page.wait_for_timeout(16000)
    visible = hits["detail"] - base
    log("可见时 16s 详情请求数", str(visible))
    # 无头浏览器不会真正后台化标签，这里覆盖 document.hidden 模拟隐藏（组件每 tick 读取）
    page.evaluate("() => Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })")
    log("模拟 document.hidden", str(page.evaluate("() => document.hidden")))
    base2 = hits["detail"]
    page.wait_for_timeout(16000)
    hidden = hits["detail"] - base2
    log("隐藏时 16s 详情请求数", str(hidden))
    page.evaluate("() => Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })")
    base3 = hits["detail"]
    page.wait_for_timeout(11000)
    resumed = hits["detail"] - base3
    log("恢复可见后 11s 详情请求数", str(resumed))
    shot(page, "nms-13-polling.png")


PHASES = {
    "flow1": (USER, flow1),
    "manual": (USER, manual),
    "observe": (USER, observe),
    "recovered": (USER, recovered),
    "case": (USER, case),
    "approve": (ADMIN, approve),
    "cite": (USER, cite),
    "cleared": (USER, cleared),
    "error": (USER, error_banner),
    "polling": (USER, polling),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=sorted(PHASES))
    args = ap.parse_args()
    who, fn = PHASES[args.phase]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 950}, device_scale_factor=1)
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        login(page, who, PWD)
        fn(page)
        REPORT["console_errors"] = errors[:10]
        browser.close()
    print(json.dumps(REPORT, ensure_ascii=False, indent=2)[:4000])


if __name__ == "__main__":
    sys.exit(main())
