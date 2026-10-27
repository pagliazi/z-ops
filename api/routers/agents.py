"""Z-OPS /api Agent/工具/知识库 域路由 (v4.0 Phase 6: 从 webchat_api.py 拆分, 行为不变)

端点逻辑原样搬迁 (@app → @router); 共享依赖经 from webchat_api import * 获取 —
webchat_api.py 在文件底部 include_router, 彼时全部共享名已定义, 无循环导入问题。
"""
from webchat_api import *  # noqa: F401,F403
from core.bizperms import require_admin_or_biz_perm  # 业务权限门 (与系统角色分离)  (config/logger/get_redis/verify_token/...)
from webchat_api import (_HOSTNAME_RE, _IP4_RE, _NMS_ACTIVE_SEV, _NMS_HARDWARE_TITLES, _NMS_PLAN_GROUPS, _NMS_PLAN_GROUP_BY_TITLE, _NMS_PLATFORM_NETS, _NMS_SEV_LABEL, _NMS_SEV_PRIORITY, _log_user_op)  # noqa: F401  本域引用的主文件私有辅助
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from fastapi import Response  # SSL 调查创建: 202 新建 / 200 幂等复用
from datetime import datetime as _dt  # Phase 2.1: 时间范围过滤
import base64 as _nms_b64  # noqa: F401  (nms 凭据解码, 原主文件模块级 import)
import uuid as _uuid  # noqa: F401  (Phase 1.3: SSE stream_id)

router = APIRouter()


async def _get_all_providers():
    """惰性转发 admin 域的 provider 列表: systemd 以 __main__ 直接运行 webchat_api.py 时,
    顶层 `from api.routers.admin import ...` 会触发 webchat_api 二次加载→ admin 部分初始化→
    循环导入 ImportError; 运行时导入时全部模块已就绪, 无此问题。"""
    from api.routers.admin import _get_all_providers as _admin_providers
    return await _admin_providers()



# ═══ 工具注册表 & 用量查询 (参考 claude-code cost-tracker + claw-code tools) ═══

@router.get("/api/tools")
async def list_tools(current_user: str = Depends(verify_token)):
    """获取所有已注册的工具列表 (参考 claude-code Tool System)"""
    try:
        from core.tool_schema import get_tool_registry
        from core.skill_tool_bridge import auto_register_all_skills
        reg = get_tool_registry()
        # 确保工具已注册 (跨进程场景下 webchat_api 进程也能查到)
        if len(reg.get_tools()) == 0:
            auto_register_all_skills(reg)
            # 手动注册 gcoc_agent 工具定义 (元数据, 不含 handler)
            _register_gcoc_tool_metadata(reg)
        tools = reg.get_tools()
        return {
            "count": len(tools),
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "category": t.category,
                    "agent_id": t.agent_id,
                    "permission": t.permission.value if hasattr(t.permission, 'value') else str(t.permission),
                    "params": [p.name for p in t.input_schema.params] if t.input_schema else [],
                }
                for t in tools
            ]
        }
    except Exception as e:
        return {"count": 0, "tools": [], "error": str(e)}

def _register_gcoc_tool_metadata(reg):
    """在 webchat_api 进程中注册 gcoc_agent 工具元数据 (仅描述信息, 不含实际 handler)"""
    try:
        from core.tool_schema import ToolDef, ToolInputSchema, ToolParam, ParamType, PermissionLevel
        gcoc_tools = [
            ("cmdb_query", "查询 CMDB 主机/服务器信息", ["search"]),
            ("backup_status", "查询 BRR 备份作业状态", ["search"]),
            ("backup_objects", "查询备份对象 (VM 级别)", ["search"]),
            ("snapshot_check", "查询虚拟机快照列表", ["search"]),
            ("smartops_vms", "查询 Smart-Ops 虚拟机列表", ["search"]),
            ("smartops_events", "查询运维事件", ["search"]),
            ("vcd_resources", "查询 VCD 云平台资源", ["search"]),
            ("dns_query", "查询 DNS 记录 (A/AAAA/CNAME/MX/TXT/NS/SOA/PTR/SRV, 支持批量域名/IP反向解析)", ["domain", "record_type", "server"]),
            ("dns_resolve", "智能 DNS 解析 (IP反向解析主机名/域名正向解析IP, 支持批量)", ["targets"]),
            ("dns_compare", "多 DNS 服务器对比查询", ["domain", "servers", "record_type"]),
            ("dns_trace", "DNS 全链路解析追踪 (dig +trace)", ["domain", "record_type"]),
            ("ha_query", "查询 HA 高可用重启事件", ["search"]),
            ("monitor_query", "查询监控数据", ["search"]),
            ("alert_query", "查询告警信息", ["search"]),
            ("aria_ops", "查询 Aria Ops 仪表盘", ["search", "type"]),
            ("m365_status", "查询 M365 备份状态", []),
        ]
        for name, desc, params in gcoc_tools:
            td = ToolDef(
                name=f"gcoc_agent.{name}",
                description=desc,
                agent_id="gcoc_agent",
                category="gcoc",
                input_schema=ToolInputSchema(
                    params=[ToolParam(name=p, type=ParamType.STRING, description=p) for p in params]
                ),
                permission=PermissionLevel.PUBLIC,
                execute_fn=None,
            )
            reg.register(td)
    except Exception as e:
        logger.warning(f"[tools] gcoc 元数据注册失败: {e}")

@router.get("/api/overview")
async def overview(current_user: str = Depends(verify_token)):
    try:
        r = await get_redis()
        agents_raw = await r.hgetall(config.KEY_HEARTBEATS)
        agents = {}
        for name, data in agents_raw.items():
            try:
                info = json.loads(data)
                last_hb = info.get("last_heartbeat", 0)
                if time.time() - last_hb > 30:
                    info["status"] = "timeout"
                agents[name] = info
            except json.JSONDecodeError:
                agents[name] = {"status": "unknown"}

        channels_raw = await r.hgetall(config.KEY_CHANNEL_HEARTBEATS)
        channels = {}
        for name, data in channels_raw.items():
            try:
                channels[name] = json.loads(data)
            except json.JSONDecodeError:
                channels[name] = {"status": "unknown"}

        # 累计 LLM 调用数: 读 incr 计数器 (旧口径 llen(KEY_LLM_USAGE) 恒为 1000 ——
        # 该 list 被 ltrim(-1000,-1) 截断作日志清单用, 不是总量, 勿再用它当统计)
        usage_count = int(await r.get(config.KEY_LLM_USAGE_TOTAL) or 0)
        # 读取 Redis 动态配置，优先于 .env 静态值
        pref_raw = await r.get(config.KEY_LLM_PREF)
        pref = json.loads(pref_raw) if pref_raw else {}
        cur_provider = pref.get("provider") or config.LLM_PROVIDER
        cur_model = pref.get("model") or config.LLM_MODEL
        # 真实模型名: 网关验证映射 > 静态别名表 > 原名
        from core.model_gateway import _display_real_model, verified_model_names
        real_model = _display_real_model(await verified_model_names(r), cur_model)
        # Agent 数与技能数（来自注册表，供管理后台“系统信息”展示）
        agent_count = len(agents)
        total_skills = 0
        try:
            from core.registry import get_registry
            _reg = get_registry().get_all()
            total_skills = sum(len(getattr(c, "skills", []) or []) for c in _reg.values())
        except Exception:
            total_skills = 0
        return {
            "app": config.APP_NAME, "description": config.APP_DESC,
            "version": config.VERSION,
            "agents": agents, "channels": channels,
            "agent_count": agent_count, "total_skills": total_skills,
            "llm_provider": cur_provider, "llm_model": cur_model,
            "llm_real_model": real_model,
            "llm_model_alias": cur_model != real_model,  # 是否为别名
            "llm_usage_count": usage_count, "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"[API] overview 错误：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取系统概览失败：{str(e)}")

# ═══ 知识库 KB ═══
@router.post("/api/kb/search")
async def kb_search_api(request: Request, current_user: str = Depends(verify_token)):
    from agents.kb_agent import kb_multi_recall, format_kb_results
    body = await request.json()
    query = body.get("query", "")
    limit = body.get("limit", config.KB_SEARCH_LIMIT)
    if not query:
        raise HTTPException(400, "缺少 query")
    # search (关键词) + hybrid (语义) 双路召回: search 端点对长句全字段匹配几乎全 0 条
    results = await kb_multi_recall([query], limit)
    return {"query": query, "results": results, "count": len(results), "formatted": format_kb_results(results)}

@router.get("/api/kb/config")
async def kb_config_get(current_user: str = Depends(verify_token)):
    return {"kb_api_url": config.KB_API_URL, "kb_search_limit": config.KB_SEARCH_LIMIT, "kb_rag_enabled": config.KB_RAG_ENABLED}

# ═══ 健康检查 ═══
@router.get("/api/health")
async def health():
    try:
        r = await get_redis()
        await r.ping()
        return {"status": "ok", "redis": "connected", "timestamp": time.time()}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "redis": str(e)})

# ═══ Agent 详情 (v2.0.0) ═══

@router.get("/api/agents/status")
async def agents_status(current_user: str = Depends(verify_token)):
    """返回 Agent 实时状态: 进程心跳 + ToolPool注册 + 执行统计 + 配置信息"""
    try:
        import subprocess
        r = await get_redis()
        # 1. 检查进程存活
        proc_result = subprocess.run(
            ['ps', 'aux'], capture_output=True, text=True, timeout=5
        )
        ps_output = proc_result.stdout
        # 2. 读取 agents.yaml 配置
        from core.registry import get_registry
        registry = get_registry()
        all_agents = registry.get_all()
        # 3. 读取 ToolPool 注册
        toolpool_raw = await r.hgetall("zops:toolpool:agents")
        toolpool = {}
        for k, v in toolpool_raw.items():
            try: toolpool[k] = json.loads(v)
            except: pass
        # 4. 读取 ExecutionRegistry 统计
        from datetime import datetime as _dt
        exec_stats = {}
        for aid in all_agents:
            key = f"zops:exec_stats:{aid}"
            raw = await r.hgetall(key)
            if raw:
                exec_stats[aid] = {k: float(v) if '.' in str(v) else int(v) for k, v in raw.items()}
        # 5. 读取全局 LLM 配置
        pref_raw = await r.get(config.KEY_LLM_PREF)
        pref = json.loads(pref_raw) if pref_raw else {}
        global_model = f"{pref.get('provider') or config.LLM_PROVIDER}/{pref.get('model') or config.LLM_MODEL}"
        # 6. 组装结果
        result = []
        for agent_id, cfg in all_agents.items():
            # 检查进程是否存活
            process_alive = f"agents.{agent_id}" in ps_output or f"-m agents.{agent_id}" in ps_output
            # ToolPool 注册状态
            tp = toolpool.get(agent_id, {})
            tp_registered = bool(tp)
            tp_tools = tp.get("tools", [])
            tp_caps = tp.get("capabilities", [])
            # 统计数据
            stats = exec_stats.get(agent_id, {})
            total_calls = int(stats.get("total_calls", 0))
            success_calls = int(stats.get("success_calls", 0))
            total_latency = float(stats.get("total_latency_ms", 0))
            # 成功率与平均延迟由原始字段派生 (Redis 存 success_calls/total_latency_ms)
            success_rate = round(success_calls / total_calls * 100, 1) if total_calls > 0 else 0.0
            avg_latency_ms = round(total_latency / total_calls, 1) if total_calls > 0 else 0.0
            # 在线状态: 进程存活 = online
            status = "online" if process_alive else "offline"
            # Per-agent 模型
            override_raw = await r.get(config.key_agent_model(agent_id))
            model = override_raw or (cfg.model if cfg.model else global_model)
            # bindings keywords
            bindings_kw = []
            for b in cfg.bindings:
                bindings_kw.extend(b.keywords)
            result.append({
                "id": agent_id,
                "name": cfg.name,
                "description": cfg.description,
                "status": status,
                "process_alive": process_alive,
                "toolpool_registered": tp_registered,
                "model": model,
                "skills": cfg.skills,
                "tools": tp_tools,
                "capabilities": tp_caps,
                "tool_count": len(tp_tools) if tp_tools else len(cfg.skills),
                "bindings_keywords": list(set(bindings_kw)),
                "enabled": cfg.enabled,
                "stats": {
                    "total_calls": total_calls,
                    "success_rate": success_rate,
                    "avg_latency_ms": avg_latency_ms,
                },
            })
        # 按 online 优先排序
        result.sort(key=lambda x: (0 if x["status"] == "online" else 1, x["id"]))
        online_count = sum(1 for a in result if a["status"] == "online")
        return {
            "agents": result,
            "online": online_count,
            "total": len(result),
            "global_model": global_model,
            "timestamp": time.time()
        }
    except Exception as e:
        logger.warning(f"[api] agents/status 错误: {e}")
        return {"agents": [], "online": 0, "total": 0, "error": str(e)}

@router.get("/api/system/resources")
async def system_resources(current_user: str = Depends(verify_token)):
    """返回系统资源实时数据: CPU/内存/磁盘/进程"""
    import subprocess
    try:
        # CPU
        with open('/proc/loadavg') as f:
            loadavg = f.read().strip().split()
        cpu_count = os.cpu_count() or 8
        # CPU usage via /proc/stat (1-second sample)
        with open('/proc/stat') as f:
            cpu_line1 = f.readline().split()[1:]
        await asyncio.sleep(0.5)
        with open('/proc/stat') as f:
            cpu_line2 = f.readline().split()[1:]
        idle1, idle2 = int(cpu_line1[3]), int(cpu_line2[3])
        total1 = sum(int(x) for x in cpu_line1[:8])
        total2 = sum(int(x) for x in cpu_line2[:8])
        cpu_pct = round((1 - (idle2 - idle1) / max(1, total2 - total1)) * 100, 1)
        # Memory
        mem = {}
        with open('/proc/meminfo') as f:
            for line in f:
                parts = line.split()
                if parts[0] in ('MemTotal:', 'MemAvailable:', 'MemFree:', 'Buffers:', 'Cached:'):
                    mem[parts[0].rstrip(':')] = int(parts[1])  # kB
        mem_total = mem.get('MemTotal', 0)
        mem_avail = mem.get('MemAvailable', mem.get('MemFree', 0))
        mem_used = mem_total - mem_avail
        mem_pct = round(mem_used / max(1, mem_total) * 100, 1)
        # Disk
        st = os.statvfs('/')
        disk_total = st.f_frsize * st.f_blocks
        disk_free = st.f_frsize * st.f_bavail
        disk_used = disk_total - disk_free
        disk_pct = round(disk_used / max(1, disk_total) * 100, 1)
        # Uptime
        with open('/proc/uptime') as f:
            uptime_sec = float(f.read().split()[0])
        # Z-OPS processes
        proc_result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        ps_lines = proc_result.stdout.split('\n')
        zops_procs = []
        for line in ps_lines:
            if any(k in line for k in ['webchat_api', 'agents.', 'next-server', 'redis-server', 'nginx', 'n8n']):
                parts = line.split(None, 10)
                if len(parts) >= 11:
                    zops_procs.append({
                        'pid': parts[1], 'cpu': parts[2], 'mem': parts[3],
                        'cmd': parts[10][:80]
                    })
        # GPU check
        gpu_info = None
        try:
            gpu_result = subprocess.run(
                ['nvidia-smi', '--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=3
            )
            if gpu_result.returncode == 0:
                g = gpu_result.stdout.strip().split(', ')
                gpu_info = {'name': g[0], 'mem_total_mb': int(g[1]), 'mem_used_mb': int(g[2]), 'mem_free_mb': int(g[3]), 'util_pct': int(g[4])}
        except Exception:
            pass
        return {
            'cpu': {'count': cpu_count, 'usage_pct': cpu_pct, 'load_1m': float(loadavg[0]), 'load_5m': float(loadavg[1]), 'load_15m': float(loadavg[2])},
            'memory': {'total_gb': round(mem_total / 1048576, 1), 'used_gb': round(mem_used / 1048576, 1), 'avail_gb': round(mem_avail / 1048576, 1), 'usage_pct': mem_pct},
            'disk': {'total_gb': round(disk_total / (1024**3), 1), 'used_gb': round(disk_used / (1024**3), 1), 'free_gb': round(disk_free / (1024**3), 1), 'usage_pct': disk_pct},
            'gpu': gpu_info,
            'uptime_hours': round(uptime_sec / 3600, 1),
            'processes': zops_procs,
            'local_model_recommendation': {
                'feasible': mem_total > 8 * 1048576,
                'has_gpu': gpu_info is not None,
                'recommended_models': [
                    {'name': 'Qwen2.5-3B-Q4_K_M', 'ram_gb': 2.5, 'desc': '超轻量 — 上下文摘要/意图分类', 'speed': '~15 tok/s CPU'},
                    {'name': 'Phi-3.5-mini-Q4', 'ram_gb': 2.8, 'desc': '推理能力强 — 代码/逻辑任务', 'speed': '~12 tok/s CPU'},
                    {'name': 'Qwen2.5-7B-Q4_K_M', 'ram_gb': 5.0, 'desc': '均衡推荐 — 通用对话/上下文增强', 'speed': '~8 tok/s CPU'},
                ] if not gpu_info else [
                    {'name': 'Qwen2.5-7B-Q4_K_M', 'ram_gb': 5.0, 'desc': 'GPU加速 — 通用对话', 'speed': '~40 tok/s GPU'},
                    {'name': 'Qwen2.5-14B-Q4_K_M', 'ram_gb': 9.0, 'desc': 'GPU推荐 — 高质量推理', 'speed': '~20 tok/s GPU'},
                ],
                'use_cases': ['上下文窗口摘要压缩', '意图预分类(减少主LLM调用)', '敏感信息脱敏', '本地embedding生成'],
                'runtime': 'Ollama (推荐) 或 llama.cpp server',
                'note': f'当前系统: {cpu_count}核 / {round(mem_total/1048576,1)}GB RAM / {"有GPU" if gpu_info else "无GPU"}'
            },
            'timestamp': time.time()
        }
    except Exception as e:
        logger.warning(f'[api] system/resources 错误: {e}')
        return {'error': str(e)}

@router.get("/api/agents/detail")
async def agents_detail(current_user: str = Depends(verify_token)):
    """返回 agents.yaml 中所有 Agent 的声明式配置 (含 per-agent 模型覆盖)"""
    try:
        from core.registry import get_registry
        registry = get_registry()
        agents = registry.get_all()
        r = await get_redis()
        # 读取全局配置
        pref_raw = await r.get(config.KEY_LLM_PREF)
        pref = json.loads(pref_raw) if pref_raw else {}
        global_model = f"{pref.get('provider') or config.LLM_PROVIDER}/{pref.get('model') or config.LLM_MODEL}"
        # 真实模型名 (展示用): 网关验证映射 > OneAPI 静态别名表
        from core.model_gateway import _display_real_model, verified_model_names
        _vmap = await verified_model_names(r)
        def _real_model(display: str) -> str:
            return _display_real_model(_vmap, display.split("/")[-1])
        global_real_model = _real_model(global_model)
        result = {}
        for agent_id, cfg in agents.items():
            bindings_kw = []
            for b in cfg.bindings:
                bindings_kw.extend(b.keywords)
            # Per-Agent 模型覆盖: Redis > agents.yaml > 全局
            override_raw = await r.get(config.key_agent_model(agent_id))
            if override_raw:
                model_display = override_raw
            elif cfg.model:
                model_display = cfg.model
            else:
                model_display = global_model
            result[agent_id] = {
                "id": cfg.id, "name": cfg.name, "description": cfg.description,
                "model": model_display,
                "real_model": _real_model(model_display),
                "model_source": "override" if override_raw else ("yaml" if cfg.model else "global"),
                "skills": cfg.skills, "workspace": cfg.workspace, "soul": cfg.soul,
                "enabled": cfg.enabled, "tools_allow": cfg.tools.allow, "tools_deny": cfg.tools.deny,
                "bindings_keywords": list(set(bindings_kw)), "bindings_count": len(cfg.bindings),
            }
        return {"agents": result, "count": len(result), "global_model": global_model,
                "global_real_model": global_real_model, "providers": await _get_all_providers()}
    except Exception as e:
        logger.warning(f"[api] agents/detail 错误: {e}")
        return {"agents": {}, "count": 0, "error": str(e)}

@router.get("/api/agents/{agent_id}/model")
async def agent_model_get(agent_id: str, current_user: str = Depends(verify_token)):
    """Per-Agent 模型配置"""
    r = await get_redis()
    override = await r.get(config.key_agent_model(agent_id))
    pref_raw = await r.get(config.KEY_LLM_PREF)
    pref = json.loads(pref_raw) if pref_raw else {}
    global_model = f"{pref.get('provider') or config.LLM_PROVIDER}/{pref.get('model') or config.LLM_MODEL}"
    return {"agent_id": agent_id, "override": override or "", "global": global_model, "providers": await _get_all_providers()}

@router.post("/api/agents/{agent_id}/model")
async def agent_model_set(agent_id: str, request: Request, current_user: str = Depends(verify_token)):
    """Per-Agent 模型配置 (仅管理员)"""
    await require_admin(current_user)
    body = await request.json()
    model = body.get("model", "").strip()  # "provider/model" 或空串 (清除覆盖)
    r = await get_redis()
    if model:
        await r.set(config.key_agent_model(agent_id), model)
    else:
        await r.delete(config.key_agent_model(agent_id))
    await _log_user_op(current_user, "agent_model", f"{agent_id}: {model or '(恢复全局)'}")
    return {"status": "ok", "agent_id": agent_id, "model": model or "(global)"}

# ═══ LLM-Wiki API (Karpathy 知识编译思想) ═══

# ═══ Outline 知识库 API (/api/wiki/*) ═══
# 对接 Outline KB (KB_API_URL=http://198.18.18.166:8089)

@router.get("/api/wiki/search")
async def wiki_search(q: str = "", top_k: int = 10, current_user: str = Depends(verify_token)):
    """搜索 Outline 知识库文档（优先混合搜索，携带上级集合分类）。"""
    import httpx

    def _map(docs: list) -> list:
        # hybrid-search 同一文档可能命中多个 chunk，按 id 去重保留最高分
        best: dict = {}
        for d in docs:
            did = d.get("id", "")
            if not did or not (d.get("context") or d.get("title")):
                continue
            cur = best.get(did)
            if cur is None or (d.get("score") or 0) > (cur.get("score") or 0):
                best[did] = d
        return [
            {
                "page_id": d.get("id", ""),
                "title": d.get("title", ""),
                "collection": d.get("collection", "") or "",
                "breadcrumb": d.get("breadcrumb", "") or "",
                "match_type": d.get("match_type", "") or "",
                "child_count": d.get("child_count", 0) or 0,
                "empty_content": bool(d.get("empty_content")),
                "score": round(d.get("score", 1.0) or 1.0, 3),
                "snippet": (d.get("context", "") or "")[:300],
                "url": d.get("url", ""),
                "updated_at": d.get("updated_at", "")
            }
            for d in best.values()
        ]

    # 1) 混合搜索（关键词+语义，返回 collection 字段）
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=3), verify=False) as client:
            resp = await client.post(
                f"{config.KB_API_URL}/api/kb/hybrid-search",
                headers={"Authorization": f"Bearer {config.KB_API_TOKEN}", "Content-Type": "application/json"},
                json={"query": q, "limit": top_k, "mode": "hybrid"}
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("success", False):
            hits = _map(data.get("documents", []))
            hits.sort(key=lambda h: h["score"], reverse=True)
            return {"query": q, "hits": hits, "total": len(hits)}
    except Exception as e:
        logger.warning(f"[api] wiki/search hybrid 失败，回退关键词搜索: {e}")
    # 2) 回退：纯关键词搜索
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), verify=False) as client:
            resp = await client.post(
                f"{config.KB_API_URL}/api/kb/search",
                headers={"Authorization": f"Bearer {config.KB_API_TOKEN}", "Content-Type": "application/json"},
                json={"query": q, "limit": top_k}
            )
            resp.raise_for_status()
            data = resp.json()
        if not data.get("success", False):
            return {"query": q, "hits": [], "total": 0}
        return {"query": q, "hits": _map(data.get("documents", [])), "total": len(_map(data.get("documents", [])))}
    except Exception as e:
        logger.warning(f"[api] wiki/search 错误: {e}")
        return {"query": q, "hits": [], "total": 0, "error": str(e)}

@router.get("/api/wiki/page/{page_id}")
async def wiki_get_page(page_id: str, current_user: str = Depends(verify_token)):
    """获取 Outline 文档内容 (POST /api/kb/document)。"""
    try:
        import httpx
        url = f"{config.KB_API_URL}/api/kb/document"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), verify=False) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {config.KB_API_TOKEN}", "Content-Type": "application/json"},
                json={"id": page_id}
            )
            resp.raise_for_status()
            data = resp.json()
        doc = data.get("document", {})
        if not doc:
            return {"error": f"文档 {page_id} 不存在", "status": 404}

        # 子文档树透传 (父目录文档在内容区展开子KB)
        def _node(c: dict) -> dict:
            return {
                "page_id": c.get("id", ""),
                "title": c.get("title", ""),
                "url": c.get("url", ""),
                "updated_at": c.get("updated_at", ""),
                "child_count": c.get("child_count", 0) or 0,
                "children": [_node(k) for k in (c.get("children") or [])],
            }

        return {
            "page_id": doc.get("id", page_id),
            "title": doc.get("title", ""),
            "page_type": doc.get("collection", ""),
            "content": doc.get("text", ""),
            "url": doc.get("url", ""),
            "child_count": doc.get("child_count", 0) or 0,
            "children": [_node(c) for c in (doc.get("children") or [])],
            "metadata": {
                "created": doc.get("created_at", ""),
                "updated": doc.get("updated_at", ""),
                "sources": [],
                "tags": doc.get("tags", [])
            }
        }
    except Exception as e:
        logger.warning(f"[api] wiki/page 错误：{e}")
        return {"error": str(e), "status": 500}

@router.get("/api/wiki/stats")
async def wiki_stats(current_user: str = Depends(verify_token)):
    """获取 Outline 知识库统计信息。"""
    try:
        import httpx
        url = f"{config.KB_API_URL}/api/kb/stats"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), verify=False) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {config.KB_API_TOKEN}"}
            )
            resp.raise_for_status()
            data = resp.json()
        stats = data.get("stats", {})
        collections = stats.get("collections", [])
        by_type = {c.get("name", ""): 1 for c in collections}
        return {
            "total_pages": stats.get("total_documents", 0),
            "total_collections": stats.get("total_collections", len(collections)),
            "by_type": by_type,
            "collections": collections,
            "last_sync": stats.get("last_sync", ""),
            "api_status": "online",
            "source": "outline"
        }
    except Exception as e:
        logger.warning(f"[api] wiki/stats 错误: {e}")
        return {"total_pages": 0, "total_collections": 0, "by_type": {}, "api_status": "offline", "error": str(e)}

async def _outline_post(path: str, payload: dict) -> dict:
    """调用 Outline 原生 API (POST + Bearer Token)。"""
    import httpx
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), verify=False) as client:
        resp = await client.post(
            f"{config.OUTLINE_API_URL}/{path}",
            headers={"Authorization": f"Bearer {config.OUTLINE_API_TOKEN}", "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

@router.get("/api/wiki/collections")
async def wiki_collections(current_user: str = Depends(verify_token)):
    """Outline 集合列表 + 每集合真实文档数 (原生 API)。"""
    import asyncio
    try:
        data = await _outline_post("collections.list", {})
        colls = data.get("data", [])

        async def _count(cid: str) -> int:
            try:
                r = await _outline_post("documents.list", {"collectionId": cid, "limit": 1})
                return int(r.get("pagination", {}).get("total", 0))
            except Exception:
                return 0

        counts = await asyncio.gather(*[_count(c.get("id", "")) for c in colls])
        return {
            "collections": [
                {
                    "id": c.get("id", ""),
                    "name": c.get("name", ""),
                    "description": (c.get("description") or "").strip(),
                    "count": n,
                }
                for c, n in zip(colls, counts)
            ]
        }
    except Exception as e:
        logger.warning(f"[api] wiki/collections 错误: {e}")
        return {"collections": [], "error": str(e)}

@router.get("/api/wiki/collection/{collection_id}/docs")
async def wiki_collection_docs(
    collection_id: str,
    current_user: str = Depends(verify_token),
):
    """Outline 集合下完整文档树 (父文档嵌套子文档，按更新时间倒序)。"""
    import asyncio
    import math
    try:
        first = await _outline_post("documents.list", {
            "collectionId": collection_id, "limit": 100, "offset": 0,
            "sort": "updatedAt", "direction": "DESC",
        })
        docs = first.get("data", [])
        total = int(first.get("pagination", {}).get("total", len(docs)))
        # 并发拉取剩余页 (每页 100，上限 30 页)
        pages = min(math.ceil(total / 100), 30)
        if pages > 1:
            rest = await asyncio.gather(*[
                _outline_post("documents.list", {
                    "collectionId": collection_id, "limit": 100, "offset": i * 100,
                    "sort": "updatedAt", "direction": "DESC",
                })
                for i in range(1, pages)
            ])
            for r in rest:
                docs.extend(r.get("data", []))
        # 建树：docs 已按更新时间倒序，子文档按同样顺序挂到父文档下
        items = []
        for d in docs:
            items.append((d.get("parentDocumentId") or "", {
                "page_id": d.get("id", ""),
                "title": d.get("title", "") or "（无标题文档）",
                "url": d.get("url", ""),
                "updated_at": (d.get("updatedAt") or "")[:10],
                "children": [],
            }))
        nodes = {n["page_id"]: n for _, n in items}
        tree = []
        for pid, n in items:
            if pid and pid != n["page_id"] and pid in nodes:
                nodes[pid]["children"].append(n)
            else:
                tree.append(n)
        return {"tree": tree, "total": total}
    except Exception as e:
        logger.warning(f"[api] wiki/collection/{collection_id}/docs 错误: {e}")
        return {"tree": [], "total": 0, "error": str(e)}

# ═══ 内部 LLM-Wiki API (/api/llm-wiki/*) ═══
# 对接本地 wiki_store (LLM 自动生成的知识页面)，仅管理员可见

@router.get("/api/llm-wiki/search")
async def llm_wiki_search(q: str = "", top_k: int = 10, current_user: str = Depends(verify_token)):
    """搜索 LLM-Wiki 知识。"""
    await require_admin_or_biz_perm(current_user, "knowledge.manage")  # 查看/审计门: admin 或持「LLM-Wiki 运营」权限
    try:
        from core.memory.wiki_query import search_wiki
        hits = search_wiki(q, top_k=top_k)
        return {
            "query": q,
            "hits": [
                {
                    "page_id": h.page_id,
                    "title": getattr(h, 'title', h.page_id),
                    "page_type": getattr(h, 'page_type', ''),
                    "score": getattr(h, 'score', 0),
                    "snippet": getattr(h, 'snippet', '')[:300]
                }
                for h in hits
            ],
            "total": len(hits)
        }
    except Exception as e:
        logger.warning(f"[api] llm-wiki/search 错误: {e}")
        return {"query": q, "hits": [], "total": 0, "error": str(e)}

@router.get("/api/llm-wiki/page/{page_id}")
async def llm_wiki_get_page(page_id: str, page_type: str = "", current_user: str = Depends(verify_token)):
    """获取 LLM-Wiki 页面完整内容。"""
    await require_admin_or_biz_perm(current_user, "knowledge.manage")  # 查看/审计门: admin 或持「LLM-Wiki 运营」权限
    try:
        from core.memory.wiki_store import get_wiki_store
        store = get_wiki_store()
        if page_type:
            page = store.get(page_type, page_id)
        else:
            page = None
            for p in store.all_pages():
                if p.page_id == page_id:
                    page = p
                    break
        if not page:
            return {"error": f"页面 {page_id} 不存在", "status": 404}
        return {
            "page_id": page.page_id,
            "title": getattr(page, 'title', page.page_id),
            "page_type": getattr(page, 'page_type', ''),
            "content": page.body,
            "metadata": {
                "created": getattr(page, 'created', page.frontmatter.get('created', '')),
                "updated": getattr(page, 'updated', page.frontmatter.get('updated', '')),
                "sources": getattr(page, 'sources', page.frontmatter.get('sources', [])),
                "tags": getattr(page, 'tags', page.frontmatter.get('tags', []))
            }
        }
    except Exception as e:
        logger.warning(f"[api] llm-wiki/page 错误：{e}")
        return {"error": str(e), "status": 500}

@router.get("/api/llm-wiki/pages")
async def llm_wiki_list_pages(current_user: str = Depends(verify_token)):
    """列出所有 LLM-Wiki 页面，按类型分组。"""
    await require_admin_or_biz_perm(current_user, "knowledge.manage")  # 查看/审计门: admin 或持「LLM-Wiki 运营」权限
    try:
        from core.memory.wiki_store import get_wiki_store
        store = get_wiki_store()
        grouped = {pt: [] for pt in ("entities", "concepts", "summaries", "synthesis", "queries")}
        for p in store.all_pages():
            if p.page_type not in grouped:
                continue
            title = ""
            for line in p.body.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            grouped[p.page_type].append({
                "page_id": p.page_id,
                "title": title or p.page_id,
                "page_type": p.page_type,
                "updated": p.frontmatter.get("updated", ""),
                "created": p.frontmatter.get("created", ""),
                "tags": p.frontmatter.get("tags", []),
                "sources_count": len(p.frontmatter.get("sources", []))
            })
        for pt in grouped:
            grouped[pt].sort(key=lambda x: x.get("updated") or "", reverse=True)
        return {"groups": grouped, "total": sum(len(v) for v in grouped.values())}
    except Exception as e:
        logger.warning(f"[api] llm-wiki/pages 错误: {e}")
        return {"groups": {}, "total": 0, "error": str(e)}

@router.get("/api/llm-wiki/stats")
async def llm_wiki_stats(current_user: str = Depends(verify_token)):
    """获取 LLM-Wiki 统计信息。"""
    await require_admin_or_biz_perm(current_user, "knowledge.manage")  # 查看/审计门: admin 或持「LLM-Wiki 运营」权限
    try:
        from core.memory.wiki_store import get_wiki_store
        from core.memory.wiki_lint import run_lint
        store = get_wiki_store()
        pages = list(store.all_pages())
        by_type: dict = {}
        last_updated = ""
        for p in pages:
            by_type[p.page_type] = by_type.get(p.page_type, 0) + 1
            upd = p.frontmatter.get("updated") or ""
            if upd > last_updated:
                last_updated = upd
        lint = run_lint(store, mark_archived=False) or {}
        lint_issues = [f"[{cat}] {item}" for cat, items in lint.items() for item in (items or [])]
        return {
            "total_pages": len(pages),
            "by_type": by_type,
            "last_updated": last_updated,
            "lint_by_category": {k: len(v) for k, v in lint.items() if v},
            "lint_issues": lint_issues
        }
    except Exception as e:
        logger.warning(f"[api] llm-wiki/stats 错误: {e}")
        return {"error": str(e)}

# ═══ NMS 告警通知/飞书/截图动态配置（前端管理页可控，Redis KEY_NMS_DYN_CFG） ═══

@router.get('/api/nms/config')
async def nms_config_get(current_user: str = Depends(verify_token)):
    """获取 NMS 告警通知/飞书/截图配置（动态已保存值 + 实际生效值，密钥脱敏）"""
    r = await get_redis()
    raw = await r.get(config.KEY_NMS_DYN_CFG)
    try:
        dyn = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        dyn = {}
    if not isinstance(dyn, dict):
        dyn = {}
    _static_webhook = config.FEISHU_WEBHOOK_URL or ""
    _dyn_webhook = dyn.get("feishu_webhook_url") or ""
    _eff_webhook = _dyn_webhook or _static_webhook
    _eff_app_id = dyn.get("feishu_app_id") or config.FEISHU_APP_ID or ""
    # 截图分组/级别：动态显式空字符串=关闭，未设置=静态默认
    _sg = dyn.get("snapshot_groups") if "snapshot_groups" in dyn else config.NMS_SNAPSHOT_GROUPS
    _ss = dyn.get("snapshot_severities") if "snapshot_severities" in dyn else config.NMS_SNAPSHOT_SEVERITIES
    return {
        "saved": {
            "feishu_webhook_url": _dyn_webhook,
            "feishu_app_id": dyn.get("feishu_app_id", "") or "",
            "feishu_app_secret_set": bool(dyn.get("feishu_app_secret")),
            "feishu_verification_token": dyn.get("feishu_verification_token", "") or "",
            "push_enabled": dyn.get("push_enabled"),
            "push_only_on_new": dyn.get("push_only_on_new"),
            "snapshot_groups": dyn.get("snapshot_groups"),
            "snapshot_severities": dyn.get("snapshot_severities"),
            "snapshot_unack_only": dyn.get("snapshot_unack_only"),
            "push_groups": dyn.get("push_groups"),
            "check_interval": dyn.get("check_interval"),
        },
        "effective": {
            "feishu_webhook_url": _eff_webhook,
            "feishu_app_id": _eff_app_id,
            "push_enabled": dyn.get("push_enabled", config.NMS_FEISHU_PUSH_ENABLED),
            "push_only_on_new": dyn.get("push_only_on_new", config.NMS_PUSH_ONLY_ON_NEW),
            "snapshot_groups": _sg,
            "snapshot_severities": _ss,
            "snapshot_unack_only": dyn.get("snapshot_unack_only", config.NMS_SNAPSHOT_UNACK_ONLY),
            "push_groups": dyn.get("push_groups") or config.NMS_PUSH_GROUPS,
            "check_interval": dyn.get("check_interval", config.NMS_CHECK_INTERVAL),
        },
        "feishu_enabled": bool(_eff_webhook and "YOUR_HOOK_ID" not in _eff_webhook),
        "feishu_static_configured": bool(_static_webhook and "YOUR_HOOK_ID" not in _static_webhook),
        "event_port": config.FEISHU_EVENT_PORT,
        "public_base_url": config.ZOPS_PUBLIC_BASE_URL,
        "cmd_help": "nms 状态 / nms 分组 <名称> / nms 截图 <名称>",
        "severity_options": [
            {"value": "critical", "label": "严重"}, {"value": "major", "label": "主要"},
            {"value": "minor", "label": "轻微"}, {"value": "warning", "label": "警告"},
            {"value": "indeterminate", "label": "未知"},
        ],
    }

@router.post('/api/nms/config')
async def nms_config_set(request: Request, current_user: str = Depends(verify_token)):
    """保存 NMS 告警通知/飞书/截图动态配置（仅管理员）
    字段: feishu_webhook_url, feishu_app_id, feishu_app_secret(空=保持原值),
    feishu_verification_token, push_enabled, push_only_on_new,
    snapshot_groups(逗号分隔,空=关闭截图), snapshot_severities(逗号分隔,空=只截整组图),
    check_interval(秒,>=60)。"""
    await require_admin(current_user)
    body = await request.json()
    r = await get_redis()
    raw = await r.get(config.KEY_NMS_DYN_CFG)
    try:
        dyn = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        dyn = {}
    if not isinstance(dyn, dict):
        dyn = {}
    changed = []
    for k in ("feishu_webhook_url", "feishu_app_id", "feishu_verification_token",
              "snapshot_groups", "snapshot_severities", "push_groups"):
        if k not in body:
            continue
        dyn[k] = str(body[k]).strip() if body[k] is not None else ""
        changed.append(k)
    if "feishu_app_secret" in body and body["feishu_app_secret"]:
        dyn["feishu_app_secret"] = str(body["feishu_app_secret"]).strip()
        changed.append("feishu_app_secret")
    for k in ("push_enabled", "push_only_on_new", "snapshot_unack_only"):
        if k in body:
            dyn[k] = bool(body[k])
            changed.append(k)
    if "check_interval" in body:
        try:
            iv = max(60, int(body["check_interval"]))
            dyn["check_interval"] = iv
            changed.append("check_interval")
        except (TypeError, ValueError):
            pass
    await r.set(config.KEY_NMS_DYN_CFG, json.dumps(dyn, ensure_ascii=False))
    await _log_user_op(current_user, "nms_config", f"saved: {','.join(changed)}")
    logger.info(f"[NMS-CFG] 动态配置已保存 ({','.join(changed)}), 操作人: {current_user}")
    return {"status": "ok", "changed": changed}

@router.post('/api/nms/config/test-feishu')
async def nms_config_test_feishu(request: Request, current_user: str = Depends(verify_token)):
    """发送测试通知到飞书群，直接同步调用 webhook 并返回飞书 API 结果"""
    await require_admin(current_user)
    r = await get_redis()
    raw = await r.get(config.KEY_NMS_DYN_CFG)
    try:
        dyn = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        dyn = {}
    webhook_url = (dyn.get("feishu_webhook_url") or "").strip() or config.FEISHU_WEBHOOK_URL
    if not webhook_url or "YOUR_HOOK_ID" in webhook_url:
        raise HTTPException(400, "飞书 Webhook 未配置，请先在下方保存 Webhook URL")
    import httpx
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"content": "Z-OPS 运维通知 [连通性测试]", "tag": "plain_text"},
                      "template": "blue"},
            "elements": [
                {"tag": "div", "text": {"content": "🧪 **Z-OPS 飞书通道连通性测试**\n"
                 "✅ 若您收到此消息，说明告警通知链路已打通（配置来自前端管理页）。\n"
                 "**重点分组**: GCOC_use\n**预案命中**: SSL证书过期\n"
                 f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "tag": "lark_md"}}
            ]
        }
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code == 200:
                result = resp.json()
                if result.get("code") == 0:
                    return {"status": "ok", "message": "测试消息已发送，请查收飞书群"}
                raise HTTPException(500, f"飞书返回错误: {result}")
            raise HTTPException(500, f"飞书 HTTP {resp.status_code}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"发送失败: {e}")

# ═══ NMS 飞书订阅频道（KEY_NMS_SUBS: chat_id -> [分组名]） ═══

async def _feishu_bot_chats() -> list:
    """用应用凭据查机器人所在群列表（供订阅 UI 选择），失败返回空列表。"""
    r = await get_redis()
    raw = await r.get(config.KEY_NMS_DYN_CFG)
    try:
        dyn = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        dyn = {}
    app_id = (dyn.get("feishu_app_id") or config.FEISHU_APP_ID or "").strip()
    app_secret = (dyn.get("feishu_app_secret") or config.FEISHU_APP_SECRET or "").strip()
    if not app_id or not app_secret:
        return []
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            tok_resp = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret})
            token = tok_resp.json().get("tenant_access_token") or ""
            if not token:
                return []
            chat_resp = await client.get(
                "https://open.feishu.cn/open-apis/im/v1/chats",
                headers={"Authorization": f"Bearer {token}"},
                params={"page_size": 100})
            data = chat_resp.json()
            if data.get("code") != 0:
                return []
            return [{"chat_id": it.get("chat_id", ""),
                     "name": it.get("name", "")}
                    for it in data.get("data", {}).get("items", [])]
    except Exception:
        return []

@router.get('/api/nms/subs')
async def nms_subs_get(current_user: str = Depends(verify_token)):
    """获取飞书订阅列表 + 机器人所在群列表（供订阅 UI 选择）"""
    r = await get_redis()
    subs_raw = await r.hgetall(config.KEY_NMS_SUBS)
    subs = []
    for chat_id, raw in (subs_raw or {}).items():
        try:
            groups = json.loads(raw or "[]")
        except json.JSONDecodeError:
            groups = []
        subs.append({"chat_id": chat_id, "groups": groups if isinstance(groups, list) else []})
    subs.sort(key=lambda x: x["chat_id"])
    return {"subs": subs, "chats": await _feishu_bot_chats()}

@router.post('/api/nms/subs')
async def nms_subs_set(request: Request, current_user: str = Depends(verify_token)):
    """设置/删除飞书订阅: {chat_id, groups: [分组名]}；groups 空数组=退订该群"""
    await require_admin(current_user)
    body = await request.json()
    chat_id = str(body.get("chat_id") or "").strip()
    groups = body.get("groups")
    if not chat_id:
        raise HTTPException(400, "chat_id 不能为空")
    if not isinstance(groups, list):
        raise HTTPException(400, "groups 必须是数组")
    groups = [str(g).strip() for g in groups if str(g).strip()][:50]
    r = await get_redis()
    if groups:
        await r.hset(config.KEY_NMS_SUBS, chat_id, json.dumps(groups, ensure_ascii=False))
        await _log_user_op(current_user, "nms_subs", f"set: {chat_id} -> {','.join(groups)}")
        return {"status": "ok", "chat_id": chat_id, "groups": groups}
    await r.hdel(config.KEY_NMS_SUBS, chat_id)
    await _log_user_op(current_user, "nms_subs", f"del: {chat_id}")
    return {"status": "ok", "chat_id": chat_id, "groups": [], "removed": True}

# ═══ NMS 飞书多源推送通道（KEY_NMS_CHANNELS: channel_id -> JSON） ═══

async def _nms_channels_all(r) -> list:
    """读取全部推送通道（解析失败跳过），按创建时间排序。"""
    raw = await r.hgetall(config.KEY_NMS_CHANNELS)
    channels = []
    for _id, d in (raw or {}).items():
        try:
            ch = json.loads(d)
            if isinstance(ch, dict):
                channels.append(ch)
        except json.JSONDecodeError:
            continue
    channels.sort(key=lambda c: (c.get("created_at") or "", c.get("id") or ""))
    return channels

async def _nms_migrate_subs(r) -> None:
    """旧订阅表(KEY_NMS_SUBS)一次性迁移为 app 型通道；已有通道或无旧订阅时跳过。"""
    try:
        if await r.hlen(config.KEY_NMS_CHANNELS) > 0:
            return
        subs_raw = await r.hgetall(config.KEY_NMS_SUBS)
    except Exception:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for chat_id, raw_groups in (subs_raw or {}).items():
        try:
            groups = [g for g in json.loads(raw_groups or "[]")
                      if isinstance(g, str) and g.strip()]
        except json.JSONDecodeError:
            groups = []
        ch = {
            "id": f"ch_{uuid.uuid4().hex[:8]}", "name": f"订阅群 {chat_id[:12]}",
            "type": "app", "webhook_url": "", "chat_id": chat_id,
            "groups": groups, "severities": [],
            "enabled": True, "is_default": False,
            "created_at": now, "updated_at": now,
        }
        await r.hset(config.KEY_NMS_CHANNELS, ch["id"],
                     json.dumps(ch, ensure_ascii=False))
        logger.info(f"[NMS-CH] 旧订阅已迁移为通道: {chat_id[:12]}... -> "
                    f"{ch['id']} (groups={','.join(groups) or '全部'})")

@router.get('/api/nms/channels')
async def nms_channels_get(current_user: str = Depends(verify_token)):
    """获取多源推送通道列表 + 机器人所在群 + 分组/级别候选（供通道 UI）
    P0: 全局 NMS 快照数据 (无 ACL 历史) 收权 — admin 或通知渠道管理者可见"""
    await require_admin_or_biz_perm(current_user, "notify.manage")
    r = await get_redis()
    await _nms_migrate_subs(r)
    channels = await _nms_channels_all(r)
    # 分组候选 = 最新告警快照的 groups 并集 + 采集过滤组 + 用户组 NMS 映射
    group_opts = {g for g in config.NMS_FILTERS if g not in ("*", "ALL", "all")}
    try:
        latest_raw = await r.get(f"{config.REDIS_PREFIX}:nms_alarms:latest")
        if latest_raw:
            for a in json.loads(latest_raw).get("alarms") or []:
                for g in (a.get("groups") or
                          ([a.get("group")] if a.get("group") else [])):
                    if g:
                        group_opts.add(g)
    except Exception:
        pass
    try:
        for raw in (await r.hgetall(config.KEY_GROUP_NMS)).values():
            try:
                for g in json.loads(raw):
                    if isinstance(g, str) and g.strip():
                        group_opts.add(g.strip())
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return {
        "channels": channels,
        "chats": await _feishu_bot_chats(),
        "group_options": sorted(group_opts),
        "severity_options": [
            {"value": "critical", "label": "严重"}, {"value": "major", "label": "主要"},
            {"value": "minor", "label": "轻微"}, {"value": "warning", "label": "警告"},
            {"value": "indeterminate", "label": "未知"},
        ],
    }

@router.post('/api/nms/channels')
async def nms_channels_set(request: Request, current_user: str = Depends(verify_token)):
    """新建/更新推送通道。is_default 全局唯一：置 true 时清除其他通道标记。
    (2026-10-18 集成: admin 或持「通知渠道管理」权限 — 与权限点语义对齐)"""
    await require_admin_or_biz_perm(current_user, "notify.manage")
    body = await request.json()
    ch_id = str(body.get("id") or "").strip()
    name = str(body.get("name") or "").strip()
    ch_type = str(body.get("type") or "webhook").strip()
    webhook_url = str(body.get("webhook_url") or "").strip()
    chat_id = str(body.get("chat_id") or "").strip()
    groups = [str(g).strip() for g in body.get("groups") or [] if str(g).strip()][:50]
    _SEVS = ("critical", "major", "minor", "warning", "indeterminate")
    severities = [str(s).strip() for s in body.get("severities") or []
                  if str(s).strip() in _SEVS]
    enabled = bool(body.get("enabled", True))
    is_default = bool(body.get("is_default", False))
    if not name:
        raise HTTPException(400, "通道名称不能为空")
    if ch_type not in ("webhook", "app"):
        raise HTTPException(400, "type 必须是 webhook 或 app")
    if ch_type == "webhook" and (not webhook_url or not webhook_url.startswith("http")):
        raise HTTPException(400, "Webhook 型通道必须填写合法 Webhook URL")
    if ch_type == "app" and not chat_id:
        raise HTTPException(400, "应用群型通道必须填写群 chat_id（机器人需已入群）")
    r = await get_redis()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = await _nms_channels_all(r)
    if ch_id:
        prev = next((c for c in existing if c.get("id") == ch_id), None)
        if not prev:
            raise HTTPException(404, f"通道 {ch_id} 不存在")
        created_at = prev.get("created_at") or now
    else:
        prev = None
        ch_id = f"ch_{uuid.uuid4().hex[:8]}"
        created_at = now
    ch = {
        "id": ch_id, "name": name, "type": ch_type,
        "webhook_url": webhook_url if ch_type == "webhook" else "",
        "chat_id": chat_id if ch_type == "app" else "",
        "groups": groups, "severities": severities,
        "enabled": enabled, "is_default": is_default,
        "created_at": created_at, "updated_at": now,
    }
    # 保留服务端运行时字段：卡片按钮「静默」写入的 silent_until（表单不携带，防覆盖丢失）
    if prev and prev.get("silent_until"):
        ch["silent_until"] = prev["silent_until"]
    if is_default:
        for c in existing:
            if c.get("id") != ch["id"] and c.get("is_default"):
                c["is_default"] = False
                await r.hset(config.KEY_NMS_CHANNELS, c["id"],
                             json.dumps(c, ensure_ascii=False))
    await r.hset(config.KEY_NMS_CHANNELS, ch["id"], json.dumps(ch, ensure_ascii=False))
    await _log_user_op(current_user, "nms_channels",
                       f"{'update' if body.get('id') else 'create'}: {name}({ch_type}) "
                       f"groups={','.join(groups) or '全部'} sev={','.join(severities) or '全部'}")
    return {"status": "ok", "channel": ch}

@router.delete('/api/nms/channels/{ch_id}')
async def nms_channels_delete(ch_id: str, current_user: str = Depends(verify_token)):
    """删除推送通道 (admin 或持「通知渠道管理」权限)"""
    await require_admin_or_biz_perm(current_user, "notify.manage")
    r = await get_redis()
    removed = await r.hdel(config.KEY_NMS_CHANNELS, ch_id)
    if not removed:
        raise HTTPException(404, f"通道 {ch_id} 不存在")
    await _log_user_op(current_user, "nms_channels", f"delete: {ch_id}")
    return {"status": "ok", "removed": ch_id}

async def _feishu_tenant_token() -> str:
    """应用机器人 tenant_access_token（供通道测试发送），失败返回空串。"""
    r = await get_redis()
    raw = await r.get(config.KEY_NMS_DYN_CFG)
    try:
        dyn = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        dyn = {}
    app_id = (dyn.get("feishu_app_id") or config.FEISHU_APP_ID or "").strip()
    app_secret = (dyn.get("feishu_app_secret") or config.FEISHU_APP_SECRET or "").strip()
    if not app_id or not app_secret:
        return ""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret})
            data = resp.json()
            if data.get("code") == 0:
                return data.get("tenant_access_token") or ""
    except Exception:
        pass
    return ""

@router.post('/api/nms/channels/test')
async def nms_channels_test(request: Request, current_user: str = Depends(verify_token)):
    """向指定通道发送测试卡片：Webhook 型直发 URL；应用群型走 im/v1/messages。
    (2026-10-18 集成: admin 或持「通知渠道管理」权限 — 通知测试页共用)"""
    await require_admin_or_biz_perm(current_user, "notify.manage")
    body = await request.json()
    ch_id = str(body.get("id") or "").strip()
    r = await get_redis()
    raw = await r.hget(config.KEY_NMS_CHANNELS, ch_id)
    if not raw:
        raise HTTPException(404, f"通道 {ch_id} 不存在")
    ch = json.loads(raw)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                              "content": f"🧪 通道连通性测试 · {ch['name']}"},
                   "template": "blue"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"**通道**: {ch['name']}（{'Webhook' if ch['type'] == 'webhook' else '应用群'}）\n"
                        f"**分组**: {'、'.join(ch.get('groups') or []) or '全部'}\n"
                        f"**级别**: {'、'.join(ch.get('severities') or []) or '全部'}\n"
                        f"**时间**: {now_str}\n\n"
                        "✅ 收到此卡片说明该通道链路已打通，告警将按上述过滤条件推送到本群。"}},
        ],
    }
    import httpx
    try:
        if ch["type"] == "webhook":
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(ch["webhook_url"],
                                         json={"msg_type": "interactive", "card": card})
            if resp.status_code != 200:
                raise HTTPException(500, f"飞书 HTTP {resp.status_code}")
            result = resp.json()
            if result.get("code") != 0:
                raise HTTPException(500, f"飞书返回错误: {result.get('msg') or result}")
        else:
            token = await _feishu_tenant_token()
            if not token:
                raise HTTPException(400, "应用凭据(App ID/Secret)未配置，无法测试应用群型通道")
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"receive_id": ch["chat_id"], "msg_type": "interactive",
                          "content": json.dumps(card, ensure_ascii=False)})
            result = resp.json()
            if result.get("code") != 0:
                raise HTTPException(500, f"飞书返回错误: {result.get('code')} {result.get('msg')}（确认机器人已加入该群）")
        return {"status": "ok", "message": f"测试卡片已发送到通道「{ch['name']}」，请查收"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"发送失败: {e}")


@router.get('/api/nms/default-groups')
async def nms_get_default_groups(current_user: str = Depends(verify_token)):
    """获取用户NMS默认分组: 优先个人设置，其次组别映射"""
    try:
        r = await get_redis()
        user_raw = await r.hget(config.KEY_USERS, current_user)
        if not user_raw:
            return {'groups': []}
        user = json.loads(user_raw)
        # 1. 优先返回用户个人 nms_groups
        personal = user.get('nms_groups') or []
        if personal:
            return {'groups': personal, 'source': 'personal'}
        # 2. 其次根据用户组别查组级映射
        user_group = user.get('group', '')
        if user_group:
            group_nms_raw = await r.hget(config.KEY_GROUP_NMS, user_group)
            if group_nms_raw:
                group_nms = json.loads(group_nms_raw)
                if group_nms:
                    return {'groups': group_nms, 'source': 'group'}
        # 3. 回退到系统监控的默认过滤器（NMS_FILTERS，排除 * / ALL 通配符），再回退 NMS_DEFAULT_GROUPS
        nms_filters = [g for g in config.NMS_FILTERS if g not in ('*', 'ALL', 'all')]
        fallback = nms_filters or list(config.NMS_DEFAULT_GROUPS)
        if fallback:
            return {'groups': fallback, 'source': 'config'}
        # 4. 都没有则返回空
        return {'groups': []}
    except Exception as e:
        return {'error': str(e), 'groups': []}

@router.post('/api/nms/default-groups')
async def nms_set_default_groups(request: Request, current_user: str = Depends(verify_token)):
    """保存用户个人NMS默认分组"""
    try:
        body = await request.json()
        groups = body.get('groups', [])
        if not isinstance(groups, list):
            raise HTTPException(400, 'groups must be a list')
        groups = [str(g) for g in groups][:300]
        r = await get_redis()
        user_raw = await r.hget(config.KEY_USERS, current_user)
        if not user_raw:
            raise HTTPException(404, 'user not found')
        user = json.loads(user_raw)
        user['nms_groups'] = groups
        await r.hset(config.KEY_USERS, current_user, json.dumps(user, ensure_ascii=False))
        return {'status': 'ok', 'groups': groups}
    except HTTPException:
        raise
    except Exception as e:
        return {'status': 'error', 'error': str(e)}

def _nms_resolve_owner(cred_raw, current_user):
    """根据用户 NMS 凭据决定读取键前缀。返回 (base, is_personal, creds)。
    无凭据或凭据为全局账号(svc-gcoc) -> 全局共享键。"""
    creds = None
    if cred_raw:
        try:
            creds = json.loads(_nms_b64.b64decode(cred_raw))
        except Exception:
            creds = None
    nms_user = (creds or {}).get('username') or ''
    if creds and nms_user and nms_user != config.NMS_USERNAME:
        return f"{config.REDIS_PREFIX}:nms_alarms:user:{nms_user}", True, creds
    return f"{config.REDIS_PREFIX}:nms_alarms", False, creds

def _nms_resolve_session_owner(r, current_user):
    """SSO 会话版 owner 解析 (零密码存储)。

    用户在 Z-OPS 走 AD/LDAP 登录时后台自动建立 NMS 会话 (仅 Cookie);
    有会话 → 个人视图 (快照键按用户名隔离); 无会话 → 全局共享。
    返回 (base, is_personal)。
    """
    import asyncio as _aio
    # 同步判断不了 async get — 调用方已持有 r, 直接用
    return None  # placeholder (actual impl below)


async def _nms_resolve_owner_async(r, current_user):
    """异步版: 读会话键判定个人/全局视图。"""
    has_session = bool(await r.get(f"{config.REDIS_PREFIX}:nms_session:{current_user}"))
    if has_session and current_user and current_user != config.NMS_USERNAME:
        return f"{config.REDIS_PREFIX}:nms_alarms:user:{current_user}", True
    return f"{config.REDIS_PREFIX}:nms_alarms", False


def _nms_groups_of(a):
    """单条去重告警的分组归属列表（一条告警可属于多个视图）"""
    g = a.get('groups')
    if g:
        return g
    single = a.get('group')
    return [single] if single else ['未分组']

def _nms_group_desc(g, ms):
    """群描述动态化：用实时命中桶的真实 IP/对象替换静态说明（不再外露脱敏占位符）"""
    hits = []
    for t in g.get('titles', []):
        m = next((x for x in ms if x['title'] == t), None)
        if not m:
            continue
        ips = m.get('ips') or []
        if ips:
            objs = '、'.join(ips[:4]) + (' 等' if len(ips) > 4 else '')
        else:
            objs = '、'.join(s.get('name', '') for s in (m.get('sources') or [])[:4])
        hits.append(f"{t} {m['count']} 条（{objs}）")
    if not hits:
        return g.get('desc', '')
    return '命中预案：' + '；'.join(hits) + '。' + g.get('desc', '')

def _nms_alarm_ips(a):
    """提取告警中的 IP 列表（source/node 若为 IP 直接用；否则从 summary 中正则提取）"""
    ips = set()
    for k in ("source", "node", "node_alias"):
        v = (a.get(k) or "").strip()
        if _IP4_RE.fullmatch(v):
            ips.add(v)
    for m in _IP4_RE.findall(a.get("summary") or ""):
        ips.add(m)
    return sorted(ips)

def _nms_is_platform_ip(ip):
    return any(ip.startswith(net) for net in _NMS_PLATFORM_NETS)

def _nms_match_tenant(b):
    """预案单客户/平台影响面：platform=平台(多客户共享)、single=单客户、both=两者都有"""
    has_p = any(_nms_is_platform_ip(ip) for ip in b.get("ips", []))
    has_s = any(not _nms_is_platform_ip(ip) for ip in b.get("ips", []))
    if has_p and has_s:
        return "both"
    if has_p:
        return "platform"
    if has_s:
        return "single"
    return "unknown"

def _nms_variant_applies(sheet, tenant):
    """按影响面判定某侧处理建议是否适用（范围未知/归并双侧时都保留）"""
    if tenant not in ('platform', 'single') or not sheet:
        return True
    has_p, has_s = '平台' in sheet, '单客户' in sheet
    if has_p and has_s:
        return True  # 两侧归并的建议对两种影响面都适用
    if has_p:
        return tenant == 'platform'
    if has_s:
        return tenant == 'single'
    return True

def _nms_parse_steps(steps):
    """把 checklist 步骤文本拆成 (编号, 步骤) 列表（如 '1. xxx\n2. yyy'）"""
    import re as _re
    matches = list(_re.finditer(r'(\d{1,2})\s*[.、)]\s*', steps or ''))
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else None
        text = ' '.join((steps or '')[m.end():end].split())
        if text:
            out.append((m.group(1), text))
    if not out and (steps or '').strip():
        out.append(('', ' '.join((steps or '').split())))
    return out

def _nms_step_annotation(step, b, impact, devices, tool_checks=None, state=None):
    """按 checklist 步骤关键词映射真实数据标注（无对应数据返回空串）。
    优先级：工具实测(DNS/SSL 证书/TCP/备份作业) > 设备信息(SN/型号/固件) > 维保 > 客户业务 > 磁盘/RAID > 日志/状态/检查。
    state: 跨步骤共享状态 {'alert_used','backup_used'}，用于去重，避免同一条告警/实测反复显示。"""
    if state is None:
        state = {}
    low = step.lower()
    ok_devs = [d for d in (devices or []) if d.get('status') == 'ok']
    samples = [s for s in (b.get('samples') or []) if (s.get('summary') or '').strip()]

    def dev_info(d):
        segs = [d.get('name') or d.get('key')]
        if d.get('ip') and d['ip'] not in segs[0]:
            segs.append(f"({d['ip']})")
        if d.get('sn'):
            segs.append(f"SN {d['sn']}")
        v = ' '.join(x for x in (d.get('vendor'), d.get('model')) if x and x != '未采集')
        if v:
            segs.append(v)
        fw = d.get('firmware')
        segs.append(f"固件 {fw}" if fw and fw != '未采集' else '固件未采集')
        return ' '.join(segs)

    # 工具实测（GCOC DNS API）：解析/测试类步骤优先标注实测结果
    dns_tests = (tool_checks or {}).get("dns") or []
    if dns_tests and any(k in low for k in ('解析', '测试', '查询', 'resolve', 'dns')):
        return ('DNS API 实测：' + '；'.join(t['summary'] for t in dns_tests[:3])
                + (f' 等 {len(dns_tests)} 项' if len(dns_tests) > 3 else ''))

    # 工具实测（SSL 证书）：浏览器检查/证书过期类步骤标注实测剩余天数
    ssl_tests = (tool_checks or {}).get("ssl") or []
    if ssl_tests and any(k in low for k in ('证书', 'ssl', '过期', '浏览器', '网页')):
        return ('SSL 证书实测：' + '；'.join(t['summary'] for t in ssl_tests[:3])
                + (f' 等 {len(ssl_tests)} 项' if len(ssl_tests) > 3 else ''))

    # 工具实测（TCP 端口探测）：ping/端口/连通类步骤标注探测结果
    tcp_tests = (tool_checks or {}).get("tcp") or []
    if tcp_tests and any(k in low for k in ('端口', '可达', '连通', 'tcp', '连接', 'outage', 'ping')):
        return ('TCP 端口探测：' + '；'.join(t['summary'] for t in tcp_tests[:3])
                + (f' 等 {len(tcp_tests)} 项' if len(tcp_tests) > 3 else ''))

    # 工具实测（备份作业 BRR/M365 API）：按步骤语义细分标注，避免同类实测跨步骤重复
    bk_tests = (tool_checks or {}).get("backup") or []
    if bk_tests:
        if any(k in low for k in ('备份', '作业', '报告')):
            if state.get('backup_used'):
                return ''
            state['backup_used'] = True
            return ('备份作业实测：' + '；'.join(t['summary'] for t in bk_tests[:3])
                    + (f' 等 {len(bk_tests)} 项' if len(bk_tests) > 3 else ''))
        if any(k in low for k in ('proxy', '服务器', 'vcenter', 'esxi')):
            servers = sorted({s for t in bk_tests for s in (t.get('servers') or [])})
            if servers:
                return '备份服务器（GCOC-MGT BRR）：' + '、'.join(servers[:3])
        if any(k in low for k in ('空间', '限额', '订单', '链', '还原')):
            rp = max((t.get('latest_rp') or '' for t in bk_tests), key=len)
            if rp:
                return '备份链最近还原点：' + rp

    if any(k in low for k in ('sn', '型号', '厂商', '固件')):
        return f'设备：{dev_info(ok_devs[0])}' if ok_devs else \
            '设备：GCOC资源管理平台未收录该告警 IP（严格精确匹配）'
    if any(k in low for k in ('维保', '报障', '更换', 'change')):
        return f'{dev_info(ok_devs[0])}；维保未登记（GCOC资源管理平台无维保数据）' \
            if ok_devs else '维保未登记（GCOC资源管理平台无维保数据）'
    if any(k in step for k in ('客户', '业务')):
        cus = (impact or {}).get('customers') or {}
        if cus:
            names = sorted(cus, key=lambda x: -cus[x])
            return '、'.join(f'{c}（{cus[c]} 台）' for c in names[:3]) + \
                (f' 等 {len(names)} 家' if len(names) > 3 else '')
        if (impact or {}).get('vms'):
            return f"关联资源 {(impact or {}).get('vms')} 台，未登记客户归属"
        return '受影响 IP 未关联到客户虚机/卷映射'
    if any(k in low for k in ('磁盘', 'raid', 'disk')):
        for s in samples:
            sm = s.get('summary') or ''
            if 'disk' in sm.lower() or 'raid' in sm.lower() or '磁盘' in sm:
                return f'告警：{sm[:100]}'
        return f'告警：{(samples[0].get("summary") or "")[:100]}' if samples else ''
    if any(k in low for k in ('日志', '状态', '检查', '管理页面', '登录')):
        if state.get('alert_used'):
            return ''
        state['alert_used'] = True
        return f'告警：{(samples[0].get("summary") or "")[:100]}' if samples else ''
    return ''

def _nms_pick_steps(variants, tenant):
    """取适用当前影响面的 variant 处置步骤（归并侧/影响面未知时取步骤更详细的）"""
    cands = [v for v in (variants or []) if _nms_variant_applies(v.get('sheet', ''), tenant)] or (variants or [])
    best = max(cands, key=lambda v: len(v.get('steps') or '')) if cands else None
    return (best or {}).get('steps', '')

def _nms_build_real_analysis(title, b, tenant, variants, group_name, impact=None,
                             devices=None, checklist_steps=None, tool_checks=None):
    """按 checklist 预案步骤组织的处理分析：概况/级别/时间/影响面 4 行概览 +
    处置核对（预案每条步骤 → 真实数据标注），只给结论不给过程描述。
    tool_checks: 抓取时工具实测结果（如 DNS API 解析测试），注入处置核对做初步分析判断。"""
    pts = []
    ips = sorted(b['ips'])
    dup = b['count'] - len(ips)
    # 1 概况（IP 为主指标去重）
    if ips:
        dup_txt = f'，重复告警 {dup} 条已合并' if dup > 0 else ''
        top = '、'.join(ips[:5]) + (f' 等' if len(ips) > 5 else '')
        pts.append(f'告警概况：{b["count"]} 条按 IP 去重后 {len(ips)} 个设备指标{dup_txt}（{top}）')
    else:
        pts.append(f'告警概况：{b["count"]} 条，未提取到 IP')
    # 2 级别/确认
    sev_txt = '、'.join(f'{_NMS_SEV_LABEL.get(k, k)} {v}'
                        for k, v in sorted(b['severities'].items(),
                                           key=lambda x: _NMS_SEV_PRIORITY.get(x[0], 9)))
    un_txt = f'未确认 {b["unacked"]} 条' if b['unacked'] else '均已确认'
    pts.append(f'级别/确认：{sev_txt}；{un_txt}')
    # 3 时间（含持续时长）
    if b.get('first_time') and b.get('last_time'):
        dur = _nms_time_duration(b['first_time'], b['last_time'])
        pts.append(f'时间：{b["first_time"]} 起' + (f'，已持续 {dur}' if dur else '') + '（持续中）')
    # 4 影响面 + 客户影响分析
    tname = {'platform': '平台侧（多客户共享环境）', 'single': '单客户侧',
             'both': '平台与单客户混合', 'unknown': '未知（告警中未提取到 IP）'}.get(tenant, '未知')
    line = f'影响面：{tname}'
    if impact and (impact.get('vms') or impact.get('customers')):
        vms, cus = impact.get('vms', 0), impact.get('customers') or {}
        if vms or cus:
            names = sorted(cus, key=lambda x: -cus[x])
            if names:
                top_c = '、'.join(f'{c}（{cus[c]} 台）' for c in names[:3])
                line += (f'；客户影响（GCOC资源管理平台）：关联资源 {vms} 台，涉及客户 {len(names)} 家：'
                         f'{top_c}' + (' 等' if len(names) > 3 else ''))
                line += '；判定：' + ('多客户受损，按平台侧处置' if len(names) > 1
                                     else '仅 1 家客户，按单客户侧处置')
            else:
                line += f'；客户影响（GCOC资源管理平台）：关联资源 {vms} 台，未登记客户归属'
    elif tenant != 'unknown':
        line += ('；IP 未关联到 GCOC 虚机/卷映射，客户归属无法自动识别（按告警网段判定单客户侧）'
                 if tenant == 'single' else
                 '；IP 未关联到 GCOC 虚机/卷映射（平台内网段）')
    pts.append(line)
    # 5 处置核对：按预案步骤逐条给出真实数据（解析类步骤优先标注工具实测结果）
    parsed = _nms_parse_steps(checklist_steps) if checklist_steps else []
    if parsed:
        pts.append('处置核对（按预案步骤）：')
        state = {}
        for num, text in parsed:
            ann = _nms_step_annotation(text, b, impact, devices, tool_checks,
                                       state=state)
            short = text if len(text) <= 42 else text[:42] + '…'
            prefix = f'{num}. ' if num else ''
            pts.append(f'　{prefix}{short}' + (f' → {ann}' if ann else ''))
        # 工具实测汇总：初步分析判断结论（仅当本预案有实测结果）
        dns_tests = (tool_checks or {}).get("dns") or []
        if dns_tests:
            n_ok = sum(1 for t in dns_tests if t.get('status') == 'ok')
            n_bad = len(dns_tests) - n_ok
            verdict = '解析均正常' if n_bad == 0 else f'{n_bad} 个目标解析失败/无记录'
            pts.append(f'　工具初步判断（GCOC DNS API 实测）：{len(dns_tests)} 个目标，'
                       f'正常 {n_ok}，{verdict}')
        ssl_tests = (tool_checks or {}).get("ssl") or []
        if ssl_tests:
            # 旧采集实测只读取证书（不校验链/域名），只有 status=ok（剩余达门槛）可视为暂未到期
            oks = [t for t in ssl_tests if t.get('status') == 'ok']
            warns = [t for t in ssl_tests if t.get('status') == 'warn']
            bads = [t for t in ssl_tests if t.get('status') in ('expired', 'fail')]
            days = sorted(t['days_left'] for t in ssl_tests if t.get('days_left') is not None)
            parts = []
            if oks:
                parts.append(f'{len(oks)} 个目标剩余天数达标')
            if warns:
                parts.append(f'{len(warns)} 个目标低于观察门槛')
            if bads:
                parts.append(f'{len(bads)} 个目标已过期/无法核验')
            verdict = '；'.join(p for p in parts if p) or '证据不足'
            if days:
                verdict += f"（剩余 {'/'.join(str(d) for d in days)} 天）"
            verdict += '；仅读取证书，未校验证书链/域名'
            pts.append(f'　工具初步判断（SSL 证书实测）：{verdict}。')
        tcp_tests = (tool_checks or {}).get("tcp") or []
        if tcp_tests:
            n_ok = sum(1 for t in tcp_tests if t.get('status') == 'ok')
            n_bad = len(tcp_tests) - n_ok
            verdict = '端口均可达' if n_bad == 0 else f'{n_bad} 个端口不可达'
            pts.append(f'　工具初步判断（TCP 端口探测）：{len(tcp_tests)} 个目标，'
                       f'可达 {n_ok}，{verdict}')
        bk_tests = (tool_checks or {}).get("backup") or []
        if bk_tests:
            n_att = sum(1 for t in bk_tests if t.get('status') == 'attention')
            rp = max((t.get('latest_rp') or '' for t in bk_tests), key=len)
            verdict = '均正常' if n_att == 0 else f'{n_att} 个目标存在 Failed 作业'
            line = (f'　工具初步判断（备份作业实测）：{len(bk_tests)} 个备份服务器，'
                    f'最近作业结果 {verdict}')
            if rp:
                line += f'；最近还原点 {rp}'
            pts.append(line + '。')
    else:
        seen, samples_txt = set(), []
        for s in b.get('samples') or []:
            sm = (s.get('summary') or '').strip()
            if not sm or sm in seen:
                continue
            seen.add(sm)
            samples_txt.append(sm[:90])
            if len(samples_txt) >= 3:
                break
        if samples_txt:
            pts.append('告警原文：' + ' || '.join(samples_txt))
    return pts

def _nms_nimble_to_device(a, key):
    return {'key': key, 'name': a.get('name', ''), 'hostname': a.get('name', ''),
            'ip': a.get('ip', ''), 'sn': str(a.get('serial_number') or ''),
            'vendor': 'HPE Nimble', 'model': a.get('model') or '未采集',
            'firmware': a.get('firmware_version') or '未采集',
            'site': '', 'status': 'ok'}


def _nms_netapp_to_device(a, key):
    return {'key': key, 'name': a.get('name', ''), 'hostname': a.get('name', ''),
            'ip': a.get('ip', ''), 'sn': str(a.get('serial_number') or ''),
            'vendor': 'NetApp', 'model': a.get('model') or '未采集',
            'firmware': a.get('ontap_version') or '未采集',
            'site': a.get('cluster_name') or '', 'status': 'ok'}

async def _nms_mgt_list_cached(r, ck, path):
    """GCOC-MGT 列表端点全量拉取（Redis 缓存 10 分钟），失败返回 None"""
    try:
        cached = await r.get(ck)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    try:
        from agents.gcoc_api import get_mgt_client
        data = await get_mgt_client().request("GET", path, params={"page_size": 200})
        if isinstance(data, dict) and 'error' not in data:
            items = data.get('results') or []
            await r.set(ck, json.dumps(items, ensure_ascii=False), ex=600)
            return items
    except Exception:
        pass
    return None

def _nms_compellent_to_device(a, key):
    return {'key': key, 'name': a.get('name', ''), 'hostname': a.get('name', ''),
            'ip': a.get('ip', ''), 'sn': str(a.get('serial_number') or ''),
            'vendor': 'Dell Compellent', 'model': a.get('model') or '未采集',
            'firmware': a.get('firmware_version') or '未采集',
            'site': a.get('system_name') or '', 'status': 'ok'}

async def _nms_query_esxi_host(r, name):
    """ESXi 主机精确匹配：GCOC-MGT smartops_vms（host_name 严格相等，IP 不查主机）。
    命中返回主机归属（vCenter/数据中心/集群），无 SN 字段。单主机结果缓存 10 分钟。"""
    ku = name.strip()
    if not ku or _IP4_RE.fullmatch(ku):
        return None
    ck = f"{config.REDIS_PREFIX}:nms:esxi_host:{ku.lower()}"
    try:
        cached = await r.get(ck)
        if cached is not None:
            return json.loads(cached) or None
    except Exception:
        pass
    dev = None
    try:
        from agents.gcoc_api import get_mgt_client
        data = await get_mgt_client().request(
            "GET", "/api/smart-ops/vms/", params={"search": ku, "page_size": 50})
        if isinstance(data, dict) and 'error' not in data:
            for vm in data.get('results') or []:
                if (vm.get('host_name') or '').lower() == ku.lower():
                    loc = ' / '.join(x for x in (vm.get('vcenter_server'),
                                                 vm.get('datacenter'),
                                                 vm.get('cluster')) if x)
                    dev = {'key': name, 'name': vm.get('host_name', ''),
                           'hostname': vm.get('host_name', ''), 'ip': '',
                           'sn': '', 'vendor': 'VMware ESXi', 'model': '未采集',
                           'firmware': '未采集', 'site': loc, 'status': 'ok'}
                    break
    except Exception:
        pass
    try:
        await r.set(ck, json.dumps(dev, ensure_ascii=False) if dev else 'null', ex=600)
    except Exception:
        pass
    return dev

async def _nms_query_cmdb_device(r, key):
    """按 IP/主机名/SN 查询设备资产：走 GCOC-MGT，告警中的 IP 为严格设备标识，
    全链路仅精确匹配，不做模糊关联。数据源链（2026-09 实测端点，
    旧 /api/storage/arrays/ 已 404 下架）：
    compellent-arrays（磁盘故障告警的管理 IP/SN）→ netapp-arrays → nimble 存储
    → vCenter 主机 hosts（厂商/型号/集群）→ smartops_vms 的 ESXi 主机名。
    各源独立容错：单源 404/超时不断路，继续查后续源；全部源不可用才判查询失败。"""
    ku = key.strip()
    _sources_ok = 0
    comps = await _nms_mgt_list_cached(
        r, f"{config.REDIS_PREFIX}:nms:mgt_compellent", "/api/storage/compellent-arrays/")
    if comps is not None:
        _sources_ok += 1
    for a in comps or []:  # Compellent IP/名称/SN 精确
        if (a.get('ip') == ku or (a.get('name') or '').lower() == ku.lower()
                or str(a.get('serial_number') or '').lower() == ku.lower()):
            return _nms_compellent_to_device(a, key)
    netapps = await _nms_mgt_list_cached(
        r, f"{config.REDIS_PREFIX}:nms:mgt_netapp", "/api/storage/netapp-arrays/")
    if netapps is not None:
        _sources_ok += 1
    for a in netapps or []:  # NetApp IP/名称/SN 精确
        if (a.get('ip') == ku or (a.get('name') or '').lower() == ku.lower()
                or str(a.get('serial_number') or '').lower() == ku.lower()):
            return _nms_netapp_to_device(a, key)
    nimbles = await _nms_mgt_list_cached(
        r, f"{config.REDIS_PREFIX}:nms:mgt_nimble", "/api/storage/nimble/")
    if nimbles is not None:
        _sources_ok += 1
    for a in nimbles or []:  # Nimble IP/名称/SN 精确
        if (a.get('ip') == ku or (a.get('name') or '').lower() == ku.lower()
                or str(a.get('serial_number') or '').lower() == ku.lower()):
            return _nms_nimble_to_device(a, key)
    vhosts = await _nms_mgt_list_cached(
        r, f"{config.REDIS_PREFIX}:nms:mgt_vhosts", "/api/vcenter/hosts/")
    if vhosts is not None:
        _sources_ok += 1
    for h in vhosts or []:  # vCenter ESXi 主机 IP/名称精确
        if h.get('ip_address') == ku or (h.get('name') or '').lower() == ku.lower():
            loc = ' / '.join(x for x in (h.get('datacenter'), h.get('cluster')) if x)
            fw = ' / '.join(x for x in (h.get('version'), h.get('build')) if x)
            return {'key': key, 'name': h.get('name', ''), 'hostname': h.get('name', ''),
                    'ip': h.get('ip_address', ''), 'sn': '',
                    'vendor': h.get('manufacturer') or 'VMware ESXi',
                    'model': h.get('model') or '未采集',
                    'firmware': fw or '未采集', 'site': loc, 'status': 'ok'}
    dev = await _nms_query_esxi_host(r, ku)  # smartops_vms 中 ESXi 主机名精确匹配
    if dev:
        return dev
    if _sources_ok == 0:
        return {'key': key, 'status': 'GCOC-MGT 资产查询失败'}
    return {'key': key, 'status': 'not_found'}

async def _nms_lookup_cmdb_devices(r, targets):
    """并发查询设备资产（最多 8 个目标，去重保序）"""
    keys = list(dict.fromkeys(t for t in targets if t))[:8]
    if not keys:
        return []
    return await asyncio.gather(*[_nms_query_cmdb_device(r, k) for k in keys])

def _nms_time_duration(t1, t2):
    """计算告警时间跨度时长（格式 M/D/YY, h:MM AM/PM），失败返回空串"""
    from datetime import datetime
    for fmt in ('%m/%d/%y, %I:%M %p', '%m/%d/%Y, %I:%M %p', '%Y-%m-%d %H:%M:%S'):
        try:
            d = datetime.strptime(t2.strip(), fmt) - datetime.strptime(t1.strip(), fmt)
            if d.total_seconds() < 0:
                return ''
            days = d.days
            h = int(d.total_seconds()) % 86400 // 3600
            mnt = int(d.total_seconds()) % 3600 // 60
            parts = []
            if days:
                parts.append(f'{days}天')
            if h:
                parts.append(f'{h}小时')
            if mnt or not parts:
                parts.append(f'{mnt}分钟')
            return ''.join(parts)
        except ValueError:
            continue
    return ''

async def _nms_vms_by_ip(r, ip):
    """按 guest_ip 精确查 vCenter 虚机（search 接口 + 精确过滤，单 IP 缓存 10 分钟）"""
    ck = f"{config.REDIS_PREFIX}:nms:vms_ip:{ip}"
    try:
        cached = await r.get(ck)
        if cached is not None:
            return json.loads(cached)
    except Exception:
        pass
    vms = []
    try:
        from agents.gcoc_api import get_mgt_client
        data = await get_mgt_client().request(
            "GET", "/api/vcenter/vms/", params={"search": ip, "page_size": 100})
        if isinstance(data, dict) and 'error' not in data:
            for vm in data.get('results') or []:
                if vm.get('guest_ip') == ip:
                    vms.append({'name': vm.get('name', ''),
                                'customer': str(vm.get('customer_name') or vm.get('customer') or ''),
                                'vcenter': vm.get('vcenter_ip') or '',
                                'cluster': vm.get('cluster') or ''})
    except Exception:
        pass
    try:
        await r.set(ck, json.dumps(vms, ensure_ascii=False), ex=600)
    except Exception:
        pass
    return vms

async def _nms_mcp_customer_impact(r, ips):
    """基于 GCOC资源管理平台数据分析受影响客户：
    storage/mappings（storage_array_ip → 卷映射客户）+ vcenter/vms（guest_ip → 虚机客户）。
    返回 {'vms': 关联虚机数, 'customers': {客户: 虚机数}, 'src': 命中数据源说明}"""
    ips = list(dict.fromkeys(i for i in ips if i))[:8]
    if not ips:
        return {'vms': 0, 'customers': {}, 'src': ''}
    customers, vm_count, srcs = {}, 0, []
    maps = await _nms_mgt_list_cached(
        r, f"{config.REDIS_PREFIX}:nms:mgt_mappings", "/api/storage/mappings/")
    hit_arrays = set()
    for m in maps or []:
        if m.get('storage_array_ip') in ips:
            hit_arrays.add(m.get('storage_array_ip'))
            vm_count += 1
            c = str(m.get('company_name') or m.get('customer') or '').strip()
            if c:
                customers[c] = customers.get(c, 0) + 1
    if hit_arrays:
        srcs.append(f'存储卷映射命中 {len(hit_arrays)} 台阵列')
    vm_results = await asyncio.gather(*[_nms_vms_by_ip(r, ip) for ip in ips])
    for vms in vm_results:
        if vms:
            vm_count += len(vms)
            srcs.append(f'vCenter 虚机 {len(vms)} 台')
            for v in vms:
                if v['customer']:
                    customers[v['customer']] = customers.get(v['customer'], 0) + 1
    return {'vms': vm_count, 'customers': customers, 'src': '；'.join(srcs)}

@router.get('/api/nms/groups')
async def nms_get_groups(current_user: str = Depends(verify_token)):
    """返回当前可用的 NMS 告警分组列表（来自最新快照的 group_counts，按数量降序）。
    供组权限配置页将 NMS 默认分组做成可选项，避免手动填写。
    P0: 全局 NMS 快照聚合 (无 ACL 历史) 暂收 admin-only。"""
    from webchat_api import require_admin
    await require_admin(current_user)
    try:
        r = await get_redis()
        latest_raw = await r.get(f"{config.REDIS_PREFIX}:nms_alarms:latest")
        if not latest_raw:
            return {'groups': [], 'group_counts': {}}
        latest = json.loads(latest_raw)
        group_counts = latest.get('group_counts') or {}
        groups = sorted(group_counts.keys(), key=lambda g: -group_counts[g])
        return {'groups': groups, 'group_counts': group_counts}
    except Exception as e:
        return {'groups': [], 'group_counts': {}, 'error': str(e)}

@router.get('/api/nms/snapshot-meta')
async def nms_snapshot_meta(current_user: str = Depends(verify_token)):
    """NMS 快照轻量元信息（供前端「翻页中探测新数据」使用）。

    只读一次 latest 快照的头部字段，不做过滤/排序/分页统计，开销恒定；
    前端比对 timestamp 判定后台是否已完成新一轮采集。同时返回当前生效采集间隔。"""
    try:
        r = await get_redis()
        base, is_personal = await _nms_resolve_owner_async(r, current_user)
        raw = await r.get(f"{base}:latest")
        used_base = base
        if not raw and is_personal:
            used_base = f"{config.REDIS_PREFIX}:nms_alarms"
            raw = await r.get(f"{used_base}:latest")
            is_personal = False
        # 动态配置优先，静态环境变量兜底
        _iv = config.NMS_CHECK_INTERVAL
        try:
            _dyn_raw = await r.get(config.KEY_NMS_DYN_CFG)
            _dyn = json.loads(_dyn_raw) if _dyn_raw else {}
            if isinstance(_dyn, dict) and isinstance(_dyn.get('check_interval'), (int, float)) \
                    and _dyn['check_interval'] >= 60:
                _iv = int(_dyn['check_interval'])
        except Exception:
            pass
        if not raw:
            return {'timestamp': None, 'total': 0, 'seen_count': 0,
                    'check_interval': _iv,
                    'configured': bool(config.NMS_USERNAME and config.NMS_PASSWORD),
                    'is_personal': is_personal, 'pending': True}
        latest = json.loads(raw)
        return {
            'timestamp': latest.get('timestamp'),
            'total': latest.get('total', 0),
            'raw_total': latest.get('raw_total', 0),
            'seen_count': await r.hlen(f"{used_base}:seen"),
            'check_interval': _iv,
            'configured': bool(config.NMS_USERNAME and config.NMS_PASSWORD),
            'is_personal': is_personal,
        }
    except Exception as e:
        return {'timestamp': None, 'total': 0, 'error': str(e)}

@router.get('/api/nms/alarms')
async def nms_get_alarms(groups: str = '', page: int = 1, page_size: int = 0,
                         status: str = 'active',
                         from_ts: str = '', to_ts: str = '',
                         cidr: str = '', alarm_type: str = '',
                         cidr_exclude: str = '', type_exclude: str = '',
                         current_user: str = Depends(verify_token)):
    """最新 NMS 告警快照（按分组过滤 + 分页 + 时间范围 + 维度下钻）。
    - groups: 逗号分隔的分组名，为空表示全部分组；
    - page/page_size: 分页参数（page_size 默认 config.NMS_PAGE_SIZE，上限 NMS_PAGE_SIZE_MAX）；
    - status: 'active' (现存) | 'cleared' (已清除) | 'all' (全部)；
    - from_ts / to_ts: ISO-8601 时间字符串 (Phase 2.1 新增)，按告警的 ``first_time`` 或 ``last_time`` 过滤；
      解析失败时静默忽略。空 = 不限时间。
    - cidr / alarm_type: 维度下钻 (事件视图矩阵点击)，/24 网段与告警类型，复用
      nms_correlation 的同名提取函数，保证与矩阵列/行的 key 完全一致。
      cidr/alarm_type 传 ``__other__`` 表示矩阵「其他」桶（非空且不在
      cidr_exclude / type_exclude 逗号列表内）。
    始终返回完整 group_counts（供分组选择器）与所选范围的 severity_counts（全量，不受分页影响）。"""
    try:
        r = await get_redis()
        base, is_personal = await _nms_resolve_owner_async(r, current_user)
        latest_raw = await r.get(f"{base}:latest")
        used_base = base
        if not latest_raw and is_personal:
            used_base = f"{config.REDIS_PREFIX}:nms_alarms"
            latest_raw = await r.get(f"{used_base}:latest")
            is_personal = False
        configured = bool(config.NMS_USERNAME and config.NMS_PASSWORD)
        seen_count = await r.hlen(f"{used_base}:seen")
        history_count = (await r.zcard(f"{used_base}:history_z")
                         or await r.llen(f"{used_base}:history"))
        analysis_raw = await r.get(f"{used_base}:analysis")
        analysis = json.loads(analysis_raw) if analysis_raw else None
        if not latest_raw:
            return {
                'latest': None, 'analysis': analysis,
                'seen_count': seen_count, 'history_count': history_count,
                'configured': configured, 'is_personal': is_personal,
                'pending': True,
                'message': ('暂无告警快照，监控任务尚未完成首次抓取'
                            if configured else '未配置 NMS 凭据'),
            }
        latest = json.loads(latest_raw)
        # 快照不透明引用（服务端重算校验）：供 SSL 调查创建时防止绑定到错误告警集
        from core.nms_ssl_workflow import snapshot_ref as _ssl_snapshot_ref
        latest['snapshot_ref'] = _ssl_snapshot_ref(current_user, used_base, latest)
        all_alarms = latest.get('alarms', [])
        # Phase 2.1: 时间范围过滤 (按 first_time 或 last_time, 任一落在区间内即命中)
        from_dt = None
        to_dt = None
        if from_ts:
            try:
                from_dt = _dt.fromisoformat(from_ts.replace('Z', '+00:00'))
            except Exception:
                pass
        if to_ts:
            try:
                to_dt = _dt.fromisoformat(to_ts.replace('Z', '+00:00'))
            except Exception:
                pass
        if from_dt or to_dt:
            def _in_range(a):
                ts_str = a.get('last_time') or a.get('first_time') or a.get('time') or ''
                if not ts_str:
                    return False
                try:
                    t = _dt.fromisoformat(ts_str.replace('Z', '+00:00'))
                except Exception:
                    return False
                if from_dt and t < from_dt:
                    return False
                if to_dt and t > to_dt:
                    return False
                return True
            all_alarms = [a for a in all_alarms if _in_range(a)]
        # 从去重后的 all_alarms 重新统计分组计数，确保与“按分组过滤”的结果完全一致
        # （否则面板徒标计数会与表格实际条数不一致）；以快照 group_counts 为底初始化，
        # 保留当前匹配 0 条告警的空视图；同时统计活跃口径计数（与表格默认展示一致）
        group_counts = {g: 0 for g in (latest.get('group_counts') or {})}
        group_counts_active = {g: 0 for g in group_counts}
        for a in all_alarms:
            _is_act = a.get('severity') in _NMS_ACTIVE_SEV
            for g in _nms_groups_of(a):
                group_counts[g] = group_counts.get(g, 0) + 1
                if _is_act:
                    group_counts_active[g] = group_counts_active.get(g, 0) + 1
        # 分组过滤（按 groups[] 归属）
        want = {g.strip() for g in groups.split(',') if g.strip()}
        if want:
            filtered = [a for a in all_alarms if want.intersection(_nms_groups_of(a))]
        else:
            filtered = all_alarms
        # 维度下钻过滤（与事件视图矩阵同函数同口径，避免前后端口径漂移）
        # __other__ = 矩阵「其他」桶：值非空且不在矩阵已列出的 Top7 内
        if cidr or alarm_type:
            from agents.nms_correlation import (alarm_segment as _seg,
                                                alarm_type as _atype)
            _seg_ex = {x.strip() for x in cidr_exclude.split(',') if x.strip()}
            _typ_ex = {x.strip() for x in type_exclude.split(',') if x.strip()}

            def _dim_match(val: str, want: str, exclude: set) -> bool:
                if not want:
                    return True
                if want == '__other__':
                    return bool(val) and val not in exclude
                return val == want

            filtered = [a for a in filtered
                        if _dim_match(_seg(a), cidr, _seg_ex)
                        and _dim_match(_atype(a), alarm_type, _typ_ex)]
        # 拆分 现存(active) / 已清除(cleared)：当前告警只展示现存，已清除归历史告警板块
        active_list = [a for a in filtered if a.get('severity') in _NMS_ACTIVE_SEV]
        cleared_list = [a for a in filtered if a.get('severity') not in _NMS_ACTIVE_SEV]
        active_total = len(active_list)
        cleared_total = len(cleared_list)
        st = (status or 'active').lower()
        if st == 'cleared':
            scoped = cleared_list
        elif st == 'all':
            scoped = filtered
        else:
            scoped = active_list
        # 所选范围级别统计（全量，随 status 范围）
        sev_counts = {}
        for a in scoped:
            s = a.get('severity', 'unknown')
            sev_counts[s] = sev_counts.get(s, 0) + 1
        # 级别优先级排序后分页
        filtered = sorted(scoped, key=lambda a: _NMS_SEV_PRIORITY.get(a.get('severity'), 9))
        psize = page_size or config.NMS_PAGE_SIZE
        psize = max(1, min(int(psize), config.NMS_PAGE_SIZE_MAX))
        pg = max(1, int(page or 1))
        start = (pg - 1) * psize
        page_alarms = filtered[start:start + psize]
        latest['alarms'] = page_alarms
        latest['returned'] = len(page_alarms)
        latest['filtered_total'] = len(filtered)
        latest['page'] = pg
        latest['page_size'] = psize
        latest['has_more'] = (start + psize) < len(filtered)
        latest['severity_counts'] = sev_counts
        latest['group_counts'] = group_counts
        latest['group_counts_active'] = group_counts_active
        latest['active_total'] = active_total
        latest['cleared_total'] = cleared_total
        return {
            'latest': latest, 'analysis': analysis,
            'seen_count': seen_count, 'history_count': history_count,
            'configured': configured, 'is_personal': is_personal,
        }
    except Exception as e:
        return {'latest': None, 'analysis': None, 'seen_count': 0,
                'history_count': 0, 'configured': False, 'error': str(e)}

@router.get('/api/nms/incidents')
async def nms_get_incidents(groups: str = '',
                            current_user: str = Depends(verify_token)):
    """关联降噪后的「事件簇」列表（按 priority 降序）。
    - groups: 逗号分隔的分组名，为空表示全部；按 incident.groups 交集过滤。
    incidents 由 nms_agent 采集时基于当前快照全局计算并存入 {base}:incidents。"""
    try:
        r = await get_redis()
        base, is_personal = await _nms_resolve_owner_async(r, current_user)
        raw = await r.get(f"{base}:incidents")
        used_base = base
        if not raw and is_personal:
            used_base = f"{config.REDIS_PREFIX}:nms_alarms"
            raw = await r.get(f"{used_base}:incidents")
            is_personal = False
        configured = bool(config.NMS_USERNAME and config.NMS_PASSWORD)
        if not raw:
            return {
                'incidents': [], 'incident_count': 0, 'timestamp': None,
                'configured': configured, 'is_personal': is_personal,
                'pending': True,
                'message': ('暂无事件数据，监控任务尚未完成首次抓取'
                            if configured else '未配置 NMS 凭据'),
            }
        payload = json.loads(raw)
        incidents = payload.get('incidents', [])
        want = {g.strip() for g in groups.split(',') if g.strip()}
        if want:
            incidents = [i for i in incidents
                         if want.intersection(set(i.get('groups') or []))]
        # 关联分析口径：单分组优先用该分组的预计算结果，否则用全量
        scope = payload.get('scope_analysis') or None
        scope_scope = ''
        if len(want) == 1:
            g = next(iter(want))
            by_group = payload.get('scope_analysis_by_group') or {}
            if g in by_group:
                scope = by_group[g]
                scope_scope = g
        elif len(want) > 1:
            scope_scope = '全量快照（多分组）'
        return {
            'incidents': incidents,
            'incident_count': len(incidents),
            'total_alarms': payload.get('total_alarms', 0),
            'scope_analysis': scope,
            'scope_analysis_scope': scope_scope,
            'timestamp': payload.get('timestamp'),
            'configured': configured, 'is_personal': is_personal,
        }
    except Exception as e:
        return {'incidents': [], 'incident_count': 0, 'timestamp': None,
                'configured': False, 'error': str(e)}

@router.get('/api/nms/history')
async def nms_get_history(limit: int = 200, page: int = 1, groups: str = '',
                          from_ts: str = '', to_ts: str = '',
                          current_user: str = Depends(verify_token)):
    """历史告警列表（时间倒序，最多 limit 条，上限 1000；page 分页）。

    数据源优先 Phase H2 ZSET ({base}:history_z 内容键去重 + history_data
    payload)。groups: 逗号分隔分组，服务端过滤（命中稀疏时分批扫描，上限 6 万条）；
    from_ts/to_ts: 时间窗口 (ISO, 按事件首见时间过滤, 分数区间直查)；
    个人视图为空时回退全局。"""
    try:
        r = await get_redis()
        base, is_personal = await _nms_resolve_owner_async(r, current_user)
        want_groups = [g for g in (groups or '').split(',') if g.strip()]
        from agents.nms_agent import nms_history_read
        alarms, source, has_more = await nms_history_read(
            r, base, limit, page, want_groups, from_ts, to_ts)
        if not alarms and is_personal:
            alarms, source, has_more = await nms_history_read(
                r, f"{config.REDIS_PREFIX}:nms_alarms", limit, page,
                want_groups, from_ts, to_ts)
        return {'count': len(alarms), 'alarms': alarms, 'source': source,
                'has_more': bool(has_more), 'page': page}
    except Exception as e:
        return {'count': 0, 'alarms': [], 'error': str(e)}

@router.post('/api/nms/refresh')
async def nms_refresh(current_user: str = Depends(verify_token)):
    """通过 Redis pub/sub 触发 nms_agent 立即抓取一次。
    有 NMS SSO 会话的用户以自己的身份抓取 (零密码), 否则触发全局共享抓取。"""
    try:
        r = await get_redis()
        _base, is_personal = await _nms_resolve_owner_async(r, current_user)
        params = {}
        if is_personal:
            params = {'session_user': current_user,
                      'nms_username': current_user}
        msg = AgentMessage(
            source='webchat', target='nms_agent',
            action='nms_alarm_now', username=current_user, params=params,
        )
        await r.publish(config.channel('nms_agent'), msg.to_json())
        scope = '个人视图 (SSO)' if is_personal else '全局共享'
        return {'success': True,
                'message': f'已触发 NMS {scope}抓取，请稍候刷新查看'}
    except Exception as e:
        return {'success': False, 'error': str(e), 'message': f'触发失败: {e}'}

# ────────────────────────────────────────────────────────────────────────────
# NMS SSO 会话 (零密码存储): 用户 AD 登录 Z-OPS 时后台自动建立 Netcool 会话,
# 只存会话 Cookie 于 Redis (密码即弃)。此端点仅查询会话状态, 无任何凭据读写。
# ────────────────────────────────────────────────────────────────────────────
@router.get('/api/nms/session')
async def nms_get_session(current_user: str = Depends(verify_token)):
    """查询当前用户的 NMS SSO 会话状态 (Cookie 是否可用, 不含任何密钥)。"""
    try:
        r = await get_redis()
        raw = await r.get(f"{config.REDIS_PREFIX}:nms_session:{current_user}")
        if not raw:
            return {'success': True, 'has_session': False,
                    'message': '无 NMS 会话 (AD 登录 Z-OPS 后自动建立)'}
        cookies = json.loads(raw)
        expires_max = max((c.get('expires', 0) or 0) for c in cookies) if cookies else 0
        remaining_min = max(0, int((expires_max - time.time()) / 60)) if expires_max else None
        return {'success': True, 'has_session': True,
                'cookie_count': len(cookies),
                'expires_in_min': remaining_min,
                'message': 'NMS 会话有效, "立即抓取"将以你的身份采集'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@router.delete('/api/nms/session')
async def nms_delete_session(current_user: str = Depends(verify_token)):
    """主动注销自己的 NMS 会话 Cookie (回退全局视图)。"""
    try:
        r = await get_redis()
        deleted = await r.delete(
            f"{config.REDIS_PREFIX}:nms_session:{current_user}")
        return {'success': True, 'deleted': bool(deleted)}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@router.get('/api/nms/analysis')
async def nms_get_analysis(group: str = '',
                           current_user: str = Depends(verify_token)):
    """分组维度 LLM 分析。group 为空返回全局分析；否则返回该分组的预算/按需分析。"""
    try:
        r = await get_redis()
        base, is_personal = await _nms_resolve_owner_async(r, current_user)
        used_base = base
        if is_personal and not (await r.exists(f"{base}:latest")):
            used_base = f"{config.REDIS_PREFIX}:nms_alarms"
        g = (group or '').strip()

        async def _with_sev(analysis_obj, want=None):
            """附加当前快照的级别分布（供前端徽标展示），无快照时原样返回。"""
            if not isinstance(analysis_obj, dict):
                return analysis_obj
            sev = {}
            latest_raw = None
            try:
                latest_raw = await r.get(f"{used_base}:latest")
            except Exception:
                pass
            if latest_raw:
                try:
                    latest = json.loads(latest_raw)
                    want = want or set()
                    for a in latest.get('alarms') or []:
                        s = a.get('severity')
                        if s in _NMS_ACTIVE_SEV and (not want or want.intersection(_nms_groups_of(a))):
                            sev[s] = sev.get(s, 0) + 1
                except Exception:
                    pass
            if sev:
                analysis_obj['severity_counts'] = sev
            return analysis_obj

        if g:
            raw = await r.get(f"{used_base}:analysis:group:{g}")
            if raw:
                a = await _with_sev(json.loads(raw), {g})
                return {'analysis': a, 'group': g, 'precomputed': True}
            return {'analysis': None, 'group': g, 'precomputed': False,
                    'message': '该分组暂无分析，可点击“生成分析”按需触发'}
        raw = await r.get(f"{used_base}:analysis")
        a = await _with_sev(json.loads(raw)) if raw else None
        return {'analysis': a, 'group': '', 'precomputed': bool(raw)}
    except Exception as e:
        return {'analysis': None, 'error': str(e)}

@router.get('/api/nms/checklist-plans')
async def nms_get_checklist_plans(current_user: str = Depends(verify_token)):
    """Checklist 预案清单（仅管理员）：全量预案（主步骤/单客户-平台变体）+ 确定性匹配规则
    + 当前活跃告警命中统计，供 NMS 模块「预案清单」页核对维护。"""
    await require_admin(current_user)
    try:
        from agents.nms_agent import _nms_merged_checklist, _NMS_CHECKLIST_RULES
        r = await get_redis()
        entries = await _nms_merged_checklist(r)
        # 覆盖层中的预案标记 origin=custom（新增或编辑过），其余为 xlsx 源
        custom_titles = set(await r.hkeys(config.KEY_NMS_CHECKLIST_PLANS) or [])
        rules_by_title: dict = {}
        for pat, title in _NMS_CHECKLIST_RULES:
            rules_by_title.setdefault(title, []).append(pat)
        plans = []
        for title, ent in sorted(entries.items()):
            plans.append({
                'title': title,
                'sheet': ent.get('sheet', ''),
                'steps': ent.get('steps', ''),
                'variants': ent.get('variants') or [],
                'rules': rules_by_title.get(title, []),
                'origin': 'custom' if title in custom_titles else 'xlsx',
            })
        # 当前活跃告警命中统计（最新全局快照，alarm.checklist 标注）
        hit_stats: dict = {}
        latest_raw = await r.get(f"{config.REDIS_PREFIX}:nms_alarms:latest")
        if latest_raw:
            latest = json.loads(latest_raw)
            for a in latest.get('alarms', []):
                t = a.get('checklist')
                if t:
                    hit_stats[t] = hit_stats.get(t, 0) + 1
        for p in plans:
            p['active_hits'] = hit_stats.get(p['title'], 0)
        # 规则表中存在但预案清单无条目（孤儿规则，提示同步）
        orphan_rules = [pat for pat, title in _NMS_CHECKLIST_RULES if title not in entries]
        return {'plans': plans, 'total': len(plans),
                'active_hit_total': sum(hit_stats.values()),
                'orphan_rules': orphan_rules,
                'source': config.NMS_CHECKLIST_PATH}
    except HTTPException:
        raise
    except Exception as e:
        return {'plans': [], 'total': 0, 'error': str(e)}


async def _nms_checklist_plan_body(body: dict):
    """校验/规范化预案写入体: {steps_customer, steps_platform} → variants 结构。
    至少一侧步骤非空；主份取步骤更详细的一侧。"""
    sc = str(body.get('steps_customer') or '').strip()
    sp = str(body.get('steps_platform') or '').strip()
    if not sc and not sp:
        raise HTTPException(400, '单客户/平台两侧处置步骤至少填写一侧')
    variants = ([{'sheet': '单客户', 'steps': sc}] if sc else []) + \
               ([{'sheet': '平台', 'steps': sp}] if sp else [])
    main = max(variants, key=lambda v: len(v['steps']))
    return {'steps': main['steps'], 'sheet': main['sheet'], 'variants': variants}


@router.post('/api/nms/checklist-plans')
async def nms_add_checklist_plan(body: dict,
                                 current_user: str = Depends(verify_token)):
    """新增自定义 Checklist 预案（仅管理员）：写入 Redis 覆盖层，匹配/分析立即生效。
    匹配规则需在代码 _NMS_CHECKLIST_RULES 维护（防误配需回放验证，不开放 UI 编辑）。"""
    await require_admin(current_user)
    title = str(body.get('title') or '').strip()
    if not title:
        raise HTTPException(400, '预案名称不能为空')
    try:
        from agents.nms_agent import _nms_merged_checklist
        ent = await _nms_checklist_plan_body(body)
        r = await get_redis()
        merged = await _nms_merged_checklist(r)
        if title in merged:
            raise HTTPException(409, f'预案「{title}」已存在，请直接编辑')
        await r.hset(config.KEY_NMS_CHECKLIST_PLANS, title,
                     json.dumps(ent, ensure_ascii=False))
        _log_user_op(current_user, 'nms_checklist_plan_add', f'新增预案: {title}')
        return {'ok': True, 'title': title}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f'保存失败: {e}')


@router.put('/api/nms/checklist-plans/{title}')
async def nms_update_checklist_plan(title: str, body: dict,
                                    current_user: str = Depends(verify_token)):
    """编辑预案（仅管理员）：xlsx 或自定义预案均写入 Redis 覆盖层同名覆盖；
    若此前被移除则同时恢复。"""
    await require_admin(current_user)
    try:
        from agents.nms_agent import _nms_merged_checklist, _nms_load_checklist
        ent = await _nms_checklist_plan_body(body)
        r = await get_redis()
        merged = await _nms_merged_checklist(r)
        # 存在性判定含 xlsx 原始清单：被移除(deleted)的 xlsx 预案仍可编辑同名恢复
        if title not in merged and title not in _nms_load_checklist():
            raise HTTPException(404, f'预案「{title}」不存在')
        pipe = r.pipeline()
        pipe.hset(config.KEY_NMS_CHECKLIST_PLANS, title,
                  json.dumps(ent, ensure_ascii=False))
        pipe.srem(config.KEY_NMS_CHECKLIST_DELETED, title)  # 编辑即恢复被移除的预案
        await pipe.execute()
        _log_user_op(current_user, 'nms_checklist_plan_update', f'编辑预案: {title}')
        return {'ok': True, 'title': title}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f'保存失败: {e}')


@router.delete('/api/nms/checklist-plans/{title}')
async def nms_delete_checklist_plan(title: str,
                                    current_user: str = Depends(verify_token)):
    """移除预案（仅管理员）：自定义预案物理删除覆盖层记录；xlsx 预案加 deleted 标记
    （源文件保留，重新编辑同合预案可恢复），移除后不再参与匹配/分析。"""
    await require_admin(current_user)
    try:
        from agents.nms_agent import _nms_merged_checklist
        r = await get_redis()
        merged = await _nms_merged_checklist(r)
        if title not in merged:
            raise HTTPException(404, f'预案「{title}」不存在')
        await r.hdel(config.KEY_NMS_CHECKLIST_PLANS, title)
        await r.sadd(config.KEY_NMS_CHECKLIST_DELETED, title)
        _log_user_op(current_user, 'nms_checklist_plan_delete', f'移除预案: {title}')
        return {'ok': True, 'title': title}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f'移除失败: {e}')


@router.get('/api/nms/checklist')
async def nms_get_checklist(groups: str = '',
                            current_user: str = Depends(verify_token)):
    """Checklist 预案匹配聚合（以告警为核心的确定性匹配，实时读快照，不依赖 LLM）。
    - groups: 逗号分隔分组名，为空表示全部；
    返回命中预案列表（告警数/级别分布/受影响对象/样本/处置步骤）
    与未匹配告警的分类摘要，供智能分析页结构化展示。"""
    try:
        r = await get_redis()
        base, is_personal = await _nms_resolve_owner_async(r, current_user)
        latest_raw = await r.get(f"{base}:latest")
        if not latest_raw and is_personal:
            latest_raw = await r.get(f"{config.REDIS_PREFIX}:nms_alarms:latest")
        if not latest_raw:
            return {'matches': [], 'matched_count': 0, 'active_count': 0,
                    'unmatched_count': 0, 'unmatched': [],
                    'timestamp': None, 'pending': True}
        latest = json.loads(latest_raw)
        want = {g.strip() for g in groups.split(',') if g.strip()}
        steps_map = latest.get('checklist_steps') or {}
        # 工具实测结果（抓取时写入快照，如 DNS API 解析测试）
        tool_checks = latest.get('tool_checks') or {}
        active = [a for a in latest.get('alarms', [])
                  if a.get('severity') in _NMS_ACTIVE_SEV and
                  (not want or want.intersection(_nms_groups_of(a)))]
        buckets = {}
        buckets_alarms = {}  # title -> 告警列表（供硬件类预案从 summary 提取主机名）
        unmatched = []
        for a in active:
            t = a.get('checklist')
            if not t:
                unmatched.append(a)
                continue
            buckets_alarms.setdefault(t, []).append(a)
            b = buckets.setdefault(t, {'count': 0, 'unacked': 0,
                                       'severities': {}, 'sources': {}, 'ips': set(),
                                       'first_time': '', 'last_time': '', 'samples': []})
            b['count'] += 1
            sv = a.get('severity', 'unknown')
            b['severities'][sv] = b['severities'].get(sv, 0) + 1
            src = (a.get('source') or a.get('node') or '').strip()
            if src:
                b['sources'][src] = b['sources'].get(src, 0) + 1
            b['ips'].update(_nms_alarm_ips(a))
            _ft = (a.get('first_time') or '').strip()
            _lt = (a.get('last_time') or a.get('time') or '').strip()
            if _ft and (not b['first_time'] or _ft < b['first_time']):
                b['first_time'] = _ft
            if _lt and _lt > b['last_time']:
                b['last_time'] = _lt
            if str(a.get('acknowledged') or a.get('status') or '') != '1':
                b['unacked'] += 1
            if len(b['samples']) < 5:
                b['samples'].append({
                    'summary': (a.get('summary') or '')[:160], 'source': src,
                    'ips': _nms_alarm_ips(a),
                    'severity': sv, 'time': a.get('last_time') or a.get('time') or '',
                    'serial': a.get('serial') or '',
                })
        matches = []
        hw_targets = {}   # title -> 资产查询目标列表（硬件类预案异步补设备 SN）
        for t, b in buckets.items():
            top_src = sorted(b['sources'].items(), key=lambda x: -x[1])[:6]
            sm = steps_map.get(t) or {}
            # variants: 单客户/平台两侧处理建议（旧快照无 variants 时回退主份）
            variants = sm.get('variants') or (
                [{'sheet': sm.get('sheet', ''), 'steps': sm['steps']}] if sm.get('steps') else [])
            g = _NMS_PLAN_GROUP_BY_TITLE.get(t) or {}
            tenant = _nms_match_tenant(b)
            matches.append({
                'title': t,
                'sheet': sm.get('sheet', ''),
                'steps': sm.get('steps', ''),
                'variants': variants,
                'group': g.get('key', 'other'),
                'group_name': g.get('name', '其他'),
                'tenant': tenant,
                'ips': sorted(b['ips']),
                'count': b['count'], 'unacked': b['unacked'],
                'severities': b['severities'],
                'sources': [{'name': k, 'count': v} for k, v in top_src],
                'source_total': len(b['sources']),
                'samples': b['samples'],
                'hardware': t in _NMS_HARDWARE_TITLES,
                '_bucket': b,
            })
            if t in _NMS_HARDWARE_TITLES:
                # 资产查询目标: 受影响对象(IP/主机名) + summary 中的大写主机名(如 ARRAY-SN-0001)
                tgts = list(b['sources'].keys())
                for a in buckets_alarms.get(t, []):
                    tgts.extend(_HOSTNAME_RE.findall(a.get('summary') or ''))
                hw_targets[t] = tgts
        # 硬件类预案: 并发查 GCOC-MGT 资产系统补设备 SN（全量数组 Redis 缓存 10min）
        if hw_targets:
            dev_results = await asyncio.gather(
                *[_nms_lookup_cmdb_devices(r, tgts) for tgts in hw_targets.values()])
            dev_map = dict(zip(hw_targets.keys(), dev_results))
            for m in matches:
                if m['hardware']:
                    devs = dev_map.get(m['title']) or []
                    for v in devs:
                        v.setdefault('name', v.get('hostname') or v.get('key'))
                    # 按设备实体去重：同一设备被多个查询目标（如 IP 与主机名）命中时合并为一行
                    merged = {}
                    for v in devs:
                        if v.get('status') == 'ok':
                            ident = v.get('sn') or f"{v.get('name')}|{v.get('ip')}"
                        else:
                            ident = f"miss:{v.get('key')}"
                        if ident in merged:
                            merged[ident]['matched_by'].append(v.get('key'))
                        else:
                            v['matched_by'] = [v.get('key')]
                            merged[ident] = v
                    m['devices'] = list(merged.values())
        # GCOC资源管理平台客户影响分析（按受影响 IP 关联虚机/卷映射客户量区分单客户/平台）
        impacts = await asyncio.gather(
            *[_nms_mcp_customer_impact(r, m['ips']) for m in matches])
        for m, impact in zip(matches, impacts):
            b = m.pop('_bucket')
            # 影响面细化：优先按 GCOC资源管理平台分析的受影响客户量判定单客户/平台，
            # 关联到多家 → 平台侧、仅 1 家 → 单客户侧；未关联客户时保持网段判定
            cus_n = len(impact.get('customers') or {})
            if cus_n > 1:
                m['tenant'] = 'platform'
            elif cus_n == 1:
                m['tenant'] = 'single'
            # 工具实测结果按预案类型过滤注入（避免汇总串到对方预案），并下发前端展开/复制
            _tc = None
            if m['title'] == 'DNS解析异常' and tool_checks.get('dns'):
                _tc = {'dns': tool_checks['dns']}
            elif m['title'] == 'SSL证书过期' and tool_checks.get('ssl'):
                _tc = {'ssl': tool_checks['ssl']}
            elif m['title'] == 'PDNS outage故障' and tool_checks.get('tcp'):
                _tc = {'tcp': tool_checks['tcp']}
            elif m['title'] in ('VBR备份失败', 'A2C VSPC备份失败', 'A2C O365备份失败') \
                    and tool_checks.get('backup'):
                _tc = {'backup': tool_checks['backup']}
            m['tool_checks'] = _tc
            m['analysis'] = _nms_build_real_analysis(
                m['title'], b, m['tenant'], m['variants'], m.get('group_name', ''),
                impact=impact, devices=m.get('devices') or [],
                checklist_steps=_nms_pick_steps(m['variants'], m['tenant']),
                tool_checks=_tc)
        for m in matches:
            m.setdefault('devices', [])
            # variants 增加适用标记（按影响面判定单客户/平台哪侧适用）
            for v in m['variants']:
                v['applies'] = _nms_variant_applies(v.get('sheet', ''), m['tenant'])
        # 最高级别优先、同级别告警数降序
        matches.sort(key=lambda m: (
            min((_NMS_SEV_PRIORITY.get(s, 9) for s in m['severities']), default=9),
            -m['count']))
        # 未匹配告警按 AlertGroup 分类摘要 TOP8（提示规则盲区/人工处置面）
        um_stats = {}
        for a in unmatched:
            k = (a.get('alarm_type') or a.get('event_group') or '').strip() or '其他'
            um_stats[k] = um_stats.get(k, 0) + 1
        um_top = sorted(um_stats.items(), key=lambda x: -x[1])[:8]
        # 按预案群分组：有命中的群优先（按组序），其余归其他
        groups_out = []
        by_g: dict = {}
        for m in matches:
            by_g.setdefault(m['group'], []).append(m)
        for g in _NMS_PLAN_GROUPS:
            ms = by_g.pop(g['key'], None)
            if ms:
                groups_out.append({"key": g["key"], "name": g["name"],
                                   "desc": _nms_group_desc(g, ms),
                                   "matches": ms, "count": sum(x['count'] for x in ms)})
        for key, ms in by_g.items():
            groups_out.append({"key": key, "name": key, "desc": "",
                               "matches": ms, "count": sum(x['count'] for x in ms)})
        return {'matches': matches,
                'groups': groups_out,
                'matched_count': sum(m['count'] for m in matches),
                'active_count': len(active),
                'unmatched_count': len(unmatched),
                'unmatched': [{'category': k, 'count': v} for k, v in um_top],
                'timestamp': latest.get('timestamp')}
    except Exception as e:
        return {'matches': [], 'matched_count': 0, 'active_count': 0,
                'unmatched_count': 0, 'unmatched': [], 'error': str(e)}

@router.post('/api/nms/analyze')
async def nms_analyze(request: Request, current_user: str = Depends(verify_token)):
    """按需触发某分组的 LLM 分析（nms_agent 计算后写入 analysis:group:{group}）。

    Phase 1.2: 接受 ``fresh: true`` / ``force: true`` 强制跳过 TTL 缓存。
    """
    try:
        body = await request.json()
        group = (body.get('group') or '').strip()
        if not group:
            raise HTTPException(400, 'group is required')
        force = bool(body.get('fresh') or body.get('force'))
        r = await get_redis()
        _base, is_personal = await _nms_resolve_owner_async(r, current_user)
        params = {'group': group, 'force': force}
        if is_personal:
            # 分组分析只读快照 (不重抓), 只需 owner 键, 无需会话 Cookie
            params['nms_username'] = current_user
        msg = AgentMessage(
            source='webchat', target='nms_agent',
            action='nms_analyze_group', username=current_user, params=params,
        )
        await r.publish(config.channel('nms_agent'), msg.to_json())
        return {'success': True,
                'message': f'已触发分组「{group}」分析，约1-2分钟后刷新查看'
                           + (' (强制刷新)' if force else '')}
    except HTTPException:
        raise
    except Exception as e:
        return {'success': False, 'error': str(e), 'message': f'触发失败: {e}'}


# ────────────────────────────────────────────────────────────────────────────
# Phase 1.3: SSE 流式分组分析端点
# 客户端发起 GET /api/nms/analyze/stream?group=...&token=...
# 后端订阅 Redis pubsub zops:nms:analyze:stream:{stream_id}, 转发为 SSE chunk。
# nms_agent 通过 on_delta / on_reasoning / on_wait 回调逐 token 推事件到该频道。
# 旧 /api/nms/analyze 仍可作为 fire-and-forget 兼容入口使用。
# ────────────────────────────────────────────────────────────────────────────
@router.get('/api/nms/analyze/stream')
async def nms_analyze_stream(group: str, request: Request,
                              current_user: str = Depends(verify_token)):
    """SSE 流式触发并消费分组 LLM 分析 (Phase 1.3)。"""
    g = (group or '').strip()
    if not g:
        raise HTTPException(400, 'group is required')
    stream_id = _uuid.uuid4().hex
    r = await get_redis()
    _base, is_personal = await _nms_resolve_owner_async(r, current_user)
    params = {'group': g, 'stream_id': stream_id}
    if is_personal:
        params['session_user'] = current_user
        params['nms_username'] = current_user
    msg = AgentMessage(
        source='webchat', target='nms_agent',
        action='nms_analyze_stream', username=current_user, params=params,
    )
    await r.publish(config.channel('nms_agent'), msg.to_json())

    channel = f"{config.REDIS_PREFIX}:nms:analyze:stream:{stream_id}"

    async def _sse():
        pubsub = r.pubsub()
        await pubsub.subscribe(channel)
        # 初始 SSE 注释 (避免代理超时 + 让前端快速确认连接)
        yield ":connected\n\n"
        yield f"event: meta\ndata: {json.dumps({'stream_id': stream_id, 'group': g})}\n\n"
        try:
            deadline = time.time() + 240  # 流式分析最长 4 分钟
            while time.time() < deadline:
                if await request.is_disconnected():
                    break
                try:
                    raw = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                        timeout=2.0)
                except asyncio.TimeoutError:
                    # 心跳保活 (避免 nginx/proxy 30s idle 切断)
                    yield ": ping\n\n"
                    continue
                if not raw:
                    continue
                if raw.get('type') != 'message':
                    continue
                data = raw.get('data')
                if isinstance(data, bytes):
                    data = data.decode('utf-8')
                # 透传任意事件: queued / running / token / reasoning / wait / fallback / done / error
                try:
                    evt = json.loads(data)
                except Exception:
                    evt = {'type': 'raw', 'data': data}
                evt_type = evt.get('type') or 'message'
                # done 事件带完整 analysis, 用 done 事件作为收尾
                yield f"event: {evt_type}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
                if evt_type in ('done', 'error'):
                    break
        finally:
            try:
                await pubsub.unsubscribe(channel)
            except Exception:
                pass
            try:
                await pubsub.close()
            except Exception:
                pass

    return StreamingResponse(_sse(), media_type='text/event-stream',
                            headers={'Cache-Control': 'no-cache',
                                     'X-Accel-Buffering': 'no'})


# ────────────────────────────────────────────────────────────────────────────
# A2 处置闭环 API: 手动触发 + 列表 + 反馈 (无 LLM, 走 orchestrator COMMAND_ROUTES)
# ────────────────────────────────────────────────────────────────────────────
@router.post('/api/nms/remediate')
async def nms_remediate(request: Request,
                        current_user: str = Depends(verify_token)):
    """手动触发 NMS 自动处置 (action schema → orchestrator 精准路由)。"""
    try:
        body = await request.json()
        alarms = body.get('alarms') or []
        if not isinstance(alarms, list) or not alarms:
            return {'success': False, 'error': 'alarms 列表不能为空'}
        # 跨文件: nms_agent 必须已加载 (webchat_api 启动时已 import)
        from agents.nms_agent import NmsAgent
        # nms_agent 是单例 BaseAgent 子类, 取其方法静态调用 (无需实例化)
        actions = NmsAgent._nms_extract_remediation_actions(alarms)
        r = await get_redis()
        from agents.base import AgentMessage
        dispatched = []
        for act in actions:
            msg = AgentMessage(
                source='webchat', target='orchestrator',
                action=act['action_type'], params=act['params'],
                text=f"NMS 手动处置: {act['action_type']} on {act['params']}",
                username=current_user,
            )
            msg.msg_id = f"nms_remediate_{act['action_id']}"
            await r.publish(config.channel('orchestrator'), msg.to_json())
            await r.set(
                f"{config.REDIS_PREFIX}:nms_remediations:{act['action_id']}",
                json.dumps(act, ensure_ascii=False),
                ex=24 * 3600,
            )
            await r.rpush(
                f"{config.REDIS_PREFIX}:nms_remediations:index",
                act['action_id'],
            )
            dispatched.append(act)
        await r.ltrim(f"{config.REDIS_PREFIX}:nms_remediations:index", -500, -1)
        return {'success': True, 'dispatched': dispatched,
                'count': len(dispatched)}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@router.get('/api/nms/remediations')
async def nms_list_remediations(limit: int = 50,
                                current_user: str = Depends(verify_token)):
    """列出最近 N 条处置动作记录 (按时间倒序)。"""
    try:
        r = await get_redis()
        ids = await r.lrange(
            f"{config.REDIS_PREFIX}:nms_remediations:index", -limit, -1)
        # lrange 给出的是正序 [0..limit-1], -50,-1 = 最后 50 个, 需反转
        ids = list(reversed(ids)) if ids else []
        items = []
        for aid in ids:
            raw = await r.get(
                f"{config.REDIS_PREFIX}:nms_remediations:{aid}")
            if raw:
                rec = json.loads(raw)
                items.append(rec)
        return {'success': True, 'items': items, 'count': len(items)}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@router.post('/api/nms/remediations/{action_id}/feedback')
async def nms_remediation_feedback(action_id: str, request: Request,
                                    current_user: str = Depends(verify_token)):
    """手动反馈处置结果 (A4 闭环: outcome 写回, 后续 KB 反哺)。"""
    try:
        body = await request.json()
        outcome = body.get('outcome', '').strip()
        note = body.get('note', '').strip()
        if outcome not in ('采纳', '已解决', '未解决', '误报'):
            return {'success': False,
                    'error': 'outcome 必须为 采纳/已解决/未解决/误报'}
        r = await get_redis()
        key = f"{config.REDIS_PREFIX}:nms_remediations:{action_id}"
        raw = await r.get(key)
        if not raw:
            return {'success': False, 'error': '动作不存在或已过期'}
        rec = json.loads(raw)
        rec['outcome'] = outcome
        rec['outcome_note'] = note
        rec['outcome_by'] = current_user
        rec['outcome_at'] = _dt.now(_dt.now().astimezone().tzinfo).isoformat() \
            if hasattr(_dt, 'astimezone') else _dt.now().isoformat()
        await r.set(key, json.dumps(rec, ensure_ascii=False), ex=7 * 24 * 3600)
        # A4: 写入 experiences 反哺 KB (SelfImprovingEngine 后续消费)
        try:
            await r.rpush(
                f"{config.REDIS_PREFIX}:nms_remediations:outcomes",
                json.dumps({
                    'action_id': action_id,
                    'action_type': rec.get('action_type'),
                    'target': (rec.get('params') or {}).get('target')
                              or (rec.get('params') or {}).get('ip'),
                    'outcome': outcome, 'note': note,
                    'reason': rec.get('reason'),
                    'evidence': rec.get('evidence'),
                    'by': current_user,
                    'at': rec['outcome_at'],
                }, ensure_ascii=False),
            )
            await r.ltrim(
                f"{config.REDIS_PREFIX}:nms_remediations:outcomes",
                -1000, -1,
            )
        except Exception:
            pass
        return {'success': True, 'record': rec}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ────────────────────────────────────────────────────────────────────────────
# A4 处置学习: LLM 分析整体的 outcome 反馈 → KB 反哺
# ────────────────────────────────────────────────────────────────────────────
@router.post('/api/nms/analysis/outcome')
async def nms_analysis_outcome(request: Request,
                                current_user: str = Depends(verify_token)):
    """给某分组最近一次分析报告打 outcome (采纳 / 已解决 / 未解决 / 误报)。

    写入位置:
      - {prefix}:nms_alarms:analysis:group:{group}    → record.outcome (回写)
      - {prefix}:nms_alarms:analysis:outcomes        → 列表 (KB 反哺源)
    """
    try:
        body = await request.json()
        group = (body.get('group') or '').strip()
        outcome = body.get('outcome', '').strip()
        note = body.get('note', '').strip()
        if not group:
            return {'success': False, 'error': 'group 必填'}
        if outcome not in ('采纳', '已解决', '未解决', '误报'):
            return {'success': False,
                    'error': 'outcome 必须为 采纳/已解决/未解决/误报'}
        r = await get_redis()
        key = f"{config.REDIS_PREFIX}:nms_alarms:analysis:group:{group}"
        raw = await r.get(key)
        if not raw:
            return {'success': False, 'error': '该分组尚无分析记录'}
        rec = json.loads(raw)
        rec['outcome'] = outcome
        rec['outcome_note'] = note
        rec['outcome_by'] = current_user
        rec['outcome_at'] = _dt.now().isoformat()
        await r.set(key, json.dumps(rec, ensure_ascii=False))
        # KB 反哺源: outcomes 列表 (self_improving / KB 增强消费)
        await r.rpush(
            f"{config.REDIS_PREFIX}:nms_alarms:analysis:outcomes",
            json.dumps({
                'group': group,
                'outcome': outcome, 'note': note,
                'trigger': rec.get('trigger'),
                'new_count': rec.get('new_count'),
                'total_count': rec.get('total_count'),
                'analysis_excerpt': (rec.get('analysis') or '')[:500],
                'by': current_user,
                'at': rec['outcome_at'],
            }, ensure_ascii=False),
        )
        await r.ltrim(
            f"{config.REDIS_PREFIX}:nms_alarms:analysis:outcomes",
            -1000, -1,
        )
        return {'success': True, 'record': rec}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@router.get('/api/nms/analysis/outcomes')
async def nms_analysis_outcomes(limit: int = 30,
                                current_user: str = Depends(verify_token)):
    """最近 N 条 outcome 记录 (供前端展示历史反馈 / 趋势分析)。"""
    try:
        r = await get_redis()
        raw_list = await r.lrange(
            f"{config.REDIS_PREFIX}:nms_alarms:analysis:outcomes",
            -limit, -1,
        )
        items = [json.loads(x) for x in (raw_list or [])]
        items.reverse()
        # 简明分布
        dist = {}
        for it in items:
            dist[it.get('outcome', '')] = dist.get(it.get('outcome', ''), 0) + 1
        return {'success': True, 'items': items,
                'count': len(items), 'distribution': dist}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ────────────────────────────────────────────────────────────────────────────
# A5 告警抑制管理 API
# ────────────────────────────────────────────────────────────────────────────
def _gen_rule_id(pattern: str) -> str:
    import hashlib
    return "sup_" + hashlib.md5(pattern.encode()).hexdigest()[:10]


@router.get('/api/nms/suppress')
async def nms_list_suppress(current_user: str = Depends(verify_token)):
    """列出当前所有抑制规则 + 维护窗口 (含过期状态判定)。"""
    try:
        r = await get_redis()
        rule_raw_list = await r.hvals(config.KEY_NMS_SUPPRESS_RULES)
        rules = []
        for raw in rule_raw_list or []:
            try:
                rec = json.loads(raw)
                rec['expired'] = bool(rec.get('until_epoch')
                                      and time.time() > float(rec['until_epoch']))
                rules.append(rec)
            except Exception:
                pass
        maint_raw = await r.hgetall(config.KEY_NMS_MAINT_WINDOWS) or {}
        maint = []
        for k, v in maint_raw.items():
            try:
                until = float(v)
                maint.append({'source': k, 'until_epoch': until,
                              'expired': time.time() > until,
                              'remaining_min': max(0, int((until - time.time()) / 60))})
            except Exception:
                pass
        return {'success': True,
                'rules': rules, 'rule_count': len(rules),
                'maint_windows': maint, 'maint_count': len(maint)}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@router.post('/api/nms/suppress')
async def nms_create_suppress(request: Request,
                              current_user: str = Depends(require_admin)):
    """新增抑制规则 (admin)。pattern = 告警摘要正则, max_hits + window_min 控制触发。"""
    try:
        body = await request.json()
        pattern = (body.get('pattern') or '').strip()
        if not pattern:
            return {'success': False, 'error': 'pattern 必填 (正则字符串)'}
        try:
            re.compile(pattern)
        except re.error as e:
            return {'success': False, 'error': f'pattern 不是合法正则: {e}'}
        max_hits = int(body.get('max_hits')
                       or config.NMS_SUPPRESS_DEFAULT_MAX_HITS)
        window_min = int(body.get('window_min')
                         or config.NMS_SUPPRESS_DEFAULT_WINDOW)
        duration_min = int(body.get('duration_min') or window_min)
        reason = (body.get('reason') or '').strip()
        rid = _gen_rule_id(pattern)
        now = time.time()
        rec = {
            'id': rid,
            'pattern': pattern,
            'max_hits': max_hits,
            'window_min': window_min,
            'until_epoch': now + duration_min * 60,
            'created_at': now,
            'created_by': current_user,
            'reason': reason,
        }
        r = await get_redis()
        await r.hset(config.KEY_NMS_SUPPRESS_RULES, rid,
                     json.dumps(rec, ensure_ascii=False))
        return {'success': True, 'rule': rec}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@router.delete('/api/nms/suppress/{rule_id}')
async def nms_delete_suppress(rule_id: str,
                              current_user: str = Depends(require_admin)):
    """一键解除抑制 (admin)。"""
    try:
        r = await get_redis()
        deleted = await r.hdel(config.KEY_NMS_SUPPRESS_RULES, rule_id)
        if not deleted:
            return {'success': False, 'error': '规则不存在或已过期清理'}
        return {'success': True, 'deleted': rule_id}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@router.post('/api/nms/suppress/maint')
async def nms_set_maint(request: Request,
                        current_user: str = Depends(require_admin)):
    """设置维护窗口静音 (admin): source 在 until 之前不推送 critical+ 告警。"""
    try:
        body = await request.json()
        source = (body.get('source') or '').strip()
        duration_min = int(body.get('duration_min') or 60)
        reason = (body.get('reason') or '').strip()
        if not source:
            return {'success': False, 'error': 'source 必填'}
        until = time.time() + duration_min * 60
        r = await get_redis()
        await r.hset(config.KEY_NMS_MAINT_WINDOWS, source, str(until))
        return {'success': True,
                'maint': {'source': source, 'until_epoch': until,
                          'reason': reason, 'set_by': current_user}}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@router.delete('/api/nms/suppress/maint/{source}')
async def nms_delete_maint(source: str,
                            current_user: str = Depends(require_admin)):
    """一键解除维护窗口 (admin)。"""
    try:
        r = await get_redis()
        deleted = await r.hdel(config.KEY_NMS_MAINT_WINDOWS, source)
        if not deleted:
            return {'success': False, 'error': '维护窗口不存在'}
        return {'success': True, 'deleted': source}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# NMS SSL 证书调查闭环（告警 → 只读取证 → 人工处置 → 自动恢复核验 → 案例）
# 业务逻辑全部在 core/nms_ssl_workflow.py，本段只做认证、快照校验与错误映射。
# ═══════════════════════════════════════════════════════════════════════════

def _ssl_error(status: int, code: str, message: str, retryable: bool = False):
    from fastapi import HTTPException
    raise HTTPException(status_code=status,
                        detail={'code': code, 'message': message, 'retryable': retryable})


async def _ssl_call(coro):
    """把工作流异常映射为统一错误体 code/message/retryable。"""
    from core.nms_ssl_workflow import WorkflowError
    try:
        return await coro
    except WorkflowError as e:
        _ssl_error(e.status, e.code, e.message, e.retryable)


async def _ssl_current_user(current_user: str) -> dict:
    r = await get_redis()
    raw = await r.hget(config.KEY_USERS, current_user)
    if not raw:
        _ssl_error(401, 'user_not_found', '用户不存在或已失效')
    return json.loads(raw)


async def _nms_ssl_load_snapshot(r, current_user: str):
    """读取当前用户实际生效的快照（个人优先，缺失回退全局），返回 (base, latest)。"""
    base, is_personal = await _nms_resolve_owner_async(r, current_user)
    raw = await r.get(f"{base}:latest")
    used_base = base
    if not raw and is_personal:
        used_base = f"{config.REDIS_PREFIX}:nms_alarms"
        raw = await r.get(f"{used_base}:latest")
    return used_base, (json.loads(raw) if raw else None)


async def _ssl_idempotent(owner: str, key: str, producer):
    """同 key 同载荷重放原响应；不缓存异常。"""
    from core.nms_ssl_workflow import get_store
    if not key:
        return await producer()
    store = await get_store()
    cached = await store.get_idem(owner, key)
    if cached:
        return json.loads(cached)
    result = await producer()
    await store.set_idem(owner, key, json.dumps(result, ensure_ascii=False))
    return result


def _ssl_is_ssl_alarm(alarm: dict) -> bool:
    if (alarm.get('checklist') or '') == 'SSL证书过期':
        return True
    low = ((alarm.get('summary') or '') + ' ' + ((alarm.get('fields') or {}).get('Summary') or '')).lower()
    return ('ssl' in low or '证书' in low) and 'position' in low


@router.post('/api/nms/ssl-investigations')
async def nms_ssl_create(body: dict, response: Response,
                         current_user: str = Depends(verify_token)):
    """创建（或幂等复用）一条 SSL 证书调查。新建 202，复用 200。"""
    from core.nms_ssl_workflow import create_investigation
    if not getattr(config, 'NMS_SSL_WORKFLOW_ENABLED', True):
        _ssl_error(503, 'workflow_disabled', 'SSL 调查闭环已停用')
    alarm_id = str(body.get('alarm_id') or '').strip()
    ts = str(body.get('snapshot_timestamp') or '').strip()
    ref_in = str(body.get('snapshot_ref') or '').strip()
    if not alarm_id or not ts or not ref_in:
        _ssl_error(422, 'invalid_request', 'alarm_id / snapshot_timestamp / snapshot_ref 必填')
    r = await get_redis()
    used_base, latest = await _nms_ssl_load_snapshot(r, current_user)
    if not latest:
        _ssl_error(409, 'snapshot_missing', '暂无告警快照，请等待本轮采集完成', retryable=True)
    if str(latest.get('timestamp') or '') != ts:
        _ssl_error(409, 'snapshot_stale', '快照已更新，请刷新告警后重试', retryable=True)
    from core.nms_ssl_workflow import snapshot_ref as _ssl_ref
    if _ssl_ref(current_user, used_base, latest) != ref_in:
        _ssl_error(409, 'snapshot_mismatch', '快照校验失败，请刷新后重试', retryable=True)
    alarm = next((a for a in (latest.get('alarms') or [])
                  if str(a.get('alarm_id')) == alarm_id), None)
    if not alarm:
        _ssl_error(404, 'alarm_not_found', '告警不在当前快照中')
    if alarm.get('severity') not in _NMS_ACTIVE_SEV:
        _ssl_error(409, 'alarm_not_active', '该告警已不在活跃状态')
    if not _ssl_is_ssl_alarm(alarm):
        _ssl_error(422, 'not_ssl_alarm', '仅 SSL 证书告警可发起证书调查')

    inv, created = await _ssl_call(create_investigation(
        owner=current_user, scope_base=used_base, alarm=alarm,
        incident_id=str(body.get('incident_id') or ''),
        snapshot_timestamp=ts, snapshot_ref_value=ref_in,
        idempotency_key=str(body.get('idempotency_key') or '')))
    response.status_code = 202 if created else 200
    return {'investigation': inv, 'created': created}


@router.get('/api/nms/ssl-investigations')
async def nms_ssl_list(cursor: str = '', limit: int = 50,
                       current_user: str = Depends(verify_token)):
    """我的调查列表（游标分页）；他人记录不可见。"""
    from core.nms_ssl_workflow import list_investigations
    limit = max(1, min(int(limit or 50), 100))
    return await _ssl_call(list_investigations(current_user, cursor, limit))


@router.get('/api/nms/ssl-investigations/{inv_id}')
async def nms_ssl_detail(inv_id: str, current_user: str = Depends(verify_token)):
    from core.nms_ssl_workflow import get_investigation
    return await _ssl_call(get_investigation(current_user, inv_id))


@router.post('/api/nms/ssl-investigations/{inv_id}/recheck')
async def nms_ssl_recheck(inv_id: str, body: dict = None,
                          current_user: str = Depends(verify_token)):
    from core.nms_ssl_workflow import recheck
    body = body or {}
    return await _ssl_call(_ssl_idempotent(
        current_user, f"recheck:{inv_id}:{body.get('idempotency_key') or ''}",
        lambda: recheck(current_user, inv_id)))


@router.post('/api/nms/ssl-investigations/{inv_id}/manual-completed')
async def nms_ssl_manual_completed(inv_id: str, body: dict = None,
                                   current_user: str = Depends(verify_token)):
    from core.nms_ssl_workflow import manual_completed
    body = body or {}
    category = str(body.get('category') or '')
    note = str(body.get('note') or '')
    expected = body.get('expected_version')
    return await _ssl_call(_ssl_idempotent(
        current_user, f"manual:{inv_id}:{body.get('idempotency_key') or ''}",
        lambda: manual_completed(current_user, inv_id, category, note,
                                 int(expected) if expected is not None else None)))


@router.post('/api/nms/ssl-investigations/{inv_id}/pause')
async def nms_ssl_pause(inv_id: str, body: dict = None,
                        current_user: str = Depends(verify_token)):
    from core.nms_ssl_workflow import pause_investigation
    body = body or {}
    return await _ssl_call(pause_investigation(
        current_user, inv_id, str(body.get('reason') or '')))


@router.post('/api/nms/ssl-investigations/{inv_id}/resume')
async def nms_ssl_resume(inv_id: str, current_user: str = Depends(verify_token)):
    from core.nms_ssl_workflow import resume_investigation
    return await _ssl_call(resume_investigation(current_user, inv_id))


@router.post('/api/nms/ssl-investigations/{inv_id}/case')
async def nms_ssl_submit_case(inv_id: str, body: dict = None,
                              current_user: str = Depends(verify_token)):
    from core.nms_ssl_workflow import submit_case
    body = body or {}
    return await _ssl_call(submit_case(
        current_user, inv_id, str(body.get('root_cause') or ''),
        str(body.get('action') or ''), str(body.get('conditions') or '')))


@router.post('/api/nms/ssl-investigations/{inv_id}/feedback')
async def nms_ssl_feedback(inv_id: str, body: dict = None,
                           current_user: str = Depends(verify_token)):
    from core.nms_ssl_workflow import add_feedback
    body = body or {}
    return await _ssl_call(add_feedback(
        current_user, inv_id, bool(body.get('helpful')), str(body.get('note') or '')))


@router.get('/api/nms/ssl-cases')
async def nms_ssl_cases(status: str = '',
                        current_user: str = Depends(verify_token)):
    from core.nms_ssl_workflow import list_cases
    user = await _ssl_current_user(current_user)
    return await _ssl_call(list_cases(current_user, user, status))


@router.get('/api/nms/ssl-cases/{case_id}')
async def nms_ssl_case_detail(case_id: str,
                              current_user: str = Depends(verify_token)):
    from core.nms_ssl_workflow import get_case
    user = await _ssl_current_user(current_user)
    return await _ssl_call(get_case(current_user, user, case_id))


@router.post('/api/nms/ssl-cases/{case_id}/review')
async def nms_ssl_case_review(case_id: str, body: dict = None,
                              current_user: str = Depends(verify_token)):
    from core.nms_ssl_workflow import review_case
    await require_admin(current_user)
    user = await _ssl_current_user(current_user)
    body = body or {}
    return await _ssl_call(review_case(
        current_user, user, case_id, str(body.get('action') or ''),
        str(body.get('note') or '')))


@router.post('/api/nms/ssl-cases/{case_id}/revoke')
async def nms_ssl_case_revoke(case_id: str, body: dict = None,
                              current_user: str = Depends(verify_token)):
    from core.nms_ssl_workflow import revoke_case
    user = await _ssl_current_user(current_user)
    body = body or {}
    return await _ssl_call(revoke_case(
        current_user, user, case_id, str(body.get('note') or '')))


@router.get('/api/nms/ssl-metrics')
async def nms_ssl_metrics(current_user: str = Depends(verify_token)):
    """闭环运行指标（只读，管理员）。

    进程内指标来自本 backend 进程（租约/探测/调度/等待/恢复），
    采集计数来自 Redis（由 nms_agent 写入），两者合并返回。
    不返回任何告警内容或凭据，仅供灰度评估与排障。
    """
    from core.nms_ssl_workflow import metrics_report
    await require_admin(current_user)
    return await _ssl_call(metrics_report(await get_redis()))


@router.get('/api/nms/closure-coverage')
async def nms_closure_coverage(top: int = 15,
                               current_user: str = Depends(verify_token)):
    """闭环覆盖度（只读，管理员）：按探针类型/分层统计当前活跃告警。

    回答「SSL 之外先做哪类告警闭环」：返回 L1/L2/L3 分层、按类型与
    event_group 的量、以及推荐顺序。不返回告警原文，不触发任何处置。
    """
    from core.alert_closure import coverage_report
    await require_admin(current_user)
    _, latest = await _nms_ssl_load_snapshot(await get_redis(), current_user)
    if not latest:
        _ssl_error(409, 'snapshot_missing', '暂无告警快照，请等待本轮采集完成', retryable=True)
    return coverage_report(latest, max(1, min(int(top or 15), 50)))


# ═══════════════════════════════════════════════════════════════════════════
# 通用告警闭环（L1 探针闭环 / L2 设备级簇闭环）
# 业务逻辑在 core/alert_closure_engine.py：单元聚合、状态机、恢复核验、案例。
# 本段只做认证、参数校验与错误映射；写操作限管理员，读操作为管理员审计视图。
# ═══════════════════════════════════════════════════════════════════════════

async def _closure_call(coro):
    """把闭环引擎异常映射为统一错误体 code/message/retryable。"""
    from core.alert_closure_engine import ClosureError
    try:
        return await coro
    except ClosureError as e:
        _ssl_error(e.status, e.code, e.message, e.retryable)


@router.get('/api/nms/closure/summary')
async def nms_closure_summary(current_user: str = Depends(verify_token)):
    """闭环总览（只读，管理员）：单元状态分布、跟踪数、阈值与运行指标。"""
    from core.alert_closure_engine import stats
    await require_admin(current_user)
    return await _closure_call(stats())


@router.get('/api/nms/closure/units')
async def nms_closure_units(status: str = '', tier: str = '', kind: str = '',
                            q: str = '', limit: int = 50,
                            current_user: str = Depends(verify_token)):
    """故障单元列表（只读，管理员）：status 支持逗号分隔多值。"""
    from core.alert_closure_engine import list_units
    await require_admin(current_user)
    return await _closure_call(list_units(status, tier, kind, q, limit))


@router.get('/api/nms/closure/cases')
async def nms_closure_cases(limit: int = 50, kind: str = '',
                            current_user: str = Depends(verify_token)):
    """案例库（只读，管理员）。"""
    from core.alert_closure_engine import list_cases
    await require_admin(current_user)
    return await _closure_call(list_cases(limit, kind))


@router.post('/api/nms/closure/refresh')
async def nms_closure_refresh(current_user: str = Depends(verify_token)):
    """立即刷新单元并推进恢复核验（管理员；与后台 worker 同一逻辑）。"""
    from core.alert_closure_engine import run_once
    await require_admin(current_user)
    return await _closure_call(run_once())


@router.post('/api/nms/closure/units/{key:path}/confirm')
async def nms_closure_confirm(key: str, request: Request,
                              current_user: str = Depends(verify_token)):
    """确认根因并进入自动恢复核验（管理员）。不会 ACK/屏蔽任何告警。"""
    from core.alert_closure_engine import confirm_unit
    await require_admin(current_user)
    body = await request.json()
    return await _closure_call(confirm_unit(
        key, current_user, str(body.get('root_cause') or ''),
        str(body.get('note') or '')))


@router.post('/api/nms/closure/units/{key:path}/pause')
async def nms_closure_pause(key: str, request: Request,
                            current_user: str = Depends(verify_token)):
    """暂停跟踪（管理员）：暂停后不再推进恢复判定。"""
    from core.alert_closure_engine import pause_unit
    await require_admin(current_user)
    body = await request.json()
    return await _closure_call(pause_unit(key, current_user, str(body.get('note') or '')))


@router.post('/api/nms/closure/units/{key:path}/resume')
async def nms_closure_resume(key: str, current_user: str = Depends(verify_token)):
    """恢复跟踪（管理员）。"""
    from core.alert_closure_engine import resume_unit
    await require_admin(current_user)
    return await _closure_call(resume_unit(key, current_user))


@router.post('/api/nms/closure/units/{key:path}/reopen')
async def nms_closure_reopen(key: str, request: Request,
                             current_user: str = Depends(verify_token)):
    """重新打开已恢复/已过期单元（管理员）：需要新的处置依据。"""
    from core.alert_closure_engine import reopen_unit
    await require_admin(current_user)
    body = await request.json()
    return await _closure_call(reopen_unit(key, current_user, str(body.get('note') or '')))


@router.post('/api/nms/closure/units/{key:path}/recheck')
async def nms_closure_recheck(key: str, current_user: str = Depends(verify_token)):
    """按需核验（管理员）：L1 立即探测、L2 用最近快照重算，不写告警。"""
    from core.alert_closure_engine import recheck_unit
    await require_admin(current_user)
    return await _closure_call(recheck_unit(key))


@router.post('/api/nms/closure/units/{key:path}/case')
async def nms_closure_case(key: str, request: Request,
                           current_user: str = Depends(verify_token)):
    """归档案例（管理员）：仅已恢复单元；记录根因/处置/预防措施。"""
    from core.alert_closure_engine import record_case
    await require_admin(current_user)
    body = await request.json()
    return await _closure_call(record_case(
        key, current_user, str(body.get('resolution') or ''),
        str(body.get('prevention') or ''), str(body.get('root_cause') or '')))


@router.get('/api/nms/closure/units/{key:path}')
async def nms_closure_unit_detail(key: str, current_user: str = Depends(verify_token)):
    """单元详情（只读，管理员）：单元、最近事件与关联案例。"""
    from core.alert_closure_engine import get_unit_detail
    await require_admin(current_user)
    return await _closure_call(get_unit_detail(key))
