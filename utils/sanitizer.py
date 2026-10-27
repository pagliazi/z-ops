"""
Z-OPS 运维智能体 — 数据脱敏模块
在发送数据到公共 LLM API 前自动识别和掩码敏感信息，
LLM 返回后自动还原掩码占位符，确保内部数据不外泄。

设计思路:
  1. 正则识别: 匹配 IP、MAC、密码、API Key、域名、邮箱、主机名等
  2. 确定性掩码: 同一敏感值在同一会话中映射到固定占位符（保持上下文一致性）
  3. 反向还原: LLM 回复中的占位符自动还原为真实值
  4. 自定义规则: 支持通过 Redis 动态添加额外脱敏规则和白名单
"""

import re
import json
import hashlib
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("zops")


# ═══ 内置脱敏规则 ═══

@dataclass
class SanitizeRule:
    """脱敏规则定义"""
    name: str           # 规则名称
    pattern: str        # 正则表达式
    tag: str            # 掩码标签前缀, 如 "IP" → [IP_001]
    priority: int = 10  # 优先级 (数字小 = 优先匹配)
    enabled: bool = True

    @property
    def compiled(self):
        return re.compile(self.pattern)


# 内置规则集 (按优先级排列)
BUILTIN_RULES = [
    # ─── 凭据类 (最高优先级) ───
    SanitizeRule(
        name="api_key",
        pattern=r'(?:sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9\-_.]{20,})',
        tag="APIKEY", priority=1
    ),
    SanitizeRule(
        name="password_field",
        pattern=r'(?:password|passwd|pwd|secret|token|credential)\s*[=:]\s*["\']?([^\s"\'&,;}{]{3,})',
        tag="PASSWD", priority=2
    ),
    SanitizeRule(
        name="private_key",
        pattern=r'-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:RSA\s+)?PRIVATE\s+KEY-----',
        tag="PRIVKEY", priority=2
    ),

    # ─── 网络地址类 ───
    SanitizeRule(
        name="ipv4_internal",
        pattern=r'\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b',
        tag="INTIP", priority=5
    ),
    SanitizeRule(
        name="ipv4_public",
        pattern=r'\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b',
        tag="IP", priority=6
    ),
    SanitizeRule(
        name="ipv6",
        pattern=r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b',
        tag="IPV6", priority=6
    ),
    SanitizeRule(
        name="mac_address",
        pattern=r'\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b',
        tag="MAC", priority=7
    ),
    SanitizeRule(
        name="cidr",
        pattern=r'\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b',
        tag="CIDR", priority=7
    ),

    # ─── 域名/URL 类 ───
    SanitizeRule(
        name="internal_url",
        pattern=r'https?://(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})(?::\d+)?(?:/[^\s]*)?',
        tag="INTURL", priority=4
    ),
    SanitizeRule(
        name="internal_domain",
        pattern=r'\b[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?\.(?:local|internal|intranet|corp|lan|example\.com)\b',
        tag="INTDOM", priority=8
    ),

    # ─── 个人信息类 ───
    SanitizeRule(
        name="email",
        pattern=r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
        tag="EMAIL", priority=9
    ),
    SanitizeRule(
        name="phone_cn",
        pattern=r'\b1[3-9]\d{9}\b',
        tag="PHONE", priority=9
    ),

    # ─── 主机/资产标识 ───
    # v4.0 (Phase 4.3): GID 客户编号 (TICKET-47141 / G12345 / g88381) — 计划明确要求的可逆脱敏类别
    SanitizeRule(
        name="gid_number",
        pattern=r'\b(?:GID\s*|G|g)\d{4,6}\b',
        tag="GID", priority=5
    ),
    SanitizeRule(
        name="hostname_pattern",
        pattern=r'\b(?:srv|svr|host|node|esxi|vcsa|pve|vm|dc|ad|dns|web|app|db|nas|san|bak|fw|lb|gw)-[a-zA-Z0-9\-]{2,30}\b',
        tag="HOST", priority=10
    ),
    SanitizeRule(
        name="vcenter_path",
        pattern=r'(?:datacenter|cluster|datastore|resourcepool|folder)/[^\s,;]{3,}',
        tag="VCPATH", priority=10
    ),
]


# ═══ 白名单 — 不需要脱敏的值 ═══

DEFAULT_WHITELIST = {
    # 公共 DNS
    "8.8.8.8", "8.8.4.4", "1.1.1.1", "114.114.114.114",
    # 回环 / 通配
    "127.0.0.1", "0.0.0.0", "255.255.255.255",
    "localhost",
    # 常见模型名中的数字 (避免误匹配)
    "192", "168", "172", "100",
}


# ═══ Sanitizer 引擎 ═══

class DataSanitizer:
    """
    数据脱敏器
    用法:
        s = DataSanitizer()
        masked_text, ctx = s.sanitize(raw_text)
        # masked_text 发送给 LLM
        # LLM 返回后:
        restored = s.restore(llm_reply, ctx)
    """

    def __init__(self,
                 extra_rules: list[SanitizeRule] = None,
                 extra_whitelist: set[str] = None,
                 custom_mappings: dict[str, str] = None):
        # 规则集 (按优先级排序)
        self.rules = sorted(
            BUILTIN_RULES + (extra_rules or []),
            key=lambda r: r.priority
        )
        self.whitelist = DEFAULT_WHITELIST | (extra_whitelist or set())
        # 固定映射: 用户可以预定义 "真实值→替代值"
        self.custom_mappings = custom_mappings or {}

    def sanitize(self, text: str, enabled_rules: list[str] = None) -> tuple[str, "SanitizeContext"]:
        """
        脱敏处理
        返回 (脱敏后文本, 上下文对象用于还原)
        """
        ctx = SanitizeContext()

        if not text:
            return text, ctx

        # 先应用固定映射
        result = text
        for real_val, alias in self.custom_mappings.items():
            if real_val in result:
                result = result.replace(real_val, alias)
                ctx.add_mapping(real_val, alias, "custom")

        # 按优先级逐条规则匹配
        for rule in self.rules:
            if not rule.enabled:
                continue
            if enabled_rules and rule.name not in enabled_rules:
                continue

            compiled = rule.compiled
            for match in compiled.finditer(result):
                original = match.group(0)

                # 白名单检查
                if original in self.whitelist:
                    continue
                # 避免重复脱敏 (已经是占位符了)
                if original.startswith("[") and original.endswith("]"):
                    continue

                # 生成确定性占位符
                placeholder = ctx.get_or_create(original, rule.tag)
                result = result.replace(original, placeholder)

        ctx.masked_text = result
        return result, ctx

    def restore(self, text: str, ctx: "SanitizeContext") -> str:
        """
        还原处理: 将 LLM 回复中的占位符替换回真实值。
        LLM 常把 [TAG_###] 抄成无括号 TAG_###，两种形式都还原；
        长占位符先替换，避免 INTIP_001 被 IP_001 的无括号形式先行污染。
        """
        if not text or not ctx.mapping:
            return text

        result = text
        # 先还原自动生成的占位符 (长的先替换, 避免子串冲突)
        for placeholder, original in sorted(ctx.reverse_mapping.items(),
                                            key=lambda x: -len(x[0])):
            result = result.replace(placeholder, original)
            bare = placeholder.strip("[]")
            if bare != placeholder:
                result = result.replace(bare, original)

        return result

    def get_audit_log(self, ctx: "SanitizeContext") -> list[dict]:
        """
        返回本次脱敏的审计日志 (不含真实值)
        """
        return [
            {"tag": info["tag"], "placeholder": info["placeholder"],
             "hash": hashlib.sha256(original.encode()).hexdigest()[:12]}
            for original, info in ctx.mapping.items()
        ]


class SanitizeContext:
    """
    脱敏上下文: 维护本次会话的 真实值↔占位符 双向映射
    """

    def __init__(self):
        self.mapping: dict[str, dict] = {}       # real_value → {tag, placeholder, index}
        self.reverse_mapping: dict[str, str] = {} # placeholder → real_value
        self.counters: dict[str, int] = {}        # tag → counter
        self.masked_text: str = ""

    def get_or_create(self, original: str, tag: str) -> str:
        """获取或创建占位符 (同一真实值总是映射到同一占位符)"""
        if original in self.mapping:
            return self.mapping[original]["placeholder"]

        # 生成新占位符
        idx = self.counters.get(tag, 0) + 1
        self.counters[tag] = idx
        placeholder = f"[{tag}_{idx:03d}]"

        self.mapping[original] = {
            "tag": tag,
            "placeholder": placeholder,
            "index": idx
        }
        self.reverse_mapping[placeholder] = original
        return placeholder

    def add_mapping(self, original: str, alias: str, tag: str):
        """添加自定义映射"""
        self.mapping[original] = {
            "tag": tag,
            "placeholder": alias,
            "index": 0
        }
        self.reverse_mapping[alias] = original

    @property
    def count(self) -> int:
        return len(self.mapping)

    def summary(self) -> str:
        """脱敏统计摘要"""
        by_tag = {}
        for info in self.mapping.values():
            tag = info["tag"]
            by_tag[tag] = by_tag.get(tag, 0) + 1
        parts = [f"{tag}:{cnt}" for tag, cnt in sorted(by_tag.items())]
        return f"脱敏 {self.count} 项 ({', '.join(parts)})" if parts else "无需脱敏"


# ═══ 便捷函数 ═══

_default_sanitizer: Optional[DataSanitizer] = None


def get_sanitizer(custom_mappings: dict = None,
                  extra_whitelist: set = None) -> DataSanitizer:
    """获取全局脱敏器实例"""
    global _default_sanitizer
    if _default_sanitizer is None or custom_mappings or extra_whitelist:
        _default_sanitizer = DataSanitizer(
            custom_mappings=custom_mappings,
            extra_whitelist=extra_whitelist
        )
    return _default_sanitizer


def sanitize_for_llm(text: str) -> tuple[str, SanitizeContext]:
    """便捷函数: 脱敏"""
    return get_sanitizer().sanitize(text)


def restore_from_llm(text: str, ctx: SanitizeContext) -> str:
    """便捷函数: 还原"""
    return get_sanitizer().restore(text, ctx)
