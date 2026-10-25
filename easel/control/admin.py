"""管理员用户、邀请与撤销的服务边界。

所有写操作在 Store 的写事务内按数据库复查 actor 角色/禁用状态与目标，
不信任路由层传入的 role 或旧 ``Identity``。返回值只包含公共视图：没有
token 摘要、登录记录、worker 凭证、内部地址或文件路径。
"""
from __future__ import annotations

import sqlite3

from .auth import AuthService
from .store import Store
from .types import DomainError, Identity


def _page(limit: int, offset: int) -> tuple[int, int]:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise DomainError("invalid_argument", 400)
    if type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise DomainError("invalid_argument", 400)
    return limit, offset


def _display_name(value: object) -> str:
    if not isinstance(value, str):
        raise DomainError("invalid_argument", 400)
    name = value.strip()
    if (not name or len(name) > 200
            or any(ord(character) < 32 for character in name)):
        raise DomainError("invalid_argument", 400)
    return name


class AdminService:
    """管理员操作服务；方法只返回对应 HTTP 接口的公共视图。"""

    def __init__(self, store: Store, auth: AuthService):
        self._store = store
        self._auth = auth

    @staticmethod
    def _user_view(row: sqlite3.Row) -> dict:
        return {
            "userId": str(row["user_id"]),
            "workspaceId": str(row["workspace_id"]),
            "displayName": str(row["display_name"]),
            "role": str(row["role"]),
            "disabled": row["disabled_at"] is not None,
            "createdAt": int(row["created_at"]),
        }

    @staticmethod
    def _invite_view(row: sqlite3.Row) -> dict:
        return {
            "id": str(row["id"]),
            "userId": str(row["user_id"]),
            "createdAt": int(row["created_at"]),
            "expiresAt": int(row["expires_at"]),
            "redeemedAt": row["redeemed_at"],
            "revokedAt": row["revoked_at"],
        }

    def list_users(self, actor: Identity, *, limit: int = 50,
                   offset: int = 0) -> dict:
        limit, offset = _page(limit, offset)
        rows = self._store.admin_list_users(
            actor, limit=limit, offset=offset)
        next_offset = offset + len(rows) if len(rows) == limit else None
        return {"users": [self._user_view(row) for row in rows],
                "nextOffset": next_offset}

    def create_member(self, actor: Identity, display_name: object) -> dict:
        row = self._store.admin_create_member(
            actor, _display_name(display_name))
        return self._user_view(row)

    def set_member_disabled(self, actor: Identity, user_id: str,
                            disabled: bool) -> dict:
        if type(disabled) is not bool:
            raise DomainError("invalid_argument", 400)
        row = self._store.admin_set_user_disabled(
            actor, user_id, disabled)
        return self._user_view(row)

    def revoke_member_sessions(self, actor: Identity, user_id: str) -> dict:
        revoked = self._store.admin_revoke_user_sessions(actor, user_id)
        return {"revoked": int(revoked)}

    def list_invites(self, actor: Identity, *, limit: int = 50,
                     offset: int = 0) -> dict:
        limit, offset = _page(limit, offset)
        rows = self._store.admin_list_invites(
            actor, limit=limit, offset=offset)
        next_offset = offset + len(rows) if len(rows) == limit else None
        return {"invites": [self._invite_view(row) for row in rows],
                "nextOffset": next_offset}

    def create_invite(self, actor: Identity, user_id: str) -> dict:
        invite_id, token, expires_at = self._auth.issue_invite(
            actor, user_id)
        return {"id": invite_id, "token": token, "expiresAt": expires_at}

    def revoke_invite(self, actor: Identity, invite_id: str) -> None:
        self._store.admin_revoke_invite(actor, invite_id)
