"""PyInstaller 专用入口；产物仅用于本机未签名测试。"""
import os
import sys

if "native" in os.path.basename(sys.executable).lower():
    from companion.native_host import main
else:
    from companion.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
