"""Z-OPS P3 — MCP 服务注册表 (已批准 server 白名单 + stdio 命令登记)

SPEC P3「工具发现和标准 MCP」:
  - 仅允许已批准 server; stdio 命令/参数必须登记且受限, 不提供任意进程启动能力;
  - cache 按主体/委托、服务配置版本、授权版本隔离 (见 mcp_official.MCPCacheKey);
  - Schema 不兼容或风险上升时停用旧注册并重新审核, 不自动继承权限。

配置 (zops.env):
  MCP_APPROVED_SERVERS=[{"id":"gcoc","transport":"rest|stdio|http",
                          "url":"... 或 command":"/path/to/bin","args":[...],
                          "allowed_tools":["*"]}, ...]   # JSON 数组

未登记的 server / stdio 命令一律拒绝 (fail-closed)。REST 适配 (现有) 也受白名单约束。
"""

import json
import logging
import shlex
from typing import List, Optional

import config

logger = logging.getLogger("zops")


def approved_servers() -> List[dict]:
    raw = getattr(config, "MCP_APPROVED_SERVERS", "") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"[mcp_registry] MCP_APPROVED_SERVERS 解析失败 (视为空, fail-closed): {e}")
        return []


def get_server(server_id: str) -> Optional[dict]:
    for s in approved_servers():
        if s.get("id") == server_id:
            return s
    return None


def is_approved(server_id: str) -> bool:
    return get_server(server_id) is not None


def tool_allowed(server_id: str, tool_name: str) -> bool:
    s = get_server(server_id) or {}
    allowed = s.get("allowed_tools") or ["*"]
    return "*" in allowed or tool_name in allowed


def validate_stdio(server_id: str) -> tuple:
    """stdio server 校验: 命令必须显式登记 (绝对路径), 参数只允许登记值/简单标量。

    不提供任意进程启动能力 — 未登记命令/相对路径/危险 shell 元字符一律拒绝。
    Returns: (ok, detail)
    """
    s = get_server(server_id) or {}
    if not s:
        return False, f"MCP server '{server_id}' 未在白名单登记"
    if s.get("transport") != "stdio":
        return True, ""
    cmd = str(s.get("command") or "")
    if not cmd:
        return False, "stdio server 缺少 command"
    if not cmd.startswith("/"):
        return False, f"stdio command 必须为登记的绝对路径: {cmd}"
    _shell_meta = ";|&$`><\n"
    for ch in _shell_meta:
        if ch in cmd:
            return False, f"stdio command 含 shell 元字符: {cmd!r}"
    args = s.get("args") or []
    for a in args:
        if not isinstance(a, (str, int, float, bool)):
            return False, f"stdio args 仅允许标量: {a!r}"
        if isinstance(a, str) and any(ch in a for ch in ";|&$`><\n"):
            return False, f"stdio args 含 shell 元字符: {a!r}"
    return True, ""


def schema_fingerprint(s: dict) -> str:
    """服务配置版本指纹 (cache 隔离键的一部分; 配置变化 → 缓存自动失效)"""
    import hashlib
    canon = json.dumps({k: s.get(k) for k in ("id", "transport", "url",
                                              "command", "args", "allowed_tools")},
                       sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()[:12]
