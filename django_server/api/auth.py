"""Bearer token 认证与账号端点（2026-09 多用户改造 M1）。

- 签发：``secrets.token_urlsafe(32)``，库里只存 sha256；
- 传输：``Authorization: Bearer <key>``——不用 cookie/session，规避 CSRF
  与 Capacitor 明文 http 的 cookie 问题（全部端点维持 @csrf_exempt 现状）；
- 单设备：注册/登录轮换旧 token（同账号同时只有一个有效 token，与演示
  语义一致；多设备并存是后续路线）。

端点：register/login 免认证（中间件白名单）；me/logout 依赖中间件解析出的
``request.auth_user``。模型一律函数级延迟导入（保证本模块可在 settings
就绪前被 import，与既有测试的裸 import 方式兼容）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import logging
from typing import Any, Dict, Optional, Tuple

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

logger = logging.getLogger("api.auth")

MIN_PASSWORD_LEN = 6


def _json_body(request: HttpRequest) -> Dict[str, Any]:
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def issue_token(user: User) -> str:
    """签发新 token 并轮换掉该用户的旧 token（单设备语义）。

    review P3（2026-09）：delete+create 包事务——并发登录时轮换与签发
    原子生效，避免短暂出现两个并存 token 打破单设备语义。
    """
    from django.db import transaction

    from api.models import AuthToken

    with transaction.atomic():
        AuthToken.objects.filter(user=user).delete()
        key = secrets.token_urlsafe(32)
        AuthToken.objects.create(user=user, key_hash=_hash(key))
    return key


def resolve_bearer(request: HttpRequest) -> Optional[User]:
    """``Authorization: Bearer <key>`` → User；缺失/无效返回 None。

    供中间件与需要自解析的场景共用；last_seen 节流 60s 写一次
    （轮询每 5s 一次，不节流会把 sqlite 写放大 5 倍）。
    """
    from django.utils import timezone

    from api.models import AuthToken

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    key = header[len("Bearer "):].strip()
    if not key:
        return None
    token = (
        AuthToken.objects.select_related("user")
        .filter(key_hash=_hash(key))
        .first()
    )
    if token is None:
        return None
    if not token.user.is_active:
        return None   # review P3：停用用户的 token 一律无效（纯防御，当前无停用入口）
    try:
        if token.last_seen_at is None or (
            timezone.now() - token.last_seen_at
        ).total_seconds() > 60:
            token.save(update_fields=["last_seen_at"])
    except Exception:  # noqa: BLE001  last_seen 更新失败不影响鉴权
        logger.debug("token last_seen update failed", exc_info=True)
    return token.user


@csrf_exempt
@require_http_methods(["POST"])
def register(request: HttpRequest) -> JsonResponse:
    """注册并直接登录：``POST /api/auth/register/`` {username, password}。"""
    payload = _json_body(request)
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        return _error("username 和 password 必填")
    if len(password) < MIN_PASSWORD_LEN:
        return _error(f"password 至少 {MIN_PASSWORD_LEN} 位")
    if User.objects.filter(username=username).exists():
        return _error("用户名已存在", status=409)
    try:
        user = User.objects.create_user(username=username, password=password)
    except IntegrityError:
        # review P3：exists() 检查与 create_user 之间的 TOCTOU 竞态——
        # 并发同名注册在此处落入唯一约束，映射 409 固定文案（不泄漏异常）
        return _error("用户名已存在", status=409)
    except Exception:  # noqa: BLE001
        logger.exception("register failed for %s", username)
        return _error("注册失败（服务端内部错误）", status=500)
    return JsonResponse({
        "status": "ok",
        "token": issue_token(user),
        "username": user.username,
    })


@csrf_exempt
@require_http_methods(["POST"])
def login(request: HttpRequest) -> JsonResponse:
    """登录：``POST /api/auth/login/`` {username, password} → 新 token。"""
    payload = _json_body(request)
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        return _error("username 和 password 必填")
    user = User.objects.filter(username=username).first()
    if user is None or not user.check_password(password):
        return _error("用户名或密码错误", status=401)
    return JsonResponse({
        "status": "ok",
        "token": issue_token(user),
        "username": user.username,
    })


@require_http_methods(["GET"])
def me(request: HttpRequest) -> JsonResponse:
    """当前用户信息（需 token）：用户名 + 服务端是否已有行程。"""
    user = getattr(request, "auth_user", None)
    if user is None:
        return _error("unauthorized", status=401)
    from api.models import Trip

    trip = Trip.objects.filter(user=user).first()
    return JsonResponse({
        "username": user.username,
        "user_id": user.id,
        "has_plan": bool(trip and trip.timeline),
    })


@csrf_exempt
@require_http_methods(["POST"])
def logout(request: HttpRequest) -> JsonResponse:
    """注销：删除当前 token（需 token）。"""
    user = getattr(request, "auth_user", None)
    if user is None:
        return _error("unauthorized", status=401)
    from api.models import AuthToken

    AuthToken.objects.filter(user=user).delete()
    return JsonResponse({"status": "ok"})
