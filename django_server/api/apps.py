from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _set_sqlite_pragmas(sender, connection, **kwargs) -> None:
    """sqlite 连接级 PRAGMA（多用户 review 小项，2026-09）。

    WAL 日志模式：gthread 8 线程 + 每 POST 一次 Trip 整行覆写的场景下，
    WAL 读写不互斥，显著减少写锁等待（原本只靠 OPTIONS timeout=20 兜底）。
    journal_mode 持久化在库文件里，每个新连接重复执行幂等且开销可忽略。
    Django sqlite 后端无 init_command 选项（那是 MySQL 的），故用信号实现。
    """
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL;")


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"

    def ready(self):
        connection_created.connect(_set_sqlite_pragmas)
