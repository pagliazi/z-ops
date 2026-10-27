"""
Z-OPS 飞书通道包 — 按域拆分(2026-08-27 架构整理)

模块地图(依赖单向,无环):
    protocol      协议原语:token 获取/图片上传/Webhook+IM 发送/回复降级/
                  markdown 转换 + 共享 Bot 单例
    nms           监控域:NMS 告警富卡片、群命令、自然语言查询、订阅通道读写
    approval      审批域:destructive 操作审批卡片(同意/拒绝按钮)
    conversation  对话域:帮助卡片、会话锚点/历史、消息处理链路、orchestrator 转发
    actions       卡片按钮回调分发:approval 决策写回 / nms_silence 通道静默
    gateway       事件接收:WS 长连接(含 lark SDK CARD 帧补丁)+ HTTP 接收器
    bot           组合根:FeishuBot(mixin 组合)+ 通知消费主循环 + main()

拆分自原单文件 feishu_bot.py(1788 行);feishu_bot.py 保留为兼容 shim,
启动方式(python3 feishu_bot.py)与 from feishu_bot import FeishuBot 不变。
"""
