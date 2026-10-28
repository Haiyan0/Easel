"""常驻 worker 的逐 Run 接纳、不可变执行上下文与持久事件。

边界：

* 每个 ``POST /internal/runs/{id}`` 先重新计算输入摘要并验证 v2 签名，签名
  必须同时绑定 workspace/run/会话/输入/任务类型/配置修订；任何替换都在
  接纳前以 403 拒绝。
* 接纳、唯一执行认领、状态、事件与问题都落在本人 ``state_root`` 的 SQLite；
  同一 Run/nonce 重复提交只返回持久状态，不会再次启动适配器。
* 执行使用逐 Run 的不可变 ``RunRequest``（env/native/command/model 均来自
  该 Run 的签名快照），不改写全局 ``_WORKER_SNAPSHOT`` 或 ``os.environ``。
* HTTP 请求返回后 supervisor 独立运行；连接断开不取消后台任务，显式停止
  或进程关闭才取消。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..control.types import DomainError, WorkspacePaths
from ..harnesses.base import RunRequest
from .config import (
    RUN_TASK_TYPES,
    RunSnapshot,
    WorkerConfigSnapshot,
    build_run_env,
    run_input_digest,
    run_snapshot_fingerprint,
    verify_run_snapshot,
)
from .fsutil import best_effort_fchmod, fsync_directory
from .sessions import (
    MAX_ATTACHMENTS,
    SUPPORTED_RUNTIMES,
    WorkspaceSessionStore,
)

ACTIVE_STATES = ("accepted", "running", "awaiting_permission")
TERMINAL_STATES = ("done", "failed", "interrupted", "cancelled")
_EVENT_KINDS = frozenset({
    "token", "thinking", "activity", "question", "error", "done"})
_MAX_TIMEOUT = 86400
_DEFAULT_TIMEOUT = 1800
SHUTDOWN_GRACE_ENV = "EASEL_WORKER_DRAIN_TIMEOUT"
DEFAULT_SHUTDOWN_GRACE = 15.0
MAX_SHUTDOWN_GRACE = 120.0
_SHUTDOWN_CANCEL_TIMEOUT = 5.0


def shutdown_grace_from_env(environ: Mapping[str, str] | None = None) -> float:
    """SIGTERM 后允许在途 Run 完成的宽限期；非法值启动失败。"""
    env = os.environ if environ is None else environ
    raw = (env.get(SHUTDOWN_GRACE_ENV) or "").strip()
    if not raw:
        return DEFAULT_SHUTDOWN_GRACE
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("shutdown_grace_invalid") from None
    if not 0.0 <= value <= MAX_SHUTDOWN_GRACE:
        raise ValueError("shutdown_grace_invalid")
    return value


def _placeholders(values: tuple | list) -> str:
    return ", ".join("?" * len(values))

Emit = Callable[[str, str], None]
Ask = Callable[[list[dict]], Awaitable[dict[str, list[str]]]]
Bind = Callable[[str], None]
Executor = Callable[
    [RunRequest, "RunContext", Bind, Emit, Ask], Awaitable[str | None]]


# ---------------------------------------------------------------------------
# 纯校验与不可变上下文
# ---------------------------------------------------------------------------


def _invalid(code: str = "run_invalid") -> DomainError:
    return DomainError(code, 400)


def _forbidden(code: str = "run_forbidden") -> DomainError:
    return DomainError(code, 403)


def _text(value: object, *, maximum: int) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(character) < 32 for character in value)):
        raise _invalid()
    return value


def _optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, maximum=maximum)


def _normalize_attachments(value: object) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise _invalid("attachment_invalid")
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"id", "name", "path"}:
            raise _invalid("attachment_invalid")
        path = item.get("path")
        identifier = item.get("id")
        name = item.get("name")
        if (not isinstance(path, str) or not path or len(path) > 1024
                or any(ord(character) < 32 for character in path)
                or "\\" in path or path.startswith("~")):
            raise _invalid("attachment_invalid")
        if (not isinstance(identifier, str) or not identifier
                or len(identifier) > 128):
            raise _invalid("attachment_invalid")
        if name is None:
            name = PurePosixPath(path).name
        if (not isinstance(name, str) or not name or len(name) > 260
                or any(ord(character) < 32 for character in name)):
            raise _invalid("attachment_invalid")
        normalized.append({"id": identifier, "name": name, "path": path})
    return tuple(normalized)


def _attachment_context(paths: WorkspacePaths, conversation_id: str,
                        attachments: tuple[dict[str, str], ...]) -> str:
    """只允许当前会话 inbox 内的附件，并生成 Agent 侧清单。"""
    if not attachments:
        return ""
    outputs = (paths.workspace_root / "outputs").resolve()
    scope = hashlib.sha256(
        conversation_id.encode()).hexdigest()[:20]
    rows: list[str] = []
    seen: set[str] = set()
    for attachment in attachments:
        relative = PurePosixPath(attachment["path"])
        if (relative.is_absolute() or len(relative.parts) != 4
                or relative.parts[0] != "_inbox"
                or relative.parts[1] != scope):
            raise _forbidden("attachment_forbidden")
        normalized = relative.as_posix()
        expected = hashlib.sha256(
            f"{scope}\0{normalized}".encode()).hexdigest()[:24]
        if attachment["id"] != expected:
            raise _forbidden("attachment_forbidden")
        full = (outputs / normalized).resolve()
        if outputs not in full.parents:
            raise _forbidden("attachment_forbidden")
        if not full.is_file():
            raise DomainError("attachment_missing", 404)
        if normalized in seen:
            continue
        seen.add(normalized)
        rows.append(f"- outputs/{normalized}")
    return (
        "〔系统附件清单，仅供本轮执行，不要向用户复述文件上传过程或内部路径〕\n"
        "只允许使用下列当前会话附件；禁止扫描、枚举或猜测 outputs/_inbox "
        "中的其他文件：\n"
        + "\n".join(rows)
        + "\n需要纳入内容项目时，将清单内文件复制到 outputs/<项目>/assets/ "
          "后再使用；保留 inbox 原件，确保重试仍可复现。"
    )


def _prompt(message: str, persona: str | None, manifest: str) -> str:
    text = message.strip()
    if manifest:
        text = f"{text}\n\n{manifest}" if text else manifest
    if not text:
        raise _invalid("message_required")
    if persona:
        try:
            from easel.persona import chat_turn_message
            return chat_turn_message(text, persona)
        except (ImportError, OSError):
            pass
    return text


def _sandbox(value: object) -> str:
    if value is None:
        return "workspace-write"
    if value not in ("read-only", "workspace-write", "danger-full-access"):
        raise _invalid("agent_config_invalid")
    return str(value)


def _command(value: object, runtime: str) -> tuple[str, ...]:
    if value is None:
        if runtime == "acp":
            raise _invalid("managed_command_required")
        return ()
    if (not isinstance(value, (list, tuple)) or not value
            or len(value) > 64
            or not all(isinstance(part, str) and part and len(part) <= 4096
                       and "\0" not in part and "\n" not in part
                       and "\r" not in part for part in value)):
        raise _invalid("managed_command_invalid")
    return tuple(value)


def _timeout(value: object) -> int:
    if value is None:
        return _DEFAULT_TIMEOUT
    if type(value) is not int or not 1 <= value <= _MAX_TIMEOUT:
        raise _invalid("agent_config_invalid")
    return value


def _sensitive_values(snapshot: RunSnapshot) -> tuple[str, ...]:
    values: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, str):
            if len(value) >= 6:
                values.add(value)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                if (isinstance(key, str) and any(
                        marker in key.upper()
                        for marker in ("TOKEN", "SECRET", "KEY", "PASSWORD"))
                        or isinstance(item, Mapping)):
                    collect(item)

    collect(snapshot.routes)
    collect(snapshot.agent)
    return tuple(sorted(values, key=len, reverse=True))


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    return text


def _scrub_payload(value: object, secrets: tuple[str, ...]) -> object:
    """递归脱敏问题正文/选项/标题等嵌套文本；键名（ID）保持不变。"""
    if isinstance(value, str):
        return _scrub(value, secrets)
    if isinstance(value, list):
        return [_scrub_payload(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_payload(item, secrets)
                for key, item in value.items()}
    return value


OPAQUE_OPTION_PREFIX = "qopt_"


def _public_questions(questions: list[dict], secrets: tuple[str, ...]
                      ) -> tuple[list[dict], dict[str, dict[str, dict[str, str]]]]:
    """生成公开问题与按问题隔离的授权映射。

    公开选项带 opaque ``value``；浏览器可回传 ``value`` 或脱敏后的 label，
    worker 只在**同一问题**内映射回原始选项。Opaque 值随机生成且避开本题
    所有 label，避免 label 假冒 value；脱敏后重复的 label 通过序号区分。
    返回值外层按 questionId 分区，跨题值不会互相翻译。
    """
    public: list[dict] = []
    selections: dict[str, dict[str, dict[str, str]]] = {}
    for question_index, question in enumerate(questions):
        item = _scrub_payload(question, secrets)
        if not isinstance(item, dict):
            raise _invalid("question_invalid")
        question_key = str(question.get(
            "questionId", question.get("id", question_index)))
        if question_key in selections:
            raise _invalid("question_invalid")
        values: dict[str, str] = {}
        labels: dict[str, str] = {}
        options = question.get("options")
        if isinstance(options, list):
            public_options: list[object] = []
            for option_index, option in enumerate(options):
                if isinstance(option, dict):
                    original = option.get("label",
                                          option.get("name", ""))
                    original_text = (
                        original if isinstance(original, str) else str(original))
                    label = _scrub(original_text, secrets) or (
                        f"option {option_index + 1}")
                    while label in labels or label in values:
                        label = f"{label} #{option_index + 1}"
                    labels[label] = original_text
                    base = OPAQUE_OPTION_PREFIX + uuid.uuid4().hex
                    value = base
                    suffix = 1
                    while value in values or value in labels:
                        value = f"{base}_{suffix}"
                        suffix += 1
                    values[value] = original_text
                    public_options.append({
                        **{key: _scrub_payload(field_value, secrets)
                           for key, field_value in option.items()},
                        "label": label,
                        "value": value,
                    })
                else:
                    original_text = (
                        option if isinstance(option, str) else str(option))
                    label = _scrub(original_text, secrets)
                    while label in labels or label in values:
                        label = f"{label} #{option_index + 1}"
                    labels[label] = original_text
                    base = OPAQUE_OPTION_PREFIX + uuid.uuid4().hex
                    value = base
                    suffix = 1
                    while value in values or value in labels:
                        value = f"{base}_{suffix}"
                        suffix += 1
                    values[value] = original_text
                    public_options.append({"label": label, "value": value})
            item["options"] = public_options
        selections[question_key] = {"values": values, "labels": labels}
        public.append(item)
    return public, selections


def _translate_answers(selections: dict[str, dict[str, dict[str, str]]],
                       answers: dict[str, list[str]]) -> dict[str, list[str]]:
    """把公开选择翻译回原选项；跨题/伪造 opaque 值明确拒绝。"""
    translated: dict[str, list[str]] = {}
    for question_key, chosen in answers.items():
        mapping = selections.get(question_key)
        if mapping is None:
            raise _invalid("question_answer_mismatch")
        values = mapping["values"]
        labels = mapping["labels"]
        if not values and not labels:
            # 无选项的自由文本问题：保持原有透传语义
            translated[question_key] = list(chosen)
            continue
        resolved: list[str] = []
        for item in chosen:
            if item in values and item in labels:
                raise DomainError("question_answer_ambiguous", 400)
            if item in values:
                resolved.append(values[item])
            elif item in labels:
                resolved.append(labels[item])
            elif item.startswith(OPAQUE_OPTION_PREFIX):
                # 不得用别题的 opaque 值或伪造前缀冒充本题选项
                raise DomainError("question_answer_ambiguous", 400)
            else:
                raise _invalid("question_answer_mismatch")
        translated[question_key] = resolved
    return translated


@dataclass(frozen=True)
class RunContext:
    """一次执行的不可变身份；适配器只能看到本 Run 的内容。"""

    workspace_id: str
    run_id: str
    conversation_id: str
    task_type: str
    snapshot: RunSnapshot
    runtime: str
    native_id: str
    persona: str | None
    attachments: tuple[dict[str, str], ...]


# ---------------------------------------------------------------------------
# 持久接纳、状态、事件与问题
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_admissions (
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    task_type TEXT NOT NULL,
    revision INTEGER NOT NULL,
    nonce TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    finalizing INTEGER NOT NULL DEFAULT 0,
    snapshot_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, run_id)
);
CREATE TABLE IF NOT EXISTS run_events (
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, run_id, event_id)
);
CREATE TABLE IF NOT EXISTS run_questions (
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    answers_json TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, run_id, question_id)
);
"""


class RunRegistry:
    """worker 侧唯一的 Run 账本；不复制 control 的容量/ID 表。"""

    def __init__(self, path: Path | str, workspace_id: str, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace_id = workspace_id
        self._clock = clock
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            # 旧 worker 库只追加列：不重建、不覆盖既有接纳与事件。
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(run_admissions)")}
            if "finalizing" not in columns:
                connection.execute(
                    "ALTER TABLE run_admissions ADD COLUMN finalizing"
                    " INTEGER NOT NULL DEFAULT 0")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _now(self) -> int:
        return int(self._clock())

    def _append_locked(self, connection: sqlite3.Connection, run_id: str,
                       event: str, data: object, now: int) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(event_id), 0) + 1 FROM run_events"
            " WHERE workspace_id = ? AND run_id = ?",
            (self.workspace_id, run_id)).fetchone()
        event_id = int(row[0])
        connection.execute(
            "INSERT INTO run_events"
            " (workspace_id, run_id, event_id, event, data_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (self.workspace_id, run_id, event_id, event,
             json.dumps(data, ensure_ascii=False, separators=(",", ":")),
             now))
        return event_id

    def admit(self, snapshot: RunSnapshot, snapshot_json: str) -> dict:
        """原子接纳；重复同 nonce+同内容返回原状态，其它重用 409。"""
        fingerprint = run_snapshot_fingerprint(snapshot)
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT nonce, fingerprint, state FROM run_admissions"
                    " WHERE workspace_id = ? AND run_id = ?",
                    (self.workspace_id, snapshot.run_id)).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO run_admissions"
                        " (workspace_id, run_id, conversation_id,"
                        " input_digest, task_type, revision, nonce,"
                        " fingerprint, state, snapshot_json, created_at,"
                        " updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?)",
                        (self.workspace_id, snapshot.run_id,
                         snapshot.conversation_id, snapshot.input_digest,
                         snapshot.task_type, snapshot.revision,
                         snapshot.nonce, fingerprint, snapshot_json,
                         now, now))
                    self._append_locked(
                        connection, snapshot.run_id, "state",
                        {"state": "accepted"}, now)
                    connection.execute("COMMIT")
                    return {"state": "accepted", "duplicate": False}
                if (row["nonce"] == snapshot.nonce
                        and row["fingerprint"] == fingerprint):
                    connection.execute("COMMIT")
                    return {"state": row["state"], "duplicate": True}
                connection.execute("ROLLBACK")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        raise DomainError("snapshot_conflict", 409)

    def claim(self, run_id: str, snapshot: RunSnapshot) -> bool:
        """accepted→running 的唯一认领；重复/并发读同一行只成功一次。"""
        fingerprint = run_snapshot_fingerprint(snapshot)
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT nonce, fingerprint, state FROM run_admissions"
                    " WHERE workspace_id = ? AND run_id = ?",
                    (self.workspace_id, run_id)).fetchone()
                if (row is None or row["state"] != "accepted"
                        or row["nonce"] != snapshot.nonce
                        or row["fingerprint"] != fingerprint):
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "UPDATE run_admissions SET state = 'running',"
                    " updated_at = ? WHERE workspace_id = ? AND run_id = ?",
                    (now, self.workspace_id, run_id))
                self._append_locked(
                    connection, run_id, "state", {"state": "running"}, now)
                connection.execute("COMMIT")
                return True
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def state(self, run_id: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state FROM run_admissions"
                " WHERE workspace_id = ? AND run_id = ?",
                (self.workspace_id, run_id)).fetchone()
        return None if row is None else str(row["state"])

    def admission(self, run_id: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM run_admissions"
                " WHERE workspace_id = ? AND run_id = ?",
                (self.workspace_id, run_id)).fetchone()
        if row is None:
            return None
        return {
            "runId": row["run_id"],
            "conversationId": row["conversation_id"],
            "inputDigest": row["input_digest"],
            "taskType": row["task_type"],
            "revision": row["revision"],
            "nonce": row["nonce"],
            "fingerprint": row["fingerprint"],
            "state": row["state"],
            "reason": row["reason"],
            "snapshot": json.loads(row["snapshot_json"]),
        }

    def transition(self, run_id: str, expected: tuple[str, ...], state: str,
                   reason: str | None = None) -> bool:
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT state FROM run_admissions"
                    " WHERE workspace_id = ? AND run_id = ?",
                    (self.workspace_id, run_id)).fetchone()
                if row is None or row["state"] not in expected:
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "UPDATE run_admissions SET state = ?, reason = ?,"
                    " finalizing = 0, updated_at = ?"
                    " WHERE workspace_id = ? AND run_id = ?",
                    (state, reason, now, self.workspace_id, run_id))
                data: dict[str, object] = {"state": state}
                if reason:
                    data["reason"] = reason
                self._append_locked(connection, run_id, "state", data, now)
                if state in TERMINAL_STATES:
                    connection.execute(
                        "UPDATE run_questions SET status = 'expired',"
                        " updated_at = ? WHERE workspace_id = ?"
                        " AND run_id = ? AND status = 'pending'",
                        (now, self.workspace_id, run_id))
                connection.execute("COMMIT")
                return True
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def finish_if_active(self, run_id: str, state: str,
                         reason: str | None = None) -> bool:
        """仅活跃且未进入收尾的 Run 可终止；收尾中的输出已开始落盘。

        条件与写入在同一事务内完成，避免检查后恰好被 ``begin_finalize``
        抢占，把正在保存的 run 取消。
        """
        if state not in TERMINAL_STATES:
            raise ValueError("finish_if_active requires a terminal state")
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE run_admissions SET state = ?, reason = ?,"
                    " finalizing = 0, updated_at = ?"
                    " WHERE workspace_id = ? AND run_id = ?"
                    " AND finalizing = 0 AND state IN"
                    f" ({_placeholders(ACTIVE_STATES)})",
                    (state, reason, now, self.workspace_id, run_id,
                     *ACTIVE_STATES))
                if cursor.rowcount != 1:
                    connection.execute("COMMIT")
                    return False
                data: dict[str, object] = {"state": state}
                if reason:
                    data["reason"] = reason
                self._append_locked(connection, run_id, "state", data, now)
                connection.execute(
                    "UPDATE run_questions SET status = 'expired',"
                    " updated_at = ? WHERE workspace_id = ? AND run_id = ?"
                    " AND status = 'pending'",
                    (now, self.workspace_id, run_id))
                connection.execute("COMMIT")
                return True
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    # -- 输出收尾协议 ------------------------------------------------------

    def begin_finalize(self, run_id: str) -> bool:
        """声明唯一成功收尾；停止不能再把正在保存输出的 Run 取消。

        该标记与运行状态分离：公开状态仍为 running，只有输出写入成功后才
        提交终态，因此消费者不会在消息落盘前看到 done。
        """
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE run_admissions SET finalizing = 1,"
                    " updated_at = ? WHERE workspace_id = ? AND run_id = ?"
                    " AND finalizing = 0 AND state IN"
                    f" ({_placeholders(ACTIVE_STATES)})",
                    (now, self.workspace_id, run_id, *ACTIVE_STATES))
                connection.execute("COMMIT")
                return cursor.rowcount == 1
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def finish_finalizing(self, run_id: str, state: str,
                          reason: str | None = None) -> bool:
        """输出已落盘后提交唯一成功/失败终态；只由收尾声明者调用。"""
        if state not in TERMINAL_STATES:
            raise ValueError("finish_finalizing requires a terminal state")
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT state, finalizing FROM run_admissions"
                    " WHERE workspace_id = ? AND run_id = ?",
                    (self.workspace_id, run_id)).fetchone()
                if row is None or not row["finalizing"]:
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "UPDATE run_admissions SET state = ?, reason = ?,"
                    " finalizing = 0, updated_at = ?"
                    " WHERE workspace_id = ? AND run_id = ?",
                    (state, reason, now, self.workspace_id, run_id))
                data: dict[str, object] = {"state": state}
                if reason:
                    data["reason"] = reason
                self._append_locked(connection, run_id, "state", data, now)
                connection.execute(
                    "UPDATE run_questions SET status = 'expired',"
                    " updated_at = ? WHERE workspace_id = ? AND run_id = ?"
                    " AND status = 'pending'",
                    (now, self.workspace_id, run_id))
                connection.execute("COMMIT")
                return True
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def finalizing_runs(self) -> list[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT run_id FROM run_admissions"
                " WHERE workspace_id = ? AND finalizing = 1",
                (self.workspace_id,)).fetchall()
        return [str(row["run_id"]) for row in rows]

    def append_event(self, run_id: str, event: str, data: object) -> int | None:
        """终态后冻结业务事件；返回 None 表示被终态拦截。"""
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT state FROM run_admissions"
                    " WHERE workspace_id = ? AND run_id = ?",
                    (self.workspace_id, run_id)).fetchone()
                if row is None or row["state"] in TERMINAL_STATES:
                    connection.execute("COMMIT")
                    return None
                event_id = self._append_locked(
                    connection, run_id, event, data, now)
                connection.execute("COMMIT")
                return event_id
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def events(self, run_id: str, after: int = 0) -> dict:
        if type(after) is not int or after < 0:
            raise _invalid("invalid_cursor")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state FROM run_admissions"
                " WHERE workspace_id = ? AND run_id = ?",
                (self.workspace_id, run_id)).fetchone()
            if row is None:
                raise DomainError("run_not_found", 404)
            maximum = connection.execute(
                "SELECT COALESCE(MAX(event_id), 0) FROM run_events"
                " WHERE workspace_id = ? AND run_id = ?",
                (self.workspace_id, run_id)).fetchone()[0]
            if after > int(maximum):
                raise _invalid("invalid_cursor")
            rows = connection.execute(
                "SELECT event_id, event, data_json FROM run_events"
                " WHERE workspace_id = ? AND run_id = ? AND event_id > ?"
                " ORDER BY event_id",
                (self.workspace_id, run_id, after)).fetchall()
        events = [{
            "eventId": int(item["event_id"]),
            "event": item["event"],
            "data": json.loads(item["data_json"]),
        } for item in rows]
        return {
            "runId": run_id,
            "state": row["state"],
            "events": events,
            "lastEventId": int(maximum),
        }

    # -- 问题 --------------------------------------------------------------

    def open_question(self, run_id: str, question_id: str,
                      payload: dict, event_data: object) -> bool:
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT state FROM run_admissions"
                    " WHERE workspace_id = ? AND run_id = ?",
                    (self.workspace_id, run_id)).fetchone()
                if row is None or row["state"] not in (
                        "running", "awaiting_permission"):
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO run_questions"
                    " (workspace_id, run_id, question_id, payload_json,"
                    " status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                    (self.workspace_id, run_id, question_id,
                     json.dumps(payload, ensure_ascii=False,
                                separators=(",", ":")), now, now))
                if row["state"] != "awaiting_permission":
                    connection.execute(
                        "UPDATE run_admissions SET state ="
                        " 'awaiting_permission', updated_at = ?"
                        " WHERE workspace_id = ? AND run_id = ?",
                        (now, self.workspace_id, run_id))
                    self._append_locked(
                        connection, run_id, "state",
                        {"state": "awaiting_permission"}, now)
                self._append_locked(
                    connection, run_id, "question", event_data, now)
                connection.execute("COMMIT")
                return True
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def answer(self, run_id: str, question_id: str,
               answers: Mapping[str, list[str]]) -> dict:
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status, payload_json FROM run_questions"
                    " WHERE workspace_id = ? AND run_id = ?"
                    " AND question_id = ?",
                    (self.workspace_id, run_id, question_id)).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    raise DomainError("question_not_found", 404)
                if row["status"] == "resolved":
                    connection.execute("COMMIT")
                    raise DomainError("question_resolved", 409)
                if row["status"] != "pending":
                    connection.execute("COMMIT")
                    raise DomainError("question_expired", 409)
                run = connection.execute(
                    "SELECT state FROM run_admissions"
                    " WHERE workspace_id = ? AND run_id = ?",
                    (self.workspace_id, run_id)).fetchone()
                if (run is None or run["state"] not in
                        ("running", "awaiting_permission")):
                    connection.execute(
                        "UPDATE run_questions SET status = 'expired',"
                        " updated_at = ? WHERE workspace_id = ?"
                        " AND run_id = ? AND question_id = ?",
                        (now, self.workspace_id, run_id, question_id))
                    connection.execute("COMMIT")
                    raise DomainError("question_expired", 409)
                payload = json.loads(row["payload_json"])
                required = set(_question_keys(payload.get("questions") or []))
                if set(answers) != required:
                    connection.execute("COMMIT")
                    raise _invalid("question_answer_mismatch")
                connection.execute(
                    "UPDATE run_questions SET status = 'resolved',"
                    " answers_json = ?, updated_at = ?"
                    " WHERE workspace_id = ? AND run_id = ?"
                    " AND question_id = ? AND status = 'pending'",
                    (json.dumps(dict(answers), ensure_ascii=False,
                                separators=(",", ":")),
                     now, self.workspace_id, run_id, question_id))
                # 同一 Run 可以同时存在多个权限问题；只有最后一个未决问题
                # 结束后才恢复 running，否则其它问题的 answer 会被误判过期。
                pending = connection.execute(
                    "SELECT COUNT(*) AS total FROM run_questions"
                    " WHERE workspace_id = ? AND run_id = ?"
                    " AND status = 'pending'",
                    (self.workspace_id, run_id)).fetchone()["total"]
                if int(pending) == 0 and run["state"] == "awaiting_permission":
                    connection.execute(
                        "UPDATE run_admissions SET state = 'running',"
                        " updated_at = ? WHERE workspace_id = ? AND run_id = ?",
                        (now, self.workspace_id, run_id))
                    self._append_locked(
                        connection, run_id, "state", {"state": "running"}, now)
                connection.execute("COMMIT")
                return {"ok": True, "status": "resolved"}
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def expire_question(self, run_id: str, question_id: str) -> None:
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE run_questions SET status = 'expired',"
                    " updated_at = ? WHERE workspace_id = ? AND run_id = ?"
                    " AND question_id = ? AND status = 'pending'",
                    (now, self.workspace_id, run_id, question_id))
                pending = connection.execute(
                    "SELECT COUNT(*) AS total FROM run_questions"
                    " WHERE workspace_id = ? AND run_id = ?"
                    " AND status = 'pending'",
                    (self.workspace_id, run_id)).fetchone()["total"]
                if int(pending) == 0:
                    row = connection.execute(
                        "SELECT state FROM run_admissions"
                        " WHERE workspace_id = ? AND run_id = ?",
                        (self.workspace_id, run_id)).fetchone()
                    if row is not None and row["state"] == "awaiting_permission":
                        connection.execute(
                            "UPDATE run_admissions SET state = 'running',"
                            " updated_at = ? WHERE workspace_id = ?"
                            " AND run_id = ?",
                            (now, self.workspace_id, run_id))
                        self._append_locked(
                            connection, run_id, "state",
                            {"state": "running"}, now)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def question_status(self, run_id: str,
                        question_ids: list[str]) -> dict:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state FROM run_admissions"
                " WHERE workspace_id = ? AND run_id = ?",
                (self.workspace_id, run_id)).fetchone()
            if row is None:
                raise DomainError("run_not_found", 404)
            found: dict[str, str] = {}
            for question_id in question_ids:
                question = connection.execute(
                    "SELECT status FROM run_questions"
                    " WHERE workspace_id = ? AND run_id = ?"
                    " AND question_id = ?",
                    (self.workspace_id, run_id, question_id)).fetchone()
                found[question_id] = (
                    "not_found" if question is None else question["status"])
        return {"ok": True, "questions": {
            question_id: {"status": status}
            for question_id, status in found.items()}}

    # -- 恢复 --------------------------------------------------------------

    def recover_inflight(self, reason: str = "worker_restarted") -> list[str]:
        """进程重载：残留活跃 Run 明确转 interrupted，问题作废。

        已进入输出收尾（``finalizing=1``）的 Run 不在本方法处理；调用方读取
        暂存输出后提交 done/failed，避免把已保存的输出当作崩溃丢弃。
        """
        now = self._now()
        recovered: list[str] = []
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT run_id, finalizing FROM run_admissions"
                    " WHERE workspace_id = ? AND state IN"
                    " ('accepted', 'running', 'awaiting_permission')",
                    (self.workspace_id,)).fetchall()
                for row in rows:
                    if row["finalizing"]:
                        continue
                    run_id = str(row["run_id"])
                    connection.execute(
                        "UPDATE run_admissions SET state = 'interrupted',"
                        " reason = ?, updated_at = ?"
                        " WHERE workspace_id = ? AND run_id = ?",
                        (reason, now, self.workspace_id, run_id))
                    self._append_locked(
                        connection, run_id, "state",
                        {"state": "interrupted", "reason": reason}, now)
                    recovered.append(run_id)
                if recovered:
                    connection.execute(
                        "UPDATE run_questions SET status = 'expired',"
                        " updated_at = ? WHERE workspace_id = ?"
                        " AND status = 'pending' AND run_id IN"
                        f" ({_placeholders(recovered)})",
                        (now, self.workspace_id, *recovered))
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return recovered


def _question_keys(questions: list) -> list[str]:
    keys: list[str] = []
    for index, question in enumerate(questions):
        if not isinstance(question, Mapping):
            raise _invalid("question_invalid")
        key = question.get("questionId", question.get("id", str(index)))
        if not isinstance(key, str) or not key or len(key) > 256:
            raise _invalid("question_invalid")
        keys.append(key)
    return keys


# ---------------------------------------------------------------------------
# 执行管理
# ---------------------------------------------------------------------------


class RunManager:
    """逐 Run 接纳与执行；一个进程内只维护一份持久登记。"""

    def __init__(self, *, workspace_id: str, paths: WorkspacePaths,
                 public_key, sessions: WorkspaceSessionStore | None = None,
                 executor: Executor | None = None,
                 base_env: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.time,
                 native_binding_version: int = 1,
                 question_timeout: int = _DEFAULT_TIMEOUT,
                 recover: bool = True,
                 shutdown_grace: float = 0.0) -> None:
        if not isinstance(paths, WorkspacePaths):
            raise TypeError("paths must be WorkspacePaths")
        if type(native_binding_version) is not int or native_binding_version < 1:
            raise ValueError("native_binding_version must be positive")
        if type(question_timeout) is not int or not 1 <= question_timeout <= _MAX_TIMEOUT:
            raise ValueError("question_timeout out of range")
        if (isinstance(shutdown_grace, bool)
                or not isinstance(shutdown_grace, (int, float))
                or not 0.0 <= float(shutdown_grace) <= MAX_SHUTDOWN_GRACE):
            raise ValueError("shutdown_grace out of range")
        self._shutdown_grace = float(shutdown_grace)
        self.workspace_id = workspace_id
        self._paths = paths
        self._public_key = public_key
        self._sessions = sessions or WorkspaceSessionStore(
            paths.state_root, workspace_id)
        self._custom_executor = executor is not None
        self._executor = executor or _harness_executor
        self._base_env = dict(base_env if base_env is not None else os.environ)
        self._clock = clock
        self._native_version = native_binding_version
        self._question_timeout = question_timeout
        self.registry = RunRegistry(
            paths.state_root / "runs.sqlite3", workspace_id, clock=clock)
        self._tasks: dict[str, asyncio.Task] = {}
        self._questions: dict[tuple[str, str], asyncio.Future] = {}
        self._active_conversations: dict[str, str] = {}
        self._closing = False
        self._closed = False
        self._draining = False
        self._force_stop = False
        if recover:
            self.registry.recover_inflight()
            # 已保存输出但尚未提交 done 的 Run：补提交，消息按 runId 去重。
            self._recover_finalizing()
            # 失败/中断/未知 Run 的暂存输出保留，不按“不在 finalizing”删除。
            self._recover_orphan_outputs()

    # -- 公开只读接口 ------------------------------------------------------

    def active_runs(self) -> int:
        """真实在途 Run 数；SSE/请求断开不影响该计数。"""
        return sum(1 for task in self._tasks.values() if not task.done())

    def begin_drain(self) -> None:
        """停止接纳新 Run；已接纳的 supervisor 继续执行。"""
        self._draining = True

    def is_draining(self) -> bool:
        return self._draining

    def request_force_stop(self) -> None:
        """重复 SIGTERM：跳过剩余宽限期，立即取消在途 Run。"""
        self._force_stop = True

    def state(self, run_id: str) -> str | None:
        return self.registry.state(run_id)

    def admission(self, run_id: str) -> dict | None:
        return self.registry.admission(run_id)

    def events(self, run_id: str, after: int = 0,
               nonce: str | None = None) -> dict:
        """读取持久事件；对账可携带原派发 nonce 复核接纳身份。"""
        if nonce is not None:
            if not isinstance(nonce, str) or not nonce or len(nonce) > 128:
                raise DomainError("run_binding_mismatch", 403)
            admission = self.registry.admission(run_id)
            if admission is None:
                raise DomainError("run_not_found", 404)
            if admission.get("nonce") != nonce:
                raise DomainError("run_binding_mismatch", 403)
        return self.registry.events(run_id, after)

    def question_status(self, run_id: str,
                        question_ids: list[str]) -> dict:
        return self.registry.question_status(run_id, question_ids)

    def messages(self, session_id: str, *, limit: int = 100,
                 offset: int = 0) -> dict:
        return {
            "sessionId": session_id,
            "messages": self._sessions.messages(
                session_id, limit=limit, offset=offset),
            "total": self._sessions.count_messages(session_id),
        }

    # -- 接纳 --------------------------------------------------------------

    def _verify(self, run_id: str, envelope: object,
                request: object) -> tuple[dict, RunSnapshot, dict]:
        if self._public_key is None:
            raise DomainError("capability_unavailable", 503)
        if not isinstance(envelope, dict) or not isinstance(request, dict):
            raise _invalid()
        conversation_id = _text(request.get("sessionId"), maximum=256)
        task_type = _text(request.get("taskType"), maximum=32)
        if task_type not in RUN_TASK_TYPES:
            raise _invalid()
        message = request.get("message")
        if not isinstance(message, str) or not message:
            raise _invalid("message_required")
        attachments = _normalize_attachments(request.get("attachments"))
        persona = _optional_text(request.get("persona"), maximum=200)
        digest = run_input_digest(
            conversation_id=conversation_id, task_type=task_type,
            message=message, attachments=list(attachments), persona=persona)
        supplied_digest = request.get("inputDigest")
        if not isinstance(supplied_digest, str) or supplied_digest != digest:
            raise _invalid("input_digest_mismatch")
        snapshot = verify_run_snapshot(
            envelope, self._public_key,
            expected_workspace_id=self.workspace_id,
            expected_run_id=run_id,
            expected_conversation_id=conversation_id,
            expected_input_digest=digest,
            expected_task_type=task_type,
            now=int(self._clock()))
        self._sessions.validate_message({
            "role": "user",
            "content": message.strip(),
            "attachments": list(attachments),
        })
        normalized = {
            "sessionId": conversation_id,
            "taskType": task_type,
            "message": message,
            "attachments": attachments,
            "persona": persona,
        }
        return envelope, snapshot, normalized

    async def submit(self, run_id: str, envelope: object,
                     request: object) -> dict:
        if self._draining:
            raise DomainError("worker_draining", 503)
        envelope, snapshot, normalized = self._verify(
            run_id, envelope, request)
        admission = self.registry.admit(
            snapshot, json.dumps(envelope, sort_keys=True,
                                 separators=(",", ":")))
        if admission["duplicate"]:
            return {"runId": run_id, "state": admission["state"],
                    "duplicate": True}
        if not self.registry.claim(run_id, snapshot):
            return {"runId": run_id, "state": self.registry.state(run_id),
                    "duplicate": True}
        try:
            context, run_request = self._prepare(snapshot, normalized)
        except DomainError:
            self.registry.finish_if_active(
                run_id, "failed", "admission_rejected")
            raise
        task = asyncio.create_task(
            self._supervise(context, run_request),
            name=f"easel-run:{run_id}")
        self._tasks[run_id] = task

        def _cleanup(finished: asyncio.Task) -> None:
            if self._tasks.get(run_id) is finished:
                self._tasks.pop(run_id, None)

        task.add_done_callback(_cleanup)
        return {"runId": run_id, "state": "running", "duplicate": False}

    def _prepare(self, snapshot: RunSnapshot, request: dict
                 ) -> tuple[RunContext, RunRequest]:
        agent = snapshot.agent
        runtime = agent.get("runtime")
        if runtime not in SUPPORTED_RUNTIMES:
            raise _invalid("agent_runtime_unsupported")
        if not self._custom_executor and runtime not in (
                "acp", "opencode", "codex"):
            raise DomainError("capability_unavailable", 503)
        conversation_id = request["sessionId"]
        if conversation_id in self._active_conversations:
            holder = self._active_conversations[conversation_id]
            holder_state = self.registry.state(holder)
            holder_task = self._tasks.get(holder)
            if (holder_state in TERMINAL_STATES
                    or (holder_task is not None and holder_task.done())):
                # 上一轮 supervisor 已到终态/任务已结束但锁未释放：修复
                # 陈旧锁。终态 Run 不可能仍在执行，不能永久阻塞同会话。
                self._active_conversations.pop(conversation_id, None)
            else:
                raise DomainError("conversation_busy", 409)
        command = _command(agent.get("command"), str(runtime))
        model = agent.get("model")
        if model is not None and (not isinstance(model, str)
                                  or len(model) > 256):
            raise _invalid("agent_config_invalid")
        sandbox = _sandbox(agent.get("sandbox"))
        timeout = _timeout(agent.get("timeout"))
        auto_approve = agent.get("codex_auto_approve")
        if auto_approve is not None and type(auto_approve) is not bool:
            raise _invalid("agent_config_invalid")

        binding = self._sessions.binding(conversation_id)
        native_id = ""
        if binding:
            if binding.get("runtime") != runtime:
                raise DomainError("session_binding_conflict", 409)
            if binding.get("agent_version") != self._native_version:
                raise DomainError("session_version_unsupported", 409)
            native_id = binding.get("native_id") or ""
            if not native_id and self._sessions.messages(
                    conversation_id, limit=1):
                if conversation_id in self._active_conversations:
                    raise DomainError("session_binding_incomplete", 409)
                # 上次启动在建立 native session 前失败（无活动 Run）：
                # 允许本轮重新 session/new 并写回新的 native id，保留
                # 已存的 Easel 消息历史；不能永久卡死该会话。
        else:
            self._sessions.bind(
                conversation_id, str(runtime), "", self._native_version,
                cwd=str(self._paths.workspace_root))

        manifest = _attachment_context(
            self._paths, conversation_id, request["attachments"])
        prompt = _prompt(request["message"], request["persona"], manifest)
        user_message: dict[str, object] = {
            "role": "user", "content": request["message"].strip()}
        if request["attachments"]:
            user_message["attachments"] = list(request["attachments"])
        self._sessions.append_message(conversation_id, user_message)

        config = WorkerConfigSnapshot(
            workspace_id=snapshot.workspace_id,
            run_id=snapshot.run_id,
            revision=snapshot.revision,
            routes=snapshot.routes,
            agent=snapshot.agent,
            nonce=snapshot.nonce,
            expires_at=snapshot.expires_at,
        )
        env = build_run_env(self._paths, config, self._base_env)
        run_request = RunRequest(
            prompt=prompt,
            cwd=self._paths.workspace_root,
            env=env,
            native_id=native_id,
            model=model or "",
            sandbox=sandbox,
            timeout=timeout,
            command=command,
            session_key=conversation_id,
            codex_auto_approve=bool(auto_approve),
            workspace_id=self.workspace_id,
            code_root=self._paths.code_root,
        )
        context = RunContext(
            workspace_id=self.workspace_id,
            run_id=snapshot.run_id,
            conversation_id=conversation_id,
            task_type=snapshot.task_type,
            snapshot=snapshot,
            runtime=str(runtime),
            native_id=native_id,
            persona=request["persona"],
            attachments=request["attachments"],
        )
        self._active_conversations[conversation_id] = snapshot.run_id
        return context, run_request

    # -- 执行 --------------------------------------------------------------

    def _emit_for(self, context: RunContext, chunks: list[str],
                  secrets: tuple[str, ...]) -> Emit:
        def emit(kind: str, text: object) -> None:
            if kind not in _EVENT_KINDS:
                kind = "activity"
            if not isinstance(text, str):
                text = json.dumps(text, ensure_ascii=False)
            text = _scrub(text, secrets)
            if kind == "token":
                chunks.append(text)
            self.registry.append_event(context.run_id, kind, text)

        return emit

    def _bind_for(self, context: RunContext) -> Bind:
        def bind(native_id: str) -> None:
            if (not isinstance(native_id, str) or not native_id
                    or len(native_id) > 512
                    or any(character in native_id
                           for character in ("\0", "\n", "\r", "\t"))):
                raise _invalid("native_id_invalid")
            self._sessions.bind(
                context.conversation_id, context.runtime, native_id,
                self._native_version,
                cwd=str(self._paths.workspace_root))

        return bind

    def _ask_for(self, context: RunContext) -> Ask:
        secrets = _sensitive_values(context.snapshot)

        async def ask(questions: list[dict]) -> dict[str, list[str]]:
            if (not isinstance(questions, list) or not questions
                    or len(questions) > 20):
                raise _invalid("question_invalid")
            if not all(isinstance(item, dict) for item in questions):
                raise _invalid("question_invalid")
            _question_keys(questions)
            question_id = "runq_" + uuid.uuid4().hex
            public_questions, selections = _public_questions(questions, secrets)
            payload = {"id": question_id, "questions": public_questions}
            key = (context.run_id, question_id)
            future = asyncio.get_running_loop().create_future()
            # 公开文本已脱敏；浏览器只能返回 value/label，内部映射回原选项，
            # 不能把展示文本当作授权值，也不能借问题正文泄漏 Run token。
            self._questions[key] = {"future": future,
                                    "selections": selections}
            opened = self.registry.open_question(
                context.run_id, question_id, payload,
                json.dumps(payload, ensure_ascii=False))
            if not opened:
                self._questions.pop(key, None)
                raise DomainError("run_not_active", 409)
            try:
                return await asyncio.wait_for(
                    future, timeout=self._question_timeout)
            except asyncio.TimeoutError:
                self.registry.expire_question(context.run_id, question_id)
                raise
            finally:
                self._questions.pop(key, None)
                self.registry.expire_question(context.run_id, question_id)

        return ask

    async def _supervise(self, context: RunContext,
                         request: RunRequest) -> None:
        run_id = context.run_id
        chunks: list[str] = []
        secrets = _sensitive_values(context.snapshot)
        emit = self._emit_for(context, chunks, secrets)
        bind = self._bind_for(context)
        ask = self._ask_for(context)

        try:
            result = await asyncio.wait_for(
                self._executor(request, context, bind, emit, ask),
                timeout=request.timeout)
        except asyncio.CancelledError:
            self.registry.finish_if_active(run_id, "cancelled", "stopped")
            # 取消也是终态：必须释放会话锁，否则停止后同会话永远 busy
            self._release(context)
            raise
        except asyncio.TimeoutError:
            emit("error", "Agent 执行超时，已终止本轮进程")
            self.registry.finish_if_active(run_id, "failed", "timeout")
            self._release(context)
            return
        except DomainError as error:
            emit("error", error.code)
            self.registry.finish_if_active(
                run_id, "failed", error.code)
            self._release(context)
            return
        except Exception:  # noqa: BLE001 - 适配器边界统一收敛失败
            emit("error", "Agent 执行失败，请检查本机 CLI、登录和模型配置")
            self.registry.finish_if_active(run_id, "failed", "agent_failed")
            self._release(context)
            return

        try:
            self._complete(context, result, chunks)
        finally:
            self._release(context)

    def _complete(self, context: RunContext, result: str | None,
                  chunks: list[str]) -> None:
        """成功终态协议：暂存输出 → 声明收尾 → 幂等落盘 → 唯一 done。

        失败只发布 failed；停止与保存竞争时由 ``begin_finalize`` 原子的
        ``finalizing`` 标记裁决，已开始收尾的 Run 不会再被取消伪造成功。
        """
        run_id = context.run_id
        text = result if isinstance(result, str) else "".join(chunks)
        try:
            self._stage_output(run_id, text)
        except DomainError:
            self.registry.finish_if_active(
                run_id, "failed", "output_storage_failed")
            return
        if not self.registry.begin_finalize(run_id):
            # 停止/中断先取得终态：不追加助手输出，也不覆盖终态
            self._discard_output(run_id)
            return
        try:
            self._sessions.append_message_once(
                context.conversation_id, run_id,
                {"role": "assistant", "content": text, "runId": run_id})
        except Exception:  # noqa: BLE001 - 输出保存失败必须收敛
            self.registry.append_event(run_id, "error", "输出保存失败")
            self.registry.finish_finalizing(
                run_id, "failed", "output_storage_failed")
            # 普通完成失败同样保留唯一完整输出；移动失败时留在原位置，
            # 由下次启动的重开清理再尝试，绝不静默删除。
            self._preserve_failed_output(
                run_id, self._finalizing_path(run_id))
            return
        self.registry.finish_finalizing(run_id, "done", "completed")
        self._discard_output(run_id)

    # -- 输出暂存与崩溃恢复 ------------------------------------------------

    def _finalizing_dir(self) -> Path:
        return self._paths.state_root / "finalizing"

    def _finalizing_path(self, run_id: str) -> Path:
        if (not isinstance(run_id, str) or not run_id or len(run_id) > 128
                or any(not (character.isalnum()
                            or character in "-_.:")
                        for character in run_id)):
            raise DomainError("output_storage_failed", 500)
        return self._finalizing_dir() / f"{run_id}.json"

    def _stage_output(self, run_id: str, text: str) -> None:
        """原子暂存最终输出；崩溃后仍能凭它补提交 done。"""
        import tempfile

        path = self._finalizing_path(run_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, name = tempfile.mkstemp(
                dir=path.parent, prefix=".stage-", suffix=".tmp")
            temporary = Path(name)
            try:
                best_effort_fchmod(descriptor, path.parent)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump({"runId": run_id,
                               "workspaceId": self.workspace_id,
                               "content": text}, stream,
                              ensure_ascii=False, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(path)
                fsync_directory(path.parent)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            raise DomainError("output_storage_failed", 500) from None

    def _discard_output(self, run_id: str) -> None:
        try:
            self._finalizing_path(run_id).unlink(missing_ok=True)
        except (OSError, DomainError):
            pass

    def _preserve_failed_output(self, run_id: str, path: Path) -> None:
        """提交失败时保留暂存输出；只有一致提交后才允许删除恢复材料。"""
        try:
            target_dir = self._paths.state_root / "failed_outputs"
            target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(path, target_dir / f"{run_id}.json")
        except OSError:
            # 保留原位置也可；绝不静默删除
            pass

    def _recover_finalizing(self) -> None:
        """重开时补提交已暂存输出；消息按 runId 去重，不重复追加。"""
        finalizing = set(self.registry.finalizing_runs())
        for run_id in sorted(finalizing):
            admission = self.registry.admission(run_id) or {}
            conversation_id = admission.get("conversationId")
            path = self._finalizing_path(run_id)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                content = data.get("content")
                if (data.get("runId") != run_id
                        or data.get("workspaceId") != self.workspace_id
                        or not isinstance(content, str)
                        or not isinstance(conversation_id, str)
                        or not conversation_id):
                    raise ValueError
            except (OSError, ValueError):
                self.registry.finish_finalizing(
                    run_id, "failed", "output_missing")
                # 损坏/不匹配的暂存材料也不静默删除；移入失败区保留证据
                self._preserve_failed_output(run_id, path)
                continue
            try:
                self._sessions.append_message_once(
                    conversation_id, run_id,
                    {"role": "assistant", "content": content,
                     "runId": run_id})
            except Exception:  # noqa: BLE001 - 恢复输出保存失败必须收敛
                self.registry.finish_finalizing(
                    run_id, "failed", "output_storage_failed")
                # 提交未成功：保留暂存输出供审查/人工恢复，不能当垃圾删除
                self._preserve_failed_output(run_id, path)
            else:
                self.registry.finish_finalizing(
                    run_id, "done", "completed")
                path.unlink(missing_ok=True)

    def _recover_orphan_outputs(self) -> None:
        """处理不处于 finalizing 的暂存文件。

        只有 Run 明确 done（成功提交后才会写 done）才可删除；failed /
        interrupted / 未知状态的文件一律保留或移入 failed_outputs。移动
        失败时留在原位置，绝不以“不在 finalizing 集合”为依据删除。
        """
        directory = self._finalizing_dir()
        if not directory.exists():
            return
        finalizing = set(self.registry.finalizing_runs())
        for path in directory.glob("*.json"):
            run_id = path.stem
            if run_id in finalizing:
                continue
            admission = self.registry.admission(run_id)
            state = admission.get("state") if admission else None
            if state == "done":
                # done 只在本轮输出一致提交后才写入，可作为清理证据
                path.unlink(missing_ok=True)
                continue
            self._preserve_failed_output(run_id, path)

    def _release(self, context: RunContext) -> None:
        if self._active_conversations.get(context.conversation_id) == \
                context.run_id:
            self._active_conversations.pop(context.conversation_id, None)

    # -- 控制入口 ----------------------------------------------------------

    async def stop(self, run_id: str) -> dict:
        state = self.registry.state(run_id)
        if state is None:
            raise DomainError("run_not_found", 404)
        if state in TERMINAL_STATES:
            return {"runId": run_id, "state": state}
        self.registry.finish_if_active(run_id, "cancelled", "stop_requested")
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            # 取消消息只是排队；必须等 supervisor 收尾，返回后同会话才真正
            # 释放。否则调用方紧接着提交下一 Run 会读到过期的 conversation_busy。
            await asyncio.gather(task, return_exceptions=True)
        # 输出收尾已取得 finalizing：返回真实终态，不能伪报 cancelled
        return {"runId": run_id,
                "state": self.registry.state(run_id) or "cancelled"}

    async def answer(self, run_id: str, question_id: str,
                     answers: object) -> dict:
        if not isinstance(answers, dict):
            raise _invalid("question_answer_invalid")
        for key, values in answers.items():
            if (not isinstance(key, str)
                    or not isinstance(values, list)
                    or not all(isinstance(item, str) for item in values)):
                raise _invalid("question_answer_invalid")
        key = (run_id, question_id)
        entry = self._questions.get(key)
        if entry is None:
            raise DomainError("question_unavailable", 409)
        normalized = {str(k): list(v) for k, v in answers.items()}
        # 先校验/翻译，再消耗 pending 问题：非法回答不能把问题置为 resolved
        translated = _translate_answers(entry["selections"], normalized)
        result = self.registry.answer(run_id, question_id, normalized)
        future = entry["future"]
        self._questions.pop(key, None)
        if future is not None and not future.done():
            future.set_result(translated)
        return {**result, "state": self.registry.state(run_id)}

    async def aclose(self) -> None:
        """先进入 draining，宽限期内等在途 Run，超时后有限取消并清理。

        SIGTERM 后不能立刻取消所有 supervisor：已接纳的短 Run 应在期限内
        完成并保留历史；挂起 Run 超过宽限期才取消。取消后的任务由
        supervisor 写 cancelled/interrupted，绝不能被伪标 done。
        """
        self.begin_drain()
        if self._closed:
            return
        self._closing = True
        pending = {task for task in self._tasks.values() if not task.done()}
        if pending and self._shutdown_grace > 0 and not self._force_stop:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._shutdown_grace
            while pending and not self._force_stop:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                _done, pending = await asyncio.wait(
                    pending, timeout=min(0.1, remaining))
        for task in pending:
            task.cancel()
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=_SHUTDOWN_CANCEL_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # 进程仍要退出；残留任务由 registry 对账为 interrupted。
                pass
        # 同时关闭本 workspace 的空闲 ACP/CLI 子进程，避免测试或重载时
        # 留下跨事件循环的僵死句柄。关闭动作本身也要有界，不能拖死
        # docker stop。
        from ..harnesses.rpc import close_sessions
        try:
            await asyncio.wait_for(
                close_sessions(workspace_id=self.workspace_id),
                timeout=_SHUTDOWN_CANCEL_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        self.registry.recover_inflight("worker_shutdown")
        self._tasks.clear()
        self._questions.clear()
        self._active_conversations.clear()
        self._closed = True


# ---------------------------------------------------------------------------
# 默认适配器执行器
# ---------------------------------------------------------------------------


async def _harness_executor(request: RunRequest, context: RunContext,
                            bind: Bind, emit: Emit,
                            ask: Ask) -> str | None:
    from easel import harnesses

    adapter = harnesses.adapter(context.runtime)
    await adapter.run(request, emit, ask, bind)
    return None
