"""认证依赖（S2-1）：get_current_user / require_admin / client_ip。

会话 cookie 解析在 api 层（cookie 由 auth 路由下发）；身份判定
owner 在 AuthService.resolve_session；本文件只做 cookie → 用户 的
解析与 401/403 映射，不拥有业务规则。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app.services.auth import AuthService


def get_auth_service(request: Request) -> AuthService:
    """AuthService 实例挂在 app.state（create_app 初始化，测试可注入）。"""
    return request.app.state.auth_service


def get_current_user(
    request: Request,
    auth: AuthService = Depends(get_auth_service),
) -> dict:
    """从 session cookie 解析当前用户；无效/过期/撤销/停用 → 401。

    返回 {id, username, role}（AuthService 契约；user_id 即 user["id"]）。
    """
    session_id = request.cookies.get("session")
    user = auth.resolve_session(session_id)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "unauthorized", "message": "请先登录"}},
        )
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """非 admin 访问管理接口 → 403（普通用户仍通过 get_current_user 链）。"""
    if user["role"] != "admin":
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "forbidden", "message": "需要管理员权限"}},
        )
    return user


def client_ip(request: Request) -> str:
    """登录限流客户端标识：X-Forwarded-For 第一跳（S2-3 Caddy 反代后）或直连地址。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"
