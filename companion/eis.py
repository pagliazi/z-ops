"""EIS（Emulated Input）客户端：libei 二进制协议的纯标准库实现。

为什么要有这一层：Wayland 上 X11 的 XTest 注入对原生客户端无效，portal 只负责
“什么时候允许谁拿输入”，真正把按键/指针事件送到合成器的通道是 EI 协议
（`ConnectToEIS()` 返回一个 socket fd，之后所有输入都走它）。没有这一层，
Wayland 适配连“能不能做”都无从判断。

协议事实（取自 libei `proto/protocol.xml` 与 `doc/protocol/*`，不是凭印象）：
- 线路格式：16 字节头 = uint64 object_id + uint32 length（含头）+ uint32 opcode；
  参数按 4 字节对齐，字符串是“长度（含结尾 NUL）+ 内容 + 补零”，`fd` 在消息里
  占 0 字节、靠 SCM_RIGHTS 带外传递；整数用 EIS 实现（本机即合成器）的原生字节序。
- 能力掩码由合成器在 `ei_seat.capability(mask, interface)` 里逐个公布，**不是固定值**，
  所以本层绝不硬编码 “键盘 = 0x4”，只 OR 实际公布回来的掩码。
- 新公布的设备处于 paused 态；未 `ei_device.resumed` 就发输入是 client bug。
- `ei_device.frame()` 才是硬件事务边界：没 frame 的事件合成器有权不处理。
- 设备只有 `region`（屏幕区域偏移/尺寸/缩放），**协议里没有窗口对象**——所以窗口
  作用域在这里依然拿不到，本层如实把 `supports_window_scope` 写死为 False。

安全纪律（与 `x11.py`/`ax.py`/`portal.py` 一致）：连不上、握手不过、设备被暂停、
没进 emulating、区域未知，一律以固定错误码 `Denied` 上抛，由 `desktop.py` 决定
禁用哪些能力；绝不把“没问到”当成“可以做到”。
"""
import os
import socket
import struct
import sys
import time

from .security import Denied, require

PROTOCOL_VERSION = 1                 # ei_handshake 层版本
CONTEXT_RECEIVER, CONTEXT_SENDER = 1, 2      # receiver=收用户输入, sender=注入
DEVICE_VIRTUAL, DEVICE_PHYSICAL = 1, 2
KEY_RELEASED, KEY_PRESSED = 0, 1
BUTTON_RELEASED, BUTTON_PRESSED = 0, 1
BUTTON_LEFT = 272                    # Linux input 事件码（BTN_LEFT），与 X11/BT 编码一致
KEYMAP_XKB = 1                       # ei_keyboard.keymap_type 里目前唯一定义的值
MAX_MESSAGE = 65536                  # 超过就当协议错乱：不给对端拖死本进程的机会
CLIENT_ID_FLOOR = 0xff00000000000000  # 对端分配的 id 下界（types.md）

#: 接口名 -> (requests, events)，元组顺序即 opcode（与 protocol.xml 中声明顺序一致）。
SCHEMA = {
    "ei_handshake": (
        [("handshake_version", ("u32",)), ("finish", ()), ("context_type", ("u32",)),
         ("name", ("s",)), ("interface_version", ("s", "u32"))],
        [("handshake_version", ("u32",)), ("interface_version", ("s", "u32")),
         ("connection", ("u32", "new_id", "u32"))],
    ),
    "ei_connection": (
        [("sync", ("new_id", "u32")), ("disconnect", ())],
        [("disconnected", ("u32", "u32", "s")), ("seat", ("new_id", "u32")),
         ("invalid_object", ("u32", "u64")), ("ping", ("new_id", "u32"))],
    ),
    "ei_callback": ([], [("done", ("u64",))]),
    "ei_pingpong": ([("done", ("u64",))], []),
    "ei_seat": (
        [("release", ()), ("bind", ("u64",)), ("request_device", ("u64",))],
        [("destroyed", ("u32",)), ("name", ("s",)), ("capability", ("u64", "s")),
         ("done", ()), ("device", ("new_id", "u32"))],
    ),
    "ei_device": (
        [("release", ()), ("start_emulating", ("u32", "u32")),
         ("stop_emulating", ("u32",)), ("frame", ("u32", "u64")), ("ready", ())],
        [("destroyed", ("u32",)), ("name", ("s",)), ("device_type", ("u32",)),
         ("dimensions", ("u32", "u32")),
         ("region", ("u32", "u32", "u32", "u32", "f")), ("interface", ("new_id", "s", "u32")),
         ("done", ()), ("resumed", ("u32",)), ("paused", ("u32",)),
         ("start_emulating", ("u32", "u32")), ("stop_emulating", ("u32",)),
         ("frame", ("u32", "u64")), ("region_mapping_id", ("s",))],
    ),
    "ei_pointer": ([("release", ()), ("motion_relative", ("f", "f"))],
                   [("destroyed", ("u32",)), ("motion_relative", ("f", "f"))]),
    "ei_pointer_absolute": ([("release", ()), ("motion_absolute", ("f", "f"))],
                            [("destroyed", ("u32",)), ("motion_absolute", ("f", "f"))]),
    "ei_scroll": ([("release", ()), ("scroll", ("f", "f")), ("scroll_discrete", ("i32", "i32")),
                   ("scroll_stop", ("u32", "u32", "u32"))],
                  [("destroyed", ("u32",)), ("scroll", ("f", "f")),
                   ("scroll_discrete", ("i32", "i32")), ("scroll_stop", ("u32", "u32", "u32"))]),
    "ei_button": ([("release", ()), ("button", ("u32", "u32"))],
                  [("destroyed", ("u32",)), ("button", ("u32", "u32"))]),
    "ei_keyboard": ([("release", ()), ("key", ("u32", "u32"))],
                    [("destroyed", ("u32",)), ("keymap", ("u32", "u32", "fd")),
                     ("key", ("u32", "u32")),
                     ("modifiers", ("u32", "u32", "u32", "u32", "u32"))]),
    "ei_text": ([("release", ()), ("keysym", ("u32", "u32")), ("utf8", ("s",))],
                [("destroyed", ("u32",)), ("keysym", ("u32", "u32")), ("utf8", ("s",))]),
    "ei_touchscreen": ([("release", ()), ("down", ("u32", "f", "f")), ("motion", ("u32", "f", "f")),
                        ("up", ("u32",)), ("cancel", ("u32",))],
                       [("destroyed", ("u32",)), ("down", ("u32", "f", "f")),
                        ("motion", ("u32", "f", "f")), ("up", ("u32",)), ("cancel", ("u32",))]),
}

#: 本层支持并向对端申报的接口及其版本（高于对端时按 min 协商）。
CLIENT_INTERFACES = {"ei_connection": 1, "ei_callback": 1, "ei_pingpong": 1, "ei_seat": 2,
                     "ei_device": 3, "ei_pointer": 1, "ei_pointer_absolute": 1, "ei_scroll": 1,
                     "ei_button": 1, "ei_keyboard": 1, "ei_text": 1, "ei_touchscreen": 2}

#: 第一个参数是 serial 的事件：客户端必须记住它，后续请求的 last_serial 用它。
SERIAL_EVENTS = frozenset({"destroyed", "resumed", "paused", "disconnected", "invalid_object",
                           "start_emulating", "stop_emulating", "frame"})

_HEADER = struct.Struct("@QII")      # object_id, length(含头), opcode
_U32 = struct.Struct("@I")
_I32 = struct.Struct("@i")
_F = struct.Struct("@f")
_U64 = struct.Struct("@Q")
_SIZEOF_INT = struct.calcsize("@i")
HEADER_SIZE = _HEADER.size          # 固定 16 字节：uint64 + uint32 + uint32


def encode_string(value):
    """长度（含 NUL）+ 内容 + 补零到 4 的倍数；空串是 8 字节，NULL 串是 4 字节。"""
    if value is None:
        return _U32.pack(0)
    raw = str(value).encode("utf-8") + b"\0"
    padded = raw + b"\0" * ((4 - len(raw) % 4) % 4)
    return _U32.pack(len(raw)) + padded


def decode_string(payload, offset):
    """返回 (字符串, 新下标)；长度字段不含 NUL 或越界都算协议错乱。"""
    if offset + 4 > len(payload):
        raise Denied("eis_protocol_violation")
    length = _U32.unpack_from(payload, offset)[0]
    offset += 4
    if offset + length > len(payload):
        raise Denied("eis_protocol_violation")
    raw = payload[offset:offset + length]
    consumed = length + ((4 - length % 4) % 4)
    if length:
        if not raw.endswith(b"\0"):
            raise Denied("eis_protocol_violation")
        raw = raw[:-1]
    return raw.decode("utf-8", "replace"), offset + consumed


def _encode_arg(kind, value):
    if kind == "u32" or kind == "fd_count":
        return _U32.pack(int(value) & 0xFFFFFFFF)
    if kind == "i32":
        return _I32.pack(int(value))
    if kind == "f":
        return _F.pack(float(value))
    if kind in ("u64", "new_id", "object_id"):
        return _U64.pack(int(value))
    if kind == "s":
        return encode_string(value)
    if kind == "fd":
        return b""                    # 带外传递，消息里 0 字节
    raise Denied("eis_protocol_violation")


def _decode_arg(kind, payload, offset):
    if kind == "fd":
        return None, offset
    size = {"u32": 4, "i32": 4, "f": 4, "u64": 8, "new_id": 8, "object_id": 8}.get(kind)
    if size is not None:
        if offset + size > len(payload):
            raise Denied("eis_protocol_violation")
        struct_map = {"u32": _U32, "i32": _I32, "f": _F, "u64": _U64,
                      "new_id": _U64, "object_id": _U64}
        return struct_map[kind].unpack_from(payload, offset)[0], offset + size
    if kind == "s":
        return decode_string(payload, offset)
    raise Denied("eis_protocol_violation")


def encode_message(object_id, opcode, kinds, values):
    """拼一条完整消息；参数个数不符或超长都直接拒绝，不发半成品。"""
    if len(kinds) != len(values):
        raise Denied("eis_protocol_violation")
    body = b"".join(_encode_arg(kind, value) for kind, value in zip(kinds, values))
    if HEADER_SIZE + len(body) > MAX_MESSAGE:
        raise Denied("eis_message_too_large")
    return _HEADER.pack(object_id, HEADER_SIZE + len(body), opcode) + body


def unpack_fds(ancdata):
    """从 recvmsg 的附带数据里取 fd 列表。

    不用 `socket.unpack_fds`：部分 Python 构建里连这个函数都没有（本机 venv 3.11
    就没有），冻结包用的就是同一个解释器，写在那里能跑才算能跑。
    """
    fds = []
    for level, kind, data in ancdata or ():
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.extend(int(value) for value in struct.unpack("@i" * (len(data) // 4), data))
    return fds


def _signature(interface, direction, opcode):
    table = SCHEMA.get(interface)
    if not table:
        return None
    entries = table[0] if direction == "request" else table[1]
    if opcode < 0 or opcode >= len(entries):
        return None
    name, kinds = entries[opcode]
    return name, kinds


class EIS:
    """EI 协议客户端：一个连接一个会话，注入前必须走完握手 + resume + start_emulating。"""

    #: 协议里没有窗口对象，只有屏幕 region —— 窗口作用域恒为不可用（写死，不是没查到）。
    supports_window_scope = False

    def __init__(self, path=None, sock=None, timeout=5.0, name="zops-companion",
                 context=CONTEXT_SENDER):
        self.path = path
        self.timeout = timeout
        self.name = name
        self.context = context
        self.sock = sock
        self._owns_socket = sock is None
        self._buffer = bytearray()      # 收发包都用 bytearray：需要原地丢弃已解出的消息
        self._fds = []
        self._objects = {}             # object_id -> (interface, version)
        self._next_id = 1
        self.connection = None
        self.seat = None
        self.seat_name = None
        self.capabilities = {}         # interface -> mask（由对端公布）
        self.devices = {}              # device_id -> 状态字典
        self.serial = 0
        self.sequence = 0
        self.emulating = False
        self._emulating = set()        # 已进入 emulating 的设备 id
        self._pending = set()          # 本 frame 内已发事件的设备 id
        self.dead = None               # 断开/错乱原因码
        self.negotiated = {}
        self.ready_sent = set()
        self._handshake_done = False
        if sock is not None:
            self._adopt(sock)

    # ---- 传输 ----
    def _adopt(self, sock):
        sock.settimeout(self.timeout)
        self.sock = sock

    def connect(self):
        if self.sock is not None:
            return self
        if not self.path:
            raise Denied("eis_socket_unavailable")
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(self.path)
        except OSError:
            raise Denied("eis_socket_unavailable")
        self._owns_socket = True
        self._adopt(sock)
        return self

    def _read_more(self):
        """收字节 + 带外 fd。

        只用 recvmsg 不用 recv_fds：后者在部分 Python 构建里根本不存在（本机
        venv 3.11 是 Clang 构建，没 `recv_fds`），而冻结包用的就是同一个解释器，
        写在那里能跑的代码到包里反而会没。
        """
        try:
            data, ancdata, _flags, _addr = self.sock.recvmsg(
                4096, socket.CMSG_SPACE(16 * _SIZEOF_INT))
        except socket.timeout:
            raise Denied("eis_timeout")
        except OSError:
            raise Denied("eis_connection_lost")
        fds = unpack_fds(ancdata)
        if not data and not fds:
            raise Denied("eis_connection_lost")
        self._fds.extend(fds)
        self._buffer += data

    def _take_fds(self, count):
        """fd 与消息同批到达是常态；没到就继续读，读不到即协议错乱（不猜内容）。"""
        deadline = time.monotonic() + self.timeout
        while len(self._fds) < count:
            if time.monotonic() > deadline:
                raise Denied("eis_protocol_violation")
            before = len(self._buffer)
            try:
                self._read_more()
            except Denied:
                raise
            if len(self._buffer) == before and len(self._fds) < count:
                raise Denied("eis_protocol_violation")
        taken, self._fds = self._fds[:count], self._fds[count:]
        return taken

    def _recv(self):
        """读一条消息并解码成 (interface, direction, name, args, fds)。"""
        while len(self._buffer) < HEADER_SIZE:
            self._read_more()
        object_id, length, opcode = _HEADER.unpack_from(self._buffer)
        if length < HEADER_SIZE or length > MAX_MESSAGE:
            raise Denied("eis_protocol_violation")
        while len(self._buffer) < length:
            self._read_more()
        payload = self._buffer[HEADER_SIZE:length]
        del self._buffer[:length]
        entry = self._objects.get(object_id)
        if entry is None:
            raise Denied("eis_unknown_object")
        interface, version = entry
        # 对端发来的一律是 event（object 0 也一样）；客户端方向不从这里出现。
        signature = _signature(interface, "event", opcode)
        if signature is None:
            raise Denied("eis_protocol_violation")
        name, kinds = signature
        args, offset = [], 0
        for kind in kinds:
            value, offset = _decode_arg(kind, payload, offset)
            args.append(value)
        fd_count = sum(1 for kind in kinds if kind == "fd")
        fds = self._take_fds(fd_count) if fd_count else []
        # 带 serial 的事件都要记账：后续请求的 last_serial 必须是对端最近一个 serial。
        if kinds and kinds[0] == "u32" and name in SERIAL_EVENTS:
            self.serial = args[0]
        self._track(object_id, interface, name, args, fds)
        return interface, name, args, version

    def _send(self, object_id, interface, request, *values):
        if self.dead:
            raise Denied(self.dead)
        opcode = self._opcode(interface, "request", request)
        kinds = SCHEMA[interface][0][opcode][1]
        self.sock.sendall(encode_message(object_id, opcode, kinds, values))

    @staticmethod
    def _opcode(interface, direction, name):
        entries = SCHEMA[interface][0] if direction == "request" else SCHEMA[interface][1]
        for index, (entry_name, _) in enumerate(entries):
            if entry_name == name:
                return index
        raise Denied("eis_protocol_violation")

    def _new_id(self, interface, version):
        # 客户端分配的 id 必须小于对端下界，且不复用。
        require(self._next_id < CLIENT_ID_FLOOR, "eis_id_space_exhausted")
        object_id = self._next_id
        self._next_id += 1
        self._objects[object_id] = (interface, version)
        return object_id

    # ---- 握手 ----
    def handshake(self):
        self.connect()
        if self._handshake_done:
            return self
        self._objects[0] = ("ei_handshake", PROTOCOL_VERSION)
        interface, name, args, _ = self._recv()
        require(name == "handshake_version", "eis_handshake_unexpected")
        eis_version = args[0]
        version = min(int(eis_version), PROTOCOL_VERSION)
        self._send(0, "ei_handshake", "handshake_version", version)
        self._send(0, "ei_handshake", "context_type", self.context)
        self._send(0, "ei_handshake", "name", self.name)
        for iface, iface_version in sorted(CLIENT_INTERFACES.items()):
            self._send(0, "ei_handshake", "interface_version", iface, iface_version)
        self._send(0, "ei_handshake", "finish")
        deadline = time.monotonic() + self.timeout
        while self.connection is None:
            if time.monotonic() > deadline:
                raise Denied("eis_handshake_timeout")
            interface, name, args, version = self._recv()
            if name == "interface_version":
                self.negotiated[args[0]] = args[1]
            elif name == "connection":
                _serial, connection_id, conn_version = args
                self._objects[connection_id] = ("ei_connection", conn_version)
                self.connection = connection_id
            else:
                raise Denied("eis_handshake_unexpected")
        self._handshake_done = True
        return self

    # ---- 会话建立：seat → capability → bind → device → ready → resumed ----
    def _track(self, object_id, interface, name, args, fds):
        """把对端事件落到本地状态；所有后续请求都依赖这里记录的事实。"""
        if interface == "ei_connection" and name == "seat":
            self._objects[args[0]] = ("ei_seat", args[1])
            self.seat = args[0]
        elif interface == "ei_seat" and name == "capability":
            self.capabilities[args[1]] = int(args[0])
        elif interface == "ei_seat" and name == "name":
            self.seat_name = args[0]
        elif interface == "ei_seat" and name == "device":
            self._objects[args[0]] = ("ei_device", args[1])
            self.devices[args[0]] = {"version": args[1], "name": None, "type": None,
                                     "region": None, "resumed": False, "interfaces": {},
                                     "done": False, "emulating": False}
        elif interface == "ei_device":
            device = self.devices.get(object_id)
            if device is None:
                return
            if name == "name":
                device["name"] = args[0]
            elif name == "device_type":
                device["type"] = args[0]
            elif name == "region":
                device["region"] = {"offset_x": args[0], "offset_y": args[1],
                                    "width": args[2], "height": args[3], "scale": args[4]}
            elif name == "interface":
                sub_interface, sub_version = args[1], args[2]
                if sub_interface in SCHEMA:
                    self._objects[args[0]] = (sub_interface, sub_version)
                    device["interfaces"][sub_interface] = args[0]
            elif name == "done":
                device["done"] = True
                # 规范明确：支持 v3 的客户端必须响应 done 发 ready，设备才能被 resume。
                if device["version"] >= 3 and object_id not in self.ready_sent:
                    self._send(object_id, "ei_device", "ready")
                    self.ready_sent.add(object_id)
            elif name == "resumed":
                device["resumed"] = True
            elif name == "paused":
                device["resumed"] = False      # 暂停即失效：下一条输入必须重新等 resumed
            elif name == "region_mapping_id":
                device["region_mapping_id"] = args[0]
        elif interface == "ei_connection" and name == "disconnected":
            self.dead = "eis_connection_closed"
        elif interface == "ei_connection" and name == "invalid_object":
            self.dead = "eis_invalid_object"
        elif interface == "ei_keyboard" and name == "keymap":
            for fd in fds:                     # 用不上也必须关掉，不能漏 fd
                try:
                    os.close(fd)
                except OSError:
                    pass
        elif name == "ping" or interface == "ei_pingpong":
            pass

    def wait_seat(self):
        """等到 seat 公布完能力（`ei_seat.done`），返回掩码表。"""
        self._require_live()
        deadline = time.monotonic() + self.timeout
        while True:
            if time.monotonic() > deadline:
                raise Denied("eis_timeout")
            interface, name, _args, _ = self._recv()
            if interface == "ei_seat" and name == "done":
                return dict(self.capabilities)
            if interface == "ei_seat" and name == "destroyed":
                raise Denied("eis_seat_lost")

    def bind(self, interfaces=("ei_keyboard", "ei_pointer", "ei_button")):
        """只 OR 对端实际公布的掩码；缺任何一个请求的接口都拒绝，绝不降级成“先绑上再说”。"""
        self._require_live()
        require(self.seat is not None, "eis_no_seat")
        mask = 0
        for iface in interfaces:
            value = self.capabilities.get(iface)
            require(value is not None, "eis_capability_unavailable")
            mask |= int(value)
        require(mask, "eis_capability_unavailable")
        self._send(self.seat, "ei_seat", "bind", mask)
        return mask

    def wait_devices(self, needed=("ei_keyboard", "ei_pointer", "ei_button")):
        """等设备公布完（`ei_device.done`）；`ready` 在收到 done 时自动发出。"""
        self._require_live()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self._recv()
            if self._devices_ready(needed):
                return True
        raise Denied("eis_no_device")

    def wait_resumed(self, needed=1):
        """等到足够多的设备进入 resumed：没 resume 就发输入是 client bug。"""
        self._require_live()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if len(self._emulatable()) >= needed:
                return True
            self._recv()
        raise Denied("eis_timeout")

    def _devices_ready(self, needed):
        return any(device["done"] and all(iface in device["interfaces"] for iface in needed)
                   for device in self.devices.values())

    def _pick(self, interface):
        """找具备该接口的设备；有设备但都被暂停时绝不当成可用（paused 就是不能发）。"""
        have = [(device_id, device) for device_id, device in self.devices.items()
                if interface in device["interfaces"]]
        require(bool(have), "eis_no_device")
        resumed = [item for item in have if item[1]["resumed"]]
        if not resumed:
            raise Denied("eis_device_paused")
        return resumed[0]

    def _require_live(self):
        if self.dead:
            raise Denied(self.dead)
        require(self._handshake_done, "eis_not_connected")

    def pump(self, limit=64, timeout=0.2):
        """把对端已发来的事件读干净（resumed/paused/frame 等），不新发请求。

        只有“没数据了”才默默收尾；协议错乱、对象未知必须原样上抛，
        不能被当成“本轮没事件”吐掉。
        """
        self._require_live()
        previous = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        processed = 0
        try:
            for _ in range(limit):
                self._recv()
                processed += 1
        except Denied as error:
            if str(error) != "eis_timeout":
                raise
        finally:
            self.sock.settimeout(previous)
        return processed

    # ---- 注入 ----
    def _emulatable(self):
        """可以开始模拟的设备：必须已 resumed 且至少有一个输入子接口。"""
        return [device_id for device_id, device in self.devices.items()
                if device["resumed"] and device["interfaces"]]

    def start_emulating(self):
        self._require_live()
        require(not self.emulating, "eis_already_emulating")
        targets = self._emulatable()
        require(bool(targets), "eis_device_paused")
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        for device_id in targets:
            self._send(device_id, "ei_device", "start_emulating", self.serial, self.sequence)
        self._emulating = set(targets)
        self.emulating = True
        return self.sequence

    def stop_emulating(self):
        """断开/错乱时不再向对端发请求，但本地必须立刻失效（不能留着“还在注入”的错觉）。"""
        targets = set(self._emulating)
        self.emulating = False
        self._emulating = set()
        self._pending = set()
        if self.dead or not targets:
            return False
        for device_id in sorted(targets):
            self._send(device_id, "ei_device", "stop_emulating", self.serial)
        return True

    def _emit(self, interface, request, *values):
        """所有输入的统一闸门：未握手/未 emulating/被暂停一律拒绝。"""
        self._require_live()
        require(self.emulating, "eis_not_emulating")
        device_id, device = self._pick(interface)
        require(device["resumed"], "eis_device_paused")
        self._send(device["interfaces"][interface], interface, request, *values)
        self._pending.add(device_id)
        return device_id

    def key(self, keycode, pressed=True):
        return self._emit("ei_keyboard", "key", int(keycode),
                          KEY_PRESSED if pressed else KEY_RELEASED)

    def button(self, code, pressed=True):
        return self._emit("ei_button", "button", int(code),
                          BUTTON_PRESSED if pressed else BUTTON_RELEASED)

    def motion(self, dx, dy):
        return self._emit("ei_pointer", "motion_relative", float(dx), float(dy))

    def motion_absolute(self, x, y):
        """绝对指针必须落在设备 region 内；区域未知就不发（宁可不点）。"""
        self._require_live()
        require(self.emulating, "eis_not_emulating")
        device_id, device = self._pick("ei_pointer_absolute")
        region = device.get("region")
        require(region is not None, "eis_region_unknown")
        require(region["width"] and region["height"], "eis_region_unknown")
        if not (0 <= float(x) <= region["width"] and 0 <= float(y) <= region["height"]):
            raise Denied("eis_out_of_region")
        self._send(device["interfaces"]["ei_pointer_absolute"], "ei_pointer_absolute",
                   "motion_absolute", float(x), float(y))
        self._pending.add(device_id)
        return device_id

    def scroll(self, dx, dy):
        return self._emit("ei_scroll", "scroll", float(dx), float(dy))

    def frame(self):
        """硬件事务边界：没 frame 的事件合成器有权不处理；timestamp 用 CLOCK_MONOTONIC 微秒。

        frame 是每个设备一个：本轮在几个设备上发过事件就要给几个设备发 frame。
        """
        self._require_live()
        require(self.emulating, "eis_not_emulating")
        if not self._pending:
            return None
        stamp = int(time.clock_gettime(time.CLOCK_MONOTONIC) * 1000000)
        for device_id in sorted(self._pending):
            self._send(device_id, "ei_device", "frame", self.serial, stamp)
        self._pending.clear()
        return stamp

    def click(self, code=BUTTON_LEFT):
        """按下与抬起各占一个 frame；start/stop_emulating 是会话级边界，由调用方管。"""
        self.button(code, True)
        self.frame()
        self.button(code, False)
        return self.frame()

    # ---- 状态汇报（doctor 用；只报事实，不报“应该可以”）----
    def state(self):
        return {"connected": bool(self._handshake_done),
                "dead": self.dead,
                "seat": self.seat_name,
                "capabilities": {name: int(mask) for name, mask in self.capabilities.items()},
                "devices": [{"name": device["name"], "version": device["version"],
                             "type": device["type"], "resumed": device["resumed"],
                             "interfaces": sorted(device["interfaces"]),
                             "region": device["region"]}
                            for device in self.devices.values()],
                "emulating": self.emulating,
                "window_scoped": self.supports_window_scope}

    def close(self):
        if self.sock is not None:
            if not self.dead and self._handshake_done:
                try:
                    self._send(self.connection, "ei_connection", "disconnect")
                except Exception:
                    pass
            try:
                self.sock.close()          # 接管来的 fd 也一样要关，不然每重连一次漏一个
            except OSError:
                pass
            self.sock = None
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = []
