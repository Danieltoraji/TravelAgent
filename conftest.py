"""pytest 全局环境适配（DSH 沙箱对 0o700 目录的只读拦截规避）。

DSH 沙箱拦截**以 ``0o700`` 权限创建的文件目录**：其内部任何「建子目录 /
写文件」都会抛 PermissionError（WinError 5 / Errno 13），事后 ``chmod``
无法逆转；而 ``0o777`` 创建的目录完全正常（任意层可写）。

``tempfile.mkdtemp``（``TemporaryDirectory`` 的基础）恰好用 ``0o700``
创建目录 → 依赖临时目录写盘验证的测试（booking 持久化、ics·markdown 导出、
日志目录创建）在全量时失败。pytest 的 ``tmp_path``（``os.makedirs``，
0o777）不受影响。

修复：在测试进程内把 ``os.mkdir`` 的目录权限恒置 ``0o777``——Windows 上
mode 本无实义（不减读写位），仅用于规避沙箱对 0o700 目录的只读标记；
对 ``TemporaryDirectory``、pytest 内部 ``tmp_path``、业务代码均无行为变化。
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _mkdir_always_0777(monkeypatch) -> None:
    real_mkdir = os.mkdir

    def mkdir(path, mode: int = 0o777, **kwargs) -> None:
        return real_mkdir(path, 0o777, **kwargs)

    monkeypatch.setattr(os, "mkdir", mkdir)