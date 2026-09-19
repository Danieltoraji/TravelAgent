"""决赛传单落地页 /static/（2026-09-19，二维码扫码入口）。

- 免鉴权：middleware.PUBLIC_PREFIXES 对 /static/ 前缀放行（扫码用户无 token）；
- 文件名白名单：只服务 _FILES 登记过的文件，杜绝目录穿越与随手挂新文件；
- 页面零外链（会场弱网可用）：无 CDN 字体/图标，全部内联在 index.html。
"""

from __future__ import annotations

import os

from django.http import FileResponse, Http404

# django_server/api/static_site.py → django_server/static_site/
_SITE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static_site"
)

# filename → (content_type, Content-Disposition 的 attachment 文件名；None=inline)
_FILES: dict[str, tuple[str, str | None]] = {
    "index.html": ("text/html; charset=utf-8", None),
    "travel-app-v15.apk": (
        "application/vnd.android.package-archive",
        "travel-app-v15.apk",
    ),
}


def landing(request):
    """落地页本体（/static/ 与 /static/index.html 同一响应）。"""
    path = os.path.join(_SITE_DIR, "index.html")
    return FileResponse(open(path, "rb"), content_type="text/html; charset=utf-8")


def asset(request, filename: str):
    """白名单静态资产（当前仅 APK 安装包）。"""
    entry = _FILES.get(filename)
    if entry is None:
        raise Http404(filename)
    content_type, attachment = entry
    path = os.path.join(_SITE_DIR, filename)
    if not os.path.isfile(path):
        raise Http404(filename)
    response = FileResponse(open(path, "rb"), content_type=content_type)
    if attachment:
        response["Content-Disposition"] = f'inline; filename="{attachment}"'
    return response
