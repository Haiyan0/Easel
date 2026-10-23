"""Task 11：control 固定前端构建目录的公开文件边界。"""
from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.store import Store

ORIGIN = "https://easel.test"


def make_frontend(root):
    root.mkdir()
    (root / "index.html").write_text("<html>control</html>", encoding="utf-8")
    assets = root / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (root / "icons.svg").write_text("<svg/>", encoding="utf-8")
    return root


def test_control_serves_only_fixed_frontend_files(tmp_path):
    frontend = make_frontend(tmp_path / "dist")
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-serve", encoding="utf-8")
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        app = create_app(store=store, auth=AuthService(store),
                         config=ControlConfig(public_origin=ORIGIN),
                         frontend_dir=frontend)
        with TestClient(app, base_url=ORIGIN) as client:
            index = client.get("/")
            assert index.status_code == 200
            assert "control" in index.text
            assert index.headers["cache-control"] == "no-store"
            assert client.get("/onepage").status_code == 200
            asset = client.get("/assets/app.js")
            assert asset.status_code == 200
            assert "immutable" in asset.headers["cache-control"]
            assert client.get("/favicon.svg").status_code == 200
            assert client.get("/icons.svg").status_code == 200
            assert client.get("/assets/missing.js").status_code == 404
            # 目录穿越与任意服务器路径都不得读取
            for path in ("/assets/..%2Fsecret.txt",
                         "/assets/%2e%2e/secret.txt",
                         "/static/%2e%2e/secret.txt"):
                response = client.get(path)
                assert response.status_code == 404, (path, response.text)
                assert "do-not-serve" not in response.text
            # 未登记的路由仍默认拒绝
            assert client.get("/package.json").status_code == 401
    finally:
        store.close()


def test_missing_frontend_build_fails_closed(tmp_path):
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        with pytest.raises(ValueError, match="frontend_build_missing"):
            create_app(store=store, auth=AuthService(store),
                       config=ControlConfig(public_origin=ORIGIN),
                       frontend_dir=tmp_path / "missing")
    finally:
        store.close()
