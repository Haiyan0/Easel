"""Built-in adapters retain upstream compatibility after moving out of web/app.py."""
from __future__ import annotations

import sys
import asyncio
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
import app as web
from easel.trends import builtin
from easel.trends.protocol import validate_payload
from easel.trends.sources import load_sources


@pytest.mark.parametrize("response,hot,url", [
    ({"data": ["标题", "   "]}, "", ""),
    ({"data": [{"Title": "标题", "HotValue": 1234567, "Url": "https://example.com/1"}]},
     "1234567", "https://example.com/1"),
    ({"data": [{"target": {"title": "标题", "url": "https://api.zhihu.com/questions/123",
                            "metrics_area": {"text": "387 万热度"}}}]},
     "387 万热度", "https://www.zhihu.com/question/123"),
    ({"data": [{"title": "标题", "hot": "118万", "url": "https://example.com/1"}]},
     "118万", "https://example.com/1"),
    ({"data": {"list": [{"word": "标题", "num": 42}]}}, "42", ""),
    ({"data": {"data": [{"name": "标题"}]}}, "", ""),
])
def test_builtin_normalizes_upstream_shapes(response, hot, url):
    assert builtin.parse_upstream(response) == [{"title": "标题", "url": url, "heat": {"text": hot}}]


@pytest.mark.parametrize("bad", [{}, {"data": None}, {"data": "invalid"}, {"data": [1, 2]},
                                  {"data": [{"hot": "1"}]}, []])
def test_builtin_rejects_junk(bad):
    assert builtin.parse_upstream(bad) == []


@pytest.mark.parametrize("mode", ["exception", "empty", "malformed"])
def test_builtin_falls_back_and_returns_wire_contract(monkeypatch, mode):
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            if mode == "exception":
                raise OSError("upstream unavailable")
            if mode == "malformed":
                return {"data": [{"title": "bad link", "url": "javascript:alert(1)"}]}
            return {"data": []}
        return {"data": [{"title": "备用热点"}]}

    monkeypatch.setattr(builtin, "fetch_json", fetch)
    result = validate_payload(builtin.collect_platform("weibo"))
    assert result["items"][0]["title"] == "备用热点"
    assert calls == list(builtin.SOURCES["weibo"])


def test_trends_reuses_cached_items_with_stale_marker_when_sources_fail(
        monkeypatch):
    items = [{"title": "缓存热点", "hot": "", "url": ""}]
    cached_at = time.time() - 3600
    web._TREND_CACHE["weibo"] = (cached_at, items)
    monkeypatch.setattr(web, "_fetch_platform", lambda platform: [])
    data = asyncio.run(web.api_trends("weibo", 15))
    group = data["trends"][0]
    assert group["items"] == items
    assert group["ok"] is False
    assert group["stale"] is True
    assert group["cached_at"] == int(cached_at)
    assert data["updated"] == int(cached_at)
    assert data["stale"] is True
    assert data["failed_platforms"] == ["weibo"]


def test_trends_fresh_and_valid_cache_are_not_marked_stale(monkeypatch):
    now = time.time()
    web._TREND_CACHE.clear()
    web._TREND_CACHE["weibo"] = (
        now - 30, [{"title": "有效缓存", "hot": "", "url": ""}])
    monkeypatch.setattr(web, "_fetch_platform", lambda platform: [])
    cached = asyncio.run(web.api_trends("weibo", 15))
    assert cached["trends"][0]["ok"] is True
    assert "stale" not in cached["trends"][0]
    assert cached["updated"] == int(web._TREND_CACHE["weibo"][0])
    assert "stale" not in cached

    web._TREND_CACHE.clear()
    monkeypatch.setattr(
        web, "_fetch_platform",
        lambda platform: [{"title": "新热点", "hot": "", "url": ""}])
    fresh = asyncio.run(web.api_trends("weibo", 15))
    assert fresh["trends"][0]["ok"] is True
    assert "stale" not in fresh["trends"][0]
    assert fresh["updated"] >= int(now)
    assert "stale" not in fresh
