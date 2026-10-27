"""
Z-OPS 运维智能体 — 飞书消息通道(兼容 shim)

2026-08-27 架构整理:原单文件(1787 行)已按域拆分至 feishu/ 包
(协议/监控/审批/对话/回调分发/网关/组合根,详见 feishu/__init__.py 模块地图),
本文件仅保留兼容入口,历史实现备份于 feishu_bot.py.bak-monolith。

兼容性:
  - 启动方式不变: python3 feishu_bot.py(或 systemd/nohup/setsid 均可)
  - from feishu_bot import FeishuBot / handle_card_action / ... 不变
"""

from feishu.bot import FeishuBot, main
from feishu.protocol import shared_bot as _shared_bot
from feishu.actions import handle_card_action
from feishu.conversation import _handle_feishu_message, _HELP_TEXT, _save_session_msg
from feishu.gateway import (create_feishu_receiver, _start_ws_receiver,
                            _patch_ws_card_support)

__all__ = [
    "FeishuBot", "main", "handle_card_action",
    "_handle_feishu_message", "_HELP_TEXT", "_save_session_msg",
    "create_feishu_receiver", "_start_ws_receiver", "_patch_ws_card_support",
    "_shared_bot",
]

if __name__ == "__main__":
    main()
