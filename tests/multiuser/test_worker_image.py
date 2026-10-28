"""Task 8 R1：最终 worker 镜像必须包含实际执行代码、依赖与构建期冒烟。"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy" / "multiuser" / "Dockerfile.worker"
REQUIREMENTS = ROOT / "deploy" / "multiuser" / "requirements-worker.txt"
SMOKE = ROOT / "deploy" / "multiuser" / "smoke_worker.py"
DOCKERIGNORE = ROOT / ".dockerignore"


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_final_image_contains_worker_execution_code_and_skills():
    text = _text(DOCKERFILE)
    assert "FROM chromium AS execution" in text
    assert "FROM execution AS runtime" in text
    assert "COPY web ./web" in text
    assert "COPY skills ./skills" in text
    assert "COPY agents ./agents" in text
    assert "COPY deploy/multiuser/worker_sidecar.py" in text
    assert "COPY deploy/multiuser/egress_proxy.py" in text
    assert "COPY deploy/multiuser/managed /opt/easel/managed" in text
    assert "requirements-worker.txt" in text
    assert "smoke_worker.py --check-files" in text
    assert "smoke_worker.py --check-files --require-dsh" in text
    executable_text = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#"))
    assert "--no-sandbox" not in executable_text
    assert "chmod -R a+w" not in executable_text


def test_worker_requirements_are_pinned_and_cover_runtime_imports():
    lines = [
        line.strip() for line in _text(REQUIREMENTS).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "必须显式固定 worker 运行依赖"
    assert all("==" in line for line in lines), lines
    names = {line.split("==", 1)[0].lower() for line in lines}
    for required in (
            "sse-starlette", "python-multipart", "cryptography", "requests",
            "beautifulsoup4", "websocket-client", "segno", "markdown",
            "pillow", "numpy", "pandas", "matplotlib", "opencv-python-headless",
            "jieba"):
        assert required in names, f"缺少受管依赖：{required}"


def test_build_smoke_checks_files_modules_worker_mode_and_fake_harness():
    text = _text(SMOKE)
    assert "REQUIRED_FILES" in text
    assert "web/app.py" in text
    assert "agents/workspace/AGENTS.md" in text
    assert "worker_sidecar.py" in text
    assert "egress_proxy.py" in text
    assert "skills/shared/scripts/runtime_paths.py" in text
    assert "REQUIRED_MODULES" in text
    assert "chromium_launch_args" in text
    assert "chromium_sandbox_options" in text
    assert "sign_worker_snapshot" in text
    assert "load_worker_snapshot" in text
    assert "ACPAdapter" in text
    assert "session/prompt" in text
    assert "FORBIDDEN_FILE_NAMES" in text
    assert "镜像包含开发者私有文件" in text
    assert "FAKE_ACP_FLAG" in text
    assert "--require-dsh" in text
    assert "acp-profile.json" in text


def test_runtime_build_pins_node_pnpm_and_actually_builds_harness():
    text = _text(DOCKERFILE)
    assert "FROM ${NODE_IMAGE} AS dsh-build" in text
    assert "ARG NODE_IMAGE=node@sha256:" in text
    assert "ARG PNPM_VERSION=11.28.5" in text
    assert 'corepack prepare "pnpm@${PNPM_VERSION}" --activate' in text
    assert "pnpm install --frozen-lockfile" in text
    assert "pnpm run build:native-system" in text
    assert "./node_modules/typescript/bin/tsc -b tsconfig.host.json" in text
    assert ("pnpm exec tsdown --config-loader native "
            "--env.DSH_BUILD_FACE host") in text
    assert "test -f apps/cli/lib/bin.js" in text
    assert "COPY --from=dsh-build /opt/dsh /opt/dsh" in text
    assert ("COPY --from=dsh-build /usr/local/bin/node "
            "/usr/local/bin/node") in text
    assert 'DSH_COMMIT="${DSH_COMMIT}"' in text
    executable_text = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#"))
    # 运行阶段不得再在线安装 Node/npm，也不得只装 prod 依赖而不构建。
    assert "nodejs npm" not in executable_text
    assert "npm ci --omit=dev" not in executable_text
    assert "npm install --omit=dev" not in executable_text
    assert "pnpm install --frozen-lockfile --prod" not in executable_text


def test_require_dsh_runs_real_acp_profile_and_native_runtime():
    smoke = _text(SMOKE)
    for marker in (
            "verify_managed_profile.validate_manifest",
            "verify_managed_profile.verify_installation",
            "smoke_harness.run_smoke",
            "acp-profile.json"):
        assert marker in smoke
    verifier = _text(ROOT / "deploy" / "multiuser"
                     / "verify_managed_profile.py")
    assert "apps/cli/lib/bin.js" in verifier
    assert "flock" in verifier
    assert "native/system/packages/entry/lib/flock.js" in verifier
    harness = _text(ROOT / "deploy" / "multiuser" / "smoke_harness.py")
    for marker in (
            "initialize", "session/new", "sessionCapabilities",
            "easel-broker", "deepseek-chat", "session/close"):
        assert marker in harness


def test_build_context_excludes_skill_secrets_tests_and_caches():
    lines = {line.strip() for line in _text(DOCKERIGNORE).splitlines()}
    for pattern in (
            "skills/**/tests/",
            "skills/**/.github/",
            "skills/**/wechat-publisher.yaml",
            "skills/**/cookies.json",
            "skills/**/*.token",
            "skills/**/.cache/",
            "skills/**/profiles/",
            "**/.env",
            "**/.env.*"):
        assert pattern in lines, f"构建上下文未排除：{pattern}"
    # 技能运行代码本身必须保留，不能因排除规则被整体丢弃。
    assert "skills/" not in lines
    assert "web/" not in lines
