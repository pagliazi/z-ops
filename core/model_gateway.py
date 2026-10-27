"""Z-OPS P3 — 统一模型调用层 (model_gateway)

SPEC P3「模型调用统一」:
  - BaseAgent / QueryEngine / Wiki ingest / embedding / synthesizer / fallback
    共用模型解析与调用层;
  - 保持 `agent Redis > agents.yaml > global Redis > env` 优先级
    (provider 与 model 同层解析, 不混层);
  - OneAPI 专用密钥优先; 凭据绑定 provider/endpoint, 不跨 provider 混用;
  - endpoint 级限流 (令牌桶) + 429/5xx 指数退避 (尊重 Retry-After) + 熔断
    (连续失败开闸, 半开探活);
  - 不改变现用模型; 无价格元数据时只约束 token/请求预算, 不报告虚构费用。

用法:
    from core.model_gateway import get_model_gateway, resolve_model_config
    cfg = await resolve_model_config(redis, agent_id="ops_agent")   # 或 agent_yaml_model
    gw = get_model_gateway()
    resp = await gw.request_chat(cfg, body, http_client)            # httpx.Response

解析语义与 BaseAgent._get_llm_config 完全一致 (该函数已改为委托此处)。
"""

import asyncio
import json
import logging
import time
from typing import Optional

import httpx

import config

logger = logging.getLogger("zops")

# ── 限流/熔断参数 (endpoint 级, 保守默认; 环境变量可调) ──
RATE_LIMIT_PER_MIN = int(getattr(config, "MODEL_GATEWAY_RPM", 0) or 0)   # 0=不限
BREAKER_FAIL_THRESHOLD = 5        # 连续失败阈值
BREAKER_OPEN_S = 60               # 熔断开闸时长
BACKOFF_MAX_RETRY = 4             # 429/5xx 最大重试
BACKOFF_BASE_DELAY = 1.5          # 指数退避基数


async def verified_model_names(redis) -> dict:
    """网关验证过的模型名映射 (hash: 请求名 → 网关返回的真实 model 名)。

    来源: llm_test / 实际调用成功后回写。展示层优先用它 (该是什么模型就是什么模型),
    静态 MODEL_ALIASES 只作未验证时的兜底。
    """
    try:
        return await redis.hgetall(config.KEY_MODEL_NAMES) or {}
    except Exception:
        return {}


def _display_real_model(vmap: dict, model: str) -> str:
    return vmap.get(model) or config.MODEL_ALIASES.get(model, model)


async def display_real_model(redis, model: str) -> str:
    """展示用真实模型名: 已验证映射 > 静态别名表 > 原名"""
    return _display_real_model(await verified_model_names(redis), model)


async def resolve_model_config(redis, agent_id: str = "",
                               agent_yaml_model: str = "",
                               agent_config_model: str = "") -> dict:
    """统一模型解析 (SPEC 优先级链; 与 BaseAgent 原实现语义一致)

    provider 和 model 必须来自同一层级, 避免 provider=oneapi 但 model=deepseek-chat。
    凭据按 provider 绑定, 不跨 provider 混用。
    """
    pref = {}
    try:
        pref_raw = await redis.get(config.KEY_LLM_PREF)
        if pref_raw:
            pref = json.loads(pref_raw)
    except Exception:
        pref = {}

    agent_provider, agent_model_name = "", ""
    agent_model = (redis and agent_id and
                   await redis.get(config.key_agent_model(agent_id))) or ""
    agent_model = agent_model or agent_yaml_model or agent_config_model
    if agent_model:
        if "/" in agent_model:
            p, m = agent_model.split("/", 1)
            agent_provider, agent_model_name = p, m
        else:
            agent_model_name = agent_model

    if agent_provider:
        provider, model = agent_provider, agent_model_name
    elif pref.get("provider"):
        provider = pref["provider"]
        model = agent_model_name or pref.get("model", "")
    else:
        provider = config.LLM_PROVIDER
        model = agent_model_name or config.LLM_MODEL

    # 凭据绑定 provider (OneAPI 专用密钥优先; 不 fallback 到其他 provider 的 key)
    if provider == "oneapi":
        # 2026-10-18: 移除 `or pref.get("api_key")` 兜底 — top-level api_key 属于当前 provider,
        # 曾在 oneapi_key 未存时把别家 provider 的 key 串给 oneapi (与"凭据不跨 provider 混用"自相矛盾)
        api_key = pref.get("oneapi_key") or config.ONEAPI_API_KEY or ""
    elif provider == "anthropic":
        api_key = pref.get("anthropic_key") or config.ANTHROPIC_API_KEY or ""
    else:
        api_key = pref.get("api_key") or config.OPENAI_API_KEY or config.DEEPSEEK_API_KEY or ""

    # base_url 兜底: pref 未存 → 自定义 provider 定义的默认 URL (兑现前端「留空则使用默认地址」)
    base_url = pref.get("base_url") or ""
    if not base_url and provider not in ("oneapi", "anthropic"):
        try:
            cp_raw = await redis.get(getattr(config, "KEY_CUSTOM_PROVIDERS", "")) if redis else None
            for cp in (json.loads(cp_raw) if cp_raw else []):
                if cp.get("id") == provider and cp.get("base_url"):
                    base_url = cp["base_url"]
                    break
        except Exception:
            base_url = ""

    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "anthropic_key": pref.get("anthropic_key") or config.ANTHROPIC_API_KEY,
        "oneapi_key": pref.get("oneapi_key") or config.ONEAPI_API_KEY,
        "oneapi_base_url": pref.get("oneapi_base_url") or config.ONEAPI_BASE_URL,
        "sanitize_enabled": pref.get("sanitize_enabled", True),
    }


class _EndpointState:
    """单 endpoint 的限流桶 + 熔断器"""
    def __init__(self):
        self.window_start = time.time()
        self.count = 0
        self.consecutive_fails = 0
        self.breaker_open_until = 0.0

    def rate_allow(self) -> bool:
        if RATE_LIMIT_PER_MIN <= 0:
            return True
        now = time.time()
        if now - self.window_start >= 60:
            self.window_start, self.count = now, 0
        self.count += 1
        return self.count <= RATE_LIMIT_PER_MIN

    def breaker_blocked(self) -> bool:
        return time.time() < self.breaker_open_until

    def record_success(self):
        self.consecutive_fails = 0

    def record_failure(self):
        self.consecutive_fails += 1
        if self.consecutive_fails >= BREAKER_FAIL_THRESHOLD:
            self.breaker_open_until = time.time() + BREAKER_OPEN_S
            self.consecutive_fails = 0
            logger.warning("[model_gateway] 熔断开闸 %.0fs (endpoint 连续失败)", BREAKER_OPEN_S)


class _BreakerOpen(Exception):
    pass


class ModelGateway:
    """endpoint 级 限流 + 退避 + 熔断 的统一 HTTP 执行层。

    不解析业务语义 (tool_calls/截断升限等由调用方处理);
    只统一: 发请求前的限流/熔断门, 429/5xx 的退避重试, 成功/失败计数。
    """

    def __init__(self):
        self._endpoints: dict = {}

    def _state(self, url: str) -> _EndpointState:
        if url not in self._endpoints:
            self._endpoints[url] = _EndpointState()
        return self._endpoints[url]

    def stats(self) -> dict:
        return {url: {"req_per_min": s.count, "consecutive_fails": s.consecutive_fails,
                      "breaker_open": s.breaker_blocked()}
                for url, s in self._endpoints.items()}

    def preflight(self, base_url: str) -> None:
        """流式等特殊请求的前置门: 熔断检查 + 限流令牌 (执行前调用; 结束配对 report)"""
        st = self._state(base_url.rstrip("/"))
        if st.breaker_blocked():
            raise _BreakerOpen(f"endpoint {base_url} 熔断中")
        if not st.rate_allow():
            raise _BreakerOpen(f"endpoint {base_url} 本地限流 (>{RATE_LIMIT_PER_MIN}/min)")

    def report(self, base_url: str, ok: bool) -> None:
        """preflight 的配对上报 (成功清零连续失败; 失败累计触发熔断)"""
        st = self._state(base_url.rstrip("/"))
        st.record_success() if ok else st.record_failure()

    async def request_chat(self, cfg: dict, body: dict,
                           client: httpx.AsyncClient,
                           path: str = "/chat/completions",
                           req_kwargs: dict = None) -> httpx.Response:
        """OpenAI 兼容 chat 请求 (限流+熔断+429/5xx 退避)。

        Returns: 最终 httpx.Response (可能是 429/5xx — 耗尽重试后如实返回)。
        Raises:  _BreakerOpen (endpoint 熔断中, 调用方快速失败)。
        """
        base_url = (cfg.get("base_url") or "").rstrip("/")
        if not base_url:
            # 复用引擎侧 PROVIDER_URL_MAP 解析 (保持既有路由表)
            try:
                from webchat_api import PROVIDER_URL_MAP
                base_url = (PROVIDER_URL_MAP.get(cfg.get("provider", ""), "")
                            or "https://api.openai.com").rstrip("/")
            except Exception:
                base_url = "https://api.openai.com"
            if cfg.get("provider") == "oneapi":
                base_url = (cfg.get("oneapi_base_url") or base_url).rstrip("/")
        url = f"{base_url}{path}"
        st = self._state(base_url)
        if st.breaker_blocked():
            raise _BreakerOpen(f"endpoint {base_url} 熔断中")
        headers = {"Authorization": f"Bearer {cfg.get('api_key', '')}",
                   "Content-Type": "application/json"}

        attempt = 0
        last_resp: Optional[httpx.Response] = None
        while True:
            if not st.rate_allow():
                # endpoint 限流: 本地拒绝 (不虚构远端原因), 按退避节奏让出
                await asyncio.sleep(BACKOFF_BASE_DELAY * (2 ** min(attempt, 3)))
                if attempt >= BACKOFF_MAX_RETRY:
                    raise _BreakerOpen(f"endpoint {base_url} 本地限流 (>{RATE_LIMIT_PER_MIN}/min)")
            try:
                resp = await client.post(url, headers=headers,
                                         json=body, **(req_kwargs or {}))
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                st.record_failure()
                attempt += 1
                if attempt > BACKOFF_MAX_RETRY:
                    raise
                await asyncio.sleep(BACKOFF_BASE_DELAY * (2 ** (attempt - 1)))
                continue
            last_resp = resp
            if resp.status_code == 429 or resp.status_code >= 500:
                st.record_failure()
                attempt += 1
                if attempt > BACKOFF_MAX_RETRY:
                    return resp        # 耗尽重试: 如实返回最终响应
                retry_after = 0.0
                try:
                    ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                    retry_after = min(float(ra), 60.0) if ra else 0.0
                except (TypeError, ValueError):
                    retry_after = 0.0
                delay = retry_after or (BACKOFF_BASE_DELAY * (2 ** (attempt - 1)))
                logger.warning(f"[model_gateway] {resp.status_code} 退避 {delay:.1f}s "
                               f"(attempt {attempt}/{BACKOFF_MAX_RETRY}, {base_url})")
                await asyncio.sleep(delay)
                continue
            st.record_success()
            return resp


    def request_chat_sync(self, cfg: dict, body: dict, path: str = "/chat/completions",
                          timeout: float = 90.0):
        """同步变体 (synthesizer/wiki_vector 等同步路径) — 同一套限流/退避/熔断"""
        import httpx as _hx
        base_url = (cfg.get("base_url") or "").rstrip("/")
        if not base_url:
            try:
                from webchat_api import PROVIDER_URL_MAP
                base_url = (PROVIDER_URL_MAP.get(cfg.get("provider", ""), "")
                            or "https://api.openai.com").rstrip("/")
            except Exception:
                base_url = "https://api.openai.com"
        if not base_url.startswith(("http://", "https://")):
            # 体检修复: 空/无协议 base_url 早失败并给出可定位的错误 (而非 UnsupportedProtocol)
            raise RuntimeError(
                f"模型网关: base_url 缺失或无协议 (provider={cfg.get('provider')!r}, "
                f"base_url={base_url!r}); 请检查管理页模型配置的 Base URL")
        url = f"{base_url}{path}"
        st = self._state(base_url)
        if st.breaker_blocked():
            raise _BreakerOpen(f"endpoint {base_url} 熔断中")
        headers = {"Authorization": f"Bearer {cfg.get('api_key', '')}",
                   "Content-Type": "application/json"}
        attempt = 0
        with _hx.Client(timeout=timeout, verify=False) as client:
            while True:
                if not st.rate_allow():
                    if attempt >= BACKOFF_MAX_RETRY:
                        raise _BreakerOpen(f"endpoint {base_url} 本地限流")
                    time.sleep(BACKOFF_BASE_DELAY)
                    attempt += 1
                    continue
                try:
                    resp = client.post(url, headers=headers, json=body)
                except _hx.HTTPError:
                    st.record_failure()
                    attempt += 1
                    if attempt > BACKOFF_MAX_RETRY:
                        raise
                    time.sleep(BACKOFF_BASE_DELAY * (2 ** (attempt - 1)))
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    st.record_failure()
                    attempt += 1
                    if attempt > BACKOFF_MAX_RETRY:
                        return resp
                    try:
                        ra = resp.headers.get("Retry-After")
                        delay = min(float(ra), 60.0) if ra else \
                            BACKOFF_BASE_DELAY * (2 ** (attempt - 1))
                    except (TypeError, ValueError):
                        delay = BACKOFF_BASE_DELAY * (2 ** (attempt - 1))
                    time.sleep(delay)
                    continue
                st.record_success()
                return resp



_gateway: Optional[ModelGateway] = None


def get_model_gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway
