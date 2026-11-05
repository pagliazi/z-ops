"""
Z-OPS — network_agent: 网络运维 Agent
职责: 连通性检测、端口扫描、配置审计、拓扑发现
集成 OPS-CORE API: 自动从 OPS-CORE 平台检索 DNS 记录、网络相关 CMDB 数据
"""

import asyncio
import json
import logging
import shlex
from typing import List, Dict

from agents.base import BaseAgent, AgentMessage, run_agent
from agents.opscore_api import smart_query, query_api

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

NETWORK_ANALYSIS_PROMPT = """你是 Z-OPS 运维智能体的网络分析模块。

你可以通过 OPS-CORE 平台查询以下网络数据:
- DNS 记录: 域名解析记录查询
- DNS SPF: SPF 记录检查
- DNS DMARC: DMARC 记录检查
- DNS Zone: Zone 传送查询
- CMDB: 网络设备、服务器的基础信息
- 监控: 网络相关的监控数据

分析数据时请:
- 用 🔴(故障) 🟡(异常) 🟢(正常) 标注状态
- 识别连通性问题和网络异常
- 对 DNS 配置问题给出修复建议"""

# network_agent 关注的 API 领域
NET_DOMAINS = ["network_dns", "infra", "monitoring"]


class NetworkAgent(BaseAgent):
    name = "network_agent"
    description = "网络拓扑 / 连通性检测 / 配置审计 + OPS-CORE DNS/网络数据查询"

    async def handle(self, msg: AgentMessage):
        action = msg.action or "chat"
        handlers = {
            "ping_sweep": self._ping_sweep,
            "port_scan": self._port_scan,
            "config_backup": self._config_backup,
            "route_trace": self._route_trace,
            "chat": self._network_chat,
        }
        handler = handlers.get(action, self._network_chat)
        await handler(msg)

    async def _run_cmd(self, cmd: str, timeout: int = 30) -> str:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = stdout.decode("utf-8", errors="replace")
            if stderr:
                err = stderr.decode("utf-8", errors="replace")
                if err.strip():
                    output += f"\n[STDERR] {err}"
            return output.strip()
        except asyncio.TimeoutError:
            return f"[超时] 命令执行超过 {timeout}s"
        except Exception as e:
            return f"[错误] {str(e)}"

    async def _ping_sweep(self, msg: AgentMessage):
        targets_str = msg.params.get("targets") or msg.params.get("args") or msg.text
        if not targets_str:
            await self.reply(msg, error="缺少目标地址")
            return
        targets = [t.strip() for t in targets_str.replace("\n", ",").split(",") if t.strip()]
        count = msg.params.get("count", 3)
        results = {}
        tasks = []
        for target in targets:
            cmd = f"ping -c {count} -W 2 {shlex.quote(target)}"
            tasks.append(self._ping_one(target, cmd, results))
        await asyncio.gather(*tasks)

        # 从 OPS-CORE CMDB 补充目标信息
        opscore_extra = ""
        for target in targets[:3]:
            try:
                cmdb = await query_api("cmdb_info", {"search": target, "page_size": 3})
                if "error" not in cmdb and cmdb.get("count", 0) > 0:
                    opscore_extra += f"\nCMDB 信息 ({target}): {json.dumps(cmdb.get('results', [])[:2], ensure_ascii=False)}"
            except Exception:
                pass

        raw = json.dumps(results, ensure_ascii=False, indent=2)
        prompt = f"以下是 {len(targets)} 个目标的 ping 结果，请分析:\n\n{raw}"
        if opscore_extra:
            prompt += f"\n\n从 OPS-CORE 查到的补充信息:{opscore_extra}"

        analysis = await self.llm_chat(prompt=prompt, system=NETWORK_ANALYSIS_PROMPT)
        await self.reply(msg, result={"text": analysis, "raw_data": results, "targets": targets})
        await self.log_daily(f"Ping 检测: {len(targets)} 个目标")

    async def _ping_one(self, target: str, cmd: str, results: dict):
        output = await self._run_cmd(cmd, timeout=15)
        loss = "unknown"
        for line in output.split("\n"):
            if "packet loss" in line:
                parts = line.split(",")
                for p in parts:
                    if "loss" in p:
                        loss = p.strip()
                        break
        results[target] = {
            "output": output, "loss": loss,
            "reachable": "0% packet loss" in output or "0.0% packet loss" in output
        }

    async def _port_scan(self, msg: AgentMessage):
        target = msg.params.get("target") or msg.params.get("args") or msg.text
        ports = msg.params.get("ports", "22,80,443,3306,6379,8080")
        if not target:
            await self.reply(msg, error="缺少扫描目标")
            return
        targets = [t.strip() for t in target.split(",") if t.strip()]
        results = {}
        for t in targets:
            cmd = f"nmap -sT -p {ports} --open -T4 {shlex.quote(t)} 2>/dev/null || echo 'nmap unavailable, using nc...'"
            output = await self._run_cmd(cmd, timeout=30)
            if "nmap unavailable" in output:
                port_results = {}
                for port in ports.split(","):
                    nc_cmd = f"nc -z -w 2 {shlex.quote(t)} {port.strip()} 2>&1 && echo 'OPEN' || echo 'CLOSED'"
                    nc_out = await self._run_cmd(nc_cmd, timeout=5)
                    port_results[port.strip()] = "open" if "OPEN" in nc_out else "closed"
                results[t] = {"ports": port_results}
            else:
                results[t] = {"nmap_output": output}
        raw = json.dumps(results, ensure_ascii=False, indent=2)
        analysis = await self.llm_chat(prompt=f"以下是端口扫描结果，请分析:\n\n{raw}", system=NETWORK_ANALYSIS_PROMPT)
        await self.reply(msg, result={"text": analysis, "raw_data": results})
        await self.log_daily(f"端口扫描: {', '.join(targets)}")

    async def _config_backup(self, msg: AgentMessage):
        host = msg.params.get("host") or msg.params.get("args", "")
        device_type = msg.params.get("device_type", config.NETMIKO_DEVICE_TYPE)
        user = msg.params.get("user", "admin")
        if not host:
            await self.reply(msg, error="缺少设备地址")
            return
        cmd = "show running-config"
        ssh_cmd = f"ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no {user}@{shlex.quote(host)} {shlex.quote(cmd)}"
        output = await self._run_cmd(ssh_cmd, timeout=20)
        await self.reply(msg, result={
            "text": f"设备 {host} 配置备份:\n\n```\n{output[:5000]}\n```", "host": host, "config": output
        })
        await self.log_daily(f"配置备份: {host}")

    async def _route_trace(self, msg: AgentMessage):
        target = msg.params.get("target") or msg.params.get("args") or msg.text
        if not target:
            await self.reply(msg, error="缺少目标地址")
            return
        targets = [t.strip() for t in target.split(",") if t.strip()]
        results = {}
        for t in targets:
            tr_cmd = f"traceroute -m 20 -w 2 {shlex.quote(t)} 2>&1 || tracepath {shlex.quote(t)} 2>&1"
            tr_output = await self._run_cmd(tr_cmd, timeout=60)
            mtr_cmd = f"mtr -r -c 3 {shlex.quote(t)} 2>/dev/null || echo 'mtr not available'"
            mtr_output = await self._run_cmd(mtr_cmd, timeout=30)
            results[t] = {
                "traceroute": tr_output,
                "mtr": mtr_output if "not available" not in mtr_output else None
            }
        raw = json.dumps(results, ensure_ascii=False, indent=2)
        analysis = await self.llm_chat(
            prompt=f"以下是路由追踪结果，请分析链路质量和潜在问题:\n\n{raw}",
            system=NETWORK_ANALYSIS_PROMPT
        )
        await self.reply(msg, result={"text": analysis, "raw_data": results})
        await self.log_daily(f"路由追踪: {', '.join(targets)}")

    async def _network_chat(self, msg: AgentMessage):
        """网络运维对话 — 自动检索 OPS-CORE DNS/网络数据"""
        prompt = msg.text or msg.params.get("text", "")
        if not prompt:
            await self.reply(msg, error="缺少问题内容")
            return

        # 智能查询 OPS-CORE DNS/网络数据
        raw_data = await smart_query(
            question=prompt, domains=NET_DOMAINS,
            llm_func=self.llm_chat, max_queries=3
        )

        if raw_data:
            result = await self.llm_chat(
                prompt=f"用户问题: {prompt}\n\n从 OPS-CORE 平台查询到的网络/DNS数据:\n{raw_data}",
                system=NETWORK_ANALYSIS_PROMPT
            )
            await self.reply(msg, result={"text": result, "source": "opscore"})
            await self.log_daily(f"网络智能查询: {prompt[:50]}")
        else:
            result = await self.llm_chat(
                prompt=prompt,
                system=(
                    "你是 Z-OPS 运维智能体的网络运维专家。"
                    "擅长网络拓扑分析、连通性诊断、交换机/路由器配置、DNS 解析等。\n"
                    "你可以查询 OPS-CORE 平台的 DNS 记录(查询/SPF/DMARC/Zone)、"
                    "CMDB 网络设备信息、监控数据等。"
                )
            )
            await self.reply(msg, result={"text": result})


if __name__ == "__main__":
    run_agent(NetworkAgent())
