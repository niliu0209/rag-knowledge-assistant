"""认证 API（S2-1）：/api/auth/register|login|logout|me。

薄层：读 JSON（统一错误体风格同 qa 路由）、调用 AuthService、映射
AuthError → 统一错误体；Set-Cookie 下发会话（HttpOnly / SameSite=Lax
/ Secure=settings.cookie_secure——本机 False，S2-3 公开部署 True）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.deps import client_ip, get_auth_service, get_current_user
from app.core.config import get_settings
from app.services.auth import (
    AccountDisabledError,
    AuthError,
    AuthService,
    InvalidCredentialsError,
    InvalidInviteCodeError,
    PasswordValidationError,
    RateLimitedError,
    SESSION_TTL_DAYS,
    UsernameTakenError,
    UsernameValidationError,
)

logger = logging.getLogger(__name__)

_SESSION_COOKIE = "session"

# AuthError 子类 → (HTTP 状态, 错误码)（统一错误体 {error: {code, message}}）
_STATUS_MAP = {
    UsernameValidationError: (400, "invalid_username"),
    PasswordValidationError: (400, "invalid_password"),
    UsernameTakenError: (409, "username_taken"),
    InvalidInviteCodeError: (400, "invalid_invite_code"),
    InvalidCredentialsError: (401, "invalid_credentials"),
    AccountDisabledError: (403, "account_disabled"),
    RateLimitedError: (429, "rate_limited"),
}


async def _json_body(request: Request):
    """读 JSON 请求体；非法 → 422 统一错误体（同 qa 路由风格）。"""
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001——JSON 解析失败返回统一错误体
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "invalid_json", "message": "请求体不是合法 JSON"}},
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "invalid_json", "message": "请求体必须是 JSON 对象"}},
        )
    return body


def _set_session_cookie(response: Response, session_id: str) -> None:
    settings = get_settings()
    response.set_cookie(
        _SESSION_COOKIE,
        session_id,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def create_auth_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/auth/register")
    async def register(request: Request, response: Response):
        body = await _json_body(request)
        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "invalid_username",
                        "message": "用户名和密码必须是字符串",
                    }
                },
            )
        invite_code = body.get("invite_code")
        if invite_code is not None and not isinstance(invite_code, str):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {"code": "invalid_invite_code", "message": "邀请码格式错误"}
                },
            )
        auth = get_auth_service(request)
        try:
            result = auth.register(username, password, invite_code)
        except AuthError as exc:
            status, code = _STATUS_MAP.get(type(exc), (400, "auth_failed"))
            raise HTTPException(
                status_code=status,
                detail={"error": {"code": code, "message": str(exc)}},
            ) from exc
        _set_session_cookie(response, result["session_id"])
        return {"user": result["user"]}

    @router.post("/api/auth/login")
    async def login(request: Request, response: Response):
        body = await _json_body(request)
        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            # 结构与参数错误也走限流后的统一路径（防探测），见 service.login
            username, password = "", ""
        auth = get_auth_service(request)
        key = client_ip(request)
        try:
            result = auth.login(username, password, key)
        except AuthError as exc:
            status, code = _STATUS_MAP.get(type(exc), (401, "invalid_credentials"))
            raise HTTPException(
                status_code=status,
                detail={"error": {"code": code, "message": str(exc)}},
            ) from exc
        _set_session_cookie(response, result["session_id"])
        return {"user": result["user"]}

    @router.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        session_id = request.cookies.get(_SESSION_COOKIE)
        if session_id:
            get_auth_service(request).logout(session_id)
        response.delete_cookie(_SESSION_COOKIE, path="/")
        return {"ok": True}

    @router.get("/api/auth/me")
    def me(user: dict = Depends(get_current_user)):
        return {"user": user}

    return router
