"""geo-content-sop 的 Tavily 检索回退脚本。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "builtin"
    / "geo-content-sop"
    / "scripts"
    / "web_search.py"
)


def _load_module():
    assert SCRIPT_PATH.is_file(), "缺少 web_search.py"
    spec = importlib.util.spec_from_file_location("geo_web_search", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # 不把 __pycache__ 写进技能目录
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def test_search_normalizes_tavily_results() -> None:
    module = _load_module()
    captured: dict = {}

    def fake_post(url, *, json, timeout):  # noqa: ANN001 - 测试替身
        captured.update({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse(
            {
                "results": [
                    {
                        "title": "示例标题",
                        "url": "https://example.com/a",
                        "content": "摘要内容",
                        "score": 0.91,
                    },
                    {
                        "title": "第二条",
                        "url": "https://example.com/b",
                        "content": "第二条摘要",
                        "score": 0.42,
                    },
                ]
            }
        )

    results = module.search(
        "GEO 内容优化",
        max_results=2,
        api_key="test-key",
        post=fake_post,
    )

    assert captured["url"] == module.DEFAULT_ENDPOINT
    assert captured["json"]["query"] == "GEO 内容优化"
    assert captured["json"]["max_results"] == 2
    assert captured["json"]["api_key"] == "test-key"
    assert results == [
        {
            "title": "示例标题",
            "url": "https://example.com/a",
            "content": "摘要内容",
            "score": 0.91,
        },
        {
            "title": "第二条",
            "url": "https://example.com/b",
            "content": "第二条摘要",
            "score": 0.42,
        },
    ]


def test_search_requires_api_key() -> None:
    module = _load_module()
    with pytest.raises(module.MissingApiKeyError):
        module.search("GEO", max_results=1, api_key="")


def test_search_requires_query() -> None:
    module = _load_module()
    with pytest.raises(module.SearchError):
        module.search("  ", max_results=1, api_key="test-key")


def test_load_env_file_is_literal_and_rejects_duplicates(tmp_path: Path) -> None:
    module = _load_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nTAVILY_API_KEY='tvly-demo'\nOTHER=$HOME\n",
        encoding="utf-8",
    )
    assert module.load_env_file(env_file) == {
        "TAVILY_API_KEY": "tvly-demo",
        "OTHER": "$HOME",
    }

    env_file.write_text("A=1\nA=2\n", encoding="utf-8")
    with pytest.raises(module.SearchError):
        module.load_env_file(env_file)


def test_main_prints_json_and_uses_env_file(tmp_path: Path, monkeypatch, capsys) -> None:
    module = _load_module()
    env_file = tmp_path / ".env"
    env_file.write_text("TAVILY_API_KEY=tvly-demo\n", encoding="utf-8")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(
        module.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(
            {"results": [{"title": "T", "url": "https://example.com", "content": "C", "score": 1.0}]}
        ),
    )

    exit_code = module.main(["--env-file", str(env_file), "GEO", "--max-results", "1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "GEO"
    assert payload["results"][0]["url"] == "https://example.com"
