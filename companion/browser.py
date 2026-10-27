"""独立无头浏览器；绝不附加用户 Chrome、复制 profile 或采集整屏。"""
import asyncio
import base64
import contextlib
import hashlib
import io
import os
import re
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from playwright.async_api import async_playwright
import aiohttp
from .security import Denied, Paused, fields, integer, origin, require, text

METHODS = ["browser.goto", "browser.snapshot", "browser.act", "browser.scroll",
           "browser.screenshot", "browser.download"]
MAX_DOWNLOAD = 32 * 1024 * 1024
DOWNLOAD_DIR = "Z-OPS下载"
# 值采集前按元数据过滤。不能证明安全的 shadow DOM、登录页面一律暂停。
SENSITIVE = r"password|passwd|secret|token|otp|one.time|verification|credential|验证码|密码|口令"
GATE = r"""() => {
 const secret = /password|passwd|secret|token|otp|one.time|verification|credential|验证码|密码|口令/i;
 const nodes = document.querySelectorAll('*');
 if (nodes.length > 12000) return true;
 for (const el of nodes) {
   if (el.shadowRoot) return true;
   const metadata = ['type','name','id','autocomplete','aria-label','placeholder'].map(a => el.getAttribute(a)||'').join(' ');
   if (secret.test(metadata)) return true;
 }
 return /\b(sign[ -]?in|log[ -]?in|authentication)\b|登录|登入/i.test(document.title)
   || /\/(login|signin|sign-in|oauth|authorize|auth)(\/|$)/i.test(location.pathname);
}"""
SAFE_TEXT = r"""el => {
 const skip = 'script,style,noscript,input,textarea,select,option,iframe,object,embed,[contenteditable]';
 const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
 let out = '', n;
 while ((n = walker.nextNode()) && out.length < 60000) {
   const p = n.parentElement;
   if (!p || p.closest(skip) || p.closest('[hidden],[aria-hidden="true"]')) continue;
   const s = getComputedStyle(p);
   if (s.display === 'none' || s.visibility !== 'visible' || !p.getClientRects().length) continue;
   out += n.textContent.slice(0, 2000) + ' ';
 }
 return out.slice(0, 60000);
}"""


class Browser:
    def __init__(self, config):
        self.config = config
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.task = None
        self.refs = {}
        self.blocked = False
        self.http = None
        self.redirect_target = None
        self.following_redirects = False
        self.pending_routes = 0
        self.download_expected = 0
        # 每次换任务/换上下文递增；旧任务残留的请求不得污染新任务的 blocked 状态。
        self.generation = 0

    async def prepare(self):
        """仅探测本机独立浏览器可启动；没有访问页面、桌面或用户输入。"""
        if self.browser:
            return
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.launch(
                headless=True, chromium_sandbox=True,
                args=["--disable-background-networking", "--disable-component-update", "--disable-sync",
                      "--disable-extensions", "--no-first-run", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
        except BaseException:
            await self.playwright.stop()
            self.playwright = None
            raise

    def allowed(self, url):
        return origin(url) in self.config.origins

    async def route(self, route, request):
        generation, http = self.generation, self.http
        self.pending_routes += 1
        try:
            require(http is not None and generation == self.generation, "browser_task_replaced")
            require(self.pending_routes <= 16, "browser_request_queue_limit")
            allowed = self.allowed(request.url)
            # 单跳有界转发，30x 在产生第二个请求前校验；取消会关闭网络流。
            if not allowed:
                self.blocked = True
                await route.abort("blockedbyclient")
                return
            payload = request.post_data_buffer
            require(payload is None or len(payload) <= 256 * 1024, "browser_request_limit")
            headers = await request.all_headers()
            for key in ("host", "content-length", "connection", "accept-encoding"):
                headers.pop(key, None)
            # 忽略系统代理、netrc、隐式 CookieJar；只转发当前隔离上下文请求。
            async with http.request(request.method, request.url, headers=headers, data=payload,
                                    allow_redirects=False) as response:
                if response.content_length:
                    require(response.content_length <= 8 * 1024 * 1024, "browser_response_limit")
                if 300 <= response.status < 400:
                    from urllib.parse import urljoin
                    target = urljoin(request.url, response.headers.get("location", ""))
                    require(self.allowed(target), "redirect_not_authorized")
                    require(self.following_redirects and request.is_navigation_request()
                            and request.frame == self.page.main_frame, "redirect_requires_navigation")
                    # 不将 30x 交给浏览器自动跟随（路由可能被绕过）。
                    self.redirect_target = target
                    await route.abort("aborted")
                    return
                body = bytearray()
                async for chunk in response.content.iter_chunked(32768):
                    body.extend(chunk)
                    require(len(body) <= 8 * 1024 * 1024, "browser_response_limit")
                reply_headers = {k: v for k, v in response.headers.items()
                                 if k.lower() not in ("content-length", "transfer-encoding", "connection")}
                await route.fulfill(status=response.status, headers=reply_headers, body=bytes(body))
        except Exception:
            # 只有当前任务自己活着的会话里的失败才算被拦截：
            # 已切上下文/任务，或根本没有会话时的迟到请求不得污染新任务的 guard。
            if http is not None and generation == self.generation:
                self.blocked = True
            with contextlib.suppress(Exception):
                await route.abort("blockedbyclient")
        finally:
            self.pending_routes -= 1

    async def new_task(self, task):
        if self.task == task and self.context:
            return
        await self.stop_task()
        await self.prepare()
        self.task = task
        self.http = aiohttp.ClientSession(trust_env=False, auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=15), connector=aiohttp.TCPConnector(limit=8))
        self.context = await self.browser.new_context(
            viewport={"width": 1024, "height": 720}, device_scale_factor=1,
            service_workers="block", accept_downloads=True, permissions=[],
            java_script_enabled=True, ignore_https_errors=False)
        await self.import_login_state()
        await self.context.route("**/*", self.route)
        await self.context.route_web_socket("**/*", lambda socket: socket.close())
        self.page = await self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
        self.page.on("popup", lambda popup: asyncio.create_task(popup.close()))
        # 未被 download 命令显式等待的下载一律取消, 不落盘。
        self.page.on("download", lambda download: asyncio.create_task(
            download.cancel()) if self.download_expected <= 0 else None)

    async def import_login_state(self):
        """领取一次本机扩展交接的登录态并注入任务上下文。

        Cookie 值只在本机内存/浏览器上下文中存在；不进入快照、模型输入或日志。
        交接文件无论成功与否都只消费一次，失败时继续走正常登录墙暂停。
        """
        from .login_state import claim
        try:
            data = await asyncio.to_thread(claim, self.config)
        except Exception:
            return
        if not data:
            return
        cookies = []
        for item in data["cookies"]:
            cookie = {"name": item["name"], "value": item["value"], "url": data["origin"] + "/"}
            if item.get("secure"):
                cookie["secure"] = True
            if item.get("httpOnly"):
                cookie["httpOnly"] = True
            site = item.get("sameSite")
            if site in ("no_restriction", "None"):
                cookie["sameSite"] = "None"
            elif site in ("lax", "Lax"):
                cookie["sameSite"] = "Lax"
            elif site in ("strict", "Strict"):
                cookie["sameSite"] = "Strict"
            if item.get("expirationDate"):
                cookie["expires"] = item["expirationDate"]
            cookies.append(cookie)
        if cookies:
            await self.context.add_cookies(cookies)

    async def guard(self):
        if self.blocked:
            raise Paused("origin_blocked")
        require(self.page is not None and self.allowed(self.page.url), "page_not_authorized")
        if await self.page.evaluate(GATE):
            raise Paused("sensitive_or_login_page")

    async def clear_refs(self):
        refs, self.refs = self.refs, {}
        for handle in refs.values():
            with contextlib.suppress(Exception):
                await handle.dispose()

    async def snapshot(self):
        await self.guard()
        await self.clear_refs()
        items = []
        handles = await self.page.query_selector_all('a,button,input,textarea,select,[role="button"]')
        try:
            for handle in handles[:200]:
                if not await handle.is_visible():
                    continue
                info = await handle.evaluate("el => ({tag:el.tagName.toLowerCase(), type:el.getAttribute('type')||''})")
                if info["type"].lower() in ("password", "hidden", "file"):
                    continue
                label = await handle.evaluate(SAFE_TEXT)
                ref = secrets.token_hex(12)
                self.refs[ref] = handle
                items.append({"ref": ref, "tag": info["tag"], "text": label[:200], "pw": False})
            body = await self.page.locator("body").evaluate(SAFE_TEXT)
            await self.guard()
            url = urlsplit(self.page.url)
            # query/fragment 可能携带 token，任何快照均不返回它们。
            return {"text": body, "items": items,
                    "url": urlunsplit((url.scheme, url.netloc, url.path, "", "")),
                    "title": (await self.page.title())[:200]}
        finally:
            retained = set(self.refs.values())
            for handle in handles:
                if handle not in retained:
                    await handle.dispose()

    async def screenshot(self):
        await self.guard()
        image = await self.page.screenshot(type="jpeg", quality=55, full_page=False,
            animations="disabled", caret="hide", timeout=5000,
            mask=[self.page.locator('input,textarea,select,[contenteditable],iframe,canvas,video,object,embed')])
        await self.guard()
        require(len(image) <= 400 * 1024, "screenshot_too_large")
        from PIL import Image
        with Image.open(io.BytesIO(image)) as picture:
            w, h = picture.size
        require(w <= 1024 and h <= 720, "screenshot_dimensions")
        return {"jpeg": base64.b64encode(image).decode(), "w": w, "h": h}

    def download_dir(self):
        """下载只落在第一个 write_new 授权根下的独立子目录。"""
        roots = [Path(root["path"]) for root in self.config.roots if root["write_new"]]
        require(bool(roots), "no_download_root")
        base = roots[0] / DOWNLOAD_DIR
        if not base.exists():
            base.mkdir(mode=0o700)
        require(base.is_dir() and not base.is_symlink(), "download_directory_denied")
        root, actual = os.path.normcase(os.path.realpath(roots[0])), os.path.normcase(os.path.realpath(base))
        require(actual.startswith(root + os.sep), "download_directory_denied")
        return base

    def download_target(self, base, name):
        """净化文件名并选择未被占用的新名字; 已存在的文件绝不覆盖。"""
        from .files import Files
        clean = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff ]+", "_", str(name or "")).strip(" .")
        clean = clean[:120] or "download"
        stem, suffix = Path(clean).stem, Path(clean).suffix[:16]
        for n in range(50):
            candidate = base / (clean if n == 0 else f"{stem}-{n}{suffix}")
            target = Files(self.config).checked(str(candidate), write=True)
            if not target.exists():
                return target
        raise Denied("download_name_conflict")

    async def download(self, params):
        """点击快照引用触发一次下载, 校验并原子发布为本机授权目录下的新文件。"""
        fields(params, ("ref",), ("name",))
        ref = text(params["ref"], 64)
        require(ref in self.refs, "stale_browser_reference")
        handle = self.refs[ref]
        require(await handle.evaluate("el => el.isConnected && el.ownerDocument === document"),
                "stale_browser_reference")
        base = self.download_dir()
        temp = base / (".zops-dl-" + secrets.token_hex(12))
        self.download_expected += 1
        try:
            async with self.page.expect_download(timeout=20000) as pending:
                await handle.click(timeout=5000, no_wait_after=True)
            item = await pending.value
            target = self.download_target(base, params.get("name") or item.suggested_filename)
            await item.save_as(str(temp))
            info = temp.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_DOWNLOAD, "download_too_large")
            digest = hashlib.sha256()
            with open(temp, "rb") as stream:
                while block := stream.read(262144):
                    digest.update(block)
            try:
                os.link(temp, target)
            except OSError:
                raise Denied("atomic_publish_unavailable") from None
            return {"saved": target.name, "path": str(target), "size": info.st_size,
                    "sha256": digest.hexdigest()}
        finally:
            self.download_expected = max(0, self.download_expected - 1)
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()

    async def execute(self, method, params, task):
        require(method in METHODS, "unsupported_method")
        if method == "browser.goto":
            fields(params, ("url",))
            require(self.allowed(params["url"]), "origin_not_authorized")
            await self.new_task(task)
            await self.clear_refs()
            self.following_redirects = True
            target, visited = params["url"], set()
            try:
                for _ in range(10):
                    require(target not in visited, "redirect_loop")
                    visited.add(target)
                    self.redirect_target = None
                    try:
                        await self.page.goto(target, wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        if self.blocked:
                            raise Paused("origin_blocked") from None
                        if self.redirect_target is None:
                            raise
                    if self.redirect_target is None:
                        break
                    target = self.redirect_target
                else:
                    raise Paused("redirect_limit")
            finally:
                self.following_redirects = False
                self.redirect_target = None
            await self.guard()
            return await self.snapshot()
        require(self.task == task and self.page is not None, "task_browser_missing")
        await self.guard()
        if method == "browser.snapshot":
            fields(params)
            return await self.snapshot()
        if method == "browser.screenshot":
            fields(params)
            return await self.screenshot()
        if method == "browser.scroll":
            fields(params, ("direction", "amount"))
            require(params["direction"] in ("up", "down", "left", "right"))
            amount = integer(params["amount"], 1, 2000)
            dx = amount * ({"left": -1, "right": 1}.get(params["direction"], 0))
            dy = amount * ({"up": -1, "down": 1}.get(params["direction"], 0))
            await self.page.mouse.wheel(dx, dy)
        elif method == "browser.act":
            fields(params, ("ref", "verb"), ("value",))
            require(params["verb"] in ("click", "fill", "select"))
            ref = text(params["ref"], 64)
            require(ref in self.refs, "stale_browser_reference")
            handle = self.refs[ref]
            require(await handle.evaluate("el => el.isConnected && el.ownerDocument === document"), "stale_browser_reference")
            if params["verb"] == "click":
                require("value" not in params)
                await handle.click(timeout=5000, no_wait_after=True)
            elif params["verb"] == "fill":
                await handle.fill(text(params.get("value"), 4096), timeout=5000)
            else:
                await handle.select_option(value=text(params.get("value"), 4096), timeout=5000)
        elif method == "browser.download":
            data = await self.download(params)
            await self.clear_refs()
            await self.guard()
            return data
        await self.clear_refs()
        await self.guard()
        return {"performed": True}

    async def stop_task(self):
        self.refs = {}
        self.download_expected = 0
        # 先递代际：停止前已在途的请求结束后不再改写 blocked。
        self.generation += 1
        context, self.context = self.context, None
        self.page, self.task, self.blocked = None, None, False
        if self.http:
            await self.http.close()
            self.http = None
        if context:
            await context.close()

    async def close(self):
        await self.stop_task()
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
