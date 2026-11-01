"""Task 5C3：control 对本人原发布工作台与稿件导出的受管适配。

边界：

* 平台、动作、媒体扩展名来自固定白名单；浏览器只能提交稿件字段，不能
  指定脚本、命令、任意 URL 或绝对路径；
* 自动发布先按 control 受管策略判断，策略拒绝发生在容量/worker/派发之前；
* 发布与导出都进入既有 ``Store.reserve_run`` / WorkerRelay / worker
  RunManager 的接纳、容量、签名、停止与终态，不新建第二套任务系统；
* 状态与短信只读取当前 Run 自己的事件，不根据共享平台旧状态结算新 Run；
* 接受请求不等于平台成功；无法确认时返回 ``unknown``，不伪造 success，
  也不自动补发。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from .store import Store
from .types import DomainError, Identity

PUBLISH_PLATFORMS = frozenset({
    "xiaohongshu", "douyin", "kuaishou", "weixin-channels", "zhihu",
    "bilibili", "wechat-oa",
})
MEDIA_REQUIRED = frozenset({
    "xiaohongshu", "douyin", "kuaishou", "weixin-channels", "bilibili",
})
VIDEO_ONLY = frozenset({"douyin", "weixin-channels", "bilibili"})
# 发布沿用原 web.app 的媒体类型；导出沿用 manual_publish 的交接包白名单。
PUBLISH_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
})
PUBLISH_VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm", ".m4v"})
EXPORT_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif",
})
EXPORT_VIDEO_EXTS = frozenset({
    ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".flv", ".ts",
})
IMAGE_EXTS = EXPORT_IMAGE_EXTS
VIDEO_EXTS = EXPORT_VIDEO_EXTS
MAX_MEDIA = 32
MAX_TITLE = 1000
MAX_BODY = 100_000
MAX_TAGS = 2000
MAX_PERSONA = 200
MAX_PATH = 2048
POLICY_MODES = frozenset({"automated", "manual"})

_PLATFORM = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SMS_CODE = re.compile(r"^[0-9]{4,8}$")
_EXPORT_PATH = re.compile(
    r"^_publish/manual/easel-("
    + "|".join(sorted(PUBLISH_PLATFORMS))
    + r")-[0-9a-f]{12}\.zip$")
EXPORT_PATH = _EXPORT_PATH

_TERMINAL = frozenset({"done", "failed", "interrupted", "cancelled"})
_ACTIVE = frozenset({"reserved", "running", "awaiting_permission"})
_EXPORT_TIMEOUT = 600.0
_POLL_GRACE = 0.2
_MAX_TRANSIENT = 5

PUBLISH_STATES = frozenset({
    "starting", "sms_required", "verifying", "success", "error", "unknown",
})
_TERMINAL_PUBLISH_STATES = frozenset({"success", "error", "unknown"})


def canonical_platform(value: object) -> str:
    if (not isinstance(value, str) or not _PLATFORM.fullmatch(value)
            or value not in PUBLISH_PLATFORMS):
        raise DomainError("invalid_request", 400)
    return value


def canonical_request_id(value: object) -> str:
    if (not isinstance(value, str) or not _REQUEST_ID.fullmatch(value)):
        raise DomainError("invalid_request", 400)
    return value


def _canonical_text(value: object, field: str, maximum: int, *,
                    required: bool = False) -> str:
    if value is None and not required:
        return ""
    if (not isinstance(value, str) or len(value) > maximum
            or (required and not value.strip())
            or any(ord(character) < 32 and character not in "\n\r\t"
                   for character in value)):
        raise DomainError("invalid_request", 400)
    return value


def canonical_media_path(value: object) -> str:
    """校验 outputs 相对媒体路径；真正的文件边界由 worker 再复核。"""
    if (not isinstance(value, str) or not value or len(value) > MAX_PATH
            or value.startswith(("/", "\\"))
            or any(character in value for character in "\\:\x00\r\n")
            or any(ord(character) < 32 or ord(character) == 127
                   for character in value)):
        raise DomainError("invalid_request", 400)
    parts = value.split("/")
    if (any(not part or part in (".", "..") or part.startswith(".")
            for part in parts)
            or parts[0].startswith("_")
            or parts[0] in ("analytics", "wechat")):
        raise DomainError("invalid_request", 400)
    return value


def canonical_export_path(value: object) -> str:
    """只接受本人导出包的精确形状；不接受任意服务器路径。"""
    if (not isinstance(value, str) or not value or len(value) > MAX_PATH
            or not _EXPORT_PATH.fullmatch(value)):
        raise DomainError("invalid_request", 400)
    return value


def publish_task_key(platform: str) -> str:
    return f"publish:{platform}"


def export_task_key(platform: str) -> str:
    return f"publish:export:{platform}"


def _descriptor(action: str, platform: str, **extra: object) -> str:
    return json.dumps(
        {"action": action, "platform": platform, **extra},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _media_kind(path: str, image_exts, video_exts) -> str:
    lower = path.lower()
    if lower.endswith(tuple(image_exts)):
        return "image"
    if lower.endswith(tuple(video_exts)):
        return "video"
    raise DomainError("invalid_media", 400)


def _media_list(value: object, image_exts, video_exts) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_MEDIA:
        raise DomainError("invalid_request", 400)
    result: list[str] = []
    for item in value:
        path = canonical_media_path(item)
        _media_kind(path, image_exts, video_exts)
        if path in result:
            raise DomainError("invalid_request", 400)
        result.append(path)
    return result


def _clean_message(value: object, maximum: int = 2000) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(
        character for character in value
        if ord(character) >= 32 or character in "\n\r\t")
    return text[:maximum]


class WorkspacePublishing:
    """发布/导出的受管适配；所有 Store 查询都带 Identity。"""

    def __init__(self, store: Store, relay, workspace_api, *,
                 publish_mode: str = "automated", db_runner=None,
                 clock=time.time, sleep=asyncio.sleep,
                 poll_interval: float = 0.25) -> None:
        if publish_mode not in POLICY_MODES:
            raise ValueError("publish_mode must be automated or manual")
        self._store = store
        self._relay = relay
        self._workspace_api = workspace_api
        self._publish_mode = publish_mode
        self._db_runner = db_runner
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = float(poll_interval)

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

    # ------------------------------------------------------------------
    # 策略
    # ------------------------------------------------------------------

    def policy(self) -> dict:
        automatic = self._publish_mode == "automated"
        return {"mode": self._publish_mode,
                "automaticSubmission": automatic}

    def _require_automated(self) -> None:
        if self._publish_mode != "automated":
            raise DomainError("publishing_manual_only", 403)

    # ------------------------------------------------------------------
    # 输入校验
    # ------------------------------------------------------------------

    def _publish_payload(self, platform: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise DomainError("invalid_request", 400)
        allowed = {"title", "body", "tags", "media", "persona"}
        if set(payload) - allowed:
            raise DomainError("invalid_request", 400)
        title = _canonical_text(payload.get("title"), "title", MAX_TITLE)
        body = _canonical_text(payload.get("body"), "body", MAX_BODY)
        tags = _canonical_text(payload.get("tags"), "tags", MAX_TAGS)
        persona = _canonical_text(
            payload.get("persona"), "persona", MAX_PERSONA)
        if persona and (persona.startswith((".", "_"))
                        or any(character in persona for character in "/\\:")):
            raise DomainError("invalid_request", 400)
        media = _media_list(
            payload.get("media"), PUBLISH_IMAGE_EXTS, PUBLISH_VIDEO_EXTS)
        if not title.strip() and not body.strip():
            raise DomainError("invalid_request", 400)
        kinds = [_media_kind(path, PUBLISH_IMAGE_EXTS, PUBLISH_VIDEO_EXTS)
                 for path in media]
        if "image" in kinds and "video" in kinds:
            raise DomainError("invalid_media", 400)
        if platform in MEDIA_REQUIRED and not media:
            raise DomainError("media_required", 400)
        if platform in VIDEO_ONLY and "video" not in kinds:
            raise DomainError("video_required", 400)
        if platform == "wechat-oa":
            if "video" in kinds:
                raise DomainError("invalid_media", 400)
            if "image" not in kinds:
                raise DomainError("media_required", 400)
        return {"title": title, "body": body, "tags": tags,
                "media": media, "persona": persona}

    def _export_payload(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise DomainError("invalid_request", 400)
        allowed = {"title", "body", "tags", "media", "reviewed"}
        if set(payload) - allowed:
            raise DomainError("invalid_request", 400)
        if payload.get("reviewed") is not True:
            raise DomainError("review_required", 400)
        title = _canonical_text(payload.get("title"), "title", MAX_TITLE)
        body = _canonical_text(payload.get("body"), "body", MAX_BODY)
        tags = _canonical_text(payload.get("tags"), "tags", MAX_TAGS)
        media = _media_list(
            payload.get("media"), EXPORT_IMAGE_EXTS, EXPORT_VIDEO_EXTS)
        if not title.strip() and not body.strip():
            raise DomainError("invalid_request", 400)
        return {"title": title, "body": body, "tags": tags,
                "media": media, "reviewed": True}

    # ------------------------------------------------------------------
    # 任务会话 / 接纳
    # ------------------------------------------------------------------

    async def _conversation(self, actor: Identity, key: str, title: str):
        return await self._db(
            self._store.get_or_create_task_conversation, actor, key, title)

    async def _latest(self, actor: Identity, key: str):
        conversation = await self._db(
            self._store.get_task_conversation, actor, key)
        if conversation is None:
            return None
        return await self._db(
            self._store.latest_run_for_conversation, actor, conversation.id)

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
                      request_id: str):
        # 未装配 relay 时不创建任务会话，明确 503 且不留任何接纳痕迹。
        relay = self._require_relay()
        conversation = await self._conversation(actor, key, title)
        admission = await relay.submit_task(
            actor, task_type, {
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
                              request_id: str, *, busy_code: str):
        """同一任务键最多一个在途 Run；同 requestId 加入，不同请求拒绝。"""
        active = await self._active(actor, key)
        if active is not None:
            if active.client_request_id == request_id:
                return {"runId": active.id, "duplicate": True}, active
            raise DomainError(busy_code, 409)
        try:
            return await self._submit(
                actor, key, title, task_type, descriptor, request_id)
        except DomainError as error:
            if error.code not in ("conversation_busy", "request_conflict"):
                raise
            active = await self._active(actor, key)
            if active is not None:
                if active.client_request_id == request_id:
                    return {"runId": active.id, "duplicate": True}, active
                raise DomainError(busy_code, 409) from None
            raise

    async def _run_view(self, actor: Identity, run):
        """读取当前 Run 的状态与它自己写下的最新发布状态。"""
        if run.status in _TERMINAL:
            return run.status, None
        relay = self._require_relay()
        try:
            page = await relay.events(actor, run.id, after=0)
        except DomainError as error:
            if error.status == 503:
                raise
            return run.status, None
        if not isinstance(page, dict):
            return run.status, None
        state = page.get("state")
        if not isinstance(state, str) or not state:
            state = run.status
        return state, _status_from_events(page.get("events"))

    async def _finish(self, actor: Identity, run, status: str) -> None:
        if status not in _TERMINAL:
            return
        try:
            await self._db(self._store.finish_run, actor, run.id, status)
        except DomainError:
            pass

    async def _result(self, actor: Identity, run) -> object:
        relay = self._require_relay()
        try:
            data = await relay.messages(
                actor, run.conversation_id, limit=500, offset=0)
            # 任务会话是长期累积的；结果可能在最新一页，不能只读最旧 500 条。
            total = data.get("total") if isinstance(data, dict) else None
            if type(total) is int and total > 500:
                data = await relay.messages(
                    actor, run.conversation_id, limit=500,
                    offset=total - 500)
        except DomainError:
            raise DomainError("publish_result_unavailable", 502) from None
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            raise DomainError("publish_result_unavailable", 502)
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
                    "publish_result_invalid", 502) from None
        raise DomainError("publish_result_unavailable", 502)

    async def _wait_result(self, actor: Identity, run):
        deadline = self._clock() + _EXPORT_TIMEOUT
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
                await self._finish(actor, run, str(state))
                return None
            if self._clock() >= deadline:
                raise DomainError("publish_timeout", 504)
            await self._sleep(self._poll_interval)

    def _result_view(self, value: object, run_id: str) -> dict:
        if not isinstance(value, dict):
            raise DomainError("publish_result_invalid", 502)
        state = value.get("state")
        if state not in _TERMINAL_PUBLISH_STATES:
            raise DomainError("publish_result_invalid", 502)
        ok = value.get("ok")
        if type(ok) is not bool or (state == "success" and ok is not True):
            raise DomainError("publish_result_invalid", 502)
        result = {
            "ok": ok, "state": state,
            "message": _clean_message(value.get("message")),
            "runId": run_id,
        }
        if isinstance(value.get("calendarRecorded"), bool):
            result["calendarRecorded"] = value["calendarRecorded"]
        if isinstance(value.get("calendarError"), str):
            result["calendarError"] = _clean_message(value["calendarError"])
        return result

    # ------------------------------------------------------------------
    # 自动发布
    # ------------------------------------------------------------------

    async def publish(self, actor: Identity, platform: object,
                      payload: object, *, request_id: object = None) -> dict:
        platform = canonical_platform(platform)
        # 策略拒绝必须先于容量、worker 启动与任何 Run 接纳。
        self._require_automated()
        fields = self._publish_payload(platform, payload)
        request_id = (canonical_request_id(request_id)
                      if request_id is not None else uuid.uuid4().hex)
        key = publish_task_key(platform)
        descriptor = _descriptor("publish", platform, **fields)
        _admission, run = await self._join_or_submit(
            actor, key, f"发布：{platform}", "publish", descriptor,
            request_id, busy_code="publish_busy")
        # 所有平台统一“先接纳、后轮询”：HTTP 断连/超时不会让客户端失去
        # 当前离线 Run，刷新或状态查询仍能定位原任务，避免重复外部动作。
        return {
            "async": True, "pending": True,
            "message": "发布已启动，请稍候…",
            "state": "starting", "runId": run.id,
        }

    async def _terminal_status(self, actor: Identity, run,
                               status: str) -> dict:
        """把 worker Run 终态结算为公开发布状态；done 才读取结果。"""
        if status == "done":
            try:
                value = await self._result(actor, run)
            except DomainError:
                value = None
            if isinstance(value, dict) and value.get(
                    "state") in _TERMINAL_PUBLISH_STATES:
                return {"mode": "publish",
                        **self._result_view(value, run.id)}
        message = {
            "cancelled": "发布任务已停止，平台结果未知，请到平台核对。",
            "interrupted": "发布任务中断，平台结果未知，请到平台核对。",
            "failed": "发布未完成，平台结果未知，请到平台核对。",
        }.get(status, "发布结果未知，请到平台核对。")
        return {"mode": "publish", "state": "unknown",
                "message": message, "runId": run.id}

    async def status(self, actor: Identity, platform: object) -> dict:
        platform = canonical_platform(platform)
        run = await self._latest(actor, publish_task_key(platform))
        if run is None:
            return {"mode": "publish", "state": "unknown",
                    "message": "", "runId": ""}
        if run.status in _TERMINAL:
            await self._finish(actor, run, run.status)
            return await self._terminal_status(actor, run, run.status)
        state, browser = await self._run_view(actor, run)
        if state in _TERMINAL:
            # 先同步控制库终态释放容量，再按该 Run 自己的结果结算。
            await self._finish(actor, run, str(state))
            return await self._terminal_status(actor, run, str(state))
        if browser is None:
            return {"mode": "publish", "state": "starting",
                    "message": "发布已启动，请稍候…", "runId": run.id}
        state_value = browser.get("state")
        if state_value not in PUBLISH_STATES:
            state_value = "unknown"
        return {"mode": "publish", "state": state_value,
                "message": _clean_message(browser.get("message")),
                "runId": run.id}

    async def submit_sms(self, actor: Identity, platform: object,
                         code: object, *, run_id: object = None) -> dict:
        platform = canonical_platform(platform)
        self._require_automated()
        if not isinstance(code, str) or not _SMS_CODE.fullmatch(code):
            raise DomainError("invalid_argument", 400)
        key = publish_task_key(platform)
        current = await self._active(actor, key)
        if run_id is not None:
            if not isinstance(run_id, str) or not run_id:
                raise DomainError("invalid_argument", 400)
            owned = await self._db(self._store.assert_run, actor, run_id)
            if current is None or owned.id != current.id:
                raise DomainError("sms_not_pending", 409)
        if current is None:
            raise DomainError("sms_not_pending", 409)
        state, browser = await self._run_view(actor, current)
        if (state in _TERMINAL or browser is None
                or browser.get("state") != "sms_required"):
            raise DomainError("sms_not_pending", 409)
        result = await self._signed_json(
            actor, "POST", f"/api/publish/{platform}/sms",
            payload={"code": code, "runId": current.id})
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise DomainError("sms_rejected", 502)
        return {"ok": True, "runId": current.id}

    # ------------------------------------------------------------------
    # 人工导出
    # ------------------------------------------------------------------

    async def manual_export(self, actor: Identity, platform: object,
                            payload: object, *,
                            request_id: object = None) -> dict:
        platform = canonical_platform(platform)
        fields = self._export_payload(payload)
        request_id = (canonical_request_id(request_id)
                      if request_id is not None else uuid.uuid4().hex)
        key = export_task_key(platform)
        descriptor = _descriptor("manual-export", platform, **fields)
        _admission, run = await self._join_or_submit(
            actor, key, f"导出稿件：{platform}", "publish", descriptor,
            request_id, busy_code="export_busy")
        value = await self._wait_result(actor, run)
        if value is None:
            raise DomainError("export_failed", 502)
        if not isinstance(value, dict) or value.get("published") is not False:
            # 导出永远不能声称已发布；缺失该字段按上游无效处理。
            raise DomainError("export_result_invalid", 502)
        path = canonical_export_path(value.get("path"))
        filename = value.get("filename")
        if not isinstance(filename, str) or not filename \
                or filename != path.rsplit("/", 1)[-1]:
            raise DomainError("export_result_invalid", 502)
        return {"path": path, "filename": filename,
                "published": False, "runId": run.id}

    # ------------------------------------------------------------------
    # 签名短请求
    # ------------------------------------------------------------------

    async def _signed_json(self, actor: Identity, method: str, path: str,
                           *, query=None, payload=None) -> object:
        response = await self._require_api().request(
            actor, method, path, query=query, payload=payload)
        try:
            return response.json()
        except ValueError:
            raise DomainError("upstream_invalid", 502) from None


def _status_from_events(events: object) -> dict | None:
    """只取当前 Run 自己写下的最新 ``browser_status`` 事件。"""
    if not isinstance(events, list):
        return None
    for event in reversed(events):
        if (not isinstance(event, dict)
                or event.get("event") != "activity"):
            continue
        data = event.get("data")
        if not isinstance(data, str) or len(data) > 4096:
            continue
        try:
            value = json.loads(data)
        except (TypeError, ValueError):
            continue
        if (not isinstance(value, dict)
                or value.get("event") != "browser_status"):
            continue
        state = value.get("state")
        if not isinstance(state, str) or not state:
            state = "unknown"
        return {
            "state": state[:32],
            "message": value.get("message")
            if isinstance(value.get("message"), str) else "",
        }
    return None
