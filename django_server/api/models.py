"""多用户数据模型（2026-09 多用户改造，方案见 docs/sync_notes_multiuser_for_ac_*.md）。

- ``AuthToken``：Bearer token（只存 sha256，不存明文）；单设备语义——
  注册/登录都轮换旧 token，同账号同时只有一个有效 token。
- ``Trip``：每用户当前行程的持久化快照，六个 JSON blob 整体覆写。
  进程内 AgentRuntime/MockWorld 等运行时对象不序列化：重启后由
  ``runtime.manager.UserRuntimeManager`` 按本表懒重建（tool_call_log/
  hotel 缓存/MockWorld 注入等演示态不持久化，丢失无碍）。

注意：本模块只能在 Django settings 就绪后 import（ORM），
api/auth.py 与 api/middleware.py 一律函数级延迟导入。
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import models


class AuthToken(models.Model):
    """API 访问令牌（key 明文只在签发响应里出现一次）。"""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    # unique 自带索引，不再显式 db_index（review P3）
    key_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"token:{self.user_id}"


class Trip(models.Model):
    """每用户当前行程快照（新 plan 整行覆写，与单用户时代"新会话清空"语义一致）。

    blob 内容由 ``AgentRuntime.snapshot()`` 产出（多用户 M2）：
    - requirement：A 侧结构化需求 dict（POST /api/plan/ 提交体）
    - timeline：TripTimeline.to_dict()（None = 尚未规划）
    - events：List[MonitorEvent.to_dict()]（C 的 since 游标基于列表长度，
      恢复后游标语义延续）
    - replans / timeline_history：既有内存态原样（dict 列表）
    - booking_state：BookingManager.snapshot()（records + actions）
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="trip")
    requirement = models.JSONField(null=True, blank=True)
    timeline = models.JSONField(null=True, blank=True)
    events = models.JSONField(default=list, blank=True)
    replans = models.JSONField(default=list, blank=True)
    timeline_history = models.JSONField(default=list, blank=True)
    booking_state = models.JSONField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "用户行程"
        verbose_name_plural = "用户行程"

    def __str__(self) -> str:  # pragma: no cover
        return f"trip:{self.user_id}"
