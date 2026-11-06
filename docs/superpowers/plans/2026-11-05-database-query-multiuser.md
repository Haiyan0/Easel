# database-query 多人 Docker 凭据与连接通路实施计划

执行状态（2026-11-06）：主体实现已入库，本次整理提交补齐 Windows Desktop 接线。下文复选框保留原始逐步计划，不作为当前验收记录；本轮仅按实际测试结果记录验证，不追认原计划所有步骤均已执行。Linux 生产 SSH 与真实外部数据库连通性未在本轮重测。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让多人 Docker 的 worker 通过每 workspace sidecar 的固定转发访问公网直连和 SSH 跳板两类 MySQL；DSN 与私钥只从宿主全局 secrets 只读挂载进入，且未启用时现有行为逐项不变。

**Architecture:** 非秘密拓扑随技能镜像发布（`config.multiuser.yaml` + `tunnel.multiuser.yaml`）；provisioner 用唯一开关 `--database-query-secrets` 给 worker 挂 DSN env 文件、给 sidecar 挂 keys 目录并加 `db-tunnel` 别名；sidecar 内的 `db_tunnel.py` 按拓扑启动 direct TCP 转发或常驻 `ssh -N -L`，worker 只连 `db-tunnel:<端口>`。

**Tech Stack:** Python 3.11 / asyncio / PyYAML / Docker CLI / OpenSSH client / pytest。

**Spec:** `docs/superpowers/specs/2026-11-04-database-query-multiuser-design.md`

## Global Constraints

- 唯一启用开关是 `--database-query-secrets <宿主绝对目录>`；未提供时 argv、挂载数、inspect 验收与现状逐项一致（worker 3、sidecar 1、无别名）。
- 不修改 `easel/worker/config.py` 的 `_ENV_ALLOWLIST`，不把 DSN 放进 `--env` 或 worker `--env-file`；DSN 只通过只读 bind mount 到 `/run/easel-secrets/database-query.env`。
- 不修改 `worker_wiring_env` 的 `NO_PROXY`，不修改 `verify_worker_wiring`。
- 容器内固定路径：`/opt/easel/skills/builtin/database-query/assets/config.multiuser.yaml`、`.../tunnel.multiuser.yaml`、`/run/easel-secrets/database-query.env`、`/run/easel-secrets/keys`；sidecar 别名 `db-tunnel`。
- 隧道监听端口只能在 `13300–13399`；direct 并发上限 64、连接超时 10 秒；SSH 退避 1/2/4/8/16/30 秒，稳定 120 秒后重置；停止时先 SIGTERM，5 秒后 SIGKILL。
- SSH 只能用 `/usr/bin/ssh`、无口令公钥、固定 `known_hosts`；禁止 `StrictHostKeyChecking=no`、密码/键盘交互、远程命令、agent 转发。
- 仓库只提交 `tunnel.multiuser.example.yaml`；真实 `tunnel.multiuser.yaml` 被 `.gitignore` 排除，但必须进入 Docker 构建上下文。
- 日志固定 `db-tunnel source=<id> mode=<direct|ssh> state=<starting|ready|failed|stopped> reason=<code>`；不记录 DSN、口令、私钥内容、私钥路径或 ssh stderr 原文。
- 代码注释与文档用简体中文，标识符英文；提交信息用简体中文。
- Python 一律用 `C:\Users\hyh\anaconda3\envs\Agent\python.exe`（下称 `python`），测试命令在仓库根执行。

## Review Focus

1. **占位拓扑未替换**：管理员忘记把 example 复制成真实 `tunnel.multiuser.yaml`，或真实文件被 `.dockerignore` 排除。预期 sidecar 记录 `state=failed reason=config_invalid`，不启动任何 forward，但 broker/egress 仍健康；绝不能连接 `.example.com` 或让 sidecar 崩溃。测试：Task 2 的保留域名拒绝、Task 5 的缺配置仍可服务。
2. **旧形状容器迁移**：启用或停用 secrets 时旧 worker 仍在运行。预期 provisioner 以 `container_conflict` 失败关闭、保留容器交人工 drain+stop；sidecar 可自动删除重建。测试：Task 6/7 的迁移用例。
3. **secrets 缺失或权限错误**：secrets 目录缺失、`database-query.env` 是目录/符号链接、`keys/` 缺失，或私钥不可读/带口令。预期 provisioner 启动即 `database_query_secrets_invalid`，sidecar 记录 `key_unreadable`；不得回退到进程环境、宽松挂载或交互式认证。测试：Task 6 的参数校验、Task 4 的错误码映射。
4. **SSH 主机身份与认证失败**：host key 不匹配、私钥权限错误、认证失败。预期分别记录 `host_key_mismatch`/`key_unreadable`/`auth_failed` 并退避重启，不回退到 accept-new 或密码；日志不出现 key 路径或 stderr 原文。测试：Task 4 的参数与监督用例。
5. **单 forward 故障不拖垮整条链路**：一个 direct/SSH 目标不可达、连接数超限或 ssh 进程反复退出时，broker/egress 与其他 forward 必须继续工作，worker 侧失败应快速关闭而非挂起。测试：Task 3 的超限/拒绝连接、Task 4 的退避、Task 5 的非法配置隔离。

---

### Task 1: 多人技能配置与忽略规则

**Files:**
- Create: `skills/builtin/database-query/assets/config.multiuser.yaml`
- Modify: `.gitignore`
- Modify: `.dockerignore`
- Modify: `skills/builtin/database-query/.gitignore`
- Test: `tests/multiuser/test_database_query_multiuser_config.py`
- Test: `tests/test_added_skills.py`

**Interfaces:**
- Produces: `config.multiuser.yaml`，固定 3 个 source（`yuanmeng_mall_tracking_readonly`、`yuanmeng_space_readonly`、`collectibles_readonly`），供 Task 2、5、8 使用。
- Produces: 忽略规则——真实 `assets/tunnel.multiuser.yaml` 被忽略，`assets/tunnel.multiuser.example.yaml` 与 `assets/config.multiuser.yaml` 可提交。

- [ ] **Step 1: 写失败测试 `tests/multiuser/test_database_query_multiuser_config.py`**

```python
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "builtin" / "database-query"
CONFIG = SKILL / "assets" / "config.multiuser.yaml"
SCRIPTS = SKILL / "scripts"

EXPECTED_IDS = {
    "yuanmeng_mall_tracking_readonly",
    "yuanmeng_space_readonly",
    "collectibles_readonly",
}


def _app_config():
    sys.path.insert(0, str(SCRIPTS))
    try:
        from dbquery.config import AppConfig
    finally:
        sys.path.remove(str(SCRIPTS))
    return AppConfig


def test_multiuser_config_is_valid_and_enables_three_sources():
    config = _app_config().load(CONFIG)
    assert {source.id for source in config.sources} == EXPECTED_IDS
    assert all(source.enabled for source in config.sources)
    assert all(source.readonly_account_verified for source in config.sources)


def test_multiuser_sources_cover_all_project_doc_bindings():
    catalogue = yaml.safe_load(
        (ROOT / "skills/builtin/project-docs/references/projects.yaml")
        .read_text(encoding="utf-8"))
    connection_ids = {
        project["connection_id"] for project in catalogue["projects"]
        if project.get("connection_id")
    }
    config = _app_config().load(CONFIG)
    assert connection_ids == EXPECTED_IDS
    assert connection_ids <= {source.id for source in config.sources}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/multiuser/test_database_query_multiuser_config.py -q`
Expected: FAIL，提示 `config.multiuser.yaml` 不存在。

- [ ] **Step 3: 在 `tests/test_added_skills.py` 增加忽略规则测试**

```python
def test_database_query_multiuser_tunnel_is_gitignored_but_config_is_tracked():
    assert subprocess.run(
        ["git", "check-ignore", "-q",
         "skills/builtin/database-query/assets/tunnel.multiuser.yaml"],
        cwd=PROJECT_ROOT, capture_output=True).returncode == 0
    for tracked in (
        "skills/builtin/database-query/assets/config.multiuser.yaml",
        "skills/builtin/database-query/assets/tunnel.multiuser.example.yaml",
    ):
        assert subprocess.run(
            ["git", "check-ignore", "-q", tracked],
            cwd=PROJECT_ROOT, capture_output=True).returncode == 1


def test_build_context_excludes_skill_runtime_state():
    lines = {line.strip() for line in
             (PROJECT_ROOT / ".dockerignore").read_text(
                 encoding="utf-8").splitlines()}
    for pattern in (
        "skills/**/config.yaml", "skills/**/config/database-query.yaml",
        "skills/**/state/", "skills/**/outputs/", "skills/**/.secrets/",
        "skills/**/.env", "新增的skill/", "skill-test/",
    ):
        assert pattern in lines, f"构建上下文未排除：{pattern}"
    for allowed in (
        "assets/config.multiuser.yaml", "assets/tunnel.multiuser.yaml",
        "deploy/multiuser/db_tunnel.py",
    ):
        assert allowed not in lines
```

- [ ] **Step 4: 运行测试确认失败**

Run: `python -m pytest tests/test_added_skills.py -q`
Expected: FAIL，缺少忽略规则。

- [ ] **Step 5: 创建 `skills/builtin/database-query/assets/config.multiuser.yaml`**

逐字使用 spec §5.1 的配置：`version: 1`；`project_docs_root: /opt/easel/skills/builtin/project-docs`；状态/结果目录为 `/workspace/outputs/_analytics/database-query/{state,results}`；3 个 source 的 `secret_env` 分别为 `YUANMENG_MALL_TRACKING_DATABASE_URL`、`YUANMENG_SPACE_DATABASE_URL`、`COLLECTIBLES_DATABASE_URL`；schema 分别为 `ymkj2.0`、`meta_new_sync`、`szcp_sync`；表白名单逐字使用 spec（tracking 仅 `track_event`；space 14 张；collectibles 3 张）；全部 `readonly_account_verified: true`、`enabled: true`。

- [ ] **Step 6: 补齐忽略规则**

根 `.gitignore` 末尾增加：

```text
# 原始技能副本与构建隔离 checkout 不入库
新增的skill/
.tmp-dsh-iso/
```

`skills/builtin/database-query/.gitignore` 增加：

```text
assets/tunnel.multiuser.yaml
```

`.dockerignore` 在技能排除段增加：

```text
skills/**/config.yaml
skills/**/config/database-query.yaml
skills/**/state/
skills/**/outputs/
skills/**/.secrets/
skills/**/.env
新增的skill/
skill-test/
```

注意：不要排除 `assets/config.multiuser.yaml` 或 `assets/tunnel.multiuser.yaml`；`.tmp-dsh-iso/` 已存在，不重复添加。

- [ ] **Step 7: 运行测试确认通过**

Run: `python -m pytest tests/multiuser/test_database_query_multiuser_config.py tests/test_added_skills.py -q`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add skills/builtin/database-query/assets/config.multiuser.yaml \
  skills/builtin/database-query/.gitignore .gitignore .dockerignore \
  tests/multiuser/test_database_query_multiuser_config.py tests/test_added_skills.py
git commit -m "feat: 增加 database-query 多人 Docker 配置与忽略规则"
```

---

### Task 2: 隧道拓扑解析与 example 模板

**Files:**
- Create: `deploy/multiuser/db_tunnel.py`
- Create: `skills/builtin/database-query/assets/tunnel.multiuser.example.yaml`
- Test: `tests/multiuser/test_db_tunnel.py`
- Modify: `tests/multiuser/test_database_query_multiuser_config.py`

**Interfaces:**
- Produces（`deploy/multiuser/db_tunnel.py`）：

```python
DB_TUNNEL_CONFIG = Path(
    "/opt/easel/skills/builtin/database-query/assets/tunnel.multiuser.yaml")
DB_TUNNEL_KEYS = Path("/run/easel-secrets/keys")
DB_TUNNEL_ALIAS = "db-tunnel"


class TunnelConfigError(Exception):
    code = "config_invalid"


@dataclass(frozen=True)
class DatabaseTarget:
    host: str
    port: int


@dataclass(frozen=True)
class SshTarget:
    host: str
    port: int
    user: str
    private_key: Path
    known_hosts: Path


@dataclass(frozen=True)
class Forward:
    source: str
    mode: str
    listen_port: int
    database: DatabaseTarget
    ssh: SshTarget | None


def load_forwards(
    path: str | Path, *,
    keys_dir: str | Path | None = None,
    allow_placeholders: bool = False,
) -> tuple[Forward, ...]:
    """解析并严格校验拓扑；非法一律抛 TunnelConfigError。"""
```

- 后续 Task 3/4/5 在同一个模块追加转发与监督实现。

- [ ] **Step 1: 写失败测试 `tests/multiuser/test_db_tunnel.py`（校验部分）**

```python
from __future__ import annotations

import copy
import importlib.util
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "deploy" / "multiuser" / "db_tunnel.py"
EXAMPLE = (ROOT / "skills/builtin/database-query/assets"
           / "tunnel.multiuser.example.yaml")


def _load():
    spec = importlib.util.spec_from_file_location(
        "easel_db_tunnel", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


db_tunnel = _load()
BASE = {
    "version": 1,
    "forwards": [{
        "source": "sales_readonly",
        "mode": "direct",
        "listen_port": 13300,
        "database": {"host": "db.internal", "port": 3306},
    }],
}
SSH = {
    "host": "jump.internal", "port": 22, "user": "easel-tunnel",
    "private_key": "id_ed25519", "known_hosts": "known_hosts",
}


def _write(tmp_path, payload) -> pathlib.Path:
    path = tmp_path / "tunnel.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_example_is_valid_with_placeholders():
    forwards = db_tunnel.load_forwards(EXAMPLE, allow_placeholders=True)
    assert [(f.source, f.mode, f.listen_port) for f in forwards] == [
        ("yuanmeng_mall_tracking_readonly", "ssh", 13306),
        ("yuanmeng_space_readonly", "direct", 13307),
        ("collectibles_readonly", "direct", 13308),
    ]
    assert forwards[0].ssh is not None
    assert forwards[0].ssh.private_key.name == "database-query_ed25519"
    assert forwards[1].ssh is None


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(version=2),
    lambda p: p.update(version=True),
    lambda p: p.update(extra=True),
    lambda p: p.update(forwards=[]),
    lambda p: p["forwards"].append(copy.deepcopy(p["forwards"][0])),
    lambda p: p["forwards"][0].update(mode="tunnel"),
    lambda p: p["forwards"][0].update(listen_port=13299),
    lambda p: p["forwards"][0].update(listen_port=13400),
    lambda p: p["forwards"][0].update(listen_port=13300, password="x"),
    lambda p: p["forwards"][0]["database"].update(port=0),
    lambda p: p["forwards"][0].update(
        ssh={"host": "jump.internal", "port": 22, "user": "u",
             "private_key": "k", "known_hosts": "known_hosts"}),
])
def test_invalid_config_is_rejected(tmp_path, mutate):
    payload = copy.deepcopy(BASE)
    mutate(payload)
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(
            _write(tmp_path, payload), keys_dir=tmp_path / "keys",
            allow_placeholders=True)


def test_ssh_requires_ssh_section_and_rejects_at_in_user(tmp_path):
    payload = copy.deepcopy(BASE)
    payload["forwards"][0]["mode"] = "ssh"
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(_write(tmp_path, payload))
    payload["forwards"][0]["ssh"] = {**SSH, "user": "a@b"}
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(_write(tmp_path, payload))


@pytest.mark.parametrize("host", [
    "db.example.com", "foo.example", "x.invalid", "x.test", "localhost",
    "LOCALHOST",
])
def test_placeholder_hosts_are_rejected_in_strict_mode(tmp_path, host):
    payload = copy.deepcopy(BASE)
    payload["forwards"][0]["database"]["host"] = host
    keys = tmp_path / "keys"
    keys.mkdir()
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(
            _write(tmp_path, payload), keys_dir=keys)


@pytest.mark.parametrize("name", [
    "/etc/private_key", "../private_key", "sub/../key", "", "sub/key",
])
def test_ssh_key_paths_must_stay_directly_in_keys_dir(tmp_path, name):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "id_ed25519").write_text("key", encoding="utf-8")
    (keys / "known_hosts").write_text("", encoding="utf-8")
    payload = copy.deepcopy(BASE)
    payload["forwards"][0]["mode"] = "ssh"
    payload["forwards"][0]["ssh"] = {**SSH, "private_key": name}
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(_write(tmp_path, payload), keys_dir=keys)


def test_ssh_keys_must_exist_be_regular_and_not_symlinks(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "id_ed25519").write_text("key", encoding="utf-8")
    (keys / "known_hosts").write_text("", encoding="utf-8")
    payload = copy.deepcopy(BASE)
    payload["forwards"][0]["mode"] = "ssh"
    payload["forwards"][0]["ssh"] = SSH
    path = _write(tmp_path, payload)
    forwards = db_tunnel.load_forwards(path, keys_dir=keys)
    assert forwards[0].ssh.private_key == (keys / "id_ed25519").resolve()

    (keys / "id_ed25519").unlink()
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(path, keys_dir=keys)

    target = tmp_path / "outside_key"
    target.write_text("key", encoding="utf-8")
    try:
        (keys / "id_ed25519").symlink_to(target)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(db_tunnel.TunnelConfigError):
        db_tunnel.load_forwards(path, keys_dir=keys)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/multiuser/test_db_tunnel.py -q`
Expected: FAIL，`db_tunnel.py` 或 example 不存在。

- [ ] **Step 3: 实现 `deploy/multiuser/db_tunnel.py` 的解析与校验**

用标准库 dataclass + 手写校验，不引入 pydantic。实现要点：

- 用 `yaml.safe_load` 读取，并使用拒绝重复键的 `SafeLoader` 子类；文件不可读（`OSError`）、YAML 异常、根不是字典、`set(raw) != {"version", "forwards"}` 均抛 `TunnelConfigError`。
- `version` 必须是 `type(value) is int and value == 1`，避免 `True == 1`。
- `forwards` 必须是非空 list。
- 每个 forward：必填键 `source/mode/listen_port/database`；`mode == "ssh"` 必填 `ssh`；`mode == "direct"` 出现 `ssh` 即拒绝；其他未知键拒绝。
- `source` 匹配 `^[a-z][a-z0-9_-]{2,63}$`，且不重复。
- `listen_port` 必须是 `type(value) is int`（避免 `True`）且 `13300 <= p <= 13399`；重复拒绝。
- `database` 键必须恰为 `host/port`；host 匹配 `^[A-Za-z0-9._-]{1,253}$`；port 为 int 且 1–65535。
- `ssh` 键必须恰为 `host/port/user/private_key/known_hosts`；host 规则同上；port 1–65535；user 匹配 `^[A-Za-z0-9._-]{1,64}$`；`private_key`/`known_hosts` 必须是不含 `/` 或 `\\` 的裸文件名（不得为绝对路径、`.`、`..` 或空串）。
- 严格模式（`allow_placeholders=False`）：host 等于 `example.com`、或以 `.example.com` 结尾，或以 `.example`/`.invalid`/`.test` 结尾，或为 `localhost`（大小写不敏感）时拒绝。模板测试传 `allow_placeholders=True` 跳过该检查。
- 严格模式：`keys_dir` 默认 `DB_TUNNEL_KEYS`；候选路径先查 `is_symlink()`，再 `resolve(strict=True)`，必须是普通文件且 `is_relative_to(keys_dir.resolve(strict=True))`。模板模式跳过存在性/符号链接检查，但仍做相对路径检查。
- 所有失败只抛 `TunnelConfigError()`，消息固定为 `config_invalid`，不拼接路径或值。

- [ ] **Step 4: 创建 `assets/tunnel.multiuser.example.yaml`**

逐字使用 spec §5.1 的模板：3 个 forward，端口 13306/13307/13308；`yuanmeng_mall_tracking_readonly` 为 `mode: ssh`（`db-internal.example.com`、`jump.example.com`、`easel-tunnel`、`database-query_ed25519`、`known_hosts`），其余两个为 `direct`（`db-public.example.com:3306`）。

- [ ] **Step 5: 运行校验测试确认通过**

Run: `python -m pytest tests/multiuser/test_db_tunnel.py -q`
Expected: PASS。

- [ ] **Step 6: 在 `tests/multiuser/test_database_query_multiuser_config.py` 增加两份配置的一致性测试**

```python
import importlib.util
from collections import Counter

EXAMPLE = SKILL / "assets" / "tunnel.multiuser.example.yaml"


def _db_tunnel():
    spec = importlib.util.spec_from_file_location(
        "db_tunnel_for_config_test",
        ROOT / "deploy" / "multiuser" / "db_tunnel.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_enabled_sources_have_exactly_one_matching_forward():
    config = _app_config().load(CONFIG)
    forwards = _db_tunnel().load_forwards(EXAMPLE, allow_placeholders=True)
    enabled = {source.id for source in config.sources if source.enabled}
    counts = Counter(forward.source for forward in forwards)
    assert set(counts) == enabled
    assert all(count == 1 for count in counts.values())
    assert len({forward.listen_port for forward in forwards}) == len(forwards)
    assert all("PRIVATE KEY" not in path.read_text(encoding="utf-8")
               for path in (CONFIG, EXAMPLE))
    assert all("mysql+asyncmy://" not in path.read_text(encoding="utf-8")
               for path in (CONFIG, EXAMPLE))
```

- [ ] **Step 7: 运行一致性测试确认通过**

Run: `python -m pytest tests/multiuser/test_database_query_multiuser_config.py tests/multiuser/test_db_tunnel.py -q`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add deploy/multiuser/db_tunnel.py \
  skills/builtin/database-query/assets/tunnel.multiuser.example.yaml \
  tests/multiuser/test_db_tunnel.py \
  tests/multiuser/test_database_query_multiuser_config.py
git commit -m "feat: 增加数据库隧道拓扑解析与示例模板"
```

---

### Task 3: direct TCP 转发

**Files:**
- Modify: `deploy/multiuser/db_tunnel.py`
- Test: `tests/multiuser/test_db_tunnel.py`

**Interfaces:**
- Produces：

```python
DIRECT_CONNECT_TIMEOUT = 10.0
DIRECT_MAX_CONNECTIONS = 64

def log_state(source: str, mode: str, state: str,
              reason: str = "") -> None:
    """固定格式写 stderr；source/mode 为空时省略对应字段。"""

async def serve_direct(
    forward: Forward, *,
    host: str = "0.0.0.0",
    connect_timeout: float = DIRECT_CONNECT_TIMEOUT,
    max_connections: int = DIRECT_MAX_CONNECTIONS,
    emit=log_state,
) -> asyncio.AbstractServer:
    """启动 direct 转发；绑定失败时记录 failed 并抛 OSError。"""
```

- [ ] **Step 1: 写失败测试（往返、并发、上游拒绝、日志）**

先在 `tests/multiuser/test_db_tunnel.py` 顶部补充 `import asyncio` 与 `import socket`，再加以下测试：

```python
async def _echo(reader, writer):
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


def _direct(listen_port, upstream_port):
    return db_tunnel.Forward(
        source="sales_readonly", mode="direct", listen_port=listen_port,
        database=db_tunnel.DatabaseTarget("127.0.0.1", upstream_port),
        ssh=None)


def test_direct_forward_pipes_bytes_both_ways():
    async def scenario():
        echo = await asyncio.start_server(_echo, "127.0.0.1", 0)
        upstream_port = echo.sockets[0].getsockname()[1]
        server = await db_tunnel.serve_direct(
            _direct(0, upstream_port), host="127.0.0.1")
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            payload = b"ping" * 4096
            writer.write(payload)
            await writer.drain()
            assert await reader.readexactly(len(payload)) == payload
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
            echo.close()
            await echo.wait_closed()
    asyncio.run(scenario())


def test_direct_forward_caps_concurrent_connections():
    async def scenario():
        echo = await asyncio.start_server(_echo, "127.0.0.1", 0)
        upstream_port = echo.sockets[0].getsockname()[1]
        server = await db_tunnel.serve_direct(
            _direct(0, upstream_port), host="127.0.0.1", max_connections=1)
        port = server.sockets[0].getsockname()[1]
        try:
            first_reader, first_writer = await asyncio.open_connection(
                "127.0.0.1", port)
            second_reader, second_writer = await asyncio.open_connection(
                "127.0.0.1", port)
            assert await asyncio.wait_for(second_reader.read(), 2) == b""
            first_writer.close()
            await first_writer.wait_closed()
            second_writer.close()
            await second_writer.wait_closed()
            await first_reader.read()
        finally:
            server.close()
            await server.wait_closed()
            echo.close()
            await echo.wait_closed()
    asyncio.run(scenario())


def test_direct_forward_closes_client_when_upstream_refuses():
    async def scenario():
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        refused_port = probe.getsockname()[1]
        probe.close()
        server = await db_tunnel.serve_direct(
            _direct(0, refused_port), host="127.0.0.1")
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            assert await asyncio.wait_for(reader.read(), 2) == b""
            writer.close()
            await writer.wait_closed()
            # 转发器仍存活，可接受下一次连接。
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
            assert await asyncio.wait_for(reader2.read(), 2) == b""
            writer2.close()
            await writer2.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_direct_forward_logs_starting_and_ready():
    events = []

    async def scenario():
        echo = await asyncio.start_server(_echo, "127.0.0.1", 0)
        upstream_port = echo.sockets[0].getsockname()[1]
        server = await db_tunnel.serve_direct(
            _direct(0, upstream_port), host="127.0.0.1",
            emit=lambda *args: events.append(args))
        server.close()
        await server.wait_closed()
        echo.close()
        await echo.wait_closed()
    asyncio.run(scenario())
    assert events == [
        ("sales_readonly", "direct", "starting", ""),
        ("sales_readonly", "direct", "ready", ""),
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/multiuser/test_db_tunnel.py -q -k direct`
Expected: FAIL，`serve_direct` 未定义。

- [ ] **Step 3: 实现 direct 转发**

- `log_state` 按固定格式写 `sys.stderr`，`flush=True`；`source`/`mode` 为空时省略字段。
- `_DirectForward` 保存 `Forward`、`host`、`connect_timeout`、`max_connections`、`emit` 与活动连接计数。
- 启动时 `emit(source, "direct", "starting", "")`；`asyncio.start_server` 成功后 `emit(source, "direct", "ready", "")`；`OSError` 时 `emit(source, "direct", "failed", "forward_failed")` 并重新抛出。`emit` 始终传 4 个位置参数。
- 每连接：若 `active >= max_connections`，立即关闭客户端；否则 `active += 1`，`asyncio.wait_for(open_connection(target), connect_timeout)` 失败则关闭客户端；成功则两个方向各跑一个 `_pipe`（`read(65536)` → `write` → `drain`，EOF 关闭目标 writer），`return_exceptions=True`，最后统一关闭两端并 `active -= 1`。
- 不在日志中记录 DSN；host/port 也不主动记录（避免实现者误加）。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/multiuser/test_db_tunnel.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add deploy/multiuser/db_tunnel.py tests/multiuser/test_db_tunnel.py
git commit -m "feat: 实现数据库直连 TCP 转发与并发上限"
```

---

### Task 4: SSH 参数、错误码与监督

**Files:**
- Modify: `deploy/multiuser/db_tunnel.py`
- Test: `tests/multiuser/test_db_tunnel.py`

**Interfaces:**
- Produces：

```python
SSH_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
SSH_STABLE_SECONDS = 120.0
SSH_STOP_TIMEOUT = 5.0

def build_ssh_argv(forward: Forward) -> list[str]:
    """返回固定顺序、固定选项的 ssh argv；forward.mode 必须是 ssh。"""

def classify_ssh_stderr(text: str) -> str:
    """把 ssh stderr 映射为固定错误码；不返回原文。"""

class SshForwardSupervisor:
    def __init__(self, forward: Forward, *, emit=log_state,
                 spawn=None, sleep=asyncio.sleep, clock=time.monotonic,
                 ready_probe=None, backoff: tuple[float, ...] = SSH_BACKOFF,
                 stable_after: float = SSH_STABLE_SECONDS,
                 stop_timeout: float = SSH_STOP_TIMEOUT) -> None: ...
    async def run(self) -> None: ...
    async def stop(self) -> None: ...
```

- `spawn(argv) -> Awaitable[Process]` 必须是 async 可等待对象；默认 `async def _spawn(argv)` 返回 `await asyncio.create_subprocess_exec(*argv, stdin=DEVNULL, stdout=DEVNULL, stderr=PIPE, env={"HOME": "/tmp", "LC_ALL": "C"})`。
- `ready_probe(port) -> bool` 默认每 0.1 秒尝试 `open_connection("127.0.0.1", port)`，最多 10 秒。

- [ ] **Step 1: 写失败测试（argv 逐项、错误码映射）**

```python
def _ssh_forward(tmp_path, port=13306):
    keys = tmp_path / "keys"
    keys.mkdir(exist_ok=True)
    key = keys / "database-query_ed25519"
    known = keys / "known_hosts"
    key.write_text("key", encoding="utf-8")
    known.write_text("", encoding="utf-8")
    return db_tunnel.Forward(
        source="tracking_readonly", mode="ssh", listen_port=port,
        database=db_tunnel.DatabaseTarget("10.0.0.10", 3306),
        ssh=db_tunnel.SshTarget(
            host="jump.internal", port=2222, user="easel-tunnel",
            private_key=key, known_hosts=known))


def test_ssh_argv_is_fixed_and_fail_closed(tmp_path):
    forward = _ssh_forward(tmp_path)
    argv = db_tunnel.build_ssh_argv(forward)
    assert argv[0] == "/usr/bin/ssh"
    for option in (
        "StrictHostKeyChecking=yes", "BatchMode=yes",
        "PreferredAuthentications=publickey", "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no", "IdentitiesOnly=yes",
        "IdentityAgent=none", "ForwardAgent=no", "ExitOnForwardFailure=yes",
        "ConnectTimeout=10", "ServerAliveInterval=30",
        "ServerAliveCountMax=3", "GlobalKnownHostsFile=/dev/null",
        "UpdateHostKeys=no", "ControlMaster=no", "ControlPath=none",
    ):
        assert ["-o", option] == argv[argv.index(option) - 1:
                                       argv.index(option) + 1]
    assert f"UserKnownHostsFile={forward.ssh.known_hosts}" in argv
    assert ["-i", str(forward.ssh.private_key)] == argv[argv.index("-i"):][:2]
    assert f"0.0.0.0:{forward.listen_port}:10.0.0.10:3306" == (
        argv[argv.index("-L") + 1])
    assert argv[-1] == "easel-tunnel@jump.internal"
    assert "-N" in argv and "-T" in argv and "-F" in argv
    assert "/dev/null" in argv
    assert "StrictHostKeyChecking=no" not in argv
    assert "accept-new" not in " ".join(argv)
    assert not any("/bin/sh" in part or "bash" in part for part in argv)


@pytest.mark.parametrize("text,reason", [
    ("Host key verification failed.", "host_key_mismatch"),
    ('Load key "/run/easel-secrets/keys/id": bad permissions',
     "key_unreadable"),
    ("Warning: Identity file /x not accessible", "key_unreadable"),
    ("Permission denied (publickey).", "auth_failed"),
    ("Could not request local forwarding.", "forward_failed"),
    ("cannot listen to port 13306", "forward_failed"),
    ("ssh: connect to host jump port 22: Connection refused",
     "connect_failed"),
    ("ssh: Could not resolve hostname jump", "connect_failed"),
    ("something unexpected", "connect_failed"),
])
def test_stderr_maps_to_fixed_reason(text, reason):
    assert db_tunnel.classify_ssh_stderr(text) == reason
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/multiuser/test_db_tunnel.py -q -k ssh`
Expected: FAIL，`build_ssh_argv` 未定义。

- [ ] **Step 3: 实现 SSH argv 与错误码映射**

- `build_ssh_argv` 严格按 spec §6.3 的顺序返回：`/usr/bin/ssh`、`-F /dev/null`、`-N`、`-T`、`-i <私钥>`、全部 `-o` 选项、`-p <ssh.port>`、`-L 0.0.0.0:<listen>:<db.host>:<db.port>`、`<user>@<host>`。
- `classify_ssh_stderr` 用固定子串表，按以下优先级返回：`host key verification failed` → `host_key_mismatch`；`load key`/`identity file`/`bad permissions` → `key_unreadable`；`permission denied` → `auth_failed`；`local forwarding`/`forwarding failed`/`cannot listen` → `forward_failed`；`connect to host`/`connection refused`/`timed out`/`could not resolve`/`network is unreachable` → `connect_failed`；其他 → `connect_failed`。全部小写比较。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/multiuser/test_db_tunnel.py -q -k ssh`
Expected: PASS。

- [ ] **Step 5: 写失败测试（监督：退避、稳定重置、停止、日志不泄露）**

测试用可注入的假进程：

```python
class FakeProcess:
    def __init__(self, stderr_text=b"", duration=0.0, ignore_term=False,
                 clock=None):
        self._stderr = _FakeStderr(stderr_text)
        self.returncode = None
        self._duration = duration
        self._ignore_term = ignore_term
        self._clock = clock if clock is not None else [0.0]
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()

    @property
    def stderr(self):
        return self._stderr

    async def wait(self):
        if self._ignore_term:
            await self._exited.wait()
            return -9
        # 只推进注入时钟，不让测试真的等待 duration 秒。
        self._clock[0] += self._duration
        await asyncio.sleep(0)
        self.returncode = 1
        return 1

    def terminate(self):
        self.terminated = True
        if not self._ignore_term:
            self.returncode = -15
            self._exited.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._exited.set()

    def release(self):
        self._exited.set()
```

`_FakeStderr.read(n)` 为 async，返回设置文本。

```python
def test_supervisor_backs_off_and_stops(tmp_path):
    async def scenario():
        sleeps = []
        logs = []
        argvs = []
        forward = _ssh_forward(tmp_path)

        async def spawn(argv):
            argvs.append(argv)
            return FakeProcess(b"Permission denied (publickey).", 0)

        supervisor = db_tunnel.SshForwardSupervisor(
            forward,
            spawn=spawn,
            sleep=lambda delay: sleeps.append(delay) or asyncio.sleep(0),
            ready_probe=lambda _port: asyncio.sleep(0, result=False),
            emit=lambda *args: logs.append(args))
        task = asyncio.create_task(supervisor.run())
        for _ in range(100):
            if len(sleeps) >= 4:
                break
            await asyncio.sleep(0.01)
        await supervisor.stop()
        await asyncio.wait_for(task, 2)
        assert argvs[0] == db_tunnel.build_ssh_argv(forward)
        assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0]
        assert ("tracking_readonly", "ssh", "failed", "auth_failed") in logs
        assert ("tracking_readonly", "ssh", "stopped", "") in logs
        assert all("Permission denied" not in str(entry) for entry in logs)
    asyncio.run(scenario())


def test_supervisor_resets_backoff_after_stable_run(tmp_path):
    async def scenario():
        sleeps = []
        clock = [0.0]
        durations = [0.0, 200.0, 10.0] + [0.0] * 100

        async def spawn(_argv):
            return FakeProcess(b"", durations.pop(0), clock=clock)

        async def fake_sleep(delay):
            sleeps.append(delay)
            clock[0] += delay

        supervisor = db_tunnel.SshForwardSupervisor(
            _ssh_forward(tmp_path), spawn=spawn, sleep=fake_sleep,
            clock=lambda: clock[0],
            ready_probe=lambda _p: asyncio.sleep(0, result=False))
        task = asyncio.create_task(supervisor.run())
        for _ in range(100):
            if len(sleeps) >= 3:
                break
            await asyncio.sleep(0)
        await supervisor.stop()
        await asyncio.wait_for(task, 2)
        assert sleeps[:3] == [1.0, 1.0, 2.0]
    asyncio.run(scenario())


def test_supervisor_terminates_then_kills_on_stop(tmp_path):
    async def scenario():
        process = FakeProcess(ignore_term=True)
        logs = []

        async def spawn(_argv):
            return process

        supervisor = db_tunnel.SshForwardSupervisor(
            _ssh_forward(tmp_path), spawn=spawn,
            ready_probe=lambda _p: asyncio.sleep(0, result=True),
            stop_timeout=0.01, emit=lambda *args: logs.append(args))
        task = asyncio.create_task(supervisor.run())
        for _ in range(100):
            if ("tracking_readonly", "ssh", "ready", "") in logs:
                break
            await asyncio.sleep(0.01)
        await supervisor.stop()
        await asyncio.wait_for(task, 2)
        assert process.terminated and process.killed
        assert ("tracking_readonly", "ssh", "stopped", "") in logs
    asyncio.run(scenario())
```

第二个测试的时钟注入需要实现支持：进程运行时长 = `clock()` 在 spawn 前与 exit 后的差值。`FakeProcess.wait()` 在被等待时把注入时钟推进 `duration`，`fake_sleep` 再推进退避时间：第一个进程 0 秒 → 退避 1；第二个 200 秒 → 稳定重置；第三个 10 秒 → 下一次退避 2。断言前三个 sleep 为 `[1.0, 1.0, 2.0]`。

- [ ] **Step 6: 运行测试确认失败**

Run: `python -m pytest tests/multiuser/test_db_tunnel.py -q -k supervisor`
Expected: FAIL，`SshForwardSupervisor` 未定义。

- [ ] **Step 7: 实现监督器**

- `run()` 进入时保存 `self._task = asyncio.current_task()`；循环直到停止事件被设置：每轮 `emit(source, "ssh", "starting", "")`、`process = await spawn(argv)`，随即只创建一次 `wait_task = asyncio.create_task(process.wait())`，后续只 await 该任务（不能重复调用 `wait()`）；`FileNotFoundError` → `ssh_unavailable`，其他 `OSError` → `connect_failed`；记录 `started_at = clock()`。
- 竞速 `process.wait()` 与 ready 探针。探针循环每 0.1 秒调用一次 `ready_probe(port)`，直到返回 True 或累计 10 秒超时。先 ready：`emit(source, "ssh", "ready", "")`，继续等进程退出；先退出或探针超时：读取 stderr（最多 64KiB，1 秒超时，失败视为空），探针超时且进程仍存活时 reason 固定为 `forward_failed` 并 `terminate()`（5 秒后 `kill()`），否则 `classify_ssh_stderr` 得 reason。
- 非停止导致的退出：`emit(source, "ssh", "failed", reason)`；运行时长 `>= stable_after` 时退避索引归零；`await` 可被停止事件中断的 `sleep(backoff[index])`，再前进到下一档（上限 30 秒）。
- `stop()` 设置停止事件；若当前子进程存活则 `terminate()`；若 `self._task` 存在则等待 `run` 任务结束。`run` 在等待进程与退避 sleep 时同时监听停止事件；`finally` 对存活子进程 `terminate()`，`stop_timeout` 后用 `kill()` 并等待，最后 `emit(source, "ssh", "stopped", "")`。
- 所有 `emit` 都传 4 个位置参数；任何日志都不包含 stderr 原文、私钥路径或 DSN。

- [ ] **Step 8: 运行测试确认通过**

Run: `python -m pytest tests/multiuser/test_db_tunnel.py -q`
Expected: PASS。

- [ ] **Step 9: 提交**

```bash
git add deploy/multiuser/db_tunnel.py tests/multiuser/test_db_tunnel.py
git commit -m "feat: 实现 SSH 固定参数、失败码映射与常驻监督"
```

---

### Task 5: sidecar 集成与镜像

**Files:**
- Modify: `deploy/multiuser/db_tunnel.py`
- Modify: `deploy/multiuser/worker_sidecar.py`
- Modify: `deploy/multiuser/Dockerfile.worker`
- Modify: `deploy/multiuser/smoke_worker.py`
- Test: `tests/multiuser/test_worker_sidecar.py`
- Test: `tests/multiuser/test_worker_image.py`

**Interfaces:**
- Produces（`db_tunnel.py`）：

```python
@dataclass
class TunnelRuntime:
    tasks: list[asyncio.Task]
    servers: list[asyncio.AbstractServer]
    supervisors: list[SshForwardSupervisor]

    async def stop(self) -> None: ...


async def start_tunnel(
    *, keys_dir: Path = DB_TUNNEL_KEYS,
    config_path: Path = DB_TUNNEL_CONFIG,
    emit=log_state,
    listen_host: str = "0.0.0.0",
) -> TunnelRuntime | None:
    """keys_dir 不存在返回 None；存在但配置非法返回空 runtime 并记录 config_invalid。"""
```

- Produces（`worker_sidecar.py`）：

```python
async def _start(
    host: str, broker_port: int, egress_port: int,
    upstream: tuple[str, int], policy: EgressPolicy, *,
    tunnel_keys: Path = db_tunnel.DB_TUNNEL_KEYS,
    tunnel_config: Path = db_tunnel.DB_TUNNEL_CONFIG,
) -> tuple[asyncio.AbstractServer, asyncio.AbstractServer,
           db_tunnel.TunnelRuntime | None]:
    """先创建 broker/egress 监听，再启动隧道；测试用它取实际端口。"""
```

- [ ] **Step 1: 写失败测试 `tests/multiuser/test_worker_sidecar.py`（无 keys / 非法配置 / 直连转发）**

```python
import socket


async def _echo(reader, writer):
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


def _free_port(start=13300, end=13399) -> int:
    for port in range(start, end + 1):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("端口段已耗尽")


def test_start_without_keys_returns_no_tunnel(tmp_path):
    async def scenario():
        policy = EgressPolicy()
        servers = await sidecar._start(
            "127.0.0.1", 0, 0, ("127.0.0.1", 1), policy,
            tunnel_keys=tmp_path / "missing",
            tunnel_config=tmp_path / "missing.yaml")
        broker, egress, tunnel = servers
        assert tunnel is None
        broker.close()
        egress.close()
        await broker.wait_closed()
        await egress.wait_closed()
    asyncio.run(scenario())


def test_invalid_tunnel_config_keeps_broker_serving(tmp_path, capsys):
    async def scenario():
        keys = tmp_path / "keys"
        keys.mkdir()
        config = tmp_path / "tunnel.yaml"
        config.write_text("version: 2\nforwards: []\n", encoding="utf-8")
        broker, egress, tunnel = await sidecar._start(
            "127.0.0.1", 0, 0, ("127.0.0.1", 1), EgressPolicy(),
            tunnel_keys=keys, tunnel_config=config)
        try:
            assert tunnel is not None
            port = broker.sockets[0].getsockname()[1]
            assert b"200" in await _broker_roundtrip(
                port, _request("GET", "/healthz"))
            assert "state=failed reason=config_invalid" in capsys.readouterr().err
        finally:
            broker.close()
            egress.close()
            await broker.wait_closed()
            await egress.wait_closed()
            await tunnel.stop()
    asyncio.run(scenario())


def test_missing_tunnel_config_keeps_broker_serving(tmp_path, capsys):
    async def scenario():
        keys = tmp_path / "keys"
        keys.mkdir()
        broker, egress, tunnel = await sidecar._start(
            "127.0.0.1", 0, 0, ("127.0.0.1", 1), EgressPolicy(),
            tunnel_keys=keys, tunnel_config=tmp_path / "missing.yaml")
        try:
            assert tunnel is not None
            port = broker.sockets[0].getsockname()[1]
            assert b"200" in await _broker_roundtrip(
                port, _request("GET", "/healthz"))
            assert "state=failed reason=config_invalid" in capsys.readouterr().err
        finally:
            broker.close()
            egress.close()
            await broker.wait_closed()
            await egress.wait_closed()
            await tunnel.stop()
    asyncio.run(scenario())


def test_direct_forward_is_started_from_tunnel_config(tmp_path):
    async def scenario():
        echo = await asyncio.start_server(_echo, "127.0.0.1", 0)
        upstream_port = echo.sockets[0].getsockname()[1]
        keys = tmp_path / "keys"
        keys.mkdir()
        listen_port = _free_port()
        config = tmp_path / "tunnel.yaml"
        config.write_text(
            "version: 1\nforwards:\n"
            "  - source: sales_readonly\n"
            "    mode: direct\n"
            f"    listen_port: {listen_port}\n"
            "    database:\n"
            "      host: 127.0.0.1\n"
            f"      port: {upstream_port}\n",
            encoding="utf-8")
        broker, egress, tunnel = await sidecar._start(
            "127.0.0.1", 0, 0, ("127.0.0.1", 1), EgressPolicy(),
            tunnel_keys=keys, tunnel_config=config)
        try:
            assert tunnel is not None and len(tunnel.servers) == 1
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", listen_port)
            writer.write(b"ping")
            await writer.drain()
            assert await reader.readexactly(4) == b"ping"
            writer.close()
            await writer.wait_closed()
        finally:
            broker.close()
            egress.close()
            await broker.wait_closed()
            await egress.wait_closed()
            await tunnel.stop()
            echo.close()
            await echo.wait_closed()
    asyncio.run(scenario())
```

（`_echo` 复用 Task 3 的 helper，放在本文件顶部。）

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/multiuser/test_worker_sidecar.py -q -k "tunnel or keys"`
Expected: FAIL，`_start` 未定义。

- [ ] **Step 3: 实现 `TunnelRuntime` 与 `start_tunnel`**

- `start_tunnel`：`if not Path(keys_dir).is_dir(): return None`。
- `load_forwards` 抛 `TunnelConfigError`：`emit("", "", "failed", "config_invalid")`，返回空 `TunnelRuntime`。
- 逐 forward：`direct` → `await serve_direct(...)`（`OSError` → `emit(source, "direct", "failed", "forward_failed")`，继续下一个）；`ssh` → 创建 `SshForwardSupervisor`，`asyncio.create_task(supervisor.run())`。
- `TunnelRuntime.stop()`：逐个 `await supervisor.stop()`；取消并 gather 所有 task；关闭并等待所有 server。

- [ ] **Step 4: 实现 `_start` 与 SIGTERM 关停**

- `_start`：先 `asyncio.start_server` 创建 broker/egress；再 `await db_tunnel.start_tunnel(keys_dir=tunnel_keys, config_path=tunnel_config)`；返回三元组。
- `_serve`：调用 `_start`；安装 SIGTERM handler（`loop.add_signal_handler`，在 Windows/不支持时 `try/except NotImplementedError` 跳过）设置 `asyncio.Event`；把两个 `serve_forever()` 与 `stop.wait()` 放入 `asyncio.wait(FIRST_COMPLETED)`；任一 server 任务异常通过 `task.result()` 抛出；`finally` 取消剩余任务、关闭两个 server、`await tunnel.stop()`。
- `main` 的参数校验不变。

- [ ] **Step 5: 修改 `Dockerfile.worker` 与 `smoke_worker.py`**

- `execution` 阶段在 `COPY deploy/multiuser/egress_proxy.py` 后增加：

```dockerfile
COPY deploy/multiuser/db_tunnel.py ./deploy/multiuser/db_tunnel.py
```

- `runtime` 阶段在 `USER root` 后、`COPY deploy/multiuser/managed` 前增加：

```dockerfile
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*
```

- `smoke_worker.py` 的 `REQUIRED_FILES` 增加 `"deploy/multiuser/db_tunnel.py"`。

- [ ] **Step 6: 在 `tests/multiuser/test_worker_image.py` 增加静态断言**

```python
def test_worker_image_contains_tunnel_and_openssh_client():
    text = _text(DOCKERFILE)
    assert "COPY deploy/multiuser/db_tunnel.py" in text
    assert "openssh-client" in text
    execution = text.index("FROM execution AS runtime")
    install = text.index("apt-get install -y --no-install-recommends openssh-client")
    final_user = text.rindex("USER 10001:10001")
    assert execution < install < final_user
    assert "db_tunnel.py" in _text(SMOKE)
```

- [ ] **Step 7: 运行测试确认通过**

Run: `python -m pytest tests/multiuser/test_worker_sidecar.py tests/multiuser/test_worker_image.py -q`
Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add deploy/multiuser/db_tunnel.py deploy/multiuser/worker_sidecar.py \
  deploy/multiuser/Dockerfile.worker deploy/multiuser/smoke_worker.py \
  tests/multiuser/test_worker_sidecar.py tests/multiuser/test_worker_image.py
git commit -m "feat: sidecar 集成数据库隧道并安装 openssh-client"
```

---

### Task 6: provisioner worker 侧 secrets 参数与验收

**Files:**
- Modify: `deploy/multiuser/provisioner.py`
- Test: `tests/multiuser/test_sidecar_provisioner.py`

**Interfaces:**
- Produces：

```python
DB_QUERY_ENV_PATH = "/run/easel-secrets/database-query.env"
DB_TUNNEL_KEYS_PATH = "/run/easel-secrets/keys"
DB_TUNNEL_ALIAS = "db-tunnel"


@dataclass(frozen=True)
class DatabaseQuerySecrets:
    env_file: Path
    keys_dir: Path


def validate_database_query_secrets(
    raw: Path | None,
    sidecar: tuple[str, Path, str] | None,
) -> DatabaseQuerySecrets | None:
    """None 直通；有值但 sidecar 未配置或目录不合法时抛 ProvisionerError。"""


def build_run_argv(
    *, image: str, workspace_id: str, config_version: int,
    paths: RuntimePaths, env_file: Path, public_key_file: Path,
    cidfile: Path | None = None,
    database_query_env_file: Path | None = None,
) -> list[str]: ...
```

- `ProvisionerSettings` 增加 `database_query_secrets: Path | None = None`。
- `_verify_worker_static`、`verify_worker_inspect` 增加末尾关键字参数 `database_query_env_file: Path | None = None`。

- [ ] **Step 1: 写失败测试（参数校验、argv 挂载、严格 inspect）**

```python
def _secrets(tmp_path):
    root = tmp_path / "secrets"
    (root / "keys").mkdir(parents=True)
    (root / "database-query.env").write_text(
        "SALES_READONLY_DATABASE_URL="
        "mysql+asyncmy://readonly:secret@db-tunnel:13300/sales\n",
        encoding="utf-8")
    (root / "keys" / "id_ed25519").write_text("key", encoding="utf-8")
    (root / "keys" / "known_hosts").write_text("", encoding="utf-8")
    return root


def test_database_query_secrets_require_sidecar(tmp_path):
    settings = _settings(tmp_path, sidecar=False)
    settings = dataclasses.replace(
        settings, database_query_secrets=_secrets(tmp_path))
    with pytest.raises(provisioner.ProvisionerError) as rejected:
        provisioner.Provisioner(settings)
    assert rejected.value.code == "database_query_requires_sidecar"


@pytest.mark.parametrize("damage", ["missing", "env_dir", "env_symlink",
                                    "keys_missing", "keys_file"])
def test_database_query_secrets_reject_invalid_layout(tmp_path, damage):
    root = _secrets(tmp_path)
    if damage == "missing":
        root = tmp_path / "absent"
    elif damage == "env_dir":
        (root / "database-query.env").unlink()
        (root / "database-query.env").mkdir()
    elif damage == "env_symlink":
        target = tmp_path / "real.env"
        target.write_text("X=Y\n", encoding="utf-8")
        (root / "database-query.env").unlink()
        try:
            (root / "database-query.env").symlink_to(target)
        except OSError:
            pytest.skip("当前环境不允许创建符号链接")
    elif damage == "keys_missing":
        (root / "keys" / "id_ed25519").unlink()
        (root / "keys" / "known_hosts").unlink()
        (root / "keys").rmdir()
    elif damage == "keys_file":
        (root / "keys" / "id_ed25519").unlink()
        (root / "keys" / "known_hosts").unlink()
        (root / "keys").rmdir()
        (root / "keys").write_text("not a dir", encoding="utf-8")
    settings = dataclasses.replace(
        _settings(tmp_path), database_query_secrets=root)
    with pytest.raises(provisioner.ProvisionerError) as rejected:
        provisioner.Provisioner(settings)
    assert rejected.value.code == "database_query_secrets_invalid"


def test_worker_argv_adds_readonly_env_mount_without_dsn(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    secrets = _secrets(tmp_path)
    env_file = (secrets / "database-query.env").resolve()
    argv = provisioner.build_run_argv(
        image=IMAGE, workspace_id=WORKSPACE, config_version=1, paths=paths,
        env_file=tmp_path / "worker.env",
        public_key_file=_write_public_key(tmp_path / "worker.pub"),
        database_query_env_file=env_file)
    mounts = [argv[index + 1] for index, value in enumerate(argv)
              if value == "--mount"]
    assert len(mounts) == 4
    assert (f"type=bind,src={env_file},dst={provisioner.DB_QUERY_ENV_PATH},"
            f"readonly") in mounts
    assert "readonly:secret@" not in " ".join(argv)
    assert "mysql+asyncmy://" not in " ".join(argv)


def test_worker_static_requires_exact_mount_count_when_enabled(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    paths = provisioner.prepare_workspace(root, WORKSPACE)
    secrets = _secrets(tmp_path)
    env_file = (secrets / "database-query.env").resolve()
    raw = _worker_container(root)
    public_key = pathlib.Path(raw["Mounts"][2]["Source"])
    kwargs = dict(workspace_id=WORKSPACE, config_version=1, image=IMAGE,
                  paths=paths, public_key_file=public_key)
    extra = {"Type": "bind", "Source": str(env_file),
             "Destination": provisioner.DB_QUERY_ENV_PATH, "RW": False}
    raw["Mounts"].append(extra)
    assert provisioner._verify_worker_static(
        raw, database_query_env_file=env_file, **kwargs)
    assert not provisioner._verify_worker_static(raw, **kwargs)
    raw["Mounts"][-1]["RW"] = True
    assert not provisioner._verify_worker_static(
        raw, database_query_env_file=env_file, **kwargs)
```

（`_worker_container` 需要新增 `mounts=None` 参数，见 Step 3。）

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/multiuser/test_sidecar_provisioner.py -q -k "database_query or worker_argv"`
Expected: FAIL，`database_query_secrets` 字段/参数不存在。

- [ ] **Step 3: 实现 worker 侧变更并调整测试夹具**

- 新增常量、`DatabaseQuerySecrets`、`validate_database_query_secrets`；在 `Provisioner.__init__` 中紧跟 `self._sidecar` 校验后调用，结果保存为 `self._database_query`（`DatabaseQuerySecrets | None`）。
- argparse 新增 `--database-query-secrets <宿主绝对目录>`；构造 `ProvisionerSettings` 时转成 `Path`，未提供则保持 `None`。
- 校验顺序：`raw is None` 返回 None；`sidecar is None` → `database_query_requires_sidecar`；目录 `is_symlink()` 或不存在/非目录 → `database_query_secrets_invalid`；`database-query.env` 非普通文件或是 symlink → 同码；`keys` 非目录或是 symlink → 同码；两个路径 `resolve(strict=True)` 后保存。
- `build_run_argv` 在公钥挂载之后、`--env` 之前插入第四个 `--mount`（仅当参数非 None）。
- `_verify_worker_static`：期望挂载数 `3 if database_query_env_file is None else 4`；按 `Destination == DB_QUERY_ENV_PATH` 识别新增挂载（不依赖 Docker 返回顺序），要求 `type=bind`、`Source == str(database_query_env_file)`、`RW is False`、不含 `docker.sock` 且恰好出现一次；其他挂载仍按原规则。
- `verify_worker_inspect` 透传新参数；`Provisioner` 所有 `_verify_worker_static` 调用点（`_ensure_locked`、`activate`、`inspect`、`restore_gateway`）与 `verify_worker_inspect` 调用都传 `database_query_env_file=self._database_query.env_file if self._database_query else None`。
- 测试夹具：`_worker_container(root, env=None, workspace=WORKSPACE, mounts=None)`，`mounts is None` 时保留原 3 个；`FakeDocker` 在 worker `run` 时解析 argv 的所有 `--mount` 三元组，写入 `Mounts`（`readonly` 结尾 → `RW=False`）。

- [ ] **Step 4: 运行 worker 侧测试确认通过**

Run: `python -m pytest tests/multiuser/test_sidecar_provisioner.py -q -k "database_query or worker_argv"`
Expected: PASS。

- [ ] **Step 5: 运行全部 worker 侧测试确认通过**

Run: `python -m pytest tests/multiuser/test_sidecar_provisioner.py -q`
Expected: PASS（包括既有的 3 挂载用例；端到端 4 挂载用例在 Task 7 加入，避免提交红色测试）。

- [ ] **Step 6: 提交 worker 侧变更**

```bash
git add deploy/multiuser/provisioner.py tests/multiuser/test_sidecar_provisioner.py
git commit -m "feat: provisioner 支持 worker 只读数据库凭据文件"
```

---

### Task 7: provisioner sidecar 侧 keys、别名与严格验收

**Files:**
- Modify: `deploy/multiuser/provisioner.py`
- Test: `tests/multiuser/test_sidecar_provisioner.py`

**Interfaces:**
- Produces：

```python
def build_sidecar_run_argv(
    *, image: str, workspace_id: str, policy_file: Path,
    broker_upstream: str,
    database_query_keys: Path | None = None,
) -> list[str]: ...


def verify_sidecar_inspect(
    raw: dict | None, *, workspace_id: str, image: str,
    policy_file: Path, sidecar_network: str, broker_upstream: str,
    database_query_keys: Path | None = None,
) -> bool: ...
```

- [ ] **Step 1: 写失败测试（argv、inspect、自动重建）**

```python
def test_sidecar_argv_adds_keys_mount_and_alias_only_when_enabled(tmp_path):
    keys = (_secrets(tmp_path) / "keys").resolve()
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    with_keys = provisioner.build_sidecar_run_argv(
        image=IMAGE, workspace_id=WORKSPACE, policy_file=policy,
        broker_upstream=UPSTREAM, database_query_keys=keys)
    mounts = [with_keys[index + 1] for index, value in enumerate(with_keys)
              if value == "--mount"]
    assert len(mounts) == 2
    assert f"type=bind,src={keys},dst={provisioner.DB_TUNNEL_KEYS_PATH},readonly" in mounts
    assert with_keys[with_keys.index("--network-alias") + 1] == "db-tunnel"

    without = provisioner.build_sidecar_run_argv(
        image=IMAGE, workspace_id=WORKSPACE, policy_file=policy,
        broker_upstream=UPSTREAM)
    assert len([value for value in without if value == "--mount"]) == 1
    assert "--network-alias" not in without


def test_sidecar_inspect_requires_keys_mount_and_alias(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    keys = (_secrets(tmp_path) / "keys").resolve()
    keys_mount = {"Type": "bind", "Source": str(keys),
                  "Destination": provisioner.DB_TUNNEL_KEYS_PATH,
                  "RW": False}
    raw = _sidecar_container(policy)
    raw["NetworkSettings"]["Networks"][SIDECAR_NETWORK] = {
        "IPAddress": "172.20.0.4"}
    kwargs = dict(workspace_id=WORKSPACE, image=IMAGE, policy_file=policy,
                  sidecar_network=SIDECAR_NETWORK, broker_upstream=UPSTREAM)
    assert provisioner.verify_sidecar_inspect(raw, **kwargs)
    assert not provisioner.verify_sidecar_inspect(
        raw, database_query_keys=keys, **kwargs)

    raw["Mounts"].append(keys_mount)
    assert not provisioner.verify_sidecar_inspect(
        raw, database_query_keys=keys, **kwargs)
    raw["NetworkSettings"]["Networks"][
        provisioner.network_name(WORKSPACE)]["Aliases"] = ["db-tunnel"]
    assert provisioner.verify_sidecar_inspect(
        raw, database_query_keys=keys, **kwargs)
    assert not provisioner.verify_sidecar_inspect(raw, **kwargs)


def test_old_sidecar_is_recreated_with_keys(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = dataclasses.replace(
        _settings(tmp_path), database_query_secrets=_secrets(tmp_path))
    fake = FakeDocker(root, settings.sidecar_policy)
    seeded = _sidecar_container(settings.sidecar_policy)
    fake.containers[provisioner.sidecar_container_name(WORKSPACE)] = seeded
    service = provisioner.Provisioner(settings, run=fake)

    asyncio.run(service.ensure(WORKSPACE, 1))
    sidecar = fake.containers[
        provisioner.sidecar_container_name(WORKSPACE)]
    assert len(sidecar["Mounts"]) == 2
    assert "db-tunnel" in sidecar["NetworkSettings"]["Networks"][
        provisioner.network_name(WORKSPACE)]["Aliases"]
    assert (seeded["Id"] in fake.removed
            or provisioner.sidecar_container_name(WORKSPACE) in fake.removed)


def test_sidecar_is_recreated_without_keys_when_disabled(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    policy = tmp_path / "policies" / "egress.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("{}", encoding="utf-8")
    keys = (_secrets(tmp_path) / "keys").resolve()
    seeded = _sidecar_container(
        policy, mounts=[
            {"Type": "bind", "Source": str(policy),
             "Destination": provisioner.SIDECAR_POLICY_CONTAINER_PATH,
             "RW": False},
            {"Type": "bind", "Source": str(keys),
             "Destination": provisioner.DB_TUNNEL_KEYS_PATH, "RW": False},
        ],
        aliases=["db-tunnel"])
    fake = FakeDocker(root, policy)
    fake.containers[provisioner.sidecar_container_name(WORKSPACE)] = seeded
    service = provisioner.Provisioner(_settings(tmp_path), run=fake)

    asyncio.run(service.ensure(WORKSPACE, 1))
    sidecar = fake.containers[
        provisioner.sidecar_container_name(WORKSPACE)]
    assert len(sidecar["Mounts"]) == 1
    assert "db-tunnel" not in sidecar["NetworkSettings"]["Networks"][
        provisioner.network_name(WORKSPACE)].get("Aliases", [])


def test_ensure_with_secrets_creates_four_mount_worker(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = dataclasses.replace(
        _settings(tmp_path), database_query_secrets=_secrets(tmp_path))
    fake = FakeDocker(root, settings.sidecar_policy)
    service = provisioner.Provisioner(settings, run=fake)

    asyncio.run(service.ensure(WORKSPACE, 1))
    worker = fake.containers[provisioner.container_name(WORKSPACE)]
    sidecar = fake.containers[
        provisioner.sidecar_container_name(WORKSPACE)]
    assert len(worker["Mounts"]) == 4
    assert any(mount["Destination"] == provisioner.DB_QUERY_ENV_PATH
               for mount in worker["Mounts"])
    assert len(sidecar["Mounts"]) == 2

    asyncio.run(service.ensure(WORKSPACE, 1))
    assert len([c for c in fake.commands if _is_worker_run(c)]) == 1
    assert len([c for c in fake.commands if _is_sidecar_run(c)]) == 1


def test_old_three_mount_worker_is_conflict_when_secrets_enabled(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = dataclasses.replace(
        _settings(tmp_path), database_query_secrets=_secrets(tmp_path))
    fake = FakeDocker(root, settings.sidecar_policy)
    fake.workspace_network = True
    worker = _worker_container(root)
    worker["Mounts"][2]["Source"] = str(
        settings.worker_public_key.resolve())
    fake.containers[provisioner.container_name(WORKSPACE)] = worker
    service = provisioner.Provisioner(settings, run=fake)

    with pytest.raises(provisioner.ProvisionerError) as rejected:
        asyncio.run(service.ensure(WORKSPACE, 1))
    assert rejected.value.code == "container_conflict"
    assert fake.containers[provisioner.container_name(WORKSPACE)] is worker
    assert fake.stopped == [] and fake.removed == []


def test_exited_three_mount_worker_is_recreated_when_secrets_enabled(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _fake_health(monkeypatch)
    settings = dataclasses.replace(
        _settings(tmp_path), database_query_secrets=_secrets(tmp_path))
    fake = FakeDocker(root, settings.sidecar_policy)
    fake.workspace_network = True
    worker = _worker_container(root)
    worker["State"] = {"Running": False, "Status": "exited",
                       "Health": {"Status": "unhealthy"}}
    worker["Mounts"][2]["Source"] = str(
        settings.worker_public_key.resolve())
    fake.containers[provisioner.container_name(WORKSPACE)] = worker
    service = provisioner.Provisioner(settings, run=fake)

    asyncio.run(service.ensure(WORKSPACE, 1))
    assert worker["Id"] in fake.removed
    rebuilt = fake.containers[provisioner.container_name(WORKSPACE)]
    assert len(rebuilt["Mounts"]) == 4
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/multiuser/test_sidecar_provisioner.py -q -k "sidecar_argv or sidecar_inspect or recreated_with_keys or without_keys or four_mount or old_three or exited_three"`
Expected: FAIL，`database_query_keys` 参数不存在。

- [ ] **Step 3: 实现 sidecar 侧**

- `build_sidecar_run_argv`：有 keys 时在 policy 挂载后追加第二个 `--mount`，并在 `--network` 值后插入 `--network-alias db-tunnel`。
- `verify_sidecar_inspect`：期望挂载数 `1 if database_query_keys is None else 2`；按 `Destination` 分别识别 policy 与 keys 挂载（不依赖返回顺序），keys 项必须 `type=bind`、`Source == str(keys)`、`RW is False` 且恰好一次；workspace 网络条目的 `Aliases`（缺失视为空）只有在 keys 非 None 时才必须包含 `db-tunnel`，keys 为 None 时不得包含。
- `_sidecar_verified` 传 `database_query_keys=self._database_query.keys_dir if self._database_query else None`。
- `_ensure_sidecar` 创建时同样传入；已有 sidecar 验收失败仍走现有的删除重建逻辑。
- `_ensure_locked` 的 worker 静态验收失败分支：若 `worker_confirmed_exited(existing)` 为真，按已核验实例 ID 删除并置 `existing = None` 后继续创建；运行中/重启中等状态仍抛 `container_conflict`，不得自动删除。
- 测试夹具：`_sidecar_container(policy, workspace=WORKSPACE, mounts=None, aliases=None)`；`FakeDocker` 在 sidecar `run` 时解析 `--mount` 与 `--network-alias`，写入 ``Mounts`` 和 workspace 网络条目的 ``Aliases``。

- [ ] **Step 4: 运行全部 provisioner/sidecar 测试**

Run: `python -m pytest tests/multiuser/test_sidecar_provisioner.py tests/multiuser/test_provisioner.py -q`
Expected: PASS（包括既有的 3/1 挂载用例）。

- [ ] **Step 5: 提交**

```bash
git add deploy/multiuser/provisioner.py tests/multiuser/test_sidecar_provisioner.py
git commit -m "feat: provisioner 为 sidecar 挂载隧道密钥并校验网络别名"
```

---

### Task 8: worker 执行链测试与文档

**Files:**
- Test: `tests/multiuser/test_database_query_worker_env.py`
- Modify: `skills/builtin/database-query/references/setup.md`
- Modify: `skills/builtin/database-query/SKILL.md`
- Modify: `skills/builtin/database-query/references/security-and-limits.md`
- Modify: `deploy/multiuser/README.md`
- Modify: `docs/linux-docker-operations.zh-CN.md`
- Modify: `docs/windows-desktop-operations.zh-CN.md`

**Interfaces:**
- Consumes: `easel.worker.config.build_run_env`、`WorkspacePaths`、`WorkerConfigSnapshot`；技能 CLI 的 `--config`/`--env-file`/`doctor`。

- [ ] **Step 1: 写失败测试 `tests/multiuser/test_database_query_worker_env.py`**

```python
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

    result = subprocess.run(
        [sys.executable, str(CLI), "--config", str(config),
         "--env-file", str(env_file), "doctor"],
        capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    assert '"ok": true' in result.stdout
    assert "dsn-sentinel" not in result.stdout
    assert "dsn-sentinel" not in result.stderr
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/multiuser/test_database_query_worker_env.py -q`
Expected: FAIL（环境缺少依赖时也按失败处理；本机 Agent 环境应已装齐）。

- [ ] **Step 3: 修正测试环境直到通过**

不改产品代码：确认测试用 `sys.executable`、`build_run_env` 提供 `SYSTEMROOT` 等 Windows 必需变量（allowlist 已包含）；`doctor` 不需要真实数据库。若 `_DEPENDENCIES` 缺包，按 `requirements-worker.txt` 安装到 Agent 环境，不得在测试里 skip 依赖检查。

- [ ] **Step 4: 更新 `setup.md`**

- “多人 Docker worker”小节改为：配置固定为 `/opt/easel/skills/builtin/database-query/assets/config.multiuser.yaml`；凭据固定为 `--env-file /run/easel-secrets/database-query.env`；给出完整命令：

```bash
python /opt/easel/skills/builtin/database-query/scripts/db_query.py \
  --config /opt/easel/skills/builtin/database-query/assets/config.multiuser.yaml \
  --env-file /run/easel-secrets/database-query.env \
  doctor
```

- 删除“部署环境把连接串注入 worker 进程环境”的表述，改为“worker 进程环境由 allowlist 重建，DSN 只通过只读文件挂载进入；不要在 worker 的 `--env-file` 或 `--env` 中放 DSN”。
- 增加 `db-tunnel` 端口约定（13300–13399）、直连与 SSH 两类拓扑都由 `tunnel.multiuser.yaml` 固定。
- 增加 3 条 DSN 示例（`mysql+asyncmy://…@db-tunnel:13306/ymkj2.0`、`:13307/meta_new_sync`、`:13308/szcp_sync`），说明口令需百分号编码、端口与 source 一一对应。
- “SSH 隧道”小节保留桌面手工方案，并加一句：多人 Docker 由 sidecar 常驻转发，不要在 worker 内建隧道、不要把私钥放进 workspace。
- 增加宿主 secrets 布局、`install` 权限命令、跳板机 `authorized_keys` 的 `command="",no-pty,no-agent-forwarding,no-x11-forwarding,permitopen="<db_host>:3306"` 限制。
- “私有文件”小节加入 `assets/tunnel.multiuser.yaml`、`/srv/easel/secrets/database-query/`。

- [ ] **Step 5: 更新 `SKILL.md`**

把多人 Docker 的调用方式改为上面的固定命令；明确 `--env-file` 是唯一凭据入口；桌面临时隧道继续指向 `setup.md` 第 5 节。

- [ ] **Step 6: 更新 `security-and-limits.md`**

增加“多人 Docker 的共享凭据边界”：所有 workspace 共用只读 DSN 与 SSH 私钥；agent 可在会话中读取 `database-query.env`，因此数据库账号必须是最小权限只读；`allowed_tables` 不是权限边界；私钥只在 sidecar 只读挂载，worker 不可读；`tunnel.multiuser.yaml` 含非秘密主机名，随镜像分发。

- [ ] **Step 7: 更新部署文档**

- `deploy/multiuser/README.md`：provisioner 启动参数增加 `--database-query-secrets /srv/easel/secrets/database-query`；增加 spec §9 的 7 步 runbook（复制模板、建 secrets 与权限、重建镜像、drain+stop、重启 provisioner、查 sidecar `state=ready`、逐 source `doctor --connect`）；注明轮换 DSN 或私钥后必须重建对应 worker/sidecar，运行中的容器不会自动看到新内容。
- `docs/linux-docker-operations.zh-CN.md`：增加“database-query 多人连接”小节，指向 README runbook，并写出 sidecar 日志检查与 worker 内 `doctor --connect` 命令。
- `docs/windows-desktop-operations.zh-CN.md`：增加一句——多人 Docker 的数据库隧道只支持 Linux 部署；Windows 桌面继续按 `setup.md` 第 5 节手工建 SSH 隧道。

- [ ] **Step 8: 运行文档与技能校验**

Run:

```bash
python scripts/validate_document_paths.py
python scripts/validate_skill_commands.py
python -m pytest tests/test_document_paths.py tests/test_added_skills.py tests/multiuser/test_database_query_worker_env.py -q
```

Expected: 两个脚本无输出且退出码 0；pytest PASS。

- [ ] **Step 9: 提交**

```bash
git add tests/multiuser/test_database_query_worker_env.py \
  skills/builtin/database-query/references/setup.md \
  skills/builtin/database-query/SKILL.md \
  skills/builtin/database-query/references/security-and-limits.md \
  deploy/multiuser/README.md docs/linux-docker-operations.zh-CN.md \
  docs/windows-desktop-operations.zh-CN.md
git commit -m "docs: 补充 database-query 多人 Docker 凭据与隧道说明"
```

---

### Task 9: Docker 集成测试（仅 Linux）

**Files:**
- Create: `tests/integration/multiuser/test_database_query_tunnel_real.py`

**Interfaces:**
- Consumes: `deploy/multiuser/worker_sidecar.py`、`deploy/multiuser/db_tunnel.py`、真实 Docker。
- 仅在 `sys.platform == "linux"` 且 `EASEL_RUN_CONTAINER_TESTS=1` 时运行；其他平台 skip，不得伪装通过。

- [ ] **Step 1: 写测试骨架与跳过条件**

```python
import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or os.environ.get("EASEL_RUN_CONTAINER_TESTS") != "1",
    reason="需要 Linux Docker 与 EASEL_RUN_CONTAINER_TESTS=1")
```

复用 `tests/integration/multiuser/test_sidecar_provisioner_real.py` 的 `_docker` helper 风格（`shutil.which("docker")`、失败输出截断）。

- [ ] **Step 2: 写 fixture：镜像、网络、策略、探针容器**

- 镜像：用临时 Dockerfile 构建 `dbq-tunnel-test:<uuid>`，基础镜像固定 `python:3.11.12-slim-bookworm`，安装 `PyYAML==6.0.3`、`pydantic==2.10.4`、`httpx==0.28.1`、`PyMySQL==1.1.1`、`cryptography`；COPY `easel`、`skills`、`deploy/multiuser`，设置 `WORKDIR /opt/easel`。
- 网络：`workspace` 用 `docker network create --internal`；`easel-control` 用普通 bridge。名称带 uuid，fixture 结束后 `docker rm -f` 容器并删除网络。
- 策略文件 `{}`，挂到 `/etc/easel/egress-policy.json`。
- 探针容器：`docker run -d --name probe --network workspace <image> sleep 600`，用 `docker exec probe python -c ...` 执行 PyMySQL 查询。

- [ ] **Step 3: direct 集成测试**

- 启动 `mysql:8.0.40`，环境 `MYSQL_ROOT_PASSWORD=probe`、`MYSQL_ROOT_HOST=%`、`MYSQL_DATABASE=probe`，命令加 `--default-authentication-plugin=mysql_native_password`，接 `easel-control`；轮询 `docker exec <mysql> mysqladmin ping -pprobe` 直到成功。
- 写真实 `tunnel.multiuser.yaml` 到临时目录：一个 `direct` forward，`listen_port` 取 13300–13399 中一个未被占用的端口，`database.host` 为 MySQL 容器名，`port: 3306`。
- 启动 sidecar：`docker run -d --name sidecar --network workspace --network-alias db-tunnel`，挂载 keys 目录到 `/run/easel-secrets/keys`、真实配置到容器内固定 `DB_TUNNEL_CONFIG` 路径、策略文件；命令 `python deploy/multiuser/worker_sidecar.py --broker-upstream 127.0.0.1:1 --policy /etc/easel/egress-policy.json`；然后 `docker network connect easel-control sidecar`。
- 轮询 `docker logs sidecar` 直到出现 `source=sales_readonly mode=direct state=ready`；断言没有 `config_invalid`。
- 在 probe 容器内用 PyMySQL 连 `db-tunnel:<port>`，执行 `SELECT 1`，断言结果为 1。
- 断言 probe 直连 MySQL 容器名/IP 失败（`socket.create_connection(..., 2)` 抛 `OSError`）。
- `docker inspect` 断言 sidecar 的 `Mounts` 含 `/run/easel-secrets/keys`，probe 容器不含。
- `docker inspect sidecar` 的 workspace 网络条目 `Aliases` 含 `db-tunnel`。

- [ ] **Step 4: SSH 集成测试**

- 建 `dbq-backend` internal 网络；MySQL 只接该网络，容器名 `dbq-mysql`。
- 在宿主用 `ssh-keygen -t ed25519 -N "" -f <tmp>/host_key` 和 `<tmp>/client_key` 生成 host/client 密钥；sshd 固定监听 2222，`known_hosts` 写 `[<sshd_ip>]:2222 ssh-ed25519 <host_pub>`（IP 从容器 inspect 取）。
- 构建 sshd 镜像：`FROM debian:12.9-slim`，`apt-get install -y --no-install-recommends openssh-server`，`mkdir -p /run/sshd`，创建用户 `tunnel`，写入 `authorized_keys`（前缀 `command="",no-pty,no-agent-forwarding,no-x11-forwarding,permitopen="dbq-mysql:3306"`），并设置 `chown -R tunnel:tunnel /home/tunnel/.ssh`、`chmod 700 /home/tunnel/.ssh`、`chmod 600 /home/tunnel/.ssh/authorized_keys`；host key 放到 `/etc/ssh/ssh_host_ed25519_key` 并 `chmod 600`，`CMD ["/usr/sbin/sshd","-D","-e","-p","2222","-o","UsePAM=no"]`；容器接 `dbq-backend` 与 `easel-control`。
- 真实隧道配置：`mode: ssh`，`listen_port: 13301`（或空闲段端口），`database.host: dbq-mysql`、`port: 3306`，`ssh.host: <sshd 在 easel-control 的 IP>`、`port: 2222`、`user: tunnel`、`private_key: client_key`、`known_hosts: known_hosts`。
- keys 目录放 `client_key`（0400，需 `chown 10001:10001`，否则容器内 10001 无法读取）与 `known_hosts`（0444）；目录至少 `0750`；sidecar 挂载方式同 direct。
- 轮询日志出现 `source=tracking_readonly mode=ssh state=ready`；probe 经 `db-tunnel:<port>` 执行 `SELECT 1` 成功；probe 直连 MySQL 名称/IP 失败。
- 断言日志不含 `Permission denied`、私钥路径或 `BEGIN OPENSSH`。

- [ ] **Step 5: 运行集成测试（Linux 宿主）**

Run: `EASEL_RUN_CONTAINER_TESTS=1 python -m pytest tests/integration/multiuser/test_database_query_tunnel_real.py -q`
Expected: 2 passed（direct、SSH）；非 Linux 或未设开关时 skipped。

- [ ] **Step 6: 提交**

```bash
git add tests/integration/multiuser/test_database_query_tunnel_real.py
git commit -m "test: 增加数据库隧道 Docker 集成测试"
```

---

### Task 10: 真实环境验收（人工，部署管理员）

**Files:** 无代码变更；按 `deploy/multiuser/README.md` runbook 执行。

- [ ] **Step 1: 准备宿主 secrets**

按 spec §5.2 创建 `/srv/easel/secrets/database-query/`、`keys/`，私钥 0400 属 10001、env 0400 属 10001、known_hosts 0440 root:10001；确认私钥无口令。

- [ ] **Step 2: 填写真实拓扑**

从 `assets/tunnel.multiuser.example.yaml` 复制为 `assets/tunnel.multiuser.yaml`，填入真实数据库/跳板机地址、端口、用户、密钥文件名；确认文件内没有 `.example.com`、`.invalid`、`.test`、`localhost`。

- [ ] **Step 3: 重建镜像并确认文件存在**

```bash
docker build -f deploy/multiuser/Dockerfile.worker --build-arg DSH_COMMIT=<已验收 commit> -t easel-worker:<tag> .
docker run --rm --entrypoint sh easel-worker:<tag> -c \
  'test -f /opt/easel/skills/builtin/database-query/assets/tunnel.multiuser.yaml && test -f /opt/easel/skills/builtin/database-query/assets/config.multiuser.yaml && command -v ssh'
```

Expected: 退出码 0，`ssh` 路径输出。

- [ ] **Step 4: drain + stop 所有 workspace，重启 provisioner**

```bash
# 使用现有运维命令 drain/stop 全部 workspace
# 然后以新增参数启动 provisioner：
--database-query-secrets /srv/easel/secrets/database-query
```

重建至少一个 workspace。

- [ ] **Step 5: 检查 sidecar 日志**

```bash
docker logs easel-sidecar-<workspace> 2>&1 | grep 'db-tunnel'
```

Expected: 每个启用的 source 出现 `state=ready`；没有 `config_invalid`、`key_unreadable`、`host_key_mismatch`。

- [ ] **Step 6: 在 worker 内逐 source 执行 doctor**

```bash
python /opt/easel/skills/builtin/database-query/scripts/db_query.py \
  --config /opt/easel/skills/builtin/database-query/assets/config.multiuser.yaml \
  --env-file /run/easel-secrets/database-query.env \
  doctor --connect --project yuanmeng-mall-tracking
```

对 `yuanmeng-space`（或 `yuanmeng-mall`、`yuanmeng-rental`）与 `yuanmeng-collectibles` 重复；Expected: 全部 `ok: true`，且实际库名与 `allowed_schema` 一致（特别确认 `ymkj2.0`）。

- [ ] **Step 7: 最小查询与边界确认**

每个 source 执行 `SELECT 1` 与一张授权表的 `LIMIT 1` 查询，不输出业务记录；在 worker 内尝试直连数据库地址，应失败；确认 worker 内不存在私钥文件：

```bash
test ! -e /run/easel-secrets/keys && echo no-private-key
```

- [ ] **Step 8: 记录验收**

保存命令输出与时间；在真实结果之前，部署材料不得表述“多人数据库已可用”。

---

## 自审记录

- **Spec 覆盖**：§5.1→Task 1/2；§5.2→Task 8/10；§5.3→Task 2/5/6；§6.1→Task 5/6/7；§6.2→Task 3；§6.3→Task 4/5；§6.4→Task 3/4；§6.5→Task 8；§7→Task 6/7；§8→Task 4/6/8；§9→Task 1/5/8；§10.1→Task 2–7；§10.2→Task 8；§10.3→Task 9；§10.4→Task 10；§11 分期与任务顺序一致；§12 的可调项已在接口与测试中固定默认值。
- **类型一致性**：`Forward/DatabaseTarget/SshTarget`、`load_forwards(path, keys_dir=, allow_placeholders=)`、`serve_direct(forward, *, host, connect_timeout, max_connections, emit)`、`build_ssh_argv(forward)`、`SshForwardSupervisor(forward, *, ...)`、`TunnelRuntime.stop()`、`start_tunnel(*, keys_dir, config_path, emit, listen_host)`、`DatabaseQuerySecrets(env_file, keys_dir)`、`build_run_argv(..., database_query_env_file=)`、`build_sidecar_run_argv(..., database_query_keys=)`、`verify_worker_inspect(..., database_query_env_file=)`、`verify_sidecar_inspect(..., database_query_keys=)` 在各任务中拼写一致。
- **Review Focus 对应**：上文 5 条分别由 Task 2/5、Task 6/7、Task 4/6、Task 4、Task 3/4/5 的测试覆盖。
- **篇幅**：计划只固定接口、测试断言与实现约束，未粘贴 `db_tunnel.py` 的完整实现体；具体管道、退避与进程管理由实现者按测试补齐。
