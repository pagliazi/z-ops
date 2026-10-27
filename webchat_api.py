"""
Z-OPS 运维智能体 — Web Dashboard API
FastAPI + uvicorn + SSE 流式
支持: 用户认证 / 会话管理 / 对话持久化 / 日志按会话分组
"""

import asyncio
import json
import time
import uuid
import hashlib
import logging
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import secrets
import os

import config
from agents.base import AgentMessage, AgentReply
from core.session_engine import get_session_engine

logger = logging.getLogger("zops")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [webchat] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# 添加异常处理器
def global_exception_handler(exctype, value):
    logger.error(f"未捕获的异常：{exctype.__name__}: {value}", exc_info=True)

import sys
sys.excepthook = global_exception_handler

# ═══ Redis ═══
_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(config.REDIS_URL, decode_responses=True, max_connections=100)
    return _redis


# ═══ 密码工具 ═══
def hash_password(password: str) -> str:
    return hashlib.sha256(f"zops_salt_{password}_{config.JWT_SECRET}".encode()).hexdigest()


# ═══ LDAP 认证 ═══
def ldap_authenticate(username: str, password: str) -> dict | None:
    """
    通过 LDAP/AD 验证用户，成功返回用户信息 dict，失败返回 None。
    使用 ldap3 库，流程:
      1. 用服务账号 bind 到 AD
      2. 搜索用户 DN (sAMAccountName)
      3. 用用户 DN + 用户密码 rebind 验证
      4. 返回 displayName / mail 等属性
    """
    if not config.LDAP_ENABLED:
        return None
    try:
        from ldap3 import Server, Connection, ALL, SUBTREE
    except ImportError:
        logger.warning("[LDAP] ldap3 未安装，跳过 LDAP 认证")
        return None

    try:
        server = Server(config.LDAP_SERVER_URI, get_info=ALL, connect_timeout=10)

        # 1) 服务账号绑定
        svc_conn = Connection(server, user=config.LDAP_BIND_DN,
                              password=config.LDAP_BIND_PASSWORD, auto_bind=True,
                              read_only=True, receive_timeout=10)

        # 2) 搜索用户
        search_filter = config.LDAP_SEARCH_FILTER.replace("%(user)s", username)
        svc_conn.search(
            search_base=config.LDAP_SEARCH_BASE,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=["cn", "displayName", "mail", "sAMAccountName", "distinguishedName"],
        )

        if not svc_conn.entries:
            logger.info(f"[LDAP] 用户 '{username}' 在 AD 中未找到")
            svc_conn.unbind()
            return None

        entry = svc_conn.entries[0]
        user_dn = str(entry.entry_dn)
        display_name = str(entry.displayName) if hasattr(entry, "displayName") and entry.displayName else username
        mail = str(entry.mail) if hasattr(entry, "mail") and entry.mail else ""
        svc_conn.unbind()

        # 3) 用用户 DN + 密码验证
        user_conn = Connection(server, user=user_dn, password=password,
                               auto_bind=False, receive_timeout=10)
        if not user_conn.bind():
            logger.info(f"[LDAP] 用户 '{username}' 密码验证失败")
            return None
        user_conn.unbind()

        logger.info(f"[LDAP] 用户 '{username}' (DN={user_dn}) 认证成功")
        return {
            "username": username,
            "display_name": display_name,
            "mail": mail,
            "dn": user_dn,
            "auth_source": "ldap",
        }

    except Exception as e:
        logger.warning(f"[LDAP] 认证异常: {e}")
        return None


def _ldap_escape(s: str) -> str:
    """转义 LDAP 过滤器特殊字符，防止注入"""
    return (s.replace("\\", "\\5c").replace("*", "\\2a")
             .replace("(", "\\28").replace(")", "\\29").replace("\x00", "\\00"))


def ldap_search_users(keyword: str = "", limit: int = 20) -> list:
    """从 LDAP/AD 搜索用户信息 (服务账号只读绑定)，用于通知用户组成员选择"""
    if not config.LDAP_ENABLED:
        return []
    try:
        from ldap3 import Server, Connection, ALL, SUBTREE
    except ImportError:
        logger.warning("[LDAP] ldap3 未安装，无法搜索用户")
        return []
    try:
        server = Server(config.LDAP_SERVER_URI, get_info=ALL, connect_timeout=10)
        conn = Connection(server, user=config.LDAP_BIND_DN,
                          password=config.LDAP_BIND_PASSWORD, auto_bind=True,
                          read_only=True, receive_timeout=10)
        kw = _ldap_escape(keyword.strip())
        if kw:
            search_filter = (f"(&(objectClass=user)(objectCategory=person)"
                             f"(|(sAMAccountName=*{kw}*)(displayName=*{kw}*)"
                             f"(mail=*{kw}*)(cn=*{kw}*)))")
        else:
            search_filter = "(&(objectClass=user)(objectCategory=person))"
        conn.search(
            search_base=config.LDAP_SEARCH_BASE,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=["cn", "displayName", "mail", "sAMAccountName"],
            size_limit=max(1, min(limit, 100)),
        )
        users = []
        for e in conn.entries:
            uname = str(e.sAMAccountName) if hasattr(e, "sAMAccountName") and e.sAMAccountName else str(e.cn)
            disp = str(e.displayName) if hasattr(e, "displayName") and e.displayName else uname
            mail = str(e.mail) if hasattr(e, "mail") and e.mail else ""
            users.append({"username": uname, "display_name": disp, "mail": mail, "source": "ldap"})
        conn.unbind()
        return users
    except Exception as e:
        logger.warning(f"[LDAP] 用户搜索异常: {e}")
        return []


# ═══ 邮件通知 ═══
def send_notify_email(subject: str, body: str, to_email: str = "") -> dict:
    """发送通知邮件 (同步版，返回结果; 可在线程中调用)"""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.utils import formatdate

    to_addr = to_email or config.ADMIN_NOTIFY_EMAIL
    if not to_addr:
        logger.warning("[MAIL] 未配置收件人邮箱，跳过发送")
        return {"success": False, "error": "未配置收件人邮箱"}

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = config.SMTP_FROM
        msg["To"] = to_addr
        msg["Date"] = formatdate(localtime=True)
        msg.attach(MIMEText(body, "html", "utf-8"))

        smtp_host = config.SMTP_HOST
        smtp_port = config.SMTP_PORT

        # MX 直投模式: 根据收件人域名查找 MX 记录
        if config.SMTP_MX_DIRECT:
            domain = to_addr.split("@")[-1]
            mx_host = _resolve_mx(domain)
            if mx_host:
                smtp_host = mx_host
                logger.info(f"[MAIL] MX 直投: {domain} -> {mx_host}")

        smtp = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
        ehlo_domain = getattr(config, 'SMTP_EHLO_DOMAIN', 'gcoc-ai.example.com')
        smtp.ehlo(ehlo_domain)
        if config.SMTP_USE_TLS:
            smtp.starttls()
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.sendmail(config.SMTP_FROM, [to_addr], msg.as_string())
        smtp.quit()
        logger.info(f"[MAIL] 通知邮件已发送: {subject} -> {to_addr}")
        return {"success": True, "to": to_addr, "subject": subject}
    except Exception as e:
        logger.warning(f"[MAIL] 发送失败: {e}")
        return {"success": False, "error": str(e)}


def send_notify_email_async(subject: str, body: str, to_email: str = ""):
    """异步发送 (不阻塞)"""
    import threading
    threading.Thread(target=send_notify_email, args=(subject, body, to_email), daemon=True).start()


def _resolve_mx(domain: str) -> str:
    """解析域名的 MX 记录，返回优先级最高的 MX 主机"""
    import subprocess
    try:
        result = subprocess.run(
            ['dig', '+short', 'MX', domain],
            capture_output=True, text=True, timeout=5
        )
        lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
        if lines:
            # 按优先级排序，取最低值 (最高优先级)
            entries = []
            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    entries.append((int(parts[0]), parts[1].rstrip('.')))
            entries.sort(key=lambda x: x[0])
            if entries:
                return entries[0][1]
    except Exception as e:
        logger.debug(f"[MAIL] MX 解析失败: {domain}: {e}")
    return ""


# ═══ 初始化默认管理员 ═══
async def ensure_admin_user():
    r = await get_redis()
    # 本地管理员
    existing = await r.hget(config.KEY_USERS, config.DEFAULT_ADMIN_USER)
    if not existing:
        user_data = {
            "username": config.DEFAULT_ADMIN_USER,
            "password_hash": hash_password(config.DEFAULT_ADMIN_PASS),
            "display_name": "管理员",
            "role": "admin",
            "auth_source": "local",
            "created_at": time.time()
        }
        await r.hset(config.KEY_USERS, config.DEFAULT_ADMIN_USER, json.dumps(user_data, ensure_ascii=False))
        logger.info(f"已创建默认管理员账户: {config.DEFAULT_ADMIN_USER}")

    # 预置 AD 管理员账号 (如 svc-gcoc)
    for ldap_admin in config.LDAP_ADMIN_USERS.split(","):
        ldap_admin = ldap_admin.strip()
        if not ldap_admin:
            continue
        existing = await r.hget(config.KEY_USERS, ldap_admin)
        if not existing:
            user_data = {
                "username": ldap_admin,
                "password_hash": "",  # AD 账号不存本地密码
                "display_name": ldap_admin,
                "role": "admin",
                "auth_source": "ldap",
                "created_at": time.time()
            }
            await r.hset(config.KEY_USERS, ldap_admin, json.dumps(user_data, ensure_ascii=False))
            logger.info(f"已预置 AD 管理员账户: {ldap_admin}")
        else:
            # 确保已有用户是 admin 角色
            u = json.loads(existing)
            if u.get("role") != "admin":
                u["role"] = "admin"
                await r.hset(config.KEY_USERS, ldap_admin, json.dumps(u, ensure_ascii=False))
                logger.info(f"已将 {ldap_admin} 提升为管理员")


# ═══ App ═══
@asynccontextmanager
async def lifespan(app: FastAPI):
    r = await get_redis()
    await ensure_admin_user()
    await r.hset(config.KEY_CHANNEL_HEARTBEATS, "webchat", json.dumps({
        "name": "webchat", "status": "online", "started": time.time(), "port": config.WEBCHAT_PORT
    }))
    # 审计归档后台任务: 每日日志/用户操作日志落盘 gzip 长期保存
    from core import log_archive
    _archive_task = asyncio.create_task(log_archive.archive_loop())
    # v4.0 (Phase 3.2): 后台记忆合成调度 — 每日 02:00 扫描近 24h 会话提炼长期记忆
    _synth_task = None
    if getattr(config, "MEMORY_SYNTHESIS_ENABLED", True):
        try:
            from core.memory.synthesizer import run_daily_synthesis
            _synth_task = asyncio.create_task(run_daily_synthesis())
        except Exception as e:
            logger.warning(f"[lifespan] 记忆合成调度启动失败 (不阻断): {e}")
    # P3: Computer Use 桥命令总线 (agent 进程 ↔ 本进程的扩展 WS)
    try:
        from core import cu_bridge
        await cu_bridge.bus_init()
        logger.info("[lifespan] CU 桥命令总线已启动")
    except Exception as e:
        logger.warning(f"[lifespan] CU 桥命令总线启动失败 (不阻断): {e}")
    # P3: 插件系统 (dsh "一切皆插件" — 见 core/plugin_system.py)
    try:
        from core.plugin_system import startup_plugins
        await startup_plugins()
    except Exception as e:
        logger.warning(f"[lifespan] 插件系统启动失败 (不阻断): {e}")
    # NMS SSL 调查闭环：独立 worker（不绑在采集/分析循环末尾；用户关页面不影响跟踪）
    _ssl_task = None
    try:
        from core.nms_ssl_workflow import worker_loop as _ssl_worker_loop
        _ssl_task = asyncio.create_task(_ssl_worker_loop())
    except Exception as e:
        logger.warning(f"[lifespan] SSL 调查 worker 启动失败 (不阻断): {e}")
    # 通用告警闭环：把采集快照刷新成故障单元，推进 L2 簇级恢复与 L1 探针核验
    _closure_task = None
    try:
        from core.alert_closure_engine import worker_loop as _closure_worker_loop
        _closure_task = asyncio.create_task(_closure_worker_loop())
    except Exception as e:
        logger.warning(f"[lifespan] 告警闭环 worker 启动失败 (不阻断): {e}")
    # P1 (task-c17): 任务运行时后台 — 孤儿回收恒启; Streams 消费 (GCOC 试点)
    # 仅在 readiness 达标 (Redis>=6.2 等) 且 ZOPS_TR_STREAMS 开关开启时启动
    _reclaim_task = None
    _tr_consumer = None
    try:
        from core.task_runtime import get_task_runtime
        _rt = get_task_runtime()
        _reclaim_task = asyncio.create_task(_rt.reclaim_loop())
        if await _rt.streams_enabled():
            from core.task_runtime.consumer import TaskConsumer
            from core.task_runtime.agent_dispatch import AgentDispatch
            _tr_consumer = TaskConsumer(_rt.ledger, AgentDispatch(_rt),
                                        owner="zops-backend", lease_s=_rt.lease_s)
            await _tr_consumer.start()
            logger.info("[lifespan] P1 Streams 消费已启动 (GCOC 试点路由)")
        else:
            logger.info("[lifespan] P1 Streams 消费未激活 (开关/门槛), 协调者模式运行")
    except Exception as e:
        logger.warning(f"[lifespan] 任务运行时后台启动失败 (不阻断): {e}")
    logger.info(f"Z-OPS Web Dashboard 启动 @ :{config.WEBCHAT_PORT}")
    yield
    _archive_task.cancel()
    if _synth_task:
        _synth_task.cancel()
    if _reclaim_task:
        _reclaim_task.cancel()
    if _tr_consumer:
        await _tr_consumer.stop()
    if _ssl_task:
        _ssl_task.cancel()
        try:
            await _ssl_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            from core.nms_ssl_workflow import close_store as _ssl_close_store
            await _ssl_close_store()
        except Exception:
            pass
    if _closure_task:
        _closure_task.cancel()
        try:
            await _closure_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            from core.alert_closure_engine import close_redis as _closure_close
            await _closure_close()
        except Exception:
            pass
    await r.hset(config.KEY_CHANNEL_HEARTBEATS, "webchat", json.dumps({
        "name": "webchat", "status": "offline", "stopped": time.time()
    }))
    if _redis:
        await _redis.aclose()

app = FastAPI(title="Z-OPS 运维智能体", version=config.VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# ═══ Token 认证 ═══

# 每日活跃用户记录缓存（内存层，避免每次请求都查 Redis）
_daily_active_cache: dict = {}  # {"YYYY-MM-DD:username": True}


async def _track_daily_active(username: str, client_ip: str):
    """记录用户当日首次活跃（登录态访问）到 daily_log 和 user_ops"""
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"{today}:{username}"
    if cache_key in _daily_active_cache:
        return  # 今天已记录过
    try:
        r = await get_redis()
        redis_key = f"{config.REDIS_PREFIX}:active:{today}:{username}"
        # SET NX — 只在 key 不存在时设置（防止多进程重复写）
        was_set = await r.set(redis_key, "1", ex=86400, nx=True)
        if not was_set:
            _daily_active_cache[cache_key] = True
            return  # 已被其他进程/请求记录
        # 首次活跃 → 写入日志
        _daily_active_cache[cache_key] = True
        log_key = config.daily_log_key(today)
        log_entry = json.dumps({
            "agent": "system",
            "content": f"用户 {username} 今日活跃 (IP: {client_ip})",
            "level": "info",
            "category": "auth",
            "session_id": "",
            "username": username,
            "timestamp": time.time()
        }, ensure_ascii=False)
        await r.rpush(log_key, log_entry)
        if config.DAILY_LOG_TTL > 0:
            await r.expire(log_key, config.DAILY_LOG_TTL)
        # 同时写 user_ops
        ops_entry = json.dumps({
            "username": username, "action": "active",
            "detail": f"今日首次活跃 (IP: {client_ip})",
            "ip": client_ip, "timestamp": time.time()
        }, ensure_ascii=False)
        ops_key = config.user_ops_key(today)
        await r.rpush(ops_key, ops_entry)
        if config.USER_OPS_LOG_TTL > 0:
            await r.expire(ops_key, config.USER_OPS_LOG_TTL)
        logger.info(f"[Auth] 用户 {username} 今日首次活跃 (IP: {client_ip})")
    except Exception as e:
        logger.debug(f"[Auth] 活跃记录失败: {e}")


async def verify_token(request: Request) -> str:
    """从 Authorization header 提取并验证 Token，返回 username"""
    auth = request.headers.get("Authorization", "")
    client_ip = request.client.host if request.client else "unknown"
    if auth.startswith("Bearer "):
        token = auth[7:]
        r = await get_redis()
        username = await r.get(config.key_token(token))
        if username:
            # 记录每日首次活跃
            await _track_daily_active(username, client_ip)
            return username
    # 兼容 Basic Auth (供 API 调用)
    if auth.startswith("Basic "):
        import base64
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, pwd = decoded.split(":", 1)
            if secrets.compare_digest(user, config.WEBCHAT_AUTH_USER) and \
               secrets.compare_digest(pwd, config.WEBCHAT_AUTH_PASS):
                await _track_daily_active(user, client_ip)
                return user
        except Exception:
            pass
    raise HTTPException(status_code=401, detail="认证失败")


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


# ═══ 文件下载服务 ═══
_DOWNLOADS_DIR = config.BASE_DIR / "downloads"
_DOWNLOADS_DIR.mkdir(exist_ok=True)


@app.get("/api/download/{filename:path}")
async def download_file(filename: str, request: Request, token: str = ""):
    """下载 CSV 文件 (支持 Header Bearer 和 ?token= 两种认证方式)"""
    import re as _re
    # 安全校验: 只允许下载 downloads 目录下的 CSV/图片 文件
    if not _re.match(r'^(computer_use/)?[\w\-]+\.(csv|png|jpg)$', filename):
        raise HTTPException(400, "无效的文件名")
    
    # 认证: 优先从 query param 获取 token，兼容浏览器直接访问
    auth = request.headers.get("Authorization", "")
    if not auth and token:
        auth = f"Bearer {token}"
    if not auth:
        raise HTTPException(status_code=401, detail="认证失败")
    
    # 验证 token
    if auth.startswith("Bearer "):
        tk = auth[7:]
        r = await get_redis()
        username = await r.get(config.key_token(tk))
        if not username:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")
    else:
        raise HTTPException(status_code=401, detail="认证方式不支持")
    
    filepath = _DOWNLOADS_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "文件不存在或已过期")
    if filename.startswith("computer_use/"):
        # 截图属于生成它的用户/任务; 归属无法证明的历史文件默认拒绝, 但不删除。
        try:
            r = await get_redis()
            owner = await r.hget(f"{config.REDIS_PREFIX}:cu:shot_owner", filename)
            if isinstance(owner, bytes):
                owner = owner.decode()
        except Exception:
            raise HTTPException(403, "无法核实截图归属")
        if not owner or owner != username:
            raise HTTPException(403, "无权访问该截图")
    return FileResponse(
        str(filepath),
        media_type={"png": "image/png", "jpg": "image/jpeg"}.get(
            filename.rsplit(".", 1)[-1], "text/csv"),
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ═══ 主页 ═══
@app.get("/", response_class=HTMLResponse)
async def index():
    try:
        # 使用稳定的单文件版本（避免模块化导致的加载顺序问题）
        frontend_path = config.BASE_DIR / "webchat_stable.html"
        if frontend_path.exists():
            content = frontend_path.read_text(encoding="utf-8")
            return HTMLResponse(
                content=content,
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0"
                }
            )
        
        # 备选：新的模块化前端
        index_path = config.BASE_DIR / "webchat_frontend" / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        
        logger.error("[Frontend] 前端文件未找到")
        return HTMLResponse("<h1>Z-OPS 运维智能体</h1><p style='color:red'>前端文件未找到，请检查部署</p>")
    except Exception as e:
        logger.error(f"[Frontend] 主页访问错误：{e}", exc_info=True)
        return HTMLResponse(f"<h1>服务错误</h1><p>{str(e)}</p>", status_code=500)









# ═══ 管理员权限校验辅助 ═══
async def require_admin(current_user: str) -> None:
    """校验当前用户是否为管理员，否则抛 403"""
    r = await get_redis()
    raw = await r.hget(config.KEY_USERS, current_user)
    if not raw:
        raise HTTPException(403, "权限不足")
    user = json.loads(raw)
    if user.get("role") != "admin":
        raise HTTPException(403, "仅管理员可操作")


# ═══ 用户组动态列表 (内置组 + Redis 自定义组) ═══
async def _get_all_groups() -> list:
    """合并内置组与自定义组，供组别校验与列表展示"""
    groups = list(config.USER_GROUPS)
    try:
        r = await get_redis()
        raw = await r.get(config.KEY_CUSTOM_GROUPS)
        if raw:
            for g in json.loads(raw):
                if g not in groups:
                    groups.append(g)
    except Exception as e:
        logger.warning(f"[ADMIN] 加载自定义组失败: {e}")
    return groups


async def _validate_group(group: str) -> None:
    """校验组名有效（空字符串=无组别，视为合法）"""
    if not group:
        return
    groups = await _get_all_groups()
    if group not in groups:
        raise HTTPException(400, f"无效的组别，可选: {', '.join(groups)}")













































@app.get("/api/approval/{approval_id}")
async def get_approval_status(approval_id: str):
    """
    获取审批状态
    用于 Agent 或前端轮询审批结果
    """
    r = await get_redis()
    result = await r.get(f"approval:{approval_id}")
    
    if result:
        return json.loads(result)
    return {"status": "pending", "approval_id": approval_id}


@app.post("/api/approval/{approval_id}/wait")
async def wait_for_approval(approval_id: str, timeout: int = 300):
    """
    等待审批结果
    轮询 Redis 直到审批完成或超时
    """
    import asyncio
    r = await get_redis()
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        result = await r.get(f"approval:{approval_id}")
        if result:
            return json.loads(result)
        await asyncio.sleep(2)
    
    return {"status": "timeout", "approval_id": approval_id}













# ═══ LLM 配置 ═══
# 内置 Provider 列表 — 用户可通过管理后台添加自定义 Provider (存 Redis)
_BUILTIN_PROVIDERS = [
    {"id": "oneapi", "name": "OneAPI 网关", "base_url": config.ONEAPI_BASE_URL,
     "models": [
         "deepseek-v3-0324",
     ],
     "model_aliases": config.MODEL_ALIASES,
     "builtin": True, "desc": "统一 AI 网关 (别名 deepseek-v3-0324 → 真实模型 glm5-pg1；仅可用别名请求)"},
    {"id": "deepseek", "name": "DeepSeek 官方", "base_url": "https://api.deepseek.com/v1",
     "models": ["deepseek-chat", "deepseek-reasoner"],
     "builtin": True, "desc": "DeepSeek 官方 API"},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1",
     "models": ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o3-mini"],
     "builtin": True, "desc": "OpenAI 官方 API"},
    {"id": "anthropic", "name": "Anthropic", "base_url": "https://api.anthropic.com",
     "models": ["claude-sonnet-4-20250514", "claude-haiku-4-20250514"],
     "builtin": True, "desc": "Anthropic Claude 系列"},
    {"id": "zhipu", "name": "智谱 AI", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "models": ["glm-4-plus", "glm-4-flash"],
     "builtin": True, "desc": "智谱 GLM 系列"},
    {"id": "ollama", "name": "Ollama (本地)", "base_url": "http://127.0.0.1:11434/v1",
     "models": ["qwen3:latest", "deepseek-r1:latest", "llama3.1:latest"],
     "builtin": True, "desc": "本地 Ollama 推理"},
    {"id": "custom", "name": "自定义 OpenAI 兼容", "base_url": "", "models": [],
     "builtin": True, "desc": "任何 OpenAI 兼容 API"},
]

# Provider ID → 默认 Base URL 快速查找表 (供 agents/query_engine 使用)
PROVIDER_URL_MAP: dict[str, str] = {p["id"]: p["base_url"] for p in _BUILTIN_PROVIDERS if p["base_url"]}


# OneAPI 模型列表缓存 (避免每次请求都查网关)
_oneapi_models_cache: dict = {"models": [], "ts": 0}
_ONEAPI_CACHE_TTL = 300  # 缓存 5 分钟






















# ═══ 用户操作日志 (审计用) ═══

async def _log_user_op(username: str, action: str, detail: str = "", ip: str = "", session_id: str = ""):
    """记录用户操作日志 (登录/查询/配置变更/key 管理等)

    写入 Redis: {prefix}:user_ops:{YYYY-MM-DD} (追加, append-only, 配合 TTL 自动滚动)
    """
    r = await get_redis()
    today = datetime.now().strftime("%Y-%m-%d")
    entry = json.dumps({
        "username": username, "action": action, "detail": detail,
        "ip": ip, "session_id": session_id, "timestamp": time.time()
    }, ensure_ascii=False)
    key = config.user_ops_key(today)
    await r.rpush(key, entry)
    if config.USER_OPS_LOG_TTL > 0:
        await r.expire(key, config.USER_OPS_LOG_TTL)


async def _emit_daily_log(agent: str, content: str, level: str = "info",
                          category: str = "system", session_id: str = "",
                          username: str = ""):
    """统一写入每日运维日志 (前端日志页可见)"""
    try:
        r = await get_redis()
        today = datetime.now().strftime("%Y-%m-%d")
        log_entry = json.dumps({
            "agent": agent,
            "content": content,
            "level": level,
            "category": category,
            "session_id": session_id,
            "username": username,
            "timestamp": time.time()
        }, ensure_ascii=False)
        log_key = config.daily_log_key(today)
        await r.rpush(log_key, log_entry)
        if config.DAILY_LOG_TTL > 0:
            await r.expire(log_key, config.DAILY_LOG_TTL)
    except Exception as e:
        logger.debug(f"[daily_log] 写入失败: {e}")


































# ═══ NMS 告警只读 API（读 Redis 快照/历史/分析 + 触发抓取）═══
# (端点已拆至 api/routers/agents.py; 以下 NMS 常量供其 import)
_NMS_SEV_PRIORITY = {'critical': 0, 'major': 1, 'minor': 2, 'warning': 3,
                     'indeterminate': 4, 'clear': 5, 'cleared': 6, 'unknown': 7}
# 现存(活跃)告警级别集合；其余(clear/cleared/unknown)归入历史告警
_NMS_ACTIVE_SEV = {'critical', 'major', 'minor', 'warning', 'indeterminate'}




# ═══ Checklist 预案群分类（以告警为核心的分域关联分析）═══
# 每个群: key/名称/关联分析描述/命中的预案标题集合；平台服务类预案归入 platform 群。
_NMS_PLAN_GROUPS = [
    {"key": "storage", "name": "存储与硬件群",
     "desc": "磁盘/RAID/控制器类告警指向存储层硬件风险，处置时需核对设备 SN 与厂商，评估磁盘更换窗口。",
     "titles": ["存储磁盘故障", "存储控制器故障", "存储电源故障", "ESXi电源故障",
                "ESXi CPU/内存硬件故障", "存储故障"]},
    {"key": "network", "name": "虚拟化网络群",
     "desc": "ESXi 主机连通性/网卡类告警指向虚拟化层网络链路或物理网卡故障，需先区分计划内维护与真实中断。",
     "titles": ["vCenter连接Host失败", "ESXi网卡故障", "DNS解析异常", "vCenter DRS冲突", "VM HA重启", "ESXI主机outage故障"]},
    {"key": "backup", "name": "备份容灾群",
     "desc": "备份失败/超时/Journal 空间不足显示跨平台备份容灾体系存在存储空间、代理连通性或任务调度瓶颈。",
     "titles": ["VBR备份失败", "A2C VSPC备份失败", "A2C O365备份失败", "Zerto VPG Journal空间不足", "VM快照整合异常", "vbr 备份慢"]},
    {"key": "mail", "name": "邮件系统群",
     "desc": "邮箱超配额与 MTA 队列飙升强关联，疑似垃圾邮件风暴或异常发送导致队列堆积。",
     "titles": ["SCM邮件队列堆积", "SCM邮箱异常外发", "Proxymta outage故障", "mailbox outage故障", "SCM 邮件收不到指定邮件", "SCM 邮件发出去对方没收到"]},
    {"key": "os", "name": "OS资源群",
     "desc": "主机内存/磁盘/负载类告警反映业务节点资源过载，需结合容量与扩容计划处置。",
     "titles": ["ESXi CPU/内存使用率高", "ESXi Datastore使用率高", "VM使用慢"]},
    {"key": "cert", "name": "安全证书群",
     "desc": "证书可用天数归零将直接中断对外业务，需及时续期。",
     "titles": ["SSL证书过期"]},
    {"key": "platform", "name": "平台服务群",
     "desc": "平台级服务可用性/业务功能类预案，通常影响多客户共享平台基础设施。",
     "titles": ["AVAS outage故障", "TAB故障", "vidyo outage故障", "PDNS outage故障",
               "客户MC账号无法登录登入VCD", "VM配置了WEB服务，但是该WEB浏览器无法打开"]},
]
_NMS_PLAN_GROUP_BY_TITLE = {}
for _g in _NMS_PLAN_GROUPS:
    for _t in _g['titles']:
        _NMS_PLAN_GROUP_BY_TITLE[_t] = _g


# 平台管理网段（判断告警对象属于平台多客户共享环境还是单客户环境）
# 平台段: GCOC 管理平台 10.12.x、数据中心 ESXi/VM 段 10.161-165.x、vCenter 172.19.x、
#        平台内部服务段 192.168.145.x(备份)/192.168.166.x(SCM邮件)；其余视为单客户/外部
_NMS_PLATFORM_NETS = ["10.12.", "10.161.", "10.162.", "10.163.", "10.164.", "10.165.",
                      "172.19.", "192.168.145.", "192.168.166."]


import re as _nms_re
_IP4_RE = _nms_re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')





# 硬件类预案：命中时需从 GCOC-MGT 资产系统（存储资产 API）补充设备 SN 等资产信息
_NMS_HARDWARE_TITLES = {"存储磁盘故障", "存储控制器故障", "存储电源故障", "ESXi电源故障",
                        "ESXi CPU/内存硬件故障", "存储故障", "ESXi网卡故障", "ESXI主机outage故障"}

_NMS_SEV_LABEL = {'critical': '严重', 'major': '重要', 'minor': '次要',
                  'warning': '警告', 'indeterminate': '未知'}

_HOSTNAME_RE = _nms_re.compile(r'\b([A-Z][A-Z0-9]{5,})\b')
























# ═══ v4.0 Phase 6: 域路由挂载 (api/routers/*, 端点自本文件拆分, 行为不变) ═══
# 放在文件底部: include 时本模块全部共享名已定义, router 文件可安全 from webchat_api import *
# __main__ 直接运行(systemd)时先把本实例注册为 sys.modules["webchat_api"],
# 避免下方 router 的 `from webchat_api import *` 触发本文件二次加载
# (彼时 admin 等尚部分初始化, 会循环导入 AttributeError 崩溃)
import sys as _sys
if __name__ == "__main__" and "webchat_api" not in _sys.modules:
    _sys.modules["webchat_api"] = _sys.modules[__name__]
from api.routers import admin as _r_admin, agents as _r_agents, chat as _r_chat
from api.routers import logs as _r_logs, n8n as _r_n8n, obs as _r_obs, sessions as _r_sessions
from api.routers import cu_bridge as _r_cu_bridge
from api.routers import cu_devices as _r_cu_devices
from api.routers import plugins as _r_plugins

for _router_mod in (_r_sessions, _r_chat, _r_agents, _r_obs, _r_n8n,
                    _r_admin, _r_logs, _r_cu_bridge, _r_cu_devices, _r_plugins):
    app.include_router(_router_mod.router)



# ═══ 启动 ═══
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "webchat_api:app",
        host=config.WEBCHAT_HOST,
        port=config.WEBCHAT_PORT,
        reload=False,
        log_level="info",
        # 关闭访问日志: /api/overview 等轮询每几秒一条, 长期累积曾达 11GB
        # (业务日志由 loguru 记录, 不受影响)
        access_log=False
    )
