"""Z-OPS P0 — n8n 回调签名校验 (n8n_callback_auth)

SPEC P0「通道、服务身份与凭据」:
  n8n 触发保留真实用户; 回调校验签名、方法/路径、原始 body、时间戳、nonce、
  execution 绑定。默认允许前后五分钟, 持久幂等去重; 缺签名配置暂停回调,
  不开放匿名回退。

签名协议 (n8n 侧配置 HTTP Request Header 节点):
  X-Zops-Timestamp: unix 秒
  X-Zops-Nonce:     每次回调唯一随机串
  X-Zops-Signature: hex(HMAC-SHA256(secret, "{method}\\n{path}\\n{raw_body}\\n{ts}\\n{nonce}"))

密钥: 环境变量 N8N_CALLBACK_SECRET (zops.env); 未配置 → 一律拒绝 (503 暂停语义),
绝不回退为匿名可调 — 审批结果回调可直接驱动 orchestrator 动作, 不能裸奔。
"""

import hashlib
import hmac
import json
import logging
import time

import config

logger = logging.getLogger("zops")

# 时间窗: 前后 5 分钟 (SPEC: 默认允许前后五分钟)
_TIMESTAMP_WINDOW_S = 300
# nonce 防重放: 窗口外也拦截 (nonce 键 TTL 覆盖时间窗的 2 倍)
_NONCE_TTL_S = _TIMESTAMP_WINDOW_S * 2
# 回调事件幂等去重 TTL (持久幂等; 7 天覆盖重试周期, 过期后同内容重复不再有副作用风险窗口)
_EVENT_DEDUP_TTL_S = 7 * 86400


def callback_secret() -> str:
    """读取签名密钥 (env 注入; 不落普通日志/事件)"""
    return (getattr(config, "N8N_CALLBACK_SECRET", "") or "").strip()


def _sign(secret: str, method: str, path: str, raw_body: bytes,
          ts: str, nonce: str) -> str:
    payload = f"{method.upper()}\n{path}\n{raw_body.decode('utf-8', 'replace')}\n{ts}\n{nonce}"
    mac = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


async def verify_n8n_callback(request, raw_body: bytes) -> tuple:
    """校验回调请求 (方法/路径/原始 body/时间戳/nonce/签名 + nonce 防重放)

    Returns: (ok: bool, status_code: int, reason: str)
    - 密钥未配置 → (False, 503, ...) 暂停语义 (不开放匿名回退)
    - 时间戳超窗 → (False, 401, ...)
    - nonce 已用过 → (False, 401, ...) 重放拒绝
    - 签名不符 → (False, 401, ...)
    """
    secret = callback_secret()
    if not secret:
        logger.warning("[n8n_callback] N8N_CALLBACK_SECRET 未配置, 回调已暂停 (拒绝匿名调用)")
        return False, 503, "回调签名密钥未配置, n8n 回调已暂停; 请在 zops.env 配置 N8N_CALLBACK_SECRET 并重启"

    ts = (request.headers.get("X-Zops-Timestamp") or "").strip()
    nonce = (request.headers.get("X-Zops-Nonce") or "").strip()
    sig = (request.headers.get("X-Zops-Signature") or "").strip()
    if not ts or not nonce or not sig:
        return False, 401, "缺少签名头 (X-Zops-Timestamp / X-Zops-Nonce / X-Zops-Signature)"

    try:
        ts_val = int(ts)
    except ValueError:
        return False, 401, "时间戳格式无效"
    if abs(time.time() - ts_val) > _TIMESTAMP_WINDOW_S:
        return False, 401, f"时间戳超出允许窗口 (±{_TIMESTAMP_WINDOW_S}s)"

    # 签名覆盖 方法/路径/原始 body/时间戳/nonce (常量时间比较)
    expected = _sign(secret, request.method, request.url.path, raw_body, ts, nonce)
    if not hmac.compare_digest(expected, sig):
        logger.warning(f"[n8n_callback] 签名校验失败 (path={request.url.path}, ts={ts})")
        return False, 401, "签名不匹配"

    # nonce 防重放: 同一 (nonce, ts) 只接受一次
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            nonce_key = f"{config.REDIS_PREFIX}:n8n:cb:nonce:{hashlib.sha256(f'{ts}:{nonce}'.encode()).hexdigest()}"
            if not await r.set(nonce_key, "1", ex=_NONCE_TTL_S, nx=True):
                return False, 401, "nonce 已使用 (重放拒绝)"
        finally:
            await r.aclose()
    except Exception as e:
        # Redis 故障 → fail-closed (回调驱动审批动作, 不能跳过防重放)
        logger.error(f"[n8n_callback] nonce 防重放存储异常, 拒绝本次回调: {e}")
        return False, 503, "防重放存储异常, 请稍后重试"

    return True, 200, ""


async def claim_callback_event(callback_type: str, event_key: str) -> bool:
    """回调事件持久幂等去重: 同一 (类型, 事件标识) 只处理一次

    Returns: True=首次 (处理), False=重复 (忽略)。
    """
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(config.REDIS_URL, decode_responses=True)
        try:
            k = (f"{config.REDIS_PREFIX}:n8n:cb:dedup:{callback_type}:"
                 + hashlib.sha256(str(event_key).encode("utf-8")).hexdigest()[:32])
            return bool(await r.set(k, "1", ex=_EVENT_DEDUP_TTL_S, nx=True))
        finally:
            await r.aclose()
    except Exception as e:
        logger.error(f"[n8n_callback] 幂等去重存储异常, 保守拒绝处理: {e}")
        return False  # fail-closed: 无法证明未处理过 → 不重复触发副作用


def make_signature(secret: str, method: str, path: str, body: str,
                   ts: int = None, nonce: str = "") -> dict:
    """生成签名头 (测试/调试辅助; n8n 侧可用 Code 节点同算法)"""
    import secrets as _secrets
    ts = str(int(ts if ts is not None else time.time()))
    nonce = nonce or _secrets.token_hex(8)
    sig = _sign(secret, method, path, body.encode("utf-8"), ts, nonce)
    return {"X-Zops-Timestamp": ts, "X-Zops-Nonce": nonce, "X-Zops-Signature": sig}
