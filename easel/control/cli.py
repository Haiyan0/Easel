"""本地管理 CLI：管理员引导、邀请签发与 control 服务启动。

这些命令只操作显式传入的控制目录，不读取开发者 .env、HOME 或平台凭证；
管理员引导与邀请都不存在未认证的远程等价接口。
"""
from __future__ import annotations

import sys
from pathlib import Path

import uvicorn

from .app import ControlConfig, create_app
from .auth import AuthService
from .broker import BrokerService
from .config import ConfigService, load_master_key
from .egress import EgressGateway, load_policy
from .proxy import BrokerDestination, PinnedRunConfig, WorkerRelay
from .runtime import UnixProvisionerClient
from .snapshot import load_managed_agent_config, load_signing_key
from .store import Store
from .types import DomainError

CONTROL_DB_NAME = "control.sqlite3"


def _control_db(control_dir: str) -> Path:
    return Path(control_dir) / CONTROL_DB_NAME


def _report(error: DomainError) -> int:
    """只输出错误码，不回显令牌或内部细节。"""
    print(f"错误：{error.code}", file=sys.stderr)
    return 1


def build_relay_factory(*, signing_key, revision: int, agent: dict,
                        broker_destination: BrokerDestination):
    """正式 control 装配：接纳后为每个 Run 签发最小 broker token。

    该函数被 CLI 与装配测试共同使用；不得在测试里用自造 provider 另起
    一条与生产不同的签发路径。
    """
    def pinned_config(_actor):
        return PinnedRunConfig(
            revision=revision, routes={}, agent=agent,
            broker=broker_destination)

    def relay_factory(runtime, sessions, auth, store, http_client,
                      broker, db_runner):
        return WorkerRelay(
            runtime, sessions, auth, store=store,
            signing_key=signing_key, config_provider=pinned_config,
            http_client=http_client, broker=broker,
            db_runner=db_runner)

    return relay_factory


def cmd_admin_bootstrap(args) -> int:
    """引导首个管理员；邀请明文只在本命令输出一次。"""
    try:
        with Store.open(_control_db(args.control_dir)) as store:
            identity, invite = AuthService(store).bootstrap_admin(args.name)
    except DomainError as error:
        return _report(error)
    print(f"admin_user_id={identity.user_id}")
    print(f"workspace_id={identity.workspace_id}")
    print(f"invite={invite}")
    return 0


def cmd_admin_invite(args) -> int:
    """管理员为现有用户签发一次性邀请；角色由数据库复查。"""
    try:
        with Store.open(_control_db(args.control_dir)) as store:
            admin = store.get_identity(args.admin_user_id)
            invite = AuthService(store).create_invite(
                admin, args.user_id, args.ttl_seconds)
    except DomainError as error:
        return _report(error)
    print(f"invite={invite}")
    return 0


def cmd_serve_control(args) -> int:
    """启动 control HTTP 服务；应用在 lifespan 中管理自己的数据库线程。"""
    try:
        # Origin 必须在打开控制库、启动 HTTP 之前校验失败
        config = ControlConfig(public_origin=args.public_origin)
    except ValueError:
        print("错误：invalid_origin", file=sys.stderr)
        return 2
    try:
        master_key = load_master_key(args.master_key_file)
    except OSError:
        print("错误：master_key_unavailable", file=sys.stderr)
        return 2
    except ValueError:
        print("错误：invalid_master_key", file=sys.stderr)
        return 2

    # 逐 Run 签名密钥与受管 Agent 配置必须成对显式给出；只给其一视为
    # 配置错误而不是静默降级。两者都不给时保持任务入口 503。
    snapshot_key_file = getattr(args, "snapshot_key_file", None)
    managed_config_file = getattr(args, "managed_agent_config", None)
    signing_key = None
    managed_agent = None
    if bool(snapshot_key_file) != bool(managed_config_file):
        print("错误：relay_config_incomplete", file=sys.stderr)
        return 2
    if snapshot_key_file:
        try:
            signing_key = load_signing_key(snapshot_key_file)
        except OSError:
            print("错误：snapshot_key_unavailable", file=sys.stderr)
            return 2
        except ValueError:
            print("错误：snapshot_key_invalid", file=sys.stderr)
            return 2
        try:
            managed_agent = load_managed_agent_config(managed_config_file)
        except OSError:
            print("错误：managed_config_unavailable", file=sys.stderr)
            return 2
        except ValueError:
            # 不回显字段值：配置中可能误放管理员凭证
            print("错误：managed_config_invalid", file=sys.stderr)
            return 2

    relay_factory = None
    if signing_key is not None:
        revision, agent, broker_config = managed_agent
        try:
            destination = BrokerDestination(
                broker_config["url"], broker_config["channel"])
        except (KeyError, ValueError):
            print("错误：managed_config_invalid", file=sys.stderr)
            return 2
        relay_factory = build_relay_factory(
            signing_key=signing_key, revision=revision, agent=agent,
            broker_destination=destination)

    # 出口策略必须在打开控制库、启动 HTTP 之前校验失败
    policy_path = getattr(args, "egress_policy", None)
    egress_proxy = getattr(args, "egress_proxy", None)
    if policy_path is not None:
        try:
            policy = load_policy(policy_path)
        except ValueError:
            print("错误：egress_policy_invalid", file=sys.stderr)
            return 2
    else:
        # 未显式提交策略时保留“未配置”状态；没有 proxy 或策略都必须
        # 在 broker 联网前返回 503，不能构造隐式默认策略冒充管理员配置。
        policy = None
    try:
        egress = EgressGateway(policy=policy, proxy_url=egress_proxy)
    except ValueError:
        print("错误：egress_proxy_invalid", file=sys.stderr)
        return 2

    # 受限 provisioner 必须可达才启动多人运行时；缺失时失败关闭，
    # 不回退到进程内 Docker 或“无隔离”模式。
    provisioner = None
    provisioner_socket = getattr(args, "provisioner_socket", None)
    if provisioner_socket:
        if not Path(provisioner_socket).exists():
            print("错误：provisioner_unavailable", file=sys.stderr)
            return 2
        provisioner = UnixProvisionerClient(provisioner_socket)

    # 固定前端构建目录：显式给出时必须存在，否则回退任意服务器路径是禁止的
    frontend_dir = getattr(args, "frontend_dir", None)
    if frontend_dir is not None:
        build = Path(frontend_dir).resolve()
        if not build.is_dir() or not (build / "index.html").is_file():
            print("错误：frontend_build_missing", file=sys.stderr)
            return 2

    store = Store.connect(_control_db(args.control_dir))
    try:
        try:
            # 已有密文与 key 不匹配必须在服务启动前明确失败
            config_service = ConfigService(store, master_key,
                                           egress_policy=policy)
        except ValueError:
            print("错误：invalid_master_key", file=sys.stderr)
            return 2
        broker = BrokerService(store, config_service, egress=egress)
        app = create_app(store=store, auth=AuthService(store), config=config,
                         config_service=config_service, broker=broker,
                         provisioner=provisioner, frontend_dir=frontend_dir,
                         relay_factory=relay_factory)
    finally:
        # 应用会在 lifespan 中按线程边界重新打开连接
        store.close()
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False)
    return 0
