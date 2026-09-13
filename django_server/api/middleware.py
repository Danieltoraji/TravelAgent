"""Bearer token → 每用户运行时中间件（2026-09 多用户改造 M1）。

- 白名单（health / register / login）直接放行；
- 其余请求：无/无效 token → 401 JSON（C 端 fetchJSON 会以 error 抛出）；
  有效 → ``request.auth_user`` + ``request.runtime``（UserRuntimeManager
  懒创建/触碰）。
- POST 响应后触发一次 Trip 快照落库：所有业务写入口（plan/timeline/chat/
  poll/lookahead/inject/booking/actions）都是 POST，这里是零侵入的持久化
  收口点（尽力而为，失败只记日志）。

模型一律函数级延迟导入（见 api/auth.py 同款约束）。
"""

from __future__ import annotations

import logging

from django.http import JsonResponse

from api.auth import resolve_bearer
from runtime.manager import manager as runtime_manager

logger = logging.getLogger("api.middleware")

PUBLIC_PATHS = {
    "/api/health/",
    "/api/auth/register/",
    "/api/auth/login/",
}


class TokenAuthRuntimeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 白名单归一（review P3）：/api/health（无尾斜杠）也放行——否则会在
        # 中间件层 401，走不到 CommonMiddleware 的 APPEND_SLASH 重定向
        path = request.path
        if not path.endswith("/"):
            path = path + "/"
        if path not in PUBLIC_PATHS:
            try:
                user = resolve_bearer(request)
            except Exception:  # noqa: BLE001  DB 异常按未认证处理（fail-closed）
                logger.exception("token resolve failed")
                user = None
            if user is None:
                return JsonResponse(
                    {"error": "unauthorized（请先 POST /api/auth/login/ 获取 token，"
                              "请求带 Authorization: Bearer <token> 头）"},
                    status=401,
                )
            request.auth_user = user
            request.runtime = runtime_manager.get(user.id)

        response = self.get_response(request)

        if request.method == "POST" and getattr(request, "auth_user", None) is not None:
            try:
                runtime_manager.persist(request.auth_user.id)
            except Exception:  # noqa: BLE001
                logger.exception("post-request trip persist failed")
        return response
