"""Chrome 登录态一次性交接：仅本机、限授权 origin、短时、单次、不记录值。

扩展在用户明确点击后读取当前站点 Cookie，经 Native Messaging 交给本进程；
本模块只落盘到 0700 状态目录下的 0600 文件。WSS Companion 在任务浏览器创建时
原子领取并立即删除，Cookie 值不进入服务端、模型、日志或审计。
"""
import json
import os
import secrets
import time
from urllib.parse import urlsplit

from .security import Denied, fields, no_links, origin, private_dir, require

TTL = 300
MAX = 128 * 1024


def _path(config):
    return config.state / "login-handoff.json"


def _cookies(value, origin_value):
    """校验并规范化 Cookie；值不打印、不进入异常文本。"""
    require(type(value) is list and 0 < len(value) <= 200, "invalid_cookie_list")
    host = (urlsplit(origin_value).hostname or "").lower()
    out = []
    for item in value:
        fields(item, ("name", "value"),
               ("domain", "path", "secure", "httpOnly", "sameSite", "expirationDate"))
        name, secret = item["name"], item["value"]
        require(isinstance(name, str) and 0 < len(name) <= 256
                and not any(ord(c) < 32 for c in name), "invalid_cookie")
        require(isinstance(secret, str) and len(secret) <= 4096 and "\x00" not in secret, "invalid_cookie")
        entry = {"name": name, "value": secret}
        domain = item.get("domain")
        if domain is not None:
            require(isinstance(domain, str) and 0 < len(domain) <= 255
                    and not any(ord(c) < 33 for c in domain), "invalid_cookie_domain")
            bare = domain.lstrip(".").lower()
            # 只接受会发往该站点的 Cookie；拒绝跨站域。
            require(bare == host or host.endswith("." + bare), "cookie_domain_mismatch")
            entry["domain"] = domain
        path = item.get("path")
        if path is not None:
            require(isinstance(path, str) and 0 < len(path) <= 1024 and path.startswith("/")
                    and not any(ord(c) < 32 for c in path), "invalid_cookie_path")
            entry["path"] = path
        for key in ("secure", "httpOnly"):
            if item.get(key) is not None:
                require(type(item[key]) is bool, "invalid_cookie_flag")
                entry[key] = item[key]
        site = item.get("sameSite")
        if site is not None:
            require(site in ("no_restriction", "lax", "strict", "unspecified", "None", "Lax", "Strict"),
                    "invalid_cookie_samesite")
            entry["sameSite"] = site
        expires = item.get("expirationDate")
        if expires is not None:
            require(type(expires) in (int, float) and expires > 0, "invalid_cookie_expiry")
            entry["expirationDate"] = float(expires)
        out.append(entry)
    return out


def _decode(raw, config, now):
    data = json.loads(raw)
    fields(data, ("origin", "cookies", "expiry"))
    origin_value = origin(data["origin"])
    require(origin_value in config.origins, "origin_not_authorized")
    require(type(data["expiry"]) in (int, float) and now < data["expiry"] <= now + TTL + 30,
            "handoff_expired")
    return {"origin": origin_value, "cookies": _cookies(data["cookies"], origin_value),
            "expiry": float(data["expiry"])}


def store(config, origin_value, cookies, expiry, clock=time.time):
    """本机写入交接文件；只接受本机已授权的 canonical origin。"""
    now = clock()
    origin_value = origin(origin_value)
    require(origin_value in config.origins, "origin_not_authorized")
    require(type(expiry) in (int, float) and now < expiry <= now + TTL + 30, "invalid_handoff_expiry")
    payload = {"origin": origin_value, "cookies": _cookies(cookies, origin_value), "expiry": float(expiry)}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    require(len(raw) <= MAX, "handoff_too_large")
    private_dir(config.state)
    path = _path(config)
    no_links(path, missing_leaf=True)
    tmp = path.with_name(".handoff-" + secrets.token_hex(8))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return len(payload["cookies"])


def claim(config, clock=time.time):
    """原子领取并删除；过期/非法/权限不符的文件同样删除，避免跨任务复用。"""
    path = _path(config)
    if not path.exists():
        return None
    try:
        no_links(path)
        info = path.stat()
        require(info.st_size <= MAX, "handoff_too_large")
        if os.name != "nt":
            require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "handoff_not_private")
    except (Denied, OSError):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    claimed = path.with_name(".claimed-" + secrets.token_hex(8))
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return None
    try:
        data = _decode(claimed.read_bytes(), config, clock())
    except (ValueError, TypeError, UnicodeError, OSError, Denied):
        return None
    finally:
        try:
            claimed.unlink()
        except FileNotFoundError:
            pass
    if data["expiry"] <= clock():
        return None
    return data
