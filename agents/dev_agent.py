"""
Z-OPS — dev_agent: 开发脚本 Agent
职责: 脚本开发、SSH 批量执行、配置变更、Ansible 集成
"""

import asyncio
import json
import logging
import shlex
import tempfile
import os
import time
from pathlib import Path as FilePath

from agents.base import BaseAgent, AgentMessage, run_agent

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

SCRIPT_GEN_PROMPT = """你是 Z-OPS 运维智能体的脚本开发模块。

你的能力:
1. 编写 Shell / Python / Ansible 脚本
2. 生成运维自动化工具
3. 代码审查和优化建议

要求:
- 输出可直接执行的完整脚本
- 添加注释说明关键步骤
- 考虑错误处理和安全性
- 脚本开头加上 Z-OPS 标识注释"""


class DevAgent(BaseAgent):
    name = "dev_agent"
    description = "脚本开发 / SSH 批量执行 / 配置变更"

    async def handle(self, msg: AgentMessage):
        action = msg.action or "chat"
        handlers = {
            "script_gen":  self._script_gen,
            "script_exec": self._script_exec,
            "ansible_run": self._ansible_run,
            "chat":        self._dev_chat,
        }
        handler = handlers.get(action, self._dev_chat)
        await handler(msg)

    async def _script_gen(self, msg: AgentMessage):
        """LLM 生成脚本"""
        requirement = msg.text or msg.params.get("requirement", "")
        script_type = msg.params.get("type", "shell")

        if not requirement:
            await self.reply(msg, error="缺少脚本需求描述")
            return

        prompt = (
            f"请根据以下需求生成{script_type}脚本:\n\n"
            f"需求: {requirement}\n\n"
            f"脚本类型: {script_type}\n"
            f"请输出完整可执行的脚本代码，用 ```{script_type} 包裹。"
        )

        result = await self.llm_chat(prompt=prompt, system=SCRIPT_GEN_PROMPT)

        await self.reply(msg, result={
            "text": result,
            "type": script_type,
            "requirement": requirement
        })
        await self.log_daily(f"脚本生成: {requirement[:50]} ({script_type})")

    async def _script_exec(self, msg: AgentMessage):
        """执行脚本 — P0/P2 收尾 (task-c17): 仅限登记脚本 + 受限环境

        旧行为 (任意脚本内容直接跑 / root@host ssh 直跑) 已按 SPEC 移除:
          - script_name: 必须在 scripts/registered/registry.json 登记;
          - params: 仅登记参数名 + 安全标量 (防注入);
          - host: 仅登记 host;
          - 环境: docker (配置镜像) > 本地 prlimit 沙箱 > 暂停 (fail-closed)。
        _script_gen 生成的脚本落 scripts/proposed/ 待管理员审核登记 (候选制)。
        """
        script_name = (msg.params.get("script_name") or
                       msg.params.get("script") or "").strip()
        params = msg.params.get("params") or {}
        host = (msg.params.get("host") or "localhost").strip()

        # 兼容旧调用形状: 传了原始脚本内容 → 拒绝并指引登记
        if script_name and ("\n" in script_name or len(script_name) > 120
                            or not script_name.replace("_", "").replace("-", "").isalnum()):
            await self.reply(msg, result={"text": (
                "⛔ 任意脚本内容执行已被安全策略禁止 (P0)。\n"
                f"生成的脚本已保存至 `scripts/proposed/` 待管理员审核登记; "
                "登记后请用 `script_exec(script_name=...)` 调用。")})
            try:
                import pathlib
                prop = pathlib.Path("scripts/proposed")
                prop.mkdir(parents=True, exist_ok=True)
                fname = f"proposal_{int(time.time())}.{'py' if msg.params.get('type')=='python' else 'sh'}"
                (prop / fname).write_text(script_name, encoding="utf-8")
            except Exception:
                pass
            return

        from core.script_exec import run_registered, ScriptRegistryError
        try:
            result = await run_registered(script_name, params, host)
        except ScriptRegistryError as e:
            await self.reply(msg, result={"text": f"⛔ 脚本执行被拒 ({e.code}): {e.detail}"})
            await self.log_daily(f"script_exec 拒绝: {e.code} {script_name}", level="warn", category="dev")
            return
        status = "✅ 成功" if result["ok"] else ("⏱ 超时" if result["timed_out"] else "❌ 失败")
        await self.reply(msg, result={
            "text": (f"脚本 `{script_name}` {status} [{result['env']}, "
                     f"{result['duration_s']}s, exit={result['exit_code']}]\n"
                     f"```\n{result['output'][:3000]}\n```"),
            "output": result["output"], "exit_code": result["exit_code"],
            "env": result["env"], "timed_out": result["timed_out"],
        })
        await self.log_daily(
            f"script_exec {script_name}@{host} [{'ok' if result['ok'] else 'fail'}] "
            f"env={result['env']} dur={result['duration_s']}s",
            level="info" if result["ok"] else "warn", category="dev")

    async def _ansible_run(self, msg: AgentMessage):
        """运行 Ansible Playbook"""
        playbook = msg.params.get("playbook", "")
        inventory = msg.params.get("inventory", "")

        if not playbook:
            # 如果提供了自然语言需求，先生成 Playbook
            requirement = msg.text or msg.params.get("requirement", "")
            if requirement:
                prompt = (
                    f"请根据以下需求生成 Ansible Playbook:\n\n{requirement}\n\n"
                    "输出完整的 YAML 格式 Playbook。"
                )
                playbook_content = await self.llm_chat(
                    prompt=prompt, system=SCRIPT_GEN_PROMPT
                )
                await self.reply(msg, result={
                    "text": f"生成的 Ansible Playbook:\n\n{playbook_content}",
                    "type": "ansible_playbook"
                })
                return
            else:
                await self.reply(msg, error="缺少 Playbook 内容或需求描述")
                return

        try:
            # 保存 Playbook 到临时文件
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yml", delete=False, prefix="zops_ansible_"
            ) as f:
                f.write(playbook)
                playbook_path = f.name

            cmd = f"ansible-playbook {playbook_path}"
            if inventory:
                cmd += f" -i {shlex.quote(inventory)}"

            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )
            output = stdout.decode("utf-8", errors="replace")
            if stderr:
                output += "\n" + stderr.decode("utf-8", errors="replace")

            os.unlink(playbook_path)

            await self.reply(msg, result={
                "text": f"Ansible 执行结果:\n```\n{output}\n```",
                "output": output,
                "exit_code": proc.returncode
            })
            await self.log_daily(f"Ansible Playbook 执行 (exit={proc.returncode})")
        except Exception as e:
            await self.reply(msg, error=f"Ansible 执行失败: {str(e)}")

    async def _dev_chat(self, msg: AgentMessage):
        """开发对话"""
        prompt = msg.text or msg.params.get("text", "")
        if not prompt:
            await self.reply(msg, error="缺少问题内容")
            return

        result = await self.llm_chat(
            prompt=prompt,
            system=(
                "你是 Z-OPS 运维智能体的开发专家。"
                "擅长 Shell、Python、Ansible 脚本开发，"
                "以及运维自动化工具的设计和实现。"
            )
        )
        await self.reply(msg, result={"text": result})


if __name__ == "__main__":
    run_agent(DevAgent())
