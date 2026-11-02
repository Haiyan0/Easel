"""Task 5C2：worker 内受管浏览器/分析任务执行器。

任务本身仍由既有 ``RunManager`` 接纳、签名、计时、停止与落终态；本模块只
把受签名的动作描述转换为原脚本/原 handler 的业务调用，并把进度写成普通
Run 事件。它不是第二套调度，也不保存独立任务账本。

输入描述是 control 固定生成的 JSON，浏览器不能指定脚本、路径或 URL：

* ``{"action":"login","platform":"xiaohongshu"}``
* ``{"action":"mp-login","platform":"wechat-oa"}``
* ``{"action":"whoami","platform":"xiaohongshu","force":true}``
* ``{"action":"analytics","platform":"xiaohongshu"}``
* ``{"action":"credentials","platform":"wechat-oa"}``
* ``{"action":"publish","platform":"xiaohongshu","title":…,"body":…,
  "tags":…,"media":[…],"persona":…}``
* ``{"action":"manual-export","platform":"xiaohongshu","title":…,
  "body":…,"tags":…,"media":[…],"reviewed":true}``

凭证校验的 AppSecret 不进入签名快照或 Run 事件；control 在 Run 接纳后另走
一次带身份的短请求交接给原 handler，执行器只等待该 Run 的受控结果。
"""
from __future__ import annotations

import asyncio
import json

from easel.control.types import DomainError

BROWSER_TASK_TYPES = frozenset({"browser", "analytics", "publish"})

LOGIN_PLATFORMS = frozenset({
    "xiaohongshu", "kuaishou", "weixin-channels", "zhihu",
    "bilibili", "douyin", "wechat-oa",
})
ANALYTICS_PLATFORMS = frozenset({
    "xiaohongshu", "douyin", "kuaishou", "zhihu", "weixin-channels",
    "bilibili", "wechat-oa",
})
CREDENTIAL_PLATFORMS = frozenset({"wechat-oa"})
PUBLISH_PLATFORMS = frozenset({
    "xiaohongshu", "douyin", "kuaishou", "weixin-channels", "zhihu",
    "bilibili", "wechat-oa",
})
MEDIA_REQUIRED_PLATFORMS = frozenset({
    "xiaohongshu", "douyin", "kuaishou", "weixin-channels", "bilibili",
})
VIDEO_ONLY_PLATFORMS = frozenset({"douyin", "weixin-channels", "bilibili"})
IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif",
})
VIDEO_EXTS = frozenset({
    ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".flv", ".ts",
})
MAX_MEDIA = 32
MAX_TITLE = 1000
MAX_BODY = 100_000
MAX_TAGS = 2000
MAX_PERSONA = 200
MAX_MEDIA_PATH = 2048
_MAX_PROMPT_BYTES = 2 * 1024 * 1024

_ACTION_FIELDS = {
    "login": frozenset({"action", "platform"}),
    "mp-login": frozenset({"action", "platform"}),
    "whoami": frozenset({"action", "platform", "force"}),
    "analytics": frozenset({"action", "platform"}),
    "credentials": frozenset({"action", "platform"}),
    "publish": frozenset({
        "action", "platform", "title", "body", "tags", "media", "persona"}),
    "manual-export": frozenset({
        "action", "platform", "title", "body", "tags", "media",
        "reviewed"}),
}
_TERMINAL_LOGIN_STATES = frozenset({"success", "expired", "error"})
_MAX_POLL_SECONDS = 0.5


class BrowserTaskError(DomainError):
    """受管浏览器任务的稳定失败码；只暴露分类，不带脚本输出或凭证。"""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code, status)


def _invalid(code: str = "browser_task_invalid") -> BrowserTaskError:
    return BrowserTaskError(code, 400)


def _text(value: object, maximum: int, *, allow_empty: bool = False) -> str:
    if (not isinstance(value, str)
            or (not allow_empty and not value)
            or len(value) > maximum
            or any(ord(character) < 32 and character not in "\n\r\t"
                   for character in value)):
        raise _invalid()
    return value


def _media_paths(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_MEDIA:
        raise _invalid()
    result: list[str] = []
    for item in value:
        if (not isinstance(item, str) or not item
                or len(item) > MAX_MEDIA_PATH
                or item.startswith(("/", "\\"))
                or any(character in item for character in "\\:\x00\r\n")
                or any(ord(character) < 32 or ord(character) == 127
                       for character in item)):
            raise _invalid("publish_media_invalid")
        parts = item.split("/")
        if (any(not part or part in (".", "..") or part.startswith(".")
                for part in parts)
                or parts[0].startswith("_")
                or parts[0] in ("analytics", "wechat")):
            raise _invalid("publish_media_invalid")
        suffix = "." + item.rsplit(".", 1)[-1].lower() \
            if "." in item.rsplit("/", 1)[-1] else ""
        if suffix not in IMAGE_EXTS | VIDEO_EXTS:
            raise _invalid("publish_media_invalid")
        if item in result:
            raise _invalid("publish_media_invalid")
        result.append(item)
    return result


def _validate_publish(value: dict, action: str) -> None:
    platform = value["platform"]
    if platform not in PUBLISH_PLATFORMS:
        raise _invalid("browser_platform_unsupported")
    title = _text(value.get("title"), MAX_TITLE, allow_empty=True)
    body = _text(value.get("body"), MAX_BODY, allow_empty=True)
    _text(value.get("tags"), MAX_TAGS, allow_empty=True)
    media = _media_paths(value.get("media"))
    if not title.strip() and not body.strip():
        raise _invalid("publish_content_required")
    kinds = {
        "image" if "." + path.rsplit(".", 1)[-1].lower() in IMAGE_EXTS
        else "video" for path in media}
    if "image" in kinds and "video" in kinds:
        raise _invalid("publish_media_invalid")
    if action == "publish":
        _text(value.get("persona"), MAX_PERSONA, allow_empty=True)
        if platform in MEDIA_REQUIRED_PLATFORMS and not media:
            raise _invalid("publish_media_required")
        if platform in VIDEO_ONLY_PLATFORMS and "video" not in kinds:
            raise _invalid("publish_video_required")
        if platform == "wechat-oa":
            if "video" in kinds or "image" not in kinds:
                raise _invalid("publish_media_invalid")
    else:
        if value.get("reviewed") is not True:
            raise _invalid("publish_review_required")


def parse_action(prompt: object, task_type: str) -> dict:
    """严格解析 control 生成的 JSON 描述；未知字段/平台一律拒绝。"""
    if (not isinstance(prompt, str)
            or len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES):
        raise _invalid()
    try:
        value = json.loads(prompt)
    except (TypeError, ValueError):
        raise _invalid() from None
    if not isinstance(value, dict):
        raise _invalid()
    action = value.get("action")
    if not isinstance(action, str) or action not in _ACTION_FIELDS:
        raise _invalid("browser_task_action_unsupported")
    if set(value) != _ACTION_FIELDS[action]:
        raise _invalid()
    platform = value.get("platform")
    if not isinstance(platform, str) or not platform or len(platform) > 64:
        raise _invalid()
    if action in ("login", "whoami"):
        if platform not in LOGIN_PLATFORMS:
            raise _invalid("browser_platform_unsupported")
    if action == "login" and platform == "wechat-oa":
        raise _invalid("browser_login_requires_credentials")
    if action == "mp-login" and platform != "wechat-oa":
        raise _invalid("browser_platform_unsupported")
    if action == "analytics" and platform not in ANALYTICS_PLATFORMS:
        raise _invalid("browser_platform_unsupported")
    if action == "credentials" and platform not in CREDENTIAL_PLATFORMS:
        raise _invalid("browser_platform_unsupported")
    if action in ("publish", "manual-export"):
        _validate_publish(value, action)
    if action == "whoami" and type(value.get("force")) is not bool:
        raise _invalid()
    if task_type == "analytics" and action != "analytics":
        raise _invalid("browser_task_type_mismatch")
    if task_type == "browser" and action == "analytics":
        raise _invalid("browser_task_type_mismatch")
    if task_type == "publish" and action not in ("publish", "manual-export"):
        raise _invalid("browser_task_type_mismatch")
    if task_type in ("browser", "analytics") and action in (
            "publish", "manual-export"):
        raise _invalid("browser_task_type_mismatch")
    return value


def _status_view(status: object) -> dict:
    """只接受公开的登录状态字段；未知状态原样保留为 unknown，不当成功。"""
    if not isinstance(status, dict):
        return {"state": "unknown", "message": "", "qr": "", "qrTs": 0}
    state = status.get("state")
    if not isinstance(state, str) or not state:
        state = "unknown"
    return {
        "state": state,
        "message": status.get("message")
        if isinstance(status.get("message"), str) else "",
        "qr": status.get("qr") if isinstance(status.get("qr"), str) else "",
        "qrTs": status.get("qrTs")
        if type(status.get("qrTs")) is int else 0,
    }


async def _emit_status(emit, status: dict) -> None:
    emit("activity", json.dumps(
        {"event": "browser_status", **status}, ensure_ascii=False))


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _clear_whoami_cache(web_app, platform: str) -> None:
    """登录成功后清掉登录前的 whoami 假阴性缓存。

    原 Web 由 ``GET /api/login/{platform}/status`` 的 success 分支完成；
    受管链路不再用共享 status 推进新 Run，因此由真正见到 success 的执行器
    在同一事件循环里完成同等副作用。
    """
    lock = getattr(web_app, "_WHOAMI_LOCK", None)
    cache = getattr(web_app, "_WHOAMI_CACHE", None)
    if lock is None or not isinstance(cache, dict):
        return
    with lock:
        cache.pop(platform, None)


async def _cancel(web_app, kind: str, platform: str) -> None:
    cancel = getattr(web_app, "cancel_browser_task", None)
    if callable(cancel):
        try:
            await cancel(kind, platform)
        except Exception:  # noqa: BLE001 - 清理失败不覆盖原始取消
            pass


async def _run_login(web_app, descriptor: dict, emit) -> dict:
    platform = descriptor["platform"]
    kind = descriptor["action"]
    if kind == "login":
        status = await web_app._start_login(platform, restart=False)
        if status.get("mode") == "credentials":
            raise BrowserTaskError("browser_login_requires_credentials", 409)
        get_status = lambda: web_app._login_status(platform)  # noqa: E731
        process_key = platform
    else:
        status = await web_app.api_mp_login_start(platform)
        get_status = lambda: web_app._mp_login_status()  # noqa: E731
        process_key = "wechat-oa-mp"
    previous = None
    while True:
        view = _status_view(status)
        if view != previous:
            await _emit_status(emit, view)
            previous = view
        state = view["state"]
        if state == "success":
            # 登录成功后清掉登录前缓存的「未登录」whoami 结果；受管链路
            # 不再通过共享 status 的 GET 触发这个副作用。
            _clear_whoami_cache(web_app, platform)
            settle = getattr(web_app, "settle_login_process", None)
            if callable(settle):
                await settle(process_key)
            return {"state": "success", "message": view["message"]}
        if state in ("expired", "error"):
            await _cancel(web_app, kind, platform)
            raise BrowserTaskError(f"browser_login_{state}", 502)
        await _pause(0.5)
        status = get_status()


async def _run_whoami(web_app, descriptor: dict, emit) -> dict:
    platform = descriptor["platform"]
    try:
        result = await web_app._check_account_whoami(
            platform, bool(descriptor["force"]))
    except web_app.HTTPException as error:
        code = ("whoami_timeout" if error.status_code == 504
                else "whoami_unconfirmed" if error.status_code == 502
                else "whoami_failed")
        raise BrowserTaskError(code, error.status_code) from None
    if not isinstance(result, dict):
        raise BrowserTaskError("whoami_invalid", 502)
    view = {
        "loggedIn": result.get("loggedIn") is True,
        "name": result.get("name")
        if isinstance(result.get("name"), str) else "",
        "avatar": result.get("avatar")
        if isinstance(result.get("avatar"), str) else "",
    }
    await _emit_status(emit, {"state": "whoami_done", "message": ""})
    return view


async def _run_analytics(web_app, descriptor: dict, emit) -> dict:
    platform = descriptor["platform"]
    try:
        result = await web_app._fetch_analytics(platform)
    except web_app.HTTPException as error:
        code = ("analytics_timeout" if error.status_code == 504
                else "analytics_unsupported" if error.status_code == 404
                else "analytics_failed")
        raise BrowserTaskError(code, error.status_code) from None
    if not isinstance(result, dict):
        raise BrowserTaskError("analytics_invalid", 502)
    await _emit_status(emit, {"state": "analytics_done", "message": ""})
    return result


async def _run_credentials(web_app, descriptor: dict, context, emit) -> dict:
    platform = descriptor["platform"]
    wait = getattr(web_app, "wait_credentials_job", None)
    if not callable(wait):
        raise BrowserTaskError("credentials_unavailable", 503)
    result = await wait(platform, context.run_id, timeout=45)
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise BrowserTaskError("credentials_invalid", 502)
    await _emit_status(emit, {
        "state": "credentials_done",
        "message": "验证通过" if result["ok"] else "验证未通过",
    })
    return {
        "ok": result["ok"],
        "message": result.get("message")
        if isinstance(result.get("message"), str) else "",
    }


async def _run_publish(web_app, descriptor, context, emit) -> str:
    if descriptor["action"] == "manual-export":
        runner = getattr(web_app, "run_managed_manual_export", None)
    else:
        runner = getattr(web_app, "run_managed_publish", None)
    if not callable(runner):
        raise BrowserTaskError("publish_unavailable", 503)
    try:
        return await runner(descriptor, context.run_id, emit)
    except web_app.HTTPException as error:
        code = (
            "publish_invalid" if error.status_code == 400
            else "publish_media_missing" if error.status_code == 404
            else "publish_media_invalid" if error.status_code == 403
            else "publish_failed")
        raise BrowserTaskError(code, error.status_code) from None


async def execute_browser_task(request, context, bind, emit, ask):
    """RunManager 的 browser/analytics/publish 执行器；结果写回既有 Run 终态。"""
    del bind, ask
    descriptor = parse_action(request.prompt, context.task_type)
    from web import app as web_app

    action = descriptor["action"]
    platform = descriptor["platform"]
    try:
        if action in ("login", "mp-login"):
            result = await _run_login(web_app, descriptor, emit)
            return json.dumps(result, ensure_ascii=False)
        if action == "whoami":
            result = await _run_whoami(web_app, descriptor, emit)
            return json.dumps(result, ensure_ascii=False)
        if action == "analytics":
            result = await _run_analytics(web_app, descriptor, emit)
            return json.dumps(result, ensure_ascii=False)
        if action == "credentials":
            result = await _run_credentials(
                web_app, descriptor, context, emit)
            return json.dumps(result, ensure_ascii=False)
        if action in ("publish", "manual-export"):
            return await _run_publish(web_app, descriptor, context, emit)
        raise _invalid("browser_task_action_unsupported")
    except asyncio.CancelledError:
        await _cancel(web_app, "publish" if action in (
            "publish", "manual-export") else action, platform)
        raise


__all__ = [
    "ANALYTICS_PLATFORMS", "BROWSER_TASK_TYPES", "BrowserTaskError",
    "CREDENTIAL_PLATFORMS", "LOGIN_PLATFORMS", "execute_browser_task",
    "parse_action",
]
