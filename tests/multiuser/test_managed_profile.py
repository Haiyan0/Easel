"""A5 受管 ACP manifest、Harness 原生 overlay 与构建期校验器。"""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "multiuser"
MANAGED = DEPLOY / "managed"
MANIFEST = MANAGED / "acp-profile.json"
PATCH = MANAGED / "acp.patch.yml"

for entry in (str(ROOT), str(DEPLOY)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

spec = importlib.util.spec_from_file_location(
    "verify_managed_profile", DEPLOY / "verify_managed_profile.py")
managed = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = managed
spec.loader.exec_module(managed)


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_selects_fixed_harness_acp_and_easel_broker():
    data = managed.validate_manifest(
        _manifest(), expected_commit=managed.HARNESS_COMMIT,
        patch_root=MANAGED)
    assert data["manifest"] == "easel.managed-acp-profile/1"
    assert data["harness"]["commit"] == (
        "687f282e8625c6b8d11ff58211a90524be2e4317")
    assert data["harness"]["node"] == "24.22.1"
    assert data["harness"]["pnpm"] == "11.28.5"
    assert data["harness"]["bundles"] == [
        "@deepseek-ai/dsh-base", "@deepseek-ai/dsh-acp-app"]
    assert data["command"] == [
        "node", "/opt/dsh/apps/cli/lib/bin.js", "--profile", "acp",
        "--patch", "/opt/easel/managed/acp.patch.yml"]
    assert data["provider"] == {
        "id": "easel-broker", "api": "openai-completions",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-flash", "deepseek-v4-pro"]}
    # 明确是 Easel manifest，不冒充 Harness profile schema。
    assert "dsh" not in data


def test_patch_declares_every_manifest_model():
    """Harness 的实际模型目录必须与受管 manifest 的信任目录一致。"""
    manifest = _manifest()
    patch = PATCH.read_text(encoding="utf-8")
    assert _patch_models(patch) == manifest["provider"]["models"]


def _patch_models(patch_text: str) -> list[str]:
    """从 llm-pi-ai 的 models 段解析模型 ID（不依赖第三方 YAML）。"""
    lines = patch_text.splitlines()
    start = None
    indent = None
    for index, line in enumerate(lines):
        if line.strip() == "models:" and "llm-pi-ai" in "\n".join(
                lines[max(0, index - 20):index]):
            start = index
            indent = len(line) - len(line.lstrip())
            break
    if start is None:
        return []
    models: list[str] = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        stripped = line.strip()
        if stripped.startswith("- id: "):
            models.append(stripped[len("- id: "):])
    return models


def test_validator_rejects_patch_not_matching_manifest_catalog(tmp_path):
    manifest = _manifest()
    (tmp_path / "acp-profile.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    patch = PATCH.read_text(encoding="utf-8")
    (tmp_path / "acp.patch.yml").write_text(
        patch.replace(
            "          - id: deepseek-flash\n"
            "            name: DeepSeek Flash (Easel broker)\n"
            "            contextWindow: 65536\n"
            "            maxTokens: 4096\n"
            "            reasoningEfforts: false\n", ""),
        encoding="utf-8")
    with pytest.raises(managed.ProfileError):
        managed.validate_manifest(manifest, patch_root=tmp_path)


def test_validator_rejects_default_model_outside_manifest_catalog(tmp_path):
    manifest = copy.deepcopy(_manifest())
    manifest["provider"]["model"] = "deepseek-other"
    (tmp_path / "acp-profile.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    (tmp_path / "acp.patch.yml").write_text(
        PATCH.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(managed.ProfileError):
        managed.validate_manifest(manifest, patch_root=tmp_path)


def test_patch_is_harness_native_and_uses_run_scoped_broker_env():
    text = PATCH.read_text(encoding="utf-8")
    for marker in (
            "provider: easel-broker",
            "api: openai-completions",
            "EASEL_BROKER_URL",
            "apiKeyEnv: EASEL_BROKER_TOKEN",
            "EASEL_WORKSPACE_ID",
            "EASEL_RUN_ID",
            "maxRetries: 0",
            "model: deepseek-chat"):
        assert marker in text
    assert "authorization:" not in text.lower()
    assert "sk-" not in text
    assert "Bearer " not in text


def test_validator_rejects_commit_mismatch():
    with pytest.raises(managed.ProfileError):
        managed.validate_manifest(
            _manifest(), expected_commit="0" * 40, patch_root=MANAGED)


def test_validator_rejects_secret_field_or_value():
    secret_field = _manifest()
    secret_field["provider"]["api_key"] = "not-allowed"
    with pytest.raises(managed.ProfileError):
        managed.validate_manifest(secret_field, patch_root=MANAGED)

    secret_value = _manifest()
    secret_value["validated"]["scope"] = "CHAT_API_KEY=sk-live-secret"
    with pytest.raises(managed.ProfileError):
        managed.validate_manifest(secret_value, patch_root=MANAGED)


def test_validator_rejects_wrong_command_or_bundle():
    wrong_command = _manifest()
    wrong_command["command"] = ["node", "/usr/bin/other.js"]
    with pytest.raises(managed.ProfileError):
        managed.validate_manifest(wrong_command, patch_root=MANAGED)

    wrong_bundle = _manifest()
    wrong_bundle["harness"]["bundles"] = ["@deepseek-ai/dsh-base"]
    with pytest.raises(managed.ProfileError):
        managed.validate_manifest(wrong_bundle, patch_root=MANAGED)


def test_validator_rejects_missing_patch(tmp_path):
    manifest = _manifest()
    (tmp_path / "acp-profile.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    with pytest.raises(managed.ProfileError):
        managed.validate_manifest(manifest, patch_root=tmp_path)


def test_validator_rejects_patch_with_secret(tmp_path):
    manifest = _manifest()
    (tmp_path / "acp-profile.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    (tmp_path / "acp.patch.yml").write_text(
        PATCH.read_text(encoding="utf-8") + "\napiKey: sk-live-secret\n",
        encoding="utf-8")
    with pytest.raises(managed.ProfileError):
        managed.validate_manifest(manifest, patch_root=tmp_path)


def test_validator_rejects_patch_missing_broker_markers(tmp_path):
    manifest = copy.deepcopy(_manifest())
    (tmp_path / "acp-profile.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    (tmp_path / "acp.patch.yml").write_text(
        "- id: acp\n  config:\n    provider: deepseek-official\n",
        encoding="utf-8")
    with pytest.raises(managed.ProfileError):
        managed.validate_manifest(manifest, patch_root=tmp_path)


def _fake_dsh(tmp_path: pathlib.Path, *, head: str | None = None,
              cli: bool = True) -> pathlib.Path:
    dsh = tmp_path / "dsh"
    (dsh / ".git").mkdir(parents=True)
    commit = head if head is not None else managed.HARNESS_COMMIT
    (dsh / ".git" / "HEAD").write_text(commit + "\n", encoding="ascii")
    if cli:
        artifact = dsh / "apps" / "cli" / "lib" / "bin.js"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("// built\n", encoding="utf-8")
        (dsh / "apps" / "cli" / "package.json").write_text(
            json.dumps({"version": managed.HARNESS_VERSION}),
            encoding="utf-8")
    return dsh


def test_installation_rejects_wrong_checkout_and_missing_cli(tmp_path):
    manifest = _manifest()
    wrong = _fake_dsh(tmp_path / "wrong", head="0" * 40)
    with pytest.raises(managed.ProfileError):
        managed.verify_installation(
            manifest, dsh_root=wrong, managed_root=MANAGED)

    missing = _fake_dsh(tmp_path / "missing", cli=False)
    with pytest.raises(managed.ProfileError):
        managed.verify_installation(
            manifest, dsh_root=missing, managed_root=MANAGED)


def test_installation_rejects_missing_patch_before_native_probe(tmp_path):
    manifest = _manifest()
    dsh = _fake_dsh(tmp_path / "dsh")
    managed_root = tmp_path / "managed"
    managed_root.mkdir()
    with pytest.raises(managed.ProfileError):
        managed.verify_installation(
            manifest, dsh_root=dsh, managed_root=managed_root)


@pytest.mark.parametrize("version,allowed", [
    ((22, 18, 0), False),
    ((22, 19, 0), True),
    ((23, 0, 0), False),
    ((24, 0, 0), True),
    ((24, 22, 1), True),
])
def test_node_engine_range(version, allowed):
    assert managed._supported_node(version) is allowed
