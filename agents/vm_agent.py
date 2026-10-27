"""
Z-OPS — vm_agent: 虚拟化管理 Agent
职责: VMware/KVM/PVE 虚拟机生命周期管理、资源查询、快照、迁移
集成 GCOC API: 自动从 GCOC 平台检索虚拟机数据
"""

import asyncio
import json
import logging
import shlex
from typing import Optional, Dict

from agents.base import BaseAgent, AgentMessage, run_agent
from agents.gcoc_api import smart_query, query_api, query_vms_on_esxi_host, query_vms_on_esxi_host

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

logger = logging.getLogger("zops")

VM_ANALYSIS_PROMPT = """你是 Z-OPS 运维智能体的虚拟化管理模块。

你可以通过 GCOC 平台查询虚拟机数据:
- Smart-Ops VM列表/汇总: 查看所有虚拟机及状态
- Aria Ops 虚拟机: VMware 层面的虚拟机清单
- 快照管理: 虚拟机快照列表和问题检查
- VCD 资源: vCloud Director 虚拟机和vApp
- CMDB: 主机/服务器基础信息
- 备份对象: Veeam 备份的虚拟机列表

分析数据时请:
- 整理 VM 状态，标注异常
- 用 🔴(故障) 🟡(资源紧张) 🟢(正常) 标注
- 汇总关键数字 (总数/在线/离线/异常)
- 用表格呈现结构化数据"""

# vm_agent 关注的 API 领域
VM_DOMAINS = ["vm", "snapshot", "vcd", "backup"]


class VMAgent(BaseAgent):
    name = "vm_agent"
    description = "VMware/KVM/PVE 虚拟化管理 + GCOC 虚拟机数据查询"

    async def handle(self, msg: AgentMessage):
        action = msg.action or "vm_list"
        handlers = {
            "vm_list":     self._vm_list,
            "vm_power":    self._vm_power,
            "vm_snapshot": self._vm_snapshot,
            "vm_migrate":  self._vm_migrate,
            "host_status": self._host_status,
            "chat":        self._vm_chat,
        }
        handler = handlers.get(action, self._vm_chat)
        await handler(msg)

    def _detect_platform(self, params: dict) -> str:
        platform = params.get("platform", "").lower()
        if platform:
            return platform
        if config.PVE_HOST:
            return "pve"
        if config.VCENTER_HOST:
            return "vmware"
        if config.KVM_HOSTS:
            return "kvm"
        return "kvm"

    # ─── VMware (pyVmomi) ───

    async def _vmware_connect(self):
        try:
            from pyVim.connect import SmartConnect
            import ssl
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            si = SmartConnect(
                host=config.VCENTER_HOST, user=config.VCENTER_USER,
                pwd=config.VCENTER_PASS, sslContext=ctx
            )
            return si
        except ImportError:
            raise RuntimeError("pyVmomi 未安装")
        except Exception as e:
            raise RuntimeError(f"vCenter 连接失败: {e}")

    async def _vmware_list_vms(self) -> list:
        si = await self._vmware_connect()
        try:
            from pyVim.connect import Disconnect
            from pyVmomi import vim
            content = si.RetrieveContent()
            container = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.VirtualMachine], True
            )
            vms = []
            for vm in container.view:
                vms.append({
                    "name": vm.name, "power": str(vm.runtime.powerState),
                    "cpu": vm.config.hardware.numCPU if vm.config else "N/A",
                    "memory_mb": vm.config.hardware.memoryMB if vm.config else "N/A",
                    "guest_os": vm.config.guestFullName if vm.config else "N/A",
                    "ip": vm.guest.ipAddress if vm.guest else "N/A",
                })
            container.Destroy()
            return vms
        finally:
            Disconnect(si)

    # ─── KVM (libvirt / virsh) ───

    async def _kvm_exec(self, cmd: str) -> str:
        proc = await asyncio.create_subprocess_shell(
            f"virsh {cmd}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        output = stdout.decode("utf-8", errors="replace")
        if stderr:
            err = stderr.decode("utf-8", errors="replace")
            if err.strip() and "error" in err.lower():
                output += f"\n[ERROR] {err}"
        return output.strip()

    # ─── PVE (proxmoxer / API) ───

    async def _pve_api(self, method: str, path: str, data: dict = None) -> dict:
        try:
            import httpx
            base_url = f"https://{config.PVE_HOST}:8006/api2/json"
            headers = {"Authorization": f"PVEAPIToken={config.PVE_TOKEN_ID}={config.PVE_TOKEN_SECRET}"}
            async with httpx.AsyncClient(verify=False, timeout=15) as client:
                if method == "GET":
                    resp = await client.get(f"{base_url}{path}", headers=headers)
                elif method == "POST":
                    resp = await client.post(f"{base_url}{path}", headers=headers, json=data)
                else:
                    resp = await client.get(f"{base_url}{path}", headers=headers)
                resp.raise_for_status()
                return resp.json().get("data", {})
        except ImportError:
            raise RuntimeError("httpx 未安装")
        except Exception as e:
            raise RuntimeError(f"PVE API 调用失败: {e}")

    # ═══ GCOC 虚拟机数据查询 ═══

    async def _filter_vms_by_scope(self, raw_json: str, username: str) -> str:
        """VM 查询站点范围裁剪: scope.sites 非空 → 仅保留命中站点关键词的 VM 记录

        关键词 = config.VM_SITE_KEYWORDS (站点名→vCenter/命名惯例代码);
        匹配字段为记录全文 (name/vcenter_server/datacenter/cluster 等), 大小写不敏感。
        过滤失败 fail-open 返回原数据 (裁剪是数据面收紧, 不能阻断查询)。
        """
        if not username:
            return raw_json
        try:
            from core.bizperms import effective_perms
            eff = await effective_perms(username)
            sites = (eff.get("scopes") or {}).get("sites") or []
            if not sites:
                return raw_json
            kws = []
            for site in sites:
                key = next((k for k in config.VM_SITE_KEYWORDS if k == site or site.startswith(k)), site)
                kws.extend(config.VM_SITE_KEYWORDS.get(key, [site]))
            data = json.loads(raw_json)
            if not isinstance(data, dict):
                return raw_json
            # 归一数据形态: 多源 {source: {results: []}} 或顶层单列表 {results: []}
            sources = data if data.get("results") is None and all(isinstance(v, dict) for v in data.values()) else {"_top": data}
            total_before = total_after = 0
            for src, payload in sources.items():
                if not isinstance(payload, dict):
                    continue
                results = payload.get("results")
                if isinstance(results, list):
                    total_before += len(results)
                    kept = []
                    for vm in results:
                        blob = json.dumps(vm, ensure_ascii=False).lower() if isinstance(vm, dict) else ""
                        if any(k.lower() in blob for k in kws):
                            kept.append(vm)
                    payload["results"] = kept
                    total_after += len(kept)
            logger.info(f"[vm_agent] scope 裁剪 ({username} 站点范围 {sites}): {total_before} → {total_after} 条")
            if total_before == 0:
                return raw_json
            note = f"\n[范围约束] 已按你的站点范围 ({'、'.join(sites)}) 裁剪, 仅显示范围内资源。"
            return json.dumps(data, ensure_ascii=False, indent=2)[:7800] + note
        except Exception as e:
            logger.warning(f"[vm_agent] scope 裁剪失败, fail-open: {e}")
            return raw_json

    async def _query_gcoc_vms(self, search: str = "") -> str:
        """从 GCOC 平台查询虚拟机数据 (多源汇总)
        支持站点代码扩展搜索: 'ESXI-02' → 查找 YUD3102 设备上的所有 VM"""
        all_data = {}
        params = {"page_size": 50}
        if search:
            params["search"] = search
            logger.info(f"[vm_agent] GCOC VM 查询: search='{search}'")

        # 查询多个数据源
        endpoints = [
            "smartops_vms",       # Smart-Ops VM 列表
            "snapshot_vms",       # 快照 VM (vCenter 真实数据)
            "vcd_resources",      # VCD 资源
            "aria_ops_vms",       # Aria Ops VM
        ]
        
        for key in endpoints:
            try:
                ep_params = params.copy()
                # vcd_resources 不支持 search 参数
                if key == "vcd_resources" and search:
                    continue
                data = await query_api(key, ep_params)
                if "error" not in data:
                    # 检查是否有实际结果
                    results = data.get("results", [])
                    if results:
                        all_data[key] = data
                        logger.info(f"[vm_agent] {key}: 找到 {len(results)} 条结果")
                    else:
                        logger.info(f"[vm_agent] {key}: 无结果")
                else:
                    logger.warning(f"[vm_agent] {key} 查询失败: {data.get('error')}")
            except Exception as e:
                logger.warning(f"[vm_agent] {key} 查询异常: {e}")

        # 主机查询: 按 host 字段查找主机上的所有 VM
        if search:
            expanded = await self._expand_site_vm_search(search)
            if expanded:
                host_count = len(expanded.get('results', []))
                snap_count = len(all_data.get("snapshot_vms", {}).get("results", []))
                if host_count > snap_count:
                    all_data["smartops_vms_host"] = expanded
                    logger.info(f"[vm_agent] 主机查询: host={host_count} > name={snap_count}")

        if not all_data:
            logger.info("[vm_agent] GCOC 查询: 所有端点均无数据")
            return ""
        
        logger.info(f"[vm_agent] GCOC 查询完成: {len(all_data)} 个数据源有结果")
        return json.dumps(all_data, ensure_ascii=False, indent=2)[:8000]

    async def _expand_site_vm_search(self, search: str) -> dict:
        """主机 VM 查询: 将 'ESXI-02' 视为主机名，按 host 字段查找该主机上的所有 VM
        合并 smartops_vms + snapshot_vms 数据
        数据链路统一在 gcoc_api.query_vms_on_esxi_host:
          MCP 全量缓存建 esxi_host 索引 → 短名解析完整宿主机 →
          REST host= 服务端精确过滤 (~0.2s, 替代旧的全量分页拉取 20s+/页) →
          snapshot_vms 站点前缀合并 (命中率达标即早停, 不做全量兑底)"""
        info = await query_vms_on_esxi_host(search)
        if not info or not info["vms"]:
            return None
        return {"results": info["vms"], "count": len(info["vms"]),
                "_source": info["source"], "_host_filter": info["host_code"],
                "_esxi_hosts": info["esxi_hosts"]}

    # ═══ 统一接口 ═══

    async def _vm_list(self, msg: AgentMessage):
        """列出虚拟机 — 优先从 GCOC 平台查询"""
        platform = self._detect_platform(msg.params)
        
        # 智能提取搜索关键词: 从 params.search / params.args / msg.text 中提取
        search = msg.params.get("search") or msg.params.get("args") or ""
        
        # 如果 params 中没有搜索词，尝试从 msg.text 中提取
        if not search and msg.text:
            import re
            # 匹配 "查询 ESXI-03"、"找到 ESXI-03" 等
            match = re.search(r'(?:查询|查找|搜索|查看|获取|列出|list|find|search|query|get)\s*([A-Za-z0-9\-_]+)', msg.text, re.I)
            if match:
                search = match.group(1)
            else:
                # 如果没找到动词，尝试提取最长的英文/数字字符串
                matches = re.findall(r'[A-Za-z0-9\-_]{3,}', msg.text)
                if matches:
                    search = max(matches, key=len)  # 取最长的
        
        logger.info(f"[vm_agent] VM 列表查询: 搜索关键词='{search}'")

        # 优先从 GCOC 平台查询
        gcoc_data = await self._query_gcoc_vms(search)
        if gcoc_data:
            # L4 范围约束: 用户站点范围非空时, VM 结果按站点关键词裁剪 (范围外资源不可见)
            gcoc_data = await self._filter_vms_by_scope(gcoc_data, getattr(msg, "username", "") or "")
        if gcoc_data:
            analysis = await self.llm_chat(
                prompt=f"以下是从 GCOC 平台查询到的虚拟机数据 (关键词: '{search}'):\n\n{gcoc_data}",
                system=VM_ANALYSIS_PROMPT
            )
            await self.reply(msg, result={"text": analysis, "platform": "gcoc", "raw_data": gcoc_data})
            await self.log_daily(f"VM 列表查询: GCOC (搜索: {search})")
            return

        # 降级: 尝试本地虚拟化平台
        try:
            if platform == "vmware":
                vms = await self._vmware_list_vms()
                raw = json.dumps(vms, ensure_ascii=False, indent=2)
            elif platform == "pve":
                data = await self._pve_api("GET", "/cluster/resources?type=vm")
                raw = json.dumps(data, ensure_ascii=False, indent=2)
            else:
                output = await self._kvm_exec("list --all")
                raw = output
            analysis = await self.llm_chat(
                prompt=f"以下是 {platform} 平台的虚拟机列表:\n\n{raw}",
                system=VM_ANALYSIS_PROMPT
            )
            await self.reply(msg, result={"text": analysis, "platform": platform, "raw_data": raw})
            await self.log_daily(f"VM 列表查询: {platform}")
        except Exception as e:
            await self.reply(msg, error=f"获取 VM 列表失败: 本地 {platform} 不可用且 GCOC 无数据")

    async def _vm_power(self, msg: AgentMessage):
        vm_name = msg.params.get("name") or msg.params.get("vm", "")
        action = msg.params.get("power_action", "status")
        platform = self._detect_platform(msg.params)
        if not vm_name:
            await self.reply(msg, error="缺少虚拟机名称")
            return
        try:
            if platform == "kvm":
                action_map = {"start": "start", "shutdown": "shutdown", "reboot": "reboot", "destroy": "destroy", "status": "domstate"}
                virsh_action = action_map.get(action, "domstate")
                output = await self._kvm_exec(f"{virsh_action} {shlex.quote(vm_name)}")
            elif platform == "pve":
                if action == "status":
                    output = json.dumps(await self._pve_api("GET", f"/nodes/localhost/qemu"), indent=2)
                else:
                    output = json.dumps(await self._pve_api("POST", f"/nodes/localhost/qemu/{vm_name}/status/{action}"), indent=2)
            else:
                output = f"VMware 电源操作需要额外实现: {vm_name} {action}"
            await self.reply(msg, result={"text": f"虚拟机 {vm_name} 执行 {action}:\n{output}", "vm": vm_name, "action": action, "platform": platform})
            await self.log_daily(f"VM 电源操作: {vm_name} → {action}")
        except Exception as e:
            await self.reply(msg, error=f"VM 电源操作失败: {str(e)}")

    async def _vm_snapshot(self, msg: AgentMessage):
        vm_name = msg.params.get("name") or msg.params.get("vm", "")
        snap_action = msg.params.get("snap_action", "create")
        snap_name = msg.params.get("snap_name", f"zops-snap-{int(asyncio.get_event_loop().time())}")
        platform = self._detect_platform(msg.params)
        if not vm_name:
            await self.reply(msg, error="缺少虚拟机名称")
            return
        try:
            if platform == "kvm":
                cmd_map = {"create": f"snapshot-create-as {shlex.quote(vm_name)} {shlex.quote(snap_name)}",
                           "list": f"snapshot-list {shlex.quote(vm_name)}",
                           "revert": f"snapshot-revert {shlex.quote(vm_name)} {shlex.quote(snap_name)}",
                           "delete": f"snapshot-delete {shlex.quote(vm_name)} {shlex.quote(snap_name)}"}
                output = await self._kvm_exec(cmd_map.get(snap_action, f"snapshot-list {shlex.quote(vm_name)}"))
            elif platform == "pve":
                output = json.dumps(await self._pve_api("POST", f"/nodes/localhost/qemu/{vm_name}/snapshot", {"snapname": snap_name}), indent=2)
            else:
                output = "VMware 快照操作需要额外实现"
            await self.reply(msg, result={"text": f"快照操作 {snap_action} ({vm_name}):\n{output}", "vm": vm_name, "snap_action": snap_action, "platform": platform})
            await self.log_daily(f"VM 快照: {vm_name} → {snap_action}")
            await self.notify(f"快照操作完成: {vm_name} {snap_action}", "vm")
        except Exception as e:
            await self.reply(msg, error=f"快照操作失败: {str(e)}")

    async def _vm_migrate(self, msg: AgentMessage):
        vm_name = msg.params.get("name") or msg.params.get("vm", "")
        target_host = msg.params.get("target_host", "")
        platform = self._detect_platform(msg.params)
        text = msg.text or ""

        # ── 智能判断: 查询迁移记录 vs 执行迁移操作 ──
        # 如果没有指定 vm_name 和 target_host，说明是查询类请求 ("最近迁移过的VM"、"检查迁移记录")
        if not vm_name and not target_host:
            # 查询迁移事件 — 从 GCOC Smart-Ops 获取迁移相关数据
            search_kw = text or "迁移"
            gcoc_data = await smart_query(
                question=search_kw, domains=["vm", "monitoring"],
                llm_func=self.llm_chat, max_queries=2
            )
            if gcoc_data:
                analysis = await self.llm_chat(
                    prompt=(
                        f"用户问题: {text}\n\n"
                        f"以下是从 GCOC 平台查询到的迁移相关数据:\n{gcoc_data}"
                    ),
                    system=(
                        "你是 Z-OPS 运维智能体的虚拟化管理模块。\n"
                        "用户询问的是迁移记录/迁移历史，请从数据中提取 vMotion/迁移相关的事件。\n"
                        "用 🔴(故障) 🟡(警告) 🟢(正常) 标注状态，用表格展示结果。"
                    )
                )
                await self.reply(msg, result={"text": analysis, "source": "gcoc"})
                await self.log_daily(f"VM 迁移查询: {text[:50]}")
            else:
                # GCOC 也没数据，给出提示
                result = await self.llm_chat(
                    prompt=text,
                    system=(
                        "你是 Z-OPS 运维智能体的虚拟化专家。\n"
                        "用户询问迁移记录，但当前无法从 GCOC 平台获取迁移数据。\n"
                        "请告知用户可以通过 vCenter 直接查看 vMotion 事件，或提供具体的 VM 名称和目标主机来执行迁移操作。"
                    )
                )
                await self.reply(msg, result={"text": result})
            return

        # ── 执行迁移操作 (vm_name + target_host 都有) ──
        try:
            if platform == "kvm":
                output = await self._kvm_exec(f"migrate --live {shlex.quote(vm_name)} qemu+ssh://{shlex.quote(target_host)}/system")
            elif platform == "pve":
                output = json.dumps(await self._pve_api("POST", f"/nodes/localhost/qemu/{vm_name}/migrate", {"target": target_host}), indent=2)
            else:
                output = "VMware vMotion 迁移需要额外实现"
            await self.reply(msg, result={"text": f"迁移 {vm_name} → {target_host}:\n{output}", "vm": vm_name, "target": target_host, "platform": platform})
            await self.log_daily(f"VM 迁移: {vm_name} → {target_host}")
            await self.notify(f"虚拟机迁移: {vm_name} → {target_host}", "vm")
        except Exception as e:
            await self.reply(msg, error=f"迁移失败: {str(e)}")

    async def _host_status(self, msg: AgentMessage):
        platform = self._detect_platform(msg.params)
        try:
            if platform == "kvm":
                output = await self._kvm_exec("nodeinfo")
                mem_output = await self._kvm_exec("nodememstats")
                raw = f"节点信息:\n{output}\n\n内存状态:\n{mem_output}"
            elif platform == "pve":
                data = await self._pve_api("GET", "/nodes")
                raw = json.dumps(data, ensure_ascii=False, indent=2)
            else:
                raw = "VMware ESXi 主机状态查询需要额外实现"
            analysis = await self.llm_chat(prompt=f"以下是 {platform} 宿主机状态:\n\n{raw}", system=VM_ANALYSIS_PROMPT)
            await self.reply(msg, result={"text": analysis, "platform": platform, "raw_data": raw})
        except Exception as e:
            await self.reply(msg, error=f"获取宿主机状态失败: {str(e)}")

    async def _vm_chat(self, msg: AgentMessage):
        """虚拟化运维对话 — 自动检索 GCOC API 获取真实数据"""
        prompt = msg.text or msg.params.get("text", "")
        if not prompt:
            await self.reply(msg, error="缺少问题内容")
            return

        # 智能查询 GCOC 平台 VM 相关数据
        raw_data = await smart_query(
            question=prompt, domains=VM_DOMAINS,
            llm_func=self.llm_chat, max_queries=3
        )

        if raw_data:
            result = await self.llm_chat(
                prompt=f"用户问题: {prompt}\n\n从 GCOC 平台查询到的虚拟化数据:\n{raw_data}",
                system=VM_ANALYSIS_PROMPT
            )
            await self.reply(msg, result={"text": result, "source": "gcoc"})
            await self.log_daily(f"VM 智能查询: {prompt[:50]}")
        else:
            result = await self.llm_chat(
                prompt=prompt,
                system=(
                    "你是 Z-OPS 运维智能体的虚拟化专家。擅长 VMware vSphere、KVM/QEMU、Proxmox VE 管理。\n"
                    "你可以查询 GCOC 平台的 Smart-Ops VM列表、Aria Ops VM、快照VM、VCD资源 等数据。\n"
                    "如果用户需要查询实际的虚拟机数据，请告知你会从 GCOC 平台获取。"
                )
            )
            await self.reply(msg, result={"text": result})


if __name__ == "__main__":
    run_agent(VMAgent())
