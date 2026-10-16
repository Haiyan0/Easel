# Easel 多用户工作区 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留现有 local 工作流的基础上，交付邀请登录、独立 workspace、管理员受管底座、个人能力模型覆盖和每用户隔离运行环境。

**Architecture:** 单一 control 负责认证、配置、会话归属与授权代理；独立 provisioner 创建每用户 worker，worker 复用现有 Easel 后端和私有 ACP 进程。管理员秘密只在控制层/provider broker 解密，worker 使用绑定自己和任务的短期调用凭证。按垂直可测试交付推进，全部隔离验收通过后才开放多人模式。

**Tech Stack:** Python >=3.10、现有 FastAPI/HTTPX/cryptography、sqlite3、React/TypeScript、Docker Engine/Linux、DeepSeek Harness ACP；沿用现有 pytest 与 Node test runner，不引入 Redis、ORM 或 Kubernetes。

**Spec:** [设计规格](../specs/2026-10-16-multiuser-workspaces-design.md)。

状态：规划文档，任务均未执行；用户已授权同时撰写 spec/plan，未要求本轮实现。文中代码是目标接口、关键实现约束与最小回归示例，不应被误认为已存在代码。真实供应商、Docker 和浏览器门槛必须实际运行，mock 通过不能替代。

## Global Constraints

- 一个用户拥有一个私有 workspace；workspace 持久化在服务器文件系统。
- 服务模式为 local / control / worker；默认 local，禁止 worker 因配置缺失回退为无认证 local。
- 生产目标为单台 Linux 服务器，Docker Engine；优先 rootless，必须实际验证资源限制生效。Windows 保留现有本地模式，容器集成验收在 Linux 执行。
- 多人模式第一条验证路径为管理员受管的 DeepSeek Harness ACP；其他现有底座保留 local 能力，不自动宣称经过多人隔离验收。
- 内置代码、依赖、规则与技能只读；多用户模式不开放用户安装底座插件、任意修改命令或导入未经审核的可执行配置。
- 默认每 workspace 最多 2 个活跃 Agent/技能任务，整机最多 8 个；同一会话仅 1 个 prompt；同一账号持久浏览器 profile 仅 1 个操作者。上限可由管理员调整。
- 单任务默认上限 1800 秒；worker 无活跃任务且无活跃请求后空闲 900 秒可回收；正在执行、等待权限确认或发布中的任务不计为空闲。
- 每 worker 初始预算 2 CPU、4 GiB 内存、256 个进程、1 GiB /tmp；持久区默认 10 GiB，采用独立受配额文件系统或经过验收的目录项目配额。配额无法生效时禁止启用多人模式。
- 登录会话闲置 24 小时失效、绝对有效期 7 天；邀请令牌首次兑换有效期 7 天、单次兑换。工程默认值均可由管理员收紧。
- 管理员默认凭证不可下发用户进程是正式验收条件；并非只在 Web 中隐藏字段。为此包括一个范围受限的 provider broker，见第 8 节。

这里的“第 8 节”指 spec 第 8 节。各任务实现同时受 spec 的其余要求约束。

## Review Focus

1. 同一个浏览器登录 B 时，A 的延迟 SSE、跨标签事件和缓存不能再次显示 A 的内容；对应 Task 11 的双身份切换测试。
2. 用户只改 URL 或协议且 Key 留空时，管理员/旧路由凭证不能发给新地址；对应 Task 4、5 的完整路由测试。
3. DNS 校验通过后地址变化、IPv6 私网或 302 跳转仍不能到达控制面；对应 Task 5、8、14 的实际出口测试。
4. worker 启动超时、进程退出或配置更新时，不能重复执行用户发布操作或换成新会话；对应 Task 8、9、10 的恢复和幂等测试。
5. 本地未迁移历史、同名会话 ID、原生 cwd 与新容器路径不同，不能串账号或伪装为可续接；对应 Task 9、11、13、14 的迁移/归属测试。

## 执行规则和依赖

用户工作区已有 ACP、账号统计、测试、前端 lockfile 及文档修改。开始实现前读取 git status/diff，隔离实现工作区时明确如何包含这些已验证改动；不能从不含 ACP 接入的旧基线开始再覆盖用户文件。文档提交只包含本次两份文档。

任务依赖：1 → 2 → 3；1+3 → 4 → 5；6 → 7；3+5+6+7 → 8；1+6 → 9；2+3+4+5+8+9 → 10；2+9+10 → 11；4+11 → 12；8+10 → 13；所有前置任务 → 14。建议在同一会话顺序执行，避免身份/配置/worker 接口并行漂移。切换到子代理执行须由用户选择，本文档不启动代理。

每任务按红→绿→相关回归→审查差异→独立提交执行。代码块中的提交文件列表必须显式展开为该任务实际文件，不使用 git add .。测试命令中的 python 在 Windows 指项目 .venv/Scripts/python.exe，在 Linux 指相同虚拟环境的 python。

## 模块文件图与稳定契约

新增模块均置于已有 easel 包下，web/app.py 只保留薄的兼容接线，不能继续堆入控制平面实现。

```text
easel/control/
  __init__.py       # 包标记
  types.py          # 不含网络/存储逻辑的不可变传输类型
  store.py          # SQLite schema、资源归属与事务
  auth.py           # 邀请/登录/CSRF/限速
  policy.py         # method+route 权限表
  config.py         # 默认/覆盖/版本、加密秘密
  broker.py         # provider 路由、凭证与转发
  runtime.py        # worker 状态机、provisioner 客户端
  sessions.py       # 会话目录、任务幂等与快照协调
  proxy.py          # 授权后的 worker HTTP/SSE 代理
  app.py            # create_app，装配 control API
  cli.py            # 引导管理员、邀请、迁移和备份
easel/worker/
  __init__.py
  context.py        # 模式、代码根、workspace/state/HOME
  guard.py          # 固定 workspace 与内部服务认证
  config.py         # 任务配置快照到子进程环境的映射
  sessions.py       # 本地消息快照、原生绑定与任务结果
  browser.py        # 临时 context 与持久账号互斥
  lifecycle.py      # ready、drain、任务租约与中断恢复
easel/providers/
  __init__.py
  adapters.py       # 现有 provider registry 的代理适配契约
  openai.py         # DeepSeek/图像/TTS/ASR 的受限转发实现
deploy/multiuser/
  Dockerfile.worker
  compose.yaml
  provisioner.py
  egress-policy.md
  README.md
tests/multiuser/    # 纯 Python 单元与 control/worker 假依赖集成
tests/integration/multiuser/  # Linux、Docker、实际 ACP 和浏览器门槛
```

共享类型由 Task 1 创建，后续任务不可私自更名：

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

class DomainError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status

@dataclass(frozen=True)
class Identity:
    user_id: str
    workspace_id: str
    role: Literal["admin", "member"]

@dataclass(frozen=True)
class ConversationRecord:
    id: str
    workspace_id: str
    title: str

@dataclass(frozen=True)
class RunRecord:
    id: str
    workspace_id: str
    conversation_id: str
    status: str
    config_version: int

@dataclass(frozen=True)
class BrowserSession:
    id: str
    token: str
    csrf_token: str

@dataclass(frozen=True)
class EffectiveRoute:
    channel: str
    provider: str
    model: str
    base_url: str
    protocol: str
    credential_ref: str
    source: Literal["admin", "user"]
    version: int
    options: dict

@dataclass(frozen=True)
class WorkerHandle:
    workspace_id: str
    container_id: str
    upstream: str
    service_token: str
    config_version: int

@dataclass(frozen=True)
class WorkspacePaths:
    code_root: Path
    workspace_root: Path
    state_root: Path
    home: Path
```

定义 DomainError(code: str, status: int)；不存在/外空间资源统一 DomainError("not_found", 404)，不可用依赖为 503，版本/幂等冲突为 409。API 错误只包含 code/detail/request_id，不序列化内部对象或秘密。

### Task 1: 资源模型、SQLite 事务和任务归属

**Files:**

```text
Create: easel/control/{__init__,types,store}.py
Create: tests/multiuser/{__init__,test_store}.py
```

**Interfaces:**

- Produces Store.open(path: Path) -> Store；支持 context manager 和 close()。
- create_user(display_name: str, role: str = "member") -> Identity，事务内创建唯一 workspace。
- create_conversation(actor: Identity, title: str = "新对话") -> ConversationRecord；get_conversation(actor, conversation_id) -> ConversationRecord。
- reserve_run(actor, conversation_id, client_request_id, input_digest, config_version) -> RunRecord；finish_run(actor, run_id, status) -> None；同输入重复返回原 id，不同输入抛 409。
- 表结构采用 spec 第 5 节，并加入 schema_migrations、broker_tokens、worker_leases；每个并发约束均在 BEGIN IMMEDIATE 事务中检查和写入。普通用户不能通过入参覆盖 actor.workspace_id。

- [ ] **Step 1:** 写真实 SQLite 跨用户归属和幂等回归，测试不 mock SQL：

```python
import pytest
from easel.control.store import Store
from easel.control.types import DomainError

def test_foreign_conversation_is_missing_and_retry_is_idempotent(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        a, b = store.create_user("A"), store.create_user("B")
        conversation = store.create_conversation(a)
        with pytest.raises(DomainError) as denied:
            store.get_conversation(b, conversation.id)
        assert denied.value.status == 404
        args = (a, conversation.id, "request-1", "digest-1", 1)
        first = store.reserve_run(*args)
        assert store.reserve_run(*args).id == first.id
        with pytest.raises(DomainError) as conflict:
            store.reserve_run(a, conversation.id, "request-1", "digest-2", 1)
        assert conflict.value.status == 409
```

- [ ] **Step 2:** 运行 python -m pytest tests/multiuser/test_store.py -q；确认因模块不存在失败。
- [ ] **Step 3:** 实现上述类型/schema/事务；使用参数化 SQL，时间为 UTC epoch，文件父目录仅服务账号可写。新库从 migration 1 创建；未知新版本拒绝启动。
- [ ] **Step 4:** 增加重开数据库、两连接同时申请相同 request_id、用户禁用、错误回滚和并发 2/8 限制测试；重跑该文件，全部通过。
- [ ] **Step 5:** 审查只涉及本任务文件，显式 git add 后提交 feat(multiuser): add durable workspace ownership and run admission。

### Task 2: 管理员引导、邀请兑换、Cookie 和撤销

**Files:**

```text
Create: easel/control/{auth,app,cli}.py
Create: tests/multiuser/test_auth.py
Modify: easel/cli.py
```

**Interfaces:**

- Consumes Store、Identity、BrowserSession。
- AuthService(store: Store, clock: Callable[[], float] = time.time)。
- create_invite(actor: Identity, user_id: str, ttl_seconds: int = 604800) -> str；exchange(invite: str) -> BrowserSession；authenticate(token: str) -> Identity；revoke_all(user_id: str) -> None。
- create_app(*, store: Store, auth: AuthService, config=None, runtime=None, broker=None) -> FastAPI；后续任务补齐可选服务，未装配功能显式 503。
- CLI 新增 easel serve-control、easel admin bootstrap、easel admin invite；管理 CLI 只用本地控制目录，不通过未认证 HTTP 调用。

- [ ] **Step 1:** 写兑换后不能重用与撤销测试：

```python
import pytest
from easel.control.auth import AuthService
from easel.control.store import Store
from easel.control.types import DomainError

def test_invite_is_single_use_and_login_is_revocable(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        member = store.create_user("Member")
        auth = AuthService(store, clock=lambda: 1000.0)
        invite = auth.create_invite(admin, member.user_id)
        login = auth.exchange(invite)
        assert auth.authenticate(login.token) == member
        with pytest.raises(DomainError):
            auth.exchange(invite)
        auth.revoke_all(member.user_id)
        with pytest.raises(DomainError) as revoked:
            auth.authenticate(login.token)
        assert revoked.value.status == 401
```

- [ ] **Step 2:** 运行 python -m pytest tests/multiuser/test_auth.py -q，验证先红。
- [ ] **Step 3:** 使用 secrets.token_urlsafe(32)、摘要存储与恒定时间比较；实现 auth/exchange、me、logout，设置 spec Cookie 属性。写入端验证 Origin+CSRF，邀请兑换单独校验 Origin；10 分钟失败限速 10 次。
- [ ] **Step 4:** 用 TestClient 的 https://easel.test base_url 验证 Cookie 属性、闲置/绝对超时、CSRF、禁用用户、两连接同时兑换只有一次成功、日志不含明文、普通用户不能发邀请。HTTP 非安全部署只允许显式 local 模式。
- [ ] **Step 5:** 运行该测试文件和既有 tests/test_setup_auth.py，提交 feat(multiuser): add invite login and revocable browser sessions。

### Task 3: 默认拒绝的路由权限与 worker 入口认证

**Files:**

```text
Create: easel/control/policy.py
Create: easel/worker/{__init__,guard}.py
Create: tests/multiuser/test_policy.py
Modify: easel/control/app.py
Modify: web/app.py
```

**Interfaces:**

- authorize(actor: Identity, method: str, path: str) -> RouteDecision，RouteDecision(kind: str, resource_type: str | None, resource_id: str | None)。权限表只决定候选路由；资源授权由 Store 查询，不由字符串 ID 推断。
- verify_worker_request(headers: Mapping[str, str], expected_workspace_id: str, expected_token: str) -> None；不接受客户端 role 头。
- 普通用户的敏感写入 deny 表覆盖 spec 第 9 节所有受管接口，unknown 默认 403；无身份先 401。

- [ ] **Step 1:** 参数化测试所有敏感接口和伪造身份头：

```python
import pytest
from easel.control.policy import authorize
from easel.control.types import DomainError, Identity

@pytest.mark.parametrize("path", [
    "/api/settings/harness", "/api/settings/chat/transport",
    "/api/env", "/api/env/install", "/api/gateway/restart",
    "/api/settings/local-agents/enable", "/api/admin/managed-config",
    "/api/upload/local", "/api/unclassified-new-feature",
])
def test_member_cannot_reach_managed_or_unclassified_writes(path):
    actor = Identity("u-a", "w-a", "member")
    with pytest.raises(DomainError) as denied:
        authorize(actor, "POST", path)
    assert denied.value.status == 403
```

- [ ] **Step 2:** 运行 python -m pytest tests/multiuser/test_policy.py -q，确认先红。
- [ ] **Step 3:** 写显式 method+path 分类；禁止模糊 startswith 放行 /api；代理前剥离 Authorization、Cookie、X-Easel-*、Forwarded、X-Forwarded-* 并重建所需内部头。worker 以固定身份接入，启动失败不得回退 local。
- [ ] **Step 4:** 补 worker 直连拒绝、member 使用 admin 字段/任意 env 字段仍 403、受管 ACP 文件不能经上传写入的集成用例；运行现有 tests/test_local_write_guard.py 和 tests/test_web_security.py。
- [ ] **Step 5:** 提交 feat(multiuser): enforce control and worker authorization boundaries。

### Task 4: 默认配置、个人完整路由和加密存储

**Files:**

```text
Create: easel/control/config.py
Create: tests/multiuser/test_config.py
Modify: skills/shared/scripts/model_registry.py
Modify: easel/control/app.py
```

**Interfaces:**

- ConfigService(store: Store, master_key: bytes)；master_key 为 32 字节，服务启动从受保护文件读取。
- publish(actor, settings: dict, expected_version: int) -> int；resolve(actor, channel: str) -> EffectiveRoute。
- save_override(actor, channel: str, route: dict) -> None；clear_override(actor, channel: str) -> None；public_settings(actor) -> dict。
- 完整 route 字段为 provider/model/base_url/protocol/secret/options；source/credential_ref/version 由服务端产生。支持 model-only override 时以 route_kind="managed-model" 明确表达，不能靠缺字段猜测。
- 现有 channel speech 映射 registry voice，允许列表 speech/image/video/music；其他 channel 拒绝 member 修改。registry 增加 broker_adapter 和 override_allowed，不复制供应商定义。

- [ ] **Step 1:** 写默认继承和换地址留空凭证拒绝的回归：

```python
import pytest
from easel.control.config import ConfigService
from easel.control.store import Store
from easel.control.types import DomainError

def test_new_personal_endpoint_never_inherits_admin_secret(tmp_path):
    with Store.open(tmp_path / "control.sqlite3") as store:
        admin = store.create_user("Owner", "admin")
        user = store.create_user("Member")
        config = ConfigService(store, b"x" * 32)
        route = dict(provider="openai-compatible", model="tts-default",
                     base_url="https://provider.example/v1", protocol="openai",
                     secret="admin-test-secret", options={})
        config.publish(admin, {"defaults": {"speech": route}}, expected_version=0)
        assert config.resolve(user, "speech").source == "admin"
        with pytest.raises(DomainError) as invalid:
            config.save_override(user, "speech", {
                **route, "base_url": "https://personal.example/v1", "secret": ""})
        assert invalid.value.code == "personal_credentials_required"
        assert "admin-test-secret" not in str(config.public_settings(user))
```

- [ ] **Step 2:** 运行 python -m pytest tests/multiuser/test_config.py -q，确认先红。
- [ ] **Step 3:** 实现 AES-GCM secret bundles、AAD 作用域绑定、版本比较和 spec 第 8 节全部继承行为；URL/provider/protocol 任一改变即不能保留旧 Key。master key 缺失/错误拒绝启动或解密，不生成覆盖原 key。
- [ ] **Step 4:** 增加 A/B 覆盖隔离、恢复默认、管理员更新快照、model-only 引用、同路由空 Key 保留个人 Key、换协议/双 Key 缺项、错误 master key、DB 不含明文、If-Match 409 测试。
- [ ] **Step 5:** 运行新测试及 tests/test_settings_stale_mirror.py，提交 feat(multiuser): add scoped capability configuration inheritance。

### Task 5: Provider broker、协议适配和受限出口

**Files:**

```text
Create: easel/control/broker.py
Create: easel/providers/{__init__,adapters,openai}.py
Create: tests/multiuser/test_broker.py
Create: tests/integration/multiuser/test_egress.py
Modify: easel/control/app.py
Modify: skills/shared/scripts/model_registry.py
```

**Interfaces:**

- BrokerService(store: Store, config: ConfigService, clock: Callable[[], float] = time.time)。
- issue(actor, run_id: str, channel: str) -> str；authorize(token: str, workspace_id: str, run_id: str, channel: str) -> EffectiveRoute；revoke_run(run_id: str, workspace_id: str) -> None。
- ProviderAdapter.accepts(method: str, relative_path: str) -> bool；prepare(route: EffectiveRoute, method: str, relative_path: str, headers: dict) -> UpstreamRequest。
- UpstreamRequest(method, url, headers) 不含用户可覆盖的目标；body 独立流传递。仅 adapter 内解密该 credential_ref，凭证不返回调用者。
- 私网排除由独立出口代理在连接时实施，broker 连接只允许该代理；应用层 URL 检查作为前置。测试能指定 fake transport，但生产没有 silent direct fallback。

- [ ] **Step 1:** 写 run-scoped token 不能被另一个 workspace 使用的测试：

```python
import pytest
from easel.control.broker import BrokerService
from easel.control.config import ConfigService
from easel.control.store import Store
from easel.control.types import DomainError

def test_broker_token_cannot_change_workspace_or_channel(tmp_path):
    with Store.open(tmp_path / "db") as store:
        admin = store.create_user("Owner", "admin")
        a, b = store.create_user("A"), store.create_user("B")
        config = ConfigService(store, b"x" * 32)
        config.publish(admin, {"defaults": {"speech": dict(
            provider="openai-compatible", model="tts-default", protocol="openai",
            base_url="https://provider.example/v1", secret="test-secret", options={})}}, 0)
        conversation = store.create_conversation(a)
        run = store.reserve_run(a, conversation.id, "r1", "digest", 1)
        broker = BrokerService(store, config, clock=lambda: 1000.0)
        token = broker.issue(a, run.id, "speech")
        with pytest.raises(DomainError):
            broker.authorize(token, b.workspace_id, run.id, "speech")
        with pytest.raises(DomainError):
            broker.authorize(token, a.workspace_id, run.id, "image")
```

- [ ] **Step 2:** 运行 python -m pytest tests/multiuser/test_broker.py -q，确认先红。
- [ ] **Step 3:** 实现 opaque token 摘要/过期/撤销，校验 Run 活跃、来源/模型 allowlist、2/8 并发预算；用 HTTPX 流式转发 JSON、multipart、二进制和 SSE，剥离 hop-by-hop/用户上游认证头，finally 关闭上游。DeepSeek 和首批 image/TTS/ASR adapter 列出精确路径，其他请求拒绝。
- [ ] **Step 4:** 单元测试双 Key、凭证不回显、模型参数篡改、Content-Type/boundary、SSE 断开关闭、重定向拒绝、过期 token、未知 adapter 发布拒绝；Linux 测试出口拒绝 IPv4/IPv6、重绑定与 control/worker 互访。输出 registry 每供应商支持矩阵，未适配默认显示不可用；不自动给用户进程注入 Key。
- [ ] **Step 5:** 单元测试通过并记录真实出口门槛未执行/已通过状态，提交 feat(multiuser): broker managed provider calls without exposing keys。

### Task 6: 分开代码根、数据根和管理员 ACP 配置

**Files:**

```text
Create: easel/worker/{context,config}.py
Create: ./skills/shared/scripts/runtime_paths.py
Create: tests/multiuser/test_workspace_paths.py
Modify: web/app.py
Modify: easel/harnesses/{base,commands,workspace,rpc}.py
Modify: easel/runtime_env.py
Modify: tests/test_harness_workspace.py
```

**Interfaces:**

- resolve_paths(env: Mapping[str, str], default_code_root: Path) -> WorkspacePaths；显式 mode 校验。
- WorkerConfigSnapshot(workspace_id, run_id, revision, routes, agent)；build_run_env(paths: WorkspacePaths, snapshot: WorkerConfigSnapshot, base_env: Mapping[str, str]) -> dict[str, str]。
- RunRequest 增加 workspace_id: str = ""、code_root: Path | None = None；cwd 仅代表数据/工具工作目录，local 默认保持原值。
- acp_command(cwd: Path, *, managed_command: tuple[str, ...] | None = None)；worker 必须传受管 command，不读取可写 outputs/_harness/acp.json。继续验证 argv，禁止 .cmd/.bat 注入等已有保护回退。

- [ ] **Step 1:** 验证 worker 路径不落到开发者 HOME，且缺配置拒绝：

```python
import pytest
from easel.worker.context import resolve_paths
from easel.control.types import DomainError

def test_worker_requires_explicit_private_roots(tmp_path):
    code = tmp_path / "code"
    with pytest.raises(DomainError):
        resolve_paths({"EASEL_SERVICE_MODE": "worker"}, code)
    env = {"EASEL_SERVICE_MODE": "worker", "EASEL_CODE_ROOT": str(code),
           "EASEL_WORKSPACE_ROOT": str(tmp_path / "a"),
           "EASEL_STATE_ROOT": str(tmp_path / "a-state"),
           "HOME": str(tmp_path / "a-home")}
    paths = resolve_paths(env, code)
    assert paths.workspace_root == tmp_path / "a"
    assert paths.code_root == code
```

- [ ] **Step 2:** 运行 python -m pytest tests/multiuser/test_workspace_paths.py -q，确认先红。
- [ ] **Step 3:** 显式拆分 web 全局路径来源；受管配置快照只能注入 broker token，不合并宿主所有 os.environ。仅保留运行所需 PATH/编码/明确代理变量；禁止从宿主 .env/HOME 回填秘密。DSH_HOME/DSH_AGENTS_HOME 一并显式设置。control 用 Ed25519 私钥签署 snapshot，worker 只读公钥校验 workspace/run/revision/有效期/admission nonce；相同 nonce 不重复启动。
- [ ] **Step 4:** 修正 harness_instructions：代码技能路径来自 code_root，产物/画像来自 cwd；rpc 进程池 key 加 workspace namespace，保留原生续接和私有 stdio 行为。run token 变化导致进程重建时 resume 同一 native_id，不把凭证扩大为跨 run 长期 token。增加伪造 outputs ACP 文件、伪造签名/过期快照、用户修改环境不切底座及 token 更新仍续同一历史的测试。
- [ ] **Step 5:** 运行 tests/multiuser/test_workspace_paths.py、tests/test_harness_workspace.py、tests/test_agent_harnesses.py、tests/test_runtime_env.py，提交 refactor(runtime): separate immutable code and private workspace roots。

### Task 7: 技能可写状态、浏览器账号和产物预览

**Files:**

```text
Create: easel/worker/browser.py
Create: tests/multiuser/{test_skill_state,test_browser_profiles,test_artifact_access}.py
Modify: web/app.py
Modify: skills/shared/scripts/account_stats.py
Modify: skills/builtin/skill-wechat-publisher/scripts/{config,wechat_token}.py
Modify: ./skills/shared/scripts/runtime_paths.py
```

**Interfaces:**

- state_path(kind: str, account_id: str = "default") -> Path；kind 仅允许 wechat-config/wechat-token/platform-cookie/browser-profile/skill-cache，账号 ID 验证后派生路径；local 模式保留兼容读取，不向旧目录双写。
- BrowserProfiles(paths: WorkspacePaths)；profile_dir(platform: str, account_id: str) -> Path；persistent_profile(platform, account_id) 为带跨进程文件锁的 context manager。
- 临时浏览器 context 用 Playwright 原有能力在任务退出时关闭；worker 取消/异常也释放锁。账号文件不在 outputs 公共目录。

- [ ] **Step 1:** 写 A/B 同平台同账号目录必须不同的测试：

```python
from easel.control.types import WorkspacePaths
from easel.worker.browser import BrowserProfiles

def test_browser_profiles_are_scoped_to_workspace(tmp_path):
    def manager(name):
        root = tmp_path / name
        return BrowserProfiles(WorkspacePaths(tmp_path / "code", root,
                                             root / "state", root / "home"))
    a, b = manager("a"), manager("b")
    assert a.profile_dir("xiaohongshu", "default") != b.profile_dir("xiaohongshu", "default")
```

- [ ] **Step 2:** 运行本任务三个测试文件，确认先红。
- [ ] **Step 3:** 用 rg 扫描所有现有技能脚本的 Path.home/__file__ 邻接写入、Cookies/config/cache 路径，建立“脚本、旧路径、新路径、测试”清单；修改所有多人可调用且写共享目录的路径，不能仅修公众号一个文件。按已选供应商复核 config.py 的进程内环境修改不会污染其他任务快照；任务间复用进程需显式传配置。
- [ ] **Step 4:** 增加只读技能目录下执行、两个临时 context Cookie 隔离、持久 profile 重启保持、同 profile 两任务排队、取消后锁释放测试。outputs 下载拒绝内部目录和符号链接；HTML/SVG 响应设置强制 CSP sandbox，直链也不能同源执行脚本；upload/local worker 模式拒绝。
- [ ] **Step 5:** 运行新测试及 tests/test_account_stats_login.py、tests/test_login_start.py、tests/test_output_paths.py、tests/test_web_security.py；提交 fix(multiuser): isolate skill credentials and browser state。

### Task 8: Worker 容器、provisioner 和生命周期

**Files:**

```text
Create: easel/control/runtime.py
Create: easel/worker/lifecycle.py
Create: deploy/multiuser/{Dockerfile.worker,compose.yaml,provisioner.py,egress-policy.md,README.md}
Create: .dockerignore
Create: tests/multiuser/test_runtime.py
Create: tests/integration/multiuser/test_container_isolation.py
```

**Interfaces:**

- RuntimeManager(store: Store, provisioner: Provisioner)；await ensure_ready(workspace_id: str, config_version: int) -> WorkerHandle；await drain(workspace_id: str) -> None；await collect_idle(now: float) -> list[str]。
- Provisioner 为此任务定义的 Protocol：async ensure(workspace_id, config_version) -> WorkerHandle、async stop(workspace_id) -> None、async inspect(workspace_id) -> dict；服务端只从数据库受管配置构建 docker argv。
- ensure_ready 校验 ready 的 workspace_id；容器/宿主路径均由 workspace UUID 派生，调用者不能提交任意 volume。

- [ ] **Step 1:** 用明确 fake provisioner 检查同空间并发只启动一次：

```python
import asyncio
from easel.control.runtime import RuntimeManager
from easel.control.store import Store
from easel.control.types import WorkerHandle

def test_concurrent_cold_start_is_single_flight(tmp_path):
    class FakeProvisioner:
        starts = 0
        async def ensure(self, workspace_id, config_version):
            self.starts += 1
            await asyncio.sleep(0)
            return WorkerHandle(workspace_id, "fake", "http://worker:7860", "service-token", config_version)
        async def stop(self, workspace_id):
            return None
        async def inspect(self, workspace_id):
            return {"workspace_id": workspace_id, "ready": True}
    async def scenario():
        with Store.open(tmp_path / "db") as store:
            user = store.create_user("A")
            provisioner = FakeProvisioner()
            manager = RuntimeManager(store, provisioner)
            handles = await asyncio.gather(*[
                manager.ensure_ready(user.workspace_id, 1) for _ in range(4)])
            assert provisioner.starts == 1
            assert {h.workspace_id for h in handles} == {user.workspace_id}
    asyncio.run(scenario())
```

- [ ] **Step 2:** 运行 python -m pytest tests/multiuser/test_runtime.py -q，确认先红。
- [ ] **Step 3:** 实现数据库生命周期租约、60 秒就绪期限、状态对账与安全回收。镜像固定 Harness 版本/commit 和已验收 ACP profile，在构建时安装依赖并预生成只读 profiles；启动不做 npm/pip 在线安装。不 COPY 开发者 .env、outputs、profiles、浏览器 HOME 或控制密钥进入镜像，构建 context 使用 .dockerignore 排除。
- [ ] **Step 4:** provisioner 强制非 root、read-only rootfs、cap-drop、no-new-privileges、PID/CPU/内存/tmp/持久区配额，挂载仅本人数据与只读配置。按 spec 网络矩阵配置 worker 独立网络与出口；不能通过传 --no-sandbox 让浏览器验收通过。provisioner Unix socket 不挂入 worker/broker；control 仅可连接此结构化受限 socket，不能访问 Docker socket，不开放远程容器管理端口。
- [ ] **Step 5:** 测试错误 workspace ready、启动失败、回收期间请求、活跃任务不回收、管理进程重启后认领旧容器；Linux 真实测试 Docker inspect 与越界读写/网络/资源预算。单元通过后提交 feat(multiuser): provision isolated per-workspace workers，并在记录中注明真实容器测试状态。

### Task 9: 服务端会话目录、原生绑定和消息快照

**Files:**

```text
Create: easel/control/sessions.py
Create: easel/worker/sessions.py
Create: tests/multiuser/test_sessions.py
Modify: easel/control/{store,app}.py
Modify: easel/harnesses/store.py
Modify: web/app.py
```

**Interfaces:**

- SessionService(store: Store)；list_conversations(actor) -> list[ConversationRecord]、create_conversation(actor, title="新对话") -> ConversationRecord、assert_run(actor, run_id) -> RunRecord。
- WorkspaceSessionStore(root: Path, workspace_id: str)；bind(session_id: str, runtime: str, native_id: str, agent_version: int) -> None、binding(session_id) -> dict、append_message(session_id, message: dict) -> None、messages(session_id) -> list[dict]。
- control 管会话元数据，worker 保存本人的消息快照/原生绑定；消息快照通过已授权内部路由返回，不允许浏览器提交完整助手历史覆盖服务端。
- Task 1 的 Store reserve_run 为任务幂等唯一入口。新增 list/update/archive/delete conversation 均带 actor，删除仍保留原生历史现有语义，除非管理员专门执行数据删除流程。

- [ ] **Step 1:** 写相同 Easel/native ID 可以在两空间共存且互不覆盖的回归：

```python
from easel.worker.sessions import WorkspaceSessionStore

def test_equal_native_ids_are_local_to_each_workspace(tmp_path):
    a = WorkspaceSessionStore(tmp_path / "a", "workspace-a")
    b = WorkspaceSessionStore(tmp_path / "b", "workspace-b")
    a.bind("same-easel-id", "acp", "same-native-id", 1)
    b.bind("same-easel-id", "acp", "same-native-id", 1)
    a.append_message("same-easel-id", {"role": "user", "content": "A-private"})
    assert a.binding("same-easel-id")["workspace_id"] == "workspace-a"
    assert b.binding("same-easel-id")["workspace_id"] == "workspace-b"
    assert b.messages("same-easel-id") == []
```

- [ ] **Step 2:** 运行 python -m pytest tests/multiuser/test_sessions.py -q，确认先红。
- [ ] **Step 3:** 实现服务端生成 UUID、分页目录、消息和附件归属验证；绑定记录增 workspace/version 字段。local 老格式可在 local 读取，但 worker 只接受匹配自身的记录，禁止自动认领没有归属的旧文件。native_id 改变或 Harness 恢复失败必须报错，不能重建空历史替代。
- [ ] **Step 4:** 测试外空间目录/消息/run 404、未知/归档/删除行为、改底座版本后的续接约束、worker 重开磁盘快照与原生绑定、请求幂等、原生 cwd 不兼容提示；运行现有 tests/test_agent_harnesses.py 和 tests/test_chat_question_replay.py。
- [ ] **Step 5:** 提交 feat(multiuser): persist scoped conversations and native session bindings。

### Task 10: 授权代理、任务生命周期与 SSE

**Files:**

```text
Create: easel/control/proxy.py
Create: tests/multiuser/{test_proxy,test_runs}.py
Modify: easel/control/{app,sessions,store}.py
Modify: easel/worker/{lifecycle,config,sessions}.py
Modify: web/app.py
```

**Interfaces:**

- WorkerRelay(runtime: RuntimeManager, sessions: SessionService, auth: AuthService)；forward(actor, request) -> Response，只有已匹配 policy 的请求可调用。
- create_app 装配 config/runtime/broker 后启用完整任务路由。内部 /internal/runs/{id} 接收已授权任务及固定配置 snapshot；/internal/runs/{id}/events 支持 after event_id；/internal/sessions/{id}/messages 返回本地快照。
- 入站 request_id 不复用模型/用户秘密；run admission 先成功后发一个 prompt。任务事件单调递增、终态持久化、失败释放并发租约。所有状态更新限定 workspace_id。

- [ ] **Step 1:** 写运行归属和重试只得到同一个 Run 的测试；沿用 Task 1 store，但通过 SessionService.assert_run 防止代理越权：

```python
import pytest
from easel.control.sessions import SessionService
from easel.control.store import Store
from easel.control.types import DomainError

def test_foreign_run_cannot_be_stopped_or_replayed(tmp_path):
    with Store.open(tmp_path / "db") as store:
        a, b = store.create_user("A"), store.create_user("B")
        conversation = store.create_conversation(a)
        run = store.reserve_run(a, conversation.id, "client-1", "body-digest", 1)
        sessions = SessionService(store)
        assert sessions.assert_run(a, run.id).id == run.id
        with pytest.raises(DomainError) as foreign:
            sessions.assert_run(b, run.id)
        assert foreign.value.status == 404
```

- [ ] **Step 2:** 运行 python -m pytest tests/multiuser/test_proxy.py tests/multiuser/test_runs.py -q，确认先红。
- [ ] **Step 3:** 将 chat、后台技能、画像生成、analytics、发布纳入共同任务租约；不只统计聊天。旧请求 turnId 在多人入口映射为 client_request_id，admission 事件返回服务器 run_id，前端 pendingTurnId 改存该 run_id，后续回放/停止/问题回答都按它授权。同步短请求使用 request lease；用户断开继续记录任务，重新连接只 replay，不再次 prompt。HTTPX 客户端由 app lifespan 管理，stream response 在 finally 关闭；TLS 入口配置关闭 SSE 缓冲并保留心跳。
- [ ] **Step 4:** 增加 SSE 5 秒撤销边界、A 的 question_id 被 B 回答、turn_id 混用、断线重连、双 POST 并发、启动超时/502 不自动重试、worker 崩溃标 interrupted、等待确认不被回收、drain 不接收新任务测试；所有未知路由保持拒绝。
- [ ] **Step 5:** 运行本任务测试和 tests/test_chat_question_replay.py、tests/test_login_transport.py、tests/test_web_security.py，提交 feat(multiuser): route authorized runs and resumable event streams。

### Task 11: 登录、身份缓存与服务端历史接入

**Files:**

```text
Create: web/frontend/src/lib/auth.ts
Create: web/frontend/src/components/LoginPage.tsx
Create: tests/frontend/multiuserAuth.test.mjs
Create: tests/frontend/multiuserHistory.test.mjs
Modify: web/frontend/src/{App.tsx,lib/api.ts,lib/store.ts,lib/whoami.ts}
Modify: web/frontend/src/components/ChatPage.tsx
```

**Interfaces:**

- AuthContext { userId: string; workspaceId: string; role: 'admin'|'member'; csrfToken: string; capabilities: string[] }。
- loadIdentity(): Promise<AuthContext>；logout(): Promise<void>；identityCacheKey(ctx, name): string；IdentityEpoch.begin(ctx): number、invalidate(): void、isCurrent(epoch): boolean。
- api request 自动添加 CSRF 给写请求，携带同源 Cookie，不把登录 token 存入 Web Storage。
- loadSessions/saveSessions 保留 local 模式；多人模式缓存 key 带 user/workspace，session 列表/最终消息通过服务端 API 读写，不能上传助手消息覆盖服务器。

- [ ] **Step 1:** 写延迟事件不能回填切换身份后的状态测试：

```javascript
import test from 'node:test';
import assert from 'node:assert/strict';

test('old identity cannot commit events after account switch', async () => {
  const { IdentityEpoch } = await import('../../web/frontend/src/lib/auth.ts');
  const guard = new IdentityEpoch();
  const a = guard.begin({ userId: 'a', workspaceId: 'wa' });
  guard.invalidate();
  const b = guard.begin({ userId: 'b', workspaceId: 'wb' });
  assert.equal(guard.isCurrent(a), false);
  assert.equal(guard.isCurrent(b), true);
});
```

测试导入应沿用仓库现有 TS 转译加载方法；若 Node 当前不支持直接导入 TS，复制现有测试使用的 TypeScript transpileModule 装载器，不为了该测试引入第二个 bundler。IdentityEpoch.begin 的入参使用 Pick<AuthContext, 'userId'|'workspaceId'>，避免测试依赖无关字段。

- [ ] **Step 2:** 运行 node --test tests/frontend/multiuserAuth.test.mjs tests/frontend/multiuserHistory.test.mjs，确认先红。
- [ ] **Step 3:** App 先完成 auth/me 再挂载用户页面；退出取消所有 fetch/SSE、重置 React 状态、清理对象 URL、重建 user-scoped BroadcastChannel。A/B 缓存始终分开，旧无作用域缓存只供 local 使用。登录事件通道只通知身份重查，不发送 token/内容。
- [ ] **Step 4:** 测试同浏览器 A→B、跨标签退出、会话 sessionStorage、发布草稿、onboarding、whoami 缓存、401 后取消连接、换设备目录重载与 local 兼容。服务端 final snapshot 是刷新恢复来源，不把其他 workspace 的旧缓存自动导入新用户。
- [ ] **Step 5:** 运行本任务 Node 测试、既有 tests/frontend/sessionHistory.test.mjs 与 tests/frontend/whoamiCache.test.mjs，在 web/frontend 执行 npm run build；提交 feat(web): add invite login and identity-scoped client state。

### Task 12: 受管底座展示、个人覆盖和管理员配置界面

**Files:**

```text
Create: web/frontend/src/components/AdminSettingsPage.tsx
Create: tests/frontend/multiuserSettings.test.mjs
Create: tests/multiuser/test_settings_api.py
Modify: web/frontend/src/components/{SettingsPanel,HarnessSettings,Sidebar}.tsx
Modify: web/frontend/src/lib/api.ts
Modify: easel/control/app.py
Modify: easel/control/config.py
Modify: web/app.py
```

**Interfaces:**

- 现有 GET /api/settings/models 响应保留原字段，增加 source、canOverride、version、supported、unavailableReason；保存请求 channel/rows 仍兼容，control 转换成 Task 4 的原子路由结构。
- DELETE /api/settings/models/overrides/{channel} 恢复继承，默认配置写入仅 /api/admin/managed-config（If-Match 必填）。
- GET harness 给 member 返回 locked=true；底座写入始终 403，即使隐藏按钮被手工调用。admin 默认与个人覆盖页面分别标明作用范围。

- [ ] **Step 1:** 写真实 control API 的 member 不能改 Harness、可读受管摘要，以及保存 speech 不改其他人的测试。测试 app 装配使用 Task 2 create_app，身份由真实 exchange Cookie 获得，不在测试里替换 role guard。

```python
def test_settings_response_contains_provenance_not_secrets():
    from easel.control.config import public_route
    from easel.control.types import EffectiveRoute
    route = EffectiveRoute("speech", "openai-compatible", "tts-model",
                           "https://provider.example/v1", "openai", "secret-ref",
                           "admin", 3, {})
    response = public_route(route, can_override=True, configured=True)
    assert response["source"] == "admin"
    assert response["canOverride"] is True
    assert "credential_ref" not in response
    assert "key" not in response
```

public_route(route: EffectiveRoute, *, can_override: bool, configured: bool) -> dict 由本任务加入 config.py，不能直接 dataclasses.asdict 返回内部结构。

- [ ] **Step 2:** 运行 python -m pytest tests/multiuser/test_settings_api.py -q 和 node --test tests/frontend/multiuserSettings.test.mjs，确认先红。
- [ ] **Step 3:** 实现默认/个人来源、恢复默认、凭证留空语义的 UI；member 隐藏安装/底座切换/重启操作。admin 页面含用户邀请、禁用/撤销、受管底座和默认路由编辑，邀请明文只显示一次。保存前明确显示“个人”或“全体默认”。
- [ ] **Step 4:** 添加未知 channel、敏感字段混入、空密钥换地址、model-only 管理员模型 allowlist、不受支持供应商状态和管理员乐观锁测试。运行既有 tests/frontend/harnessSettings.test.mjs、tests/frontend/settingsThinking.test.mjs、tests/test_web_model_config_parity.py；多人模式不得调用单实例 .env/OpenClaw 同步分支。
- [ ] **Step 5:** 在 web/frontend 执行 npm run build，提交 feat(web): expose managed defaults and personal capability overrides。

### Task 13: 备份恢复、显式迁移与运行手册

**Files:**

```text
Create: tests/multiuser/test_operations.py
Create: docs/multiuser-deployment.zh-CN.md
Modify: easel/control/{cli,store,runtime}.py
Modify: deploy/multiuser/README.md
Modify: README.md
Modify: docs/agent-harnesses.md
```

**Interfaces:**

- CLI easel admin backup --destination、restore --source --destination、migrate-local --source --workspace --dry-run；写操作先验证受管绝对目标属于配置的控制/数据根，源保持只读。
- backup_control(store: Store, destination: Path) -> Path 使用 sqlite3 backup API；文件系统备份前 worker drain，manifest 记录 schema、镜像/Harness/配置版本及完整性摘要，不保存明文访问令牌。
- 迁移输出 MigrationReport(imported: list[str], non_resumable_sessions: list[str], rejected: list[str])；首次目标必须为空，跨用户目标由管理员本地 CLI 显式选择。

- [ ] **Step 1:** 写 SQLite 快照在新目录重开后归属保留：

```python
from easel.control.cli import backup_control
from easel.control.store import Store

def test_backup_restores_workspace_ownership_in_new_directory(tmp_path):
    with Store.open(tmp_path / "source" / "db") as original:
        user = original.create_user("A")
        conversation = original.create_conversation(user)
        backup = backup_control(original, tmp_path / "restored" / "db")
    with Store.open(backup) as restored:
        assert restored.get_conversation(user, conversation.id).workspace_id == user.workspace_id
```

- [ ] **Step 2:** 运行 python -m pytest tests/multiuser/test_operations.py -q，确认先红。
- [ ] **Step 3:** 实现一致性控制快照、受保护 master key 备份、工作区文件清单、恢复前完整性检查；恢复测试 key 丢失/不匹配必须明确失败，不重置秘密。恢复命令禁止覆盖运行中的控制目录。
- [ ] **Step 4:** 迁移先 dry-run、备份、显式确认目标为空再复制内容；原生 cwd 不可兼容的历史仅报告不可续接，不能替换 native_id。增加 symlink/junction 逃逸、计算目标不在指定根、已有目标非空、迁移中断与源文件哈希不变测试。手册写明 Linux/rootless/配额/网络前置、供应商支持矩阵、单次邀请登录、令牌轮换、升级/drain 和恢复演练。
- [ ] **Step 5:** 运行 operations 测试、文档路径检查，提交 docs(multiuser): document deployment recovery and explicit migration。

### Task 14: 双用户端到端与发布门槛

**Files:**

```text
Create: tests/multiuser/test_two_users.py
Create: tests/integration/multiuser/{conftest,test_two_users_live,test_browser_live,test_restore_live}.py
Create: scripts/check_multiuser_release.py
Modify: tests/fixtures/harness_rpc.py
Modify: pytest.ini
Modify: deploy/multiuser/README.md
```

**Interfaces:**

- tests/integration/multiuser/conftest.py 注册 --multiuser-live 和 multiuser_integration marker；常规测试不运行真实外部服务，显式 --multiuser-live 时缺必要环境即失败，不 skip。
- 控制 URL、临时 A/B 邀请从 EASEL_TEST_CONTROL_URL、EASEL_TEST_INVITE_A/B 读取；测试凭证用本地 CLI 为专用临时工作区生成，不从开发者账号复制。
- check_multiuser_release.py 先验 Linux/Docker/rootless资源约束与所需测试环境，再运行真实门槛，并对 marker 测试的 skip/xfail 返回失败。输出只有状态和脱敏 workspace/run ID。
- 假 Harness 新增多人测试 mode，保留用户已有 fixture 修改；能确定返回相同 native ID，持久化及记录 cwd/显式 home（不记录完整 env）。

- [ ] **Step 1:** 创建完整 control+两独立 worker 假 Harness 集成；通过真实 HTTP 登录、建会话、发 prompt、SSE、产物下载验证 A/B。真实环境最小拒绝用例如下：

```python
import os
import httpx
import pytest

@pytest.mark.multiuser_integration
def test_b_cannot_read_a_conversation_live():
    base = os.environ["EASEL_TEST_CONTROL_URL"]
    origin = {"Origin": base.rstrip("/")}
    with httpx.Client(base_url=base) as a, httpx.Client(base_url=base) as b:
        for client, name in ((a, "A"), (b, "B")):
            login = client.post("/api/auth/exchange", headers=origin,
                                json={"token": os.environ[f"EASEL_TEST_INVITE_{name}"]})
            assert login.status_code == 200
        me = a.get("/api/auth/me").json()
        created = a.post("/api/sessions", json={"title": "A private"},
                         headers={**origin, "X-CSRF-Token": me["csrfToken"]})
        assert created.status_code == 201
        session_id = created.json()["id"]
        response = b.get(f"/api/sessions/{session_id}/messages")
        assert response.status_code == 404
```

单次邀请不能被多个 live 测试重复兑换；session-scoped fixture 每用户仅兑换一次，后续测试共享受隔离的 cookie jar，或由专用本地 CLI 为每测试生成新的邀请。上面用例只表达完整 HTTP 契约，实际整套测试采用前一方案且不跨并行进程共用 fixture。

- [ ] **Step 2:** 先运行假 Harness 集成，验证故意移除 workspace 过滤/请求作用域时测试会失败；恢复实现后全部通过。不能只断言 HTTP 200，还要检查文件、原生绑定、SSE 内容和调用次数。
- [ ] **Step 3:** Linux 真实 DSH ACP 测试创建→连续追问→worker 回收→续接；真实浏览器测 Cookie 隔离、持久 profile 串行、只读技能、CSP 预览；检查 worker env/挂载/配置/日志不存在管理员 sentinel Key。平台登录使用临时账号或测试站，不要求自动发布真实内容。
- [ ] **Step 4:** 实际容器资源/网络测试、SSE 撤销时间、冷启动竞争、磁盘额度、重启中断不重复执行、管理员配置变化任务快照、备份到新目录恢复全部通过。供应商逐 adapter 运行 broker 契约；不能用 OpenAI-compatible 测试推断所有供应商通过。
- [ ] **Step 5:** 执行下方回归和门槛，修复真实失败后提交 test(multiuser): verify two-user isolation and recovery。保存命令、日期、实际版本与结果到部署手册验收记录，未执行项目明确写“未执行”，不能填通过。

## 最终验证命令

普通单元与既有回归，Windows/Linux 均可运行：

```powershell
python -m pytest tests/multiuser -q
python -m pytest tests/test_agent_harnesses.py tests/test_harness_workspace.py tests/test_runtime_env.py tests/test_local_write_guard.py tests/test_web_security.py tests/test_web_model_config_parity.py tests/test_settings_stale_mirror.py tests/test_chat_question_replay.py tests/test_document_paths.py -q
node --test tests/frontend/*.test.mjs
python scripts/validate_document_paths.py
```

在 web/frontend 目录单独运行（不与其他命令串联）：

```powershell
npm run build
```

Linux 受控测试服务器，先用管理 CLI 为临时工作区生成必要配置，再运行：

```bash
python scripts/check_multiuser_release.py
python -m pytest tests/integration/multiuser --multiuser-live -m multiuser_integration -q
```

单元回归完成且真实门槛通过后，再执行一次项目范围 python -m pytest -q；若被已存在的外部依赖/旧失败阻塞，记录准确失败及基线对比，不能声称全套通过。不重复运行与改动无关的重型测试来替代缺失的真实隔离验收。

## Spec 覆盖映射

| Spec | 主任务 | 验收 |
|---|---|---|
| §1–3 目标、现状、兼容 | 6、12、13、14 | A16 |
| §4 执行与网络边界 | 3、5、8、14 | A03、A12、A13 |
| §5 资源与认证 | 1、2、3、10 | A01、A02、A03、A05 |
| §6 文件与生命周期 | 6、7、8、13 | A04、A09、A11、A12、A15、A17 |
| §7 会话、幂等、SSE | 1、9、10、11 | A04、A05、A10、A11、A14 |
| §8 默认、覆盖、秘密、broker | 4、5、6、12 | A06、A07、A08、A13、A14 |
| §9 接口与界面 | 3、7、10、11、12 | A03、A05、A06、A10、A15 |
| §10 错误、恢复、运维 | 1、8、10、13、14 | A11、A14、A17 |
| §11 全部验收 | 14 汇总真实证据 | A01–A17 |

## 文档自审与执行交接

自审要求：核对上述接口签名、14 项任务依赖、全部 A01–A17、五项 Review Focus 和 local 兼容；检查不存在未定义的核心依赖、未说明的秘密回退或自动跨用户迁移。参考代码只出现在文档，不提前创建产品模块。

建议由当前会话顺序实施：配置继承、provider broker、worker 身份和 run 生命周期共享契约较多，顺序执行便于在每个切片做实际集成。若用户选择子代理方式，再按 subagent-driven-development 分配任务与独立审查；不得仅因为计划提及代理就自行启动。

书面 spec/plan 审阅后，用户选择当前会话顺序执行或子代理分任务执行，再进入实现。此处的选择发生在文档已完整交付之后，不影响本次撰写任务完成。
