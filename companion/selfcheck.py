"""离线自检：在没有真合成器/真 portal 的机器上，把「线格式 + 闸门」跑一遍。

为什么需要这一层：`companion/eis.py` 的正确性只能靠按 libei 规范独立实现的假合成器
来证明，而冻结包必须能在交付机器上自证「包内那份 EIS 客户端真的能跑」——不能只靠
仓库里跑得通的单测（交付时仓库不在包内）。

边界（务必如实理解，别当成 Wayland 已支持）：
- 这里的对端是**本机进程内**的假合成器，跑在 `socketpair` 上；没有真实合成器参与，
  因此结论不是「Wayland 桌面可用」，只是「协议层与注入闸门可用」。
- 自检不连接任何 D-Bus、不接管任何窗口、不声明任何 `desktop.*` 能力。
- `EIS.supports_window_scope` 必须恒为 False：EI 协议里只有屏幕 `region`，没有窗口
  对象，窗口作用域拿不到（Spec 第 50/51 条要求批准绑定具体窗口），所以桌面能力
  在 Wayland 下依旧不开放。
"""
import socket
import struct
import threading

from . import eis
from .eis import EIS, encode_message, encode_string

HEADER = struct.Struct("@QII")
U32 = struct.Struct("<I")
U64 = struct.Struct("<Q")
PEER_ID_FLOOR = 0xff00000000000000
# 假合成器公布的掩码故意非常规（0x40/0x1/0x8）：客户端只能原样 OR，不得硬编码。
CAPS = {"ei_keyboard": 0x40, "ei_pointer": 0x1, "ei_button": 0x8}
BIND_MASK = 0x49
DEVICE_INTERFACES = ("ei_keyboard", "ei_pointer", "ei_button")
# 自检的全部步骤名（CLI 用它在步数不足时报非零，不把“只跑了三步”当成通过）。
STEPS = ("handshake", "seat", "device", "gate_not_emulating", "inject",
         "window_scope_false", "gate_paused", "absolute_needs_interface_and_region")


class Peer:
    """按规范独立实现的假 EIS 实现（合成器侧）：自己拼包，也自己拆包。"""

    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()
        self.next_id = PEER_ID_FLOOR
        self.messages = []

    def new_id(self):
        self.next_id += 1
        return self.next_id

    def send(self, object_id, opcode, kinds=(), values=(), fds=()):
        body = b""
        for kind, value in zip(kinds, values):
            if kind == "s":
                body += encode_string(value)
            elif kind == "fd":
                continue
            elif kind in ("u32", "i32", "f"):
                body += U32.pack(value if kind != "f" else int(value))
            else:
                body += U64.pack(value)
        self.sock.sendall(HEADER.pack(object_id, HEADER.size + len(body), opcode) + body)

    def _fill(self, size):
        while len(self.buffer) < size:
            try:
                data = self.sock.recv(4096)
            except OSError as error:  # pragma: no cover
                raise AssertionError("读客户端消息失败: %r" % (error,))
            if not data:
                raise AssertionError("客户端已断开")
            self.buffer += data

    def recv(self):
        self._fill(HEADER.size)
        object_id, length, opcode = HEADER.unpack_from(self.buffer)
        self._fill(length)
        payload = bytes(self.buffer[HEADER.size:length])
        del self.buffer[:length]
        self.messages.append((object_id, opcode, payload))
        return object_id, opcode, payload

    def expect(self, object_id, opcode, body=b""):
        got_id, got_opcode, got_body = self.recv()
        if (got_id, got_opcode, got_body) != (object_id, opcode, body):
            raise AssertionError("消息不符 %r != %r（已收到 %d 条）"
                                 % ((got_id, got_opcode, got_body), (object_id, opcode, body),
                                    len(self.messages)))


def _read_interface(payload):
    """解 `interface_version(s,u32)` / `interface(new_id,s,u32)` 里的字符串。"""
    size = U32.unpack_from(payload, 0)[0]
    name = payload[4:4 + size - 1].decode("utf-8")
    aligned = 4 + ((size + 3) // 4 * 4)
    return name, U32.unpack_from(payload, aligned)[0]


def run(steps=None):
    """跑完整链路，返回 {"passed": [...], "failed": [...]}；任何一步出错都不算通过。

    `steps` 为步骤名白名单（回归用：只跑到某一步以验证后续闸门会真的报错）。
    """
    passed, failed = [], []

    def check(name, fn):
        try:
            fn()
            passed.append(name)
            return True
        except Exception as error:
            failed.append("%s: %s" % (name, error if str(error) else repr(error)))
            return False

    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    left.settimeout(5.0)
    right.settimeout(5.0)
    peer = Peer(right)
    client = EIS(sock=left, timeout=5.0, name="zops-selfcheck")

    def do_handshake():
        peer.send(0, 0, ("u32",), (eis.PROTOCOL_VERSION,))
        thread = threading.Thread(target=client.handshake, daemon=True)
        thread.start()
        peer.expect(0, 0, U32.pack(eis.PROTOCOL_VERSION))
        peer.expect(0, 2, U32.pack(eis.CONTEXT_SENDER))
        _id, opcode, payload = peer.recv()
        if (opcode, payload) != (3, encode_string("zops-selfcheck")):
            raise AssertionError("name 编码不符")
        announced = {}
        while True:
            _id, opcode, payload = peer.recv()
            if opcode == 1:
                if payload:
                    raise AssertionError("finish 不应带参数")
                break
            name, version = _read_interface(payload)
            announced[name] = version
        if announced != eis.CLIENT_INTERFACES:
            raise AssertionError("申报接口表不符: %s" % (announced,))
        connection = peer.new_id()
        peer.send(0, 2, ("u32", "new_id", "u32"), (7, connection, eis.PROTOCOL_VERSION))
        thread.join(5)
        if client.connection != connection:
            raise AssertionError("握手未取得 connection")
        return connection

    connection = None

    def capture_connection():
        nonlocal connection
        connection = do_handshake()

    def do_seat():
        seat = peer.new_id()
        peer.send(connection, 1, ("new_id", "u32"), (seat, 2))
        peer.send(seat, 1, ("s",), ("seat0",))
        for iface, mask in CAPS.items():
            peer.send(seat, 2, ("u64", "s"), (mask, iface))
        peer.send(seat, 3)
        if client.wait_seat() != CAPS:
            raise AssertionError("能力掩码未按公布值记录")
        client.bind(interfaces=DEVICE_INTERFACES)
        peer.expect(seat, 1, U64.pack(BIND_MASK))
        return seat

    seat = None

    def capture_seat():
        nonlocal seat
        seat = do_seat()

    def do_device():
        device = peer.new_id()
        peer.send(seat, 4, ("new_id", "u32"), (device, 3))
        peer.send(device, 1, ("s",), ("Virtual keyboard",))
        peer.send(device, 2, ("u32",), (eis.DEVICE_VIRTUAL,))
        subs = {}
        for iface in DEVICE_INTERFACES:
            subs[iface] = peer.new_id()
            peer.send(device, 5, ("new_id", "s", "u32"), (subs[iface], iface, 1))
        peer.send(device, 6)
        client.wait_devices(needed=DEVICE_INTERFACES)
        peer.expect(device, 4)                       # v3：收到 done 必须回 ready
        peer.send(device, 7, ("u32",), (11,))       # resumed
        client.wait_resumed()
        return device, subs

    device = subs = None

    def capture_device():
        nonlocal device, subs
        device, subs = do_device()

    def gate_refuses_before_start():
        try:
            client.key(30)
        except eis.Denied as error:
            if str(error) != "eis_not_emulating":
                raise AssertionError("闸门错误码不符: %s" % (error,))
            return
        raise AssertionError("未 start_emulating 也能注入")

    def do_inject():
        client.start_emulating()
        peer.expect(device, 1, U32.pack(11) + U32.pack(1))
        client.key(30, True)
        peer.expect(subs["ei_keyboard"], 1, U32.pack(30) + U32.pack(eis.KEY_PRESSED))
        client.button(eis.BUTTON_LEFT, True)
        peer.expect(subs["ei_button"], 1,
                    U32.pack(eis.BUTTON_LEFT) + U32.pack(eis.BUTTON_PRESSED))
        client.motion(4.0, -2.0)
        peer.expect(subs["ei_pointer"], 1,
                    struct.Struct("<f").pack(4.0) + struct.Struct("<f").pack(-2.0))
        stamp = client.frame()
        object_id, opcode, payload = peer.recv()
        if (object_id, opcode, U32.unpack_from(payload, 0)[0]) != (device, 3, 11):
            raise AssertionError("frame 必须回填最近 serial")
        if stamp <= 0:
            raise AssertionError("frame 时间戳必须是单调时钟微秒")

    def window_scope_stays_false():
        state = client.state()
        if state["window_scoped"] or EIS.supports_window_scope:
            raise AssertionError("EIS 不得声称支持窗口作用域")
        if any("window" in name for name in eis.SCHEMA):
            raise AssertionError("协议表里不应出现窗口对象")

    def paused_stops_injection():
        peer.send(device, 8, ("u32",), (12,))        # ei_device.paused
        client.pump()
        try:
            client.key(30)
        except eis.Denied as error:
            if str(error) != "eis_device_paused":
                raise AssertionError("paused 后错误码不符: %s" % (error,))
            return
        raise AssertionError("paused 之后仍能注入")

    def absolute_needs_interface_and_region():
        """绝对指针必须同时有 `ei_pointer_absolute` 接口与已知 region：这里两个条件
        都不满足（设备只有相对指针、也没发 region），客户端必须拒绝而不是默默发包。"""
        try:
            client.motion_absolute(100, 100)
        except eis.Denied as error:
            if str(error) not in ("eis_no_device", "eis_region_unknown", "eis_not_emulating"):
                raise AssertionError("绝对指针缺前置条件时必须拒绝: %s" % (error,))
            return
        raise AssertionError("缺接口/region 时绝对指针不应发出")

    plan = [
        ("handshake", capture_connection),
        ("seat", capture_seat),
        ("device", capture_device),
        ("gate_not_emulating", gate_refuses_before_start),
        ("inject", do_inject),
        ("window_scope_false", window_scope_stays_false),
        ("gate_paused", paused_stops_injection),
        ("absolute_needs_interface_and_region", absolute_needs_interface_and_region),
    ]
    try:
        for name, fn in plan:
            if steps is not None and name not in steps:
                continue
            # 前三步是后面所有步骤的前提：任一步挂了就不必再假跑一串无关的失败。
            if not check(name, fn):
                if name in ("handshake", "seat", "device"):
                    break
    finally:
        client.close()
        for sock in (left, right):
            try:
                sock.close()
            except OSError:
                pass
    return {"passed": passed, "failed": failed,
            "messages": len(peer.messages), "window_scoped": False,
            "note": "离线自检：假合成器跑在本机 socketpair 上，不构成 Wayland 桌面验收"}
