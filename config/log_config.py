"""统一日志配置：控制台 + RotatingFileHandler 落盘。

用法::

    from config.log_config import setup_logging
    setup_logging()  # 自动从 settings 读取 log_dir / log_level / ...

日志文件写入 ``{log_dir}/travel_agent.log``，按 ``log_max_bytes`` 滚动，
保留 ``log_backup_count`` 个历史文件。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from config.settings import settings


def setup_logging(
    log_dir: str | None = None,
    level: str | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> None:
    """配置全局日志：控制台 + RotatingFileHandler。

    参数为 None 时从 ``settings`` 读取默认值。
    每次调用都会清除 root logger 上已有的 handler 后重新配置（测试友好）。
    """
    log_dir = log_dir or settings.log_dir
    level = level or settings.log_level
    max_bytes = max_bytes or settings.log_max_bytes
    backup_count = backup_count or settings.log_backup_count

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "travel_agent.log")

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 清除已有 handler，避免重复添加（测试友好）
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()

    # 控制台 handler
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # 文件 handler（滚动）
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("config.log_config").info(
        "日志已配置: console + %s (level=%s, maxBytes=%d, backupCount=%d)",
        log_path, level, max_bytes, backup_count,
    )
