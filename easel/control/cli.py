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
from .config import ConfigService, load_master_key
from .store import Store
from .types import DomainError

CONTROL_DB_NAME = "control.sqlite3"


def _control_db(control_dir: str) -> Path:
    return Path(control_dir) / CONTROL_DB_NAME


def _report(error: DomainError) -> int:
    """只输出错误码，不回显令牌或内部细节。"""
    print(f"错误：{error.code}", file=sys.stderr)
    return 1


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

    store = Store.connect(_control_db(args.control_dir))
    try:
        try:
            # 已有密文与 key 不匹配必须在服务启动前明确失败
            config_service = ConfigService(store, master_key)
        except ValueError:
            print("错误：invalid_master_key", file=sys.stderr)
            return 2
        app = create_app(store=store, auth=AuthService(store), config=config,
                         config_service=config_service)
    finally:
        # 应用会在 lifespan 中按线程边界重新打开连接
        store.close()
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False)
    return 0
