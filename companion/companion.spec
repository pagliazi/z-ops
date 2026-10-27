# 未签名、本机测试用途；不是生产安装包，不含浏览器二进制或用户配置。
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH)
datas = collect_data_files("playwright")
hidden = collect_submodules("companion") + collect_submodules("keyring.backends")
hidden += ["openpyxl", "docx", "pypdf", "PIL.Image", "aiohttp", "psutil"]
# Windows 的原生批准用 Win32 MessageBox，Linux 用 tkinter；托盘只在 Linux 需要 python-xlib。
if sys.platform.startswith("linux"):
    hidden += ["pystray", "Xlib"]
    excludes = ["pytest"]
else:
    excludes = ["pytest", "tkinter"]
a = Analysis([str(root / "entry.py")], pathex=[str(root.parent)], binaries=[],
             datas=datas, hiddenimports=hidden, hookspath=[], hooksconfig={},
             runtime_hooks=[], excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="companion-unsigned-test",
          debug=False, strip=False, upx=False, console=True, codesign_identity=None)
native = EXE(pyz, a.scripts, [], exclude_binaries=True, name="companion-native-unsigned-test",
             debug=False, strip=False, upx=False, console=True, codesign_identity=None)
coll = COLLECT(cli, native, a.binaries, a.datas, strip=False, upx=False,
               name="companion-unsigned-test")
