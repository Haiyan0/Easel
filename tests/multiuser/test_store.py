"""Task 1 资源模型：真实 SQLite 上的归属、幂等与并发接纳回归。

本文件不 mock SQL，全部使用临时目录里的真实 SQLite 文件，验证：

* Identity 只是传输对象，授权一律重查数据库中的用户/workspace 与有效状态；
* 外空间和不存在的对象统一返回 404，不能读取、结束或释放他人的任务；
* 请求幂等先于容量检查，重试不会多占并发预算；
* 并发约束在 ``BEGIN IMMEDIATE`` 事务内成立，两个独立连接竞争时依然有效；
* 未知的新版本数据库被拒绝打开，不重建、不覆盖。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from easel.control.store import CURRENT_SCHEMA_VERSION, Store
from easel.control.types import (
    BrowserSession,
    ConversationRecord,
    DomainError,
    Identity,
    RunRecord,
    WorkerHandle,
)


def reserve(store: Store, actor: Identity, conversation_id: str,
            request_id: str = "request-1", digest: str = "digest-1",
            config_version: int = 1) -> RunRecord:
    """按计划固定签名申请一个 Run。"""
    return store.reserve_run(actor, conversation_id, request_id, digest, config_version)


def assert_error(error: pytest.ExceptionInfo, status: int, code: str = "") -> None:
    assert isinstance(error.value, DomainError)
    assert error.value.status == status, error.value
    if code:
        assert error.value.code == code, error.value


def run_concurrently(path, worker, count: int = 2) -> list[Any]:
    """用同步屏障制造真实竞争，返回各线程的结果或 DomainError。

    每个线程持有自己的 ``Store``（独立 SQLite 连接），不共享连接也不依赖 sleep
    碰运气：屏障保证所有线程都进入事务前才同时放行。
    """
    barrier = threading.Barrier(count, timeout=10)
    results: list[Any] = []
    lock = threading.Lock()

    def target(index: int) -> None:
        with Store.open(path) as store:
            barrier.wait()
            try:
                outcome: Any = worker(store, index)
            except DomainError as error:
                outcome = error
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=target, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "并发线程未在超时内结束"
    return results


@contextmanager
def capacity_limits(*, per_workspace: int) -> Iterator[None]:
    """临时收紧并发预算以直接检验边界，退出时恢复默认值。"""
    original = Store.max_active_runs_per_workspace
    Store.max_active_runs_per_workspace = per_workspace
    try:
        yield
    finally:
        Store.max_active_runs_per_workspace = original


# --------------------------------------------------------------------------
# 用户与 workspace 的一对一关系
# --------------------------------------------------------------------------

def test_create_user_provisions_one_private_workspace(tmp_path):
    with Store.open(tmp_path / "db") as store:
        member = store.create_user("成员")
        admin = store.create_user("管理员", "admin")

    assert member.role == "member"
    assert admin.role == "admin"
    assert member.user_id and admin.user_id
    assert member.workspace_id and admin.workspace_id
    assert member.workspace_id != admin.workspace_id
    assert len({member.user_id, admin.user_id, member.workspace_id, admin.workspace_id}) == 4


def test_role_is_limited_to_registered_values(tmp_path):
    with Store.open(tmp_path / "db") as store:
        with pytest.raises(DomainError) as invalid:
            store.create_user("冒充者", "superuser")
        assert_error(invalid, 400, "invalid_role")
        with pytest.raises(DomainError) as empty:
            store.create_user("空角色", "")
        assert_error(empty, 400, "invalid_role")

        # 角色校验失败不得留下半个用户或 workspace
        survivor = store.create_user("正常用户")
        conversation = store.create_conversation(survivor)
        assert store.get_conversation(survivor, conversation.id).workspace_id == survivor.workspace_id


def test_user_cannot_own_two_workspaces(tmp_path):
    path = tmp_path / "db"
    with Store.open(path) as store:
        user = store.create_user("A")

    # 一对一是数据库约束，不只靠应用层判断；裸连接需自行开启外键
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO workspaces (id, owner_user_id, state, config_version, created_at)"
                " VALUES (?, ?, 'ready', 1, 1.0)", ("ws-dup", user.user_id))
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO conversations (workspace_id, id, title, archived, created_at, updated_at)"
                " VALUES (?, 'c-orphan', 'x', 0, 1.0, 1.0)", ("ws-missing",))
        connection.rollback()
    finally:
        connection.close()


def test_conversation_is_pinned_to_the_actor_workspace(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a, b = store.create_user("A"), store.create_user("B")
        conversation = store.create_conversation(a, "A 的会话")

        assert isinstance(conversation, ConversationRecord)
        assert conversation.workspace_id == a.workspace_id
        assert conversation.title == "A 的会话"
        assert store.create_conversation(b).title == "新对话"


# --------------------------------------------------------------------------
# 授权重查数据库，Identity 本身不是凭证
# --------------------------------------------------------------------------

def test_foreign_conversation_is_missing_and_retry_is_idempotent(tmp_path):
    """计划 Task 1 Step 1 的最小回归，保持原样的接口契约。"""
    with Store.open(tmp_path / "control.sqlite3") as store:
        a, b = store.create_user("A"), store.create_user("B")
        conversation = store.create_conversation(a)
        with pytest.raises(DomainError) as denied:
            store.get_conversation(b, conversation.id)
        assert denied.value.status == 404
        args = (a, conversation.id, "request-1", "digest-1", 1)
        first = store.reserve_run(*args)
        assert store.reserve_run(*args).id == first.id
        with pytest.raises(DomainError) as conflict:
            store.reserve_run(a, conversation.id, "request-1", "digest-2", 1)
        assert conflict.value.status == 409


def test_forged_identity_cannot_reach_foreign_resources(tmp_path):
    """凑出别人的 user_id + workspace_id 组合也不成立：归属必须由数据库确认。"""
    with Store.open(tmp_path / "db") as store:
        a, b = store.create_user("A"), store.create_user("B")
        conversation = store.create_conversation(a)
        run = reserve(store, a, conversation.id)

        forged = Identity(b.user_id, a.workspace_id, "member")
        with pytest.raises(DomainError) as stolen:
            store.get_conversation(forged, conversation.id)
        assert_error(stolen, 403, "unauthorized")
        with pytest.raises(DomainError) as stolen_reserve:
            reserve(store, forged, conversation.id, "request-forged")
        assert_error(stolen_reserve, 403, "unauthorized")
        with pytest.raises(DomainError) as stolen_finish:
            store.finish_run(forged, run.id, "done")
        assert_error(stolen_finish, 403, "unauthorized")
        # 伪造身份没有产生任何副作用
        assert store.get_run(a, run.id).status == "reserved"


def test_identity_pointing_at_unprovisioned_workspace_is_unauthorized(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        with pytest.raises(DomainError) as orphan:
            store.create_conversation(Identity(a.user_id, "ws-does-not-exist", "member"))
        assert_error(orphan, 403, "unauthorized")
        with pytest.raises(DomainError) as no_user:
            store.create_conversation(Identity("user-does-not-exist", a.workspace_id, "member"))
        assert_error(no_user, 403, "unauthorized")


def test_foreign_run_cannot_be_read_or_finished(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a, b = store.create_user("A"), store.create_user("B")
        conversation = store.create_conversation(a)
        run = reserve(store, a, conversation.id)

        with pytest.raises(DomainError) as foreign_get:
            store.get_run(b, run.id)
        assert_error(foreign_get, 404, "not_found")
        with pytest.raises(DomainError) as foreign_finish:
            store.finish_run(b, run.id, "done")
        assert_error(foreign_finish, 404, "not_found")
        with pytest.raises(DomainError) as missing:
            store.finish_run(a, "run-does-not-exist", "done")
        assert_error(missing, 404, "not_found")
        # 他人尝试结束后，原 Run 仍在进行，归属者仍可正常收尾
        assert store.get_run(a, run.id).status == "reserved"
        assert store.finish_run(a, run.id, "done") is None


# --------------------------------------------------------------------------
# 持久化：关闭重开后归属与幂等记录仍在
# --------------------------------------------------------------------------

def test_ownership_and_idempotency_survive_reopen(tmp_path):
    path = tmp_path / "control.sqlite3"
    with Store.open(path) as store:
        a, b = store.create_user("A"), store.create_user("B")
        conversation = store.create_conversation(a, "持久会话")
        run = reserve(store, a, conversation.id, "request-persist", "digest-persist", 7)

    with Store.open(path) as reopened:
        assert reopened.get_conversation(a, conversation.id) == conversation
        assert reopened.get_run(a, run.id) == run
        assert reserve(reopened, a, conversation.id,
                       "request-persist", "digest-persist", 7).id == run.id
        with pytest.raises(DomainError) as foreign:
            reopened.get_conversation(b, conversation.id)
        assert_error(foreign, 404, "not_found")
        with pytest.raises(DomainError) as conflict:
            reserve(reopened, a, conversation.id, "request-persist", "digest-other", 7)
        assert_error(conflict, 409, "request_conflict")


# --------------------------------------------------------------------------
# 幂等先于容量检查
# --------------------------------------------------------------------------

def test_retry_beyond_capacity_returns_the_original_run(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        conversation = store.create_conversation(a)
        first = reserve(store, a, conversation.id, "request-1", "digest-1", 3)
        reserve(store, a, conversation.id, "request-2", "digest-2", 3)

        # 容量已满，但幂等重试必须先被识别
        assert reserve(store, a, conversation.id, "request-1", "digest-1", 3).id == first.id
        with pytest.raises(DomainError) as conflict:
            reserve(store, a, conversation.id, "request-1", "digest-9", 3)
        assert_error(conflict, 409, "request_conflict")
        with pytest.raises(DomainError) as full:
            reserve(store, a, conversation.id, "request-3", "digest-3", 3)
        assert_error(full, 429, "workspace_capacity")


def test_retry_of_an_admitted_run_never_consumes_more_capacity(tmp_path):
    """空间与全局预算都已满时，重试既有请求仍必须返回原 Run。"""
    with Store.open(tmp_path / "db") as store:
        owners = [store.create_user(f"用户{index}") for index in range(4)]
        admitted = {}
        for index, owner in enumerate(owners):
            conversation = store.create_conversation(owner)
            admitted[owner.user_id] = (
                conversation.id,
                reserve(store, owner, conversation.id, f"request-{index}-1", "digest-1", 1),
                reserve(store, owner, conversation.id, f"request-{index}-2", "digest-2", 1),
            )
        assert store.count_active_runs() == 8

        owner = owners[0]
        conversation_id, first, _ = admitted[owner.user_id]
        assert reserve(store, owner, conversation_id,
                       "request-0-1", "digest-1", 1).id == first.id
        # 重试没有新增记录，容量仍是 8
        assert store.count_active_runs() == 8
        assert len(store.list_runs(owner, conversation_id)) == 2

        # 同一请求换输入在容量满时先报冲突，而不是 429
        with pytest.raises(DomainError) as conflict:
            reserve(store, owner, conversation_id, "request-0-1", "digest-other", 1)
        assert_error(conflict, 409, "request_conflict")


def test_conflicting_retry_through_two_connections_creates_no_extra_run(tmp_path):
    """两个连接同时用同一请求 ID 但不同输入：一个复用原 Run，一个报冲突，都不新增记录。"""
    path = tmp_path / "db"
    with Store.open(path) as setup:
        a = setup.create_user("A")
        conversation = setup.create_conversation(a)
        original = reserve(setup, a, conversation.id, "request-race", "digest-one")

    def attempt(store, index):
        digest = "digest-one" if index == 0 else "digest-two"
        return reserve(store, a, conversation.id, "request-race", digest).id

    results = run_concurrently(path, attempt)

    assert [item for item in results if isinstance(item, str)] == [original.id]
    conflicts = [item for item in results if isinstance(item, DomainError)]
    assert [(error.status, error.code) for error in conflicts] == [(409, "request_conflict")]

    with Store.open(path) as reopened:
        assert len(reopened.list_runs(a, conversation.id)) == 1


def test_retry_keeps_the_original_config_version(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        conversation = store.create_conversation(a)
        first = reserve(store, a, conversation.id, "request-1", "digest-1", 1)

        assert first.config_version == 1
        # 重试时管理员已发布新版本，但仍复用原 Run 与其配置版本
        retried = reserve(store, a, conversation.id, "request-1", "digest-1", 2)
        assert retried.id == first.id
        assert retried.config_version == 1


def test_same_request_id_in_another_conversation_is_a_conflict(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        first = store.create_conversation(a, "会话一")
        second = store.create_conversation(a, "会话二")
        reserve(store, a, first.id, "request-1", "digest-1")

        with pytest.raises(DomainError) as conflict:
            reserve(store, a, second.id, "request-1", "digest-1")
        assert_error(conflict, 409, "request_conflict")


def test_request_id_is_scoped_per_workspace(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a, b = store.create_user("A"), store.create_user("B")
        a_conversation = store.create_conversation(a)
        b_conversation = store.create_conversation(b)

        a_run = reserve(store, a, a_conversation.id, "same-request", "digest-a")
        b_run = reserve(store, b, b_conversation.id, "same-request", "digest-b")
        assert a_run.id != b_run.id
        assert {a_run.workspace_id, b_run.workspace_id} == {a.workspace_id, b.workspace_id}


def test_reserve_rejects_foreign_or_unknown_conversation(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a, b = store.create_user("A"), store.create_user("B")
        conversation = store.create_conversation(a)

        with pytest.raises(DomainError) as foreign:
            reserve(store, b, conversation.id)
        assert_error(foreign, 404, "not_found")
        with pytest.raises(DomainError) as missing:
            reserve(store, a, "conversation-does-not-exist")
        assert_error(missing, 404, "not_found")


# --------------------------------------------------------------------------
# 容量限制：每空间 2、全局 8，事务内原子判定
# --------------------------------------------------------------------------

def test_concurrent_reservations_through_two_connections(tmp_path):
    """真实竞争：同一会话的两个新请求不能同时占用 prompt 额度。

    临时把每空间预算收紧为 1，两个独立连接同时申请时只能有一个成功，
    另一个必须收到 429 而不是靠运气错开。
    """
    path = tmp_path / "db"
    with Store.open(path) as setup:
        a = setup.create_user("A")
        conversation = setup.create_conversation(a)

    with capacity_limits(per_workspace=1):
        results = run_concurrently(
            path,
            lambda store, index: reserve(store, a, conversation.id, f"request-{index}").id)

    accepted = [item for item in results if isinstance(item, str)]
    rejected = [item for item in results if isinstance(item, DomainError)]
    assert len(accepted) == 1, results
    assert len(rejected) == 1, results
    assert (rejected[0].status, rejected[0].code) == (429, "workspace_capacity"), rejected[0]

    with Store.open(path) as reopened:
        assert [run.id for run in reopened.list_runs(a, conversation.id)] == accepted


def test_concurrent_reservations_fill_each_workspace_budget_atomically(tmp_path):
    """默认每空间 2 的预算在并发下同样原子：三个请求只能接纳两个。"""
    path = tmp_path / "db"
    with Store.open(path) as setup:
        a = setup.create_user("A")
        conversation = setup.create_conversation(a)

    results = run_concurrently(
        path,
        lambda store, index: reserve(store, a, conversation.id, f"request-{index}").id,
        count=3)

    accepted = [item for item in results if isinstance(item, str)]
    rejected = [item for item in results if isinstance(item, DomainError)]
    assert len(accepted) == 2, results
    assert [(error.status, error.code) for error in rejected] == [(429, "workspace_capacity")]
    assert len(set(accepted)) == 2, results

    with Store.open(path) as reopened:
        assert len(reopened.list_runs(a, conversation.id)) == 2


def test_same_request_over_two_connections_creates_one_run(tmp_path):
    """两个连接同时用同一请求 ID 申请，只能生成一个 Run。"""
    path = tmp_path / "db"
    with Store.open(path) as setup:
        a = setup.create_user("A")
        conversation = setup.create_conversation(a)

    results = run_concurrently(
        path,
        lambda store, index: store.reserve_run(
            a, conversation.id, "request-race", "digest-race", 1).id)

    assert len({item for item in results if isinstance(item, str)}) == 1, results
    with Store.open(path) as reopened:
        assert len(reopened.list_runs(a, conversation.id)) == 1


def test_workspace_capacity_limits_to_two_active_runs(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        conversation = store.create_conversation(a)

        reserve(store, a, conversation.id, "request-1")
        reserve(store, a, conversation.id, "request-2")
        with pytest.raises(DomainError) as full:
            reserve(store, a, conversation.id, "request-3")
        assert_error(full, 429, "workspace_capacity")

        # 被拒绝的申请整体回滚，不留下半条记录
        assert store.count_active_runs() == 2
        assert len(store.list_runs(a, conversation.id)) == 2


def test_global_capacity_limits_to_eight_active_runs(tmp_path):
    with Store.open(tmp_path / "db") as store:
        owners = [store.create_user(f"用户{index}") for index in range(5)]
        for index, owner in enumerate(owners[:4]):
            conversation = store.create_conversation(owner)
            reserve(store, owner, conversation.id, f"request-{index}-1")
            reserve(store, owner, conversation.id, f"request-{index}-2")

        assert store.count_active_runs() == 8
        last = store.create_conversation(owners[4])
        with pytest.raises(DomainError) as global_full:
            reserve(store, owners[4], last.id, "request-4-1")
        assert_error(global_full, 429, "global_capacity")


# --------------------------------------------------------------------------
# 终态与容量释放
# --------------------------------------------------------------------------

def test_finish_releases_capacity_once_and_is_safely_repeatable(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        conversation = store.create_conversation(a)
        run = reserve(store, a, conversation.id, "request-1")
        reserve(store, a, conversation.id, "request-2")

        assert store.finish_run(a, run.id, "done") is None
        assert store.get_run(a, run.id).status == "done"
        assert store.count_active_runs() == 1

        # 重复完成安全：状态不变、容量不重复释放
        assert store.finish_run(a, run.id, "done") is None
        assert store.get_run(a, run.id).status == "done"
        assert store.count_active_runs() == 1

        reserve(store, a, conversation.id, "request-3")
        with pytest.raises(DomainError) as full:
            reserve(store, a, conversation.id, "request-4")
        assert_error(full, 429, "workspace_capacity")


def test_terminal_run_cannot_be_reactivated(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        conversation = store.create_conversation(a)
        run = reserve(store, a, conversation.id, "request-1")
        store.finish_run(a, run.id, "failed")

        with pytest.raises(DomainError) as resurrect:
            store.mark_running(a, run.id)
        assert_error(resurrect, 409, "terminal_run")


def test_only_terminal_statuses_can_finish_a_run(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        conversation = store.create_conversation(a)
        run = reserve(store, a, conversation.id, "request-1")

        for status in ("running", "reserved", "whatever", ""):
            with pytest.raises(DomainError) as invalid:
                store.finish_run(a, run.id, status)
            assert_error(invalid, 400, "invalid_status")
        assert store.get_run(a, run.id).status == "reserved"

        for status in ("done", "failed", "interrupted", "cancelled"):
            other = reserve(store, a, conversation.id, f"request-{status}")
            store.finish_run(a, other.id, status)
            assert store.get_run(a, other.id).status == status


def test_interrupted_run_is_terminal_and_reported(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        conversation = store.create_conversation(a)
        run = reserve(store, a, conversation.id, "request-1")
        store.mark_running(a, run.id)

        assert store.mark_interrupted(a, run.id) is True
        assert store.get_run(a, run.id).status == "interrupted"
        assert store.mark_interrupted(a, run.id) is False
        assert store.count_active_runs() == 0


# --------------------------------------------------------------------------
# 禁用用户
# --------------------------------------------------------------------------

def test_disabled_user_cannot_create_or_accept_new_work(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a = store.create_user("A")
        conversation = store.create_conversation(a)
        run = reserve(store, a, conversation.id, "request-1")

        store.disable_user(a.user_id)
        with pytest.raises(DomainError) as disabled:
            store.create_conversation(a)
        assert_error(disabled, 403, "user_disabled")
        with pytest.raises(DomainError) as blocked:
            reserve(store, a, conversation.id, "request-2")
        assert_error(blocked, 403, "user_disabled")
        with pytest.raises(DomainError) as blocked_read:
            store.get_conversation(a, conversation.id)
        assert_error(blocked_read, 403, "user_disabled")

        # 已接纳任务的内部收尾仍需可执行，否则容量被永久占用
        assert store.finish_run(a, run.id, "done") is None
        assert store.get_run(a, run.id).status == "done"
        assert store.count_active_runs() == 0
        # 重复收尾同样安全
        assert store.finish_run(a, run.id, "done") is None


def test_disabled_user_cannot_retry_or_get_reads_reopened(tmp_path):
    """另一个连接写入的禁用状态对本连接立即可见：校验不是缓存的旧结论。"""
    path = tmp_path / "db"
    with Store.open(path) as writer:
        a = writer.create_user("A")
        writer.disable_user(a.user_id)

    with Store.open(path) as fresh:
        with pytest.raises(DomainError) as blocked:
            fresh.create_conversation(a)
        assert_error(blocked, 403, "user_disabled")


def test_disable_race_never_admits_work_for_a_disabled_user(tmp_path):
    """并发新建会话与禁用竞争：两种先后顺序下结论都必须一致且可解释。"""
    path = tmp_path / "db"
    with Store.open(path) as setup:
        a = setup.create_user("A")

    outcome = run_concurrently(path, lambda store, index: (
        store.create_conversation(a).id if index == 0 else store.disable_user(a.user_id)))

    conversations = [item for item in outcome if isinstance(item, str)]
    disable_won = [item for item in outcome if item is True]
    disable_missed = [item for item in outcome if item is False]
    errors = [item for item in outcome if isinstance(item, DomainError)]

    # 恰好一种结果：并发新建成功，或禁用先落地使新建被拒
    assert len(conversations) + len(errors) == 1, outcome
    assert len(disable_won) + len(disable_missed) == 1, outcome
    # 禁用这侧总是先落地：新建要么赶在它之前成功，要么被它挡下
    assert disable_missed == [], outcome
    assert disable_won == [True], outcome
    if errors:
        assert [(error.status, error.code) for error in errors] == [(403, "user_disabled")]

    # 无论谁先到，禁用后的新建一律被拒
    with Store.open(path) as reopened:
        # 新建先成则此处才是首次禁用；否则禁用已在并发侧生效
        assert reopened.disable_user(a.user_id) is bool(conversations)
        with pytest.raises(DomainError) as blocked:
            reopened.create_conversation(a)
        assert_error(blocked, 403, "user_disabled")


# --------------------------------------------------------------------------
# 未知 schema 版本与迁移
# --------------------------------------------------------------------------

def test_newer_schema_version_is_rejected_without_rebuild(tmp_path):
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE future_only (id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
    finally:
        connection.close()

    # Store.open 是 context manager 工厂：必须真正进入上下文才会连接与迁移
    with pytest.raises(DomainError) as rejected:
        with Store.open(path):
            pass
    assert_error(rejected, 500, "unsupported_schema")

    connection = sqlite3.connect(path)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "future_only" in tables
        assert "users" not in tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    finally:
        connection.close()


def test_empty_database_file_is_initialized(tmp_path):
    """部署时预创建的空文件（0 字节）必须能直接建库。"""
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")

    with Store.open(empty) as store:
        user = store.create_user("A")
        conversation = store.create_conversation(user)
        assert store.get_conversation(user, conversation.id).id == conversation.id


def test_version_zero_database_is_migrated_in_place(tmp_path):
    """已有其他表的 0 版本库就地升级，不丢弃原有表。"""
    legacy = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(legacy)
    try:
        connection.execute("CREATE TABLE legacy_notes (id TEXT PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO legacy_notes (id, body) VALUES ('n1', '保留我')")
        connection.commit()
    finally:
        connection.close()

    with Store.open(legacy) as store:
        user = store.create_user("A")
        run = reserve(store, user, store.create_conversation(user).id)
        assert store.get_run(user, run.id).id == run.id

    connection = sqlite3.connect(legacy)
    try:
        assert connection.execute("SELECT body FROM legacy_notes").fetchone()[0] == "保留我"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    finally:
        connection.close()


def test_current_schema_version_is_recorded(tmp_path):
    path = tmp_path / "db"
    with Store.open(path) as store:
        store.create_user("A")

    connection = sqlite3.connect(path)
    try:
        applied = [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
        assert applied == list(range(1, CURRENT_SCHEMA_VERSION + 1))
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        # 外键与 busy_timeout 是连接级设置，新开的裸连接默认关闭；由 Store 每条连接显式设置
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 参数化 SQL 与输入原样保存
# --------------------------------------------------------------------------

def test_hostile_input_is_stored_verbatim(tmp_path):
    hostile = "'; DROP TABLE users; --"
    with Store.open(tmp_path / "db") as store:
        user = store.create_user(hostile)
        conversation = store.create_conversation(user, hostile)
        run = reserve(store, user, conversation.id, hostile, hostile)

        assert store.get_conversation(user, conversation.id).title == hostile
        assert store.get_run(user, run.id).id == run.id
        assert store.create_user(hostile).user_id != user.user_id


# --------------------------------------------------------------------------
# 共享类型不泄漏凭证
# --------------------------------------------------------------------------

def test_shared_types_never_repr_credentials():
    session = BrowserSession("login-1", "secret-token-value", "secret-csrf-value")
    handle = WorkerHandle("ws-1", "container-1", "http://worker:7860", "service-secret", 3)

    assert "secret-token-value" not in repr(session)
    assert "secret-csrf-value" not in repr(session)
    assert "secret-token-value" not in str(session)
    assert "service-secret" not in repr(handle)
    assert "service-secret" not in str(handle)
    assert "login-1" in repr(session)
    assert "ws-1" in repr(handle)
