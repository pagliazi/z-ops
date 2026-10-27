"""失败关闭的公共边界；异常仅携带固定错误码。"""
import json
import math
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

MAX_INPUT = 256 * 1024
MAX_OUTPUT = 768 * 1024
MAX_TEXT = 128 * 1024
MAX_FILE = 8 * 1024 * 1024


class Denied(Exception):
    pass


class Paused(Denied):
    """本机需人工确认; 只携带静态暂停码, 不回显页面内容、参数或凭据。"""


def pause_code(error):
    """把 Paused 的静态码交给服务端展示; 非静态码一律退回通用原因。"""
    code = error.args[0] if error.args and isinstance(error.args[0], str) else ""
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "local_authorization_required"


def require(condition, code="invalid_parameters"):
    if not condition:
        raise Denied(code)


def text(value, limit=4096):
    require(isinstance(value, str) and len(value) <= limit and "\x00" not in value)
    return value


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[\w.@+:-]{1,160}", value))
    return value


def integer(value, lo=0, hi=2**63 - 1):
    require(type(value) is int and lo <= value <= hi)
    return value


def fields(params, required=(), optional=()):
    require(type(params) is dict and set(required) <= params.keys()
            and params.keys() <= set(required) | set(optional))


def encode(value, limit=MAX_OUTPUT):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    require(len(raw) <= limit, "output_too_large")
    return raw


def decode(raw, limit=MAX_INPUT):
    require(isinstance(raw, (bytes, str)) and len(raw) <= limit, "input_too_large")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    def invalid(_):
        raise Denied("invalid_json")
    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, RecursionError, UnicodeError):
        raise Denied("invalid_json") from None
    require(type(result) is dict, "invalid_json")
    return result


def origin(url):
    text(url)
    require(not any(ord(c) < 33 for c in url) and "\\" not in url, "invalid_url")
    try:
        p = urlsplit(url)
        require(p.scheme in ("http", "https") and p.hostname and not p.username
                and not p.password, "invalid_url")
        port = p.port or (443 if p.scheme == "https" else 80)
        host = p.hostname.encode("idna").decode().lower()
        if ":" in host:
            host = f"[{host}]"
        return f"{p.scheme}://{host}:{port}"
    except ValueError:
        raise Denied("invalid_url") from None


def server_url(url):
    origin(url)
    p = urlsplit(url)
    require(p.scheme == "https" and p.path in ("", "/") and not p.query
            and not p.fragment, "https_server_origin_required")
    return url.rstrip("/")


def no_links(path, missing_leaf=False):
    """拒绝符号链接、Windows junction/reparse、相对路径和设备名。"""
    path = Path(text(str(path)))
    require(path.is_absolute() and ".." not in path.parts, "absolute_path_required")
    if os.name == "nt":
        require(not str(path).startswith("\\\\") and ":" not in str(path)[2:], "unsafe_path")
    for part in [*reversed(path.parents), path]:
        try:
            s = part.lstat()
        except FileNotFoundError:
            require(missing_leaf and part == path, "path_missing")
            break
        require(not stat.S_ISLNK(s.st_mode)
                and not getattr(s, "st_file_attributes", 0) & 0x400, "link_rejected")
    return path


def private_dir(path):
    path = Path(path)
    if not path.exists():
        no_links(path.parent)
        path.mkdir(mode=0o700)
    no_links(path)
    s = path.stat()
    require(stat.S_ISDIR(s.st_mode), "unsafe_state_directory")
    if os.name != "nt":
        require(s.st_uid == os.getuid() and not s.st_mode & 0o077, "state_directory_not_private")
    return path
