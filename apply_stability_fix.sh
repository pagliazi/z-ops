#!/bin/bash
# Z-OPS WebChat 服务稳定性修复补丁

echo "🔧 Z-OPS WebChat 服务稳定性修复..."

# 1. 备份原文件
echo "📦 备份原文件..."
cp /opt/zops/webchat_api.py /opt/zops/webchat_api.py.backup.$(date +%Y%m%d_%H%M%S)

# 2. 创建修复后的 webchat_api.py
cat > /tmp/webchat_stability_patch.py << 'PYTHON_PATCH'

# 在 webchat_api.py 的日志配置后添加 (约第 36 行后)：

# 添加异常处理器
def global_exception_handler(exctype, value):
    logger.error(f"未捕获的异常：{exctype.__name__}: {value}", exc_info=True)

import sys
sys.excepthook = global_exception_handler

# 替换静态文件服务部分 (约第 248-253 行)：

# ═══ 静态文件服务 ═══
# 挂载静态文件目录 (CSS/JS/Components)
try:
    frontend_static_dir = config.BASE_DIR / "webchat_frontend"
    if frontend_static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_static_dir)), name="static")
        logger.info(f"[Frontend] ✓ 静态文件目录：{frontend_static_dir}")
    else:
        logger.warning(f"[Frontend] ⚠ 静态文件目录不存在：{frontend_static_dir}，将使用降级模式")
except Exception as e:
    logger.error(f"[Frontend] ✗ 静态文件挂载失败：{e}")

# 替换主页函数 (约第 256-268 行)：

# ═══ 主页 ═══
@app.get("/", response_class=HTMLResponse)
async def index():
    try:
        # 优先使用新的模块化前端
        index_path = config.BASE_DIR / "webchat_frontend" / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        
        # 降级使用旧版单文件前端
        frontend_path = config.BASE_DIR / "webchat_frontend.html"
        if frontend_path.exists():
            return HTMLResponse(frontend_path.read_text(encoding="utf-8"))
        
        logger.error("[Frontend] 前端文件未找到")
        return HTMLResponse("<h1>Z-OPS 运维智能体</h1><p style='color:red'>前端文件未找到，请检查部署</p>")
    except Exception as e:
        logger.error(f"[Frontend] 主页访问错误：{e}", exc_info=True)
        return HTMLResponse(f"<h1>服务错误</h1><p>{str(e)}</p>", status_code=500)

# 在 overview 函数添加异常处理 (约第 813-846 行)：

@app.get("/api/overview")
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

        usage_count = await r.llen(config.KEY_LLM_USAGE)
        # 读取 Redis 动态配置，优先于 .env 静态值
        pref_raw = await r.get(config.KEY_LLM_PREF)
        pref = json.loads(pref_raw) if pref_raw else {}
        cur_provider = pref.get("provider") or config.LLM_PROVIDER
        cur_model = pref.get("model") or config.LLM_MODEL
        return {
            "app": config.APP_NAME, "description": config.APP_DESC,
            "agents": agents, "channels": channels,
            "llm_provider": cur_provider, "llm_model": cur_model,
            "llm_usage_count": usage_count, "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"[API] overview 错误：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取系统概览失败：{str(e)}")

PYTHON_PATCH

echo "✅ 修复补丁已生成"
echo ""
echo "📝 请手动应用以下修复："
echo "1. 全局异常处理器 - 添加到日志配置后"
echo "2. 静态文件服务 - 添加 try-except 包裹"
echo "3. 主页函数 - 添加完整的异常处理"
echo "4. overview API - 添加异常处理和日志记录"
echo ""
echo "或者运行以下命令自动应用补丁："
echo "  cd /opt/zops && python apply_stability_patch.py"
