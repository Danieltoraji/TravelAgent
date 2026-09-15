"""请求级日志辅助：request_id ContextVar + LogRecord 注入（server_log 2026-09-15）。

request_id 由 ``api.middleware`` 每请求生成（8 位 hex）存入 ContextVar；
同请求链路上的 api/runtime/a_side logger 经 ``RequestIdFilter`` 统一注入
``record.request_id``，LOGGING 的 formatter 以 ``%(request_id)s`` 打出，
异常可凭同一 id 与请求行互查。无请求上下文（启动期/线程池后台线程）
显示 "-"。

本模块只依赖标准库，可在 dictConfig 阶段（app registry 就绪前）安全导入。
"""

from __future__ import annotations

import contextvars
import logging

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class RequestIdFilter(logging.Filter):
    """把 ContextVar 里的 request_id 注入每条 LogRecord（formatter 引用）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True
