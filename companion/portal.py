"""Wayland 会话下的 xdg-desktop-portal 探测层（jeepney 纯 Python D-Bus）。

定位与 `x11.py`、`ax.py` 一致：这一层只回答“这台机器上到底能不能做到”，
符号缺失、总线不可达、portal 没跑、属性读不到，一律原样报固定错误码，
由调用方（`desktop.py`）决定禁用哪些桌面能力，绝不把“读不到”当成“可以”。

为什么要单独一层：X11 的 XTest 注入对原生 Wayland 客户端无效，Wayland 上
唯一受支持的输入/采集通道是 xdg-desktop-portal。本层做的是把 portal 的真实
能力问清楚（版本、支持的方法、截图目标、锁屏与空闲查询），而不是照着文档
假装能用。结论见 `probe()`：

- 输入：RemoteDesktop v2 的 Notify* / ConnectToEIS 需要一个已启动的会话，
  而会话只能整屏授权，给不出“哪个窗口在前台”的作用域。
- 窗口清单：portal 没有面向 RemoteDesktop 的窗口枚举接口（只有 ScreenCast
  的录像源，语义是“共享内容”，不是“可寻址窗口”）。
- 采集：Screenshot 只有整屏 / 用户手选窗口 / 当前活动窗口三种目标，
  没有“只截被接管的那个窗口”的稳定通道。

本机批准必须绑定“设备、任务、应用窗口、动作范围、有效期”（Spec 第 50 条），
每次输入前还要复核目标窗口（Spec 第 51 条）。上面三条决定了：在只有 portal
的情况下，Wayland 无法满足窗口作用域的安全前提，因此本层不开放任何
`desktop.*` 能力，只把原因说清楚（远端据此能分辨“portal 没跑”与
“portal 跑了但作用域做不到”）。
"""
import os
import time

from .security import Denied, require

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP = "org.freedesktop.portal.RemoteDesktop"
SCREENSHOT = "org.freedesktop.portal.Screenshot"
INPUT_CAPTURE = "org.freedesktop.portal.InputCapture"
# ConnectToEIS 是两个接口共同的输入通道入口：拿到 fd 后说的其实是 `eis.py` 里的协议。
EIS_METHOD = "ConnectToEIS"
# InputCapture 的 SupportedCapabilities 位掩码：1=KEYBOARD, 2=POINTER, 4=TOUCHSCREEN。
CAP_KEYBOARD, CAP_POINTER, CAP_TOUCH = 1, 2, 4
PROPERTIES = "org.freedesktop.DBus.Properties"
LOGIN_BUS = "org.freedesktop.login1"
LOGIN_PATH = "/org/freedesktop/login1"
LOGIN_MANAGER = "org.freedesktop.login1.Manager"
LOGIN_SEAT = "org.freedesktop.login1.Seat"
# portal 自身给请求对象路径用的 token 前缀：只影响对象路径命名，不含隐私信息。
TOKEN_PREFIX = "zops"
CALL_TIMEOUT = 5.0
# Screenshot 目标位掩码（portal v3）：1=整屏, 2=用户选窗口, 4=区域, 8=活动窗口。
TARGET_SCREEN, TARGET_WINDOW, TARGET_AREA, TARGET_ACTIVE_WINDOW = 1, 2, 4, 8


def _load_jeepney():
    """按需取 D-Bus 绑定；缺失时给固定码，不让 ImportError 变成“已适配”。"""
    try:
        from jeepney import HeaderFields, MessageType, new_method_call  # noqa: F401
        from jeepney.low_level import Variant
        from jeepney.io.blocking import open_dbus_connection
        from jeepney.bus_messages import message_bus
        from jeepney.wrappers import MessageGenerator
    except ImportError:
        raise Denied("wayland_portal_binding_unavailable")
    return {"HeaderFields": HeaderFields, "MessageType": MessageType,
            "new_method_call": new_method_call, "Variant": Variant,
            "open_dbus_connection": open_dbus_connection,
            "message_bus": message_bus, "MessageGenerator": MessageGenerator}


class Portal:
    """portal 探测：所有查询失败都以固定错误码上报，不猜、不回落。"""

    def __init__(self, environ=None, connector=None, timeout=CALL_TIMEOUT):
        self.env = os.environ if environ is None else environ
        self.timeout = timeout
        self._libs = None
        self._connection = None          # session bus
        self._system = None              # system bus（logind 锁屏/空闲查询用）
        self._open = connector or self._connect
        self._desktop = None             # MessageGenerator: portal 桌面入口
        self._bus = None                 # MessageGenerator: org.freedesktop.DBus

    # ---- 连接 ----
    def _connect(self, bus):
        return self._libs["open_dbus_connection"](bus=bus)

    def prepare(self):
        """连接 session bus；没有总线地址就直接失败，绝不静默当成“可用”。

        “连不上”的映射放在调用处而不是 _connect 里：测试注入的 connector 同样
        要走同一道映射，不能因为换了开关就把 socket 异常当成另一种结果。
        """
        if self._connection is not None:
            return self
        if not str(self.env.get("DBUS_SESSION_BUS_ADDRESS") or "").strip():
            raise Denied("wayland_dbus_session_missing")
        self._libs = _load_jeepney()
        try:
            self._connection = self._open("SESSION")
        except Denied:
            raise
        except Exception:
            raise Denied("wayland_portal_bus_unreachable")
        MessageGenerator = self._libs["MessageGenerator"]
        self._desktop = MessageGenerator(object_path=PORTAL_PATH, bus_name=PORTAL_BUS)
        self._desktop.interface = PROPERTIES
        self._bus = self._libs["message_bus"]
        return self

    def _system_bus(self):
        if self._system is None:
            self._system = self._open("SYSTEM")
        return self._system

    # ---- 收发 ----
    def _call(self, connection, message):
        libs = self._libs
        try:
            reply = connection.send_and_get_reply(message, timeout=self.timeout)
        except Denied:
            raise
        except Exception:
            raise Denied("wayland_portal_probe_failed")
        if reply.header.message_type == libs["MessageType"].error:
            raise Denied("wayland_portal_probe_failed")
        return reply.body

    def _get(self, interface, name):
        """Properties.Get 返 Variant；读不到属性一律 None（不抛给调用方）。

        jeepney 0.9 把 Variant 表示成 ``(签名, 值)`` 元组，不是包装对象；
        两种形式都接，避免换了绑定版本就把“读不到”当成“值为 0”。
        """
        body = self._call(self._connection,
                          self._libs["new_method_call"](self._desktop, "Get", "ss",
                                                        (interface, name)))
        value = body[0] if body else None
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
            return value[1]
        return getattr(value, "data", value)

    def running(self):
        """portal 服务在不在：NameHasOwner 是唯一可靠判据（进程名不算）。"""
        self.prepare()
        body = self._call(self._connection,
                          self._libs["message_bus"].NameHasOwner(PORTAL_BUS))
        return bool(body and body[0])

    def _method_names(self, interface):
        try:
            value = self._get(interface, "SupportedMethods")
            if value is None:
                return []
            return [str(item) for item in value]
        except (Denied, TypeError, ValueError):
            # 读不到、类型不对、返回的不是数组：一律当“没支持”，不能当成“支持一切”。
            return []

    def probe(self):
        """如实报告 portal 能力，字段全部来自真机查询。"""
        self.prepare()
        result = {"running": False, "rdp_version": None, "rdp_methods": [],
                  "screenshot_version": None, "screenshot_targets": None,
                  "ic_version": None, "ic_methods": [], "ic_capabilities": None,
                  "window_query": False, "window_scoped_capture": False,
                  "window_scoped_input": False, "lock_query": None, "idle_query": None}
        if not self.running():
            return result
        result["running"] = True
        try:
            result["rdp_version"] = self._version(REMOTE_DESKTOP)
        except Denied:
            pass
        result["rdp_methods"] = self._method_names(REMOTE_DESKTOP)
        try:
            result["screenshot_version"] = self._version(SCREENSHOT)
        except Denied:
            pass
        try:
            targets = self._get(SCREENSHOT, "AvailableTargets")
            result["screenshot_targets"] = (int(targets) if targets is not None else None)
        except (Denied, TypeError, ValueError):
            pass
        # InputCapture：它不管“往哪注”，但它是 EIS 通道的另一个入口，也是
        # “用户正在输入”的可靠信号来源（比 X11 的屏保空闲推断强），所以照实查。
        try:
            result["ic_version"] = self._version(INPUT_CAPTURE)
        except Denied:
            pass
        result["ic_methods"] = self._method_names(INPUT_CAPTURE)
        try:
            caps = self._get(INPUT_CAPTURE, "SupportedCapabilities")
            result["ic_capabilities"] = (int(caps) if caps is not None else None)
        except (Denied, TypeError, ValueError):
            pass
        # portal 没有窗口作用域的输入/采集通道：这三项恒为 False，写死而不是“看起来能用”。
        state = self.lock_state()
        result["lock_query"] = state is not None
        result["idle_query"] = self.idle_ms() is not None
        return result

    def _version(self, interface):
        value = self._get(interface, "version")
        if value is None:
            return None
        return int(value)

    # ---- 锁屏与空闲（systemd-logind，读操作，无需提权）----
    def _seat_call(self, member, signature=None, body=()):
        libs = self._libs
        system = self._system_bus()
        manager = libs["MessageGenerator"](object_path=LOGIN_PATH, bus_name=LOGIN_BUS)
        manager.interface = LOGIN_MANAGER
        seat_path = self._call(system, libs["new_method_call"](
            manager, "GetSeat", signature or "s", (self.env.get("XDG_SEAT") or "seat0",)))[0]
        seat = libs["MessageGenerator"](object_path=str(seat_path), bus_name=LOGIN_BUS)
        seat.interface = LOGIN_SEAT
        return self._call(system, libs["new_method_call"](seat, member, signature, body))

    def _bool_hint(self, member):
        try:
            return bool(self._seat_call(member)[0])
        except Denied:
            return None
        except Exception:
            return None

    def lock_state(self):
        """True/False 为实测；None=logind 不可用或读不到（调用方须按未知处理）。"""
        self.prepare()
        return self._bool_hint("GetIdleHint")

    def idle_ms(self):
        """距上次用户输入的毫秒数；读不到返回 None（不返回 0 冒充“空闲 0 秒”）。

        logind 的 GetIdleSinceHint 是“上次活动时刻”（CLOCK_MONOTONIC 微秒），
        所以下标用本进程的单调时钟减它；两个时钟必须同一个源。
        """
        self.prepare()
        try:
            since = self._seat_call("GetIdleSinceHint")[0]
            now = time.clock_gettime(time.CLOCK_MONOTONIC) * 1000000
        except Denied:
            return None
        except Exception:
            return None
        try:
            delta = now - int(since)
        except (TypeError, ValueError):
            return None
        return max(0, int(delta)) // 1000  # 微秒 → 毫秒

    def close(self):
        for name in ("_connection", "_system"):
            connection = getattr(self, name, None)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
                setattr(self, name, None)


def block_reason(portal=None, environ=None):
    """Wayland 会话下 `desktop.*` 的统一拒绝原因：能问出多少事实就用哪个码。"""
    own = portal is None
    portal = Portal(environ=environ) if own else portal
    try:
        portal.prepare()
        if not portal.running():
            return "wayland_portal_not_running"
        probe = portal.probe()
    except Denied as error:
        return str(error) or "wayland_portal_probe_failed"
    except Exception:
        return "wayland_portal_probe_failed"
    finally:
        if own:
            portal.close()
    methods = set(probe.get("rdp_methods") or ())
    if not probe.get("running"):
        return "wayland_portal_not_running"
    if not methods and not probe.get("rdp_version"):
        return "wayland_remote_desktop_unavailable"
    # ConnectToEIS 是输入通道的入口；两个接口都查到了方法清单而都没它，才是真的没通道。
    # 没查到方法清单时不能断言“没 EIS”（只能归到下面那条能力边界）。
    ic_methods = set(probe.get("ic_methods") or ())
    if (methods or ic_methods) and EIS_METHOD not in methods and EIS_METHOD not in ic_methods:
        return "wayland_eis_channel_missing"
    # 到这里 portal 可问、EIS 通道在，但窗口作用域做不到：这是能力边界，不是没验收。
    return "wayland_window_scope_not_supported"


def require_no_desktop_capabilities(names):
    """自检：Wayland 分支绝不能声明任何桌面能力（防止将来误放开）。"""
    require(not list(names), "wayland_desktop_capabilities_not_supported")
