"""macOS 桌面后端：ApplicationServices(AX) + CoreGraphics + CoreFoundation 的 ctypes 封装。

本仓库所在机器不是 Mac，所以这一层的定位与 x11.py 完全一致：只做“能不能做到”的实测。
符号缺失、权限未授予、返回异常都原样报失败，由上层 MacDriver 的预检决定禁用桌面能力，
绝不猜测、也绝不降级成“看起来可用”（Spec 第 84 条同样适用：没在实机跑过就不算验收）。

纪律（x11.py 与 Windows 侧都踩过）：凡是返回或传递指针/句柄（CGEventRef、AXUIElementRef、
CFTypeRef、CGImageRef、CFMachPortRef）的调用都必须显式声明 restype/argtypes，否则 64 位
指针会被 ctypes 默认的 c_int 截断。
"""
import ctypes
import ctypes.util
import threading
import time

from .security import Denied, require

# ---- CGEvent / CoreGraphics 常量（取自 SDK 头文件的公开定义） ----
K_KEY_DOWN = 10                 # kCGEventKeyDown
MOUSE_LEFT_DOWN = 1             # kCGEventLeftMouseDown
MOUSE_LEFT_UP = 2               # kCGEventLeftMouseUp
MOUSE_MOVED = 5                 # kCGEventMouseMoved
FIELD_CLICK_STATE = 1           # kCGMouseEventClickState
FIELD_KEYCODE = 9               # kCGKeyboardEventKeycode
SCROLL_UNIT_LINE = 0            # kCGScrollEventUnitLine
FLAG_SHIFT = 0x00020000         # kCGEventFlagMaskShift
FLAG_CONTROL = 0x00040000       # kCGEventFlagMaskControl
FLAG_OPTION = 0x00080000        # kCGEventFlagMaskAlternate
FLAG_COMMAND = 0x00100000       # kCGEventFlagMaskCommand
SOURCE_COMBINED_SESSION = 0     # kCGEventSourceStateCombinedSessionState
ANY_INPUT_EVENT = 0xFFFFFFFF    # kCGAnyInputEventType
TAP_HID = 0                     # kCGHIDEventTap
TAP_HEAD_INSERT = 0             # kCGHeadInsertEventTap
TAP_LISTEN_ONLY = 1             # kCGEventTapOptionListenOnly（只观察，不改写用户输入）
WINDOW_LIST_ON_SCREEN = 1 << 0        # kCGWindowListOptionOnScreenOnly
WINDOW_LIST_NO_DESKTOP = 1 << 4       # kCGWindowListExcludeDesktopElements
NULL_WINDOW_ID = 0xFFFFFFFF           # kCGNullWindowID
IMAGE_IGNORE_FRAMING = 1 << 2         # kCGWindowImageBoundsIgnoreFraming
NORMAL_WINDOW_LAYER = 0               # 只有该 layer 才是普通应用窗口
BITS_PER_PIXEL = 32                   # 采集只接受 8bit/通道 x4，别的布局一律报不可用

# ---- Accessibility（HIServices）----
AX_SUCCESS = 0
AX_VALUE_CGPOINT = 1            # kAXValueCGPointType
AX_VALUE_CGSIZE = 2             # kAXValueCGSizeType
CF_STRING_UTF8 = 0x08000100     # kCFStringEncodingUTF8
SECURE_SUBROLE = "AXSecureTextField"

# Spec 第 53 条要求全局停止热键。Mac 键盘没有 Pause/Break 键，按平台适配取
# Ctrl+Option+Cmd+S（S=Stop；两个额外修饰键使它不与 Cmd+S 保存冲突）。
HOTKEY_TEXT_MAC = "Ctrl+Option+Cmd+S"
HOTKEY_KEYCODE = 0x01           # kVK_ANSI_S
HOTKEY_MODIFIERS = FLAG_CONTROL | FLAG_OPTION | FLAG_COMMAND

# kVK_* 虚拟键码（HIToolbox/Events.h），覆盖 desktop.act 允许的全部键。
KEYCODES = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06,
    "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E,
    "r": 0x0F, "y": 0x10, "t": 0x11, "o": 0x1F, "u": 0x20, "i": 0x22, "p": 0x23,
    "l": 0x25, "j": 0x26, "k": 0x28, "n": 0x2D, "m": 0x2E,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17,
    "9": 0x19, "7": 0x1A, "8": 0x1B, "0": 0x1E,
    "space": 0x31, "tab": 0x30, "enter": 0x24, "backspace": 0x33, "escape": 0x35,
    "delete": 0x75, "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x82, "f6": 0x80,
    "f7": 0x88, "f8": 0x85, "f9": 0x89, "f10": 0x8B, "f11": 0x83, "f12": 0x8C,
}
# 修饰键自身的键码，用于 press 里真正按下/放开修饰键。
MODIFIER_KEYCODES = {"ctrl": 0x3B, "alt": 0x3A, "shift": 0x38}
MODIFIER_FLAGS = {"ctrl": FLAG_CONTROL, "alt": FLAG_OPTION, "shift": FLAG_SHIFT}

# AX 角色→动作范围名：只开放可激活控件（与 desktop.ELEMENT_TYPES 同口径）。
# 名字对不上的 AX 角色一律不收，不做“看起来差不多”的映射。
ACTIONABLE_ROLES = {"AXButton": "button", "AXMenuItem": "menuitem",
                    "AXCheckBox": "checkbox", "AXRadioButton": "radiobutton",
                    "AXLink": "hyperlink", "AXTabButton": "tabitem",
                    "AXRow": "treeitem", "AXCell": "listitem"}
ELEMENT_LIMIT = 80
ELEMENT_DEPTH = 6
CHILD_LIMIT = 200


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def _utf16_units(char):
    """CGEventKeyboardSetUnicodeString 以 UniChar（UTF-16 code unit）计数。"""
    return max(1, len(char.encode("utf-16-le")) // 2)


def _framework(name):
    """find_library 对框架名不一定返回可用路径，按 SDK 常用路径兜底。"""
    candidates = [
        "/System/Library/Frameworks/%s.framework/Versions/A/%s" % (name, name),
        "/System/Library/Frameworks/%s.framework/%s" % (name, name),
    ]
    found = ctypes.util.find_library(name)
    if found:
        candidates.insert(0, found)
    for path in candidates:
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    raise Denied("macos_framework_unavailable")


class _Prototypes:
    """一次性声明全部原型；指针类返回值统一 c_void_p。任一点缺失即 Denied。"""

    def __init__(self):
        self.cf = _framework("CoreFoundation")
        self.cg = _framework("CoreGraphics")
        self.ax = _framework("ApplicationServices")
        void = ctypes.c_void_p
        uint32 = ctypes.c_uint32
        sint32 = ctypes.c_int32
        cbool = ctypes.c_bool
        ssize = ctypes.c_ssize_t
        # ---- CoreFoundation ----
        self.cf.CFRelease.argtypes = (void,)
        self.cf.CFAutorelease.argtypes = (void,)
        self.cf.CFRetain.restype = void
        self.cf.CFRetain.argtypes = (void,)
        self.cf.CFGetTypeID.restype = ctypes.c_ulong
        self.cf.CFGetTypeID.argtypes = (void,)
        for name in ("CFStringGetTypeID", "CFNumberGetTypeID", "CFBooleanGetTypeID",
                     "CFArrayGetTypeID", "CFDictionaryGetTypeID", "CFDataGetTypeID"):
            getattr(self.cf, name).restype = ctypes.c_ulong
        self.cf.CFStringGetLength.restype = sint32
        self.cf.CFStringGetLength.argtypes = (void,)
        self.cf.CFStringGetMaximumSizeForEncoding.restype = ssize
        self.cf.CFStringGetMaximumSizeForEncoding.argtypes = (sint32, ctypes.c_ulong)
        self.cf.CFStringGetCString.restype = cbool
        self.cf.CFStringGetCString.argtypes = (void, ctypes.c_char_p, ssize, ctypes.c_ulong)
        self.cf.CFStringCreateWithCString.restype = void
        self.cf.CFStringCreateWithCString.argtypes = (void, ctypes.c_char_p, ctypes.c_ulong)
        self.cf.CFNumberGetInt32Value.restype = cbool
        self.cf.CFNumberGetInt32Value.argtypes = (void, ctypes.POINTER(sint32))
        self.cf.CFBooleanGetValue.restype = cbool
        self.cf.CFBooleanGetValue.argtypes = (void,)
        self.cf.CFArrayGetCount.restype = ctypes.c_long
        self.cf.CFArrayGetCount.argtypes = (void,)
        self.cf.CFArrayGetValueAtIndex.restype = void
        self.cf.CFArrayGetValueAtIndex.argtypes = (void, ctypes.c_long)
        self.cf.CFDictionaryGetValue.restype = void
        self.cf.CFDictionaryGetValue.argtypes = (void, void)
        self.cf.CFDataGetLength.restype = ctypes.c_long
        self.cf.CFDataGetLength.argtypes = (void,)
        self.cf.CFDataGetBytePtr.restype = void
        self.cf.CFDataGetBytePtr.argtypes = (void,)
        self.cf.CFMachPortCreateRunLoopSource.restype = void
        self.cf.CFMachPortCreateRunLoopSource.argtypes = (void, void, ctypes.c_long)
        self.cf.CFRunLoopGetCurrent.restype = void
        self.cf.CFRunLoopAddSource.argtypes = (void, void, void)
        self.cf.CFRunLoopRun.restype = None
        self.cf.CFRunLoopStop.argtypes = (void,)
        # ---- ApplicationServices（Accessibility） ----
        self.ax.AXIsProcessTrusted.restype = cbool
        self.ax.AXUIElementCreateApplication.restype = void
        self.ax.AXUIElementCreateApplication.argtypes = (ctypes.c_int,)
        self.ax.AXUIElementCreateSystemWide.restype = void
        self.ax.AXUIElementCopyAttributeValue.restype = sint32
        self.ax.AXUIElementCopyAttributeValue.argtypes = (void, void, ctypes.POINTER(void))
        self.ax.AXUIElementCopyAttributeValues.restype = sint32
        self.ax.AXUIElementCopyAttributeValues.argtypes = (void, void, ctypes.c_long,
                                                          ctypes.c_long, ctypes.POINTER(void),
                                                          ctypes.POINTER(ctypes.c_long))
        self.ax.AXUIElementSetAttributeValue.restype = sint32
        self.ax.AXUIElementSetAttributeValue.argtypes = (void, void, void)
        self.ax.AXUIElementPerformAction.restype = sint32
        self.ax.AXUIElementPerformAction.argtypes = (void, void)
        self.ax.AXUIElementGetPid.restype = sint32
        self.ax.AXUIElementGetPid.argtypes = (void, ctypes.POINTER(ctypes.c_int))
        self.ax.AXUIElementGetWindow.restype = sint32
        self.ax.AXUIElementGetWindow.argtypes = (void, ctypes.POINTER(uint32))
        # ---- CoreGraphics ----
        self.cg.CGWindowListCopyWindowInfo.restype = void
        self.cg.CGWindowListCopyWindowInfo.argtypes = (uint32, uint32)
        self.cg.CGWindowListCreateImage.restype = void
        self.cg.CGWindowListCreateImage.argtypes = (CGRect, uint32, uint32, uint32)
        self.cg.CGImageGetWidth.restype = ctypes.c_size_t
        self.cg.CGImageGetHeight.restype = ctypes.c_size_t
        self.cg.CGImageGetBytesPerRow.restype = ctypes.c_size_t
        self.cg.CGImageGetBitsPerPixel.restype = ctypes.c_size_t
        for name in ("CGImageGetWidth", "CGImageGetHeight", "CGImageGetBytesPerRow",
                     "CGImageGetBitsPerPixel", "CGImageGetDataProvider"):
            getattr(self.cg, name).argtypes = (void,)
        self.cg.CGImageGetDataProvider.restype = void
        self.cg.CGDataProviderCopyData.restype = void
        self.cg.CGDataProviderCopyData.argtypes = (void,)
        self.cg.CGSessionCopyCurrentDictionary.restype = void
        # 屏幕录制权限预检是 10.15 才加入的符号；老系统缺失不能让原型声明整体失败。
        if hasattr(self.cg, "CGPreflightScreenCaptureAccess"):
            self.cg.CGPreflightScreenCaptureAccess.restype = cbool
        self.cg.CGEventSourceCreate.restype = void
        self.cg.CGEventSourceCreate.argtypes = (ctypes.c_int,)
        self.cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        self.cg.CGEventSourceSecondsSinceLastEventType.argtypes = (void, uint32)
        self.cg.CGEventCreate.restype = void
        self.cg.CGEventCreate.argtypes = (void,)
        self.cg.CGEventCreateMouseEvent.restype = void
        self.cg.CGEventCreateMouseEvent.argtypes = (void, uint32, CGPoint, uint32)
        self.cg.CGEventCreateKeyboardEvent.restype = void
        self.cg.CGEventCreateKeyboardEvent.argtypes = (void, ctypes.c_ushort, cbool)
        self.cg.CGEventCreateScrollWheelEvent.restype = void
        self.cg.CGEventCreateScrollWheelEvent.argtypes = (void, uint32, uint32, ctypes.c_int)
        self.cg.CGEventKeyboardSetUnicodeString.restype = None
        self.cg.CGEventKeyboardSetUnicodeString.argtypes = (void, ctypes.c_int, ctypes.c_void_p)
        self.cg.CGEventSetFlags.restype = None
        self.cg.CGEventSetFlags.argtypes = (void, ctypes.c_ulong)
        self.cg.CGEventSetIntegerValueField.restype = None
        self.cg.CGEventSetIntegerValueField.argtypes = (void, ctypes.c_int, ctypes.c_int64)
        self.cg.CGEventGetIntegerValueField.restype = ctypes.c_int64
        self.cg.CGEventGetIntegerValueField.argtypes = (void, ctypes.c_int)
        self.cg.CGEventGetFlags.restype = ctypes.c_ulong
        self.cg.CGEventGetFlags.argtypes = (void,)
        self.cg.CGEventPost.restype = None
        self.cg.CGEventPost.argtypes = (uint32, void)
        self.cg.CGEventTapCreate.restype = void
        self.cg.CGEventTapCreate.argtypes = (uint32, uint32, uint32, ctypes.c_ulong,
                                             ctypes.c_void_p, void)
        self.cg.CGEventTapEnable.restype = None
        self.cg.CGEventTapEnable.argtypes = (void, cbool)


TAP_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p)


class AX:
    """一台机器一个实例；调用点全部经 CoreGraphics/AX 实测，失败就如实返回。"""

    def __init__(self, libs=None):
        self.libs = libs or _Prototypes()
        self.lock = threading.RLock()
        self._source = self.libs.cg.CGEventSourceCreate(SOURCE_COMBINED_SESSION)
        self._tap = None
        self._tap_runloop = None
        self._tap_thread = None
        self._tap_source = None
        self._callback = None
        self._hit = threading.Event()
        self._pump_failed = False
        # 热键没跑起来时 grab_hotkey 要能区分“还在等”和“线程已失败”。
        self._pump_ready = threading.Event()

    # ---------------- CF/AX 基础 ----------------
    def _string(self, ref):
        cf = self.libs.cf
        if not ref or cf.CFGetTypeID(int(ref)) != cf.CFStringGetTypeID():
            return ""
        length = int(cf.CFStringGetLength(ref))
        size = int(cf.CFStringGetMaximumSizeForEncoding(length, CF_STRING_UTF8)) + 1
        buffer = ctypes.create_string_buffer(size)
        if not cf.CFStringGetCString(ref, buffer, size, CF_STRING_UTF8):
            return ""
        return buffer.value.decode("utf-8", "replace")

    def _new_string(self, value):
        return self.libs.cf.CFStringCreateWithCString(None, value.encode("utf-8"),
                                                     CF_STRING_UTF8)

    def _number(self, ref):
        cf = self.libs.cf
        if not ref or cf.CFGetTypeID(int(ref)) != cf.CFNumberGetTypeID():
            return None
        holder = ctypes.c_int32(0)
        if not cf.CFNumberGetInt32Value(ref, ctypes.byref(holder)):
            return None
        return int(holder.value)

    def _dict_value(self, ref, key):
        cf = self.libs.cf
        if not ref or cf.CFGetTypeID(int(ref)) != cf.CFDictionaryGetTypeID():
            return None
        name = self._new_string(key)
        try:
            return cf.CFDictionaryGetValue(ref, name)
        finally:
            cf.CFRelease(name)

    def _release(self, ref):
        if ref:
            self.libs.cf.CFRelease(ref)

    def _copy_attribute(self, element, name):
        """AX 的 Copy 语义：返回值由调用方 CFRelease。"""
        cf = self.libs.cf
        key = self._new_string(name)
        value = ctypes.c_void_p()
        try:
            code = self.libs.ax.AXUIElementCopyAttributeValue(element, key, ctypes.byref(value))
        finally:
            cf.CFRelease(key)
        if code != AX_SUCCESS or not value.value:
            return None
        return value

    def _boolean(self, name):
        """kCFBooleanTrue/False 是 CoreFoundation 导出的单例，按符号取地址。"""
        try:
            return ctypes.c_void_p.in_dll(self.libs.cf, name)
        except ValueError:
            return None

    def _set_flag(self, element, name, boolean=True):
        value = self._boolean("kCFBooleanTrue" if boolean else "kCFBooleanFalse")
        if value is None:
            return False
        key = self._new_string(name)
        try:
            return self.libs.ax.AXUIElementSetAttributeValue(element, key, value) == AX_SUCCESS
        finally:
            self._release(key)

    def _perform(self, element, action):
        key = self._new_string(action)
        try:
            return self.libs.ax.AXUIElementPerformAction(element, key) == AX_SUCCESS
        finally:
            self._release(key)

    def _window_id(self, element):
        holder = ctypes.c_uint32(0)
        if self.libs.ax.AXUIElementGetWindow(element, ctypes.byref(holder)) != AX_SUCCESS:
            return -1
        return int(holder.value)

    # ---------------- 权限 ----------------
    def trusted(self):
        """辅助功能权限：没有它任何注入都到不了应用，必须先实测。"""
        with self.lock:
            return bool(self.libs.ax.AXIsProcessTrusted())

    # ---------------- 窗口 ----------------
    def _rect(self, bounds_ref):
        """kCGWindowBounds 是 {X,Y,Width,Height} 字典，全局坐标原点在左上角。"""
        cf = self.libs.cf
        if not bounds_ref or cf.CFGetTypeID(int(bounds_ref)) != cf.CFDictionaryGetTypeID():
            return None
        values = []
        for key in ("X", "Y", "Width", "Height"):
            number = self._number(self._dict_value(bounds_ref, key))
            if number is None:
                return None
            values.append(number)
        left, top, width, height = values
        return [int(left), int(top), int(left + width), int(top + height)]

    def _window_list(self, limit):
        cg = self.libs.cg
        ref = cg.CGWindowListCopyWindowInfo(WINDOW_LIST_ON_SCREEN | WINDOW_LIST_NO_DESKTOP,
                                           NULL_WINDOW_ID)
        if not ref:
            return []
        cf = self.libs.cf
        out = []
        try:
            for index in range(int(cf.CFArrayGetCount(ref))):
                if len(out) >= limit:
                    break
                item = cf.CFArrayGetValueAtIndex(ref, index)
                if not item:
                    continue
                layer = self._number(self._dict_value(item, "kCGWindowLayer"))
                number = self._number(self._dict_value(item, "kCGWindowNumber"))
                pid = self._number(self._dict_value(item, "kCGWindowOwnerPID"))
                # 只有 layer==0 且有标题的才是可接管的普通应用窗口；Dock/桌面/面板一律排除。
                if layer != NORMAL_WINDOW_LAYER or not number or not pid:
                    continue
                title = self._string(self._dict_value(item, "kCGWindowName"))
                rect = self._rect(self._dict_value(item, "kCGWindowBounds"))
                if not title.strip() or rect is None:
                    continue
                out.append({"hwnd": number, "pid": pid, "title": title,
                            "class_name": self._string(self._dict_value(item, "kCGWindowOwnerName")),
                            "rect": rect})
        finally:
            cf.CFRelease(ref)
        return out

    def windows(self, limit):
        with self.lock:
            return self._window_list(limit)

    def info(self, number):
        number = int(number)
        for item in self._window_list(300):
            if item["hwnd"] == number:
                return item
        raise Denied("desktop_window_missing")

    def _app_element(self, pid):
        return self.libs.ax.AXUIElementCreateApplication(int(pid))

    def _window_element(self, number):
        """按 CGWindowID 找回 AX 窗口元素；调用方负责释放返回值。"""
        ax = self.libs.ax
        for info in self._window_list(300):
            if info["hwnd"] != int(number):
                continue
            app = self._app_element(info["pid"])
            windows = self._copy_attribute(app, "AXWindows")
            self._release(app)
            if not windows:
                return None
            try:
                for index in range(int(self.libs.cf.CFArrayGetCount(windows))):
                    item = self.libs.cf.CFArrayGetValueAtIndex(windows, index)
                    if item and self._window_id(item) == int(number):
                        # 数组元素归数组所有，不能 CFRelease：CFAutorelease 交给回收周期。
                        return self.libs.cf.CFAutorelease(item)
            finally:
                self._release(windows)
        return None

    def focus(self):
        """当前聚焦窗口号；拿不到返回 -1，由调用方判断，不做任何猜测。"""
        system = self.libs.ax.AXUIElementCreateSystemWide()
        app = self._copy_attribute(system, "AXFocusedApplication")
        self._release(system)
        if not app:
            return -1
        try:
            window = self._copy_attribute(app, "AXFocusedWindow") or \
                self._copy_attribute(app, "AXMainWindow")
            if not window:
                return -1
            try:
                return self._window_id(window)
            finally:
                self._release(window)
        finally:
            self._release(app)

    def top_level(self, number):
        try:
            return int(self.info(number)["hwnd"])
        except Exception:
            return -1

    def set_focus(self, number):
        element = self._window_element(int(number))
        if not element:
            return False
        try:
            pid = ctypes.c_int(0)
            if self.libs.ax.AXUIElementGetPid(element, ctypes.byref(pid)) != AX_SUCCESS:
                return False
            app = self._app_element(pid.value)
            try:
                self._set_flag(app, "AXFrontmost")
            finally:
                self._release(app)
            self._set_flag(element, "AXMain")
            self._set_flag(element, "AXFocused")
            self._perform(element, "AXRaise")
            return True
        finally:
            self._release(element)

    # ---------------- 控件树（AX） ----------------
    def _children(self, element, limit=CHILD_LIMIT):
        """AXChildren 的元素归数组所有：想留到数组之外必须自己 CFRetain。"""
        cf = self.libs.cf
        key = self._new_string("AXChildren")
        values = (ctypes.c_void_p * limit)()
        count = ctypes.c_long(0)
        try:
            code = self.libs.ax.AXUIElementCopyAttributeValues(element, key, 0, limit,
                                                              values, ctypes.byref(count))
        finally:
            self._release(key)
        if code != AX_SUCCESS:
            return []
        out = []
        for index in range(min(int(count.value), limit)):
            if values[index]:
                out.append(cf.CFRetain(values[index]))
        return out

    def _attribute_text(self, element, name):
        value = self._copy_attribute(element, name)
        if not value:
            return ""
        try:
            return self._string(value)
        finally:
            self._release(value)

    def _attribute_flag(self, element, name):
        value = self._copy_attribute(element, name)
        if not value:
            return None
        cf = self.libs.cf
        try:
            if cf.CFGetTypeID(int(value)) != cf.CFBooleanGetTypeID():
                return None
            return bool(cf.CFBooleanGetValue(value))
        finally:
            self._release(value)

    def _collect(self, number, limit=ELEMENT_LIMIT):
        """深度优先遍历：顺序稳定，供“下标 + 角色 + 名称”三重校验复用同一条路径。"""
        root = self._window_element(int(number))
        # 解析不到 AX 窗口元素时不能当作“没有控件”：否则会把“AX 完全不可用”误报成
        # “这个应用没有可激活控件”，验收与远端列表都会被假空的结果误导。
        require(root is not None, "desktop_window_ax_unavailable")
        found = []
        owned = [root]
        stack = [(root, 0)]
        try:
            while stack:
                element, depth = stack.pop()
                role = ACTIONABLE_ROLES.get(self._attribute_text(element, "AXRole"))
                if role:
                    name = (self._attribute_text(element, "AXTitle")
                            or self._attribute_text(element, "AXDescription")).strip()
                    # 无名字或已隐藏的控件不进列表：无法核对名字就不能激活它。
                    if name and not self._attribute_flag(element, "AXHidden"):
                        found.append({"index": len(found), "role": role, "name": name[:80],
                                      "enabled": bool(self._attribute_flag(element, "AXEnabled")),
                                      "element": self.libs.cf.CFRetain(element)})
                        if len(found) >= limit:
                            return found
                if depth < ELEMENT_DEPTH:
                    for child in reversed(self._children(element)):
                        owned.append(child)
                        stack.append((child, depth + 1))
            return found
        finally:
            for item in owned:
                self._release(item)

    def list_elements(self, number, limit=ELEMENT_LIMIT):
        """只读遍历：只返回可激活控件的名字，不读值也不读文本内容。"""
        items = self._collect(number, limit)
        try:
            return [{"index": item["index"], "role": item["role"], "name": item["name"],
                     "enabled": item["enabled"]} for item in items]
        finally:
            for item in items:
                self._release(item["element"])

    def invoke_element(self, number, index, role, name):
        """只用 AXPress 激活；不回落成坐标点击，也不读取控件值。"""
        items = self._collect(number, ELEMENT_LIMIT)
        try:
            target = items[int(index)] if 0 <= int(index) < len(items) else None
            require(target is not None, "desktop_element_missing")
            require(target["role"] == role and target["name"] == str(name)[:80],
                    "desktop_element_changed")
            require(self._perform(target["element"], "AXPress"),
                    "desktop_element_not_actionable")
        finally:
            for item in items:
                self._release(item["element"])

    def sensitive_focus(self, number):
        """AX 能读到焦点元素的子角色：AXSecureTextField 就是密码框。

        Linux/X11 拿不到这个信息所以不声明 desktop.screen；macOS 可以，因此这里做真判断。
        """
        element = self._window_element(int(number))
        if not element:
            return False
        try:
            focused = self._copy_attribute(element, "AXFocusedUIElement")
            if not focused:
                return False
            try:
                subrole = self._copy_attribute(focused, "AXSubrole")
                try:
                    return self._string(subrole) == SECURE_SUBROLE
                finally:
                    self._release(subrole)
            finally:
                self._release(focused)
        finally:
            self._release(element)

    # ---------------- 空闲与锁屏 ----------------
    def idle_ms(self):
        if not self._source:
            return -1.0
        seconds = self.libs.cg.CGEventSourceSecondsSinceLastEventType(
            self._source, ANY_INPUT_EVENT)
        if seconds is None or float(seconds) < 0:
            return -1.0
        return float(seconds) * 1000.0

    def lock_state(self):
        """True/False 为实测结果；None 表示会话字典根本读不到（查询不可用）。

        预检要能区分“查了，没锁屏”和“没查成”，否则会把 API 故障当成用户在场。
        """
        ref = self.libs.cg.CGSessionCopyCurrentDictionary()
        if not ref:
            return None
        try:
            value = self._dict_value(ref, "CGSSessionScreenIsLocked")
            if not value:
                return False
            number = self._number(value)
            return None if number is None else bool(number)
        finally:
            self._release(ref)

    def locked(self):
        """锁屏标志读不到就按“用户不在场”处理，与 Windows/Linux 侧同一保守口径。"""
        state = self.lock_state()
        return True if state is None else state

    def capture_allowed(self):
        """屏幕录制权限：10.15+ 有 CGPreflightScreenCaptureAccess 作为权威判定。

        老系统没有这个符号（当年也没有该权限模型）返回 None，由实际采集成败定；
        绝不因为“没报错”就当权限已拿到。
        """
        try:
            preflight = self.libs.cg.CGPreflightScreenCaptureAccess
        except AttributeError:
            return None
        return bool(preflight())

    def tray_available(self):
        # macOS 的 pystray 走 NSStatusItem，不依赖外部托盘宿主。
        return True

    # ---------------- 采集 ----------------
    def capture(self, number, limit=384 * 1024):
        cg = self.libs.cg
        info = self.info(number)
        left, top, right, bottom = info["rect"]
        rect = CGRect(CGPoint(float(left), float(top)),
                      CGSize(float(right - left), float(bottom - top)))
        image = cg.CGWindowListCreateImage(rect, WINDOW_LIST_ON_SCREEN, int(number),
                                          IMAGE_IGNORE_FRAMING)
        if not image:
            raise Denied("desktop_capture_unavailable")
        try:
            width = int(cg.CGImageGetWidth(image))
            height = int(cg.CGImageGetHeight(image))
            stride = int(cg.CGImageGetBytesPerRow(image))
            if width <= 0 or height <= 0 or int(cg.CGImageGetBitsPerPixel(image)) != BITS_PER_PIXEL:
                # 只有 8bit/通道 x4 的布局能可靠转图，其它一律报不可用，不做“看起来对”的猜测。
                raise Denied("desktop_capture_unavailable")
            provider = cg.CGImageGetDataProvider(image)
            if not provider:
                raise Denied("desktop_capture_unavailable")
            data = cg.CGDataProviderCopyData(provider)
            if not data:
                raise Denied("desktop_capture_unavailable")
            try:
                length = int(self.libs.cf.CFDataGetLength(data))
                pointer = self.libs.cf.CFDataGetBytePtr(data)
                if not pointer:
                    raise Denied("desktop_capture_unavailable")
                payload = ctypes.string_at(int(pointer), min(length, stride * height))
            finally:
                self._release(data)
            return encode_frame(payload, width, height, stride, limit=limit)
        finally:
            self._release(image)

    # ---------------- 注入 ----------------
    def _post(self, event):
        if not event:
            raise Denied("macos_injection_unavailable")
        self.libs.cg.CGEventPost(TAP_HID, event)
        self._release(event)
        time.sleep(0.005)

    def click(self, x, y):
        cg = self.libs.cg
        point = CGPoint(float(x), float(y))
        self._post(cg.CGEventCreateMouseEvent(None, MOUSE_MOVED, point, 0))
        down = cg.CGEventCreateMouseEvent(None, MOUSE_LEFT_DOWN, point, 0)
        cg.CGEventSetIntegerValueField(down, FIELD_CLICK_STATE, 1)
        self._post(down)
        up = cg.CGEventCreateMouseEvent(None, MOUSE_LEFT_UP, point, 0)
        cg.CGEventSetIntegerValueField(up, FIELD_CLICK_STATE, 1)
        self._post(up)

    def scroll(self, x, y, amount):
        cg = self.libs.cg
        point = CGPoint(float(x), float(y))
        self._post(cg.CGEventCreateMouseEvent(None, MOUSE_MOVED, point, 0))
        event = cg.CGEventCreateScrollWheelEvent(None, SCROLL_UNIT_LINE, 1, int(amount) * 3)
        self._post(event)

    def _key(self, keycode, down, flags=0):
        event = self.libs.cg.CGEventCreateKeyboardEvent(None, keycode, bool(down))
        if flags and event:
            self.libs.cg.CGEventSetFlags(event, flags)
        self._post(event)

    def press(self, keys):
        """组合键：按住修饰键 → 按主键 → 释放主键 → 反序释放修饰键。"""
        flags = 0
        for item in keys:
            flags |= MODIFIER_FLAGS.get(item, 0)
        key = str(next(item for item in keys if item not in MODIFIER_FLAGS)).lower()
        code = KEYCODES.get(key)
        require(code is not None, "invalid_keys")
        for name in ("ctrl", "alt", "shift"):
            if flags & MODIFIER_FLAGS[name]:
                self._key(MODIFIER_KEYCODES[name], True)
        self._key(code, True, flags)
        self._key(code, False, flags)
        for name in ("shift", "alt", "ctrl"):
            if flags & MODIFIER_FLAGS[name]:
                self._key(MODIFIER_KEYCODES[name], False)

    def type_text(self, value):
        """走 unicode-string 通道，不依赖键盘布局；逐字符成对 down/up。"""
        cg = self.libs.cg
        for char in value:
            units = _utf16_units(char)
            raw = char.encode("utf-16-le")
            payload = (ctypes.c_uint16 * units)()
            for index in range(units):
                payload[index] = int.from_bytes(raw[index * 2:index * 2 + 2], "little")
            down = cg.CGEventCreateKeyboardEvent(None, 0, True)
            up = cg.CGEventCreateKeyboardEvent(None, 0, False)
            if down:
                cg.CGEventKeyboardSetUnicodeString(down, units, payload)
            if up:
                cg.CGEventKeyboardSetUnicodeString(up, units, payload)
            self._post(down)
            self._post(up)

    # ---------------- 全局停止热键 ----------------
    def grab_hotkey(self):
        """listen-only 事件流：只观察用户按键，不改写也不吞掉任何输入。

        需要辅助功能权限；建不起来或跑不起来一律 False，由预检禁用桌面接管（Spec 第 53 条）。
        """
        if self._tap:
            return True

        def proxy(_tap, kind, event, _refcon):
            try:
                if int(kind) == K_KEY_DOWN and self._matched(event):
                    self._hit.set()
            except Exception:
                pass
            return event

        self._callback = TAP_CALLBACK(proxy)
        mask = 1 << K_KEY_DOWN
        tap = self.libs.cg.CGEventTapCreate(TAP_HID, TAP_HEAD_INSERT, TAP_LISTEN_ONLY, mask,
                                           self._callback, None)
        if not tap:
            return False
        self._tap = tap
        self._hit.clear()
        thread = threading.Thread(target=self._pump, name="zops-macos-hotkey", daemon=True)
        thread.start()
        self._tap_thread = thread
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not self._pump_ready.wait(0.05):
            if self._pump_failed:
                break
        if self._pump_failed or self._tap_runloop is None:
            self.ungrab_hotkey()
            return False
        return True

    def _matched(self, event):
        cg = self.libs.cg
        flags = int(cg.CGEventGetFlags(event))
        code = int(cg.CGEventGetIntegerValueField(event, FIELD_KEYCODE))
        return code == HOTKEY_KEYCODE and (flags & HOTKEY_MODIFIERS) == HOTKEY_MODIFIERS

    def _pump(self):
        cf = self.libs.cf
        self._tap_runloop = cf.CFRunLoopGetCurrent()
        source = cf.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        if not source:
            self._pump_failed = True
            self._tap_runloop = None
            self._pump_ready.set()
            return
        self._tap_source = source
        try:
            modes = self._boolean("kCFRunLoopCommonModes")
            if modes is None:
                raise Denied("macos_runloop_mode_unavailable")
            cf.CFRunLoopAddSource(self._tap_runloop, source, modes)
            self.libs.cg.CGEventTapEnable(self._tap, True)
            self._pump_ready.set()
            cf.CFRunLoopRun()
        except Exception:
            self._pump_failed = True
        finally:
            self._pump_ready.set()
            self._tap_runloop = None

    def hotkey_pending(self):
        return self._hit.is_set()

    def ungrab_hotkey(self):
        runloop = self._tap_runloop
        if runloop:
            self.libs.cf.CFRunLoopStop(runloop)
        if self._tap_thread is not None:
            self._tap_thread.join(2)
        if self._tap:
            try:
                self.libs.cg.CGEventTapEnable(self._tap, False)
            except Exception:
                pass
            self._release(self._tap)
        self._tap = None
        self._tap_thread = None
        self._tap_runloop = None
        self._pump_failed = False
        self._pump_ready.clear()

    # ---------------- 自检 ----------------
    def probe(self):
        with self.lock:
            windows = self._window_list(8)
            return {"windows": len(windows),
                    "ax": bool(self.trusted()),
                    "focus": self.focus() >= 0,
                    "idle_ms": int(max(0.0, self.idle_ms())),
                    # 采集权限与锁屏查询都只报实测结果，不给“看起来可用”。
                    "xss": self.lock_state() is not None,
                    "capture": self.capture_allowed() is not False,
                    "hotkey": HOTKEY_TEXT_MAC,
                    "tray": True,
                    "elements": "ax_subrole"}

    def close(self):
        self.ungrab_hotkey()
        if self._tap_source:
            self._release(self._tap_source)
            self._tap_source = None
        if self._source:
            self._release(self._source)
            self._source = None


def encode_frame(payload, width, height, stride, limit=384 * 1024):
    """CGImage 原始字节（小端 BGRA）编成 JPEG，并压到 limit 以内。

    只做“尺寸/质量降级”这一种裁剪：拿不准的像素布局在 capture 里已经先拒了。
    """
    import base64
    import io

    from PIL import Image

    rows = bytearray()
    for index in range(height):
        start = index * stride
        rows += payload[start:start + width * 4]
    if len(rows) < stride * (height - 1) + width * 4:
        # 采集层给的字节不够（CFData 被截断等）：报不可用，不能丢给 PIL 抛 ValueError。
        raise Denied("desktop_capture_unavailable")
    image = Image.frombytes("RGBA", (width, height), bytes(rows), "raw", "BGRA")
    scale = 1.0
    for _ in range(4):
        edge = int(max(width, height) * scale)
        resized = image if scale == 1.0 else image.resize((max(1, int(width * scale)),
                                                          max(1, int(height * scale))))
        stream = io.BytesIO()
        resized.convert("RGB").save(stream, "JPEG", quality=62)
        if len(stream.getvalue()) <= limit or edge <= 400:
            return base64.b64encode(stream.getvalue()).decode()
        scale *= 0.6
    return base64.b64encode(stream.getvalue()).decode()
