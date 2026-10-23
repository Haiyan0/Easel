"""Task 9：控制会话目录与 worker 原生绑定/消息快照。"""
from __future__ import annotations

import hashlib
import json
import pathlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from easel.control.sessions import SessionService
from easel.control.store import Store
from easel.control.types import DomainError
from easel.worker.sessions import WorkspaceSessionStore

WORKSPACE_A = "workspace-a"
WORKSPACE_B = "workspace-b"
MESSAGE_FORMAT = "easel.session.message"
MESSAGE_VERSION = 1


def assert_error(error: pytest.ExceptionInfo, code: str, status: int) -> None:
    assert isinstance(error.value, DomainError)
    assert (error.value.code, error.value.status) == (code, status)


def test_append_peak_memory_does_not_scale_with_history(tmp_path):
    """小追加的内存峰值只与单条记录相关，不随历史总量线性增长。"""
    import tracemalloc

    def build(root: pathlib.Path, count: int):
        store = WorkspaceSessionStore(root, WORKSPACE_A)
        store.bind("s", "acp", "native", 1)
        content = "x" * (256 * 1024)
        for _ in range(count):
            store.append_message("s", {"role": "user", "content": content})
        return store

    def append_peak(store: WorkspaceSessionStore) -> int:
        tracemalloc.start()
        try:
            store.append_message(
                "s", {"role": "assistant", "content": "small"})
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    small = build(tmp_path / "small", 8)    # 约 2 MB 历史
    large = build(tmp_path / "large", 32)   # 约 8 MB 历史
    small_peak = append_peak(small)
    large_peak = append_peak(large)
    assert large_peak < 4 * 1024 * 1024, large_peak
    assert large_peak < small_peak * 2 + 1024 * 1024, (
        small_peak, large_peak)
    messages = small.messages("s", limit=500, offset=0)
    assert len(messages) == 9
    assert messages[-1]["content"] == "small"


def _legacy_binding(root: pathlib.Path, workspace_id: str,
                    session_id: str) -> pathlib.Path:
    base = (root / "sessions" / hashlib.sha256(
        workspace_id.encode()).hexdigest() / hashlib.sha256(
            session_id.encode()).hexdigest())
    base.mkdir(parents=True)
    path = base / "binding.json"
    path.write_text(json.dumps({"runtime": "acp",
                                "native_id": "native-legacy"}),
                    encoding="utf-8")
    return path


def _write_message_record(path: pathlib.Path, workspace_id: str,
                          session_id: str, message: dict, *,
                          version: int = MESSAGE_VERSION,
                          fmt: str = MESSAGE_FORMAT) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format": fmt,
        "version": version,
        "workspace_id": workspace_id,
        "session_id": session_id,
        "message": message,
    }
    data = (json.dumps(record, ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(data)
    return data


# --------------------------------------------------------------------------
# worker 私有存储
# --------------------------------------------------------------------------

def test_equal_native_ids_are_local_to_each_workspace(tmp_path):
    a = WorkspaceSessionStore(tmp_path / "a", WORKSPACE_A)
    b = WorkspaceSessionStore(tmp_path / "b", WORKSPACE_B)

    a.bind("same-easel-id", "acp", "same-native-id", 1)
    b.bind("same-easel-id", "acp", "same-native-id", 1)
    a.append_message("same-easel-id", {"role": "user", "content": "A-private"})

    assert a.binding("same-easel-id")["workspace_id"] == WORKSPACE_A
    assert b.binding("same-easel-id")["workspace_id"] == WORKSPACE_B
    assert b.messages("same-easel-id") == []
    assert a.messages("same-easel-id")[0]["content"] == "A-private"


def test_workspace_and_session_identifiers_cannot_traverse_paths(tmp_path):
    with pytest.raises(DomainError) as bad_workspace:
        WorkspaceSessionStore(tmp_path, "../other")
    assert_error(bad_workspace, "session_invalid", 400)

    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    for session_id in ("../escape", "a/b", "..", "", "a\\b"):
        with pytest.raises(DomainError) as bad_session:
            store.bind(session_id, "acp", "native", 1)
        assert_error(bad_session, "session_invalid", 400)


def test_native_binding_changes_are_rejected_without_overwrite(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    store.bind("s1", "acp", "native-one", 1)

    with pytest.raises(DomainError) as native:
        store.bind("s1", "acp", "native-two", 1)
    assert_error(native, "session_binding_conflict", 409)
    with pytest.raises(DomainError) as version:
        store.bind("s1", "acp", "native-one", 2)
    assert_error(version, "session_version_unsupported", 409)
    with pytest.raises(DomainError) as runtime:
        store.bind("s1", "codex", "native-one", 1)
    assert_error(runtime, "session_binding_conflict", 409)

    assert store.binding("s1")["native_id"] == "native-one"
    assert store.binding("s1")["runtime"] == "acp"
    assert store.binding("s1")["agent_version"] == 1


def test_empty_native_id_can_be_filled_once_but_not_erased(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    store.bind("s1", "acp", "", 1)
    store.bind("s1", "acp", "native-one", 1)
    assert store.binding("s1")["native_id"] == "native-one"

    with pytest.raises(DomainError) as erased:
        store.bind("s1", "acp", "", 1)
    assert_error(erased, "session_binding_conflict", 409)
    assert store.binding("s1")["native_id"] == "native-one"


def test_legacy_unowned_binding_is_rejected_not_claimed(tmp_path):
    _legacy_binding(tmp_path, WORKSPACE_A, "s1")
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)

    with pytest.raises(DomainError) as rejected:
        store.binding("s1")
    assert_error(rejected, "session_legacy_unowned", 409)
    with pytest.raises(DomainError) as bind_rejected:
        store.bind("s1", "acp", "native-new", 1)
    assert_error(bind_rejected, "session_legacy_unowned", 409)


def test_messages_are_durable_and_ordered_after_reopen(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    store.bind("s1", "acp", "native", 1)
    store.append_message("s1", {"role": "user", "content": "one"})
    store.append_message("s1", {"role": "assistant", "content": "two"})

    reopened = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    assert [message["content"] for message in reopened.messages("s1")] == [
        "one", "two"]


def test_same_root_workspaces_do_not_share_session_index(tmp_path):
    a = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    b = WorkspaceSessionStore(tmp_path, WORKSPACE_B)
    a.bind("same", "acp", "native-a", 1)
    b.bind("same", "acp", "native-b", 1)
    a.append_message("same", {"role": "user", "content": "A"})
    b.append_message("same", {"role": "user", "content": "B"})

    assert a.binding("same")["workspace_id"] == WORKSPACE_A
    assert b.binding("same")["workspace_id"] == WORKSPACE_B
    assert [item["content"] for item in a.messages("same")] == ["A"]
    assert [item["content"] for item in b.messages("same")] == ["B"]


def test_message_snapshot_records_owner_and_format(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    store.bind("s1", "acp", "native-a", 1)
    store.append_message("s1", {"role": "user", "content": "A-private"})

    record = json.loads(store.messages_path("s1").read_text(encoding="utf-8"))
    assert record["format"] == MESSAGE_FORMAT
    assert record["version"] == MESSAGE_VERSION
    assert record["workspace_id"] == WORKSPACE_A
    assert record["session_id"] == "s1"
    assert record["message"] == {"role": "user", "content": "A-private"}


def test_copied_message_snapshot_from_another_workspace_is_rejected(tmp_path):
    a = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    b = WorkspaceSessionStore(tmp_path, WORKSPACE_B)
    a.bind("same", "acp", "native-a", 1)
    b.bind("same", "acp", "native-b", 1)
    a.append_message("same", {"role": "user", "content": "A-private"})
    b.messages_path("same").write_bytes(
        a.messages_path("same").read_bytes())

    with pytest.raises(DomainError) as copied:
        b.messages("same")
    assert_error(copied, "session_workspace_mismatch", 409)
    with pytest.raises(DomainError) as append_copied:
        b.append_message("same", {"role": "user", "content": "B"})
    assert_error(append_copied, "session_workspace_mismatch", 409)


def test_copied_message_snapshot_from_another_session_is_rejected(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    store.bind("s1", "acp", "native-a", 1)
    store.bind("s2", "acp", "native-b", 1)
    store.append_message("s1", {"role": "user", "content": "s1-private"})
    store.messages_path("s2").write_bytes(
        store.messages_path("s1").read_bytes())

    with pytest.raises(DomainError) as copied:
        store.messages("s2")
    assert_error(copied, "session_identity_mismatch", 409)


def test_missing_or_unknown_message_version_is_rejected(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    store.bind("legacy", "acp", "native-legacy", 1)
    legacy_path = store.messages_path("legacy")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps({"role": "user", "content": "old"}) + "\n",
        encoding="utf-8")
    before = legacy_path.read_bytes()

    with pytest.raises(DomainError) as legacy:
        store.messages("legacy")
    assert_error(legacy, "session_legacy_unowned", 409)
    with pytest.raises(DomainError) as append_legacy:
        store.append_message("legacy", {"role": "user", "content": "new"})
    assert_error(append_legacy, "session_legacy_unowned", 409)
    assert legacy_path.read_bytes() == before

    store.bind("future", "acp", "native-future", 1)
    future_path = store.messages_path("future")
    _write_message_record(future_path, WORKSPACE_A, "future",
                          {"role": "user", "content": "future"},
                          version=MESSAGE_VERSION + 1)
    with pytest.raises(DomainError) as future:
        store.messages("future")
    assert_error(future, "session_version_unsupported", 409)


def test_invalid_persisted_message_and_attachment_are_rejected(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    for session_id, message in (
            ("bad-role", {"role": "root", "content": "x"}),
            ("bad-attachment", {
                "role": "user", "content": "x",
                "attachments": [{"path": "../secret"}]})):
        store.bind(session_id, "acp", f"native-{session_id}", 1)
        _write_message_record(
            store.messages_path(session_id), WORKSPACE_A, session_id,
            message)
        with pytest.raises(DomainError) as invalid:
            store.messages(session_id)
        assert_error(invalid, "session_corrupt", 500)


def test_concurrent_bind_allows_one_identity_only(tmp_path):
    first = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    second = WorkspaceSessionStore(tmp_path, WORKSPACE_A)

    def bind(store, native):
        try:
            store.bind("same", "acp", native, 1)
            return "ok"
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(
            lambda pair: bind(*pair),
            [(first, "native-a"), (second, "native-b")]))
    assert sorted(outcomes) == [
        "ok", "session_binding_conflict"]
    assert first.binding("same")["native_id"] in (
        "native-a", "native-b")


def test_concurrent_append_from_two_store_objects_loses_no_messages(tmp_path):
    first = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    second = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    first.bind("s1", "acp", "native", 1)

    def append(store, start):
        for index in range(start, start + 20):
            store.append_message(
                "s1", {"role": "user", "content": f"message-{index}"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda pair: append(*pair),
                      [(first, 0), (second, 20)]))

    contents = [message["content"]
                for message in WorkspaceSessionStore(
                    tmp_path, WORKSPACE_A).messages("s1", limit=100)]
    assert len(contents) == 40
    assert set(contents) == {f"message-{index}" for index in range(40)}


def test_messages_without_binding_are_not_claimed_as_empty_history(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    path = store.messages_path("orphan")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"role": "user", "content": "orphan"}) + "\n",
        encoding="utf-8")

    with pytest.raises(DomainError) as unbound:
        store.binding("orphan")
    assert_error(unbound, "session_unbound", 409)
    with pytest.raises(DomainError) as bind_rejected:
        store.bind("orphan", "acp", "native", 1)
    assert_error(bind_rejected, "session_unbound", 409)
    assert not store.binding_path("orphan").exists()


def test_failed_append_does_not_leave_partial_line(tmp_path, monkeypatch):
    import easel.worker.sessions as worker_sessions

    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    store.bind("s1", "acp", "native", 1)
    store.append_message("s1", {"role": "user", "content": "before"})

    def fail_write(_descriptor, _data):
        raise OSError("disk full")

    monkeypatch.setattr(worker_sessions.os, "write", fail_write)
    with pytest.raises(DomainError) as failed:
        store.append_message("s1", {"role": "user", "content": "after"})
    assert_error(failed, "session_write_failed", 500)
    assert [message["content"] for message in store.messages("s1")] == [
        "before"]


def test_corrupt_or_truncated_snapshot_is_not_treated_as_empty(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    store.bind("s1", "acp", "native", 1)
    store.append_message("s1", {"role": "user", "content": "complete"})
    path = store.messages_path("s1") if hasattr(store, "messages_path") else (
        tmp_path / "sessions" / hashlib.sha256(
            WORKSPACE_A.encode()).hexdigest() / hashlib.sha256(
                b"s1").hexdigest() / "messages.jsonl")
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: max(1, len(text) // 2)], encoding="utf-8")

    with pytest.raises(DomainError) as corrupt:
        store.messages("s1")
    assert_error(corrupt, "session_corrupt", 500)


def test_append_requires_binding_and_rejects_attachment_traversal(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    with pytest.raises(DomainError) as unbound:
        store.append_message("s1", {"role": "user", "content": "x"})
    assert_error(unbound, "session_unbound", 409)

    store.bind("s1", "acp", "native", 1)
    for path in ("../secret", "/etc/passwd", "a/../../b", "a\\b",
                 "a\nb", "C:/secret", "~/.ssh/id_ed25519"):
        with pytest.raises(DomainError) as bad_attachment:
            store.append_message("s1", {
                "role": "user", "content": "x",
                "attachments": [{"path": path}]})
        assert_error(bad_attachment, "session_invalid", 400)
    assert store.messages("s1") == []


def test_message_pagination_has_stable_order(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    store.bind("s1", "acp", "native", 1)
    for index in range(5):
        store.append_message(
            "s1", {"role": "user", "content": f"m-{index}"})

    assert [message["content"]
            for message in store.messages("s1", limit=2)] == ["m-0", "m-1"]
    assert [message["content"]
            for message in store.messages("s1", limit=2, offset=2)] == [
        "m-2", "m-3"]
    assert [message["content"]
            for message in store.messages("s1", limit=2, offset=4)] == ["m-4"]


def test_native_cwd_change_is_rejected(tmp_path):
    store = WorkspaceSessionStore(tmp_path, WORKSPACE_A)
    store.bind("s1", "acp", "native", 1, cwd="/workspace")
    with pytest.raises(DomainError) as incompatible:
        store.bind("s1", "acp", "native", 1, cwd="/other")
    assert_error(incompatible, "session_cwd_incompatible", 409)
    assert store.binding("s1")["cwd"] == "/workspace"


# --------------------------------------------------------------------------
# control 会话目录
# --------------------------------------------------------------------------

def test_session_service_is_scoped_to_actor_workspace(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a, b = store.create_user("A"), store.create_user("B")
        service = SessionService(store)
        session = service.create_conversation(a, "A 的会话")
        run = store.reserve_run(a, session.id, "request-a", "digest-a", 1)

        assert service.list_conversations(a) == [session]
        assert service.list_conversations(b) == []
        with pytest.raises(DomainError) as foreign:
            service.assert_run(b, run.id)
        assert_error(foreign, "not_found", 404)
        assert service.assert_run(a, run.id).id == run.id


def test_disabled_actor_cannot_assert_run_or_read_directory(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        service = SessionService(store)
        session = service.create_conversation(a)
        run = store.reserve_run(a, session.id, "request-disabled", "digest", 1)
        store.disable_user(a.user_id)

        with pytest.raises(DomainError) as directory:
            service.list_conversations(a)
        assert_error(directory, "user_disabled", 403)
        with pytest.raises(DomainError) as run_rejected:
            service.assert_run(a, run.id)
        assert_error(run_rejected, "user_disabled", 403)


def test_list_update_archive_delete_and_active_run_conflict(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        service = SessionService(store)
        first = service.create_conversation(a, "一")
        second = service.create_conversation(a, "二")
        third = service.create_conversation(a, "三")

        listed = service.list_conversations(a)
        assert {item.id for item in listed} == {first.id, second.id, third.id}

        renamed = service.update_conversation(a, first.id, title="新标题")
        assert renamed.title == "新标题"
        archived = service.update_conversation(a, second.id, archived=True)
        assert archived.archived is True
        assert {item.id for item in service.list_conversations(a)} == {
            first.id, third.id}
        assert {item.id for item in service.list_conversations(
            a, include_archived=True)} == {first.id, second.id, third.id}

        active = store.reserve_run(a, third.id, "request-active",
                                   "digest", 1)
        with pytest.raises(DomainError) as busy:
            service.delete_conversation(a, third.id)
        assert_error(busy, "conversation_busy", 409)
        with pytest.raises(DomainError) as archive_busy:
            service.update_conversation(a, third.id, archived=True)
        assert_error(archive_busy, "conversation_busy", 409)
        assert store.get_run(a, active.id).id == active.id

        service.delete_conversation(a, first.id)
        with pytest.raises(DomainError) as missing:
            store.get_conversation(a, first.id)
        assert_error(missing, "not_found", 404)
        assert {item.id for item in service.list_conversations(a)} == {
            third.id}


def test_deleting_conversation_keeps_completed_run_reference(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        service = SessionService(store)
        session = service.create_conversation(a)
        run = store.reserve_run(a, session.id, "request-1", "digest", 1)
        store.finish_run(a, run.id, "done")

        service.delete_conversation(a, session.id)
        assert store.get_run(a, run.id).id == run.id
        with pytest.raises(DomainError) as missing:
            service.get_conversation(a, session.id)
        assert_error(missing, "not_found", 404)


def test_list_pagination_is_bounded_and_stable(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        service = SessionService(store)
        created = [service.create_conversation(a, f"标题{index}")
                   for index in range(5)]

        page1 = service.list_conversations(a, limit=2, offset=0)
        page2 = service.list_conversations(a, limit=2, offset=2)
        assert len(page1) == len(page2) == 2
        assert {item.id for item in page1} | {item.id for item in page2} <= {
            item.id for item in created}
        with pytest.raises(DomainError) as invalid:
            service.list_conversations(a, limit=0)
        assert_error(invalid, "invalid_argument", 400)


def test_archived_conversation_cannot_accept_new_run(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        service = SessionService(store)
        session = service.create_conversation(a)
        service.update_conversation(a, session.id, archived=True)
        with pytest.raises(DomainError) as archived:
            store.reserve_run(a, session.id, "request-1", "digest", 1)
        assert_error(archived, "conversation_archived", 409)


def test_large_history_count_and_latest_window(tmp_path):
    store = WorkspaceSessionStore(tmp_path / "state", WORKSPACE_A)
    store.bind("big", "acp", "native-big", 1)
    path = store.messages_path("big")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for index in range(2500):
            stream.write(json.dumps({
                "format": "easel.session.message",
                "version": 1,
                "workspace_id": WORKSPACE_A,
                "session_id": "big",
                "message": {"role": "user", "content": f"m{index}"},
            }, ensure_ascii=False) + "\n")
    assert store.count_messages("big") == 2500
    latest = store.messages("big", limit=500, offset=2000)
    assert len(latest) == 500
    assert latest[0]["content"] == "m2000"
    assert latest[-1]["content"] == "m2499"
    older = store.messages("big", limit=500, offset=1500)
    assert older[0]["content"] == "m1500"
