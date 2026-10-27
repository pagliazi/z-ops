"""本机设备协议 v2：纯验证、保守权限和脱敏边界。"""

import base64
import hashlib
import hmac
import json
import math
import re
from urllib.parse import urlsplit

PROTOCOL = 2
PAIR_TTL = 180
CHALLENGE_TTL = 60
TICKET_TTL = 20
CONNECTION_TTL = 45
TASK_LEASE = 60
FRAME_TTL = 15
RESULT_TTL = 30
MAX_HTTP = 8192
MAX_PARAMS = 65536
MAX_FRAME = 512 * 1024
MAX_MESSAGE = 768 * 1024
SEND_TIMEOUT = 2.0
BINDINGS = (
    "protocol", "actor_id", "device_id", "session_id", "task_id", "command_id",
    "seq", "expiry", "connection_generation", "fencing_token",
)
METHODS = frozenset({
    "file.list", "file.read", "file.find", "file.write_new", "file.move", "file.delete",
    "file.replace", "document.extract",
    "document.write_new", "system.info", "process.list", "service.list", "log.collect", "script.run",
    "browser.goto", "browser.snapshot", "browser.act", "browser.scroll",
    "browser.screenshot", "browser.download", "desktop.inspect",
    "desktop.request_control", "desktop.act", "desktop.screen",
})
DESKTOP_METHODS = frozenset({"desktop.inspect", "desktop.request_control", "desktop.act",
                             "desktop.screen"})
SAFE_METHODS = METHODS - {"script.run", "desktop.inspect", "desktop.request_control", "desktop.act",
                          "desktop.screen"}
# 桌面租约可授予的动作范围；invoke 指 UIA 元素级激活（不落坐标点击）, view 指采集被接管窗口画面。
DESKTOP_SCOPES = frozenset({"click", "type_text", "keys", "scroll", "invoke", "view"})
# 与 companion/desktop.py 的 ELEMENT_TYPES 保持一致，只允许可激活控件。
DESKTOP_ELEMENT_ROLES = frozenset({
    "button", "menuitem", "tabitem", "checkbox", "radiobutton", "hyperlink", "listitem",
    "treeitem",
})
DESKTOP_ELEMENT_LIMIT = 80


def admin_ceiling(desktop=False, scripts=False):
    """管理员上限：桌面/脚本默认排除，只能由服务端配置显式放开。"""
    allowed = set(SAFE_METHODS)
    if desktop:
        allowed |= DESKTOP_METHODS
    if scripts:
        allowed.add("script.run")
    return frozenset(allowed & METHODS)
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "unknown"})
STATUSES = frozenset({"ok", "paused", "rejected", "unknown", "cancelled"})
# 执行方式只接受执行器上报的白名单值; 服务端不推断, 缺失即不展示。
EXECUTION_MODES = frozenset({"background", "unfocused_window", "desktop", "foreground"})


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def loads(raw):
    return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非法数值")))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def identifier(value, label="标识"):
    if not isinstance(value, str) or not re.fullmatch(r"[\w.@+-]{1,128}", value):
        raise ValueError(f"无效{label}")
    return value


def text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("文本格式错误")
    return value.strip()


def b64(value, size):
    if not isinstance(value, str) or len(value) > ((size + 2) // 3) * 4:
        raise ValueError("凭据格式错误")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("凭据格式错误") from exc
    if len(raw) != size:
        raise ValueError("凭据长度错误")
    return raw


def result(status, data=None, error=""):
    return {"status": status, "data": data or {}, "error": error}


def signed(secret, payload):
    raw = dumps(payload)
    return dumps({"payload": payload, "mac": hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()})


def verified(secret, raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_MESSAGE + 8192:
        raise ValueError("总线消息过大")
    obj = loads(raw)
    if not isinstance(obj, dict) or set(obj) != {"payload", "mac"}:
        raise ValueError("总线消息格式错误")
    expected = hmac.new(secret, dumps(obj["payload"]).encode(), hashlib.sha256).hexdigest()
    if not isinstance(obj["mac"], str) or not hmac.compare_digest(expected, obj["mac"]):
        raise ValueError("总线鉴权失败")
    return obj["payload"]


def binding_matches(message, envelope):
    if not isinstance(message, dict) or not isinstance(envelope, dict):
        return False
    for key in BINDINGS:
        actual, expected = message.get(key), envelope.get(key)
        if key == "expiry":
            if type(actual) not in (int, float) or not math.isfinite(actual) or actual != expected:
                return False
        elif type(actual) is not type(expected) or actual != expected:
            return False
    return True


def allowed(method, params, capabilities, ceiling=SAFE_METHODS):
    """管理员上限来自服务端注入，绝不读取请求中的 approval。脚本不接收源码/Shell。"""
    if not isinstance(method, str) or method not in METHODS or method not in capabilities or method not in ceiling:
        return False
    if not isinstance(params, dict) or len(dumps(params).encode()) > MAX_PARAMS:
        return False
    forbidden = {"shell", "command", "cmd", "argv", "executable", "code", "javascript", "eval", "approval", "approved", "overwrite"}

    def unsafe(value):
        if isinstance(value, dict):
            return any(str(k).lower() in forbidden or unsafe(v) for k, v in value.items())
        if isinstance(value, list):
            return any(unsafe(v) for v in value)
        return False

    if unsafe(params):
        return False
    if method == "script.run":
        # 仅预部署脚本标识，默认上限仍禁止；本机执行器必须独立维护白名单与哈希。
        if not set(params) <= {"name", "args"}:
            return False
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(params.get("name", ""))):
            return False
        args = params.get("args", {})
        return isinstance(args, dict) and len(args) <= 16 and all(
            isinstance(k, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", k)
            and (isinstance(v, (str, int, bool)) and not isinstance(v, float)) for k, v in args.items())
    if method == "log.collect":
        # 只读授权目录内的日志尾部; 本机执行器再做路径解析与脱敏。
        if not set(params) <= {"path", "max_bytes", "tail"}:
            return False
        path = params.get("path")
        if not isinstance(path, str) or not 0 < len(path) <= 1024 or any(ord(c) < 32 for c in path):
            return False
        size = params.get("max_bytes", 65536)
        if type(size) is not int or not 1024 <= size <= 262144:
            return False
        return type(params.get("tail", True)) is bool
    if method == "service.list":
        # 只允许空参数, 服务名/状态由本机执行器读取。
        return params == {}
    if method == "desktop.inspect":
        # window_id 可选: 指定后附带该窗口的 UIA 可激活元素列表（只读, 不含控件文本）。
        return set(params) <= {"limit", "window_id"} and (
            "limit" not in params or type(params["limit"]) is int and 1 <= params["limit"] <= 50
        ) and ("window_id" not in params
               or type(params["window_id"]) is int and params["window_id"] > 0)
    if method == "desktop.request_control":
        scopes = params.get("scopes")
        return (set(params) == {"window_id", "scopes", "reason"}
                and type(params.get("window_id")) is int and params["window_id"] > 0
                and isinstance(scopes, list) and 1 <= len(scopes) <= len(DESKTOP_SCOPES)
                and len(set(scopes)) == len(scopes)
                and all(isinstance(item, str) and item in DESKTOP_SCOPES for item in scopes)
                and isinstance(params.get("reason"), str) and 0 < len(params["reason"]) <= 200)
    if method == "desktop.screen":
        # 真实桌面画面: 只接受空参数, 采集条件（view 范围/锁屏/密码框）由本机执行器判定。
        return params == {}
    if method == "desktop.act":
        action = params.get("action")
        allowed_params = {"action", "x", "y", "text", "keys", "amount",
                          "element", "role", "name"}
        if action not in DESKTOP_SCOPES or not set(params) <= allowed_params:
            return False
        if action in {"click", "scroll"}:
            if not set(params) <= {"action", "x", "y"} | ({"amount"} if action == "scroll" else set()):
                return False
            return all(type(params.get(k)) is int for k in ("x", "y")) \
                and (action == "click" or type(params.get("amount")) is int)
        if action == "type_text":
            return set(params) == {"action", "text"} and isinstance(params["text"], str) \
                and 0 < len(params["text"]) <= 512
        if action == "invoke":
            # 元素级动作: 下标 + 角色 + 名称三者必填, 本机执行器再做一次精确匹配。
            if set(params) != {"action", "element", "role", "name"}:
                return False
            element, role, name = params["element"], params["role"], params["name"]
            return (type(element) is int and 0 <= element < DESKTOP_ELEMENT_LIMIT
                    and isinstance(role, str) and role in DESKTOP_ELEMENT_ROLES
                    and isinstance(name, str) and 0 < len(name) <= 120
                    and not any(ord(c) < 32 for c in name))
        return set(params) == {"action", "keys"} and isinstance(params["keys"], list) \
            and 1 <= len(params["keys"]) <= 4
    if method == "file.find":
        if not set(params) <= {"path", "pattern", "max_results"}:
            return False
        path, pattern = params.get("path"), params.get("pattern")
        if not isinstance(path, str) or not 0 < len(path) <= 1024 or any(ord(c) < 32 for c in path):
            return False
        if not isinstance(pattern, str) or not 1 <= len(pattern) <= 64 \
                or "/" in pattern or "\\" in pattern or ".." in pattern:
            return False
        count = params.get("max_results", 50)
        return type(count) is int and 1 <= count <= 200
    if method in {"file.move", "file.delete", "file.replace"}:
        # 破坏性操作: 参数严格, 路径由本机执行器再用 manage 授权根复核。
        paths = {"file.move": ("source", "target"),
                 "file.delete": ("path",),
                 "file.replace": ("path",)}[method]
        required = set(paths) | ({"content"} if method == "file.replace" else set())
        if set(params) != required:
            return False
        for key in paths:
            value = params.get(key)
            if not isinstance(value, str) or not 0 < len(value) <= 1024 or any(ord(c) < 32 for c in value):
                return False
        if method == "file.move":
            return params["source"] != params["target"]
        if method == "file.replace":
            return isinstance(params["content"], str) and len(params["content"]) <= 48 * 1024
        return True
    if method == "browser.goto":
        try:
            u = urlsplit(params.get("url", ""))
            return u.scheme in ("http", "https") and bool(u.hostname) and not u.username and not u.password
        except (TypeError, ValueError):
            return False
    if method == "browser.act":
        # 与 Companion 一致：只允许 click/fill/select，ref 为快照短期引用。
        if not set(params) <= {"ref", "verb", "value"}:
            return False
        if params.get("verb") not in {"click", "fill", "select"}:
            return False
        if not re.fullmatch(r"[0-9a-f]{24}", str(params.get("ref", ""))):
            return False
        return params.get("verb") == "click" or isinstance(params.get("value"), str)
    if method == "browser.scroll":
        return (params.get("direction") in {"up", "down", "left", "right"}
                and type(params.get("amount")) is int and 1 <= params["amount"] <= 2000)
    if method == "browser.download":
        # 下载只写入本机授权目录下的新文件; 文件名由本机再次净化, 不接受路径。
        if not set(params) <= {"ref", "name"}:
            return False
        if not re.fullmatch(r"[0-9a-f]{24}", str(params.get("ref", ""))):
            return False
        name = params.get("name")
        return name is None or (isinstance(name, str) and 0 < len(name) <= 160
                                and "/" not in name and "\\" not in name)
    return True


def grants(value):
    """严格校验 Companion 上报的授权摘要; 只保存展示用白名单元数据。"""
    base = {"roots", "origins", "scripts", "native_origins"}
    # apps 为新增字段: 2.0.1 及更早的 Companion 不带 apps, 按未授权任何应用处理。
    if type(value) is not dict or set(value) not in (base, base | {"apps"}):
        raise ValueError("授权摘要格式错误")
    roots = value["roots"]
    if type(roots) is not list or len(roots) > 32:
        raise ValueError("授权目录摘要格式错误")
    out_roots = []
    for item in roots:
        if type(item) is not dict or set(item) != {"name", "read", "write_new", "manage"}:
            raise ValueError("授权目录摘要格式错误")
        if type(item["read"]) is not bool or type(item["write_new"]) is not bool \
                or type(item["manage"]) is not bool:
            raise ValueError("授权目录摘要格式错误")
        out_roots.append({"name": text(item["name"], 80), "read": item["read"],
                          "write_new": item["write_new"], "manage": item["manage"]})

    def strings(items, limit, count):
        if type(items) is not list or len(items) > count:
            raise ValueError("授权摘要格式错误")
        return [text(x, limit) for x in items]

    scripts = strings(value["scripts"], 64, 32)
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in scripts):
        raise ValueError("脚本摘要格式错误")
    apps = strings(value.get("apps", []), 64, 16)
    if any(not re.fullmatch(r"[a-z0-9._-]{1,64}", app) for app in apps):
        raise ValueError("应用摘要格式错误")
    return {"roots": out_roots, "origins": strings(value["origins"], 200, 64),
            "scripts": scripts, "native_origins": strings(value["native_origins"], 200, 16),
            "apps": apps}


def timeout_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 120:
        raise ValueError("超时必须在 0 到 120 秒之间")
    return float(value)


def execution_mode(value):
    """只接受白名单执行方式; 其它值一律丢弃, 避免把任意字符串透传到界面。"""
    return value if isinstance(value, str) and value in EXECUTION_MODES else ""


def pause_reason(value):
    """只接受执行端的静态暂停码; 页面内容、参数和秘密不得借此进入界面或审计。"""
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) else ""


def frame_from(data):
    jpeg = data.get("jpeg")
    if jpeg is None:
        return None
    if not isinstance(jpeg, str) or len(jpeg) > ((MAX_FRAME + 2) // 3) * 4:
        raise ValueError("截图过大")
    try:
        raw = base64.b64decode(jpeg, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("截图格式错误") from exc
    if len(raw) > MAX_FRAME or not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
        raise ValueError("截图格式错误")
    # 读取 JPEG SOF 尺寸，不解压图像，避免解压炸弹。
    pos, dimensions = 2, None
    while pos + 4 <= len(raw):
        if raw[pos] != 255:
            break
        marker = raw[pos + 1]
        if marker == 255:
            pos += 1
            continue
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        size = int.from_bytes(raw[pos + 2:pos + 4], "big")
        if size < 2 or pos + 2 + size > len(raw):
            break
        if marker in {0xC0, 0xC1, 0xC2} and size >= 8:
            height = int.from_bytes(raw[pos + 5:pos + 7], "big")
            width = int.from_bytes(raw[pos + 7:pos + 9], "big")
            dimensions = (width, height)
            break
        pos += 2 + size
    if not dimensions or not all(0 < n <= 8192 for n in dimensions) or dimensions[0] * dimensions[1] > 16_777_216:
        raise ValueError("截图尺寸错误")
    return {"jpeg": jpeg, "width": dimensions[0], "height": dimensions[1]}


def transport_ok(scope, headers, origins=(), require_origin=False):
    """只相信 ASGI scheme；代理须在受信任边界设置，不直接信任转发头。"""
    client = scope.get("client") or ("", 0)
    local = client[0] == "127.0.0.1"
    scheme = scope.get("scheme", "")
    if scheme not in ("https", "wss") and not local:
        return False
    origin = headers.get("origin")
    if not origin:
        return not require_origin or local
    try:
        url = urlsplit(origin)
        if url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
            return False
        if local and url.scheme == "http" and url.hostname == "127.0.0.1":
            return True
        return url.scheme == "https" and origin.rstrip("/") in {o.rstrip("/") for o in origins}
    except ValueError:
        return False
