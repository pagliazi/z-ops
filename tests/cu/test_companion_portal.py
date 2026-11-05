"""Wayland portal 探测层测试：假连接验证接线与错误码，真 dbus-daemon 验证协议编解码。

和 x11/macos 两套测试同样的纪律：模拟只证明“接线与闸门正确”，绝不当作
“Wayland 桌面能力已验收”——本层的结论恰恰是 Wayland 上不开任何 desktop.*。
"""
import os
import asyncio
import shutil
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from companion import desktop
from companion.portal import (PORTAL_BUS, PORTAL_PATH, REMOTE_DESKTOP, SCREENSHOT,
                              INPUT_CAPTURE, Portal, block_reason)
from companion.security import Denied

try:
    from jeepney import HeaderFields, MessageType, new_error, new_method_return
    from jeepney.low_level import Variant
    from jeepney.wrappers import MessageGenerator, new_method_call
    from jeepney.io.blocking import open_dbus_connection
    HAS_JEEPNEY = True
except ImportError:
    HAS_JEEPNEY = False

ENV = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "XDG_SEAT": "seat0"}


def as_variant(value):
    """jeepney 0.9 的 Variant 就是 (签名, 值) 元组。"""
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, int):
        return ("u", value)
    if isinstance(value, list):
        return ("as", value)
    return ("s", str(value))


class Reply:
    def __init__(self, body, error=False):
        self.header = types.SimpleNamespace(
            message_type=MessageType.error if error else MessageType.method_return)
        self.body = body


class FakeConnection:
    """只替换收发：消息仍由 jeepney 真实构造，所以签名/字段名错了会在这里就炸。"""

    def __init__(self, handler):
        self.handler = handler
        self.sent = []
        self.closed = False

    def send_and_get_reply(self, message, timeout=None):
        key = (message.header.fields[HeaderFields.interface],
               message.header.fields[HeaderFields.member])
        self.sent.append(key)
        return self.handler(key, message)

    def close(self):
        self.closed = True


def portal_for(responses, environ=None, connector=None):
    def handler(key, message):
        if key not in responses:
            return Reply((), error=True)
        return responses[key]

    connection = FakeConnection(handler)
    portal = Portal(environ=ENV if environ is None else environ,
                    connector=connector or (lambda bus: connection))
    portal._libs = portal._libs or None
    return portal, connection


def portal_for_props(props, environ=None):
    """按（接口, 属性名）分派回复：需要区分 RemoteDesktop 与 InputCapture 时用这个。"""
    def handler(key, message):
        if key == NAME_OWNER:
            return Reply((True,))
        if key == PROP_GET:
            value = props.get((message.body[0], message.body[1]))
            if value is None:
                return Reply((), error=True)
            return Reply((as_variant(value),))
        return Reply((), error=True)

    connection = FakeConnection(handler)
    portal = Portal(environ=ENV if environ is None else environ,
                    connector=lambda bus: connection)
    return portal


NAME_OWNER = ("org.freedesktop.DBus", "NameHasOwner")
PROP_GET = ("org.freedesktop.DBus.Properties", "Get")


class GateTest(unittest.TestCase):
    """每一条“做不到”都必须落到一个可区分的固定码。"""

    def test_missing_bus_address_is_reported_not_guessed(self):
        self.assertEqual(block_reason(environ={"PATH": "/bin"}), "wayland_dbus_session_missing")

    def test_missing_binding_is_reported_as_unavailable(self):
        portal, _ = portal_for({})
        with mock.patch("companion.portal._load_jeepney",
                        side_effect=Denied("wayland_portal_binding_unavailable")):
            self.assertEqual(block_reason(portal=portal), "wayland_portal_binding_unavailable")

    def test_unreachable_bus_does_not_become_ready(self):
        def boom(bus):
            raise OSError("socket gone")
        portal, _ = portal_for({}, connector=boom)
        with mock.patch("companion.portal._load_jeepney",
                        side_effect=lambda: {"open_dbus_connection": boom,
                                             "MessageType": MessageType,
                                             "new_method_call": new_method_call,
                                             "Variant": Variant,
                                             "MessageGenerator": MessageGenerator,
                                             "message_bus": None,
                                             "HeaderFields": HeaderFields}):
            self.assertEqual(block_reason(portal=portal), "wayland_portal_bus_unreachable")

    def test_portal_not_running_is_distinct_from_not_accepted(self):
        portal, _ = portal_for({NAME_OWNER: Reply((False,))})
        self.assertEqual(block_reason(portal=portal), "wayland_portal_not_running")

    def test_remote_desktop_missing_is_distinct(self):
        portal, _ = portal_for({
            NAME_OWNER: Reply((True,)),
            PROP_GET: Reply((as_variant(0),)),
        })
        self.assertEqual(block_reason(portal=portal), "wayland_remote_desktop_unavailable")

    def test_window_scope_is_the_reason_when_portal_exists(self):
        portal, _ = portal_for({
            NAME_OWNER: Reply((True,)),
            PROP_GET: Reply((as_variant(2),)),
        })
        self.assertEqual(block_reason(portal=portal), "wayland_window_scope_not_supported")

    def test_no_eis_channel_is_distinct_from_no_window_scope(self):
        """portal 在、RemoteDesktop 在，但方法清单里连 ConnectToEIS 都没有：这是另一种做不到。"""
        portal = portal_for_props({
            (REMOTE_DESKTOP, "version"): 2,
            (REMOTE_DESKTOP, "SupportedMethods"): ["CreateSession", "SelectDevices", "Start"],
            (INPUT_CAPTURE, "SupportedMethods"): ["CreateSession", "Enable"],
        })
        try:
            self.assertEqual(block_reason(portal=portal), "wayland_eis_channel_missing")
        finally:
            portal.close()

    def test_eis_channel_present_stops_at_window_scope(self):
        portal = portal_for_props({
            (REMOTE_DESKTOP, "version"): 2,
            (REMOTE_DESKTOP, "SupportedMethods"): ["CreateSession", "SelectDevices",
                                                    "Start", "ConnectToEIS"],
        })
        try:
            self.assertEqual(block_reason(portal=portal), "wayland_window_scope_not_supported")
        finally:
            portal.close()

    def test_probe_reports_input_capture_facts(self):
        portal = portal_for_props({
            (INPUT_CAPTURE, "version"): 2,
            (INPUT_CAPTURE, "SupportedMethods"): ["CreateSession", "Enable", "ConnectToEIS"],
            (INPUT_CAPTURE, "SupportedCapabilities"): 3,
            (REMOTE_DESKTOP, "version"): 2,
        })
        try:
            probe = portal.probe()
        finally:
            portal.close()
        self.assertEqual(probe["ic_version"], 2)
        self.assertEqual(probe["ic_capabilities"], 3)
        self.assertIn("ConnectToEIS", probe["ic_methods"])
        # 查不到的接口一律 None/[]，不拿 0 冒充“查到了”
        self.assertIsNone(probe["screenshot_version"])

    def test_probe_never_claims_window_or_input_support(self):
        portal, _ = portal_for({
            NAME_OWNER: Reply((True,)),
            PROP_GET: Reply((as_variant(2),)),
        })
        probe = portal.probe()
        self.assertTrue(probe["running"])
        self.assertEqual(probe["rdp_version"], 2)
        self.assertFalse(probe["window_query"])
        self.assertFalse(probe["window_scoped_capture"])
        self.assertFalse(probe["window_scoped_input"])
        # logind 读不到 → None（未知），probe 里如实记 False，不当成“能查锁屏”。
        self.assertFalse(probe["lock_query"])
        self.assertFalse(probe["idle_query"])

    def test_lock_and_idle_unknown_stays_unknown(self):
        portal, _ = portal_for({NAME_OWNER: Reply((True,))})
        self.assertIsNone(portal.lock_state())
        self.assertIsNone(portal.idle_ms())

    def test_close_is_idempotent(self):
        portal, connection = portal_for({})
        portal.prepare()
        portal.close()
        portal.close()
        self.assertTrue(connection.closed)


class DesktopWiringTest(unittest.TestCase):
    """Desktop 只负责闸门：原因码来自探测层，能力一律不声明。"""

    def _prepare_wayland(self):
        cfg = types.SimpleNamespace(state_dir="/tmp/portal-wiring", secret="",
                                    device_id="device-portal", name="portal-node",
                                    server="https://example.invalid",
                                    desktop_apps=["gimp"], token="")
        desk = desktop.Desktop(cfg, platform="linux")
        with mock.patch.object(desktop, "wayland_session", lambda environ=None: True), \
                mock.patch.object(desktop, "_wayland_reason",
                                  lambda: "wayland_window_scope_not_supported"):
            asyncio.run(desk.prepare())
        return desk

    def test_wayland_session_disables_every_desktop_capability(self):
        desk = self._prepare_wayland()
        self.assertFalse(desk.enabled)
        self.assertEqual(desk.reason, "wayland_window_scope_not_supported")
        self.assertEqual(desk.capabilities, [])
        self.assertEqual(desk.driver, None)
        for capability in desktop.DESKTOP_CAPABILITIES:
            self.assertEqual(desk.unavailable[capability], "wayland_window_scope_not_supported")

    def test_broken_probe_falls_back_to_placeholder_code(self):
        with mock.patch("companion.portal.block_reason", side_effect=RuntimeError("boom")):
            self.assertEqual(desktop._wayland_reason(), desktop.WAYLAND_REASON)

    def test_non_wayland_reason_text_is_not_forwarded(self):
        # 原因码必须是 wayland_* 形式，否则会把无关文本透到远端能力表里。
        with mock.patch("companion.portal.block_reason", return_value="/etc/secret.conf"):
            self.assertEqual(desktop._wayland_reason(), desktop.WAYLAND_REASON)

    def test_placeholder_is_default_when_portal_module_absent(self):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "portal" or name.endswith(".portal"):
                raise ImportError("打包环境没有 portal 层")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", fake_import):
            self.assertEqual(desktop._wayland_reason(), desktop.WAYLAND_REASON)


@unittest.skipUnless(HAS_JEEPNEY and shutil.which("dbus-daemon"),
                     "需要 jeepney 与 dbus-daemon 才能跑真实总线")
class RealBusTest(unittest.TestCase):
    """在真 session bus 上跑一遍：证明签名、Variant、属性读取真的能编解码。"""

    def setUp(self):
        self.bus = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.address = self.bus.stdout.readline().strip()
        self.assertTrue(self.address.startswith("unix:"), "dbus-daemon 没给出地址")
        self.environ = dict(ENV, DBUS_SESSION_BUS_ADDRESS=self.address)
        # jeepney 在调用时才读 os.environ，线程与客户端都必须看到同一个地址。
        self._saved = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = self.address
        self.stop = threading.Event()
        self.errors = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._owned():
                break
            time.sleep(0.05)

    def tearDown(self):
        self.stop.set()
        self.thread.join(timeout=3)
        self.bus.terminate()
        self.bus.wait(timeout=3)
        if self._saved is None:
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        else:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = self._saved
        self.assertEqual(self.errors, [], "stub portal 线程报错")

    def _owned(self):
        from jeepney.bus_messages import message_bus
        conn = open_dbus_connection(bus="SESSION")
        try:
            owned = conn.send_and_get_reply(message_bus.NameHasOwner(PORTAL_BUS)).body[0]
        finally:
            conn.close()
        return bool(owned)

    def _serve(self):
        # 挂在实例上：用例可以改某个键，再走同一条真总线路径（例如去掉 ConnectToEIS）。
        self.values = {("version", REMOTE_DESKTOP): 2,
                       ("SupportedMethods", REMOTE_DESKTOP): ["CreateSession", "SelectDevices",
                                                               "Start", "ConnectToEIS"],
                       ("version", SCREENSHOT): 3,
                       ("AvailableTargets", SCREENSHOT): 11,
                       ("version", INPUT_CAPTURE): 2,
                       ("SupportedMethods", INPUT_CAPTURE): ["CreateSession", "Enable",
                                                              "ConnectToEIS", "GetZones"],
                       ("SupportedCapabilities", INPUT_CAPTURE): 3}
        values = self.values
        try:
            conn = open_dbus_connection(bus="SESSION")
            from jeepney.bus_messages import message_bus
            conn.send_and_get_reply(message_bus.RequestName(PORTAL_BUS))
            while not self.stop.is_set():
                try:
                    msg = conn.receive(timeout=0.2)
                except TimeoutError:
                    continue
                if msg.header.message_type != MessageType.method_call:
                    continue
                if (msg.header.fields[HeaderFields.interface],
                        msg.header.fields[HeaderFields.member]) != (
                        "org.freedesktop.DBus.Properties", "Get"):
                    conn.send(new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod",
                                        "s", ("no such method",)))
                    continue
                iface, name = msg.body
                value = values.get((name, iface))
                if value is None:
                    conn.send(new_error(msg, "org.freedesktop.DBus.Error.UnknownProperty",
                                        "s", ("no such property",)))
                else:
                    conn.send(new_method_return(msg, "v", (as_variant(value),)))
            conn.close()
        except Exception as error:  # pragma: no cover
            self.errors.append(repr(error))

    def _portal(self):
        def connector(bus):
            if bus != "SESSION":
                raise RuntimeError("测试环境没有 system bus")
            return open_dbus_connection(bus="SESSION")

        return Portal(environ=self.environ, connector=connector)

    def test_probe_over_real_bus(self):
        portal = self._portal()
        try:
            self.assertTrue(portal.running())
            probe = portal.probe()
        finally:
            portal.close()
        self.assertEqual(probe["rdp_version"], 2)
        self.assertEqual(probe["rdp_methods"],
                         ["CreateSession", "SelectDevices", "Start", "ConnectToEIS"])
        self.assertEqual(probe["screenshot_version"], 3)
        self.assertEqual(probe["screenshot_targets"], 11)
        self.assertEqual(probe["ic_version"], 2)
        self.assertEqual(probe["ic_capabilities"], 3)
        self.assertIn("ConnectToEIS", probe["ic_methods"])
        self.assertFalse(probe["window_query"])
        self.assertFalse(probe["lock_query"])
        self.assertEqual(block_reason(portal=portal), "wayland_window_scope_not_supported")

    def test_missing_eis_channel_reports_distinct_reason_over_real_bus(self):
        """portal 跑了、两个接口都没有 ConnectToEIS：这是“没有输入通道”，
        与“有通道但拿不到窗口作用域”必须是两个码，远端才分得清该升级哪一侧。"""
        self.values[("SupportedMethods", REMOTE_DESKTOP)] = ["CreateSession", "SelectDevices",
                                                             "Start"]
        self.values[("SupportedMethods", INPUT_CAPTURE)] = ["CreateSession", "Enable",
                                                            "GetZones"]
        portal = self._portal()
        try:
            reason = block_reason(portal=portal)
        finally:
            portal.close()
        self.assertEqual(reason, "wayland_eis_channel_missing")

    def test_unknown_property_is_not_treated_as_value(self):
        portal = self._portal()
        try:
            portal.prepare()
            with self.assertRaises(Denied) as caught:
                portal._get(REMOTE_DESKTOP, "NoSuchProperty")
            self.assertEqual(str(caught.exception), "wayland_portal_probe_failed")
        finally:
            portal.close()


if __name__ == "__main__":
    unittest.main()
