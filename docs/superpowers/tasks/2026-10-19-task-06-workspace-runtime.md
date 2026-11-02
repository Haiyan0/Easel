# Task 6：分开代码根、数据根和管理员 ACP 配置

日期：2026-10-19（Asia/Shanghai）。状态：待外部代码 Agent 执行；完成后停止等待审计，不进入 Task 7。

前置：[Task 5 已通过](../audits/2026-10-19-task-05-accepted.md)，基线 5497e40f7144d198d937e1a9006d3eebb754873c。

依据：[spec](../specs/2026-10-16-multiuser-workspaces-design.md) 第 4、6、7、8 节与[plan](../plans/2026-10-16-multiuser-workspaces.md) Global Constraints、Task 6。沿用批准设计，本任务切实接入路径和受管启动配置，不扩展为完整容器/会话服务。

## 环境与范围

先读 AGENTS.md、CLAUDE.md。PowerShell，C:\Code\Easel，C:\Users\hyh\anaconda3\envs\Agent\python.exe。中文说明、代码注释及提交信息。开始记录当前分支、HEAD、git status 和目标 diff；保留现有 ACP 与其他修改，不 reset/checkout 覆盖，不使用 git add .，只按显式路径提交本任务文件，不夹带审计/任务单。

```text
Create: easel/worker/context.py
Create: easel/worker/config.py
Create: ./skills/shared/scripts/runtime_paths.py
Create: tests/multiuser/test_workspace_paths.py
Optional split: tests/multiuser/test_worker_snapshot.py, test_worker_runtime.py
Modify: web/app.py
Modify: easel/harnesses/base.py, commands.py, workspace.py, rpc.py
Modify: easel/runtime_env.py
Modify: tests/test_harness_workspace.py
As needed: easel/control/* 的最小快照签名/受管配置装配与相关测试
As needed: ACP 调用方与私有 RPC 关闭调用方的必要参数传递
```

本轮不实现 Task 7 的浏览器 profile/全部技能写状态、Task 8 容器 provisioner、Task 9 完整 SessionService 或 Task 10 relay/SSE。为签名快照的验证和不重复启动需要的最小 worker 接纳记录属于本任务，不以未实现后续任务为由省略安全校验。

## 接口与职责

```python
resolve_paths(env: Mapping[str, str], default_code_root: Path) -> WorkspacePaths
# WorkspacePaths(code_root, workspace_root, state_root, home) 已存在

WorkerConfigSnapshot(workspace_id, run_id, revision, routes, agent)
build_run_env(paths: WorkspacePaths, snapshot: WorkerConfigSnapshot,
              base_env: Mapping[str, str]) -> dict[str, str]

# RunRequest 在原有字段末尾增加带默认值的字段，保留已有调用兼容
workspace_id: str = ""
code_root: Path | None = None

acp_command(cwd: Path, *,
            managed_command: tuple[str, ...] | None = None) -> tuple[str, ...]
```

env 是 worker 启动配置，snapshot 是已通过签名/归属/时间校验的服务端配置，不接受浏览器任意替代。明确签名 envelope、签名/验签函数、worker 公钥配置和 admission 接口的实际名称，写入交付报告，供后续 runtime/session/relay 直接复用。

## 实现步骤与验收

- [ ] **1. 先红。** 按 plan 的 worker 缺私有根测试写起；运行 test_workspace_paths.py，记录失败。随后为签名、无宿主秘密、伪造 ACP 文件、同名 session namespace 和 token 轮换续接补红测试，再做最小实现。测试只用临时目录/密钥/假 Harness，不读取或改写开发者真实 HOME/.env/ACP/平台账号。

- [ ] **2. 显式路径。** 默认 local 保持现有工作目录与行为；worker 必须显式提供 EASEL_CODE_ROOT、EASEL_WORKSPACE_ROOT、EASEL_STATE_ROOT 与本人 HOME，缺失、非法或指回不允许的源码/宿主默认路径明确失败，不静默回退 local。worker 容器内目标分别为 /opt/easel、/workspace、/home/easel；本机测试用临时绝对路径。

  EASEL_ROOT 在 worker 只兼容指向本人数据根。代码/内置技能/agents 规则按 code_root 读取，outputs/profiles 等内容按 workspace_root，运行状态按 state_root，本人缓存按 home。不要用进程 chdir 或修改宿主全局 os.environ 模拟多个请求的私有路径。paths helper 与 web/harness 实际调用接线，不能只提供未使用函数。

- [ ] **3. 干净的运行环境。** build_run_env 从明确允许的运行变量构建，不复制完整 base_env/os.environ；只保留必要 PATH/编码/受管代理等安全变量，再注入本人路径和当前 Run 范围的 broker token/内部地址。对大小写不同的秘密变量同样处理，Windows 下不可借大小写保留宿主 Key。

  HOME、DSH_HOME、DSH_AGENTS_HOME 必须显式设置：DSH_HOME 为本人 Harness 状态，DSH_AGENTS_HOME 为受管代码/规则目录。不得回填宿主 .env、NPM/Harness 默认 HOME 或管理员模型 Key。现有 runtime_env 的安装发现/Path.home 逻辑在 local 保留，worker 不能借它重新扫描宿主目录或恢复秘密。命令解析应使用可信受管可执行入口/运行 PATH，而非浏览器或可写工作区提供的启动器。

- [ ] **4. 签名快照。** control 使用 Ed25519 私钥签署完整、确定性编码的快照；worker 仅加载受保护公钥。签名内容绑定 workspace_id、run_id、revision、routes、agent、有效期及 admission nonce；变更任一字段、过期、错误公钥、错误 workspace/run/version 都拒绝，不能靠本人 worker 服务 token 代替签名。

  worker 的命令、主模型、sandbox、自动批准与工具策略来自签名 agent，不能由请求 env/argv 或可写 outputs 文件覆盖。秘密仍仅 broker token，签名包中不得带管理员原始 Key。私钥不进入 worker env/挂载/日志；缺必需配置失败关闭，认证-only control 测试可不装配本功能。

  同一 run/nonce 的重放不重复启动：最小接纳记录需原子、可持久化，重复合法快照返回原接纳/执行状态；篡改重用冲突拒绝。用并发假执行计数证明仅启动一次，不提前构建完整 Task 10 作业系统。

- [ ] **5. 受管 ACP 实际接线。** worker 必须将验签后的 managed command 传到 acp_command/RunRequest.command；worker 缺受管 command 明确失败，即使 outputs/_harness/acp.json 存在也不读取它。local 继续支持当前文件配置。保留参数数组、控制字符及 Windows .cmd/.bat 等已有启动保护，不使用 shell 拼接。

  用临时恶意 ACP 文件和 sentinel 可执行程序验证 worker 始终使用受管命令，不能只单测 acp_command 参数而未验证 web 到 Harness 的真实传递。已获得合法服务 token 的请求仍不能自行换 command/runtime/自动批准策略。

- [ ] **6. 指令与进程池。** harness_instructions 分清代码技能/共享脚本/规则与本人 outputs/profiles；Python/脚本入口按受管代码环境解析，不再假设每个数据目录有一份源码/.venv/.env。local 文案/路径行为保持。

  RPC 池 key 增加 workspace namespace，保留每会话私有 stdio。关闭、异常清理、空闲淘汰和 timer 必须按完整 namespace 选择，不能只改创建 key 后仍按裸 session_key 关闭。用 A/B 相同 session_key 和 native_id 证明进程、锁和关闭互不影响；local 兼容调用保持。

  token 或配置变化触发进程重建时必须 resume 原 native_id，不把短期 token 扩成长期共用凭证以维持进程复用。用假 Harness 记录实际 cwd/显式 HOME/命令/namespace 和连续续接结果，不记录完整 env；既有 ACP 追问与真实协议 fixture 保留。

- [ ] **7. 最终本地验证。** 路径、环境、签名、重放、ACP 命令选择、进程池隔离测试通过后，运行下列回归及 diff 检查。已有失败须精确基线对比，不能全部归为环境问题。只在有环境且明确配置时做原生 DSH/容器门槛；未执行如实记录，不用假 Harness 声称只读挂载或真实续接已验收。

```powershell
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/multiuser -q
& 'C:\Users\hyh\anaconda3\envs\Agent\python.exe' -m pytest tests/test_harness_workspace.py tests/test_agent_harnesses.py tests/test_runtime_env.py tests/test_local_write_guard.py tests/test_web_security.py tests/test_web_model_config_parity.py tests/test_settings_stale_mirror.py -q
git diff --check
```

## 交付与停止点

显式路径、中文信息提交，报告父 SHA/新 SHA、文件清单、红绿/实际回归计数、path/env 映射、签名 envelope 与公钥加载方式、nonce 接纳记录、worker 真实调用路径、token 轮换仍续接的证据及未执行门槛。不修改旧 schema；如最小持久化需要新控制表，追加 migration 并补旧库保留回归。

本机验证通过后停止等待审计，不进入 Task 7；不宣称已完成容器只读挂载、进程资源配额、真实 DSH 或多用户发布门槛。
