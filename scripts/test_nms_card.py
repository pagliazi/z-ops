"""验证：飞书图片上传（image_key）+ NMS 卡片构造（不实际推送）。"""
import asyncio
import json
import sys

sys.path.insert(0, "/opt/zops")

import config
from feishu_bot import FeishuBot


async def main():
    bot = FeishuBot()
    # 1) 图片上传测试（取最近一张截图）
    import os
    shot_dir = config.BASE_DIR / "webchat_frontend" / "nms_shots"
    files = sorted(shot_dir.glob("*.png"), key=os.path.getmtime, reverse=True)
    if not files:
        print("FAIL: nms_shots 无截图文件")
        return
    f = files[0]
    print(f"[1] 上传图片: {f.name} ({f.stat().st_size} bytes)")
    key = await bot._upload_image(str(f))
    print(f"[1] image_key: {key or 'FAILED(空)'}")

    # 2) 卡片构造测试
    extra = {
        "time": "2026-08-22 23:30:00",
        "total": 57062, "new": 2139,
        "severity": {"critical": 282, "major": 27038, "minor": 18895, "warning": 5786},
        "top_groups": [{"name": "GCOC_use", "count": 57062}],
        "checklists": [{"name": "Zerto VPG Journal空间不足", "count": 14},
                       {"name": "存储控制器故障", "count": 7}],
        "push_groups": ["GCOC_use"],
        "console_url": "https://nms.example.com:16311/ibm/console/",
        "analysis": "测试分析摘要",
        "incidents": [
            {"severity": "major", "category": "系统级",
             "title": "BGP Peer Connection Idle (BGP Peer: 198.19.218.166)",
             "members": 12555, "sources": 392, "unacked": 11217, "new": 1,
             "last_time": "9/9/25, 1:59 PM"},
            {"severity": "critical", "category": "系统级",
             "title": "ESXI host.net.droppedRX (hostname=198.18.153.110)",
             "members": 5, "sources": 5, "unacked": 5, "new": 1,
             "last_time": "8/20/26, 5:30 AM"},
        ],
        "new_alarms": [],
        "shots": [
            {"group": "GCOC_use", "severity": "", "unack": True,
             "file": f.name, "url": "https://gcoc-ai.example.com/static/nms_shots/" + f.name},
            {"group": "GCOC_use", "severity": "critical", "unack": True,
             "file": "", "url": "https://example.com/x.png"},
        ],
    }
    payload = await bot._build_nms_card(extra)
    print("[2] 卡片 payload:")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:3000])


asyncio.run(main())
