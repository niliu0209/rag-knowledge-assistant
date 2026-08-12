"""AuthService：注册/登录/会话/邀请码/管理员 业务规则 owner（S2-1）。

规则来源：stage2-mvp.md S2-1 与 architecture.md 阶段 2 规划节。
owner 合同：
  - 密码 Argon2id 哈希（argon2-cffi，OWASP 现行推荐；库中无明文）
  - 会话 = sessions 表 + cookie（api 层下发 HttpOnly cookie）；登出/停用即时失效
  - 首启：users 表空时第一个注册用户为 admin 并认领 user_id='default' 存量数据
  - 邀请码：仅 admin 生成；注册校验（存在/未用/未撤销/未过期）；一次性
  - 登录限流：IP 维度滑动窗口（内存，单进程够用——单体部署约束）
  - 防枚举：错误密码/不存在用户返回同一通用错误；dummy hash 对齐耗时

data 层（auth_store）不做业务判断，规则集中在本文件。
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.data import auth_store
from app.data.db import get_connection

logger = logging.getLogger(__name__)

# 会话有效期（cookie max_age 与 expires_at 同源；7 天，邀请制足够）
SESSION_TTL_DAYS = 7
# 登录名与密码规则（服务端白名单重新验证，前端不可信）
USERNAME_MIN_CHARS = 2
USERNAME_MAX_CHARS = 32
PASSWORD_MIN_CHARS = 8
# 登录限流：IP 维度滑动窗口（10 次失败 / 5 分钟 → 429）
LOGIN_FAIL_LIMIT = 10
LOGIN_FAIL_WINDOW_SECONDS = 5 * 60
# 邀请码字符集与长度（可复制、无易混淆字符）
INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
INVITE_CODE_LENGTH = 8

_ph = PasswordHasher()

# dummy hash：用户不存在时也执行一次哈希验证，对齐耗时（防时序用户枚举）
_DUMMY_HASH = _ph.hash("dummy-password-for-timing")


class AuthError(Exception):
    """认证错误基类（api 层映射统一错误体）。"""


class UsernameValidationError(AuthError):
    """登录名不合法（api 映射 400 invalid_username）。"""


class PasswordValidationError(AuthError):
    """密码不合法（api 映射 400 invalid_password）。"""


class UsernameTakenError(AuthError):
    """用户名已存在（api 映射 409 username_taken）。"""


class InvalidInviteCodeError(AuthError):
    """邀请码缺失/无效/已用/已撤销（api 映射 400 invalid_invite_code）。"""


class InvalidCredentialsError(AuthError):
    """用户名或密码错误（统一错误防枚举，api 映射 401 invalid_credentials）。"""


class AccountDisabledError(AuthError):
    """用户已被停用（api 映射 403 account_disabled）。"""


class RateLimitedError(AuthError):
    """登录尝试过频（api 映射 429 rate_limited）。"""


class UserNotFoundError(AuthError):
    """管理员操作目标用户不存在（api 映射 404 user_not_found）。"""


class InviteCodeNotFoundError(AuthError):
    """撤销目标邀请码不存在（api 映射 404 invite_code_not_found）。"""


class LoginRateLimiter:
    """登录失败限流（内存滑动窗口，单进程；容器重启重置，邀请制可接受）。

    键 = 客户端标识（api 层传 IP；S2-3 接 Caddy 后取 X-Forwarded-For 第一跳）。
    记录失败时间戳，窗口内失败次数达到上限 → 抛 RateLimitedError。
    """

    def __init__(
        self, limit: int = LOGIN_FAIL_LIMIT, window: float = LOGIN_FAIL_WINDOW_SECONDS
    ) -> None:
        self.limit = limit
        self.window = window
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        dq = self._fails[key]
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= self.limit:
            raise RateLimitedError("登录尝试过于频繁，请 5 分钟后再试")

    def record_failure(self, key: str) -> None:
        self._fails[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        self._fails.pop(key, None)


class AuthService:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.limiter = LoginRateLimiter()

    # ---------- 密码（Argon2id，owner） ----------

    @staticmethod
    def hash_password(password: str) -> str:
        return _ph.hash(password)

    @staticmethod
    def verify_password(password_hash: str, password: str) -> bool:
        """校验密码；格式异常（非 Argon2 哈希）按不匹配处理（不抛出区分结果）。"""
        try:
            return _ph.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
            return False

    # ---------- 注册 ----------

    def register(
        self, username: str, password: str, invite_code: str | None
    ) -> dict:
        """注册并自动登录；返回 {user, session_id}。

        首启（users 空）：role=admin + 认领 default 存量数据（作者数据不丢）。
        非首启：必须有效邀请码，role=user。用户名/密码规则服务端白名单校验。
        """
        username = (username or "").strip()
        if not (USERNAME_MIN_CHARS <= len(username) <= USERNAME_MAX_CHARS):
            raise UsernameValidationError(
                f"用户名需 {USERNAME_MIN_CHARS}~{USERNAME_MAX_CHARS} 个字符"
            )
        if not password or len(password) < PASSWORD_MIN_CHARS:
            raise PasswordValidationError(
                f"密码至少 {PASSWORD_MIN_CHARS} 个字符"
            )

        password_hash = self.hash_password(password)
        with get_connection(self.data_dir) as conn:
            if auth_store.get_user_by_username(conn, username) is not None:
                raise UsernameTakenError("用户名已被使用，请换一个")

            first_user = auth_store.count_users(conn) == 0
            role = "admin" if first_user else "user"
            used_code: str | None = None
            if not first_user:
                code_row = self._validate_invite_code(conn, invite_code)
                used_code = code_row["code"]

            user_id = auth_store.new_id()
            auth_store.insert_user(
                conn,
                user_id=user_id,
                username=username,
                password_hash=password_hash,
                role=role,
                invite_code=used_code,
            )
            if used_code:
                auth_store.mark_invite_used(conn, used_code, user_id)
            migrated = {}
            if first_user:
                # data_dir 传参：有存量文档时同步迁移 Chroma 切片 user_id
                migrated = auth_store.claim_default_data(
                    conn, user_id, data_dir=self.data_dir
                )
            session_id = self._create_session(conn, user_id)
            if first_user:
                logger.info(
                    "首启管理员注册成功（认领 default 存量数据: %s）", migrated
                )
        return {"user": self._user_dict(username, user_id, role), "session_id": session_id}

    def _validate_invite_code(self, conn, invite_code: str | None) -> dict:
        code = (invite_code or "").strip().upper()
        if not code:
            raise InvalidInviteCodeError("注册需要邀请码，请联系管理员")
        row = auth_store.get_invite_code(conn, code)
        if row is None:
            raise InvalidInviteCodeError("邀请码无效")
        if row["used_by"] is not None:
            raise InvalidInviteCodeError("邀请码已被使用")
        if row["revoked_at"] is not None:
            raise InvalidInviteCodeError("邀请码已撤销")
        if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc).isoformat():
            raise InvalidInviteCodeError("邀请码已过期")
        return row

    # ---------- 登录 / 登出 / 会话 ----------

    def login(self, username: str, password: str, client_key: str) -> dict:
        """登录；失败限流（IP 维度）。成功返回 {user, session_id}。"""
        self.limiter.check(client_key)
        username = (username or "").strip()
        with get_connection(self.data_dir) as conn:
            row = auth_store.get_user_by_username(conn, username)
            if row is None:
                # 用户不存在也执行 dummy 验证（防时序枚举）；再记录失败并抛统一错误
                self.verify_password(_DUMMY_HASH, password)
                self.limiter.record_failure(client_key)
                raise InvalidCredentialsError("用户名或密码错误")
            ok = self.verify_password(row["password_hash"], password)
            if not ok:
                self.limiter.record_failure(client_key)
                raise InvalidCredentialsError("用户名或密码错误")
            if row["status"] != "active":
                raise AccountDisabledError("账号已被停用，请联系管理员")
            self.limiter.clear(client_key)
            auth_store.delete_expired_sessions(conn)
            session_id = self._create_session(conn, row["id"])
        return {"user": self._user_dict(row["username"], row["id"], row["role"]), "session_id": session_id}

    def logout(self, session_id: str) -> None:
        with get_connection(self.data_dir) as conn:
            auth_store.revoke_session(conn, session_id)

    def resolve_session(self, session_id: str | None) -> dict | None:
        """会话 → 用户；无效/过期/撤销/用户被停用 → None（api 层映射 401）。"""
        if not session_id:
            return None
        with get_connection(self.data_dir) as conn:
            row = auth_store.get_session(conn, session_id)
            if row is None or row["revoked_at"] is not None:
                return None
            if row["expires_at"] < datetime.now(timezone.utc).isoformat():
                return None
            user = auth_store.get_user_by_id(conn, row["user_id"])
            if user is None or user["status"] != "active":
                return None
        return {"id": user["id"], "username": user["username"], "role": user["role"]}

    # ---------- 管理员：邀请码 ----------

    def create_invite_code(self, admin_id: str) -> str:
        code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))
        with get_connection(self.data_dir) as conn:
            auth_store.insert_invite_code(conn, code=code, created_by=admin_id, expires_at=None)
        logger.info("管理员生成邀请码（created_by=%s）", admin_id)
        return code

    def revoke_invite_code(self, code: str) -> None:
        code = (code or "").strip().upper()
        with get_connection(self.data_dir) as conn:
            row = auth_store.get_invite_code(conn, code)
            if row is None:
                raise InviteCodeNotFoundError("邀请码不存在")
            auth_store.revoke_invite_code(conn, code)

    def list_invite_codes(self) -> list[dict]:
        with get_connection(self.data_dir) as conn:
            rows = auth_store.list_invite_codes(conn)
        return [
            {
                "code": r["code"],
                "created_by": r["created_by"],
                "used_by": r["used_by"],
                "used_at": r["used_at"],
                "revoked_at": r["revoked_at"],
                "expires_at": r["expires_at"],
            }
            for r in rows
        ]

    # ---------- 管理员：用户 ----------

    def list_users(self) -> list[dict]:
        with get_connection(self.data_dir) as conn:
            rows = auth_store.list_users(conn)
        return [
            {
                "id": r["id"],
                "username": r["username"],
                "role": r["role"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def reset_password(self, user_id: str, new_password: str) -> None:
        if not new_password or len(new_password) < PASSWORD_MIN_CHARS:
            raise PasswordValidationError(f"新密码至少 {PASSWORD_MIN_CHARS} 个字符")
        with get_connection(self.data_dir) as conn:
            if auth_store.get_user_by_id(conn, user_id) is None:
                raise UserNotFoundError("用户不存在")
            auth_store.update_password(conn, user_id, self.hash_password(new_password))
        logger.info("管理员重置密码（user_id=%s）", user_id)

    def set_user_status(self, user_id: str, status: str) -> None:
        if status not in ("active", "disabled"):
            raise ValueError("非法状态")
        with get_connection(self.data_dir) as conn:
            if auth_store.get_user_by_id(conn, user_id) is None:
                raise UserNotFoundError("用户不存在")
            auth_store.update_user_status(conn, user_id, status)
            if status == "disabled":
                revoked = auth_store.revoke_sessions_by_user(conn, user_id)
                logger.info("用户停用并撤销其 %d 个会话（user_id=%s）", revoked, user_id)

    # ---------- 内部 ----------

    def _create_session(self, conn, user_id: str) -> str:
        session_id = auth_store.new_id()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)).isoformat()
        auth_store.create_session(
            conn, session_id=session_id, user_id=user_id, expires_at=expires_at
        )
        return session_id

    @staticmethod
    def _user_dict(username: str, user_id: str, role: str) -> dict:
        return {"id": user_id, "username": username, "role": role}
