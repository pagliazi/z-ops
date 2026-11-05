"""真实桌面接管：默认关闭；本机验收 + 原生批准 + 60 秒租约，三层均不可由远端代答。"""
import asyncio
import base64
import ctypes
import io
import os
import re
import secrets
import sys
import threading
import time

from . import __version__
from . import ax as macos_ax
from .security import Denied, Paused, decode, encode, fields, integer, no_links, private_dir, require, text

LEASE_SECONDS = 60
# 批准对话框的等待上限：无人操作自动拒绝（不默认同意）。必须短于命令有效期
# （引擎按 min(60, expiry) 中止），否则命令已按过期失败、对话框还挂着，用户之后
# 再点“允许”就成了批准一个早已作废的请求。
APPROVAL_SECONDS = 30
# 本机写验收记录前的确认不受远端命令约束，给足阅读时间，同样到期不写入。
ACCEPTANCE_SECONDS = 90
APPROVAL_TIMEOUT = "desktop_approval_timeout"
# view 表示用户显式同意在接管期间采集被接管窗口的画面（Spec 第 67 条）。
ACTION_SCOPES = ("click", "type_text", "keys", "scroll", "invoke", "view")
FRAME_BYTES = 384 * 1024
# UIA 元素级动作只开放“可激活”的控件类型；文本/文档/输入框一律不进列表。
ELEMENT_TYPES = ("button", "menuitem", "tabitem", "checkbox", "radiobutton",
                 "hyperlink", "listitem", "treeitem")
ELEMENT_LIMIT = 80
SENSITIVE_PLACEHOLDER = "[敏感控件]"
SENSITIVE_NAME = re.compile(r"password|passwd|pwd|otp|one.?time|secret|token|credential|"
                            r"验证码|密码|口令", re.I)
SECURITY_PROCESSES = {"logonui.exe", "consent.exe", "winlogon.exe", "lockapp.exe",
                      "credentialuibroker.exe", "useraccountcontrol.exe"}
# Linux 上的认证/锁屏程序：命中即视为安全界面，永不作为接管目标。
LINUX_SECURITY_PROCESSES = {"gcr-prompter", "polkit-gnome-authentication-agent-1",
                            "polkit-agent-helper-1", "gnome-shell-lock", "xscreensaver",
                            "light-locker", "i3lock", "swaylock", "systemd-ask-password"}
# macOS 上的认证/锁屏进程（进程名按 psutil 的短名，不含 .app 后缀）：同样永不作为目标。
MACOS_SECURITY_PROCESSES = {"loginwindow", "securityagent", "authorizationhost",
                            "coreauthd", "coreautha", "keychainaccessd",
                            "opendirectoryd", "screencaptureui"}
SECURITY_PROCESSES_ALL = SECURITY_PROCESSES | LINUX_SECURITY_PROCESSES | MACOS_SECURITY_PROCESSES
SHELL_CLASSES = {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd"}
ACCEPTANCE_NAME = "desktop-acceptance.json"
# 三个平台的验收项不同：Windows 依赖 UIA 与安全桌面查询，Linux X11 依赖 XTest 与屏保查询，
# macOS 依赖辅助功能授权与窗口采集；记录不能互相冒充。
ACCEPTANCE_CHECKS = ("uia", "native_dialog", "input_sentinel", "secure_desktop_query",
                     "hotkey", "tray", "focus_query")
ACCEPTANCE_CHECKS_X11 = ("x11", "native_dialog", "input_sentinel", "lock_query",
                         "focus_query", "hotkey", "injection")
ACCEPTANCE_CHECKS_MACOS = ("ax", "native_dialog", "input_sentinel", "lock_query",
                           "focus_query", "hotkey", "capture")
# 桌面能力按平台声明：Linux 不做控件树也不截窗口，因此不包含 desktop.screen。
DESKTOP_CAPABILITIES = ("desktop.inspect", "desktop.request_control", "desktop.act",
                        "desktop.screen")
X11_CAPABILITIES = DESKTOP_CAPABILITIES[:3]
CAPABILITIES_BY_PLATFORM = {"nt": DESKTOP_CAPABILITIES, "posix": X11_CAPABILITIES,
                            "darwin": DESKTOP_CAPABILITIES}
# Linux 会话不接受 Wayland 与 XWayland：XTest 注入对原生 Wayland 客户端无效。
WAYLAND_MARKERS = ("wayland",)
# 兜底原因码：portal 探测层本身出错时用它；能问到事实时一律用 `portal.block_reason()`
# 给出的精确码（“portal 没跑”/“没有 EIS 输入通道”/“没有窗口作用域”/“没有 D-Bus 地址”
# 与“还没验收”必须可区分）。注意：无论哪个码，Wayland 下都不声明任何 `desktop.*`——
# portal 与 EIS 协议里都没有“窗口”对象，只有整屏授权与屏幕 region（见 eis.py）。
WAYLAND_REASON = "wayland_portal_not_implemented"


def _wayland_reason():
    """问一遍 portal，把 Wayland 上做不到的具体原因报出来。

    这里不能报错也不能默认“可用”：探测层任何异常都回到兜底码，
    桌面能力仍然是禁用，只是原因描述精度下降。
    """
    try:
        from .portal import block_reason
        reason = block_reason()
    except Exception:
        return WAYLAND_REASON
    return reason if re.fullmatch(r"wayland_[a-z0-9_]{3,60}", str(reason)) else WAYLAND_REASON


def wayland_session(environ=None):
    """Wayland（含 XWayland）判定：XTest 注入对原生 Wayland 客户端无效。

    只看 XDG_SESSION_TYPE 不够：部分会话不设它，只留 WAYLAND_DISPLAY。这时 DISPLAY
    指向 XWayland，注入对用户正在用的原生窗口无效，还会让焦点核对看起来“成功”。
    """
    env = os.environ if environ is None else environ
    session = str(env.get("XDG_SESSION_TYPE") or "").lower()
    return session in WAYLAND_MARKERS or bool(env.get("WAYLAND_DISPLAY"))
HOTKEY_ID = 0x5A43
HOTKEY_TEXT = "Ctrl+Alt+Pause"
# Spec 第 34 条首批验收应用；UIA 只读探测按进程名匹配，不要求全部同时打开。
STANDARD_APPS = ("notepad.exe", "calc.exe", "calculatorapp.exe", "calculator.exe",
                 "explorer.exe")


def _reasons(system):
    if system == "nt":
        code = "desktop_requires_local_windows_acceptance"
        login = "independent_headed_login_not_implemented"
    elif system == "darwin":
        code = "macos_ax_desktop_not_accepted"
        login = "macos_headed_login_not_implemented"
    else:
        code = "linux_x11_wayland_desktop_not_accepted"
        login = "linux_headed_login_not_implemented"
    return {
        "desktop.inspect": code,
        "desktop.request_control": code,
        "desktop.act": code,
        "desktop.screen": code,
        "headed_login": login,
        "attach_user_browser": "user_profile_attachment_not_supported",
    }


class Lease:
    """批准绑定的全部条件；没有续期方法，到期只能重新本机批准。"""

    __slots__ = ("approval_id", "device_id", "task_id", "session_id", "window",
                 "scopes", "issued_at", "expires_at", "baseline")

    def __init__(self, device_id, task_id, session_id, window, scopes, issued_at, baseline):
        self.approval_id = secrets.token_hex(8)
        self.device_id = device_id
        self.task_id = task_id
        self.session_id = session_id
        self.window = dict(window)
        self.scopes = tuple(scopes)
        self.issued_at = issued_at
        self.expires_at = issued_at + LEASE_SECONDS
        self.baseline = baseline

    def check(self, now, task, action):
        if now >= self.expires_at:
            return "lease_expired"
        if task.get("device_id") != self.device_id:
            return "device_changed"
        if task.get("task_id") != self.task_id:
            return "task_changed"
        if task.get("session_id") != self.session_id:
            return "session_changed"
        if action not in self.scopes:
            return "action_not_approved"
        return ""

    def public(self, now=None):
        current = time.monotonic() if now is None else now
        return {"approval_id": self.approval_id, "scopes": list(self.scopes),
                "expires_in": max(0, int(round(self.expires_at - current))),
                "window": {"hwnd": self.window.get("hwnd"),
                           "title": str(self.window.get("title") or "")[:120],
                           "process": str(self.window.get("process") or "")[:80]}}


def platform_key(platform=None):
    """把 os.name、sys.platform 和测试里的写法统一成 'nt' / 'posix' / 'darwin'。

    缺省读 sys.platform 而不是 os.name：macOS 上 os.name 同样是 'posix'，
    按它分支会把 Mac 当成 Linux X11 去驱动，能力表与验收记录都会张冠李戴。
    """
    value = str(platform or sys.platform).lower()
    if value in ("nt", "windows", "win32"):
        return "nt"
    if value in ("darwin", "macos"):
        return "darwin"
    return "posix"


# 平台相关的“做不到”原因：executor 在桌面未启用时用它如实标注能力缺口。
UNAVAILABLE = _reasons(platform_key())


def acceptance_platform(platform=None):
    """验收记录的平台名与检查项；Windows 走 UIA，Linux 走 X11，macOS 走 AX。"""
    platform = platform_key(platform)
    if platform == "nt":
        return "windows", ACCEPTANCE_CHECKS
    if platform == "darwin":
        return "macos", ACCEPTANCE_CHECKS_MACOS
    require(sys.platform.startswith("linux"), "desktop_unsupported_platform")
    return "linux", ACCEPTANCE_CHECKS_X11


def validate_acceptance(data, config, platform=None):
    """验收记录只由本机 CLI 写入；设备、版本、检查项任一不符即视为未验收。"""
    name, expected = acceptance_platform(platform)
    fields(data, ("version", "device_id", "platform", "accepted_version", "accepted_at", "checks"),
           ("uia_apps",))
    require(data["version"] == 1, "invalid_acceptance")
    require(data["platform"] == name, "invalid_acceptance")
    require(data["device_id"] == config.device_id, "acceptance_device_mismatch")
    require(data["accepted_version"] == __version__, "acceptance_stale")
    require(type(data["accepted_at"]) is int and data["accepted_at"] > 0, "invalid_acceptance")
    checks = data["checks"]
    require(type(checks) is dict and set(checks) == set(expected), "invalid_acceptance")
    require(all(value is True for value in checks.values()), "acceptance_incomplete")
    if "uia_apps" in data:
        # UIA 实测记录: 只含进程名与可读控件数, 不含控件名称或内容。
        apps = data["uia_apps"]
        require(type(apps) is dict and 1 <= len(apps) <= 16, "invalid_acceptance")
        for name, item in apps.items():
            require(type(name) is str and 0 < len(name) <= 80, "invalid_acceptance")
            require(type(item) is dict and item.get("readable") is True
                    and type(item.get("elements")) is int
                    and 0 <= item["elements"] <= 500, "acceptance_incomplete")
    return data


def acceptance_path(config):
    require(config.path is not None, "configuration_required")
    private_dir(config.state)
    return config.state / ACCEPTANCE_NAME


def load_acceptance(config):
    path = acceptance_path(config)
    if not path.exists():
        return None
    with path.open("rb") as stream:
        return decode(stream.read(65537))


def write_acceptance(config, checks, uia_apps=None, platform=None):
    """仅 --accept-desktop 在全部检查通过且本机 ALLOW 后调用。"""
    name, expected = acceptance_platform(platform)
    require(set(checks) == set(expected) and all(v is True for v in checks.values()),
            "acceptance_incomplete")
    require(bool(config.device_id), "pairing_required")
    body = {"version": 1, "device_id": config.device_id, "platform": name,
            "accepted_version": __version__, "accepted_at": int(time.time()), "checks": dict(checks)}
    if uia_apps:
        require(type(uia_apps) is dict and 1 <= len(uia_apps) <= 16, "invalid_acceptance")
        body["uia_apps"] = {str(name)[:80]: {"readable": True, "elements": int(item["elements"])}
                            for name, item in uia_apps.items()}
    path = acceptance_path(config)
    no_links(path, missing_leaf=True)
    tmp = path.with_name(".acceptance-" + secrets.token_hex(8))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encode(body, 16384))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return body


class Desktop:
    capabilities = []

    def __init__(self, config, driver=None, acceptance=None, platform=None, clock=time.monotonic):
        self.config = config
        self.driver = driver
        self.acceptance = acceptance
        self.platform = platform_key(platform)
        self.clock = clock
        self.enabled = False
        self.reason = ""
        self.unavailable = _reasons(self.platform)
        self.lease = None
        self.suspended = "desktop_not_enabled"
        self.revoked = False

    async def prepare(self):
        self.unavailable = _reasons(self.platform)
        # 只有未适配的平台（Wayland 之外的其它 posix 会话）直接保持禁用；
        # 不报验收缺失，也不抛异常影响其它能力。nt/darwin 由各自驱动的 prepare 定实际预检。
        if self.platform not in ("nt", "darwin") and not sys.platform.startswith("linux"):
            self.reason = self.unavailable["desktop.inspect"]
            return
        if self.platform == "posix" and wayland_session():
            # Wayland 下 XTest 对原生客户端无效，只能走 portal；问清原因后仍全部禁用：
            # 批准无法绑定到具体窗口（Spec 第 50、51 条），所以不开放任何 desktop.*。
            reason = await asyncio.to_thread(_wayland_reason)
            self.reason = reason
            self.unavailable = {key: value for key, value in _reasons(self.platform).items()
                                if key not in DESKTOP_CAPABILITIES}
            self.unavailable.update({name: reason for name in DESKTOP_CAPABILITIES})
            return
        try:
            data = self.acceptance if self.acceptance is not None else load_acceptance(self.config)
            validate_acceptance(data, self.config, self.platform)
        except Exception:
            self.reason = "desktop_acceptance_required"
            return
        driver = self.driver
        if driver is None:
            driver = self._make_driver()
        try:
            await asyncio.to_thread(driver.prepare)
        except Exception as error:
            # 预检失败也要可复核：只暴露固定错误码（如 emergency_hotkey_unavailable），不回显路径。
            code = str(error)
            self.reason = (code if isinstance(error, (Denied, Paused))
                           and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
                           else "desktop_preflight_failed")
            try:
                driver.close()
            except Exception:
                pass
            return
        self.driver = driver
        self.enabled = True
        self.reason = ""
        # 能力表由平台决定：Linux 不声明 desktop.screen（无法在 X11 上识别密码框）。
        self.capabilities = list(CAPABILITIES_BY_PLATFORM[self.platform])
        # 启用后仍要如实标注做不到的项：没声明的能力必须有原因，远端才能分辨
        # “还没验收”和“这个平台做不到”。
        self.unavailable = {key: value for key, value in _reasons(self.platform).items()
                            if key not in self.capabilities}
        if self.platform == "posix":
            self.unavailable["desktop.screen"] = "desktop_capture_not_accepted_on_platform"
        self.suspended = ""

    def _make_driver(self):
        if self.platform == "nt":
            return WindowsDriver(on_stop=self.emergency_stop)
        if self.platform == "darwin":
            return MacDriver(on_stop=self.emergency_stop)
        return X11Driver(on_stop=self.emergency_stop)

    def _require_enabled(self):
        require(self.enabled and self.driver is not None, "desktop_unsupported_or_disabled")

    async def execute(self, method, params, command=None):
        self._require_enabled()
        command = command or {}
        task = {"device_id": self.config.device_id,
                "task_id": str(command.get("task_id") or ""),
                "session_id": str(command.get("session_id") or "")}
        if method == "desktop.inspect":
            return await asyncio.to_thread(self.inspect, params)
        if method == "desktop.request_control":
            return await self.request_control(params, task)
        if method == "desktop.act":
            return await asyncio.to_thread(self.act, params, task)
        if method == "desktop.screen":
            # Linux 未验收窗口采集，不声明能力；即使直接调用也必须拒绝，不能静默降级。
            require("desktop.screen" in self.capabilities, "capability_unavailable")
            return await asyncio.to_thread(self.screen, params, task)
        raise Denied("desktop_unsupported_or_disabled")

    def inspect(self, params):
        fields(params, (), ("limit", "window_id"))
        limit = integer(params.get("limit", 50), 1, 50) if "limit" in params else 50
        locked, windows = self.driver.list_windows(limit)
        # 只有本机显式授权的应用进程才可见; 白名单为空时看不到任何窗口。
        windows = [window for window in windows
                   if not self._forbidden(window) and self._app_allowed(window)]
        view = {"locked": bool(locked), "windows": windows,
                "takeover_active": self.lease is not None,
                "suspended": self.suspended}
        if "window_id" in params:
            # UIA 元素列表只对本机授权应用开放；只能看到可激活控件，不返回文本内容。
            window = self.driver.window(integer(params["window_id"], 1, 2**63 - 1))
            self._reject_forbidden(window)
            require(self._app_allowed(window), "desktop_app_not_authorized")
            view["window"] = {key: window[key] for key in ("hwnd", "title", "process")}
            view["elements"] = self._elements(window["hwnd"])
        return view

    def _elements(self, hwnd):
        """规范化 UIA 元素列表；不返回输入值，敏感标题以占位符替代。"""
        out = []
        for item in self.driver.list_elements(int(hwnd), ELEMENT_LIMIT)[:ELEMENT_LIMIT]:
            role = str(item.get("role") or "").lower()
            if role not in ELEMENT_TYPES:
                continue
            name = str(item.get("name") or "")[:80]
            if SENSITIVE_NAME.search(name):
                name = SENSITIVE_PLACEHOLDER
            out.append({"index": int(item["index"]), "role": role, "name": name,
                        "enabled": bool(item.get("enabled", True))})
        return out

    def _resolve_element(self, hwnd, element, role, name):
        """重新解析元素并校验角色/名称；元素被替换或改名即拒绝，避免按旧下标误点。"""
        require(role in ELEMENT_TYPES, "desktop_element_role_not_allowed")
        require(name != SENSITIVE_PLACEHOLDER and not SENSITIVE_NAME.search(name),
                "desktop_element_sensitive")
        for item in self._elements(hwnd):
            if item["index"] != element:
                continue
            require(item["role"] == role and item["name"] == name, "desktop_element_changed")
            require(item["enabled"], "desktop_element_disabled")
            return item
        raise Denied("desktop_element_missing")

    async def request_control(self, params, task):
        fields(params, ("window_id", "scopes", "reason"))
        window_id = integer(params["window_id"], 1, 2**63 - 1)
        scopes = params["scopes"]
        require(type(scopes) is list and 1 <= len(scopes) <= len(ACTION_SCOPES)
                and len(set(scopes)) == len(scopes)
                and all(scope in ACTION_SCOPES for scope in scopes), "invalid_scopes")
        reason = text(params["reason"], 200).strip()
        require(bool(reason), "reason_required")
        require(bool(self.config.device_id), "pairing_required")
        require(self.lease is None, "desktop_lease_active")
        # 紧急停止只终结当前租约；再次接管必须重新走本机原生批准（每次都是独立决定），
        # 不保留任何“永久同意”，因此不在此处永久封死桌面能力。
        window = self.driver.window(window_id)
        self._reject_forbidden(window)
        require(self._app_allowed(window), "desktop_app_not_authorized")
        if self.driver.secure():
            raise Paused("desktop_secure_desktop")
        request = {"device_id": self.config.device_id,
                   "device_name": text(self.config.name, 100),
                   "task_id": task["task_id"], "session_id": task["session_id"],
                   "window": {key: window[key] for key in ("hwnd", "title", "class_name",
                                                           "pid", "process", "rect")},
                   "scopes": list(scopes), "reason": reason, "seconds": LEASE_SECONDS,
                   "hotkey": HOTKEY_TEXT}
        approved = await asyncio.to_thread(self.driver.approve, request)
        if not approved:
            # 到期自动拒绝要和用户主动拒绝分开上报：小窗和审计才能说清是谁做的决定。
            timed_out = getattr(self.driver, "last_approval", "") == "timeout"
            self.suspended = "approval_timeout" if timed_out else "approval_declined"
            raise Paused(APPROVAL_TIMEOUT if timed_out else "desktop_approval_declined")
        # 对话框期间窗口可能被关闭、替换或桌面被锁定，必须重新核对。
        fresh = self.driver.window(window_id)
        self._reject_forbidden(fresh)
        require(self._app_allowed(fresh), "desktop_app_not_authorized")
        require(fresh["pid"] == window["pid"] and fresh["class_name"] == window["class_name"],
                "desktop_target_changed")
        if self.driver.secure():
            raise Paused("desktop_secure_desktop")
        now = self.clock()
        baseline = self.driver.last_input()
        self.lease = Lease(self.config.device_id, task["task_id"], task["session_id"],
                           fresh, scopes, now, baseline)
        # 新批准建立后才清除紧急停止标记；批准本身是本机用户的显式动作。
        self.revoked = False
        self.suspended = ""
        self.driver.begin_lease(fresh)
        self.driver.set_status("桌面接管中 · " + HOTKEY_TEXT + " 停止")
        return {"approved": True, "lease": self.lease.public(now), "reason": reason}

    def act(self, params, task):
        fields(params, ("action",), ("x", "y", "text", "keys", "amount", "element", "role", "name"))
        action = params["action"]
        require(action in ACTION_SCOPES, "invalid_action")
        if action == "click":
            integer(params.get("x"), 0, 100000)
            integer(params.get("y"), 0, 100000)
        elif action == "type_text":
            value = text(params.get("text"), 512)
            require(all(ord(c) >= 32 or c in "\n\t" for c in value), "invalid_text")
        elif action == "keys":
            _validate_keys(params.get("keys"))
        elif action == "invoke":
            # UIA 元素动作按“下标 + 角色 + 名称”三重校验, 名称不进入回执。
            element = integer(params.get("element"), 0, ELEMENT_LIMIT - 1)
            role = text(params.get("role"), 32)
            name = text(params.get("name"), 120)
            require(bool(name), "desktop_element_name_required")
        else:
            integer(params.get("x"), 0, 100000)
            integer(params.get("y"), 0, 100000)
            integer(params.get("amount"), -10, 10)
        if action in ("click", "scroll") and self.lease is not None:
            _require_point(self.lease.window, params["x"], params["y"])
        lease = self._precheck(task, action)
        window = lease.window
        try:
            if action == "invoke":
                # 先在本机重新解析并比对，再由驱动在激活前再核一次。
                self._resolve_element(window["hwnd"], element, role, name)
                self.driver.focus(window["hwnd"])
                self.driver.invoke_element(window["hwnd"], element, role, name)
            else:
                self.driver.focus(window["hwnd"])
                self.driver.perform(action, params, window)
        except Paused:
            self._suspend("focus_or_input_failed")
            raise
        # 本次动作由 Companion 注入, 也会推进系统“最后输入时间”。
        # 必须把基线推进到本次动作之后, 否则下一次复核会把自己的输入当成用户输入而误暂停。
        lease.baseline = self.driver.mark_baseline()
        result = {"action": action, "window": str(window.get("title") or "")[:120],
                  "lease": self.lease.public(self.clock())}
        if action == "invoke":
            result["element"] = {"index": element, "role": role}
        return result

    def screen(self, params, task):
        """真实桌面画面：只采集被接管窗口本身，且仅在用户授予 view 范围的租约内。"""
        fields(params, (), ())
        lease = self.lease
        if lease is None:
            raise Paused("desktop_control_not_approved")
        reason = lease.check(self.clock(), task, "view")
        if reason:
            self._suspend(reason)
            raise Paused("desktop_control_suspended")
        if self.revoked:
            self._suspend("revoked")
            raise Paused("desktop_control_suspended")
        window = str(lease.window.get("title") or "")[:120]
        # 锁屏、安全窗口或密码框有焦点时不采集，也绝不截整屏；租约不因此被终结。
        if self.driver.sensitive_focus(lease.window["hwnd"]):
            return {"unavailable": "sensitive_input", "window": window}
        return {"jpeg": self.driver.capture_window(lease.window["hwnd"]),
                "execution_mode": "desktop", "window": window}

    def _precheck(self, task, action):
        lease = self.lease
        if lease is None:
            raise Paused("desktop_control_not_approved")
        reason = lease.check(self.clock(), task, action)
        if reason:
            self._suspend(reason)
            raise Paused("desktop_control_suspended")
        if self.revoked:
            self._suspend("revoked")
            raise Paused("desktop_control_suspended")
        if self.driver.secure():
            self._suspend("secure_desktop")
            raise Paused("desktop_control_suspended")
        if not self.driver.same_window(lease.window):
            self._suspend("target_window_changed")
            raise Paused("desktop_control_suspended")
        if not self._app_allowed(lease.window):
            self._suspend("app_not_authorized")
            raise Paused("desktop_control_suspended")
        if self.driver.external_input_since(lease.baseline):
            # 用户真实输入优先：一旦检测到就撤销，不尝试“抢回来”。
            self._suspend("user_input")
            raise Paused("desktop_control_suspended")
        return lease

    def _suspend(self, reason):
        self.suspended = reason
        self.lease = None
        if self.driver is not None:
            self.driver.end_lease()
            self.driver.set_status("桌面接管已暂停: " + reason)

    def _app_allowed(self, window):
        """本机应用白名单（进程名）; 未列出的应用即使窗口可见也不可接管。"""
        process = str(window.get("process") or "").lower()
        return bool(process) and process in set(self.config.desktop_apps or [])

    @staticmethod
    def _forbidden(window):
        # 三个平台的安全界面黑名单合并判定：即使驱动已按平台过滤，这里再兜一层。
        return (window.get("pid") == os.getpid()
                or str(window.get("process") or "").lower() in SECURITY_PROCESSES_ALL
                or str(window.get("class_name") or "").lower() in SHELL_CLASSES)

    def _reject_forbidden(self, window):
        require(not self._forbidden(window), "security_ui_not_targetable")

    def emergency_stop(self):
        """托盘/热键线程调用；不等待网络，不做任何远端确认。"""
        self.revoked = True
        self.suspended = "emergency_stop"
        self.lease = None
        if self.driver is not None:
            try:
                self.driver.end_lease()
                self.driver.set_status("桌面接管已停止: " + HOTKEY_TEXT)
            except Exception:
                pass

    async def release(self):
        self.lease = None
        self.suspended = "released"
        if self.driver is not None:
            await asyncio.to_thread(self.driver.end_lease)

    async def close(self):
        await self.release()
        if self.driver is not None:
            await asyncio.to_thread(self.driver.close)

    def status(self):
        driver = self.driver
        return {"enabled": self.enabled, "reason": self.reason, "suspended": self.suspended,
                "apps": list(self.config.desktop_apps or []),
                "lease": self.lease.public(self.clock()) if self.lease else None,
                "hotkey": HOTKEY_TEXT,
                # 预检逐项结果和 UIA 实测摘要: 实机验收要能复核, 不能只回一句 enabled。
                "checks": dict(getattr(driver, "checks", None) or {}),
                "uia": dict(getattr(driver, "uia_probe", None) or {})}


def _require_point(window, x, y):
    left, top, right, bottom = window.get("rect") or (0, 0, 0, 0)
    require(left <= x < right and top <= y < bottom, "desktop_point_outside_window")


def _validate_keys(value):
    require(type(value) is list and 1 <= len(value) <= 4, "invalid_keys")
    require(len(set(value)) == len(value), "invalid_keys")
    modifiers = [item for item in value if item in ("ctrl", "alt", "shift")]
    keys = [item for item in value if item not in ("ctrl", "alt", "shift")]
    require(len(keys) == 1 and len(modifiers) + 1 == len(value), "invalid_keys")
    key = keys[0]
    require(bool(re.match(r"^(?:[a-z0-9]|f(?:[1-9]|1[0-2])|escape|tab|enter|up|down|left|right|"
                          r"home|end|pageup|pagedown|backspace|delete)$", key)), "invalid_keys")
    blocked = {("ctrl", "alt", "delete"), ("ctrl", "shift", "escape"),
               ("alt", "tab"), ("alt", "escape"), ("ctrl", "escape")}
    require(tuple(sorted(value)) not in {tuple(sorted(x)) for x in blocked}, "system_shortcut_blocked")


class WindowsDriver:
    """只由 Desktop 调用；构造/预检失败一律关闭桌面能力。"""

    def __init__(self, on_stop=None):
        self.on_stop = on_stop
        self.checks = {}
        self.hotkey_ready = False
        self.hotkey_thread = None
        self.hotkey_thread_id = 0
        self.tray = None
        self.tray_ready = threading.Event()
        self.status = "桌面接管未启用"
        self._closed = False
        self._user32 = None
        self._baseline = 0
        self.last_approval = ""
        self.approval_seconds = APPROVAL_SECONDS
        self.acceptance_seconds = ACCEPTANCE_SECONDS

    def prepare(self):
        require(os.name == "nt", "windows_only")
        import pywinauto  # noqa: F401  (UIA COM 初始化)
        import pystray  # noqa: F401
        import win32con  # noqa: F401
        import win32gui  # noqa: F401
        from PIL import Image  # noqa: F401
        self._user32 = ctypes.windll.user32
        require(self._input_desktop_name() == "Default", "desktop_locked")
        self.checks = {"uia": False, "native_dialog": False, "input_sentinel": False,
                       "secure_desktop_query": True, "focus_query": False,
                       "hotkey": False, "tray": False}
        # 不能只靠 import 判断 UIA：必须实际枚举到窗口与控件树。
        self.uia_probe = self.probe_uia()
        self.checks["uia"] = True
        self.last_input()
        self.checks["input_sentinel"] = True
        self.foreground()
        self.checks["focus_query"] = True
        self._start_hotkey()
        require(self.hotkey_ready, "emergency_hotkey_unavailable")
        self.checks["hotkey"] = True
        self._start_tray()
        require(self.tray_ready.wait(8), "tray_unavailable")
        self.checks["tray"] = True
        return dict(self.checks)

    def probe_uia(self):
        """UIA 实测：能枚举桌面窗口且至少一个窗口的控件树可读，才算具备 UIA。"""
        from pywinauto import Desktop as UiaDesktop
        desktop = UiaDesktop(backend="uia")
        windows = [window for window in desktop.windows()
                   if str(window.window_text() or "").strip()]
        require(bool(windows), "uia_unavailable")
        for window in windows[:8]:
            try:
                list(window.descendants(depth=3))
            except Exception:
                continue
            return {"windows": len(windows), "tree_readable": True}
        raise Denied("uia_unavailable")

    def sensitive_focus(self, hwnd):
        """锁屏/安全窗口或密码框有焦点时暂停采集；判断不了就按敏感处理。"""
        if self.secure():
            return True
        try:
            for wrapper in self._uia_root(int(hwnd)).descendants(depth=8):
                info = wrapper.element_info
                if bool(getattr(info, "is_password", False)) \
                        and bool(getattr(info, "has_keyboard_focus", False)):
                    return True
        except Exception:
            return True
        return False

    def capture_window(self, hwnd):
        """只用 PrintWindow 截取被授权窗口本身：不截整屏、不截其他应用、不读剪贴板。"""
        import win32gui
        import win32ui
        from ctypes import windll
        from PIL import Image

        hwnd = int(hwnd)
        require(bool(win32gui.IsWindow(hwnd)), "desktop_window_missing")
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        require(0 < width <= 8192 and 0 < height <= 8192, "desktop_window_unavailable")
        window_dc = win32gui.GetWindowDC(hwnd)
        require(bool(window_dc), "desktop_capture_failed")
        try:
            source = win32ui.CreateDCFromHandle(window_dc)
            memory = source.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            try:
                bitmap.CreateCompatibleBitmap(source, width, height)
                memory.SelectObject(bitmap)
                # PW_RENDERFULLCONTENT 支持 UWP；失败时回退到普通 PrintWindow。
                if not windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), 0x00000002):
                    require(bool(windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), 0)),
                            "desktop_capture_failed")
                info = bitmap.GetInfo()
                image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                                         bitmap.GetBitmapBits(True), "raw", "BGRX", 0, 1)
            finally:
                win32gui.DeleteObject(bitmap.GetHandle())
                memory.DeleteDC()
                source.DeleteDC()
        finally:
            win32gui.ReleaseDC(hwnd, window_dc)
        image.thumbnail((1280, 800))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=70)
        data = buffer.getvalue()
        require(len(data) <= FRAME_BYTES, "desktop_capture_too_large")
        return base64.b64encode(data).decode("ascii")

    def uia_app_report(self, extra=()):
        """对已打开的标准/登记应用做只读 UIA 探测：只回报可读性与控件数，不含名称与内容。"""
        wanted = {name.lower() for name in STANDARD_APPS}
        wanted |= {str(name).lower() for name in (extra or ())}
        report = {}
        for info in self.list_windows(60)[1]:
            process = str(info.get("process") or "").lower()
            if process not in wanted:
                continue
            try:
                count = len(self.list_elements(info["hwnd"], 40))
            except Exception:
                report[process] = {"readable": False, "elements": 0}
                continue
            report[process] = {"readable": True, "elements": count}
        return report

    def _start_hotkey(self):
        import win32con

        ready = threading.Event()

        def loop():
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            self.hotkey_thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
            ok = user32.RegisterHotKey(None, HOTKEY_ID,
                                       win32con.MOD_CONTROL | win32con.MOD_ALT, win32con.VK_PAUSE)
            self.hotkey_ready = bool(ok)
            ready.set()
            if not ok:
                return
            try:
                message = wintypes.MSG()
                while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                    if message.message == 0x0312 and message.wParam == HOTKEY_ID:
                        self.stop()
            finally:
                user32.UnregisterHotKey(None, HOTKEY_ID)

        self.hotkey_thread = threading.Thread(target=loop, name="zops-desktop-hotkey", daemon=True)
        self.hotkey_thread.start()
        ready.wait(5)

    def _start_tray(self):
        import pystray
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (64, 64), "white")
        draw = ImageDraw.Draw(image)
        draw.ellipse((8, 8, 56, 56), outline="black", width=4)
        draw.rectangle((28, 20, 36, 36), fill="black")
        draw.rectangle((20, 40, 44, 46), fill="black")
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: self.status, None, enabled=False),
            pystray.MenuItem("停止桌面接管 (" + HOTKEY_TEXT + ")", self.stop, default=True),
        )
        self.tray = pystray.Icon("zops-companion-desktop", image, "Z-OPS Companion", menu)

        def setup(icon):
            icon.visible = True
            self.tray_ready.set()

        threading.Thread(target=lambda: self.tray.run(setup=setup),
                         name="zops-desktop-tray", daemon=True).start()

    def set_status(self, value):
        self.status = str(value)[:80]
        if self.tray is not None:
            try:
                self.tray.title = "Z-OPS Companion — " + self.status
            except Exception:
                pass

    def stop(self):
        if self.on_stop:
            self.on_stop()
        self.set_status("桌面接管已停止: " + HOTKEY_TEXT)

    def approve(self, request):
        """原生 Win32 对话框；返回值不经过网页、模型或远端消息。"""
        require(self._input_desktop_name() == "Default", "desktop_locked")
        body = (
            "Companion 请求临时接管一个应用窗口。请只在你本人确认时选择“是”。\n\n"
            f"设备: {request['device_name']} ({request['device_id'][:12]})\n"
            f"任务: {request['task_id']}\n"
            f"会话: {request['session_id']}\n"
            f"窗口: {str(request['window']['title'])[:80]} "
            f"[PID {request['window']['pid']}, {request['window']['class_name']}]\n"
            f"允许动作: {', '.join(request['scopes'])}\n"
            f"原因: {request['reason']}\n\n"
            f"有效期: {LEASE_SECONDS} 秒，不自动续期。\n"
            f"你操作鼠标键盘、切换/关闭窗口、锁屏或按 {HOTKEY_TEXT} 会立即暂停。\n"
            "网页、模型和远端消息不能代你批准。")
        body += f"\n\n{self.approval_seconds} 秒内无操作将自动拒绝。"
        answer, timed_out = self._dialog(body, "Z-OPS Companion — 本机桌面接管",
                                         self.approval_seconds,
                                         0x00000004 | 0x00000030 | 0x00000100
                                         | 0x00040000 | 0x00010000)
        self.last_approval = ("timeout" if timed_out
                              else ("approved" if answer == 6 else "declined"))
        return self.last_approval == "approved"

    def confirm_acceptance(self, body):
        """验收写入前的本机确认；返回 False 就不写记录。"""
        body += f"\n\n{self.acceptance_seconds} 秒内无操作将不写入。"
        answer, timed_out = self._dialog(body, "Z-OPS Companion — 桌面验收",
                                         self.acceptance_seconds,
                                         0x00000004 | 0x00000030 | 0x00000100)
        self.last_approval = ("timeout" if timed_out
                              else ("approved" if answer == 6 else "declined"))
        return answer == 6 and not timed_out

    def _dialog(self, body, title, seconds, flags):
        """MessageBox 没有超时参数：放到子线程弹出，到期按标题关闭（等同拒绝）。"""
        answer = {}

        def show():
            answer["value"] = ctypes.windll.user32.MessageBoxW(None, body, title, flags)

        thread = threading.Thread(target=show, name="zops-desktop-dialog", daemon=True)
        thread.start()
        thread.join(float(seconds))
        if thread.is_alive():
            self._close_dialog(title)
            thread.join(2)
            # 关不掉也不能继续等：这次批准按“到期拒绝”处理，绝不默认同意。
            return None, True
        return answer.get("value"), False

    def _close_dialog(self, title):
        """只关闭本进程内同标题的对话框，不动用户的其它窗口。"""
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def visit(hwnd, _param):
            # 句柄一律按 void* 传：默认 argtypes 会当成 32 位 int，高地址句柄会被截断。
            handle = ctypes.c_void_p(hwnd)
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            if pid.value != os.getpid():
                return True
            length = int(user32.GetWindowTextLengthW(handle))
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(handle, buffer, length + 1)
            if buffer.value == title:
                found.append(handle)
                return False
            return True

        user32.EnumWindows(visit, None)
        for handle in found:
            user32.PostMessageW(handle, 0x0010, 0, 0)
        return bool(found)

    def _input_desktop_name(self):
        DESKTOP_SWITCHDESKTOP = 0x0100
        UOI_NAME = 2
        user32 = ctypes.windll.user32
        # 收/发句柄的调用都必须显式声明类型：默认 restype 是 c_int，64 位句柄会被截断。
        # 截断后 CloseDesktop 关不掉原句柄（泄漏），名字查询失败又会被当成“已锁屏”，
        # 桌面能力会永久不可用；查询成功时也可能拿错桌面的名字。
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.OpenInputDesktop.argtypes = (ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong)
        user32.GetUserObjectInformationW.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong))
        user32.CloseDesktop.argtypes = (ctypes.c_void_p,)
        handle = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not handle:
            return "unavailable"
        try:
            buffer = ctypes.create_unicode_buffer(256)
            needed = ctypes.c_ulong(0)
            if not user32.GetUserObjectInformationW(handle, UOI_NAME, buffer,
                                                    ctypes.sizeof(buffer), ctypes.byref(needed)):
                return "unavailable"
            return buffer.value
        finally:
            user32.CloseDesktop(handle)

    def secure(self):
        if self._input_desktop_name() != "Default":
            return True
        try:
            import win32gui
            import win32process
            import psutil
            foreground = win32gui.GetForegroundWindow()
            if not foreground:
                return False
            _, pid = win32process.GetWindowThreadProcessId(foreground)
            return psutil.Process(pid).name().lower() in SECURITY_PROCESSES
        except Exception:
            return True

    def last_input(self):
        from ctypes import wintypes

        class LastInput(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LastInput()
        info.cbSize = ctypes.sizeof(LastInput)
        require(bool(self._user32.GetLastInputInfo(ctypes.byref(info))), "input_sentinel_unavailable")
        return int(info.dwTime)

    def external_input_since(self, baseline):
        return ((self.last_input() - int(baseline)) & 0xFFFFFFFF) > 100

    def mark_baseline(self):
        """返回新的输入基线；调用方（Desktop）据此推进租约基线。"""
        self._baseline = self.last_input()
        return self._baseline

    def _window_info(self, hwnd):
        import win32gui
        import win32process
        import psutil

        require(win32gui.IsWindow(hwnd), "desktop_window_missing")
        require(win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd),
                "desktop_window_unavailable")
        title = win32gui.GetWindowText(hwnd)[:120]
        require(bool(title.strip()), "desktop_window_untitled")
        class_name = win32gui.GetClassName(hwnd)[:120]
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            process = psutil.Process(pid).name()[:80]
        except psutil.Error:
            process = ""
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return {"hwnd": int(hwnd), "title": title, "class_name": class_name, "pid": int(pid),
                "process": process, "rect": [int(left), int(top), int(right), int(bottom)]}

    @staticmethod
    def _forbidden(info):
        return (info["pid"] == os.getpid() or info["process"].lower() in SECURITY_PROCESSES
                or info["class_name"].lower() in SHELL_CLASSES)

    def window(self, hwnd):
        info = self._window_info(int(hwnd))
        require(not self._forbidden(info), "security_ui_not_targetable")
        return info

    def list_windows(self, limit):
        import win32gui

        if self._input_desktop_name() != "Default":
            return True, []
        windows = []

        def visit(hwnd, _):
            if len(windows) >= limit:
                return False
            try:
                info = self._window_info(hwnd)
            except Exception:
                return True
            if not self._forbidden(info):
                windows.append(info)
            return True

        win32gui.EnumWindows(visit, None)
        return False, windows

    def _uia_root(self, hwnd):
        from pywinauto import Desktop as UiaDesktop
        require(int(hwnd) > 0, "desktop_window_missing")
        return UiaDesktop(backend="uia").window(handle=int(hwnd)).wrapper_object()

    def list_elements(self, hwnd, limit):
        """只读遍历 UIA 树：只返回可激活控件，不读取输入值或文本内容。"""
        out = []
        for wrapper in self._uia_root(hwnd).descendants(depth=6):
            if len(out) >= limit:
                break
            try:
                info = wrapper.element_info
            except Exception:
                continue
            role = _control_role(info)
            if role not in ELEMENT_TYPES:
                continue
            name = str(getattr(info, "name", "") or "")
            if not name.strip() or bool(getattr(info, "is_offscreen", False)):
                continue
            out.append({"index": len(out), "role": role, "name": name[:80],
                        "enabled": bool(getattr(info, "is_enabled", True))})
        return out

    def invoke_element(self, hwnd, element, role, name):
        """只使用 UIA InvokePattern 激活；不回退到坐标点击，也不读取控件值。"""
        target = None
        position = 0
        for wrapper in self._uia_root(hwnd).descendants(depth=6):
            try:
                info = wrapper.element_info
            except Exception:
                continue
            if _control_role(info) != role:
                continue
            label = str(getattr(info, "name", "") or "")
            if not label.strip() or bool(getattr(info, "is_offscreen", False)):
                continue
            if position == element:
                require(label[:80] == name, "desktop_element_changed")
                target = wrapper
                break
            position += 1
        require(target is not None, "desktop_element_missing")
        try:
            target.invoke()
        except Exception:
            try:
                target.iface_invoke.Invoke()
            except Exception:
                raise Denied("desktop_element_not_actionable") from None

    def same_window(self, identity):
        try:
            import win32gui
            info = self._window_info(identity["hwnd"])
        except Exception:
            return False
        return (info["pid"] == identity["pid"] and info["class_name"] == identity["class_name"]
                and win32gui.IsWindow(identity["hwnd"]))

    def foreground(self):
        import win32gui
        return int(win32gui.GetForegroundWindow())

    def focus(self, hwnd):
        import win32gui
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            raise Paused("desktop_focus_denied") from None
        if self.foreground() != int(hwnd):
            raise Paused("desktop_focus_denied")

    def begin_lease(self, window):
        self.mark_baseline()
        self.set_status("桌面接管中: " + str(window.get("title") or "")[:40])

    def end_lease(self):
        self.set_status("桌面接管已结束")
        self.mark_baseline()

    def perform(self, action, params, window):
        from pywinauto import keyboard, mouse

        hwnd = window["hwnd"]
        left, top, right, bottom = window["rect"]
        if action in ("click", "scroll"):
            x, y = params["x"], params["y"]
            require(left <= x < right and top <= y < bottom, "desktop_point_outside_window")
        if action == "type_text":
            self.focus(hwnd)
            keyboard.send_keys(_literal_text(params["text"]), with_spaces=True, pause=0.01)
        elif action == "keys":
            self.focus(hwnd)
            keyboard.send_keys(_key_sequence(params["keys"]), pause=0.01)
        elif action == "click":
            self.focus(hwnd)
            require(left <= params["x"] < right and top <= params["y"] < bottom,
                    "desktop_point_outside_window")
            mouse.click(button="left", coords=(params["x"], params["y"]))
        else:
            self.focus(hwnd)
            require(left <= params["x"] < right and top <= params["y"] < bottom,
                    "desktop_point_outside_window")
            mouse.scroll(coords=(params["x"], params["y"]), wheel_dist=params["amount"])

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.end_lease()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        if self.hotkey_thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self.hotkey_thread_id, 0x0012, 0, 0)
            except Exception:
                pass
        if self.hotkey_thread is not None:
            self.hotkey_thread.join(3)


class NativeDriverBase:
    """X11 与 macOS 共用的本机原生驱动层。

    两个平台真正一致的部分都在这里：原生批准对话框、托盘与全局热键轮询、窗口
    规范化与黑名单过滤、租约状态、以及基于“系统空闲计时”的外部输入哨兵。
    子类只负责三件平台相关的事：准备后端并逐项预检（prepare/_preflight）、
    给出安全界面黑名单与停止热键文案、实现平台能力差异（控件树/截图/密码框）。
    分开写两份会让两边的安全语义逐轮漂移，那是不可接受的。
    """

    hotkey_text = HOTKEY_TEXT
    security_processes = frozenset()
    shell_classes = SHELL_CLASSES
    checks_names = ACCEPTANCE_CHECKS

    def __init__(self, on_stop=None):
        self.on_stop = on_stop
        self.checks = {}
        self.uia_probe = {}
        self.status = "桌面接管未启用"
        self.tray_ready = False
        # 托盘在无桌面环境下永远就绪不了；测试用它把等待缩短到毫秒级。
        self.tray_timeout = 3
        self._backend = None
        self._baseline = 0.0
        self._stop = threading.Event()
        self._hotkey_thread = None
        self._tray = None
        self._closed = False
        self.last_approval = ""
        self.approval_seconds = float(APPROVAL_SECONDS)
        self.acceptance_seconds = float(ACCEPTANCE_SECONDS)

    @staticmethod
    def _dialog_ready():
        """原生对话框必须能真的弹出来；只 import tkinter 不算。"""
        try:
            import tkinter

            root = tkinter.Tk()
            root.withdraw()
            root.destroy()
            return True
        except Exception:
            return False

    # ---- 窗口 ----
    def _window(self, info):
        pid = int(info.get("pid") or 0)
        process = ""
        if pid > 0:
            try:
                import psutil

                process = psutil.Process(pid).name()[:80]
            except Exception:
                process = ""
        return {"hwnd": int(info["hwnd"]), "title": str(info.get("title") or "")[:120],
                "class_name": str(info.get("class_name") or "")[:120], "pid": pid,
                "process": process, "rect": [int(value) for value in info["rect"]]}

    def _forbidden(self, info):
        return (info["pid"] == os.getpid()
                or info["process"].lower() in self.security_processes
                or info["class_name"].lower() in self.shell_classes)

    def window(self, xid):
        info = self._window(self._backend.info(int(xid)))
        require(not self._forbidden(info), "security_ui_not_targetable")
        # 拿不到进程就无法核对应用白名单；宁可拒绝，也不降级成按窗口类放行。
        # Linux 上没有 _NET_WM_PID 的窗口就在这里被拦下，macOS 的 CGWindowList 永远带 PID。
        require(info["pid"] > 0 and bool(info["process"]), "desktop_window_pid_unavailable")
        return info

    def list_windows(self, limit):
        if self._backend.locked():
            return True, []
        windows = [self._window(info) for info in self._backend.windows(limit)]
        # 无法核对进程的窗口永远不可接管，也不在列表里出现（宁可少给，不给错的）。
        return False, [item for item in windows
                       if item["pid"] > 0 and not self._forbidden(item)]

    def same_window(self, identity):
        try:
            info = self._window(self._backend.info(int(identity["hwnd"])))
        except Exception:
            return False
        return info["pid"] == identity["pid"] and info["class_name"] == identity["class_name"]

    # ---- 安全状态 ----
    def secure(self):
        """锁屏/屏保/查询失败都按“用户不在场”处理。"""
        try:
            return bool(self._backend.locked())
        except Exception:
            return True

    # ---- 输入基线 ----
    def last_input(self):
        idle = self._backend.idle_ms()
        require(idle >= 0, "input_sentinel_unavailable")
        return time.monotonic() - idle / 1000.0

    def external_input_since(self, baseline):
        try:
            # 无新输入时空闲计时随时间增长, last_input 保持恒定; 用户一动就跳到当前时刻。
            return self.last_input() > float(baseline) + 0.1
        except Exception:
            return True

    def mark_baseline(self):
        """注入会让系统的空闲计时归零；等它反映本次注入，避免把自己的输入当成用户输入。"""
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                if self._backend.idle_ms() < 60:
                    break
            except Exception:
                break
            time.sleep(0.01)
        self._baseline = self.last_input()
        return self._baseline

    # ---- 焦点与注入 ----
    def foreground(self):
        return self._backend.top_level(self._backend.focus())

    def focus(self, hwnd):
        target = int(hwnd)
        if not self._backend.set_focus(target):
            raise Paused("desktop_focus_denied")
        if self._backend.top_level(self._backend.focus()) != target:
            raise Paused("desktop_focus_denied")

    def perform(self, action, params, window):
        left, top, right, bottom = window["rect"]
        if action in ("click", "scroll"):
            require(left <= params["x"] < right and top <= params["y"] < bottom,
                    "desktop_point_outside_window")
        if action == "type_text":
            self._backend.type_text(params["text"])
        elif action == "keys":
            self._backend.press(params["keys"])
        elif action == "click":
            self._backend.click(params["x"], params["y"])
        else:
            self._backend.scroll(params["x"], params["y"], params["amount"])

    # ---- 租约与状态 ----
    def begin_lease(self, window):
        self.mark_baseline()
        self.set_status("桌面接管中: " + str(window.get("title") or "")[:40])

    def end_lease(self):
        self.set_status("桌面接管已结束")
        try:
            self.mark_baseline()
        except Exception:
            pass

    def set_status(self, value):
        self.status = str(value)[:80]
        if self._tray is not None:
            try:
                self._tray.title = "Z-OPS Companion — " + self.status
            except Exception:
                pass

    def stop(self):
        if self.on_stop:
            self.on_stop()
        self.set_status("桌面接管已停止: " + self.hotkey_text)

    def _start_hotkey_loop(self):
        def loop():
            while not self._stop.is_set():
                try:
                    if self._backend.hotkey_pending():
                        self.stop()
                        return
                except Exception:
                    return
                time.sleep(0.05)

        self._hotkey_thread = threading.Thread(target=loop, name="zops-desktop-hotkey", daemon=True)
        self._hotkey_thread.start()

    def _start_tray(self):
        """托盘只是第二条停止通道：没有托盘宿主时如实记 false，不阻断热键。"""
        try:
            # pystray 在无托盘宿主的会话里会一直重试并往 stderr 打堆栈，先问清楚再启动。
            if not (self._backend is not None and self._backend.tray_available()):
                self.tray_ready = False
                return
        except Exception:
            self.tray_ready = False
            return
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            self.tray_ready = False
            return
        try:
            image = Image.new("RGB", (64, 64), "white")
            draw = ImageDraw.Draw(image)
            draw.ellipse((8, 8, 56, 56), outline="black", width=4)
            menu = pystray.Menu(
                pystray.MenuItem(lambda _: self.status, None, enabled=False),
                pystray.MenuItem("停止桌面接管 (" + self.hotkey_text + ")", self.stop, default=True))
            icon = pystray.Icon("zops-companion-desktop", image, "Z-OPS Companion", menu)
            ready = threading.Event()

            def setup(active):
                active.visible = True
                ready.set()

            threading.Thread(target=lambda: icon.run(setup=setup), name="zops-desktop-tray",
                             daemon=True).start()
            self._tray = icon if ready.wait(self.tray_timeout) else None
        except Exception:
            self._tray = None
        self.tray_ready = self._tray is not None

    # ---- 原生批准 ----
    def _ask(self, title, body, accept_text, decline_text, seconds=None):
        """原生对话框；到期自动关闭并返回 (approved=False, timed_out=True)。"""
        seconds = self.approval_seconds if seconds is None else float(seconds)
        import tkinter

        root = tkinter.Tk()
        answer = {"ok": False, "timeout": False}
        try:
            root.title(title)
            root.attributes("-topmost", True)
            root.resizable(False, False)
            tkinter.Label(root, text=body, justify="left", anchor="w", padx=16,
                          pady=16).pack()
            row = tkinter.Frame(root)
            row.pack(pady=(0, 12))

            def accept():
                answer["ok"] = True
                root.destroy()

            def expire():
                # 先记到期再关窗：用户点“允许”与到期同时发生时不能算成批准。
                answer["timeout"] = True
                root.destroy()

            decline = root.destroy
            tkinter.Button(row, text=decline_text, command=decline, width=14).pack(side="left", padx=8)
            tkinter.Button(row, text=accept_text, command=accept, width=14).pack(side="left", padx=8)
            root.protocol("WM_DELETE_WINDOW", decline)
            root.bind("<Escape>", lambda _event: decline())
            root.after(max(1, int(seconds * 1000)), expire)
            # 默认焦点在“拒绍”：误按回车不会批准。
            root.focus_force()
            root.update_idletasks()
            x = max(0, (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2)
            y = max(0, (root.winfo_screenheight() - root.winfo_reqheight()) // 3)
            root.geometry("+%d+%d" % (x, y))
            root.grab_set()
            root.mainloop()
        finally:
            try:
                root.destroy()
            except Exception:
                pass
        return answer["ok"], bool(answer["timeout"])

    def approve(self, request):
        body = (
            "Companion 请求临时接管一个应用窗口。只在你本人确认时选“允许”。\n\n"
            f"设备: {request['device_name']} ({request['device_id'][:12]})\n"
            f"任务: {request['task_id']}\n"
            f"会话: {request['session_id']}\n"
            f"窗口: {str(request['window']['title'])[:80]} "
            f"[PID {request['window']['pid']}, {request['window']['class_name']}]\n"
            f"允许动作: {', '.join(request['scopes'])}\n"
            f"原因: {request['reason']}\n\n"
            f"有效期: {LEASE_SECONDS} 秒，不自动续期。\n"
            f"你操作鼠标键盘、切换/关闭窗口、锁屏或按 {self.hotkey_text} 会立即暂停。\n"
            "网页、模型和远端消息不能代你批准。")
        body += f"\n\n{int(self.approval_seconds)} 秒内无操作将自动拒绍。"
        approved, timed_out = self._ask("Z-OPS Companion — 本机桌面接管", body, "允许", "拒绍",
                                        seconds=self.approval_seconds)
        self.last_approval = ("timeout" if timed_out
                              else ("approved" if approved else "declined"))
        return self.last_approval == "approved"

    def confirm_acceptance(self, body):
        body += f"\n\n{int(self.acceptance_seconds)} 秒内无操作将不写入。"
        approved, timed_out = self._ask("Z-OPS Companion — 桌面验收", body, "写入验收记录",
                                        "不写入", seconds=self.acceptance_seconds)
        # “没人回应”与“有人点了不写入”不是一回事；运维看到的原因要能对上事实。
        self.last_approval = ("timeout" if timed_out
                              else ("approved" if approved else "declined"))
        return approved and not timed_out

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        try:
            self.end_lease()
        except Exception:
            pass
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
        if self._backend is not None:
            try:
                self._backend.close()
            except Exception:
                pass
        if self._hotkey_thread is not None:
            self._hotkey_thread.join(2)


class X11Driver(NativeDriverBase):
    """Linux X11 driver：只经 XTest 注入 + XScreenSaver 查空闲，不提供控件树与窗口截图。

    与 Windows 的差别必须如实上报：没有 UIA 等价物（AT-SPI 未适配），无法判断密码框，
    因此能力表不包含 desktop.screen；没有 _NET_WM_PID 的窗口一律不可接管。
    """

    checks_names = ACCEPTANCE_CHECKS_X11
    security_processes = LINUX_SECURITY_PROCESSES

    def prepare(self):
        require(sys.platform.startswith("linux"), "linux_only")
        # XWayland 下 XTest 对原生 Wayland 客户端无效，整段会话都不可信。
        require(not wayland_session(), "wayland_desktop_not_accepted")
        require(bool(os.environ.get("DISPLAY")), "x11_display_unavailable")
        from .x11 import X11

        backend = X11()
        try:
            self._preflight(backend)
        except Exception:
            backend.close()
            raise
        self._backend = backend
        self._start_hotkey_loop()
        self._start_tray()
        self.uia_probe = backend.probe()
        self.uia_probe["tray"] = bool(self.tray_ready)
        self.uia_probe["elements"] = "not_supported"
        return dict(self.checks)

    def _preflight(self, backend):
        """逐项实测：枚举窗口、查空闲、查焦点、抓热键、开原生对话框，全过才算就绪。"""
        self.checks = {name: False for name in ACCEPTANCE_CHECKS_X11}
        require(bool(backend.windows(8)), "x11_no_visible_windows")
        self.checks["x11"] = True
        require(bool(backend.xss) and backend.idle_ms() >= 0, "lock_query_unavailable")
        self.checks["lock_query"] = True
        self.checks["input_sentinel"] = True
        require(backend.focus() >= 0, "x11_focus_query_failed")
        self.checks["focus_query"] = True
        require(bool(backend.xtest), "x11_injection_unavailable")
        self.checks["injection"] = True
        require(self._dialog_ready(), "native_dialog_unavailable")
        self.checks["native_dialog"] = True
        require(bool(backend.grab_hotkey()), "emergency_hotkey_unavailable")
        self.checks["hotkey"] = True

    def list_elements(self, hwnd, limit):
        # X11 协议没有控件树；AT-SPI 未适配前不假装能列出元素。
        return []

    def invoke_element(self, hwnd, element, role, name):
        raise Denied("desktop_element_not_supported_on_platform")

    # ---- 安全状态 ----
    def sensitive_focus(self, hwnd):
        # X11 拿不到密码框信息，所以 Linux 不声明 desktop.screen；这里只报锁屏。
        return self.secure()

    def capture_window(self, hwnd):
        raise Denied("desktop_capture_not_accepted_on_platform")


class MacDriver(NativeDriverBase):
    """macOS driver：AX 读控件树与焦点子角色，CoreGraphics 做注入与窗口采集。

    与 Linux X11 的实质差别：AX 能识别安全文本输入（AXSecureTextField），也
    能只截被授权的那个窗口，因此可以声明 desktop.screen 并在密码框上主动暂停。
    与 Windows 的差别：没有“安全桌面”那种独立桌面对象，锁屏走会话字典查询。

    这一层没在实机上验收过（本机是 Linux）：预检只接受实测成功的项，辅助功能或屏幕
    录制权限缺失时直接禁用桌面能力，不降级成“看起来可用”（Spec 第 84 条同类纪律）。
    """

    hotkey_text = macos_ax.HOTKEY_TEXT_MAC
    security_processes = MACOS_SECURITY_PROCESSES
    # macOS 没有 Windows 盾层壳窗口；Dock/壁纸由 layer 过滤排除，故不沿用 SHELL_CLASSES。
    shell_classes = frozenset()
    checks_names = ACCEPTANCE_CHECKS_MACOS

    def prepare(self):
        require(sys.platform == "darwin", "macos_only")
        from .ax import AX

        backend = AX()
        try:
            self._preflight(backend)
        except Exception:
            backend.close()
            raise
        self._backend = backend
        self._start_hotkey_loop()
        self._start_tray()
        self.uia_probe = backend.probe()
        self.uia_probe["tray"] = bool(self.tray_ready)
        return dict(self.checks)

    def _preflight(self, backend):
        """逐项实测：辅助功能授权、窗口枚举、空闲哨兵、锁屏查询、焦点、对话框、
        热键与采集权限，全过才算就绪。"""
        self.checks = {name: False for name in self.checks_names}
        # 没有辅助功能权限，任何注入都到不了应用；这一步失败时必须引导用户去授权。
        require(bool(backend.trusted()), "macos_accessibility_permission_required")
        require(bool(backend.windows(8)), "macos_no_visible_windows")
        self.checks["ax"] = True
        require(backend.idle_ms() >= 0, "input_sentinel_unavailable")
        self.checks["input_sentinel"] = True
        require(backend.lock_state() is not None, "lock_query_unavailable")
        self.checks["lock_query"] = True
        require(backend.focus() >= 0, "macos_focus_query_failed")
        self.checks["focus_query"] = True
        require(self._dialog_ready(), "native_dialog_unavailable")
        self.checks["native_dialog"] = True
        require(bool(backend.grab_hotkey()), "emergency_hotkey_unavailable")
        self.checks["hotkey"] = True
        # 采集权限未授予时不声明 desktop.screen；老系统没有预检符号则交给实际采集。
        require(backend.capture_allowed() is not False, "macos_screen_capture_permission_required")
        self.checks["capture"] = True

    # ---- 控件树（AX） ----
    def list_elements(self, hwnd, limit):
        # 只收可激活控件（按钮/菜单/复选框等）且名字可读的那些；不读值也不读正文。
        return self._backend.list_elements(int(hwnd), limit)

    def invoke_element(self, hwnd, element, role, name):
        # 下标 + 角色 + 名称三重核对后只发 AXPress；不回落成坐标点击。
        return self._backend.invoke_element(int(hwnd), element, role, name)

    # ---- 安全状态 ----
    def sensitive_focus(self, hwnd):
        """锁屏或密码框有焦点都算敏感：采集与输入都必须停下来。"""
        if self.secure():
            return True
        try:
            return bool(self._backend.sensitive_focus(int(hwnd)))
        except Exception:
            # 读不到焦点子角色时按敏感处理，不能因为“不确定”就继续截屏。
            return True

    def capture_window(self, hwnd):
        """只采目标窗口自己的矩形：不截整屏、不截其它应用。"""
        frame = self._backend.capture(int(hwnd), limit=FRAME_BYTES)
        # encode_frame 会降级缩图，缩到极限仍超限就如实报错，不静默发超大帧。
        require(len(frame) * 3 // 4 <= FRAME_BYTES, "desktop_capture_too_large")
        return frame

    def capture_probe(self):
        """验收时真采一帧：权限位为真不等于拿得到像素。"""
        windows = self._backend.windows(8)
        require(bool(windows), "macos_no_visible_windows")
        return bool(self.capture_window(windows[0]["hwnd"]))

    def ax_app_report(self, extra=()):
        """只读 AX 探测：回报窗口能否被 AX 解析以及可激活控件数，不含控件名与内容。

        与 Windows 侧的 UIA 报告同一形状，所以验收记录的校验不用分叉；区别是
        macOS 没有“记事本/计算器”这种固定首批应用，白名单为空时按当前可见窗口报。
        """
        wanted = {str(name).lower() for name in (extra or ())}
        report = {}
        for info in self.list_windows(60)[1]:
            process = str(info.get("process") or "").lower()
            if wanted and process not in wanted:
                continue
            try:
                count = len(self.list_elements(info["hwnd"], 40))
            except Exception:
                report[process] = {"readable": False, "elements": 0}
                continue
            report[process] = {"readable": True, "elements": count}
        return report


def _control_role(info):
    """UIA control_type 规范化成稳定小写角色名（如 TabItem -> tabitem）。"""
    return re.sub(r"[^a-z]", "", str(getattr(info, "control_type", "") or "").lower())


def _literal_text(value):
    out = []
    for char in value:
        if char in "+^%~(){}":
            out.append("{" + char + "}")
        elif char == "\n":
            out.append("{ENTER}")
        elif char == "\t":
            out.append("{TAB}")
        else:
            out.append(char)
    return "".join(out)


def _key_sequence(keys):
    prefixes = {"ctrl": "^", "alt": "%", "shift": "+"}
    names = {"escape": "{ESC}", "tab": "{TAB}", "enter": "{ENTER}", "up": "{UP}",
             "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}", "home": "{HOME}",
             "end": "{END}", "pageup": "{PGUP}", "pagedown": "{PGDN}",
             "backspace": "{BACKSPACE}", "delete": "{DELETE}"}
    prefix = "".join(prefixes[item] for item in ("ctrl", "alt", "shift") if item in keys)
    key = next(item for item in keys if item not in prefixes)
    if key.startswith("f") and key[1:].isdigit():
        return prefix + "{" + key.upper() + "}"
    return prefix + names.get(key, key)


def local_desktop_probe():
    """仅本机 CLI 显式执行；启动完整预检但不开放任何远程能力。"""
    if os.name == "nt":
        driver = WindowsDriver()
    elif sys.platform.startswith("linux"):
        driver = X11Driver()
    elif sys.platform == "darwin":
        driver = MacDriver()
    else:
        raise Denied("desktop_unsupported_platform")
    checks = {}
    try:
        checks = driver.prepare()
    finally:
        hotkey_text = driver.hotkey_text
        driver.close()
    return {"enabled": False, "checks": checks, "hotkey": hotkey_text,
            "uia": dict(getattr(driver, "uia_probe", None) or {}),
            "reason": "probe_does_not_enable_remote_desktop"}


def local_windows_probe():
    """旧入口保留为别名，避免旧脚本立刻失效。"""
    return local_desktop_probe()
