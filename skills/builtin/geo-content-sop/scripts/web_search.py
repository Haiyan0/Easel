#!/usr/bin/env python3
"""GEO 技能的可选 Tavily 检索回退脚本。

优先使用运行环境自带的联网检索工具；只有环境没有检索工具或检索失败时，
才在本机显式调用本脚本。密钥只从进程环境或用户指定的 env 文件读取，
不写入仓库、不打印、不拼进命令。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

DEFAULT_ENDPOINT = "https://api.tavily.com/search"
REQUEST_TIMEOUT = 30
MAX_RESULTS_LIMIT = 10
MAX_QUERY_CHARS = 400
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SearchError(RuntimeError):
    """检索参数、配置或上游响应不可用。"""


class MissingApiKeyError(SearchError):
    """未提供 Tavily API Key。"""


def _normalize_results(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise SearchError("检索服务返回的不是 JSON 对象")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise SearchError("检索服务响应缺少 results 列表")

    results: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": url,
                "content": str(item.get("content") or "").strip(),
                "score": item.get("score"),
            }
        )
    return results


def search(
    query: str,
    *,
    max_results: int = 5,
    api_key: str = "",
    post: Callable[..., object] | None = None,
) -> list[dict]:
    """调用 Tavily 检索并只返回标题、URL、摘要与相关度。"""
    query = (query or "").strip()
    if not query:
        raise SearchError("检索词不能为空")
    if len(query) > MAX_QUERY_CHARS:
        raise SearchError(f"检索词不能超过 {MAX_QUERY_CHARS} 个字符")
    if not 1 <= int(max_results) <= MAX_RESULTS_LIMIT:
        raise SearchError(f"max_results 必须在 1..{MAX_RESULTS_LIMIT} 之间")

    api_key = (api_key or "").strip()
    if not api_key:
        raise MissingApiKeyError(
            "缺少 TAVILY_API_KEY；请通过环境变量或 --env-file 提供，不要把密钥写入仓库"
        )

    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": int(max_results),
    }
    sender = post or requests.post
    try:
        response = sender(DEFAULT_ENDPOINT, json=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except SearchError:
        raise
    except Exception as error:  # noqa: BLE001 - 上游异常统一转为可读错误
        raise SearchError(f"检索请求失败: {error}") from error

    return _normalize_results(data)


def load_env_file(path: str | Path) -> dict[str, str]:
    """读取 KEY=VALUE 形式的 env 文件。

    只做字面量解析：不展开变量、不执行命令、不去除值内部的空白差异。
    重复键、非法键名一律报错，避免后写覆盖前写造成密钥来源不明。
    """
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        raise SearchError(f"env 文件不存在: {env_path}")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise SearchError(f"env 文件第 {line_number} 行不是 KEY=VALUE 格式")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _ENV_KEY_RE.match(key):
            raise SearchError(f"env 文件第 {line_number} 行键名非法: {key!r}")
        if key in values:
            raise SearchError(f"env 文件存在重复键: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _bounded_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是整数") from error
    if not 1 <= number <= MAX_RESULTS_LIMIT:
        raise argparse.ArgumentTypeError(f"必须在 1..{MAX_RESULTS_LIMIT} 之间")
    return number


def _resolve_api_key(env_file: str | None) -> str:
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if api_key:
        return api_key
    if env_file:
        return load_env_file(env_file).get("TAVILY_API_KEY", "").strip()
    return ""


def _validate_result_urls(results: list[dict]) -> None:
    for item in results:
        parsed = urlparse(item["url"])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SearchError(f"检索结果包含非法 URL: {item['url']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="GEO 检索回退：调用 Tavily 并输出 JSON 结果"
    )
    parser.add_argument("query", help="检索词，最多 400 字符")
    parser.add_argument(
        "--max-results", type=_bounded_int, default=5, help="返回条数，默认 5，最多 10"
    )
    parser.add_argument(
        "--env-file",
        help="从该文件读取 TAVILY_API_KEY；文件必须在本机且不得提交到仓库",
    )
    args = parser.parse_args(argv)

    try:
        api_key = _resolve_api_key(args.env_file)
        if not api_key:
            raise MissingApiKeyError(
                "缺少 TAVILY_API_KEY；请设置环境变量或通过 --env-file 提供"
            )
        results = search(args.query, max_results=args.max_results, api_key=api_key)
        _validate_result_urls(results)
    except MissingApiKeyError as error:
        print(f"配置错误: {error}", file=sys.stderr)
        return 2
    except SearchError as error:
        print(f"检索失败: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {"query": args.query.strip(), "results": results},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
