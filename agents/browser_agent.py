"""
Z-OPS — browser_agent: 浏览器自动化 Agent
职责: Web 管理页面自动化操作、截图取证
"""

import asyncio
import json
import logging
import base64
import os
import re
import time
import hashlib
from pathlib import Path as FilePath
from datetime import datetime
from typing import List, Dict, Optional

from agents.base import BaseAgent, AgentMessage, run_agent

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

SCREENSHOT_DIR = config.BASE_DIR / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

class BrowserAgent(BaseAgent):
    name = "browser_agent"
    description = "Web 管理页面自动化操作"

    def __init__(self):
        super().__init__()
        self._browser = None
        self._playwright = None

    async def _ensure_browser(self):
        """确保浏览器已启动"""
        if self._browser is None:
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                logger.info("[browser_agent] Chromium 已启动")
            except ImportError:
                raise RuntimeError(
                    "playwright 未安装，请运行: pip install playwright && playwright install chromium"
                )
            except Exception as e:
                raise RuntimeError(f"浏览器启动失败: {e}")
        return self._browser

    async def handle(self, msg: AgentMessage):
        action = msg.action or "chat"
        handlers = {
            "web_screenshot": self._screenshot,
            "web_action":     self._web_action,
            "chat":           self._browser_chat,
        }
        handler = handlers.get(action, self._browser_chat)
        await handler(msg)

    async def _screenshot(self, msg: AgentMessage):
        """网页截图"""
        url = msg.params.get("url") or msg.text
        if not url:
            await self.reply(msg, error="缺少目标 URL")
            return

        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        try:
            browser = await self._ensure_browser()
            page = await browser.new_page()

            # 设置视口
            await page.set_viewport_size({"width": 1920, "height": 1080})

            # 可选: 登录认证
            username = msg.params.get("username")
            password = msg.params.get("password")

            await page.goto(url, timeout=30000, wait_until="networkidle")

            # 如果需要登录
            if username and password:
                # 尝试自动填写常见登录表单
                try:
                    await page.fill('input[type="text"], input[name="username"], input[name="user"]',
                                    username, timeout=5000)
                    await page.fill('input[type="password"]', password, timeout=5000)
                    await page.click('button[type="submit"], input[type="submit"]', timeout=5000)
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    logger.warning("自动登录失败，继续截图")

            # 截图
            timestamp = int(time.time())
            filename = f"screenshot_{timestamp}.png"
            filepath = SCREENSHOT_DIR / filename
            await page.screenshot(path=str(filepath), full_page=True)

            # 获取页面标题
            title = await page.title()

            # 读取为 base64
            with open(filepath, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()

            await page.close()

            await self.reply(msg, result={
                "text": f"截图完成: {url}\n页面标题: {title}\n文件: {filepath}",
                "url": url,
                "title": title,
                "file": str(filepath),
                "image_base64": img_b64[:100] + "..."  # 缩略信息
            })
            await self.log_daily(f"网页截图: {url}")

        except Exception as e:
            await self.reply(msg, error=f"截图失败: {str(e)}")

    async def _web_action(self, msg: AgentMessage):
        """Web 自动化操作"""
        url = msg.params.get("url", "")
        steps = msg.params.get("steps", [])
        description = msg.text or msg.params.get("description", "")

        if not url:
            await self.reply(msg, error="缺少目标 URL")
            return

        if not steps and description:
            # LLM 生成操作步骤
            prompt = (
                f"将以下 Web 操作描述转换为 Playwright 操作步骤 JSON:\n\n"
                f"URL: {url}\n描述: {description}\n\n"
                f"返回 JSON 数组，每步格式:\n"
                f'{{"action": "click|fill|wait|screenshot", '
                f'"selector": "CSS 选择器", "value": "填充值", "timeout": 5000}}'
            )
            steps_text = await self.llm_chat(prompt=prompt, temperature=0.1)
            try:
                if "```" in steps_text:
                    lines = steps_text.split("\n")
                    json_lines = []
                    in_json = False
                    for line in lines:
                        if line.startswith("```"):
                            in_json = not in_json
                            continue
                        if in_json:
                            json_lines.append(line)
                    steps_text = "\n".join(json_lines)
                steps = json.loads(steps_text)
            except json.JSONDecodeError:
                await self.reply(msg, result={
                    "text": f"操作步骤解析失败，LLM 建议:\n{steps_text}"
                })
                return

        try:
            browser = await self._ensure_browser()
            page = await browser.new_page()
            await page.set_viewport_size({"width": 1920, "height": 1080})
            await page.goto(url, timeout=30000, wait_until="networkidle")

            results = []
            for i, step in enumerate(steps):
                action = step.get("action", "")
                selector = step.get("selector", "")
                value = step.get("value", "")
                timeout = step.get("timeout", 5000)

                try:
                    if action == "click":
                        await page.click(selector, timeout=timeout)
                        results.append(f"步骤 {i+1}: 点击 {selector} ✓")
                    elif action == "fill":
                        await page.fill(selector, value, timeout=timeout)
                        results.append(f"步骤 {i+1}: 填写 {selector} ✓")
                    elif action == "wait":
                        await page.wait_for_selector(selector, timeout=timeout)
                        results.append(f"步骤 {i+1}: 等待 {selector} ✓")
                    elif action == "screenshot":
                        ts = int(time.time())
                        path = SCREENSHOT_DIR / f"action_{ts}_{i}.png"
                        await page.screenshot(path=str(path))
                        results.append(f"步骤 {i+1}: 截图保存 {path} ✓")
                    else:
                        results.append(f"步骤 {i+1}: 未知操作 {action}")
                except Exception as e:
                    results.append(f"步骤 {i+1}: 失败 - {str(e)}")

            await page.close()

            await self.reply(msg, result={
                "text": "Web 自动化执行结果:\n" + "\n".join(results),
                "steps_results": results
            })
            await self.log_daily(f"Web 自动化: {url} ({len(steps)} 步)")

        except Exception as e:
            await self.reply(msg, error=f"Web 操作失败: {str(e)}")

    async def _browser_chat(self, msg: AgentMessage):
        """浏览器相关对话"""
        prompt = msg.text or msg.params.get("text", "")
        if not prompt:
            await self.reply(msg, error="缺少问题内容")
            return
        result = await self.llm_chat(
            prompt=prompt,
            system="你是 Z-OPS 运维智能体的 Web 自动化专家。擅长 Playwright、浏览器自动化、Web 页面操作。"
        )
        await self.reply(msg, result={"text": result})

    async def _cleanup(self):
        """清理浏览器资源"""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        await super()._cleanup()


if __name__ == "__main__":
    run_agent(BrowserAgent())
