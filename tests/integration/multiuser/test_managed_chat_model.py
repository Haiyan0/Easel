"""Task 2：真实锁定 Harness + 假 Key + 受控 provider 的受管模型链路验收。

复用仓库已有的 A5 真实拓扑测试（control/gateway/provisioner/sidecar/broker
+ 固定 Harness 镜像 + 受控 TLS fake provider），只把管理员 chat 模型改为
deepseek-flash，验证 provider 实际收到的 model 与签名路由一致。

不读取真实 Key、不调用真实供应商、不触碰用户运行实例；容器/网络/卷均带
本轮随机 run-id 并在 finally 清理。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(
    os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="设置 EASEL_RUN_CONTAINER_TESTS=1 才运行真实 Harness 拓扑测试")


def _load_real_harness():
    path = pathlib.Path(__file__).with_name("test_harness_runtime_real.py")
    spec = importlib.util.spec_from_file_location(
        "task2_real_harness", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_a6():
    path = pathlib.Path(__file__).with_name("test_desktop_chat_live.py")
    spec = importlib.util.spec_from_file_location("task2_a6_live", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _task2_images():
    control_image = os.environ.get("EASEL_TASK2_CONTROL_IMAGE", "")
    worker_image = os.environ.get("EASEL_TASK2_WORKER_IMAGE", "")
    if not control_image or not worker_image:
        pytest.skip("需要 EASEL_TASK2_CONTROL_IMAGE 与"
                    " EASEL_TASK2_WORKER_IMAGE 指向本轮构建镜像")
    return control_image, worker_image


def test_managed_chat_model_real_harness(tmp_path, monkeypatch):
    """管理员 publish flash 后，真实 Harness 请求必须是 flash，resume 亦然。"""
    model = os.environ.get("EASEL_TASK2_MODEL", "deepseek-flash")
    control_image, worker_image = _task2_images()
    real = _load_real_harness()
    monkeypatch.setattr(real, "PROVIDER_MODEL", model)
    monkeypatch.setattr(real.a4, "PROVIDER_MODEL", model)
    monkeypatch.setattr(real, "CONTROL_TAG", control_image)
    monkeypatch.setenv("EASEL_HARNESS_WORKER_IMAGE", worker_image)
    real.test_real_harness_broker_resume_and_failure_semantics(tmp_path)

    audit = tmp_path / "provider-audit" / "requests.jsonl"
    assert audit.is_file(), "真实受控 provider 未留下审计记录"
    records = [
        json.loads(line) for line in
        audit.read_text(encoding="utf-8").splitlines() if line.strip()]
    chat = [record for record in records
            if record.get("path") == "/v1/chat/completions"]
    models = sorted({(record.get("body") or {}).get("model")
                     for record in chat})
    assert models == [model], models
    assert all(
        record.get("authorization") == f"Bearer {real.a4.SENTINEL_KEY}"
        for record in chat)
    print("TASK2_MODEL_AUDIT", json.dumps({
        "provider_model": model,
        "chat_requests": len(chat),
        "workspace_requests": len(records),
        "authorization_is_fake_sentinel": True,
    }, sort_keys=True))


def test_managed_chat_model_permission_stop_real(tmp_path, monkeypatch):
    """同一镜像下的真实权限拒绝与停止后继续，仍使用管理员 flash 模型。"""
    model = os.environ.get("EASEL_TASK2_MODEL", "deepseek-flash")
    control_image, worker_image = _task2_images()
    a6 = _load_a6()
    worker_id = a6.a4._docker(
        "image", "inspect", "--format", "{{.Id}}", worker_image).stdout.strip()
    assert worker_id.startswith("sha256:")
    monkeypatch.setattr(a6, "CONTROL_TAG", control_image)
    monkeypatch.setattr(a6, "HARNESS_IMAGE_ID", worker_id)
    monkeypatch.setattr(a6, "PROVIDER_MODEL", model)
    monkeypatch.setattr(a6.a4, "PROVIDER_MODEL", model)
    monkeypatch.setattr(a6, "SCREENSHOTS", tmp_path / "screenshots")
    a6.test_browser_permission_stop_sse_and_revocation(tmp_path)
