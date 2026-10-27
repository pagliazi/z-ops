"""遮蔽 ASGI WebSocket 接入日志中的一次性票据；代理日志仍须在部署层遮蔽。"""

import logging
import re


class TicketRedaction(logging.Filter):
    def filter(self, record):
        def redact(value):
            if isinstance(value, str):
                return re.sub(r"(/api/cu/devices/connect)\?[^\s\"']+", r"\1?ticket=[redacted]", value)
            return value

        # AccessFormatter 仍需要原始五元参数，不能预格式化后清空 args。
        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact(value) for key, value in record.args.items()}
        return True


def install_ticket_redaction():
    for name in ("uvicorn.access", "uvicorn.error", "hypercorn.access", "hypercorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, TicketRedaction) for f in logger.filters):
            logger.addFilter(TicketRedaction())
