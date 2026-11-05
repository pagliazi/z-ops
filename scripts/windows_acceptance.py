"""Windows 实机验收清单（Spec 第 82 条）——命令行外壳。

实现已搬进 Companion 包内的 companion/winaccept.py，原因是 Spec 第 12 条要求
"PyInstaller 打包，不要求操作端安装 Python"：到了 Windows 测试机上只带 exe 就行，
用 `companion.exe --windows-acceptance` 直接跑完 13 项，不需要再装 Python、
克隆仓库或补依赖。本脚本保留给源码方式运行与既有文档/测试使用，
两者共用同一份判定逻辑，不存在第二套口径。

逐项口径：
- auto 由代码实测（doctor 声明自洽、全局热键注册）；
- background_* 真实起一次无头浏览器任务，前后对比前台窗口与剪贴板序列号；
- measure 由人触发、由代码测量（取消后子进程 pid 是否真的消失，走 Win32 OpenProcess）；
- manual 由人按提示操作后回答，并必须留下证据，缺证据按未做处理；
- 缺项、未做、未通过，整体结论都是"未通过"，不允许把"没报错"当验收。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion.winaccept import (  # noqa: F401  （对外 re-export，供脚本与测试使用）
    BACKGROUND_STEPS, CANCEL_TIMEOUT, DESKTOP_CAPABILITIES, HOTKEY_ID, HOTKEY_TEXT,
    KINDS, PREFLIGHT_CHECKS, PROCESS_QUERY_LIMITED_INFORMATION, STEPS,
    _windows, ask, background_step, main, markdown, measure_cancel_child, pid_exists,
    probe_auto, probe_background, probe_doctor, probe_hotkey, resolve_companion, run,
    verdict,
)
from companion.winaccept import subprocess  # noqa: F401  （清单内部以模块属性调用外部命令）

if __name__ == "__main__":
    raise SystemExit(main())
