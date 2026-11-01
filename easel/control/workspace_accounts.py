"""Task 5C2：control 对本人账号、平台登录与分析的受管适配。

边界：

* 平台/动作来自固定白名单；control 只生成固定的内部动作描述，浏览器不能
  指定脚本、URL、路径或环境；
* 登录、强制 whoami、分析刷新与凭证校验走既有 ``Store.reserve_run`` /
  WorkerRelay / worker RunManager 的接纳、容量、签名、停止与终态；
* 账号列表、登录状态、已有凭证状态、二维码是签名短请求；
* 短信、停止、注销绑定本人当前任务/平台，不新建 Agent prompt；
* 凭证 AppSecret 不进入 Run 输入摘要/事件/日志，经一次短请求交接给原
  worker handler；验证失败不覆盖旧配置。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from .store import Store
from .types import DomainError, Identity

LOGIN_PLATFORMS = frozenset({
    "xiaohongshu", "kuaishou", "weixin-channels", "zhihu",
    "bilibili", "douyin", "wechat-oa",
})
ANALYTICS_PLATFORMS = frozenset({
    "xiaohongshu", "douyin", "kuaishou", "zhihu", "weixin-channels",
    "bilibili", "wechat-oa",
})
CREDENTIAL_PLATFORMS = frozenset({"wechat-oa"})
LOGIN_ACTION_PLATFORMS = LOGIN_PLATFORMS - {"wechat-oa"}

_TERMINAL = frozenset({
    "done", "failed", "interrupted", "cancelled",
})
_ACTIVE = frozenset({"reserved", "running", "awaiting_permission"})
_PLATFORM = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TERMINAL_LOGIN_STATES = frozenset({"success", "expired", "error"})
_LOGIN_VISIBLE_STATES = frozenset({
    "starting", "qr_ready", "scanned", "sms_required", "verifying",
    "success", "expired", "error", "unknown", "window_login",
})
_ACTION_TIMEOUT = {
    "whoami": 60.0,
    "analytics": 210.0,
    "credentials": 60.0,
}
_POLL_GRACE = 0.2
_MAX_TRANSIENT = 5


def canonical_platform(value: object) -> str:
    if (not isinstance(value, str) or not _PLATFORM.fullmatch(value)
            or value not in LOGIN_PLATFORMS | ANALYTICS_PLATFORMS):
        raise DomainError("invalid_request", 400)
    return value


def canonical_request_id(value: object) -> str:
    if (not isinstance(value, str) or not _REQUEST_ID.fullmatch(value)):
        raise DomainError("invalid_request", 400)
    return value


def login_task_key(platform: str) -> str:
    return f"browser:login:{platform}"


def mp_login_task_key(platform: str) -> str:
    return f"browser:mp-login:{platform}"


def whoami_task_key(platform: str) -> str:
    return f"browser:whoami:{platform}"


def analytics_task_key(platform: str) -> str:
    return f"browser:analytics:{platform}"


def credentials_task_key(platform: str) -> str:
    return f"browser:credentials:{platform}"


def _descriptor(action: str, platform: str, **extra) -> str:
    return json.dumps(
        {"action": action, "platform": platform, **extra},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _status_view(value: object) -> dict:
    """只接受原登录状态字段；未知状态不当 success。"""
    if not isinstance(value, dict):
        raise DomainError("upstream_invalid", 502)
    state = value.get("state")
    if not isinstance(state, str) or not state:
        state = "unknown"
    if state not in _LOGIN_VISIBLE_STATES:
        # 未知新状态保留原值但绝不当成功；前端只认 success。
        state = state[:32]
    return {
        "state": state,
        "message": value.get("message")
        if isinstance(value.get("message"), str) else "",
        "qr": value.get("qr") if isinstance(value.get("qr"), str) else "",
        "qrTs": value.get("qrTs")
        if type(value.get("qrTs")) is int else 0,
    }


def _public_account(value: object) -> dict:
    if not isinstance(value, dict):
        raise DomainError("upstream_invalid", 502)
    platform = value.get("platform")
    if not isinstance(platform, str) or not platform:
        raise DomainError("upstream_invalid", 502)
    return {
        "platform": platform,
        "name": value.get("name") if isinstance(value.get("name"), str) else "",
        "backend": value.get("backend")
        if isinstance(value.get("backend"), str) else "",
        "supported": value.get("supported") is True,
        "loggedIn": value.get("loggedIn") is True,
        "note": value.get("note") if isinstance(value.get("note"), str) else "",
        **({"mpLoggedIn": value["mpLoggedIn"]}
           if isinstance(value.get("mpLoggedIn"), bool) else {}),
        **({"credentialConfigured": value["credentialConfigured"]}
           if isinstance(value.get("credentialConfigured"), bool) else {}),
    }


def _public_analytics_platform(value: object) -> dict:
    if not isinstance(value, dict):
        raise DomainError("upstream_invalid", 502)
    platform = value.get("platform")
    if not isinstance(platform, str) or not platform:
        raise DomainError("upstream_invalid", 502)
    return {
        "platform": platform,
        "name": value.get("name") if isinstance(value.get("name"), str) else platform,
        "loggedIn": value.get("loggedIn") is True,
    }


class WorkspaceAccounts:
    """账号/登录/分析的受管适配；所有 Store 查询都带 Identity。"""

    def __init__(self, store: Store, relay, workspace_api, *,
                 db_runner=None, clock=time.time, sleep=asyncio.sleep,
                 poll_interval: float = 0.25) -> None:
        self._store = store
        self._relay = relay
        self._workspace_api = workspace_api
        self._db_runner = db_runner
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = float(poll_interval)
        self._jobs: dict[tuple[str, str, str], asyncio.Task] = {}

    async def _db(self, function, *args, **kwargs):
        if self._db_runner is None:
            return function(*args, **kwargs)
        return await self._db_runner(function, *args, **kwargs)

    def _require_relay(self):
        if self._relay is None:
            raise DomainError("capability_unavailable", 503)
        return self._relay

    def _require_api(self):
        if self._workspace_api is None:
            raise DomainError("capability_unavailable", 503)
        return self._workspace_api

    async def _signed_json(self, actor: Identity, method: str, path: str,
                           *, query=None, payload=None) -> object:
        response = await self._require_api().request(
            actor, method, path, query=query, payload=payload)
        try:
            return response.json()
        except ValueError:
            raise DomainError("upstream_invalid", 502) from None

    async def _conversation(self, actor: Identity, key: str, title: str):
        return await self._db(
            self._store.get_or_create_task_conversation, actor, key, title)

    async def _latest(self, actor: Identity, conversation_id: str):
        return await self._db(
            self._store.latest_run_for_conversation, actor, conversation_id)

    async def _active(self, actor: Identity, key: str):
        conversation = await self._db(
            self._store.get_task_conversation, actor, key)
        if conversation is None:
            return None
        run = await self._db(
            self._store.latest_run_for_conversation, actor, conversation.id)
        if run is None or run.status not in _ACTIVE:
            return None
        return run

    async def _submit(self, actor: Identity, key: str, title: str,
                      task_type: str, descriptor: str,
                      request_id: str) -> tuple[dict, object]:
        conversation = await self._conversation(actor, key, title)
        admission = await self._require_relay().submit_task(actor, task_type, {
            "sessionId": conversation.id,
            "turnId": request_id,
            "message": descriptor,
            "attachments": [],
            "persona": None,
        })
        if not isinstance(admission, dict):
            raise DomainError("upstream_invalid", 502)
        run_id = admission.get("runId")
        if not isinstance(run_id, str) or not run_id:
            raise DomainError("upstream_invalid", 502)
        run = await self._db(self._store.get_run, actor, run_id)
        return admission, run

    async def _join_or_submit(self, actor: Identity, key: str, title: str,
                              task_type: str, descriptor: str,
                              request_id: str):
        active = await self._active(actor, key)
        if active is not None:
            return {"runId": active.id, "duplicate": True}, active
        try:
            return await self._submit(
                actor, key, title, task_type, descriptor, request_id)
        except DomainError as error:
            # 并发同平台请求：另一方刚占用了同一任务会话，加入其 Run。
            if error.code not in ("conversation_busy", "request_conflict"):
                raise
            active = await self._active(actor, key)
            if active is None:
                raise
            return {"runId": active.id, "duplicate": True}, active

    async def _sync_run(self, actor: Identity, run) -> str:
        if run.status in _TERMINAL:
            return run.status
        relay = self._require_relay()
        try:
            page = await relay.events(actor, run.id, after=0)
        except DomainError as error:
            if error.status == 503:
                raise
            return run.status
        state = page.get("state") if isinstance(page, dict) else None
        if isinstance(state, str) and state:
            return state
        return run.status

    async def _finish(self, actor: Identity, run, status: str) -> None:
        if status not in _TERMINAL:
            return
        try:
            await self._db(self._store.finish_run, actor, run.id, status)
        except DomainError:
            pass

    async def _stop_and_wait(self, actor: Identity, run,
                             timeout: float = 15.0) -> str:
        """停止本人现有任务并等持久终态；未完成则明确 503，不假装已停。"""
        relay = self._require_relay()
        try:
            result = await relay.stop(actor, {"runId": run.id})
        except DomainError as error:
            if error.status == 404:
                return "cancelled"
            raise
        state = result.get("state") if isinstance(result, dict) else None
        if state in _TERMINAL:
            await self._finish(actor, run, state)
            return str(state)
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            try:
                page = await relay.events(actor, run.id, after=0)
            except DomainError:
                page = None
            state = page.get("state") if isinstance(page, dict) else None
            if state in _TERMINAL:
                await self._finish(actor, run, state)
                return str(state)
            await self._sleep(self._poll_interval)
        raise DomainError("browser_task_stop_timeout", 503)

    async def _run_error(self, page: object) -> DomainError:
        """把 worker 的稳定错误码映射为明确 HTTP 语义，不回显原始输出。"""
        events = page.get("events") if isinstance(page, dict) else None
        code = ""
        if isinstance(events, list):
            for event in reversed(events):
                if (isinstance(event, dict) and event.get("event") == "error"
                        and isinstance(event.get("data"), str)):
                    code = event["data"]
                    break
        if code.endswith("_timeout"):
            return DomainError(code, 504)
        if code in ("browser_platform_unsupported",
                    "browser_login_requires_credentials"):
            return DomainError(code, 404)
        if code in ("browser_task_invalid", "browser_task_action_unsupported",
                    "browser_task_type_mismatch"):
            return DomainError(code, 400)
        return DomainError(code or "browser_task_failed", 502)

    async def _result(self, actor: Identity, run) -> object:
        relay = self._require_relay()
        try:
            data = await relay.messages(
                actor, run.conversation_id, limit=500, offset=0)
        except DomainError:
            raise DomainError("browser_task_result_unavailable", 502) from None
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            raise DomainError("browser_task_result_unavailable", 502)
        for message in reversed(messages):
            if (not isinstance(message, dict)
                    or message.get("role") != "assistant"
                    or message.get("runId") != run.id
                    or not isinstance(message.get("content"), str)):
                continue
            try:
                return json.loads(message["content"])
            except ValueError:
                raise DomainError(
                    "browser_task_result_invalid", 502) from None
        raise DomainError("browser_task_result_unavailable", 502)

    async def _wait_result(self, actor: Identity, run, action: str):
        timeout = _ACTION_TIMEOUT[action]
        deadline = self._clock() + timeout
        transient = 0
        while True:
            try:
                page = await self._require_relay().events(
                    actor, run.id, after=0)
                transient = 0
            except DomainError as error:
                if error.status != 503 or self._clock() >= deadline:
                    raise
                transient += 1
                if transient > _MAX_TRANSIENT:
                    raise
                await self._sleep(self._poll_interval)
                continue
            state = page.get("state") if isinstance(page, dict) else None
            if state == "done":
                return await self._result(actor, run)
            if state in _TERMINAL:
                raise await self._run_error(page)
            if self._clock() >= deadline:
                await self._stop_and_wait(actor, run)
                raise DomainError("browser_task_timeout", 504)
            await self._sleep(self._poll_interval)

    async def _coalesce(self, actor: Identity, action: str, platform: str,
                        factory):
        key = (actor.workspace_id, action, platform)
        existing = self._jobs.get(key)
        if existing is not None and not existing.done():
            return await asyncio.shield(existing)
        task = asyncio.create_task(factory())
        self._jobs[key] = task

        def _done(finished: asyncio.Task) -> None:
            if self._jobs.get(key) is finished:
                self._jobs.pop(key, None)

        task.add_done_callback(_done)
        return await asyncio.shield(task)

    # ------------------------------------------------------------------
    # 快速只读
    # ------------------------------------------------------------------

    async def account_list(self, actor: Identity) -> list[dict]:
        data = await self._signed_json(actor, "GET", "/api/accounts")
        if not isinstance(data, list):
            raise DomainError("upstream_invalid", 502)
        return [_public_account(item) for item in data]

    async def analytics_platforms(self, actor: Identity) -> list[dict]:
        data = await self._signed_json(
            actor, "GET", "/api/analytics/platforms")
        if not isinstance(data, list):
            raise DomainError("upstream_invalid", 502)
        return [_public_analytics_platform(item) for item in data]

    async def get_credentials(self, actor: Identity, platform: object) -> dict:
        platform = canonical_platform(platform)
        if platform not in CREDENTIAL_PLATFORMS:
            raise DomainError("not_found", 404)
        data = await self._signed_json(
            actor, "GET", f"/api/accounts/{platform}/credentials")
        if not isinstance(data, dict):
            raise DomainError("upstream_invalid", 502)
        return {
            "configured": data.get("configured") is True,
            "appIdMasked": data.get("appIdMasked")
            if isinstance(data.get("appIdMasked"), str) else "",
            "name": data.get("name")
            if isinstance(data.get("name"), str) else "",
            "author": data.get("author")
            if isinstance(data.get("author"), str) else "",
        }

    # ------------------------------------------------------------------
    # 登录 / 短信 / 停止 / 注销
    # ------------------------------------------------------------------

    def _qr_url(self, platform: str, mp: bool) -> str:
        suffix = "/mp-login/qr" if mp else "/qr"
        return f"/api/accounts/{platform}{suffix}"

    async def _login_view(self, actor: Identity, platform: str, run,
                          *, mp: bool, tolerate_status_error: bool) -> dict:
        status: dict | None = None
        try:
            raw = await self._signed_json(
                actor, "GET",
                (f"/api/accounts/{platform}/mp-login/status" if mp
                 else f"/api/login/{platform}/status"))
            status = _status_view(raw)
        except DomainError:
            if not tolerate_status_error:
                raise
        state = run.status if run is not None else "unknown"
        if run is not None:
            try:
                state = await self._sync_run(actor, run)
            except DomainError:
                if not tolerate_status_error:
                    raise
        if state in _TERMINAL:
            if state == "done":
                view = status or {
                    "state": "success", "message": "", "qr": "", "qrTs": 0}
                view["state"] = "success"
            else:
                view = {
                    "state": "error",
                    "message": ("登录任务已停止，请重新登录"
                                if state == "cancelled"
                                else "登录任务已结束，请重新登录"),
                    "qr": "", "qrTs": 0,
                }
                if status is not None and status["state"] == "success":
                    # 迟到状态不能复活已停止/注销的任务。
                    view = status
                    view["state"] = "error"
                    view["message"] = "登录任务已停止，请重新登录"
                    view["qr"] = ""
                    view["qrTs"] = 0
        elif status is not None:
            view = status
            if status["state"] in ("success", "expired", "error"):
                await self._finish(
                    actor, run,
                    "done" if status["state"] == "success" else "failed")
        else:
            view = {"state": "starting",
                    "message": "正在启动浏览器并准备二维码…",
                    "qr": "", "qrTs": 0}
        view = dict(view)
        view["mode"] = "qr"
        view["runId"] = run.id if run is not None else ""
        if view["qr"]:
            view["qrUrl"] = self._qr_url(platform, mp)
        return view

    async def login_start(self, actor: Identity, platform: object, *,
                          request_id: object = None,
                          restart: bool = False) -> dict:
        platform = canonical_platform(platform)
        if platform == "wechat-oa":
            if type(restart) is not bool:
                raise DomainError("invalid_request", 400)
            credentials = await self.get_credentials(actor, platform)
            return {
                "mode": "credentials",
                "configured": credentials["configured"],
                "state": "unknown", "message": "请填写 AppID / AppSecret",
                "qr": "", "qrTs": 0, "runId": "",
            }
        if platform not in LOGIN_ACTION_PLATFORMS:
            raise DomainError("not_found", 404)
        if type(restart) is not bool:
            raise DomainError("invalid_request", 400)
        request_id = (canonical_request_id(request_id)
                      if request_id is not None else uuid.uuid4().hex)
        key = login_task_key(platform)
        active = await self._active(actor, key)
        if active is not None and restart:
            await self._stop_and_wait(actor, active)
            active = None
        if active is not None:
            return await self._login_view(
                actor, platform, active, mp=False,
                tolerate_status_error=True)
        _admission, run = await self._join_or_submit(
            actor, key, f"账号登录：{platform}", "browser",
            _descriptor("login", platform), request_id)
        return await self._login_view(
            actor, platform, run, mp=False, tolerate_status_error=True)

    async def login_status(self, actor: Identity, platform: object) -> dict:
        platform = canonical_platform(platform)
        if platform == "wechat-oa":
            raw = await self._signed_json(
                actor, "GET", f"/api/login/{platform}/status")
            view = _status_view(raw)
            view["mode"] = "qr"
            view["runId"] = ""
            return view
        run = await self._active_or_latest(
            actor, login_task_key(platform))
        return await self._login_view(
            actor, platform, run, mp=False, tolerate_status_error=False)

    async def _active_or_latest(self, actor: Identity, key: str):
        conversation = await self._db(
            self._store.get_task_conversation, actor, key)
        if conversation is None:
            return None
        return await self._db(
            self._store.latest_run_for_conversation, actor, conversation.id)

    async def mp_login_start(self, actor: Identity, platform: object, *,
                             request_id: object = None,
                             restart: bool = False) -> dict:
        platform = canonical_platform(platform)
        if platform != "wechat-oa":
            raise DomainError("not_found", 404)
        if type(restart) is not bool:
            raise DomainError("invalid_request", 400)
        request_id = (canonical_request_id(request_id)
                      if request_id is not None else uuid.uuid4().hex)
        key = mp_login_task_key(platform)
        active = await self._active(actor, key)
        if active is not None and restart:
            await self._stop_and_wait(actor, active)
            active = None
        if active is not None:
            return await self._login_view(
                actor, platform, active, mp=True,
                tolerate_status_error=True)
        _admission, run = await self._join_or_submit(
            actor, key, "公众号后台登录：wechat-oa", "browser",
            _descriptor("mp-login", platform), request_id)
        return await self._login_view(
            actor, platform, run, mp=True, tolerate_status_error=True)

    async def mp_login_status(self, actor: Identity, platform: object) -> dict:
        platform = canonical_platform(platform)
        if platform != "wechat-oa":
            raise DomainError("not_found", 404)
        run = await self._active_or_latest(actor, mp_login_task_key(platform))
        return await self._login_view(
            actor, platform, run, mp=True, tolerate_status_error=False)

    async def _current_login_run(self, actor: Identity, platform: str,
                                 *, mp: bool):
        key = mp_login_task_key(platform) if mp else login_task_key(platform)
        return await self._active_or_latest(actor, key)

    async def login_sms(self, actor: Identity, platform: object, code: object,
                        *, run_id: object = None) -> dict:
        platform = canonical_platform(platform)
        if platform not in LOGIN_ACTION_PLATFORMS:
            raise DomainError("not_found", 404)
        if (not isinstance(code, str)
                or not re.fullmatch(r"[0-9]{4,8}", code)):
            raise DomainError("invalid_argument", 400)
        current = None
        if run_id is not None:
            if not isinstance(run_id, str) or not run_id:
                raise DomainError("invalid_argument", 400)
            # 先按当前身份重查归属：外空间/未知 Run 一律 404，不探测他人任务。
            owned = await self._db(self._store.assert_run, actor, run_id)
            current = await self._active(actor, login_task_key(platform))
            if current is None or owned.id != current.id:
                raise DomainError("sms_not_pending", 409)
        else:
            current = await self._active(actor, login_task_key(platform))
        if current is None:
            raise DomainError("sms_not_pending", 409)
        raw = await self._signed_json(
            actor, "GET", f"/api/login/{platform}/status")
        status = _status_view(raw)
        if status["state"] != "sms_required":
            raise DomainError("sms_not_pending", 409)
        result = await self._signed_json(
            actor, "POST", f"/api/login/{platform}/sms", payload={"code": code})
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise DomainError("sms_rejected", 502)
        return {"ok": True, "runId": current.id}

    async def stop_login(self, actor: Identity, platform: object, *,
                         mp: bool = False,
                         run_id: object = None) -> dict:
        platform = canonical_platform(platform)
        if platform not in LOGIN_PLATFORMS:
            raise DomainError("not_found", 404)
        if mp and platform != "wechat-oa":
            raise DomainError("not_found", 404)
        key = mp_login_task_key(platform) if mp else login_task_key(platform)
        latest = await self._active_or_latest(actor, key)
        if latest is None:
            return {"ok": True, "stopped": False, "runId": "", "state": "idle"}
        if run_id is not None:
            if not isinstance(run_id, str) or run_id != latest.id:
                raise DomainError("not_found", 404)
        if latest.status in _TERMINAL:
            return {"ok": True, "stopped": False,
                    "runId": latest.id, "state": latest.status}
        state = await self._stop_and_wait(actor, latest)
        return {"ok": True, "stopped": True,
                "runId": latest.id, "state": state}

    async def logout(self, actor: Identity, platform: object) -> dict:
        platform = canonical_platform(platform)
        if platform not in LOGIN_PLATFORMS:
            raise DomainError("not_found", 404)
        # 先停本人该平台现有登录任务并等终态，再删除 profile/状态；迟到
        # success 不会把已注销的登录态写回来。
        for key in (login_task_key(platform), mp_login_task_key(platform)):
            active = await self._active(actor, key)
            if active is not None:
                await self._stop_and_wait(actor, active)
        result = await self._signed_json(
            actor, "POST", f"/api/logout/{platform}")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise DomainError("logout_failed", 502)
        deleted = result.get("deleted")
        return {
            "ok": True,
            "deleted": [item for item in deleted
                        if isinstance(item, str)]
            if isinstance(deleted, list) else [],
        }

    # ------------------------------------------------------------------
    # whoami / 分析 / 凭证
    # ------------------------------------------------------------------

    async def whoami(self, actor: Identity, platform: object, *,
                     force: bool = False,
                     request_id: object = None) -> dict:
        platform = canonical_platform(platform)
        if platform not in LOGIN_PLATFORMS:
            raise DomainError("not_found", 404)
        if type(force) is not bool:
            raise DomainError("invalid_request", 400)
        request_id = (canonical_request_id(request_id)
                      if request_id is not None else uuid.uuid4().hex)
        key = whoami_task_key(platform)
        descriptor = _descriptor("whoami", platform, force=force)

        async def factory():
            _admission, run = await self._join_or_submit(
                actor, key, f"账号校验：{platform}", "browser",
                descriptor, request_id)
            return await self._wait_result(actor, run, "whoami")

        result = await self._coalesce(actor, "whoami", platform, factory)
        if not isinstance(result, dict):
            raise DomainError("whoami_invalid", 502)
        return {
            "loggedIn": result.get("loggedIn") is True,
            "name": result.get("name")
            if isinstance(result.get("name"), str) else "",
            "avatar": result.get("avatar")
            if isinstance(result.get("avatar"), str) else "",
        }

    async def analytics(self, actor: Identity, platform: object, *,
                        request_id: object = None) -> dict:
        platform = canonical_platform(platform)
        if platform not in ANALYTICS_PLATFORMS:
            raise DomainError("not_found", 404)
        request_id = (canonical_request_id(request_id)
                      if request_id is not None else uuid.uuid4().hex)
        key = analytics_task_key(platform)
        descriptor = _descriptor("analytics", platform)

        async def factory():
            _admission, run = await self._join_or_submit(
                actor, key, f"创作数据：{platform}", "analytics",
                descriptor, request_id)
            return await self._wait_result(actor, run, "analytics")

        result = await self._coalesce(actor, "analytics", platform, factory)
        if not isinstance(result, dict):
            raise DomainError("analytics_invalid", 502)
        if "platform" in result and result.get("platform") != platform:
            raise DomainError("analytics_invalid", 502)
        return result

    async def save_credentials(self, actor: Identity, platform: object,
                               payload: object, *,
                               request_id: object = None) -> dict:
        platform = canonical_platform(platform)
        if platform not in CREDENTIAL_PLATFORMS:
            raise DomainError("not_found", 404)
        if not isinstance(payload, dict):
            raise DomainError("invalid_argument", 400)
        if set(payload) - {"appId", "appSecret", "name", "author"}:
            raise DomainError("invalid_argument", 400)
        app_id = payload.get("appId")
        app_secret = payload.get("appSecret")
        name = payload.get("name", "")
        author = payload.get("author", "")
        if (not isinstance(app_id, str) or not app_id.strip()
                or not isinstance(app_secret, str) or not app_secret.strip()
                or len(app_id) > 128 or len(app_secret) > 512):
            raise DomainError("invalid_argument", 400)
        for value, maximum in ((name, 200), (author, 200)):
            if (not isinstance(value, str) or len(value) > maximum
                    or any(ord(character) < 32 for character in value)):
                raise DomainError("invalid_argument", 400)
        if any(ord(character) < 32
               for character in app_id + app_secret):
            raise DomainError("invalid_argument", 400)
        request_id = (canonical_request_id(request_id)
                      if request_id is not None else uuid.uuid4().hex)
        key = credentials_task_key(platform)
        active = await self._active(actor, key)
        if active is not None:
            raise DomainError("credentials_busy", 409)
        _admission, run = await self._submit(
            actor, key, "公众号凭证校验：wechat-oa", "browser",
            _descriptor("credentials", platform), request_id)
        try:
            delivered = await self._signed_json(
                actor, "POST", f"/api/accounts/{platform}/credentials", payload={
                    "appId": app_id.strip(),
                    "appSecret": app_secret.strip(),
                    "name": name.strip(),
                    "author": author.strip(),
                    "runId": run.id,
                })
        except DomainError:
            try:
                await self._stop_and_wait(actor, run)
            except DomainError:
                pass
            raise
        try:
            result = await self._wait_result(actor, run, "credentials")
        except DomainError:
            # 交接接口已经返回明确结果时，仍以 Run 终态为准；此处只保留
            # 原始错误语义，不把已保存/未保存混成成功。
            raise
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise DomainError("credentials_invalid", 502)
        return {
            "ok": result["ok"],
            "message": result.get("message")
            if isinstance(result.get("message"), str) else "",
        }

    # ------------------------------------------------------------------
    # 二维码流
    # ------------------------------------------------------------------

    async def qr_stream(self, actor: Identity, platform: object, *,
                        mp: bool = False):
        platform = canonical_platform(platform)
        if platform not in LOGIN_PLATFORMS:
            raise DomainError("not_found", 404)
        if mp and platform != "wechat-oa":
            raise DomainError("not_found", 404)
        path = (f"/api/accounts/{platform}/mp-login/qr" if mp
                else f"/api/accounts/{platform}/qr")
        return await self._require_api().open_stream(actor, "GET", path)
