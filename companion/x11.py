"""Linux X11 底层原语：只用 libX11/libXtst/libXss，不引入额外运行时依赖。

这里只放"能不能做"和"怎么做一次"；是否开放桌面能力由 `desktop.Desktop` 决定
（本机验收记录 + 原生批准 + 60 秒租约）。所有失败都抛固定错误码，不回显路径。
"""
import ctypes
import ctypes.util
import threading

from .security import Denied, require

Display = ctypes.c_void_p
Window = ctypes.c_ulong
Atom = ctypes.c_ulong

# xproto / xtest / scrnsaver 常量，只保留用到的部分。
IS_VIEWABLE = 2
REVERT_TO_POINTER_ROOT = 1
KEY_PRESS = 2
BAD_ACCESS = 10
GRAB_MODE_ASYNC = 1
CURRENT_TIME = 0
SHIFT_MASK, LOCK_MASK, CONTROL_MASK, MOD1_MASK, MOD2_MASK = 1, 2, 4, 8, 16
SCREENSAVER_OFF, SCREENSAVER_ON = 0, 1
# 屏保开启且闲置超过该时长，按"用户已离开"处理（与锁屏同等对待）。
LOCK_IDLE_MS = 60_000

# 锁屏/认证类窗口的标题或类名关键词；命中即按"用户不在场"处理。
LOCK_WINDOW_WORDS = ("lock", "screen saver", "screensaver", "gdm", "sddm", "lightdm",
                     "xscreensaver", "i3lock", "swaylock", "polkit", "authentication")

XK_SHIFT_L, XK_CONTROL_L, XK_ALT_L = 0xFFE1, 0xFFE3, 0xFFE9
XK_PAUSE, XK_BREAK = 0xFF13, 0xFF6B
# 键名到 keysym：组合键与单键动作都用这一张表，未知键名一律拒绝。
KEY_NAMES = {"escape": 0xFF1B, "tab": 0xFF09, "enter": 0xFF0D, "up": 0xFF52,
             "down": 0xFF54, "left": 0xFF51, "right": 0xFF53, "home": 0xFF50,
             "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
             "backspace": 0xFF08, "delete": 0xFFFF, "space": 0x0020,
             "pause": XK_PAUSE, "break": XK_BREAK}
MODIFIERS = {"ctrl": XK_CONTROL_L, "alt": XK_ALT_L, "shift": XK_SHIFT_L}


class XClassHint(ctypes.Structure):
    _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]


class XWindowAttributes(ctypes.Structure):
    """Xlib 会写满整个结构，字段顺序必须与 xlib.h 一致。"""
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int),
                ("height", ctypes.c_int), ("border_width", ctypes.c_int),
                ("depth", ctypes.c_int), ("visual", ctypes.c_void_p), ("root", Window),
                ("c_class", ctypes.c_int), ("bit_gravity", ctypes.c_int),
                ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
                ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
                ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong),
                ("map_installed", ctypes.c_int), ("map_state", ctypes.c_int),
                ("all_event_masks", ctypes.c_long), ("your_event_mask", ctypes.c_long),
                ("do_not_propagate_mask", ctypes.c_long), ("override_redirect", ctypes.c_int),
                ("screen", ctypes.c_void_p)]


class XErrorEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("display", ctypes.c_void_p),
                ("resourceid", ctypes.c_ulong), ("serial", ctypes.c_ulong),
                ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte),
                ("minor_code", ctypes.c_ubyte)]


class ScreenSaverInfo(ctypes.Structure):
    _fields_ = [("window", Window), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                ("since", ctypes.c_ulong), ("idle", ctypes.c_ulong), ("mask", ctypes.c_ulong)]


_ERROR_CODES = []
_ERROR_HANDLER_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, Display, ctypes.POINTER(XErrorEvent))


@_ERROR_HANDLER_TYPE
def _on_x_error(_display, event):
    # 只记录协议错误码（例如 GrabKey 的 BadAccess）；默认处理器会往 stderr 打印。
    _ERROR_CODES.append(int(event.contents.error_code))
    return 0


def _library(names):
    for name in names:
        found = ctypes.util.find_library(name) or ("lib" + name + ".so.6")
        try:
            return ctypes.CDLL(found)
        except OSError:
            continue
    raise Denied("x11_unavailable")


def _prototypes():
    """声明原型，避免指针被截断成 int（这是 ctypes 最常见的崩溃原因）。"""
    libs = {"x11": _library(("X11",)), "xtst": _library(("Xtst",)),
            "xss": _library(("Xss",))}
    x11, xtst, xss = libs["x11"], libs["xtst"], libs["xss"]
    x11.XOpenDisplay.restype = Display
    x11.XOpenDisplay.argtypes = (ctypes.c_char_p,)
    x11.XCloseDisplay.argtypes = (Display,)
    x11.XDefaultRootWindow.restype = Window
    x11.XDefaultRootWindow.argtypes = (Display,)
    x11.XQueryTree.argtypes = (Display, Window, ctypes.POINTER(Window), ctypes.POINTER(Window),
                               ctypes.POINTER(ctypes.POINTER(Window)), ctypes.POINTER(ctypes.c_uint))
    x11.XInternAtom.restype = Atom
    x11.XInternAtom.argtypes = (Display, ctypes.c_char_p, ctypes.c_int)
    x11.XGetWindowProperty.argtypes = (Display, Window, Atom, ctypes.c_long, ctypes.c_long,
                                       ctypes.c_int, Atom, ctypes.POINTER(Atom),
                                       ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
                                       ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p))
    x11.XGetWindowAttributes.argtypes = (Display, Window, ctypes.POINTER(XWindowAttributes))
    x11.XGetClassHint.argtypes = (Display, Window, ctypes.POINTER(XClassHint))
    x11.XTranslateCoordinates.argtypes = (Display, Window, Window, ctypes.c_int, ctypes.c_int,
                                          ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                          ctypes.POINTER(Window))
    x11.XGetInputFocus.argtypes = (Display, ctypes.POINTER(Window), ctypes.POINTER(ctypes.c_int))
    x11.XSetInputFocus.argtypes = (Display, Window, ctypes.c_int, ctypes.c_ulong)
    x11.XRaiseWindow.argtypes = (Display, Window)
    x11.XSync.argtypes = (Display, ctypes.c_int)
    x11.XFlush.argtypes = (Display,)
    x11.XPending.argtypes = (Display,)
    x11.XNextEvent.argtypes = (Display, ctypes.c_void_p)
    x11.XKeysymToKeycode.restype = ctypes.c_ubyte
    x11.XKeysymToKeycode.argtypes = (Display, ctypes.c_ulong)
    x11.XDisplayKeycodes.argtypes = (Display, ctypes.POINTER(ctypes.c_int),
                                     ctypes.POINTER(ctypes.c_int))
    x11.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
    x11.XGetKeyboardMapping.argtypes = (Display, ctypes.c_ubyte, ctypes.c_int,
                                        ctypes.POINTER(ctypes.c_int))
    x11.XGetSelectionOwner.restype = Window
    x11.XGetSelectionOwner.argtypes = (Display, Atom)
    x11.XGrabKey.argtypes = (Display, ctypes.c_int, ctypes.c_uint, Window, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int)
    x11.XUngrabKey.argtypes = (Display, ctypes.c_int, ctypes.c_uint, Window)
    x11.XSetErrorHandler.restype = ctypes.c_void_p
    x11.XSetErrorHandler.argtypes = (_ERROR_HANDLER_TYPE,)
    x11.XFree.argtypes = (ctypes.c_void_p,)
    xtst.XTestQueryExtension.argtypes = (Display, ctypes.POINTER(ctypes.c_int),
                                         ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                         ctypes.POINTER(ctypes.c_int))
    xtst.XTestFakeKeyEvent.argtypes = (Display, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong)
    xtst.XTestFakeButtonEvent.argtypes = (Display, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong)
    xtst.XTestFakeMotionEvent.argtypes = (Display, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_ulong)
    xss.XScreenSaverQueryExtension.argtypes = (Display, ctypes.POINTER(ctypes.c_int),
                                               ctypes.POINTER(ctypes.c_int))
    xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(ScreenSaverInfo)
    xss.XScreenSaverAllocInfo.argtypes = ()
    xss.XScreenSaverQueryInfo.argtypes = (Display, Window, ctypes.POINTER(ScreenSaverInfo))
    return libs


class X11:
    """一条独立的 Xlib 连接；Xlib 不是线程安全的，所有调用都在锁内。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.libs = _prototypes()
        self.x11 = self.libs["x11"]
        self._closed = False
        self._keymap = None
        self._errors = []
        # X 协议错误默认只打 stderr；换成自己的收集器，才能判断 GrabKey 是否被占用。
        self.x11.XSetErrorHandler(_on_x_error)
        self.dpy = self.x11.XOpenDisplay(None)
        require(bool(self.dpy), "x11_display_unavailable")
        self.root = self.x11.XDefaultRootWindow(self.dpy)
        self.screen = 0
        self.xtest = self._extension(self.libs["xtst"], "XTestQueryExtension")
        self.xss = self._extension(self.libs["xss"], "XScreenSaverQueryExtension")
        self.hotkey_display = None
        self.hotkey_keys = []

    def _extension(self, lib, name):
        first = ctypes.c_int()
        second = ctypes.c_int()
        args = [ctypes.byref(first), ctypes.byref(second)]
        if name.startswith("XTest"):
            args += [ctypes.byref(ctypes.c_int()), ctypes.byref(ctypes.c_int())]
        try:
            return bool(getattr(lib, name)(self.dpy, *args))
        except Exception:
            return False

    # ---- 窗口 ----
    def _property(self, xid, name, limit=4096):
        atom = self.x11.XInternAtom(self.dpy, name.encode(), 0)
        kind = Atom()
        fmt = ctypes.c_int()
        count = ctypes.c_ulong()
        after = ctypes.c_ulong()
        data = ctypes.c_void_p()
        rc = self.x11.XGetWindowProperty(self.dpy, xid, atom, 0, limit, 0, 0,
                                         ctypes.byref(kind), ctypes.byref(fmt),
                                         ctypes.byref(count), ctypes.byref(after),
                                         ctypes.byref(data))
        if rc != 0 or not data:
            return None
        try:
            return ctypes.string_at(data, count.value * (fmt.value // 8))
        finally:
            self.x11.XFree(data)

    def _attributes(self, xid):
        attrs = XWindowAttributes()
        require(bool(self.x11.XGetWindowAttributes(self.dpy, xid, ctypes.byref(attrs))),
                "desktop_window_missing")
        return attrs

    def _children(self, xid):
        root = Window()
        parent = Window()
        items = ctypes.POINTER(Window)()
        count = ctypes.c_uint()
        if not self.x11.XQueryTree(self.dpy, xid, ctypes.byref(root), ctypes.byref(parent),
                                   ctypes.byref(items), ctypes.byref(count)):
            return []
        try:
            return [int(items[index]) for index in range(count.value)]
        finally:
            if items:
                self.x11.XFree(ctypes.cast(items, ctypes.c_void_p))

    def _class_name(self, xid):
        hint = XClassHint()
        if not self.x11.XGetClassHint(self.dpy, xid, ctypes.byref(hint)):
            return ""
        try:
            raw = ctypes.string_at(hint.res_class) if hint.res_class else b""
        finally:
            for value in (hint.res_name, hint.res_class):
                if value:
                    self.x11.XFree(ctypes.c_void_p(value))
        return raw.decode("utf-8", "replace")[:120]

    def _title(self, xid):
        # 优先 _NET_WM_NAME（UTF-8），回退 WM_NAME（Latin-1）。
        raw = self._property(xid, "_NET_WM_NAME")
        if raw:
            return raw.decode("utf-8", "replace")[:120]
        raw = self._property(xid, "WM_NAME")
        return raw.decode("latin-1", "replace")[:120] if raw else ""

    def _pid(self, xid):
        raw = self._property(xid, "_NET_WM_PID")
        # _NET_WM_PID 是 32 位 CARDINAL；没有该属性时只能按窗口类识别，不能按进程授权。
        if not raw or len(raw) < 4:
            return 0
        return int(ctypes.c_uint.from_buffer_copy(raw[:4]).value)

    def _client_of(self, xid):
        """有窗口管理器的会话里顶层窗口是 frame，真正的客户端是它的子窗口。"""
        for _ in range(2):
            if self._class_name(xid) or self._pid(xid) or self._title(xid):
                return int(xid)
            candidates = [child for child in self._children(xid)
                          if self._class_name(child) or self._pid(child)]
            if not candidates:
                return 0
            xid = candidates[0]
        return 0

    def info(self, xid):
        """返回 Desktop 需要的窗口信息；没有 _NET_WM_PID 时 pid=0（无法按进程授权）。"""
        with self.lock:
            client = self._client_of(int(xid))
            require(bool(client), "desktop_window_missing")
            attrs = self._attributes(client)
            require(attrs.map_state == IS_VIEWABLE, "desktop_window_unavailable")
            title = self._title(client)
            require(bool(title.strip()), "desktop_window_untitled")
            x = ctypes.c_int()
            y = ctypes.c_int()
            child = Window()
            self.x11.XTranslateCoordinates(self.dpy, client, self.root, 0, 0,
                                           ctypes.byref(x), ctypes.byref(y), ctypes.byref(child))
            left, top = x.value, y.value
            return {"hwnd": client, "title": title, "class_name": self._class_name(client),
                    "pid": self._pid(client),
                    "rect": [left, top, left + attrs.width, top + attrs.height]}

    def windows(self, limit):
        with self.lock:
            out = []
            for top in self._children(self.root):
                if len(out) >= limit:
                    break
                try:
                    attrs = self._attributes(top)
                    if attrs.map_state != IS_VIEWABLE or attrs.override_redirect:
                        continue
                    info = self.info(top)
                except Exception:
                    continue
                out.append(info)
            return out

    # ---- 焦点与锁屏 ----
    def focus(self):
        with self.lock:
            target = Window()
            revert = ctypes.c_int()
            require(bool(self.x11.XGetInputFocus(self.dpy, ctypes.byref(target),
                                                 ctypes.byref(revert))), "x11_focus_query_failed")
            return int(target.value)

    def top_level(self, xid):
        """把焦点窗口沿父链上溯到顶层，便于和接管的窗口比较。"""
        with self.lock:
            current = int(xid)
            for _ in range(16):
                root = Window()
                parent = Window()
                items = ctypes.POINTER(Window)()
                count = ctypes.c_uint()
                if not self.x11.XQueryTree(self.dpy, current, ctypes.byref(root),
                                           ctypes.byref(parent), ctypes.byref(items),
                                           ctypes.byref(count)):
                    return current
                if items:
                    self.x11.XFree(ctypes.cast(items, ctypes.c_void_p))
                if parent.value in (0, self.root):
                    return current
                current = int(parent.value)
            return current

    def set_focus(self, xid):
        with self.lock:
            self.x11.XRaiseWindow(self.dpy, int(xid))
            self.x11.XSetInputFocus(self.dpy, int(xid), REVERT_TO_POINTER_ROOT, CURRENT_TIME)
            self.x11.XSync(self.dpy, 0)
            return int(xid) in (self.focus(), self.top_level(self.focus()))

    def idle_ms(self):
        """自最后一次输入起的毫秒数；XScreenSaver 不可用时返回 -1。"""
        with self.lock:
            if not self.xss:
                return -1
            info = self.libs["xss"].XScreenSaverAllocInfo()
            require(bool(info), "x11_idle_unavailable")
            try:
                require(bool(self.libs["xss"].XScreenSaverQueryInfo(self.dpy, self.root, info)),
                        "x11_idle_unavailable")
                return int(info.contents.idle)
            finally:
                self.x11.XFree(ctypes.cast(info, ctypes.c_void_p))

    def screensaver_state(self):
        with self.lock:
            if not self.xss:
                return -1
            info = self.libs["xss"].XScreenSaverAllocInfo()
            require(bool(info), "x11_idle_unavailable")
            try:
                require(bool(self.libs["xss"].XScreenSaverQueryInfo(self.dpy, self.root, info)),
                        "x11_idle_unavailable")
                return int(info.contents.state)
            finally:
                self.x11.XFree(ctypes.cast(info, ctypes.c_void_p))

    def locked(self):
        """屏保已激活或已有锁屏窗口时视为锁定；查询不了也按锁定处理。"""
        state = self.screensaver_state()
        if state < 0:
            return True
        if state == SCREENSAVER_ON:
            return True
        return self.lock_window_present()

    def tray_available(self):
        """是否有 XEmbed 托盘宿主（_NET_SYSTEM_TRAY_S0）。没有就不启动托盘图标。"""
        with self.lock:
            atom = int(self.x11.XInternAtom(self.dpy, b"_NET_SYSTEM_TRAY_S0", 0))
            return bool(atom) and int(self.x11.XGetSelectionOwner(self.dpy, atom)) != 0

    def lock_window_present(self):
        for info in self.windows(60):
            text = (info["class_name"] + " " + info["title"]).lower()
            if any(word in text for word in LOCK_WINDOW_WORDS):
                return True
        return False

    # ---- 注入 ----
    def keycode(self, keysym):
        with self.lock:
            return int(self.x11.XKeysymToKeycode(self.dpy, int(keysym)))

    def keycode_for(self, char):
        """返回 (keycode, 需不需要 Shift)；当前布局打不出该字符时返回 (0, False)。"""
        code = ord(char)
        plain, shifted = self._keymap or self._build_keymap()
        if code in plain:
            return plain[code], False
        if code in shifted:
            return shifted[code], True
        return 0, False

    def _build_keymap(self):
        with self.lock:
            low = ctypes.c_int()
            high = ctypes.c_int()
            self.x11.XDisplayKeycodes(self.dpy, ctypes.byref(low), ctypes.byref(high))
            per = ctypes.c_int()
            count = high.value - low.value + 1
            require(count > 0, "x11_keymap_unavailable")
            table = self.x11.XGetKeyboardMapping(self.dpy, low.value, count, ctypes.byref(per))
            require(bool(table) and per.value > 0, "x11_keymap_unavailable")
            plain, shifted = {}, {}
            try:
                for index in range(count):
                    base = index * per.value
                    first = int(table[base])
                    if first:
                        plain.setdefault(first, low.value + index)
                    if per.value > 1:
                        second = int(table[base + 1])
                        if second:
                            shifted.setdefault(second, low.value + index)
            finally:
                self.x11.XFree(ctypes.cast(table, ctypes.c_void_p))
            self._keymap = (plain, shifted)
            return self._keymap

    def _tap(self, keycode, modifiers=()):
        x11 = self.x11
        xtst = self.libs["xtst"]
        pressed = []
        try:
            for keysym in modifiers:
                code = int(x11.XKeysymToKeycode(self.dpy, keysym))
                require(bool(code), "x11_key_unavailable")
                xtst.XTestFakeKeyEvent(self.dpy, code, 1, 0)
                pressed.append(code)
            xtst.XTestFakeKeyEvent(self.dpy, int(keycode), 1, 0)
            xtst.XTestFakeKeyEvent(self.dpy, int(keycode), 0, 0)
        finally:
            for code in reversed(pressed):
                xtst.XTestFakeKeyEvent(self.dpy, code, 0, 0)
            x11.XFlush(self.dpy)

    def type_text(self, value):
        require(self.xtest, "x11_injection_unavailable")
        with self.lock:
            for char in value:
                if char == "\n":
                    self._tap(KEY_NAMES["enter"])
                    continue
                if char == "\t":
                    self._tap(KEY_NAMES["tab"])
                    continue
                require(32 <= ord(char) <= 126, "invalid_text")
                code, shift = self.keycode_for(char)
                require(bool(code), "x11_key_unavailable")
                self._tap(code, (XK_SHIFT_L,) if shift else ())

    def press(self, keys):
        require(self.xtest, "x11_injection_unavailable")
        with self.lock:
            modifiers = [MODIFIERS[item] for item in ("ctrl", "alt", "shift") if item in keys]
            name = next(item for item in keys if item not in MODIFIERS)
            keysym = KEY_NAMES.get(name)
            if keysym is None and name.startswith("f") and name[1:].isdigit():
                keysym = 0xFFBE + int(name[1:]) - 1  # XK_F1 起连续
            if keysym is None:
                keysym = ord(name)
            code = self.keycode(keysym)
            require(bool(code), "x11_key_unavailable")
            self._tap(code, modifiers)

    def click(self, x, y):
        require(self.xtest, "x11_injection_unavailable")
        with self.lock:
            xtst = self.libs["xtst"]
            xtst.XTestFakeMotionEvent(self.dpy, self.screen, int(x), int(y), 0)
            xtst.XTestFakeButtonEvent(self.dpy, 1, 1, 0)
            xtst.XTestFakeButtonEvent(self.dpy, 1, 0, 0)
            self.x11.XFlush(self.dpy)

    def scroll(self, x, y, amount):
        require(self.xtest, "x11_injection_unavailable")
        with self.lock:
            xtst = self.libs["xtst"]
            button = 4 if amount > 0 else 5
            xtst.XTestFakeMotionEvent(self.dpy, self.screen, int(x), int(y), 0)
            for _ in range(min(10, abs(int(amount)))):
                xtst.XTestFakeButtonEvent(self.dpy, button, 1, 0)
                xtst.XTestFakeButtonEvent(self.dpy, button, 0, 0)
            self.x11.XFlush(self.dpy)

    # ---- 全局热键 ----
    def grab_hotkey(self, keysyms=(XK_PAUSE, XK_BREAK)):
        """在 root 上抓 Ctrl+Alt+<keysym>；返回可用组合描述，全失败返回空串。"""
        with self.lock:
            if self.hotkey_display:
                return self.hotkey_name
            self.hotkey_display = self.x11.XOpenDisplay(None)
            require(bool(self.hotkey_display), "x11_display_unavailable")
            combos = (0, LOCK_MASK, MOD2_MASK, LOCK_MASK | MOD2_MASK)
            for keysym in keysyms:
                code = int(self.x11.XKeysymToKeycode(self.hotkey_display, keysym))
                if not code:
                    continue
                self._errors.clear()
                for extra in combos:
                    self.x11.XGrabKey(self.hotkey_display, code,
                                      CONTROL_MASK | MOD1_MASK | extra, self.root, 0,
                                      GRAB_MODE_ASYNC, GRAB_MODE_ASYNC)
                self.x11.XSync(self.hotkey_display, 0)
                if BAD_ACCESS in self._errors:
                    for extra in combos:
                        self.x11.XUngrabKey(self.hotkey_display, code,
                                            CONTROL_MASK | MOD1_MASK | extra, self.root)
                    self.x11.XSync(self.hotkey_display, 0)
                    continue
                self.hotkey_keys.append((code, keysym))
                self.hotkey_name = "Ctrl+Alt+" + ("Pause" if keysym == XK_PAUSE else "Break")
                return self.hotkey_name
            self.x11.XCloseDisplay(self.hotkey_display)
            self.hotkey_display = None
            return ""

    def hotkey_pending(self):
        """非阻塞读取热键事件；返回 True 表示用户按下了紧急停止。"""
        with self.lock:
            display = self.hotkey_display
            if not display:
                return False
            fired = False
            buffer = ctypes.create_string_buffer(192)  # XEvent 最大 24 个 long
            while self.x11.XPending(display) > 0:
                self.x11.XNextEvent(display, buffer)
                if ctypes.c_int.from_buffer(buffer).value == KEY_PRESS:
                    fired = True
            return fired

    def ungrab_hotkey(self):
        with self.lock:
            if not self.hotkey_display:
                return
            for code, _ in self.hotkey_keys:
                for extra in (0, LOCK_MASK, MOD2_MASK, LOCK_MASK | MOD2_MASK):
                    self.x11.XUngrabKey(self.hotkey_display, code,
                                        CONTROL_MASK | MOD1_MASK | extra, self.root)
            self.x11.XSync(self.hotkey_display, 0)
            self.x11.XCloseDisplay(self.hotkey_display)
            self.hotkey_display = None
            self.hotkey_keys = []

    # ---- 采集（Linux 未验收，driver 不声明 desktop.screen，这里只保留明确拒绝） ----
    def capture(self, xid):
        raise Denied("desktop_capture_not_accepted_on_platform")

    def probe(self):
        """预检摘要：能枚举窗口、能查焦点、能查空闲时间，三者都是实测。"""
        windows = self.windows(8)
        return {"windows": len(windows), "focus": bool(self.focus()),
                "idle_ms": self.idle_ms(), "xtest": bool(self.xtest), "xss": bool(self.xss),
                "hotkey": getattr(self, "hotkey_name", "")}

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self.ungrab_hotkey()
            if self.dpy:
                self.x11.XCloseDisplay(self.dpy)
                self.dpy = None
