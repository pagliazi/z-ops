"""
Z-OPS 运维智能体 — 配置管理模块
统一管理所有环境变量和配置项
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent

# 加载环境文件 (优先级: 命令行 > 系统环境变量 > .env 文件)
for env_file in ["zops.env", "webchat.env", "feishu.env"]:
    env_path = BASE_DIR / env_file
    if env_path.exists():
        load_dotenv(env_path, override=False)


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


# ═══ 项目标识 ═══
APP_NAME = "Z-OPS"
APP_DESC = "Z-OPS 运维智能体"
VERSION = "2.1.0"  # 版本唯一真源: 全部页面徽标/FastAPI元数据均引用此处, 修改只改这一行
REDIS_PREFIX = "zops"  # Redis 键前缀

# ═══ Redis ═══
REDIS_URL = get("REDIS_URL", "redis://127.0.0.1:6379/0")

# ═══ LLM ═══
# 默认使用 OneAPI 网关 + deepseek-v3-0324 (别名，真实模型: glm5-pg1 智谱GLM5)
# 注意: OneAPI 网关别名 deepseek-v3-0324 实际映射到 glm5-pg1
# Token 只能用别名请求，展示层通过 MODEL_ALIASES 映射真实模型名
LLM_PROVIDER = get("LLM_PROVIDER", "oneapi")
LLM_MODEL = get("LLM_MODEL", "deepseek-v3-0324")
MODEL_ALIASES = {"deepseek-v3-0324": "glm5-pg1", "openclaw:svc-opscore": "(不可用)"}
# 网关验证过的模型名映射 (hash: 请求名 → 网关返回的真实 model 名), 展示层优先于 MODEL_ALIASES
KEY_MODEL_NAMES = f"{REDIS_PREFIX}:model_names"
DEEPSEEK_API_KEY = get("DEEPSEEK_API_KEY")
OPENAI_API_KEY = get("OPENAI_API_KEY")
OPENAI_BASE_URL = get("OPENAI_BASE_URL")
ANTHROPIC_API_KEY = get("ANTHROPIC_API_KEY")
ONEAPI_API_KEY = get("ONEAPI_API_KEY")
ONEAPI_BASE_URL = get("ONEAPI_BASE_URL", "https://aigw-noc.example.com/v1")

# ═══ Web Dashboard ═══
WEBCHAT_PORT = get_int("WEBCHAT_PORT", 7789)
WEBCHAT_HOST = get("WEBCHAT_HOST", "0.0.0.0")
WEBCHAT_AUTH_USER = get("WEBCHAT_AUTH_USER", "admin")
WEBCHAT_AUTH_PASS = get("WEBCHAT_AUTH_PASS")  # ⚠️ 必须通过环境变量注入，无默认值
REPLY_TIMEOUT = get_int("REPLY_TIMEOUT", 360)  # opscore_agent 多轮 LLM + 工具调用可能需要 4-5 分钟
# 备用模型降级: 主模型网络超时后自动切换的重试模型 (空 = 同模型重试)
LLM_FALLBACK_MODEL = get("LLM_FALLBACK_MODEL", "")
# v4.0 备用模型链 (逗号分隔, 优先级从左到右; 自动兼容单模型 LLM_FALLBACK_MODEL)
LLM_FALLBACK_MODELS = [m.strip() for m in get("LLM_FALLBACK_MODELS", "").split(",") if m.strip()] or (
    [LLM_FALLBACK_MODEL] if LLM_FALLBACK_MODEL else []
)
LLM_FALLBACK_MAX_FAILURES = get_int("LLM_FALLBACK_MAX_FAILURES", 2)  # 主模型连续失败 N 次后切换

# ═══ v4.0 工具执行治理 ═══
TOOL_TIMEOUT_DEFAULT = get_int("TOOL_TIMEOUT_DEFAULT", 60)   # 工具默认超时 (秒)
TOOL_TIMEOUT_SLOW = get_int("TOOL_TIMEOUT_SLOW", 180)       # SSH/批量类慢工具超时 (秒)

# ═══ v4.0 上下文整形管线 (借鉴 claude-code 五级 shapers) ═══
TOOL_RESULT_MAX_CHARS = get_int("TOOL_RESULT_MAX_CHARS", 6000)  # 单工具结果预算 (超限转 [REF] 引用)
CTXREF_TTL = get_int("CTXREF_TTL", 3600)                        # 内容引用 Redis 保留时长
CONTEXT_SNIP_THRESHOLD = float(get("CONTEXT_SNIP_THRESHOLD", "0.70"))  # 70% 触发 snip
CONTEXT_COLLAPSE_AGE = get_int("CONTEXT_COLLAPSE_AGE", 3)       # 连续 N 轮未引用的工具结果折叠

# ═══ v4.0 Tool-call loop checkpoint ═══
QE_CHECKPOINT_TTL = get_int("QE_CHECKPOINT_TTL", 1800)  # checkpoint 保留 30 分钟

# ═══ v4.0 风险分级审批 (Phase 4) ═══
APPROVAL_ENABLED = get_bool("APPROVAL_ENABLED", True)   # destructive 工具审批门开关
# 审批只走流程 (v4.0.2): destructive 操作一律立即拦截, 系统绝不自动执行;
# 审批单仅为流程留痕 (批准/拒绝写审计), 实际执行由运维人员按变更流程线下完成;
# 单据 Redis TTL 24h 后自然作废。此字段保留仅为兼容旧环境变量, 不再参与任何逻辑。
APPROVAL_TIMEOUT = get_int("APPROVAL_TIMEOUT", 1800)

# ═══ P0: n8n 回调签名密钥 (core/n8n_callback_auth) ═══
# 签名协议: HMAC-SHA256 over "METHOD\nPATH\nraw_body\nts\nnonce",
# 头 X-Zops-Timestamp / X-Zops-Nonce / X-Zops-Signature, 时间窗 ±300s。
# 未配置 → 回调端点整体暂停 (拒绝匿名调用), 不提供关闭校验的开关。
N8N_CALLBACK_SECRET = (get("N8N_CALLBACK_SECRET") or "").strip()
N8N_WEBHOOK_KEY = (get("N8N_WEBHOOK_KEY") or "").strip()   # P0 体检补漏: trigger 端点鉴权

# ═══ P1: 任务运行时 Streams 消费开关 (readiness 达标 Redis>=6.2 才实际生效) ═══
# OPS-CORE 多对象只读试点: 开启后 route=opscore_agent 的 chat 任务经队列消费派发。
TR_STREAMS_ENABLED = get_bool("ZOPS_TR_STREAMS", False)

# P2 收尾: 用户画像 (Hermes Honcho) — 默认关闭; 开启后仅显式记忆写入, 推断不转授权
USER_PROFILE_ENABLED = get_bool("ZOPS_USER_PROFILE", False)

# P0/P2 收尾: 登记脚本的 Docker 隔离镜像 (空=不用 docker, 走本地 prlimit 沙箱)
SCRIPT_DOCKER_IMAGE = (get("ZOPS_SCRIPT_DOCKER_IMAGE") or "").strip()

# Computer Use 域名白名单 (空=不限域名只用黑名单; 逗号分隔域名列表)
CU_ALLOWED_DOMAINS = (get("ZOPS_CU_ALLOWED_DOMAINS") or "").strip()

# 管理员上限: 桌面接管默认不在服务端上限内; 需显式开启, 且本机仍须验收+原生批准
CU_DESKTOP_CEILING = get_bool("ZOPS_CU_DESKTOP_CEILING", False)

# 管理员上限: 已登记脚本默认放行 (本机仍须显式登记+哈希匹配); 可整体收紧
CU_SCRIPT_CEILING = get_bool("ZOPS_CU_SCRIPT_CEILING", True)

# ═══ v4.0 确定性优先分析 (Phase 4.4, 借鉴 K8sGPT 先规则后 LLM) ═══
DETERMINISTIC_FIRST = get_bool("DETERMINISTIC_FIRST", True)  # NMS 告警分析: 规则引擎先出分类/优先级结论, LLM 只做解释

# ═══ v4.0 工具 schema 延迟加载 (Phase 2.3, opscore_agent 试点) ═══
TOOL_SCHEMA_LAZY_LOAD = get_bool("TOOL_SCHEMA_LAZY_LOAD", True)  # True: 只注入工具名+一句话描述 + search_tool_detail 元工具
TOOL_SCHEMA_LAZY_MIN_TOOLS = get_int("TOOL_SCHEMA_LAZY_MIN_TOOLS", 10)  # 工具数低于此值不启用 (schema 本身不大)

# ═══ v4.0 wiki 混合检索 (Phase 3) ═══
EMBEDDING_MODEL = get("EMBEDDING_MODEL", "text-embedding-3-small")  # OneAPI embedding 模型
HYBRID_RECALL_TOPK = get_int("HYBRID_RECALL_TOPK", 10)  # BM25/向量各取 top-K 后融合

# ═══ v4.0 后台记忆合成 (Phase 3.2, 借鉴 ChatGPT Dreaming V3) ═══
MEMORY_SYNTHESIS_ENABLED = get_bool("MEMORY_SYNTHESIS_ENABLED", True)  # 每日 02:00 空闲期合成 + 显式记忆指令

# ═══ 飞书 ═══
FEISHU_WEBHOOK_URL = get("FEISHU_WEBHOOK_URL")
FEISHU_APP_ID = get("FEISHU_APP_ID")
FEISHU_APP_SECRET = get("FEISHU_APP_SECRET")
FEISHU_VERIFICATION_TOKEN = get("FEISHU_VERIFICATION_TOKEN")
# 问答归户 · 通讯录自动绑定: 映射表未命中时查飞书通讯录 (employee_no/邮箱前缀匹配工号)，
# 需应用开通「获取用户基本信息」等通讯录权限；未开通时自动负缓存回落手工绑定引导。
FEISHU_AUTO_BIND = get_bool("FEISHU_AUTO_BIND", True)
# 审批申请站点预置清单 (下拉选项; 与历史审批单站点去重合并下发, 逗号分隔可经环境变量扩展)
APPROVAL_SITES = get("APPROVAL_SITES", "东莞, 深圳-观澜, 香港, 上海, 北京, 广州")
# 站点名 → VM 数据匹配关键词 (VM 查询范围裁剪: name/vcenter_server/datacenter 等字段包含匹配)
VM_SITE_KEYWORDS = {
    "香港": ["HKG", "HKHKG"],
    "东莞": ["YUD", "DGDG"],
    "深圳": ["SZX", "SZXGC", "观澜"],
    "上海": ["SHHG", "SHSHA"],
    "北京": ["BJS", "BJBJS"],
    "广州": ["GZCA", "广州"],
    "新加坡": ["SIN", "新加坡"],
}
# 飞书事件接收 HTTP 服务端口（应用机器人事件订阅回调地址，公网需反代/放通该端口）
FEISHU_EVENT_PORT = get_int("FEISHU_EVENT_PORT", 8003)

# ═══ SSH ═══
SHELL_TIMEOUT = get_int("SHELL_TIMEOUT", 30)
SSH_TIMEOUT = get_int("SSH_TIMEOUT", 15)

# ═══ 虚拟化平台 ═══
VCENTER_HOST = get("VCENTER_HOST")
VCENTER_USER = get("VCENTER_USER")
VCENTER_PASS = get("VCENTER_PASS")  # ⚠️ 必须通过环境变量注入
PVE_HOST = get("PVE_HOST")
PVE_TOKEN_ID = get("PVE_TOKEN_ID")
PVE_TOKEN_SECRET = get("PVE_TOKEN_SECRET")  # ⚠️ 必须通过环境变量注入
KVM_HOSTS = get("KVM_HOSTS")

# ═══ 网络设备 ═══
NETMIKO_DEVICE_TYPE = get("NETMIKO_DEVICE_TYPE", "cisco_ios")
SNMP_COMMUNITY = get("SNMP_COMMUNITY", "public")

# ═══ OPS-CORE 平台 ═══
# 2026-10-27 起 145 改为 Token 鉴权, 并已关闭 AD 账号本地密码兜底:
# 脚本一律 Authorization: Bearer <token>; 401/403 直接失败, 禁止回退 svc-opscore + 密码登录
# (每次密码登录都是一次 AD 绑定, 密码一变更并发重试就会锁号; session 也只有 24h)
# 迁移说明: https://opscore-info.example.com/handoff/OPSCORE_AI_TOKEN_MIGRATION.md
OPSCORE_INFO_URL = get("OPSCORE_INFO_URL", "https://opscore-info.example.com")  # 信息管理系统
OPSCORE_API_TOKEN = get("OPSCORE_API_TOKEN")  # ⚠️ 必须通过环境变量注入 (scope all + IP 白名单)
OPSCORE_MGT_URL = get("OPSCORE_MGT_URL", "https://198.18.18.146")     # 管理平台
OPSCORE_MGT_USER = get("OPSCORE_MGT_USER")
OPSCORE_MGT_PASS = get("OPSCORE_MGT_PASS")  # ⚠️ 必须通过环境变量注入

# ═══ OPS-CORE DNS API (信息管理系统 /api/dns/* 新接口, 与 OPSCORE_INFO 共用同一 Token) ═══
# 接口为 POST JSON；鉴权同 OPSCORE_API_TOKEN (Bearer)；文档: /api/docs/ (Swagger) → /api/openapi.json
DNS_API_URL = get("DNS_API_URL", "https://opscore-info.example.com")
DNS_API_TIMEOUT = get_int("DNS_API_TIMEOUT", 20)           # 单次 DNS API 超时 (秒)
DNS_API_CACHE_TTL = get_int("DNS_API_CACHE_TTL", 300)      # 解析结果内存缓存 (秒)
# NMS 告警分析自动 DNS 解析 (从告警摘要/节点提取 IP 与域名 → 反向/正向解析注入分析上下文)
NMS_DNS_RESOLVE_ENABLED = get_bool("NMS_DNS_RESOLVE_ENABLED", True)
NMS_DNS_RESOLVE_MAX = get_int("NMS_DNS_RESOLVE_MAX", 20)   # 每轮分析最多解析的目标数 (IP+域名)

# ═══ 知识库 (KB) ═══
KB_API_URL = get("KB_API_URL", "http://198.18.18.166:8089")
KB_API_TOKEN = get("KB_API_TOKEN")  # ⚠️ 必须通过环境变量注入
KB_SEARCH_LIMIT = get_int("KB_SEARCH_LIMIT", 5)
KB_RAG_ENABLED = get_bool("KB_RAG_ENABLED", True)  # 对话前自动查询 KB 做 RAG 增强
# RAG 强相关性门槛 (2026-10-18): hybrid 融合分是名次归一化、无法区分相关性 (垃圾查询 top1 也是 1.0),
# 唯一有效信号是 semantic_score (BGE 原始余弦): 无关查询 ~0.40-0.46, 相关查询 ~0.6-0.8。
# general_agent 的环境 RAG 注入/引用仅采纳 ≥ 此值或有词法精确命中 (title_match/text_match) 的结果;
# 低于阈值 → 视为无匹配: 不注入上下文、不挂"参考来源" (修"自我介绍挂5条不相关引用")。
KB_RAG_SEM_MIN = get_float("KB_RAG_SEM_MIN", 0.55)

# Outline 原生 API — 用于将 KB 包装器返回的截断短链接 (/doc/{urlId})
# 解析为带 slug 的规范链接 (/doc/{slug}-{urlId})，避免用户点击 404。
KB_URL_RESOLVE = get_bool("KB_URL_RESOLVE", True)  # 是否解析规范文档链接
OUTLINE_API_URL = get("OUTLINE_API_URL", "https://opscore-outline.example.com/api")
OUTLINE_API_TOKEN = get("OUTLINE_API_TOKEN")  # ⬆ 通过环境变量注入

# ═══ HA VM 高可用信息 (HA Agent) ═══
HA_API_URL = get("HA_API_URL", "http://198.18.18.166:8088")
HA_API_TOKEN = get("HA_API_TOKEN")  # ⚠️ 必须通过环境变量注入，TODO: 从管理员获取

# ═══ Redis 键名规范 ═══
KEY_HEARTBEATS = f"{REDIS_PREFIX}:heartbeats"
KEY_CHANNEL_HEARTBEATS = f"{REDIS_PREFIX}:channel_heartbeats"
KEY_CHAT_HISTORY = f"{REDIS_PREFIX}:chat_history"
KEY_LLM_PREF = f"{REDIS_PREFIX}:llm_pref"
KEY_LLM_USAGE = f"{REDIS_PREFIX}:llm_usage"          # 最近 N 条调用日志 (ltrim 截断, 只作日志清单用)
KEY_LLM_USAGE_TOTAL = f"{REDIS_PREFIX}:llm_usage:total" # 累计调用计数器 (incr 不截断, 展示真实总量)
KEY_TASKS = f"{REDIS_PREFIX}:tasks"
KEY_ONEAPI_KEYS = f"{REDIS_PREFIX}:oneapi_keys"   # Hash: token_name -> JSON{token, created_at, created_by}
KEY_CUSTOM_PROVIDERS = f"{REDIS_PREFIX}:custom_providers"  # String: JSON array of custom LLM providers
KEY_USER_OPS_LOG = f"{REDIS_PREFIX}:user_ops_log"  # 用户操作日志 (按日: zops:user_ops:YYYY-MM-DD)


def key_agent_model(agent_id: str) -> str:
    """Per-Agent 模型覆盖配置键"""
    return f"{REDIS_PREFIX}:agent_model:{agent_id}"


def user_ops_key(date_str: str) -> str:
    """用户操作日志按日键名"""
    return f"{REDIS_PREFIX}:user_ops:{date_str}"

CHAT_HISTORY_MAX = get_int("CHAT_HISTORY_MAX", 0)     # 0 = 不截断，全量保留 (审计要求)
DAILY_LOG_TTL = get_int("DAILY_LOG_TTL", 0)          # 0 = 永不过期 (审计长期保存，另每日落盘归档)
USER_OPS_LOG_TTL = get_int("USER_OPS_LOG_TTL", 0)    # 0 = 永不过期 (审计长期保存)

# 用户审计排除名单 (临时账号/测试账号/默认管理员, 不进入"用户汇总"统计)
# 写法: 精确用户名 或 shell-style 通配符 (* 匹配任意字符, ? 匹配单字符)
# 默认排除: tmp_*/v4t_*/test_*/scratch_*/demo_* 等临时账号; admin 视业务决定
import fnmatch as _fnmatch
def _parse_exclude_list(raw: str):
    return [p.strip() for p in (raw or "").split(",") if p.strip()]
AUDIT_USER_EXCLUDE_PATTERNS = _parse_exclude_list(get(
    "AUDIT_USER_EXCLUDE_PATTERNS",
    # 临时/测试账号前缀
    "tmp_*,tmp-*,v4t_*,v4t-*,test_*,test-*,scratch_*,demo_*,adapt_*,adapt-*,tmpop*,tmpop_*"
    # 具体精确用户名 (大系统账号/无凭证测试账号/历史脚本账号)
    ",admin,op9999nocred,verify_script",
))
def is_audit_excluded(username: str) -> bool:
    if not username:
        return True
    for p in AUDIT_USER_EXCLUDE_PATTERNS:
        if p == username:
            return True
        if _fnmatch.fnmatchcase(username, p):
            return True
    return False

def user_llm_key(date_str: str) -> str:
    """用户级 LLM 用量按日键名 (Hash: {username}:{metric} -> int)"""
    return f"{REDIS_PREFIX}:user_llm:{date_str}"

# ═══ 用户认证 ═══
TOKEN_TTL = get_int("TOKEN_TTL", 604800)  # Token 有效期 7天 (604800秒)
JWT_SECRET = get("JWT_SECRET")  # ⚠️ 必须通过环境变量注入，无默认值
KEY_USERS = f"{REDIS_PREFIX}:users"                  # Hash: username -> JSON{password_hash, display_name, role, group, created_at}
KEY_FEISHU_USER_MAP = f"{REDIS_PREFIX}:feishu_user_map"  # Hash: 飞书 open_id -> 系统用户名 (bot 问答归户绑定, 首次对话时自动引导绑定)
KEY_GROUP_TABS = f"{REDIS_PREFIX}:group_tabs"        # Hash: group_name -> JSON array of visible tab hrefs
KEY_GROUP_NMS = f"{REDIS_PREFIX}:group_nms"          # Hash: group_name -> JSON array of NMS alarm group names
KEY_GROUP_BIZPERMS = f"{REDIS_PREFIX}:group_biz_perms"
# 权限使用度量 (hash: used:{perm} / denied:{perm} 累计; 定期复核依据)
KEY_PERM_METRICS = f"{REDIS_PREFIX}:perm_metrics"  # Hash: used:{perm} / denied:{perm} 计数 (权限门通过/拒绝累计)
KEY_CUSTOM_GROUPS = f"{REDIS_PREFIX}:custom_groups"  # String: JSON array of custom user group names (内置组外的自定义组)
KEY_NOTIFY_GROUPS = f"{REDIS_PREFIX}:notify_groups"  # Hash: group_name -> JSON{name, description, members[{username,display_name,mail}], ...}
# NMS 告警通知/飞书/截图动态配置（String: JSON，前端管理页可写，优先级高于环境变量静态值）
KEY_NMS_DYN_CFG = f"{REDIS_PREFIX}:nms_dyn_cfg"
# Checklist 预案覆盖层: xlsx 打底, Hash 中的自定义/编辑预案优先同名覆盖;
# deleted Set 标记从匹配中移除的 xlsx 预案（源文件保留，可恢复）
KEY_NMS_CHECKLIST_PLANS = f"{REDIS_PREFIX}:nms_checklist_plans"
KEY_NMS_CHECKLIST_DELETED = f"{REDIS_PREFIX}:nms_checklist_plans:deleted"
DEFAULT_ADMIN_USER = WEBCHAT_AUTH_USER                # 默认管理员账号
DEFAULT_ADMIN_PASS = WEBCHAT_AUTH_PASS                # 默认管理员密码（来自环境变量）

# 用户组定义
USER_GROUPS = ["OPS-CORE", "GNOC", "PSC", "COPS", "NQA", "SOC", "NSC", "CNS", "PROV"]

# ═══ 安全配置 (v2.0.0 增强) ═══
# 所有敏感配置必须通过环境变量或 SecretRef 注入，禁止硬编码默认值

# ═══ LDAP / AD 认证 ═══
LDAP_ENABLED = get_bool("LDAP_ENABLED", True)
LDAP_SERVER_URI = get("LDAP_SERVER_URI", "ldap://198.19.5.191:3268")
LDAP_BIND_DN = get("LDAP_BIND_DN", "CN=noc_app_scc,OU=Special Accounts,DC=cpcnet,DC=local")
LDAP_BIND_PASSWORD = get("LDAP_BIND_PASSWORD")  # ⚠️ 必须通过环境变量注入，无默认值
LDAP_SEARCH_BASE = get("LDAP_SEARCH_BASE", "OU=China,OU=CPCNet User,DC=cpcnet,DC=local")
LDAP_SEARCH_FILTER = get("LDAP_SEARCH_FILTER", "(sAMAccountName=%(user)s)")
LDAP_DEFAULT_ROLE = get("LDAP_DEFAULT_ROLE", "user")  # LDAP 用户默认角色
LDAP_AUTO_CREATE = get_bool("LDAP_AUTO_CREATE", False)  # 禁止 AD 自动创建账户

# ═══ 邮件通知 (SMTP) ═══
# 方式1: 通过内网 relay 发送 (需配置 SMTP_HOST)
# 方式2: MX 直投 — 直连目标域 MX 服务器 (SMTP_MX_DIRECT=true)
SMTP_MX_DIRECT = get_bool("SMTP_MX_DIRECT", True)  # 默认 MX 直投，内网 relay 不可达时使用
SMTP_HOST = get("SMTP_HOST", "mail.example.com")  # MX 直投时为目标 MX 服务器
SMTP_PORT = get_int("SMTP_PORT", 25)
SMTP_USE_TLS = get_bool("SMTP_USE_TLS", False)
SMTP_USER = get("SMTP_USER")  # MX 直投模式下无需认证
SMTP_PASSWORD = get("SMTP_PASSWORD")
SMTP_FROM = get("SMTP_FROM", "zops-notify@example.com")  # 必须使用 MX 能解析的域
SMTP_EHLO_DOMAIN = get("SMTP_EHLO_DOMAIN", "opscore-ai.example.com")
ADMIN_NOTIFY_EMAIL = get("ADMIN_NOTIFY_EMAIL", "ops-admin@example.com")

# ═══ 预置 AD 管理员账号 ═══
LDAP_ADMIN_USERS = get("LDAP_ADMIN_USERS")  # ⚠️ 逗号分隔的 AD 管理员账号，必须通过环境变量注入

# ═══ 会话管理 ═══
SESSION_MSG_MAX = get_int("SESSION_MSG_MAX", 0)      # 0 = 不截断，单会话消息全量保留
SESSION_TTL = get_int("SESSION_TTL", 0)              # 0 = 会话永不过期 (所有对话历史保留)
CONTEXT_WINDOW_SIZE = get_int("CONTEXT_WINDOW_SIZE", 10)  # 传递给 agent 的最近对话轮数 (user+assistant 消息数)

# ═══ 会话压缩 (参考 claude-code compact 系统) ═══
# Token 估算: 1 token ≈ 1.5 中文字符 ≈ 4 英文字符
CONTEXT_WINDOW_TOKENS = get_int("CONTEXT_WINDOW_TOKENS", 120000)  # 模型上下文窗口 (tokens)
AUTO_COMPACT_THRESHOLD = float(get("AUTO_COMPACT_THRESHOLD", "0.80"))  # 80% 触发自动压缩
AUTO_COMPACT_BUFFER = get_int("AUTO_COMPACT_BUFFER", 15000)  # 压缩时预留 buffer tokens
COMPACT_MAX_OUTPUT_TOKENS = get_int("COMPACT_MAX_OUTPUT_TOKENS", 4000)  # 压缩摘要最大 tokens
COMPACT_MIN_MESSAGES = get_int("COMPACT_MIN_MESSAGES", 6)  # 至少 6 条消息才触发压缩
COMPACT_PRESERVE_RECENT = get_int("COMPACT_PRESERVE_RECENT", 4)  # 压缩后保留最近 4 条原始消息
MICRO_COMPACT_AGE = get_int("MICRO_COMPACT_AGE", 6)  # 超过 6 轮前的工具结果截断
MICRO_COMPACT_MAX_CHARS = get_int("MICRO_COMPACT_MAX_CHARS", 500)  # 旧工具结果截断后保留字符数
COMPACT_CONSECUTIVE_FAIL_MAX = get_int("COMPACT_CONSECUTIVE_FAIL_MAX", 3)  # 连续失败 3 次停止重试

# ═══ jieba 关键词索引 ═══
KEYWORD_TOPK = get_int("KEYWORD_TOPK", 8)                # 每条消息提取 Top-K 关键词
KEYWORD_SESSION_TTL = get_int("KEYWORD_SESSION_TTL", 86400 * 7)  # 关键词缓存 7 天
KEYWORD_MAX_PER_SESSION = get_int("KEYWORD_MAX_PER_SESSION", 50)  # 每会话最多缓存 50 个关键词

def key_session_keywords(session_id: str) -> str:
    """会话关键词索引 Redis 键"""
    return f"{REDIS_PREFIX}:kw:{session_id}"

# ═══ Per-Agent 配置 ═══
WORKSPACES_DIR = BASE_DIR / "workspaces"

# ═══ Harness 自我进化 (参考 claw-code harness engineering) ═══
HARNESS_AUTO_SKILL_ENABLED = get_bool("HARNESS_AUTO_SKILL_ENABLED", True)  # 自动技能生成
HARNESS_MIN_FAILURES = get_int("HARNESS_MIN_FAILURES", 3)  # 触发自动技能生成的最少失败次数
HARNESS_IMPROVE_INTERVAL = get_int("HARNESS_IMPROVE_INTERVAL", 3600)  # 技能改进检查间隔(秒)
EXEC_REGISTRY_TTL = get_int("EXEC_REGISTRY_TTL", 7 * 86400)  # 执行记录保留 7 天

# ═══ ToolPool 配置 ═══
TOOL_POOL_CROSS_AGENT = get_bool("TOOL_POOL_CROSS_AGENT", True)  # 启用跨 Agent 工具发现

# ═══ NMS 告警监控 (IBM Netcool) ═══
NMS_CONSOLE_URL = get("NMS_CONSOLE_URL", "https://nms.example.com:16311/ibm/console/")
NMS_USERNAME = get("NMS_USERNAME")
NMS_PASSWORD = get("NMS_PASSWORD")
NMS_CHECK_INTERVAL = get_int("NMS_CHECK_INTERVAL", 600)          # 默认 10 分钟
NMS_ALERT_SIMILARITY_TTL = get_int("NMS_ALERT_SIMILARITY_TTL", 3600)  # 去重窗口 1h (保留兼容; seen 过期已改用 NMS_SEEN_TTL)
# seen 去重表过期时间: 必须 >> 抓取周期, 且 >= 告警典型存活时长。
# 若过短, 仍活跃的告警会被反复清出 seen → 整批重报为"新增"(历史/指纹库被冲爆)。
NMS_SEEN_TTL = get_int("NMS_SEEN_TTL", 7 * 86400)
NMS_HISTORY_MAX = get_int("NMS_HISTORY_MAX", 2000)              # 老版 list 历史最大条数 (兼容过渡)
# Phase H ZSET 历史条数保险丝 (内存兜底; 保留期由 NMS_HISTORY_RETENTION_DAYS 决定)
NMS_HISTORY_ZSET_MAX = get_int("NMS_HISTORY_ZSET_MAX", 100000)
# 历史告警保留天数 (按 ZSET score = 首见时间老化); 0 = 仅按条数兜底
NMS_HISTORY_RETENTION_DAYS = get_int("NMS_HISTORY_RETENTION_DAYS", 30)
# A3 异常检测的查询窗口 (天); ZSET score 范围扫描, 默认 14 天覆盖常见业务周期
NMS_ANOMALY_HISTORY_WINDOW_DAYS = get_int(
    "NMS_ANOMALY_HISTORY_WINDOW_DAYS", 14)
# /api/nms/alarms 单次返回告警上限（活跃告警优先，避免超大响应拖垮前端）
NMS_API_ALARM_LIMIT = get_int("NMS_API_ALARM_LIMIT", 3000)
# 要监控的告警视图分组（顶部 Filter 下拉，逗号分隔）；默认聚焦 OPSCORE_use
NMS_FILTERS = [f.strip() for f in get("NMS_FILTERS", "OPSCORE_use").split(",") if f.strip()]
# 说明: 设置 NMS_FILTERS=* 可自动枚举 NMS 页面中全部可用 filter 并逐个抓取
NMS_FILTER_SETTLE = get_int("NMS_FILTER_SETTLE", 10)            # 切换分组后等待表格刷新秒数
NMS_AUTO_CHECK_ENABLED = get_bool("NMS_AUTO_CHECK_ENABLED", False)  # 新告警驱动自动化检查工作流（默认关闭）
NMS_FEISHU_PUSH_ENABLED = get_bool("NMS_FEISHU_PUSH_ENABLED", True)   # NMS 告警飞书推送总开关（默认开启，未配置 webhook 时自动跳过）
NMS_PUSH_ONLY_ON_NEW = get_bool("NMS_PUSH_ONLY_ON_NEW", True)         # 仅有新增告警时推送（避免每次定时抓取都刷屏）
# 关联降噪：把海量原始告警归并为少量「事件簇」
NMS_NOTIFY_BY_INCIDENT = get_bool("NMS_NOTIFY_BY_INCIDENT", True)     # 飞书按事件簇汇总推送（关闭则回退逐条新增告警）
NMS_SYSTEMIC_MIN_SOURCES = get_int("NMS_SYSTEMIC_MIN_SOURCES", 5)    # 系统级事件的最小跨源数
NMS_CHECKLIST_PATH = get("NMS_CHECKLIST_PATH", "/data/jupyter/OPSCORE_Check_list.xlsx")  # OPS-CORE 故障处置 checklist（xlsx，sheet=单客户/平台）
NMS_INCIDENT_TOP = get_int("NMS_INCIDENT_TOP", 10)                   # 飞书/接口 top 事件数
# /api/nms/alarms 分页页大小（前端无虚拟化，按分组分页返回，避免超大响应卡死浏览器）
NMS_PAGE_SIZE = get_int("NMS_PAGE_SIZE", 500)
NMS_PAGE_SIZE_MAX = get_int("NMS_PAGE_SIZE_MAX", 2000)
# 后台预算「分组维度」LLM 分析的目标分组：优先取各用户组的 NMS 分组映射(KEY_GROUP_NMS)之并集，
# 都为空时回退到此列表；也作为页面「用户默认分组」的兵底默认（避免展示全部重叠视图）
NMS_DEFAULT_GROUPS = [g.strip() for g in get("NMS_DEFAULT_GROUPS", "OPSCORE_use").split(",") if g.strip()]
# 补充 system scope filter 清单：TYPE=system 的 filter 不在默认元数据列表中返回
# （如 GNOC_use，owner=system，EventDataService 需 filtertype=system 查询），
# 采集时按此清单补充查询并归属；后续发现新的 system filter 在此追加即可
NMS_EXTRA_SYSTEM_FILTERS = [g.strip() for g in get("NMS_EXTRA_SYSTEM_FILTERS", "GNOC_use").split(",") if g.strip()]
NMS_ANALYSIS_MAX_GROUPS = get_int("NMS_ANALYSIS_MAX_GROUPS", 6)
# 限流降耗: 分组维度 LLM 分析降频 — 每 N 轮定时抓取才做一次；
# 新增告警数达到阈值时无视轮次立即分析（保证告警风暴不被降频）
NMS_GROUP_ANALYZE_EVERY = get_int("NMS_GROUP_ANALYZE_EVERY", 6)
NMS_GROUP_ANALYZE_MIN_NEW = get_int("NMS_GROUP_ANALYZE_MIN_NEW", 10)

# ═══ LLM 调用降耗治理（采集与 LLM 分析解耦，手动触发优先） ═══
# 定时采集默认不调用 LLM：NMS 采集轮次只抓取/去重/入库/推送（本地规则摘要零 LLM）；
# LLM 分析仅手动触发：飞书命令「nms 分析」、webchat 用户主动抓取/分组分析、
# 或开启此开关后按模式指纹门槛自动分析（新模式累计达标才触发）
# Phase 3.3: 默认改为 True (更主动的自动分析) — 安全依赖 Phase 1 三重保护:
#   token bucket (NMS_LLM_BUCKET_*) + 分组 TTL 缓存 (NMS_ANALYSIS_TTL) + 锁让出
NMS_AUTO_LLM_ANALYZE = get_bool("NMS_AUTO_LLM_ANALYZE", True)
# 同质化优化：告警按「模式指纹」（类型+规范化摘要，IP/接口等变量归一为占位符）识别；
# 已分析过的模式再出现不重复调 LLM，直接复用上次分析结论；
# 仅当「新模式」告警累计达到 N 条（或有 critical 新模式）时才重新触发 LLM 分析
NMS_LLM_NEW_PATTERN_MIN = get_int("NMS_LLM_NEW_PATTERN_MIN", 3)
# Phase 3.3: 影响面触发 — 本轮新增告警涉及的不同来源 (source/host) 数达到阈值也触发
# (即使模式未达阈值, 大范围扩散的告警风暴也需要 LLM 介入分析)
NMS_AUTO_IMPACT_MIN = get_int("NMS_AUTO_IMPACT_MIN", 8)
# Phase 3.3: 自动分析冷却 — 距上次自动 LLM 分析不足此秒数时跳过 (防止告警风暴期间连环调用)
# 手动触发 (bot 命令/立即抓取/SSE 流式) 不受冷却限制
NMS_LLM_COOLDOWN = get_int("NMS_LLM_COOLDOWN", 1800)
# 模式指纹库 Redis key TTL（秒）：模式库过久自动刷新，避免告警类型演进后仍复用旧结论
NMS_LLM_PATTERN_TTL = get_int("NMS_LLM_PATTERN_TTL", 7 * 86400)
# 单次 LLM 分析输出 token 上限（分析质量优先，保留足够篇幅）
NMS_LLM_MAX_TOKENS = get_int("NMS_LLM_MAX_TOKENS", 8000)
# 分组分析单组最大输入告警样本数（控制 prompt 长度）
NMS_LLM_GROUP_MAX_SAMPLE = get_int("NMS_LLM_GROUP_MAX_SAMPLE", 30)

# ── 性能优化 (Phase 1: 落地 wiki/concepts/opt-nms-latency-p95.md) ──
# 按需/手动分析的同组 LLM 结果 TTL 缓存（秒）：
# 同组再次分析时直接复用，避免重复调 LLM；超过 TTL 或传 ?fresh=1 时强制重算
NMS_ANALYSIS_TTL = get_int("NMS_ANALYSIS_TTL", 480)
# Scheduled (后台批量) LLM 分析的等待预算：若 on_demand 已排队，最多等这么久(秒)再放行 scheduled
NMS_SCHED_WAIT_ON_DEMAND = get_int("NMS_SCHED_WAIT_ON_DEMAND", 10)
# Scheduled 抢占到 on_demand 排队后的最大等待次数 (轮询粒度 0.1s)
NMS_SCHED_WAIT_TICKS = get_int("NMS_SCHED_WAIT_TICKS", 100)
# LLM 调用 token-bucket 限流：窗口(秒) 内最多 N 次调用，防止网关触发 429
NMS_LLM_BUCKET_WINDOW = get_int("NMS_LLM_BUCKET_WINDOW", 60)
NMS_LLM_BUCKET_MAX = get_int("NMS_LLM_BUCKET_MAX", 2)
# Scheduled 分析在 LLM 调用前若检测到 on_demand 排队是否放弃本次(避免占着网关不释放)
NMS_SCHED_YIELD_ON_DEMAND = get_bool("NMS_SCHED_YIELD_ON_DEMAND", True)

# NMS 抓取后按分组截图的视图列表（逗号分隔，空则关闭截图）
NMS_SNAPSHOT_GROUPS = get("NMS_SNAPSHOT_GROUPS", "OPSCORE_use")
# NMS 平台内置视图组（catch-all/状态/时间视图，非用户业务组）：
# 飞书分组清单与状态摘要展示时业务组优先，这些系统视图后置，避免淹没业务组
NMS_SYSTEM_VIEW_GROUPS = [g.strip() for g in get(
    "NMS_SYSTEM_VIEW_GROUPS",
    "AllEvents,Unacknowledged,Acknowledged,Escalated,EventsOutdated,Aged,Last1H,"
    "Trap_Event_All,Syslog_Event").split(",") if g.strip()]
# 分级告警截图：对每个分组额外按级别筛选截图的级别列表（逗号分隔，空则只截整组图）
# 可选: critical(严重)/major(主要)/minor(轻微)/warning(警告)/indeterminate(未知)
NMS_SNAPSHOT_SEVERITIES = get("NMS_SNAPSHOT_SEVERITIES", "critical,major,minor")
NMS_SNAPSHOT_UNACK_ONLY = get_bool("NMS_SNAPSHOT_UNACK_ONLY", True)
# 对外访问基础地址（飞书卡片中截图/控制台链接前缀，需公网可达，对应 nginx server_name）
ZOPS_PUBLIC_BASE_URL = get("ZOPS_PUBLIC_BASE_URL", "https://opscore-ai.example.com")
# 飞书 bot 交互命令队列键（Redis list）
NMS_CMD_KEY = f"{REDIS_PREFIX}:nms:cmd"
# bot 命令唤醒频道（feishu_bot 入队命令后 publish，nms_agent 监听后可立即开下一轮，无需等满周期）
NMS_WAKE_CHANNEL = f"{REDIS_PREFIX}:nms:wake"
# 飞书推送频率控制：距上次推送的最小间隔（分钟，0=每轮都推），可在飞书群用 `nms 推送间隔 <分钟>` 动态调整
NMS_PUSH_MIN_INTERVAL = get_int("NMS_PUSH_MIN_INTERVAL", 0)
# 上次飞书推送时间戳键（Redis string，epoch 秒）
KEY_NMS_LAST_PUSH_TS = f"{REDIS_PREFIX}:nms_push:last_ts"
# 飞书群订阅表（Redis Hash: chat_id -> JSON array of NMS 分组名），飞书命令 `nms 订阅 <组>` 维护（旧表，兼容保留）
KEY_NMS_SUBS = f"{REDIS_PREFIX}:nms_subs"
# 飞书多源推送通道表（Redis Hash: channel_id -> JSON），每通道=一个飞书接收源
# 结构: {id, name, type: webhook|app, webhook_url, chat_id, groups[], severities[], enabled, is_default, created_at, updated_at}
KEY_NMS_CHANNELS = f"{REDIS_PREFIX}:nms_channels"
# 默认推送分组（逗号分隔；空=全部组）。飞书命令 `nms 推送分组 <组,组>` / 前端管理页可覆盖
NMS_PUSH_GROUPS = get("NMS_PUSH_GROUPS", "")

# ═══ A5 告警抑制 (pattern 重复压制, 避免同一告警风暴反复推送) ═══
# 抑制规则存储: Redis Hash, rule_id → {pattern, max_hits, window_min, until_epoch, reason, by}
KEY_NMS_SUPPRESS_RULES = f"{REDIS_PREFIX}:nms_suppress_rules"
# 命中计数: Redis Hash, rule_id → 当前窗口内命中数 (TTL = window_min)
KEY_NMS_SUPPRESS_HITS = f"{REDIS_PREFIX}:nms_suppress_hits"
# 抑制默认窗口 (分钟, 仅在 API 未指定时使用)
NMS_SUPPRESS_DEFAULT_WINDOW = get_int("NMS_SUPPRESS_DEFAULT_WINDOW", 60)
# 抑制默认触发阈值 (窗口内同一指纹 N 次才抑制, 避免单次告警被误压)
NMS_SUPPRESS_DEFAULT_MAX_HITS = get_int("NMS_SUPPRESS_DEFAULT_MAX_HITS", 5)
# 维护窗口静音: Redis Hash, source → until_epoch (期间同源告警全部静音)
KEY_NMS_MAINT_WINDOWS = f"{REDIS_PREFIX}:nms_maint_windows"

# ═══ NMS SSL 证书调查闭环（只读取证 + 人工处置 + 自动恢复跟踪）═══
# 总开关：关闭后不再创建新调查与调度，历史调查/证据/案例仍可只读访问
NMS_SSL_WORKFLOW_ENABLED = get_bool("NMS_SSL_WORKFLOW_ENABLED", True)
# 恢复门槛：证书链可信 + 域名匹配 + 剩余有效期不少于 N 天（按秒比较）
NMS_SSL_MIN_DAYS = get_int("NMS_SSL_MIN_DAYS", 30)
# 允许取证的端口白名单（服务端强制，默认仅 443；可逗号追加如 "443,8443"）
NMS_SSL_ALLOWED_PORTS = [int(p) for p in get("NMS_SSL_ALLOWED_PORTS", "443").split(",") if p.strip()]
# 不在白名单的端口转「人工核验模式」：不做 TLS 探测，恢复需 NMS 清除持续该秒数
NMS_SSL_MANUAL_QUIET_S = get_int("NMS_SSL_MANUAL_QUIET_S", 1800)
# 可选额外 CA 文件（与系统信任库合并；留空只用系统信任库）
NMS_SSL_CA_FILE = get("NMS_SSL_CA_FILE", "")
# 连续健康次数 / 相邻最小间隔 / 最小跨度（秒）
NMS_SSL_CONSECUTIVE_HEALTHY = get_int("NMS_SSL_CONSECUTIVE_HEALTHY", 3)
NMS_SSL_HEALTHY_MIN_SPACING_S = get_int("NMS_SSL_HEALTHY_MIN_SPACING_S", 60)
NMS_SSL_HEALTHY_MIN_SPAN_S = get_int("NMS_SSL_HEALTHY_MIN_SPAN_S", 120)
# 跟踪复核间隔（秒）；24 小时跟踪窗口到期后自动暂停
NMS_SSL_CHECK_INTERVAL = get_int("NMS_SSL_CHECK_INTERVAL", 60)
NMS_SSL_TRACK_WINDOW_S = get_int("NMS_SSL_TRACK_WINDOW_S", 86400)
# 每用户最多同时跟踪的调查数 / 全进程并发探测槽
NMS_SSL_MAX_ACTIVE_PER_USER = get_int("NMS_SSL_MAX_ACTIVE_PER_USER", 10)
NMS_SSL_GLOBAL_CONCURRENCY = get_int("NMS_SSL_GLOBAL_CONCURRENCY", 2)
# 指标汇总日志间隔（秒）：<=0 关闭周期汇总，逐次采集/租约失败日志仍保留
NMS_SSL_METRICS_LOG_INTERVAL_S = get_int("NMS_SSL_METRICS_LOG_INTERVAL_S", 300)

# ═══ 通用告警闭环（L1 探针闭环 + L2 设备级簇闭环；只读核验，不自动处置）═══
# 总开关：关闭后 worker 不再刷新/探测，历史单元与案例仍可只读访问
NMS_CLOSURE_ENABLED = get_bool("NMS_CLOSURE_ENABLED", True)
# L2 簇恢复门槛：单元活跃告警清零后连续静默时长（秒）
NMS_CLOSURE_QUIET_S = get_int("NMS_CLOSURE_QUIET_S", 1800)
# L1 恢复门槛：连续健康探测次数 / 相邻最小间隔（秒）
NMS_CLOSURE_L1_HEALTHY = get_int("NMS_CLOSURE_L1_HEALTHY", 2)
NMS_CLOSURE_L1_MIN_SPACING_S = get_int("NMS_CLOSURE_L1_MIN_SPACING_S", 120)
# 连续「无法取证」达到该次数后回退簇级判据（证据标注 probe_unavailable）
NMS_CLOSURE_PROBE_UNKNOWN_LIMIT = get_int("NMS_CLOSURE_PROBE_UNKNOWN_LIMIT", 3)
# 单次探测超时与探测节流（秒）；每轮最多探测的单元数
NMS_CLOSURE_PROBE_TIMEOUT = get_int("NMS_CLOSURE_PROBE_TIMEOUT", 8)
NMS_CLOSURE_PROBE_INTERVAL = get_int("NMS_CLOSURE_PROBE_INTERVAL", 300)
NMS_CLOSURE_MAX_PROBES = get_int("NMS_CLOSURE_MAX_PROBES", 3)
# 跟踪窗口（秒）：确认后超过该时长仍未恢复 → expired（需重新确认）
NMS_CLOSURE_TRACK_WINDOW_S = get_int("NMS_CLOSURE_TRACK_WINDOW_S", 86400)
# worker 扫描间隔（秒）与 observed 单元保留时长（秒，超期自动丢弃）
NMS_CLOSURE_WORKER_INTERVAL = get_int("NMS_CLOSURE_WORKER_INTERVAL", 120)
NMS_CLOSURE_FORGET_S = get_int("NMS_CLOSURE_FORGET_S", 604800)
# 单元总量上限（超限时保留已确认单元 + 加权量最高的 observed 单元）
NMS_CLOSURE_MAX_UNITS = get_int("NMS_CLOSURE_MAX_UNITS", 3000)

# ═══ MCP (Model Context Protocol) 外部工具集成 ═══
MCP_SSE_ENDPOINT = get("MCP_SSE_ENDPOINT", "https://opscore-mgt.example.com/mcp/sse")
MCP_API_KEY = get("MCP_API_KEY", "")  # MCP REST 调用鉴权 Key
MCP_REST_BASE = get("MCP_REST_BASE", "https://opscore-mgt.example.com/api/snapshot/mcp/tools")
MCP_TIMEOUT = get_int("MCP_TIMEOUT", 30)  # MCP 工具调用超时 (秒)
MCP_VERIFY_SSL = get_bool("MCP_VERIFY_SSL", True)  # 是否验证 SSL 证书
MCP_ENABLED = get_bool("MCP_ENABLED", True)  # 是否启用 MCP 工具集成
# P3 (task-c17): 已批准 MCP server 白名单 (JSON 数组; 未登记一律拒绝, fail-closed)
#   [{"id":"opscore","transport":"rest","url":"https://...","allowed_tools":["*"]}]
#   stdio server: {"id":"local","transport":"stdio","command":"/abs/path","args":[...]}
#   官方 SDK transport (stdio/http) 经隔离 venv 验证 (mcp==2.3.0), 生产启用需按部署流程装 SDK
MCP_APPROVED_SERVERS = (get("MCP_APPROVED_SERVERS") or "").strip()

# ═══ 优化闭环 · 自动建议回滚 (Harness 判案不代改代码, 默认关闭) ═══
# 开启后: 护栏指标回归持续 ≥ N 天的优化项会在看板置顶“建议回滚”(一键归档仍需人工确认)
OPT_AUTO_ROLLBACK_SUGGEST = get_bool("OPT_AUTO_ROLLBACK_SUGGEST", False)
OPT_ROLLBACK_SUGGEST_DAYS = get_int("OPT_ROLLBACK_SUGGEST_DAYS", 3)
# 已落地优化收益达标 (improved/resolved) 持续 ≥ N 天且样本充足 → 自动升级“已验证”
# (Harness 只推进核算闭环, 不实施不改代码; 升级仅归档成功方向, 无风险)
OPT_AUTO_VERIFY_DAYS = get_int("OPT_AUTO_VERIFY_DAYS", 3)


def key_token(token: str) -> str:
    """Token -> username 映射键"""
    return f"{REDIS_PREFIX}:token:{token}"


def key_user_sessions(username: str) -> str:
    """用户的会话列表 (sorted set: session_id -> timestamp)"""
    return f"{REDIS_PREFIX}:sessions:{username}"


def key_agent_session(agent_id: str, session_id: str) -> str:
    """Per-Agent 会话键 (包含 agentId 前缀)"""
    return f"{REDIS_PREFIX}:sessions:{agent_id}:{session_id}"


def key_agent_chat_history(agent_id: str) -> str:
    """Per-Agent 聊天历史"""
    return f"{REDIS_PREFIX}:chat_history:{agent_id}"


def key_session_meta(session_id: str) -> str:
    """会话元数据 (hash: title, username, created_at, updated_at, msg_count)"""
    return f"{REDIS_PREFIX}:session:{session_id}"


def key_session_msgs(session_id: str) -> str:
    """会话消息列表 (list of JSON)"""
    return f"{REDIS_PREFIX}:messages:{session_id}"


def channel(name: str) -> str:
    """生成 Pub/Sub 频道名"""
    return f"{REDIS_PREFIX}:{name}"


def daily_log_key(date_str: str) -> str:
    """生成每日日志键名"""
    return f"{REDIS_PREFIX}:daily_log:{date_str}"


def reply_channel(msg_id: str) -> str:
    """生成回复频道名"""
    return f"{REDIS_PREFIX}:reply:{msg_id}"
