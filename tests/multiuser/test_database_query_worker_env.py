from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from easel.control.types import WorkspacePaths
from easel.worker.config import WorkerConfigSnapshot, build_run_env

ROOT = Path(__file__).resolve().parents[2]
CLI = (ROOT / "skills/builtin/database-query/scripts/db_query.py")
SECRET = "mysql+asyncmy://readonly:dsn-sentinel@db-tunnel:13300/sales"


def test_worker_env_drops_dsn_but_env_file_doctor_passes(tmp_path):
    project_docs = tmp_path / "project-docs"
    (project_docs / "references").mkdir(parents=True)
    (project_docs / "references/a.md").write_text(
        "销售业务说明", encoding="utf-8")
    (project_docs / "references/projects.yaml").write_text(
        "version: 1\nprojects:\n"
        "  - id: sales\n    company: demo\n    name: demo\n"
        "    aliases: []\n    document: references/a.md\n"
        "    connection_id: sales_readonly\n",
        encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "version: 1\n"
        f"project_docs_root: {project_docs}\n"
        f"state_dir: {tmp_path / 'state'}\n"
        f"output_dir: {tmp_path / 'outputs'}\n"
        "sources:\n"
        "  - id: sales_readonly\n"
        "    secret_env: SALES_READONLY_DATABASE_URL\n"
        "    allowed_schema: sales\n"
        "    allowed_tables: [orders]\n"
        "    readonly_account_verified: true\n"
        "    enabled: true\n",
        encoding="utf-8")
    env_file = tmp_path / "database-query.env"
    env_file.write_text(
        f"SALES_READONLY_DATABASE_URL={SECRET}\n", encoding="utf-8")
    # 不支持的方言用于证明 --env-file 中的 DSN 确实被技能读取并解析；
    # doctor --connect 走到 DSN 解析即返回固定错误码，不会建立真实连接。
    probe_env = tmp_path / "probe.env"
    probe_env.write_text(
        "SALES_READONLY_DATABASE_URL="
        "postgresql://readonly:file-sentinel@db-tunnel:13300/sales\n",
        encoding="utf-8")

    base = dict(os.environ)
    base["SALES_READONLY_DATABASE_URL"] = SECRET
    paths = WorkspacePaths(
        code_root=(tmp_path / "code").resolve(),
        workspace_root=(tmp_path / "workspace").resolve(),
        state_root=(tmp_path / "state-root").resolve(),
        home=(tmp_path / "home").resolve())
    env = build_run_env(
        paths,
        WorkerConfigSnapshot("workspace-a", "run-a", 1, {}, {"runtime": "acp"}),
        base)
    assert "SALES_READONLY_DATABASE_URL" not in env
    assert SECRET not in env.values()

    # 加 -B 避免在只读技能目录旁写出 __pycache__；build_run_env 的
    # allowlist 不含 PYTHONDONTWRITEBYTECODE，不能依赖环境变量抑制字节码。
    result = subprocess.run(
        [sys.executable, "-B", str(CLI), "--config", str(config),
         "--env-file", str(env_file), "doctor"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    assert '"ok": true' in result.stdout
    assert "dsn-sentinel" not in result.stdout
    assert "dsn-sentinel" not in result.stderr

    # 不受支持的方言只有真正读取并解析 env 文件后才会出现；
    # 该错误发生在建连前，因此无需可用数据库。
    probe = subprocess.run(
        [sys.executable, "-B", str(CLI), "--config", str(config),
         "--env-file", str(probe_env), "doctor", "--connect",
         "--project", "sales"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    probe_output = probe.stdout + probe.stderr
    assert "database.unsupported_dialect" in probe_output, probe_output
    assert "file-sentinel" not in probe_output

    # 去掉 --env-file 后同一命令应报告凭据不可用，证明 DSN 来自该文件，
    # 而不是 worker 进程环境。
    missing = subprocess.run(
        [sys.executable, "-B", str(CLI), "--config", str(config),
         "doctor", "--connect", "--project", "sales"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
    missing_output = missing.stdout + missing.stderr
    assert "database.source_unavailable" in missing_output, missing_output
