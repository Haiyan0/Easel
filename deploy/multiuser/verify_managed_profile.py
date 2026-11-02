#!/usr/bin/env python3
"""A5 受管 ACP profile 校验器。

只使用标准库，可在最终 worker 镜像内、任何在线安装之前运行。它证明：

* ``managed/acp-profile.json`` 是 Easel manifest（不是 Harness 原生 schema）；
* 固定 Harness commit / Node / pnpm / bundle / 精确 broker command；
* 引用的 Harness 原生 patch 存在且不含秘密；
* ``/opt/dsh`` 的实际 checkout、CLI 构建产物和 Linux 原生 flock 可加载。

真实 ACP initialize/session new 由 ``smoke_harness.py`` 单独执行；两者都
通过后，``--require-dsh`` 才不会退化成只检查 ``.git/HEAD``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

MANIFEST_SCHEMA = "easel.managed-acp-profile/1"
HARNESS_REPOSITORY = "https://github.com/deepseek-ai/deepseek-harness.git"
HARNESS_COMMIT = "687f282e8625c6b8d11ff58211a90524be2e4317"
HARNESS_VERSION = "0.2.1-rc.2"
NODE_ENGINES = "^22.19.0 || >=24.0.0"
NODE_VERSION = "24.22.1"
PNPM_VERSION = "11.28.5"
PROFILE_NAME = "acp"
BUNDLES = ("@deepseek-ai/dsh-base", "@deepseek-ai/dsh-acp-app")
PATCH_NAME = "acp.patch.yml"
COMMAND = (
    "node",
    "/opt/dsh/apps/cli/lib/bin.js",
    "--profile",
    PROFILE_NAME,
    "--patch",
    "/opt/easel/managed/" + PATCH_NAME,
)
PROVIDER_ID = "easel-broker"
PROVIDER_API = "openai-completions"
PROVIDER_MODEL = "deepseek-chat"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PATCH_BYTES = 64 * 1024
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_NODE_OUTPUT = re.compile(r"^v(\d+)\.(\d+)\.(\d+)")
_SECRET_KEYS = frozenset({
    "api_key", "apikey", "secret", "password", "token", "access_token",
    "authorization", "credential", "credentials", "private_key",
    "client_secret", "broker_token",
})
_SECRET_VALUES = (
    re.compile(r"^sk-[A-Za-z0-9]"),
    re.compile(r"PRIVATE KEY"),
    re.compile(r"(?i)^Bearer\s+\S"),
    re.compile(r"(?i)(CHAT_API_KEY|DEEPSEEK_API_KEY|EASEL_BROKER_TOKEN|"
               r"API_KEY|SECRET|TOKEN)\s*=\s*\S"),
)
_YAML_SECRET_LINE = re.compile(
    r"(?im)^\s*(api[_-]?key|apikey|secret|password|token|authorization|"
    r"client_secret|private_key)\s*:\s*\S")
_PATCH_MARKERS = (
    "- id: acp",
    "provider: easel-broker",
    "- id: llm-pi-ai",
    "api: openai-completions",
    "baseURL: !!js",
    "EASEL_BROKER_URL",
    "apiKeyEnv: EASEL_BROKER_TOKEN",
    "EASEL_WORKSPACE_ID",
    "EASEL_RUN_ID",
    "maxRetries: 0",
)
_NATIVE_PROBE = r"""
import { closeSync, openSync } from 'node:fs'
const { tryLockExclusive } = await import(
  'file:///opt/dsh/native/system/packages/entry/lib/flock.js')
const path = process.env.EASEL_FLOCK_SMOKE
if (!path) throw new Error('EASEL_FLOCK_SMOKE is missing')
const fd = openSync(path, 'w')
try {
  await tryLockExclusive(fd)
} finally {
  closeSync(fd)
}
console.log('flock-ok')
"""


class ProfileError(ValueError):
    """受管 profile 或实际安装不满足 A5 契约。"""


def _fail(message: str) -> None:
    raise ProfileError(message)


def _text(value: object, name: str) -> str:
    if (not isinstance(value, str) or not value
            or any(ord(character) < 32 for character in value)):
        _fail(f"{name} must be a non-empty single-line string")
    return value


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{name} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        _fail(f"{name} fields must be {sorted(expected)}")


def _scan_secrets(value: object, path: str = "manifest") -> None:
    """递归拒绝凭证字段或明显的凭证值；公开 commit/模型名不受影响。"""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                _fail(f"{path} contains an invalid key")
            if key.lower() in _SECRET_KEYS:
                _fail(f"{path}.{key} is a secret field")
            _scan_secrets(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_secrets(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        for pattern in _SECRET_VALUES:
            if pattern.search(value):
                _fail(f"{path} contains a secret-like value")


def _check_manifest_shape(manifest: object, expected_commit: str | None) -> dict:
    data = _mapping(manifest, "manifest")
    _exact_keys(
        data,
        {"manifest", "harness", "command", "patch", "broker", "provider",
         "validated"},
        "manifest")
    if data["manifest"] != MANIFEST_SCHEMA:
        _fail("manifest schema is not easel.managed-acp-profile/1")

    harness = _mapping(data["harness"], "harness")
    _exact_keys(
        harness,
        {"repository", "commit", "package_version", "node_engines", "node",
         "pnpm", "profile", "bundles"},
        "harness")
    if _text(harness["repository"], "harness.repository") != HARNESS_REPOSITORY:
        _fail("harness.repository is not the fixed public repository")
    commit = _text(harness["commit"], "harness.commit")
    if _COMMIT.fullmatch(commit) is None:
        _fail("harness.commit must be a full lowercase SHA-1")
    if commit != HARNESS_COMMIT:
        _fail("harness.commit is not the A5 candidate commit")
    if expected_commit is not None and commit != expected_commit:
        _fail("harness.commit does not match the build DSH_COMMIT")
    if _text(harness["package_version"], "harness.package_version") != \
            HARNESS_VERSION:
        _fail("harness.package_version is not the fixed release")
    if _text(harness["node_engines"], "harness.node_engines") != NODE_ENGINES:
        _fail("harness.node_engines is not the fixed range")
    if _text(harness["node"], "harness.node") != NODE_VERSION:
        _fail("harness.node is not the fixed build version")
    if _text(harness["pnpm"], "harness.pnpm") != PNPM_VERSION:
        _fail("harness.pnpm is not the fixed build version")
    if _text(harness["profile"], "harness.profile") != PROFILE_NAME:
        _fail("harness.profile must be acp")
    bundles = harness["bundles"]
    if not isinstance(bundles, list) or tuple(bundles) != BUNDLES:
        _fail("harness.bundles must be the public base + acp-app tuple")

    command = data["command"]
    if (not isinstance(command, list) or len(command) != len(COMMAND)
            or tuple(command) != COMMAND):
        _fail("command must be the exact fixed ACP argv")
    patch_name = _text(data["patch"], "patch")
    if (patch_name != PATCH_NAME or "/" in patch_name or "\\" in patch_name
            or patch_name in (".", "..")):
        _fail("patch must be the fixed relative file name")

    broker = _mapping(data["broker"], "broker")
    _exact_keys(broker, {"mode", "channel"}, "broker")
    if broker["mode"] != "workspace-sidecar" or broker["channel"] != "chat":
        _fail("broker must select the workspace sidecar chat channel")

    provider = _mapping(data["provider"], "provider")
    _exact_keys(provider, {"id", "api", "model", "models"}, "provider")
    if (_text(provider["id"], "provider.id") != PROVIDER_ID
            or _text(provider["api"], "provider.api") != PROVIDER_API
            or _text(provider["model"], "provider.model") != PROVIDER_MODEL):
        _fail("provider must be the fixed Easel broker route")
    models = provider["models"]
    if (not isinstance(models, list) or not 1 <= len(models) <= 64
            or any(not isinstance(item, str) or not item
                   or len(item) > 256
                   or any(ord(character) < 32 for character in item)
                   for item in models)
            or len(set(models)) != len(models)):
        _fail("provider.models must be a bounded unique model list")
    if provider["model"] not in models:
        _fail("provider.model must be inside provider.models")

    validated = _mapping(data["validated"], "validated")
    _exact_keys(validated, {"date", "scope"}, "validated")
    _text(validated["date"], "validated.date")
    _text(validated["scope"], "validated.scope")
    _scan_secrets(data)
    return dict(data)


def _patch_model_ids(text: str) -> list[str] | None:
    """解析 patch 的 llm-pi-ai models 段；不依赖第三方 YAML。"""
    lines = text.splitlines()
    start = None
    indent = 0
    for index, line in enumerate(lines):
        if line.strip() == "models:":
            start = index
            indent = len(line) - len(line.lstrip())
            break
    if start is None:
        return None
    models: list[str] = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        stripped = line.strip()
        if stripped.startswith("- id: "):
            models.append(stripped[len("- id: "):])
    return models


def _check_patch(patch_path: Path, models: list[str]) -> str:
    try:
        raw = patch_path.read_bytes()
    except OSError as error:
        _fail(f"managed patch is unreadable: {error.__class__.__name__}")
    if len(raw) > MAX_PATCH_BYTES:
        _fail("managed patch is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail("managed patch must be UTF-8")
    for marker in _PATCH_MARKERS:
        if marker not in text:
            _fail(f"managed patch is missing marker: {marker}")
    if _patch_model_ids(text) != list(models):
        _fail("managed patch models do not match manifest provider.models")
    for pattern in _SECRET_VALUES:
        if pattern.search(text):
            _fail("managed patch contains a secret-like value")
    if _YAML_SECRET_LINE.search(text):
        _fail("managed patch must not carry a literal secret")
    if "authorization:" in text.lower():
        _fail("managed patch must not carry an authorization header")
    return hashlib.sha256(raw).hexdigest()


def validate_manifest(manifest: object, *, expected_commit: str | None = None,
                      patch_root: str | Path | None = None) -> dict:
    """校验 Easel manifest 与可选的实际 patch 文件，返回规范化副本。"""
    data = _check_manifest_shape(manifest, expected_commit)
    if patch_root is not None:
        root = Path(patch_root)
        _check_patch(root / data["patch"], data["provider"]["models"])
    return data


def _node_version(binary: str) -> tuple[int, int, int]:
    result = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, check=False,
        timeout=30)
    if result.returncode != 0:
        _fail("node --version failed")
    match = _NODE_OUTPUT.fullmatch(result.stdout.strip())
    if match is None:
        _fail("node --version returned an unexpected value")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _supported_node(version: tuple[int, int, int]) -> bool:
    major, minor, _patch = version
    return major >= 24 or (major == 22 and minor >= 19)


def verify_installation(manifest: Mapping[str, Any], *, dsh_root: str | Path,
                        managed_root: str | Path,
                        node_binary: str = "node") -> dict:
    """核对实际 checkout、CLI 产物、Node 版本和 Linux 原生 flock。"""
    dsh = Path(dsh_root)
    managed = Path(managed_root)
    harness = _mapping(manifest["harness"], "harness")
    commit = _text(harness["commit"], "harness.commit")

    try:
        head = (dsh / ".git" / "HEAD").read_text(encoding="ascii").strip()
    except OSError:
        _fail("fixed Harness checkout is missing")
    if head != commit:
        _fail("fixed Harness checkout HEAD does not match the manifest")

    cli = dsh / "apps" / "cli" / "lib" / "bin.js"
    if not cli.is_file() or cli.stat().st_size == 0:
        _fail("built ACP CLI artifact is missing")
    try:
        package = json.loads(
            (dsh / "apps" / "cli" / "package.json").read_text("utf-8"))
    except (OSError, ValueError):
        _fail("built ACP CLI manifest is unreadable")
    if package.get("version") != HARNESS_VERSION:
        _fail("built ACP CLI version does not match the manifest")
    if not (managed / PATCH_NAME).is_file():
        _fail("managed ACP patch is missing from the image")

    version = _node_version(node_binary)
    if not _supported_node(version):
        _fail("Node runtime does not satisfy the Harness engine range")

    with tempfile.TemporaryDirectory(prefix="easel-flock-") as directory:
        lock_path = Path(directory) / "lock"
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": directory,
            "EASEL_FLOCK_SMOKE": str(lock_path),
        }
        result = subprocess.run(
            [node_binary, "--input-type=module", "-e", _NATIVE_PROBE],
            cwd=dsh, env=env, capture_output=True, text=True,
            check=False, timeout=60)
    if result.returncode != 0 or "flock-ok" not in result.stdout:
        _fail("Linux native flock addon did not load")
    return {
        "commit": commit,
        "node": ".".join(str(part) for part in version),
        "patch_sha256": hashlib.sha256(
            (managed / PATCH_NAME).read_bytes()).hexdigest(),
    }


def _load(path: Path) -> object:
    try:
        raw = path.read_bytes()
    except OSError:
        _fail("managed manifest is unreadable")
    if len(raw) > MAX_MANIFEST_BYTES:
        _fail("managed manifest is too large")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        _fail("managed manifest must be a JSON object")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="easel-verify-managed-profile")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--dsh-root", default="/opt/dsh")
    parser.add_argument("--managed-root", default="/opt/easel/managed")
    parser.add_argument("--expected-commit")
    parser.add_argument("--check-installation", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = Path(args.manifest)
        manifest = validate_manifest(
            _load(path), expected_commit=args.expected_commit,
            patch_root=path.parent)
        details = None
        if args.check_installation:
            details = verify_installation(
                manifest, dsh_root=args.dsh_root,
                managed_root=args.managed_root)
    except ProfileError as error:
        print(f"managed_profile_invalid: {error}", file=sys.stderr)
        return 1
    suffix = ""
    if details is not None:
        suffix = (f" commit={details['commit']} node={details['node']}"
                  f" patch_sha256={details['patch_sha256']}")
    print(f"managed_profile_ok=1{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
