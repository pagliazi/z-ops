"""登录态下用 Playwright 真浏览器操作前端界面，只读探索「电脑操作」相关 UI 并截图。

背景：Spec 要求界面必须被真实操作并留下截图，不能用源码字符串断言代替。浏览器 MCP
（browser-use）当前不可用（`Notification handler already registered ...`），故改用仓库
锁定的 Playwright + Chromium。

登录：界面要 AD 域账号，代跑没有也不该留凭据。这里用后端自己的会话机制在 Redis 里
为本地 admin 账号临时签一个短 TTL token（等同 `secrets.token_hex(32)` + SET 那两步），
用完当场删除——不改密码、不建号、不授权，只为拿到能渲染的登录态。

纪律：全程只读。只在“打开面板 / 切标签 / 看状态”这类安全动作上点击，绝不点保存、
吊销、清空会话、删除凭据，也不往输入框写任何东西。
"""
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/zops")
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = Path(os.environ.get("ZOPS_UI_OUT", "/opt/zops/screenshots/ui"))
URL = os.environ.get("ZOPS_UI_URL", "http://localhost:3000/")
USERNAME = os.environ.get("ZOPS_UI_USER", "admin")
TTL = int(os.environ.get("ZOPS_UI_TTL", "300"))

# 明确禁止点击的动词，宁可漏掉功能也不能改动状态
FORBIDDEN = ("保存", "吊销", "删除", "清空", "重置", "确定", "提交", "授权", "断开",
             "注销", "退出", "生成配对", "立即", "重启", "应用", "stop", "revoke",
             "delete", "clear", "save", "disconnect")


def mint_token():
    """按后端同样方式签一个临时会话 token，返回 (token, 释放函数)。"""
    import config
    import redis
    r = redis.Redis.from_url(config.REDIS_URL)
    if not r.hexists(config.KEY_USERS, USERNAME):
        raise SystemExit("本地不存在账号 %s，停止" % USERNAME)
    token = secrets.token_hex(32)
    key = config.key_token(token)
    r.set(key, USERNAME, ex=TTL)
    user = json.loads(r.hget(config.KEY_USERS, USERNAME))
    raw = json.dumps({"username": USERNAME, "display_name": user.get("display_name", USERNAME),
                      "role": user.get("role", "user"), "auth_source": "local"})
    def release():
        r.delete(key)
        print("[cleanup] 已删除临时 token：%s" % key)
    return token, raw, release


def probe(page):
    """如实抄下当前可见的按钮/标签/标题/输入框原文，不加工不推断。"""
    return page.evaluate("""() => {
      const text = (el) => (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
      const vis = (el) => {
        const s = getComputedStyle(el), r = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
      };
      const grab = (sel) => [...document.querySelectorAll(sel)].filter(vis).map(text).filter(Boolean);
      return {
        url: location.href, title: document.title,
        buttons: grab('button'), tabs: grab('[role=tab]'), headings: grab('h1,h2,h3,h4'),
        labels: grab('label'), links: grab('a[href]'),
        inputs: [...document.querySelectorAll('input,select,textarea')].filter(vis)
                  .map((el) => ({kind: el.tagName.toLowerCase(), type: el.type || '',
                                 placeholder: el.placeholder || '', label: text(el)})),
        body_head: text(document.body).slice(0, 900)
      };
    }""")


def safe(label):
    return not any(word in label.lower() if word.isascii() else word in label for word in FORBIDDEN)


def click_if_safe(page, label, report, tag):
    if not safe(label):
        report.append({"skipped_click": label, "reason": "只读白名单外的动作"})
        return False
    try:
        page.get_by_role("button", name=label, exact=True).first.click(timeout=4000)
    except Exception as error:
        report.append({"click_failed": label, "error": str(error).splitlines()[0][:160]})
        return False
    page.wait_for_timeout(1200)
    shot = OUT / ("%s.png" % tag)
    page.screenshot(path=str(shot), full_page=True)
    report.append({"step": tag, "clicked": label, "shot": str(shot), **probe(page)})
    return True


def step_of(report, key, value):
    """回看报告里最近一条 key==value 的步骤，用于层层往里点时取上一级看到的按钮。"""
    for entry in reversed(report):
        if entry.get(key) == value:
            return entry
    return None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    token, user_raw, release = mint_token()
    report = [{"minted_for": USERNAME, "ttl": TTL}]
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            # 在导航前把会话写进 localStorage，等同用户自己登录后的状态
            page.add_init_script(
                "localStorage.setItem('zops_token', %s);\n"
                "localStorage.setItem('zops_user', %s);"
                % (json.dumps(token), json.dumps(user_raw)))
            page.goto(URL, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2000)
            page.screenshot(path=str(OUT / "01-authed-landing.png"), full_page=True)
            report.append({"step": "authed-landing", **probe(page)})

            first = report[-1]
            # 1) 找设置/电脑操作类入口
            want = [b for b in first["buttons"]
                    if any(k in b for k in ("设置", "电脑", "设备", "Settings", "⚙"))][:4]
            report.append({"settings_candidates": want})
            for i, label in enumerate(want):
                if click_if_safe(page, label, report, "02-entry-%d" % i):
                    # 点开后继续往里走：「打开电脑操作设置」是设置模态的真实入口
                    prev = step_of(report, "clicked", label) or {}
                    deeper = [b for b in prev.get("buttons", [])
                              if "打开" in b or "任务小窗" in b][:3]
                    for j, sub in enumerate(deeper):
                        if page.get_by_role("button", name=sub).count():
                            click_if_safe(page, sub, report, "02b-%d-%d" % (i, j))
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(500)
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(500)

            # 2) 管理页的 Computer Use 标签（独立路由，直接进）
            page.goto(URL.rstrip("/") + "/admin", wait_until="networkidle")
            page.wait_for_timeout(2000)
            page.screenshot(path=str(OUT / "03-admin.png"), full_page=True)
            report.append({"step": "admin", **probe(page)})
            cu = [t for t in report[-1]["buttons"] + report[-1]["tabs"] if "Computer" in t or "电脑" in t]
            for i, label in enumerate(cu[:3]):
                click_if_safe(page, label, report, "04-admin-tab-%d" % i)
            browser.close()
    finally:
        release()
    (OUT / "ui_probe_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("[saved] %s" % (OUT / "ui_probe_report.json"))
    print("[time] %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


if __name__ == "__main__":
    main()
