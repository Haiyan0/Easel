"""Task 11：control 固定前端构建目录的公开文件边界。"""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from easel.control.app import ControlConfig, create_app
from easel.control.auth import AuthService
from easel.control.store import Store

ORIGIN = "https://easel.test"
ROOT = Path(__file__).resolve().parents[2]
REAL_DIST = ROOT / "web" / "frontend" / "dist"
BRAND_SOURCE = ROOT / "web" / "static" / "easel-icon-transparent.png"
BRAND_NAME = "easel-icon-transparent.png"
BRAND_SHA256 = (
    "71c260f5a28e82aced0a513186ebdcf43782b068633e3a7ca5f159d3e3606132"
)
BRAND_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000000020001e221bc330000000049454e44"
    "ae426082"
)


def make_frontend(root):
    root.mkdir()
    (root / "index.html").write_text("<html>control</html>", encoding="utf-8")
    assets = root / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")
    static = root / "static"
    static.mkdir()
    (static / BRAND_NAME).write_bytes(BRAND_PNG)
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
            brand = client.get(f"/static/{BRAND_NAME}")
            assert brand.status_code == 200
            assert brand.content == BRAND_PNG
            assert brand.headers["content-type"] == "image/png"
            assert client.get("/static/missing.png").status_code == 404
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


def test_real_dist_brand_asset_is_served_from_the_fixed_static_dir(tmp_path):
    if not (REAL_DIST / "index.html").is_file():
        pytest.skip("frontend dist not built")
    store = Store.connect(tmp_path / "control.sqlite3")
    try:
        app = create_app(store=store, auth=AuthService(store),
                         config=ControlConfig(public_origin=ORIGIN),
                         frontend_dir=REAL_DIST)
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.get(f"/static/{BRAND_NAME}")
            assert response.status_code == 200, (
                "dist/static 未包含品牌图；请先 npm --prefix web/frontend run build")
            assert response.headers["content-type"] == "image/png"
            source = BRAND_SOURCE.read_bytes()
            assert response.content == source
            assert hashlib.sha256(response.content).hexdigest() == BRAND_SHA256
            for path in ("/static/missing.png",
                         "/static/%2e%2e/package.json",
                         "/static/%2e%2e/index.html"):
                denied = client.get(path)
                assert denied.status_code == 404, (path, denied.text)
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
