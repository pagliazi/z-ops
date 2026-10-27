"""一次性补丁: 小窗运行态文案 (执行端 = 用户本机浏览器)"""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "web-next/src/components/chat/BrowserLivePanel.tsx"
s = p.read_text(encoding="utf-8")

pairs = [
    ("AI 正在服务器浏览器中执行…", "AI 正在浏览器中执行…"),
    ("首帧马上到。画面只读推送，你的电脑和浏览器不会被占用。",
     "首帧马上到。工作窗口是独立的，不影响你当前页面。"),
]
for old, new in pairs:
    if old in s:
        s = s.replace(old, new)
        print("OK  ", old[:24])
    else:
        print("SKIP", old[:24])
p.write_text(s, encoding="utf-8")
print("done", len(s))
