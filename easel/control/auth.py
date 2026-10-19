"""邀请、浏览器登录、CSRF 与失败限速。

安全边界：

* 邀请与登录 token 只以 SHA-256 摘要入库；明文只在创建时返回一次。
* CSRF token 由登录 token 经 HMAC 单向派生，因此刷新页面或服务重启后仍能从
  当前 Cookie 重新得到同一个值，而数据库只保存其摘要。
* 角色、禁用状态与 workspace 归属每次都回数据库读取，不信任 ``Identity.role``。
* 失败计数写入数据库，多连接/多 AuthService 共享同一窗口。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Literal

from .store import Store
from .types import BrowserSession, DomainError, Identity

_TOKEN_BYTES = 32
TOKEN_MAX_LENGTH = 512
"""token 长度上限；明显超长的输入直接按非法凭证处理。"""

INVITE_TTL_SECONDS = 604800
MAX_INVITE_TTL_SECONDS = 604800
SESSION_IDLE_SECONDS = 86400
SESSION_ABSOLUTE_SECONDS = 604800
RATE_LIMIT_WINDOW_SECONDS = 600
RATE_LIMIT_MAX_FAILURES = 10

_CSRF_CONTEXT = b"easel-csrf-v1"


def _token_bytes(value: str) -> bytes:
    """令牌的确定性字节表示。

    JSON 允许孤立 UTF-16 代理项转义（如 ``\\ud800``），``json.loads`` 会得到
    含代理项的 Python 字符串。这里用 ``surrogatepass`` 保证哈希/派生阶段不会
    抛 ``UnicodeEncodeError``；这类值不可能匹配真实令牌，最终仍按 401 处理。
    """
    return value.encode("utf-8", "surrogatepass")


def _digest(value: str) -> str:
    """令牌摘要；数据库只保存它，不保存明文。"""
    return hashlib.sha256(_token_bytes(value)).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _csrf_token(session_token: str) -> str:
    """从登录 token 单向派生 CSRF token。

    使用 HMAC 而不是直接回传登录 token：拿到派生值无法反推 Cookie 凭证；
    同一登录在刷新或重启后稳定得到同一 CSRF，服务端用数据库中的摘要校验。
    """
    return hmac.new(_token_bytes(session_token), _CSRF_CONTEXT,
                    hashlib.sha256).hexdigest()


def _valid_token(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= TOKEN_MAX_LENGTH


def _failure_digest(value: object) -> str:
    """失败限速键：非法/缺失 token 也要有稳定的摘要键。"""
    return _digest(value if isinstance(value, str) else "")


@dataclass(frozen=True)
class ExchangeOutcome:
    """一次“限速准入 + 兑换”的原子结果。"""

    status: Literal["ok", "denied", "rate_limited"]
    retry_after: int = 0
    session: BrowserSession | None = None
    identity: Identity | None = None


class AuthService:
    """认证服务；所有时间来自可注入的 ``clock``，测试不依赖 sleep。"""

    INVITE_TTL_SECONDS = INVITE_TTL_SECONDS
    MAX_INVITE_TTL_SECONDS = MAX_INVITE_TTL_SECONDS
    SESSION_IDLE_SECONDS = SESSION_IDLE_SECONDS
    SESSION_ABSOLUTE_SECONDS = SESSION_ABSOLUTE_SECONDS
    RATE_LIMIT_WINDOW_SECONDS = RATE_LIMIT_WINDOW_SECONDS
    RATE_LIMIT_MAX_FAILURES = RATE_LIMIT_MAX_FAILURES

    def __init__(self, store: Store, clock: Callable[[], float] = time.time):
        self._store = store
        self.clock = clock

    def _now(self) -> int:
        return int(self.clock())

    # ------------------------------------------------------------------
    # 引导与邀请
    # ------------------------------------------------------------------

    def bootstrap_admin(self, display_name: str) -> tuple[Identity, str]:
        """本地引导首个管理员并返回一次性邀请明文。仅 CLI 调用。"""
        if not isinstance(display_name, str) or not display_name.strip():
            raise DomainError("invalid_argument", 400)
        if len(display_name) > 200:
            raise DomainError("invalid_argument", 400)
        token = _new_token()
        now = self._now()
        identity, _ = self._store.bootstrap_admin(
            display_name.strip(), _digest(token), now, now + self.INVITE_TTL_SECONDS)
        return identity, token

    def create_invite(self, actor: Identity, user_id: str,
                      ttl_seconds: int = INVITE_TTL_SECONDS) -> str:
        """管理员签发邀请；返回仅输出一次的明文 token。"""
        if (type(ttl_seconds) is not int
                or not 1 <= ttl_seconds <= self.MAX_INVITE_TTL_SECONDS):
            raise DomainError("invalid_ttl", 400)
        if not isinstance(user_id, str) or not user_id or len(user_id) > 128:
            raise DomainError("invalid_argument", 400)
        token = _new_token()
        now = self._now()
        self._store.create_invite(
            actor, user_id, _digest(token), now, now + ttl_seconds)
        return token

    def revoke_invite(self, invite: str) -> bool:
        """撤销尚未兑换的邀请；服务内部/管理操作，不暴露为公开 HTTP。"""
        if not _valid_token(invite):
            return False
        return self._store.revoke_invite(_digest(invite), self._now())

    # ------------------------------------------------------------------
    # 兑换与登录
    # ------------------------------------------------------------------

    def exchange(self, invite: str) -> BrowserSession:
        """单次兑换邀请并创建登录；登录 token 与邀请 token 不同。

        这是不含来源限速的底层原语；HTTP 入口必须使用
        :meth:`exchange_with_rate_limit` 以获得原子限速与失败记账。
        """
        if not _valid_token(invite):
            raise DomainError("unauthorized", 401)
        login_token = _new_token()
        csrf_token = _csrf_token(login_token)
        login_id = uuid.uuid4().hex
        now = self._now()
        self._store.redeem_invite(
            _digest(invite), login_id, _digest(login_token), _digest(csrf_token),
            now, now + self.SESSION_ABSOLUTE_SECONDS)
        return BrowserSession(login_id, login_token, csrf_token)

    def exchange_with_rate_limit(self, source: str, invite: object) -> ExchangeOutcome:
        """原子执行限速准入、兑换与失败记账。

        检查、兑换和失败写入由 Store 在同一个 ``BEGIN IMMEDIATE`` 事务内完成，
        因此同一库上的并发请求与多个 app/连接共享同一额度；限速拒绝发生在
        读取邀请之前，不会消耗合法邀请。
        """
        login_token = _new_token()
        csrf_token = _csrf_token(login_token)
        login_id = uuid.uuid4().hex
        now = self._now()
        status, identity, retry_after = self._store.redeem_invite_limited(
            source=str(source)[:256],
            token_digest=_failure_digest(invite),
            login_id=login_id,
            login_token_digest=_digest(login_token),
            csrf_digest=_digest(csrf_token),
            now=now,
            expires_at=now + self.SESSION_ABSOLUTE_SECONDS,
            window_seconds=self.RATE_LIMIT_WINDOW_SECONDS,
            max_failures=self.RATE_LIMIT_MAX_FAILURES,
        )
        if status == "ok":
            return ExchangeOutcome(
                status="ok",
                session=BrowserSession(login_id, login_token, csrf_token),
                identity=identity,
            )
        return ExchangeOutcome(status=status, retry_after=retry_after)

    def authenticate(self, token: str) -> Identity:
        """校验登录；滑动更新 last_seen，但不延长绝对期限。"""
        if not _valid_token(token):
            raise DomainError("unauthorized", 401)
        return self._store.authenticate_login(
            _digest(token), self._now(), self.SESSION_IDLE_SECONDS)

    def inspect(self, token: str) -> Identity:
        """校验登录但不更新活动时间。

        写请求在 Origin/CSRF/路由授权通过前使用它，避免失败请求改变会话。
        """
        if not _valid_token(token):
            raise DomainError("unauthorized", 401)
        return self._store.authenticate_login(
            _digest(token), self._now(), self.SESSION_IDLE_SECONDS, touch=False)

    def csrf_token(self, token: str) -> str:
        """为有效登录恢复 CSRF token；不刷新活动时间。"""
        if not _valid_token(token):
            raise DomainError("unauthorized", 401)
        csrf_token = _csrf_token(token)
        stored = self._store.session_csrf_digest(
            _digest(token), self._now(), self.SESSION_IDLE_SECONDS)
        if not hmac.compare_digest(_digest(csrf_token), stored):
            raise DomainError("unauthorized", 401)
        return csrf_token

    def revoke_current(self, token: str) -> None:
        """持久撤销当前登录。"""
        if not _valid_token(token):
            raise DomainError("unauthorized", 401)
        self._store.revoke_login(
            _digest(token), self._now(), self.SESSION_IDLE_SECONDS)

    def revoke_all(self, user_id: str) -> None:
        """撤销该用户全部登录；服务内部方法，不是公开 HTTP 接口。"""
        if not isinstance(user_id, str) or not user_id or len(user_id) > 128:
            raise DomainError("invalid_argument", 400)
        self._store.revoke_all_logins(user_id, self._now())

    # ------------------------------------------------------------------
    # 兑换失败限速
    # ------------------------------------------------------------------

    def rate_limit_retry_after(self, source: str, token: object) -> int:
        """返回 0 表示允许本次兑换，否则为 Retry-After 秒数。"""
        return self._store.check_auth_rate_limit(
            str(source)[:256], _failure_digest(token), self._now(),
            self.RATE_LIMIT_WINDOW_SECONDS, self.RATE_LIMIT_MAX_FAILURES)

    def record_failed_exchange(self, source: str, token: object) -> None:
        """记录一次失败；保存的是 token 摘要，不是原文。"""
        self._store.record_auth_failure(
            str(source)[:256], _failure_digest(token), self._now(),
            self.RATE_LIMIT_WINDOW_SECONDS)
