"""Django 根路由。"""

from django.urls import include, path

from api.static_site import asset as static_asset
from api.static_site import landing as static_landing

urlpatterns = [
    path("api/", include("api.urls")),
    # 决赛传单落地页（2026-09-19）：/static/ 免鉴权（middleware.PUBLIC_PREFIXES
    # 前缀放行）；APK 等资产走文件名白名单（api/static_site._FILES）
    path("static/", static_landing),
    path("static/<str:filename>", static_asset),
]
