"""X11 验收目标窗口：真实 GUI 客户端，补齐 _NET_WM_PID/WM_CLASS 供接管路径核对。

Companion 只按进程名授权桌面应用，而 Tk 不写 `_NET_WM_PID`；这个脚本给窗口补上该属性，
并把手动操作与注入事件全部落盘，用来证明"事件真的到达了应用"而不是只到达 X 服务器。
"""
import argparse
import ctypes
import ctypes.util
import os
import tkinter as tk

Display = ctypes.c_void_p
Window = ctypes.c_ulong
Atom = ctypes.c_ulong
XA_CARDINAL = 6
PROP_MODE_REPLACE = 0


class XClassHint(ctypes.Structure):
    _fields_ = [("res_name", ctypes.c_char_p), ("res_class", ctypes.c_char_p)]


def _lib():
    name = ctypes.util.find_library("X11") or "libX11.so.6"
    lib = ctypes.CDLL(name)
    lib.XOpenDisplay.restype = Display
    lib.XOpenDisplay.argtypes = (ctypes.c_char_p,)
    lib.XDefaultRootWindow.restype = Window
    lib.XDefaultRootWindow.argtypes = (Display,)
    lib.XQueryTree.argtypes = (Display, Window, ctypes.POINTER(Window), ctypes.POINTER(Window),
                               ctypes.POINTER(ctypes.POINTER(Window)), ctypes.POINTER(ctypes.c_uint))
    lib.XInternAtom.restype = Atom
    lib.XInternAtom.argtypes = (Display, ctypes.c_char_p, ctypes.c_int)
    lib.XChangeProperty.argtypes = (Display, Window, Atom, Atom, ctypes.c_int, ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_ulong), ctypes.c_int)
    lib.XSetClassHint.argtypes = (Display, Window, ctypes.POINTER(XClassHint))
    lib.XFlush.argtypes = (Display,)
    lib.XFree.argtypes = (ctypes.c_void_p,)
    return lib


def _children(lib, dpy, window):
    root = Window()
    parent = Window()
    items = ctypes.POINTER(Window)()
    count = ctypes.c_uint()
    if not lib.XQueryTree(dpy, window, ctypes.byref(root), ctypes.byref(parent),
                          ctypes.byref(items), ctypes.byref(count)):
        return []
    try:
        return [int(items[index]) for index in range(count.value)]
    finally:
        if items:
            lib.XFree(ctypes.cast(items, ctypes.c_void_p))


def top_level_of(lib, dpy, xid):
    """winfo_id() 给的是内部窗口；一直上溯到 root 的直接子窗口才是真正的顶层。"""
    root = lib.XDefaultRootWindow(dpy)
    current = int(xid)
    for _ in range(16):
        parent = None
        for candidate in _children(lib, dpy, root):
            if current in _children(lib, dpy, candidate):
                parent = candidate
        if parent is None:
            return current
        current = parent
    return current


def label_window(window, klass):
    """给窗口补 _NET_WM_PID 与 WM_CLASS；这两项就是 Companion 的授权依据。"""
    lib = _lib()
    dpy = lib.XOpenDisplay(None)
    if not dpy:
        raise SystemExit("x11_display_unavailable")
    target = top_level_of(lib, dpy, window)
    pid = ctypes.c_ulong(os.getpid())
    atom = lib.XInternAtom(dpy, b"_NET_WM_PID", 0)
    lib.XChangeProperty(dpy, Window(target), atom, XA_CARDINAL, 32, PROP_MODE_REPLACE,
                        ctypes.byref(pid), 1)
    hint = XClassHint(klass.encode(), klass.encode())
    lib.XSetClassHint(dpy, Window(target), ctypes.byref(hint))
    lib.XFlush(dpy)
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default="/tmp/zops-x11-events.log")
    parser.add_argument("--title", default="Z-OPS 验收窗口")
    parser.add_argument("--class-name", default="ZopsTarget")
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()

    def record(line):
        with open(args.log, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()

    root = tk.Tk()
    root.title(args.title)
    root.geometry("480x260+80+60")
    tk.Label(root, text="Z-OPS 桌面适配验收").pack()
    tk.Button(root, text="确认动作", command=lambda: record("BUTTON_COMMAND")).pack()
    entry = tk.Entry(root)
    entry.pack()
    root.bind("<Key>", lambda event: record("KEY " + repr(event.char) + " " + event.keysym))
    root.bind("<Button-1>", lambda event: record("CLICK %d %d" % (event.x, event.y)))
    root.update()
    xid = label_window(root.winfo_id(), args.class_name)
    record("READY pid=%d xid=%d log=%s" % (os.getpid(), xid, args.log))
    root.after(max(1, args.seconds) * 1000, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
