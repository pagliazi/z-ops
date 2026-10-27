"""
Z-OPS — ops_agent: 系统运维 Agent
职责: 主机巡检、资源监控、日志分析、故障诊断
集成 GCOC API: 自动从 GCOC 平台检索监控、告警、CMDB 数据
"""

import asyncio
import re
import json
import logging
import subprocess
import shlex
from typing import Optional

from agents.base import BaseAgent, AgentMessage, run_agent
from agents.gcoc_api import smart_query, query_api

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

ANALYSIS_PROMPT = """你是 Z-OPS 运维智能体的系统分析模块。

你可以通过 GCOC 平台查询以下运维数据:
- CMDB: 服务器/主机基础信息、资产清单
- 监控数据: 主机监控状态、性能指标
- 告警信息: 系统告警、异常事件
- Smart-Ops: 运维事件(严重/未确认)、运维状态汇总、日志告警
- Aria Ops: 容量分析、统计数据、服务器列表

分析数据时请:
- 按重要程度排序，标注告警级别 (🔴严重 🟡警告 🟢正常)
- 识别异常项和潜在风险，给出诊断建议
- 汇总关键数字"""

# ops_agent 关注的 API 领域
OPS_DOMAINS = ["monitoring", "infra", "aria"]


class OpsAgent(BaseAgent):
    name = "ops_agent"
    description = "系统巡检 / 资源监控 / 故障诊断 + GCOC 监控告警查询"

    # 本机只读诊断命令直通 (10-21: "执行 shell 命令 uptime" 此前被 LLM 幻觉"无法执行")
    _LOCAL_CMD_RE = re.compile(r'(?:执行|运行|跑一?下|查看)\s*(?:shell\s*|bash\s*)?命令\s*[`"\'\u201c]?([A-Za-z][A-Za-z0-9\- ./=]*)')
    _SAFE_CMD_PREFIXES = ('uptime', 'df', 'free', 'who', 'w', 'date', 'hostname', 'uname',
                          'id', 'cat /proc/uptime', 'cat /proc/loadavg', 'ps ', 'top -b')

    def _safe_local_cmd(self, cmd: str) -> bool:
        c = (cmd or '').strip()
        if not c or any(ch in c for ch in ';|&$`><\n'):
            return False
        return any(c == p or c.startswith(p + ' ') or c == p.strip() or c.startswith(p)
                   for p in self._SAFE_CMD_PREFIXES)

    async def handle(self, msg: AgentMessage):
        action = msg.action or "host_check"
        # Hermes ParallelExecutor: 显式并行批量执行 (带依赖调度/错误隔离/结果聚合)
        if action == "parallel_check":
            await self._parallel_check(msg)
            return
        # 本机只读诊断命令直通: "执行 shell 命令 uptime" 等 → 真实执行返回输出
        if (msg.text or '') and action in ("host_check", "chat", ""):
            m = self._LOCAL_CMD_RE.search(msg.text)
            if m:
                cmd = m.group(1).strip()
                if self._safe_local_cmd(cmd):
                    await self.send_progress(msg, f"⌨️ 本机执行: {cmd}")
                    try:
                        out = await self._run_local_cmd(cmd, timeout=15)
                        await self.reply(msg, result={"text": f"⌨️ `$ {cmd}`\n\n```\n{out[:3000]}\n```"})
                    except Exception as e:
                        await self.reply(msg, result={"text": f"⌨️ `$ {cmd}` 执行失败: {e}"})
                else:
                    await self.reply(msg, result={"text":
                        f"⚠️ 出于安全仅支持只读诊断命令 (uptime/df/free/who/w/date/hostname/uname/id/ps 等)，"
                        f"已拒绝: `{cmd}`。复合命令/写操作请走 dev_agent 的脚本登记流程。"})
                return
        handlers = {
            "host_check": self._host_check,
            "log_analyze": self._log_analyze,
            "batch_exec": self._batch_exec,
            "health_report": self._health_report,
            "chat": self._ops_chat,
        }
        handler = handlers.get(action, self._ops_chat)
        # P2 收尾 (task-c17): 已发布技能注入 (closed loop 消费端) — 仅 published 版本
        _skill_block, _skill_names = await self.skill_context_for(msg)
        await self._dispatch_with_skills(handler, msg, _skill_block, _skill_names)

    async def _parallel_check(self, msg: AgentMessage):
        """Hermes ParallelExecutor: 多主机并行检查 (带优先级调度/错误隔离/结果聚合)

        与 sidechain 的区别: ParallelExecutor 是通用并行框架 (任务依赖图+智能调度),
        sidechain 是 Z-OPS 的带预算/审计子任务方案 — 两者互补, 此路径展示 Hermes 原生能力。
        params: {"hosts": ["host1", "host2", ...], "command": "检查命令"}
        """
        hosts = (msg.params or {}).get("hosts") or []
        command = (msg.params or {}).get("command") or "uptime"
        if not hosts:
            await self.reply(msg, error="缺少 hosts 参数")
            return

        from core.delegation.parallel_executor import ParallelExecutor, ParallelTask

        executor = ParallelExecutor(max_workers=min(len(hosts), 10))
        tasks = []
        for host in hosts[:20]:   # 上限 20 台
            task = ParallelTask(
                task_id=f"check-{host}",
                task_func=self._run_local_cmd,
                args=(command,),
                timeout=30,
            )
            tasks.append(task)

        results = await executor.execute_parallel(tasks)

        # 聚合结果 (execute_parallel 返回 Dict[task_id, TaskResult])
        lines = [f"**ParallelExecutor 并行检查报告** ({len(hosts)} 台, `{command[:30]}`)\n"]
        ok_count = 0
        for tid, r in results.items():
            host = tid.replace("check-", "")
            if r.success:
                ok_count += 1
                lines.append(f"✅ **{host}**: {str(r.result)[:80] if r.result else 'OK'}")
            else:
                lines.append(f"❌ **{host}**: {r.error[:80]}")

        lines.append(f"\n**汇总**: {ok_count}/{len(results)} 成功")
        await self.reply(msg, result={"text": "\n".join(lines)})

    async def _dispatch_with_skills(self, handler, msg, skill_block: str, skill_names: list):
        """执行 handler; 自然语言分析类 handler (_ops_chat/_log_analyze) 注入技能块,
        结束后回写使用结果 (verified=False — 口径不虚高)。"""
        try:
            if skill_block and handler.__name__ in ("_ops_chat", "_log_analyze", "_health_report"):
                msg.params["_skill_block"] = skill_block
            await handler(msg)
            await self.record_skill_outcomes(skill_names, success=True)
        except Exception:
            await self.record_skill_outcomes(skill_names, success=False)
            raise

    async def _run_local_cmd(self, cmd: str, timeout: int = None) -> str:
        timeout = timeout or config.SHELL_TIMEOUT
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = stdout.decode("utf-8", errors="replace")
            if stderr:
                output += "\n[STDERR] " + stderr.decode("utf-8", errors="replace")
            return output.strip()
        except asyncio.TimeoutError:
            return f"[超时] 命令执行超过 {timeout}s: {cmd}"
        except Exception as e:
            return f"[错误] {str(e)}"

    async def _ssh_exec(self, host: str, cmd: str, user: str = "root",
                        port: int = 22, timeout: int = None) -> str:
        timeout = timeout or config.SSH_TIMEOUT
        try:
            ssh_cmd = (
                f"ssh -o ConnectTimeout={timeout} -o StrictHostKeyChecking=no "
                f"-p {port} {user}@{host} {shlex.quote(cmd)}"
            )
            return await self._run_local_cmd(ssh_cmd, timeout=timeout + 5)
        except Exception as e:
            return f"[SSH 错误] {host}: {str(e)}"

    async def _host_check(self, msg: AgentMessage):
        """主机巡检: 采集 CPU/内存/磁盘/进程

        v4.0 (Phase 2.4): 多主机走并行子任务 sidechain —
        完整采集结果永久保留在 workspaces/ops_agent/sidechains/,
        父上下文只收摘要 (summary-only return, 控制上下文膨胀)
        """
        target = msg.params.get("host") or msg.params.get("args", "localhost")
        targets = [t.strip() for t in target.split(",") if t.strip()]
        check_cmds = {
            "hostname": "hostname", "uptime": "uptime",
            "cpu": "top -bn1 | head -5", "memory": "free -h",
            "disk": "df -h", "load": "cat /proc/loadavg",
            "top_procs": "ps aux --sort=-%cpu | head -10",
        }

        async def _collect_host(host: str) -> dict:
            """单主机采集 (子任务执行体, 供串行/并行两路复用)"""
            host_data = {}
            for name, cmd in check_cmds.items():
                if host in ("localhost", "127.0.0.1", "local"):
                    host_data[name] = await self._run_local_cmd(cmd)
                else:
                    host_data[name] = await self._ssh_exec(host, cmd)
            # GCOC CMDB 补充信息
            if host not in ("localhost", "127.0.0.1", "local"):
                try:
                    cmdb_data = await query_api("cmdb_info", {"search": host})
                    if "error" not in cmdb_data and cmdb_data.get("count", 0) > 0:
                        host_data["cmdb"] = cmdb_data.get("results", [])[:3]
                except Exception:
                    pass
            return host_data

        # ── 多主机: 并行子任务 (sidechain transcript + summary-only) ──
        if len(targets) >= 2:
            from core.delegation.sidechain import run_sidechain_tasks, format_summaries
            items = [
                {"task_id": f"inspect-{i}-{h}", "description": f"巡检主机 {h}",
                 "params": {"host": h}}
                for i, h in enumerate(targets)
            ]

            async def _item_exec(item: dict):
                return await _collect_host(item["params"]["host"])

            sc_results = await run_sidechain_tasks(
                "ops_agent", items, exec_fn=_item_exec, username=msg.username)
            summary_block = format_summaries(sc_results)
            analysis = await self.llm_chat(
                prompt=(f"以下是 {len(targets)} 台主机的巡检摘要 "
                        f"(完整原始数据已存 sidechain 审计文件, 需要时按 task_id 取回):\n\n{summary_block}"),
                system=ANALYSIS_PROMPT)
            await self.reply(msg, result={
                "text": analysis,
                "sidechain_tasks": [r["task_id"] for r in sc_results],
            })
            await self.log_daily(f"主机巡检(并行sidechain): {', '.join(targets)}")
            await self.notify(f"主机巡检完成: {', '.join(targets)}\n\n{analysis[:500]}", "ops")
            return

        # ── 单主机: 原串行路径 (行为不变) ──
        results = {}
        for host in targets:
            results[host] = await _collect_host(host)

        raw_data = json.dumps(results, ensure_ascii=False, indent=2)
        prompt = f"以下是主机巡检数据，请分析:\n\n{raw_data}"

        analysis = await self.llm_chat(prompt=prompt, system=ANALYSIS_PROMPT)
        await self.reply(msg, result={"text": analysis, "raw_data": results})
        await self.log_daily(f"主机巡检: {', '.join(targets)}")
        await self.notify(f"主机巡检完成: {', '.join(targets)}\n\n{analysis[:500]}", "ops")

    async def _log_analyze(self, msg: AgentMessage):
        log_path = msg.params.get("path", "/var/log/syslog")
        host = msg.params.get("host", "localhost")
        lines = msg.params.get("lines", 100)
        keyword = msg.params.get("keyword", "")
        cmd = f"tail -n {lines} {shlex.quote(log_path)}"
        if keyword:
            cmd += f" | grep -i {shlex.quote(keyword)}"
        if host in ("localhost", "127.0.0.1", "local"):
            log_content = await self._run_local_cmd(cmd)
        else:
            log_content = await self._ssh_exec(host, cmd)
        if not log_content or "[错误]" in log_content:
            await self.reply(msg, result={"text": f"日志获取失败: {log_content}"})
            return
        analysis = await self.llm_chat(
            prompt=f"分析以下日志内容，提取关键事件和异常:\n\n日志路径: {log_path}\n主机: {host}\n\n{log_content}",
            system=ANALYSIS_PROMPT
        )
        await self.reply(msg, result={"text": analysis, "log_path": log_path, "host": host})
        await self.log_daily(f"日志分析: {host}:{log_path}")

    async def _batch_exec(self, msg: AgentMessage):
        cmd = msg.params.get("cmd") or msg.text
        hosts_str = msg.params.get("hosts") or msg.params.get("args", "localhost")
        hosts = [h.strip() for h in hosts_str.split(",") if h.strip()]
        if not cmd:
            await self.reply(msg, error="缺少要执行的命令")
            return
        results = {}
        for host in hosts:
            if host in ("localhost", "127.0.0.1", "local"):
                results[host] = await self._run_local_cmd(cmd)
            else:
                results[host] = await self._ssh_exec(host, cmd)
        raw = json.dumps(results, ensure_ascii=False, indent=2)
        summary = await self.llm_chat(
            prompt=f"以下是在 {len(hosts)} 台主机上执行 `{cmd}` 的结果，请汇总分析:\n\n{raw}",
            system=ANALYSIS_PROMPT
        )
        await self.reply(msg, result={"text": summary, "raw_data": results, "hosts": hosts, "command": cmd})
        await self.log_daily(f"批量执行: {cmd} @ {len(hosts)} 台主机")

    async def _health_report(self, msg: AgentMessage):
        hosts_str = msg.params.get("hosts") or msg.params.get("args", "localhost")
        hosts = [h.strip() for h in hosts_str.split(",") if h.strip()]
        check_cmd = (
            "echo '=== HOSTNAME ===' && hostname && "
            "echo '=== UPTIME ===' && uptime && "
            "echo '=== MEMORY ===' && free -h && "
            "echo '=== DISK ===' && df -h && "
            "echo '=== LOAD ===' && cat /proc/loadavg && "
            "echo '=== TOP CPU ===' && ps aux --sort=-%cpu | head -5"
        )

        async def _collect_health(host: str) -> str:
            if host in ("localhost", "127.0.0.1", "local"):
                return await self._run_local_cmd(check_cmd)
            return await self._ssh_exec(host, check_cmd)

        # v4.0 (Phase 2.4): 多主机健康报告走并行子任务 sidechain (summary-only)
        if len(hosts) >= 2:
            from core.delegation.sidechain import run_sidechain_tasks, format_summaries
            items = [
                {"task_id": f"health-{i}-{h}", "description": f"健康采集 {h}",
                 "params": {"host": h}}
                for i, h in enumerate(hosts)
            ]

            async def _item_exec(item: dict):
                return await _collect_health(item["params"]["host"])

            sc_results = await run_sidechain_tasks(
                "ops_agent", items, exec_fn=_item_exec, username=msg.username)
            raw = format_summaries(sc_results)
        else:
            all_data = {}
            for host in hosts:
                all_data[host] = await _collect_health(host)
            raw = json.dumps(all_data, ensure_ascii=False, indent=2)

        # 同时从 GCOC 获取监控和告警数据
        gcoc_extra = ""
        try:
            monitor_data = await query_api("monitor_info", {"page_size": 10})
            if "error" not in monitor_data:
                gcoc_extra += f"\nGCOC 监控数据: {json.dumps(monitor_data, ensure_ascii=False)[:2000]}"
        except Exception:
            pass
        try:
            alarm_data = await query_api("alarm_info", {"page_size": 10})
            if "error" not in alarm_data:
                gcoc_extra += f"\nGCOC 告警数据: {json.dumps(alarm_data, ensure_ascii=False)[:2000]}"
        except Exception:
            pass

        prompt = f"根据以下 {len(hosts)} 台主机的系统数据，生成运维健康报告:\n\n{raw}"
        if gcoc_extra:
            prompt += f"\n\n从 GCOC 平台查到的监控/告警数据:{gcoc_extra}"

        report = await self.llm_chat(
            prompt=prompt,
            system=(
                "你是 Z-OPS 运维智能体。请生成结构化的运维健康报告，包含:\n"
                "1. 总体摘要\n2. 各主机状态 (用 🔴🟡🟢 标注)\n"
                "3. 风险项和建议\n4. 资源使用趋势"
            )
        )
        await self.reply(msg, result={"text": report, "hosts": hosts})
        await self.log_daily(f"健康报告: {len(hosts)} 台主机")
        await self.notify(f"健康报告已生成 ({len(hosts)} 台主机)\n\n{report[:500]}", "report")

    async def _ops_chat(self, msg: AgentMessage):
        """运维对话 — 自动检索 GCOC 监控/告警/CMDB 数据"""
        prompt = msg.text or msg.params.get("text", "")
        if not prompt:
            await self.reply(msg, error="缺少问题内容")
            return

        # P2 收尾: 注入的已发布技能块 (由 handle 统一放入 params)
        skill_block = (msg.params or {}).pop("_skill_block", "") or ""

        # 智能查询 GCOC 监控/告警数据
        raw_data = await smart_query(
            question=prompt, domains=OPS_DOMAINS,
            llm_func=self.llm_chat, max_queries=3
        )

        if raw_data:
            result = await self.llm_chat(
                prompt=f"{skill_block}用户问题: {prompt}\n\n从 GCOC 平台查询到的运维数据:\n{raw_data}",
                system=ANALYSIS_PROMPT
            )
            await self.reply(msg, result={"text": result, "source": "gcoc"})
            await self.log_daily(f"运维智能查询: {prompt[:50]}")
        else:
            result = await self.llm_chat(
                prompt=prompt,
                system=(
                    "你是 Z-OPS 运维智能体的系统运维专家。"
                    "回答系统运维相关问题，提供诊断建议和操作步骤。\n"
                    "你可以查询 GCOC 平台的监控数据、告警信息、CMDB主机信息、"
                    "Smart-Ops运维事件、Aria Ops容量分析等数据。"
                )
            )
            await self.reply(msg, result={"text": result})


if __name__ == "__main__":
    run_agent(OpsAgent())
