"""控制层会话目录：只保存归属元数据，不读取 worker 私有历史。

消息快照位于 worker 本人的私有根；Task 10 的授权 relay 尚未装配时，
``messages`` 明确返回 ``capability_unavailable``，不读取服务器任意文件路径，
也不把空结果伪装成成功历史。
"""
from __future__ import annotations

from collections.abc import Callable

from .store import Store
from .types import ConversationRecord, DomainError, Identity, RunRecord


class SessionService:
    """会话目录的 actor 边界；所有查询回到 Store 重查归属。"""

    def __init__(self, store: Store,
                 message_reader: Callable[..., list[dict]] | None = None):
        if not isinstance(store, Store):
            raise TypeError("store must be a Store")
        self._store = store
        self._message_reader = message_reader

    def list_conversations(self, actor: Identity, *, limit: int = 50,
                           offset: int = 0,
                           include_archived: bool = False
                           ) -> list[ConversationRecord]:
        return self._store.list_conversations(
            actor, limit=limit, offset=offset,
            include_archived=include_archived)

    def create_conversation(self, actor: Identity,
                            title: str = "新对话") -> ConversationRecord:
        return self._store.create_conversation(actor, title)

    def get_conversation(self, actor: Identity,
                         session_id: str) -> ConversationRecord:
        return self._store.get_conversation(actor, session_id)

    def update_conversation(self, actor: Identity, session_id: str, *,
                            title: str | None = None,
                            archived: bool | None = None
                            ) -> ConversationRecord:
        return self._store.update_conversation(
            actor, session_id, title=title, archived=archived)

    def delete_conversation(self, actor: Identity, session_id: str) -> None:
        self._store.delete_conversation(actor, session_id)

    def assert_run(self, actor: Identity, run_id: str) -> RunRecord:
        return self._store.get_run(actor, run_id)

    def messages(self, actor: Identity, session_id: str, *,
                 limit: int = 100, offset: int = 0) -> list[dict]:
        """只有实际授权 transport 才能返回消息；本批不伪造空历史。"""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise DomainError("invalid_argument", 400)
        if type(offset) is not int or offset < 0:
            raise DomainError("invalid_argument", 400)
        conversation = self._store.get_conversation(actor, session_id)
        if self._message_reader is None:
            raise DomainError("capability_unavailable", 503)
        return self._message_reader(
            actor, conversation, limit=limit, offset=offset)
