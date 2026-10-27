"""Z-OPS Computer Use 桥 — Chrome 扩展 ↔ Z-OPS ↔ 前端实时视图 (WebSocket)

架构 (2026-10-22, 替代服务端无头浏览器直连方案):
  用户本机 Chrome --WS(扩展)--> Z-OPS 后端 <--WS(查看者) 前端实时小窗
                                ↑
                          computer_agent (命令队列)

原则:
  - 凭据零经过: 扩展驱动用户自己的浏览器, 登录态/密码由浏览器原生密码管理器
    与站点 session 负责; Z-OPS 全程只转发指令与页面快照, 不接触任何密码。
  - 按 actor 隔离: 每个用户一条扩展连接; 命令只能发给自己的浏览器,
    查看者只能看自己的浏览器画面。
  - 命令即 Future: agent 发送 {id, method, params} → 挂起等待扩展回
    {type: cmd_result, id, ok, data}; 断线时全部 future 以错误终结。

消息协议 (JSON):
  server→ext:  {"type":"cmd","id","method","params"} / {"type":"stream","on":bool}
  ext→server:  {"type":"hello","client":...} / {"type":"cmd_result","id","ok","data"}
               {"type":"frame","jpeg":b64,"w","h"} / {"type":"state","url","title"}
  server→view: {"type":"frame",...} / {"type":"state",...} / {"type":"log",entry}
               {"type":"ext_status","connected":bool,...} / {"type":"backlog",...}
"""

import asyncio
import json
import logging
import secrets
import time
from collections import deque

import config

logger = logging.getLogger("zops")

# 允许扩展执行的命令白名单 (agent → ext; 无任意 JS 执行)
EXT_METHODS = {
    "goto", "back", "forward", "reload", "snapshot", "act", "press",
    "scroll", "screenshot", "tabs", "activate_tab", "open_tab",
    "close_tab",
}

CMD_TIMEOUT_S = 45          # 单命令往返上限 (goto 含加载可到 45s)
LOG_LIMIT = 80              # 每用户动作日志条数
HB_MIN_INTERVAL_S = 25      # 心跳写 Redis 的最小间隔 (扩展每 20s ping 一次)
PAIR_TTL_S = 300            # 连接码有效期 (5 分钟, 一次性)
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class _ActorChannel:
    """单个用户的桥通道: 扩展连接 + 查看者 + 命令队列 + 状态"""

    def __init__(self, actor: str):
        self.actor = actor
        self.ext = None                    # WebSocket | None (最新连接生效)
        self.ext_meta: dict = {}           # {client, ua, connected_at}
        self.viewers: set = set()
        self.pending: dict = {}            # cmd_id -> asyncio.Future
        self.last_frame: dict = {}         # {jpeg, w, h, ts} — 扩展推送帧
        self.last_server_frame: dict = {}   # {jpeg, w, h, ts} — 服务端浏览器帧
        self.state: dict = {}              # {url, title, ts}
        self.log: deque = deque(maxlen=LOG_LIMIT)
        self.last_hb: float = 0.0          # 最近一次心跳 (扩展任意消息)
        self._cmd_seq = 0

    def next_cmd_id(self) -> str:
        self._cmd_seq += 1
        return f"{int(time.time()*1000)}-{self._cmd_seq}"


# actor -> _ActorChannel
_channels: dict = {}


def _ch(actor: str) -> _ActorChannel:
    if actor not in _channels:
        _channels[actor] = _ActorChannel(actor)
    return _channels[actor]


def is_connected(actor: str) -> bool:
    ch = _channels.get(actor)
    return bool(ch and ch.ext is not None)


def get_status(actor: str) -> dict:
    ch = _channels.get(actor)
    if not ch:
        return {"connected": False, "viewers": 0}
    return {
        "connected": ch.ext is not None,
        "viewers": len(ch.viewers),
        "state": ch.state,
        "client": ch.ext_meta,
        "last_frame_ts": ch.last_frame.get("ts", 0),
        "server_frame_ts": ch.last_server_frame.get("ts", 0),
        "last_seen": ch.last_hb,
    }


# ═══ 扩展连接管理 ═══

async def attach_ext(actor: str, ws, meta: dict = None) -> _ActorChannel:
    """扩展接入: 新连接顶替旧连接 (同账号多处安装时以最新为准)"""
    ch = _ch(actor)
    old = ch.ext
    ch.ext = ws
    ch.ext_meta = meta or {}
    ch.ext_meta["connected_at"] = time.time()
    ch.last_hb = time.time()
    if old is not None and old is not ws:
        try:
            await old.close()
        except Exception:
            pass
    await broadcast_viewers(actor, {"type": "ext_status", **get_status(actor)})
    record_log(actor, {"ts": time.time(), "kind": "connect",
                       "text": "浏览器扩展已连接"}, quiet=True)
    await _publish_status(actor, True)
    logger.info(f"[cu_bridge] 扩展接入 actor={actor} ({ch.ext_meta.get('client', '?')})")
    return ch


async def detach_ext(actor: str, ws):
    ch = _channels.get(actor)
    if not ch or ch.ext is not ws:
        return
    ch.ext = None
    ch.ext_meta = {}
    # 未完结命令全部失败
    for cid, fut in list(ch.pending.items()):
        if not fut.done():
            fut.set_exception(ConnectionError("浏览器扩展已断开"))
    ch.pending.clear()
    await broadcast_viewers(actor, {"type": "ext_status", **get_status(actor)})
    record_log(actor, {"ts": time.time(), "kind": "disconnect",
                       "text": "浏览器扩展已断开"}, quiet=True)
    await _publish_status(actor, False)
    logger.info(f"[cu_bridge] 扩展断开 actor={actor}")


async def handle_ext_message(actor: str, raw: str):
    """处理扩展上报消息 (cmd_result / frame / state)"""
    ch = _channels.get(actor)
    if not ch:
        return
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    mtype = msg.get("type")

    # 心跳续期: 扩展每 20s 发 ping / 推帧, Redis 在线表必须随之刷新 —
    # 否则 90s 后 agent 侧把在线的扩展误判为离线, 静默回退服务端浏览器。
    await _touch_status(actor)

    if mtype == "cmd_result":
        fut = ch.pending.pop(msg.get("id"), None)
        if fut and not fut.done():
            fut.set_result({"ok": bool(msg.get("ok")),
                            "data": msg.get("data") or {},
                            "error": msg.get("error") or ""})
    elif mtype == "frame":
        frame = {"type": "frame", "jpeg": msg.get("jpeg") or "",
                 "w": msg.get("w") or 0, "h": msg.get("h") or 0, "ts": time.time()}
        ch.last_frame = frame
        await broadcast_viewers(actor, frame)
    elif mtype == "state":
        ch.state = {"url": msg.get("url") or "", "title": msg.get("title") or "",
                    "tab_id": msg.get("tab_id"), "ts": time.time()}
        await broadcast_viewers(actor, {"type": "state", **ch.state})
    elif mtype == "log":
        record_log(actor, {"ts": time.time(), "kind": "ext",
                           "text": str(msg.get("text") or "")[:200]})


# ═══ 查看者 (前端实时小窗) ═══

async def add_viewer(actor: str, ws) -> dict:
    """查看者接入: 立即回放 缓存帧+状态+日志+连接状态"""
    ch = _ch(actor)
    ch.viewers.add(ws)
    backlog = {
        "type": "backlog",
        "status": get_status(actor),
        "log": list(ch.log)[-30:],
        **({"frame": ch.last_frame} if ch.last_frame.get("jpeg") else {}),
        **({"server_frame": ch.last_server_frame} if ch.last_server_frame.get("jpeg") else {}),
        **({"state": ch.state} if ch.state.get("url") else {}),
    }
    try:
        await ws.send_text(json.dumps(backlog, ensure_ascii=False))
    except Exception:
        ch.viewers.discard(ws)
    # 有查看者 → 通知扩展开始推流
    await _sync_stream(actor)
    return backlog


async def remove_viewer(actor: str, ws):
    ch = _channels.get(actor)
    if not ch:
        return
    ch.viewers.discard(ws)
    await _sync_stream(actor)


async def _sync_stream(actor: str):
    """按查看者数量开关扩展推流 (无人看时不占带宽)"""
    ch = _channels.get(actor)
    if not ch or ch.ext is None:
        return
    want = bool(ch.viewers)
    streaming = ch.ext_meta.get("_streaming", False)
    if want != streaming:
        ch.ext_meta["_streaming"] = want
        try:
            await ch.ext.send_text(json.dumps({"type": "stream", "on": want}))
        except Exception:
            pass


async def _touch_status(actor: str):
    """刷新 Redis 在线表 (节流: 最多每 HB_MIN_INTERVAL_S 写一次)"""
    ch = _channels.get(actor)
    if not ch or ch.ext is None:
        return
    now = time.time()
    if now - ch.last_hb < HB_MIN_INTERVAL_S:
        return
    ch.last_hb = now
    await _publish_status(actor, True)


async def broadcast_viewers(actor: str, msg: dict):
    ch = _channels.get(actor)
    if not ch or not ch.viewers:
        return
    dead = []
    text = json.dumps(msg, ensure_ascii=False)
    for ws in list(ch.viewers):
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ch.viewers.discard(ws)


def record_log(actor: str, entry: dict, quiet: bool = False):
    """动作日志: 留存 + 推送查看者 (quiet=只入日志不推送, 避免回放重复)"""
    ch = _ch(actor)
    ch.log.append(entry)
    if not quiet:
        # 同步上下文 (agent 协程) 里推送
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(broadcast_viewers(
                actor, {"type": "log", "entry": entry}))
        except RuntimeError:
            pass


async def update_state_from_agent(actor: str, state: dict):
    """agent 侧更新状态 (扩展也会上报, 这里兜底)"""
    ch = _ch(actor)
    ch.state = {**ch.state, **state, "ts": time.time()}
    await broadcast_viewers(actor, {"type": "state", **ch.state})


# ═══ Agent 命令接口 ═══

async def send_command(actor: str, method: str, params: dict = None,
                       timeout: float = CMD_TIMEOUT_S) -> dict:
    """向该用户的浏览器扩展发命令并等待结果

    返回 {ok, data, error}; 未连接/超时/断开均返回 ok=False (不抛异常,
    agent 侧据此降级或提示)。
    """
    if method not in EXT_METHODS:
        return {"ok": False, "error": f"未知命令: {method}"}
    ch = _channels.get(actor)
    if not ch or ch.ext is None:
        return {"ok": False, "error": "浏览器扩展未连接 (请在 Chrome 中安装并登录 Z-OPS 扩展)"}
    cid = ch.next_cmd_id()
    fut = asyncio.get_running_loop().create_future()
    ch.pending[cid] = fut
    record_log(actor, {"ts": time.time(), "kind": "cmd",
                       "text": f"→ {method} {json.dumps(params or {}, ensure_ascii=False)[:120]}"})
    try:
        await ch.ext.send_text(json.dumps(
            {"type": "cmd", "id": cid, "method": method,
             "params": params or {}}, ensure_ascii=False))
    except Exception as e:
        ch.pending.pop(cid, None)
        if not fut.done():
            fut.set_exception(ConnectionError(f"扩展连接失效: {e}"))
    try:
        result = await asyncio.wait_for(fut, timeout=timeout)
        record_log(actor, {"ts": time.time(), "kind": "result",
                           "text": ("✓" if result.get("ok") else "✗") +
                                   f" {method} " +
                                   (result.get("error") or
                                    json.dumps(result.get("data") or {}, ensure_ascii=False)[:100])})
        return result
    except asyncio.TimeoutError:
        ch.pending.pop(cid, None)
        return {"ok": False, "error": f"命令 {method} 超时 ({timeout}s)"}
    except ConnectionError as e:
        return {"ok": False, "error": str(e)}


def all_stats() -> dict:
    """桥总览 (诊断)"""
    return {
        "actors": {a: {"connected": c.ext is not None,
                       "viewers": len(c.viewers),
                       "pending": len(c.pending),
                       **({"url": c.state.get("url", "")[:80]}
                          if c.state.get("url") else {})}
                   for a, c in _channels.items() if c.ext or c.viewers},
    }


# ═══════════════════════════════════════════════════════════════
# Redis 命令总线 — agent 进程 ↔ backend 进程
# WS 连接与 _channels 注册表只存在于 backend 进程; computer_agent 是独立
# systemd 进程, 经 Redis pubsub 跨进程发命令 / 查在线状态。
# ═══════════════════════════════════════════════════════════════

import uuid as _uuid

_KEY_STATUS = f"{config.REDIS_PREFIX}:cu:ext_status"      # Hash: actor -> {connected, ts}
_CH_CMD_PREFIX = f"{config.REDIS_PREFIX}:cu:cmd:"          # agent → backend (按 actor)
_CH_RES_PREFIX = f"{config.REDIS_PREFIX}:cu:cmdres:"       # backend → agent (按 cmd_id)
_CH_LOG = f"{config.REDIS_PREFIX}:cu:log"                  # agent → backend 日志推送
_CH_FRAME = f"{config.REDIS_PREFIX}:cu:frame"              # agent → backend 服务端浏览器帧
_KEY_LIVE_PREFIX = f"{config.REDIS_PREFIX}:cu:server_live:"  # 服务端浏览器运行状态
_KEY_FORCE_EXT = f"{config.REDIS_PREFIX}:cu:force_ext:"      # 一次性强制使用本机扩展 (_login_wall 后切换)
SERVER_LIVE_FRESH_S = 90
SERVER_LIVE_TTL_S = 300
_STATUS_FRESH_S = 90


async def _publish_status(actor: str, connected: bool):
    """backend 侧维护 Redis 在线表 (agent 进程读)"""
    try:
        import redis.asyncio as _aioredis
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            if connected:
                await r.hset(_KEY_STATUS, actor,
                             json.dumps({"connected": True, "ts": time.time()}))
            else:
                await r.hdel(_KEY_STATUS, actor)
        finally:
            await r.aclose()
    except Exception as e:
        logger.debug(f"[cu_bridge] 状态发布失败: {e}")


_bus_task = None

async def bus_init():
    """backend 启动时调用: 订阅 agent 命令通道, 转发到对应扩展连接"""
    global _bus_task
    if _bus_task and not _bus_task.done():
        return
    _bus_task = asyncio.get_event_loop().create_task(_bus_loop())
    # 启动时清残留 (backend 重启后旧状态无效)
    try:
        import redis.asyncio as _aioredis
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            await r.delete(_KEY_STATUS)
        finally:
            await r.aclose()
    except Exception:
        pass


async def _bus_loop():
    """订阅 cu:cmd:* 与 cu:log; 命令转发扩展并回投结果"""
    import redis.asyncio as _aioredis
    while True:
        try:
            r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
            pubsub = r.pubsub()
            await pubsub.psubscribe(f"{_CH_CMD_PREFIX}*", _CH_LOG, _CH_FRAME)
            async for msg in pubsub.listen():
                if msg.get("type") not in ("pmessage", "message"):
                    continue
                try:
                    channel = msg["channel"]
                    data = json.loads(msg["data"])
                    if channel == _CH_LOG:
                        record_log(data.get("actor", ""), {
                            "ts": time.time(), "kind": "cmd",
                            "text": str(data.get("text", ""))[:200]}, quiet=True)
                        continue
                    if channel == _CH_FRAME:
                        await _on_server_frame(data)
                        continue
                    actor = channel[len(_CH_CMD_PREFIX):]
                    asyncio.get_event_loop().create_task(
                        _bus_forward(actor, data))
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"[cu_bridge] 命令总线断开, 3s 重连: {e}")
            await asyncio.sleep(3)


async def _bus_forward(actor: str, data: dict):
    """单条命令: 转发扩展 → 结果回投 cmdres 通道"""
    import redis.asyncio as _aioredis
    cmd_id = data.get("id", "")
    method = data.get("method", "")
    params = data.get("params") or {}
    timeout = float(data.get("timeout") or CMD_TIMEOUT_S)
    if method not in EXT_METHODS:
        result = {"ok": False, "error": f"未知命令: {method}"}
    else:
        ch = _channels.get(actor)
        if not ch or ch.ext is None:
            result = {"ok": False, "error": "浏览器扩展未连接"}
        else:
            result = await send_command(actor, method, params, timeout)
    try:
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            await r.publish(f"{_CH_RES_PREFIX}{cmd_id}",
                            json.dumps(result, ensure_ascii=False))
        finally:
            await r.aclose()
    except Exception:
        pass


async def _on_server_frame(data: dict):
    """服务端浏览器帧广播: 缓存末帧供后接入查看者回放, 同时推给在线小窗"""
    actor = str(data.get("actor") or "")
    jpeg = data.get("jpeg") or ""
    if not actor or not jpeg:
        return
    ch = _ch(actor)
    frame = {"type": "frame", "source": "server", "jpeg": jpeg,
             "w": data.get("w") or 0, "h": data.get("h") or 0, "ts": time.time()}
    ch.last_server_frame = frame
    state = {"url": data.get("url") or "", "title": data.get("title") or "",
             "step": data.get("step") or 0, "backend": "server", "ts": time.time()}
    if state["url"] or state["title"]:
        ch.state = state
        await broadcast_viewers(actor, {"type": "state", **state})
    await broadcast_viewers(actor, frame)


async def create_pair_code(actor: str) -> dict:
    """生成一次性连接码 (已登录用户在 Z-OPS 页面点「生成连接码」)

    扩展 popup 凭连接码换取 token — 用户无需把 AD 密码填进扩展,
    登录态只来自 Z-OPS 网页本身。
    """
    code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
    import redis.asyncio as _aioredis
    r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
    try:
        await r.set(f"{config.REDIS_PREFIX}:cu:pair:{code}",
                    json.dumps({"actor": actor, "ts": time.time()}),
                    ex=PAIR_TTL_S)
    finally:
        await r.aclose()
    return {"code": code, "expires_in": PAIR_TTL_S}


async def claim_pair_code(code: str) -> str:
    """扩展凭连接码换取账号 (一次性: getdel 原子消费); 无效/过期抛 ValueError"""
    code = (code or "").strip().upper()
    if len(code) != 8:
        raise ValueError("连接码格式不正确 (8 位字母数字)")
    import redis.asyncio as _aioredis
    r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
    try:
        raw = await r.getdel(f"{config.REDIS_PREFIX}:cu:pair:{code}")
    finally:
        await r.aclose()
    if not raw:
        raise ValueError("连接码无效或已过期, 请在 Z-OPS 页面重新生成")
    try:
        actor = str(json.loads(raw).get("actor") or "").strip()
    except (json.JSONDecodeError, TypeError):
        actor = ""
    if not actor:
        raise ValueError("连接码无效或已过期, 请在 Z-OPS 页面重新生成")
    logger.info(f"[cu_bridge] 连接码已兑换 actor={actor}")
    return actor


# ── agent 进程侧接口 ──

async def agent_is_connected(actor: str) -> bool:
    """agent 进程查扩展在线 (读 backend 维护的 Redis 状态表)"""
    try:
        import redis.asyncio as _aioredis
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            raw = await r.hget(_KEY_STATUS, actor)
        finally:
            await r.aclose()
        if not raw:
            return False
        d = json.loads(raw)
        return bool(d.get("connected")) and time.time() - d.get("ts", 0) < _STATUS_FRESH_S
    except Exception:
        return False


async def agent_send_command(actor: str, method: str, params: dict = None,
                             timeout: float = CMD_TIMEOUT_S) -> dict:
    """agent 进程发命令: publish → 等 cmdres → 返回 {ok, data, error}"""
    import redis.asyncio as _aioredis
    cmd_id = _uuid.uuid4().hex[:16]
    r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
    try:
        pubsub = r.pubsub()
        await pubsub.subscribe(f"{_CH_RES_PREFIX}{cmd_id}")
        await r.publish(f"{_CH_CMD_PREFIX}{actor}",
                        json.dumps({"id": cmd_id, "method": method,
                                    "params": params or {}, "timeout": timeout},
                                   ensure_ascii=False))
        deadline = time.time() + timeout + 2
        while time.time() < deadline:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
            if msg and msg.get("type") == "message":
                try:
                    return json.loads(msg["data"])
                except Exception:
                    break
        return {"ok": False, "error": f"命令 {method} 无响应 (超时 {timeout}s)"}
    except Exception as e:
        return {"ok": False, "error": f"命令总线异常: {e}"}
    finally:
        try:
            await pubsub.aclose()
        except Exception:
            pass
        await r.aclose()


async def agent_log(actor: str, text: str):
    """agent 进程推送动作日志 → backend → 前端小窗"""
    try:
        import redis.asyncio as _aioredis
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            await r.publish(_CH_LOG, json.dumps({"actor": actor, "text": text},
                                                ensure_ascii=False))
        finally:
            await r.aclose()
    except Exception:
        pass


# ── 服务端浏览器: 状态 + 画面推流 (小窗只读观看, 不占用用户电脑) ──

async def set_server_live(actor: str, running: bool, **extra):
    """标记该用户的服务端浏览器任务是否在跑 (小窗据此提示"AI 正在服务器执行")"""
    if not actor:
        return
    try:
        import redis.asyncio as _aioredis
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            key = _KEY_LIVE_PREFIX + actor
            if running:
                await r.set(key, json.dumps({"running": True, "ts": time.time(), **extra},
                                            ensure_ascii=False), ex=SERVER_LIVE_TTL_S)
            else:
                await r.delete(key)
        finally:
            await r.aclose()
    except Exception as e:
        logger.debug(f"[cu_bridge] server_live 写入失败: {e}")


async def get_server_live(actor: str) -> dict:
    """读服务端浏览器运行状态; 超时未刷新视为已结束"""
    if not actor:
        return {}
    try:
        import redis.asyncio as _aioredis
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            raw = await r.get(_KEY_LIVE_PREFIX + actor)
        finally:
            await r.aclose()
        if not raw:
            return {}
        d = json.loads(raw)
        if time.time() - float(d.get("ts") or 0) > SERVER_LIVE_FRESH_S:
            return {}
        return d
    except Exception:
        return {}


async def agent_frame(actor: str, jpeg_b64: str, w: int = 0, h: int = 0,
                      url: str = "", title: str = "", step: int = 0):
    """agent 进程推送服务端浏览器帧 (每步一次) → backend 转播给该用户的小窗"""
    if not actor or not jpeg_b64:
        return
    try:
        import redis.asyncio as _aioredis
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            await r.publish(_CH_FRAME, json.dumps({
                "actor": actor, "jpeg": jpeg_b64, "w": w, "h": h,
                "url": url, "title": title, "step": step}, ensure_ascii=False))
        finally:
            await r.aclose()
    except Exception as e:
        logger.debug(f"[cu_bridge] 帧推送失败: {e}")


async def set_force_extension(actor: str, ttl: int = 600):
    """用户在小窗里点"改用我的浏览器": 下一次任务强制走扩展 (一次性)"""
    if not actor:
        return
    try:
        import redis.asyncio as _aioredis
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            await r.set(_KEY_FORCE_EXT + actor, "1", ex=ttl)
        finally:
            await r.aclose()
    except Exception:
        pass


async def take_force_extension(actor: str) -> bool:
    """读取并消费一次性"用我的浏览器"标记"""
    if not actor:
        return False
    try:
        import redis.asyncio as _aioredis
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            return bool(await r.getdel(_KEY_FORCE_EXT + actor))
        finally:
            await r.aclose()
    except Exception:
        return False


# ── 用户级 Computer Use 偏好 (设置页可改) ──

_KEY_PREF = f"{config.REDIS_PREFIX}:cu_pref:"
CU_PREF_DEFAULTS = {
    # 新 CU 只保留画面开关; 执行位置由设备选择决定, 不存在服务器回退选项。
    "push_frames": True,       # 实时画面推送到小窗
    "desktop_optin": False,    # 用户主动选择允许申请桌面接管; 每次仍需本机原生批准
    "persist_shots": False,    # 截图证据持久保存: 必须另行显式授权, 默认不落盘
}


def _as_bool(v, default: bool) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bytes):
        v = v.decode(errors="ignore")
    return str(v).lower() in ("1", "true", "yes", "on")


async def shot_evidence_enabled(redis, actor: str) -> bool:
    """设备写入证据前实时检查用户授权, 不在设备端缓存持久同意。"""
    if not actor:
        return False
    try:
        return _as_bool(await redis.hget(_KEY_PREF + actor, "persist_shots"), False)
    except Exception:
        return False


async def get_prefs(actor: str) -> dict:
    """读用户 Computer Use 偏好 (缺省: 画面开、桌面接管关、证据保存关)"""
    prefs = dict(CU_PREF_DEFAULTS)
    if not actor:
        return prefs
    try:
        import redis.asyncio as _aioredis
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            raw = await r.hgetall(_KEY_PREF + actor) or {}
        finally:
            await r.aclose()
        for k, dft in CU_PREF_DEFAULTS.items():
            prefs[k] = _as_bool(raw.get(k), dft)
    except Exception:
        pass
    return prefs


async def set_prefs(actor: str, updates: dict) -> dict:
    """写用户 Computer Use 偏好 (只接受白名单键)"""
    if not actor:
        return dict(CU_PREF_DEFAULTS)
    mapping = {k: ("1" if _as_bool(v, True) else "0")
               for k, v in (updates or {}).items() if k in CU_PREF_DEFAULTS}
    try:
        import redis.asyncio as _aioredis
        r = _aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            if mapping:
                await r.hset(_KEY_PREF + actor, mapping=mapping)
        finally:
            await r.aclose()
    except Exception as e:
        logger.debug(f"[cu_bridge] 偏好写入失败: {e}")
    return await get_prefs(actor)
