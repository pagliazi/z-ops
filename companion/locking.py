"""同一身份/本机通道单实例；进程退出由 OS 释放锁。"""
import contextlib
import os
from .security import Denied, no_links


@contextlib.contextmanager
def instance_lock(path):
    no_links(path, missing_leaf=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Denied("companion_already_running") from None
        else:
            import msvcrt
            os.write(fd, b"0")
            os.lseek(fd, 0, 0)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise Denied("companion_already_running") from None
        yield
    finally:
        os.close(fd)
