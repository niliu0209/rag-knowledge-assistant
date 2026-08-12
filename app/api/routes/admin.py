"""管理员 API（S2-1）：邀请码管理与用户管理（require_admin 全量保护）。

薄层：读 JSON、调用 AuthService、映射 AuthError → 统一错误体；
权限边界 owner 在 api/deps.require_admin（role=admin 才可进入）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_auth_service, require_admin
from app.services.auth import (
    AuthError,
    AuthService,
    InviteCodeNotFoundError,
    PasswordValidationError,
    UserNotFoundError,
)

logger = logging.getLogger(__name__)

_STATUS_MAP = {
    UserNotFoundError: (404, "user_not_found"),
    InviteCodeNotFoundError: (404, "invite_code_not_found"),
    PasswordValidationError: (400, "invalid_password"),
}


def create_admin_router(data_dir: Path) -> APIRouter:
    router = APIRouter()

    # ---------- 邀请码 ----------

    @router.post("/api/admin/invite-codes")
    def create_invite_code(
        request: Request, admin: dict = Depends(require_admin)
    ):
        auth: AuthService = get_auth_service(request)
        code = auth.create_invite_code(admin["id"])
        return {"code": code}

    @router.get("/api/admin/invite-codes")
    def list_invite_codes(
        request: Request, admin: dict = Depends(require_admin)
    ):
        auth: AuthService = get_auth_service(request)
        return {"invite_codes": auth.list_invite_codes()}

    @router.post("/api/admin/invite-codes/{code}/revoke")
    def revoke_invite_code(
        code: str, request: Request, admin: dict = Depends(require_admin)
    ):
        auth: AuthService = get_auth_service(request)
        try:
            auth.revoke_invite_code(code)
        except AuthError as exc:
            status, err_code = _STATUS_MAP.get(type(exc), (400, "auth_failed"))
            raise HTTPException(
                status_code=status,
                detail={"error": {"code": err_code, "message": str(exc)}},
            ) from exc
        return {"ok": True}

    # ---------- 用户 ----------

    @router.get("/api/admin/users")
    def list_users(request: Request, admin: dict = Depends(require_admin)):
        auth: AuthService = get_auth_service(request)
        return {"users": auth.list_users()}

    @router.post("/api/admin/users/{user_id}/disable")
    def disable_user(
        user_id: str, request: Request, admin: dict = Depends(require_admin)
    ):
        auth: AuthService = get_auth_service(request)
        try:
            auth.set_user_status(user_id, "disabled")
        except AuthError as exc:
            status, err_code = _STATUS_MAP.get(type(exc), (400, "auth_failed"))
            raise HTTPException(
                status_code=status,
                detail={"error": {"code": err_code, "message": str(exc)}},
            ) from exc
        return {"ok": True}

    @router.post("/api/admin/users/{user_id}/enable")
    def enable_user(
        user_id: str, request: Request, admin: dict = Depends(require_admin)
    ):
        auth: AuthService = get_auth_service(request)
        try:
            auth.set_user_status(user_id, "active")
        except AuthError as exc:
            status, err_code = _STATUS_MAP.get(type(exc), (400, "auth_failed"))
            raise HTTPException(
                status_code=status,
                detail={"error": {"code": err_code, "message": str(exc)}},
            ) from exc
        return {"ok": True}

    @router.post("/api/admin/users/{user_id}/reset-password")
    async def reset_password(
        user_id: str, request: Request, admin: dict = Depends(require_admin)
    ):
        body = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001——缺 body 走统一校验路径
            pass
        new_password = body.get("new_password") if isinstance(body, dict) else None
        if not isinstance(new_password, str):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {"code": "invalid_password", "message": "缺少新密码"}
                },
            )
        auth: AuthService = get_auth_service(request)
        try:
            auth.reset_password(user_id, new_password)
        except AuthError as exc:
            status, err_code = _STATUS_MAP.get(type(exc), (400, "auth_failed"))
            raise HTTPException(
                status_code=status,
                detail={"error": {"code": err_code, "message": str(exc)}},
            ) from exc
        return {"ok": True}

    return router
