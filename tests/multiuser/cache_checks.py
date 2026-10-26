"""浏览器持久缓存的键+值隔离断言。

只读取 localStorage/sessionStorage 的键和值做 sentinel 检查；不在失败
消息中输出缓存正文、token 或邀请。
"""
from __future__ import annotations


def cache_dump(page) -> str:
    return page.evaluate(
        "JSON.stringify({"
        "local: Object.entries(localStorage),"
        "session: Object.entries(sessionStorage)"
        "})")


def assert_no_private_cache(page, markers, *, label: str = "B") -> None:
    dump = cache_dump(page)
    for marker in markers:
        if marker and marker in dump:
            raise AssertionError(f"{label} 的持久缓存包含 A 私有数据")
