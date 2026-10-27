"""测试 NMS 推送频率控制命令逻辑（直接调用 _handle_nms_command）。"""
import asyncio
import sys

sys.path.insert(0, "/opt/zops")

import config
from feishu_bot import FeishuBot


async def main():
    bot = FeishuBot()
    r = await bot.get_redis()
    # 备份当前动态配置，测试后恢复
    backup = await r.get(config.KEY_NMS_DYN_CFG)
    cmds = [
        "nms 推送",
        "nms 推送 关",
        "nms 推送",
        "nms 推送间隔 120",
        "nms 推送",
        "nms 静默 2",
        "nms 推送",
        "nms 推送间隔 0",
        "nms 推送",
        "nms 推送 开",
        "nms 推送",
    ]
    for cmd in cmds:
        print(f">>> {cmd}")
        out = await bot._handle_nms_command(cmd, "", "")
        print(out)
        print("=" * 50)
    # 恢复原配置
    if backup:
        await r.set(config.KEY_NMS_DYN_CFG, backup)
        print("已恢复原动态配置")
    await r.aclose()


asyncio.run(main())
