"""日志配置测试：setup_logging 落盘 + 配置项。"""

import logging
import os
import tempfile
import unittest

from config.settings import settings


def _close_all_handlers():
    """关闭 root logger 上所有 handler，释放文件锁（Windows 友好）。"""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()


class TestLogging(unittest.TestCase):
    """setup_logging 测试。"""

    def tearDown(self):
        _close_all_handlers()

    def test_setup_logging_creates_log_dir(self):
        """setup_logging 应创建日志目录。"""
        from config.log_config import setup_logging

        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = os.path.join(tmpdir, "test_logs")
            setup_logging(
                log_dir=log_dir,
                level="DEBUG",
                max_bytes=1024,
                backup_count=2,
            )
            self.assertTrue(os.path.isdir(log_dir))
            self.assertTrue(os.path.exists(os.path.join(log_dir, "travel_agent.log")))
            _close_all_handlers()

    def test_setup_logging_writes_to_file(self):
        """日志应写入文件。"""
        from config.log_config import setup_logging

        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = os.path.join(tmpdir, "test_logs2")
            setup_logging(
                log_dir=log_dir,
                level="DEBUG",
                max_bytes=1024,
                backup_count=2,
            )

            test_logger = logging.getLogger("test.logging")
            test_logger.info("test log message 12345")

            # flush all handlers
            for handler in logging.getLogger().handlers:
                handler.flush()

            log_path = os.path.join(log_dir, "travel_agent.log")
            with open(log_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("test log message 12345", content)
            _close_all_handlers()

    def test_settings_has_log_config_fields(self):
        """Settings 应包含 M5 日志配置字段。"""
        self.assertTrue(hasattr(settings, "api_timeout"))
        self.assertTrue(hasattr(settings, "max_retries"))
        self.assertTrue(hasattr(settings, "retry_backoff_base"))
        self.assertTrue(hasattr(settings, "log_dir"))
        self.assertTrue(hasattr(settings, "log_level"))
        self.assertTrue(hasattr(settings, "log_max_bytes"))
        self.assertTrue(hasattr(settings, "log_backup_count"))

    def test_settings_reload(self):
        """Settings.reload() 应能重新加载配置（不抛异常）。"""
        # reload 会从环境变量 + local_settings.py 重新读取
        # 我们只验证调用不抛异常，且 amap_api_key 是字符串类型
        settings.reload()
        self.assertIsInstance(settings.amap_api_key, str)
        self.assertIsInstance(settings.qweather_api_key, str)


if __name__ == "__main__":
    unittest.main()
