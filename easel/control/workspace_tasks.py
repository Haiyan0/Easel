"""Task 5C1：技能执行与画像增强的受管 Run 适配。

本模块只做三件事：校验浏览器输入、调用 worker 的“准备”接口得到受信任的
prompt/基线结果、把真正的执行交给既有 ``WorkerRelay.submit_task``。它不启动
Agent、不读写用户文件、不另建任务账本。
"""
from __future__ import annotations

from .store import Store
from .types import DomainError, Identity

_MAX_SKILL = 200
_MAX_INPUT = 64 * 1024
_MAX_PERSONA = 200
_MAX_TURN = 256
_MAX_PAYLOAD = 256 * 1024
_SKILL_CHARS = frozenset("/\\:\x00\r\n")


def _text(value: object, maximum: int, *, allow_empty: bool = False) -> str:
    if (not isinstance(value, str)
            or (not allow_empty and not value)
            or len(value) > maximum
            or any(ord(character) < 32 for character in value)):
        raise DomainError("invalid_argument", 400)
    return value


def canonical_skill_name(value: object) -> str:
    """技能名是单段目录名；真正的存在性仍由 worker 的固定目录复核。"""
    if (not isinstance(value, str) or not value
            or len(value) > _MAX_SKILL
            or value in (".", "..")
            or value.startswith((".", "_"))
            or any(character in value for character in _SKILL_CHARS)):
        raise DomainError("invalid_argument", 400)
    return value


def _run_state(state: object) -> str:
    if state in ("reserved", "running", "accepted", "awaiting_permission"):
        return "running"
    if state in ("done",):
        return "done"
    if state in ("failed", "interrupted", "cancelled"):
        return "failed"
    return "unknown"


class WorkspaceTasks:
    def __init__(self, store: Store, sessions, relay, workspace_api,
                 db_runner=None):
        self._store = store
        self._sessions = sessions
        self._relay = relay
        self._workspace_api = workspace_api
        self._db_runner = db_runner

    async def _db(self, function, *args, **kwargs):
        if self._db_runner is None:
            return function(*args, **kwargs)
        return await self._db_runner(function, *args, **kwargs)

    def _require_api(self):
        if self._workspace_api is None:
            raise DomainError("capability_unavailable", 503)
        return self._workspace_api

    def _require_relay(self):
        if self._relay is None:
            raise DomainError("capability_unavailable", 503)
        return self._relay

    async def _worker_json(self, actor: Identity, path: str,
                           payload: dict) -> dict:
        response = await self._require_api().request(
            actor, "POST", path, payload=payload)
        try:
            data = response.json()
        except ValueError:
            raise DomainError("upstream_invalid", 502) from None
        if not isinstance(data, dict):
            raise DomainError("upstream_invalid", 502)
        return data

    async def _task_conversation(self, actor: Identity, task_key: str,
                                 title: str):
        return await self._db(
            self._store.get_or_create_task_conversation,
            actor, task_key, title)

    async def _latest_run(self, actor: Identity, conversation_id: str):
        return await self._db(
            self._store.latest_run_for_conversation,
            actor, conversation_id)

    async def _refresh_run(self, actor: Identity, run) -> None:
        """读取 worker 事件页，让控制库的 Run 状态跟到最新终态。"""
        if self._relay is None:
            return
        try:
            await self._relay.events(actor, run.id, after=0)
        except DomainError:
            pass

    async def _result_text(self, actor: Identity, conversation_id: str,
                           run_id: str) -> str:
        if self._relay is None:
            return ""
        try:
            data = await self._relay.messages(
                actor, conversation_id, limit=500, offset=0)
        except DomainError:
            return ""
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return ""
        chunks: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            if (message.get("role") == "assistant"
                    and message.get("runId") == run_id
                    and isinstance(message.get("content"), str)):
                chunks.append(message["content"])
        return "\n".join(chunks)

    async def submit_skill(self, actor: Identity, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise DomainError("invalid_argument", 400)
        if set(payload) - {"skill", "input", "persona", "turnId"}:
            raise DomainError("invalid_argument", 400)
        skill = canonical_skill_name(payload.get("skill"))
        raw_input = payload.get("input")
        if not isinstance(raw_input, str) or not raw_input.strip():
            raise DomainError("invalid_argument", 400)
        if len(raw_input.encode("utf-8")) > _MAX_INPUT:
            raise DomainError("payload_too_large", 413)
        persona = payload.get("persona")
        if persona is not None:
            persona = _text(persona, _MAX_PERSONA)
        turn_id = payload.get("turnId")
        if turn_id is not None:
            turn_id = _text(turn_id, _MAX_TURN)

        prepared = await self._worker_json(
            actor, "/api/skill/prepare",
            {"skill": skill, "input": raw_input, "persona": persona})
        canonical = prepared.get("skill")
        prompt = prepared.get("prompt")
        if (not isinstance(canonical, str) or not canonical
                or not isinstance(prompt, str) or not prompt):
            raise DomainError("upstream_invalid", 502)
        if (prepared.get("needsApi") is True
                and prepared.get("apiConfigured") is not True):
            raise DomainError("skill_not_configured", 409)

        conversation = await self._task_conversation(
            actor, f"skill:{canonical}", f"技能执行：{canonical}")
        admission = await self._require_relay().submit_task(
            actor, "skill", {
                "sessionId": conversation.id,
                "turnId": turn_id,
                "message": prompt,
                "persona": persona,
                "attachments": [],
            })
        return {
            **admission,
            "sessionId": conversation.id,
            "skill": canonical,
        }

    async def skill_status(self, actor: Identity, name: object) -> dict:
        skill = canonical_skill_name(name)
        conversation = await self._db(
            self._store.get_task_conversation, actor, f"skill:{skill}")
        if conversation is None:
            return {"state": "unknown", "response": ""}
        run = await self._latest_run(actor, conversation.id)
        if run is None:
            return {"state": "unknown", "response": ""}
        await self._refresh_run(actor, run)
        run = await self._latest_run(actor, conversation.id) or run
        state = _run_state(run.status)
        response = ""
        if state == "done":
            response = await self._result_text(
                actor, conversation.id, run.id)
        return {
            "state": state,
            "runId": run.id,
            "sessionId": conversation.id,
            "response": response,
        }

    async def profile_active(self, actor: Identity, name: object) -> bool:
        """同一画像有未终态增强时，编辑/删除必须明确拒绝而不是并发覆盖。"""
        if (not isinstance(name, str) or not name or name != name.strip()
                or len(name) > 200):
            return False
        from ..persona import valid_persona_name
        if not valid_persona_name(name):
            return False
        conversation = await self._db(
            self._store.get_task_conversation, actor, f"profile:{name}")
        if conversation is None:
            return False
        run = await self._latest_run(actor, conversation.id)
        return bool(run is not None and run.status in (
            "reserved", "running", "awaiting_permission"))

    async def build_profile(self, actor: Identity, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise DomainError("invalid_argument", 400)
        if set(payload) - {"name", "form", "turnId"}:
            raise DomainError("invalid_argument", 400)
        name = payload.get("name")
        if (not isinstance(name, str) or not name or name != name.strip()
                or len(name) > 200):
            raise DomainError("invalid_argument", 400)
        from ..persona import valid_persona_name
        if not valid_persona_name(name):
            raise DomainError("invalid_argument", 400)
        form = payload.get("form")
        if form is None:
            form = {}
        if not isinstance(form, dict):
            raise DomainError("invalid_argument", 400)
        try:
            import json
            encoded = json.dumps(
                form, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8")
        except (TypeError, ValueError):
            raise DomainError("invalid_argument", 400) from None
        if len(encoded) > _MAX_PAYLOAD:
            raise DomainError("payload_too_large", 413)
        turn_id = payload.get("turnId")
        if turn_id is not None:
            turn_id = _text(turn_id, _MAX_TURN)

        prepared = await self._worker_json(
            actor, "/api/profile/baseline",
            {"name": name, "form": form, "reuse": True})
        created = prepared.get("created") is True
        instruction = prepared.get("instruction")
        if not isinstance(instruction, str) or not instruction:
            raise DomainError("upstream_invalid", 502)

        conversation = await self._db(
            self._store.get_task_conversation, actor, f"profile:{name}")
        if not created:
            if conversation is None:
                raise DomainError("profile_exists", 409)
            latest = await self._latest_run(actor, conversation.id)
            if latest is not None:
                if (turn_id is None
                        or latest.client_request_id != turn_id):
                    raise DomainError("profile_exists", 409)
            conversation = await self._task_conversation(
                actor, f"profile:{name}", f"画像增强：{name}")
        else:
            conversation = await self._task_conversation(
                actor, f"profile:{name}", f"画像增强：{name}")

        try:
            admission = await self._require_relay().submit_task(
                actor, "profile", {
                    "sessionId": conversation.id,
                    "turnId": turn_id,
                    "message": instruction,
                    "persona": None,
                    "attachments": [],
                })
        except DomainError as error:
            if not created:
                raise
            # 基线是确定性写入，已经可用；增强接纳失败必须与基线成功区分。
            return {
                "created": True,
                "name": name,
                "async": False,
                "status": "failed",
                "sessionId": conversation.id,
                "detail": error.code,
                "enhancement": {
                    "state": "failed", "detail": error.code,
                },
            }
        return {
            "created": created,
            "name": name,
            "async": True,
            "status": "running",
            "runId": admission.get("runId"),
            "sessionId": conversation.id,
            "enhancement": {"state": "running"},
        }

    async def profile_status(self, actor: Identity, name: object) -> dict:
        if (not isinstance(name, str) or not name or name != name.strip()
                or len(name) > 200):
            raise DomainError("invalid_argument", 400)
        from ..persona import valid_persona_name
        if not valid_persona_name(name):
            raise DomainError("invalid_argument", 400)
        conversation = await self._db(
            self._store.get_task_conversation, actor, f"profile:{name}")
        if conversation is None:
            return {"state": "unknown", "log": ""}
        run = await self._latest_run(actor, conversation.id)
        if run is None:
            return {"state": "unknown", "log": ""}
        await self._refresh_run(actor, run)
        run = await self._latest_run(actor, conversation.id) or run
        state = _run_state(run.status)
        log = ""
        if state == "done":
            log = await self._result_text(actor, conversation.id, run.id)
        return {"state": state, "log": log, "runId": run.id,
                "sessionId": conversation.id}
