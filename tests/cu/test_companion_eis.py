"""EIS（libei）协议层测试：对端是按规范独立实现的假合成器，不复用本模块的编码器。

纪律与 x11/macos/portal 三套一致：这里证明的是“线路格式与闸门正确”，
不等于“Wayland 桌面已验收”——本机没有合成器，结论仍然必须是桌面能力不声明。

假合成器（`Peer`）自己用 struct 拼包拆包，所以客户端只要把 opcode、参数顺序、
字节序或字符串补零搞错，都会在真 socketpair 上立刻对不上。
"""
import os
import socket
import struct
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from companion import eis
from companion.eis import EIS, encode_message, encode_string, decode_string
from companion.security import Denied

U32 = struct.Struct("<I")
I32 = struct.Struct("<i")
F32 = struct.Struct("<f")
U64 = struct.Struct("<Q")
HEADER = struct.Struct("<QII")
PEER_ID_FLOOR = 0xff00000000000000
ALL_CAPS = {"ei_keyboard": 0x40, "ei_pointer": 0x1, "ei_button": 0x8}


def wire_string(value):
    """按 types.md 独立实现：长度含 NUL，再补零到 4 的倍数。"""
    if value is None:
        return U32.pack(0)
    raw = value.encode("utf-8") + b"\x00"
    return U32.pack(len(raw)) + raw + b"\x00" * ((4 - len(raw) % 4) % 4)


def pack_fds(fds):
    """自己拼 SCM_RIGHTS：本机解释器没有 socket.pack_fds/unpack_fds。"""
    return struct.pack("@" + "i" * len(fds), *fds)


def unpack_fds(ancdata):
    fds = []
    for level, kind, data in ancdata or ():
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.extend(struct.unpack("@" + "i" * (len(data) // 4), data))
    return fds


def decode_args(payload, kinds):
    args, offset = [], 0
    for kind in kinds:
        if kind == "s":
            length = U32.unpack_from(payload, offset)[0]
            offset += 4
            raw = payload[offset:offset + length]
            args.append(raw[:-1].decode("utf-8") if length else "")
            offset += length + ((4 - length % 4) % 4)
        elif kind == "fd":
            args.append(None)                     # 带外传递，不占 payload
        elif kind == "u32":
            args.append(U32.unpack_from(payload, offset)[0])
            offset += 4
        elif kind == "i32":
            args.append(I32.unpack_from(payload, offset)[0])
            offset += 4
        elif kind == "f":
            args.append(F32.unpack_from(payload, offset)[0])
            offset += 4
        elif kind in ("u64", "new_id", "object_id"):
            args.append(U64.unpack_from(payload, offset)[0])
            offset += 8
        else:
            raise AssertionError("未知类型 " + kind)
    return args


class Peer:
    """假 EIS 实现（合成器侧）：自己拼包，并逐条核对客户端请求。"""

    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()
        self.fds = []
        self.next_id = PEER_ID_FLOOR
        self.requests = []

    def new_id(self):
        self.next_id += 1
        return self.next_id

    def send(self, object_id, opcode, kinds, values, fds=()):
        body = b""
        for kind, value in zip(kinds, values):
            if kind == "s":
                body += wire_string(value)
            elif kind == "fd":
                continue
            elif kind == "u32":
                body += U32.pack(value)
            elif kind == "i32":
                body += I32.pack(value)
            elif kind == "f":
                body += F32.pack(float(value))
            else:
                body += U64.pack(value)
        message = HEADER.pack(object_id, HEADER.size + len(body), opcode) + body
        if fds:
            # 同样只用 sendmsg：本机解释器没有 send_fds/recv_fds，也没有 pack_fds。
            self.sock.sendmsg([message], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                           pack_fds(list(fds)))])
        else:
            self.sock.sendall(message)

    def _fill(self, size):
        while len(self.buffer) < size:
            try:
                data, ancdata, _flags, _addr = self.sock.recvmsg(4096, socket.CMSG_SPACE(64))
            except socket.timeout:
                raise AssertionError("等客户端请求超时")
            if not data and not ancdata:
                raise AssertionError("客户端已断开")
            self.fds.extend(unpack_fds(ancdata))
            self.buffer += data

    def recv_raw(self):
        self._fill(HEADER.size)
        object_id, length, opcode = HEADER.unpack_from(self.buffer)
        self._fill(length)
        payload = self.buffer[HEADER.size:length]
        del self.buffer[:length]
        self.requests.append((object_id, opcode))
        return object_id, opcode, payload

    def recv(self, kinds):
        object_id, opcode, payload = self.recv_raw()
        return object_id, opcode, decode_args(payload, kinds)

    def expect(self, object_id, opcode, kinds, values):
        got_id, got_opcode, got_values = self.recv(kinds)
        if (got_id, got_opcode) != (object_id, opcode):
            raise AssertionError("请求不符：%s != %s（已收到 %s）"
                                 % ((got_id, got_opcode), (object_id, opcode), self.requests))
        if tuple(got_values) != tuple(values):
            raise AssertionError("参数不符：%s != %s" % (got_values, values))


class EISTest(unittest.TestCase):
    """每条用例都跑在真 socketpair 上，握手/绑定/注入逐字节核对。"""

    def setUp(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.peer = Peer(right)
        self.peer.sock.settimeout(3.0)
        self.client = EIS(sock=left, timeout=3.0, name="zops-test")
        self.errors = []
        self.addCleanup(self.client.close)
        self.addCleanup(right.close)

    def call_in_thread(self, fn, *args):
        def run():
            try:
                self.results.append(fn(*args))
            except Denied as error:
                self.errors.append(str(error))
            except Exception as error:      # pragma: no cover
                self.errors.append(repr(error))
        self.results = []
        thread = threading.Thread(target=run)
        thread.start()
        return thread

    def handshake(self):
        """客户端在后台发握手请求，假合成器逐条核对后回 connection 事件。"""
        self.peer.send(0, 0, ("u32",), (1,))                 # ei_handshake.handshake_version
        connection_id = self.peer.new_id()
        thread = self.call_in_thread(self.client.handshake)
        self.peer.expect(0, 0, ("u32",), (eis.PROTOCOL_VERSION,))
        self.peer.expect(0, 2, ("u32",), (eis.CONTEXT_SENDER,))
        _id, opcode, values = self.peer.recv(("s",))
        self.assertEqual((opcode, values), (3, ["zops-test"]))
        announced = {}
        while True:
            _id, opcode, payload = self.peer.recv_raw()
            if opcode == 4:
                name, version = decode_args(payload, ("s", "u32"))
                announced[name] = version
                continue
            self.assertEqual(opcode, 1, "interface_version 之后应当是 finish")
            self.assertEqual(payload, b"", "finish 不带参数")
            break
        self.assertEqual(announced, eis.CLIENT_INTERFACES)
        self.peer.send(0, 2, ("u32", "u64", "u32"), (7, connection_id, 1))
        thread.join(5)
        self.assertEqual(self.errors, [])
        self.assertEqual(self.client.connection, connection_id)
        return connection_id

    def open_seat(self, connection_id, capabilities=ALL_CAPS, seat_version=2):
        seat_id = self.peer.new_id()
        self.peer.send(connection_id, 1, ("u64", "u32"), (seat_id, seat_version))
        self.peer.send(seat_id, 1, ("s",), ("seat0",))
        for iface, mask in capabilities.items():
            self.peer.send(seat_id, 2, ("u64", "s"), (mask, iface))
        self.peer.send(seat_id, 3, (), ())                    # ei_seat.done
        self.assertEqual(self.client.wait_seat(), dict(capabilities))
        return seat_id

    def add_device(self, seat_id, interfaces, version=3, region=None, resume=True):
        """只发设备事件；`ready` 客户端要到 wait_devices 才会读出来，所以不在这里等。"""
        device_id = self.peer.new_id()
        self.peer.send(seat_id, 4, ("u64", "u32"), (device_id, version))
        self.peer.send(device_id, 1, ("s",), ("Virtual keyboard",))
        self.peer.send(device_id, 2, ("u32",), (eis.DEVICE_VIRTUAL,))
        if region:
            self.peer.send(device_id, 4, ("u32", "u32", "u32", "u32", "f"), region)
        subs = {}
        for iface in interfaces:
            subs[iface] = self.peer.new_id()
            self.peer.send(device_id, 5, ("u64", "s", "u32"), (subs[iface], iface, 1))
        self.peer.send(device_id, 6, (), ())                  # ei_device.done
        if resume:
            self.peer.send(device_id, 7, ("u32",), (11,))     # ei_device.resumed
        return device_id, subs

    def ready_session(self, interfaces=("ei_keyboard", "ei_pointer", "ei_button"),
                      region=None):
        """握手 + 绑上 + 一个已 resumed 的设备，返回 (device_id, 子对象 id 表)。"""
        connection_id = self.handshake()
        seat_id = self.open_seat(connection_id)
        self.client.bind(interfaces=tuple(interfaces))
        self.peer.expect(seat_id, 1, ("u64",),
                         (sum(ALL_CAPS[iface] for iface in interfaces),))
        device_id, subs = self.add_device(seat_id, interfaces, region=region)
        self.client.wait_devices(needed=tuple(interfaces))
        self.client.wait_resumed()
        self.peer.expect(device_id, 4, (), [])                # v3 必须响应 done 回 ready
        return device_id, subs

    # ---- 会话建立 ----
    def test_handshake_announces_every_supported_interface(self):
        self.handshake()
        self.assertEqual(self.client._objects[self.client.connection], ("ei_connection", 1))

    def test_bind_uses_announced_masks_not_hardcoded_values(self):
        """掩码由合成器公布：这里故意给一个非常规值，客户端必须原样 OR 回去。"""
        caps = {"ei_keyboard": 0x400, "ei_pointer": 0x2, "ei_button": 0x20}
        connection_id = self.handshake()
        seat_id = self.open_seat(connection_id, caps)
        self.client.bind(interfaces=("ei_keyboard", "ei_pointer"))
        self.peer.expect(seat_id, 1, ("u64",), (0x402,))

    def test_bind_refuses_unadvertised_interface(self):
        connection_id = self.handshake()
        self.open_seat(connection_id, {"ei_keyboard": 0x40})
        with self.assertRaises(Denied) as caught:
            self.client.bind(interfaces=("ei_keyboard", "ei_pointer"))
        self.assertEqual(str(caught.exception), "eis_capability_unavailable")

    def test_missing_seat_fails_closed(self):
        self.handshake()
        with self.assertRaises(Denied) as caught:
            self.client.bind()
        self.assertEqual(str(caught.exception), "eis_no_seat")

    # ---- 注入闸门：这是安全模型要求，不是可选项 ----
    def test_injection_before_resume_is_refused(self):
        connection_id = self.handshake()
        seat_id = self.open_seat(connection_id)
        self.client.bind()
        self.peer.recv(("u64",))
        device_id, _subs = self.add_device(seat_id, ("ei_keyboard", "ei_pointer", "ei_button"),
                                           resume=False)
        self.client.wait_devices()
        self.peer.expect(device_id, 4, (), [])
        with self.assertRaises(Denied) as caught:
            self.client.start_emulating()
        self.assertEqual(str(caught.exception), "eis_device_paused")

    def test_key_before_start_emulating_is_refused(self):
        self.ready_session()
        with self.assertRaises(Denied) as caught:
            self.client.key(30)
        self.assertEqual(str(caught.exception), "eis_not_emulating")

    def test_paused_mid_session_stops_injection(self):
        device_id, subs = self.ready_session()
        self.client.start_emulating()
        self.peer.expect(device_id, 1, ("u32", "u32"), (11, 1))
        self.peer.send(device_id, 8, ("u32",), (12,))          # ei_device.paused
        self.client.pump()
        with self.assertRaises(Denied) as caught:
            self.client.key(30)
        self.assertEqual(str(caught.exception), "eis_device_paused")
        self.assertFalse(self.client.devices[device_id]["resumed"])

    def test_disconnected_marks_client_dead(self):
        connection_id = self.handshake()
        self.peer.send(connection_id, 0, ("u32", "u32", "s"), (9, 0, "bye"))
        self.client.pump()
        self.assertEqual(self.client.dead, "eis_connection_closed")
        with self.assertRaises(Denied) as caught:
            self.client.key(30)
        self.assertEqual(str(caught.exception), "eis_connection_closed")

    def test_unknown_object_is_a_violation(self):
        self.handshake()
        self.peer.send(self.peer.new_id(), 0, ("u32",), (1,))
        with self.assertRaises(Denied) as caught:
            self.client.pump()
        self.assertEqual(str(caught.exception), "eis_unknown_object")

    # ---- 线路格式：与规范逐字节对齐 ----
    def test_key_button_motion_frame_on_the_wire(self):
        device_id, subs = self.ready_session()
        self.client.start_emulating()
        self.peer.expect(device_id, 1, ("u32", "u32"), (11, 1))
        self.client.key(30, True)
        self.peer.expect(subs["ei_keyboard"], 1, ("u32", "u32"), (30, eis.KEY_PRESSED))
        self.client.button(272, True)
        self.peer.expect(subs["ei_button"], 1, ("u32", "u32"), (272, eis.BUTTON_PRESSED))
        self.client.motion(12, -7)
        self.peer.expect(subs["ei_pointer"], 1, ("f", "f"), (12.0, -7.0))
        stamp = self.client.frame()
        object_id, opcode, values = self.peer.recv(("u32", "u64"))
        self.assertEqual((object_id, opcode), (device_id, 3))
        self.assertEqual(values[0], 11, "last_serial 必须是最近一次事件的 serial")
        self.assertGreater(stamp, 0)
        self.assertGreaterEqual(values[1], stamp - 1000)

    def test_frame_is_per_device(self):
        """键盘与指针分属两个设备时，一个 frame 不够——必须每设备各一个。"""
        connection_id = self.handshake()
        seat_id = self.open_seat(connection_id)
        self.client.bind(interfaces=("ei_keyboard",))
        self.peer.expect(seat_id, 1, ("u64",), (0x40,))
        keyboard, kb_subs = self.add_device(seat_id, ("ei_keyboard",))
        pointer, pt_subs = self.add_device(seat_id, ("ei_pointer", "ei_button"))
        self.client.wait_devices(needed=("ei_keyboard",))
        self.client.wait_resumed(needed=2)
        self.peer.expect(keyboard, 4, (), [])
        self.peer.expect(pointer, 4, (), [])
        self.client.start_emulating()
        self.peer.expect(keyboard, 1, ("u32", "u32"), (11, 1))
        self.peer.expect(pointer, 1, ("u32", "u32"), (11, 1))
        self.client.key(30)
        self.client.motion(1, 2)
        self.peer.expect(kb_subs["ei_keyboard"], 1, ("u32", "u32"), (30, eis.KEY_PRESSED))
        self.peer.expect(pt_subs["ei_pointer"], 1, ("f", "f"), (1.0, 2.0))
        self.client.frame()
        self.peer.expect(keyboard, 3, ("u32", "u64"), (11, mock.ANY))
        self.peer.expect(pointer, 3, ("u32", "u64"), (11, mock.ANY))

    def test_release_state_uses_zero(self):
        device_id, subs = self.ready_session()
        self.client.start_emulating()
        self.peer.recv(("u32", "u32"))
        self.client.key(30, pressed=False)
        self.peer.expect(subs["ei_keyboard"], 1, ("u32", "u32"), (30, eis.KEY_RELEASED))

    def test_absolute_motion_requires_known_region(self):
        caps = dict(ALL_CAPS, **{"ei_pointer_absolute": 0x100})
        connection_id = self.handshake()
        seat_id = self.open_seat(connection_id, caps)
        self.client.bind(interfaces=("ei_keyboard", "ei_pointer_absolute"))
        self.peer.recv(("u64",))
        device_id, subs = self.add_device(seat_id, ("ei_keyboard", "ei_pointer_absolute"))
        self.client.wait_devices(needed=("ei_keyboard", "ei_pointer_absolute"))
        self.client.wait_resumed()
        self.peer.expect(device_id, 4, (), [])
        self.client.start_emulating()
        self.peer.expect(device_id, 1, ("u32", "u32"), (11, 1))
        with self.assertRaises(Denied) as caught:
            self.client.motion_absolute(10, 10)
        self.assertEqual(str(caught.exception), "eis_region_unknown")
        self.peer.send(device_id, 4, ("u32", "u32", "u32", "u32", "f"), (0, 0, 800, 600, 1.0))
        self.client.pump()
        self.client.motion_absolute(799, 599)
        self.peer.expect(subs["ei_pointer_absolute"], 1, ("f", "f"), (799.0, 599.0))
        with self.assertRaises(Denied) as caught:
            self.client.motion_absolute(1600, 10)
        self.assertEqual(str(caught.exception), "eis_out_of_region")

    def test_keymap_fd_is_closed(self):
        """keymap 的 fd 用不上也必须关掉，否则每收一次就漏一个 fd。"""
        _device_id, subs = self.ready_session()
        read_fd, write_fd = os.pipe()
        try:
            self.peer.send(subs["ei_keyboard"], 1, ("u32", "u32", "fd"),
                           (eis.KEYMAP_XKB, 4, None), fds=[read_fd])
            closed = []
            with mock.patch("companion.eis.os.close", side_effect=lambda fd: closed.append(fd)):
                self.client.pump()
            self.assertEqual(len(closed), 1, "收到的 keymap fd 必须被关掉")
            self.assertNotIn(read_fd, closed, "收到的是新 fd，不是本端那个原句柄")
            self.assertEqual(self.client._fds, [])
        finally:
            os.close(read_fd)
            os.close(write_fd)

    # ---- 编码与结论 ----
    def test_string_golden_bytes(self):
        self.assertEqual(encode_string(None), b"\x00\x00\x00\x00")
        self.assertEqual(encode_string(""), b"\x01\x00\x00\x00\x00\x00\x00\x00")
        # types.md 的例子："hello" 长度 6，补零到 8，加 4 字节长度字段共 12 字节。
        self.assertEqual(encode_string("hello"), b"\x06\x00\x00\x00" + b"hello\x00\x00\x00")
        self.assertEqual(decode_string(encode_string("hi"), 0), ("hi", 8))

    def test_message_header_layout(self):
        message = encode_message(1, 2, ("u32",), (5,))
        self.assertEqual(len(message), 20)
        object_id, length, opcode = struct.unpack("@QII", message[:16])
        self.assertEqual((object_id, length, opcode), (1, 20, 2))
        self.assertEqual(struct.unpack("@I", message[16:])[0], 5)

    def test_arity_and_size_guards(self):
        with self.assertRaises(Denied):
            encode_message(1, 0, ("u32",), ())
        with self.assertRaises(Denied) as caught:
            encode_message(1, 0, ("s",), ("x" * (eis.MAX_MESSAGE + 1),))
        self.assertEqual(str(caught.exception), "eis_message_too_large")

    def test_decode_rejects_truncated_string(self):
        with self.assertRaises(Denied):
            decode_string(b"\x08\x00\x00\x00ab", 0)

    def test_protocol_has_no_window_object(self):
        """“窗口作用域拿不到”是协议事实：EIS 里只有屏幕 region，没有窗口对象。"""
        self.assertFalse([name for name in eis.SCHEMA if "window" in name])
        self.assertFalse(EIS.supports_window_scope)

    def test_state_reports_facts_only(self):
        device_id, _subs = self.ready_session()
        state = self.client.state()
        self.assertTrue(state["connected"])
        self.assertEqual(state["seat"], "seat0")
        self.assertEqual(state["capabilities"], ALL_CAPS)
        self.assertEqual(state["emulating"], False)
        self.assertEqual(state["window_scoped"], False)
        self.assertEqual(len(state["devices"]), 1)
        self.assertTrue(state["devices"][0]["resumed"])
        self.assertEqual(state["devices"][0]["interfaces"],
                         ["ei_button", "ei_keyboard", "ei_pointer"])


if __name__ == "__main__":
    unittest.main()
