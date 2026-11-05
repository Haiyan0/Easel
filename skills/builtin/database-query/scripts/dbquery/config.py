"""版本化本地配置、路径解析与显式 env 文件。

配置里只登记环境变量名，不登记秘密；相对路径一律相对于配置文件所在目录解析。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from dbquery.errors import DomainError
from dbquery.types import (
    ENV_NAME_PATTERN,
    EnvName,
    Identifier,
    SchemaName,
    SourceId,
)

logger = logging.getLogger(__name__)

_ENV_KEY_RE = re.compile(ENV_NAME_PATTERN)
_MAX_ENV_BYTES = 64 * 1024

Text = Annotated[str, StringConstraints(strict=True, min_length=1)]


class _UniqueLoader(yaml.SafeLoader):  # type: ignore[misc]  # PyYAML 未提供类型标注
    """拒绝重复键，避免后一个值静默覆盖连接绑定。"""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError("重复配置键")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


class SourceConfig(BaseModel):
    """单条数据源记录：只登记环境变量名、白名单与部署检查状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SourceId
    secret_env: EnvName
    allowed_schema: SchemaName
    allowed_tables: tuple[Identifier, ...] = Field(min_length=1)
    readonly_account_verified: bool
    enabled: bool


class AppConfig(BaseModel):
    """本地运行配置（``version: 1``，未知字段拒绝）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    project_docs_root: Text
    state_dir: Text = "./state"
    output_dir: Text = "./outputs"
    sources: tuple[SourceConfig, ...] = ()

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("配置版本无效")
        return value

    @model_validator(mode="after")
    def unique_source_ids(self) -> "AppConfig":
        ids = [source.id for source in self.sources]
        if len(set(ids)) != len(ids):
            raise ValueError("数据源 ID 重复")
        return self

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        """读取并严格校验配置；任何失败统一 ``config.unavailable``，不回显取值。"""
        try:
            raw = path.read_bytes()
        except OSError:
            logger.warning("本地配置不可读：%s", path)
            raise _invalid("unreadable") from None
        try:
            data = yaml.load(raw, Loader=_UniqueLoader)
        except (yaml.YAMLError, ValueError, RecursionError):
            logger.warning("本地配置无法解析：%s", path)
            raise _invalid("unparsable") from None
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            logger.warning("本地配置校验失败：%s（%s）", path, _describe(exc))
            raise _invalid(_describe(exc)) from None

    def get(self, source_id: str) -> SourceConfig | None:
        for source in self.sources:
            if source.id == source_id:
                return source
        return None


@dataclass(frozen=True, slots=True)
class AppPaths:
    """配置文件的解析结果：所有路径都已绝对化。"""

    config_path: Path
    project_docs_root: Path
    state_dir: Path
    output_dir: Path


def resolve_paths(config: AppConfig, config_path: Path) -> AppPaths:
    """把配置里的相对路径按配置文件目录解析；项目目录必须真实存在。"""
    resolved_config = config_path.resolve()
    base = resolved_config.parent
    root = _resolve(base, config.project_docs_root)
    if not root.is_dir():
        logger.warning("项目文档根目录不存在或不可用：%s", root)
        raise DomainError("project.config_unavailable", "项目配置不可用", status=503)
    return AppPaths(
        config_path=resolved_config,
        project_docs_root=root,
        state_dir=_resolve(base, config.state_dir),
        output_dir=_resolve(base, config.output_dir),
    )


def load_env_file(path: Path) -> dict[str, str]:
    """读取显式指定的 KEY=VALUE 文件：字面值、不展开、不执行文件内容。

    只做最小解析：忽略空行与 ``#`` 注释，去掉成对的外层引号，保留值中的 ``=``。
    重复键、非法键名与不可读文件统一 ``config.unavailable``。
    """
    try:
        size = path.stat().st_size
        if size > _MAX_ENV_BYTES:
            raise _invalid("env_too_large")
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise _invalid("env_unreadable") from None
    except UnicodeDecodeError:
        raise _invalid("env_unparsable") from None

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise _invalid("env_malformed")
        key, value = line.split("=", 1)
        key = key.strip()
        if _ENV_KEY_RE.fullmatch(key) is None:
            raise _invalid("env_key_invalid")
        if key in values:
            raise _invalid("env_duplicate_key")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _resolve(base: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _describe(exc: ValidationError) -> str:
    """只报字段路径与错误类型，不回显字段取值。"""
    return "；".join(
        f"{'.'.join(str(part) for part in error['loc'])}({error['type']})" for error in exc.errors()
    )


def _invalid(detail: str) -> DomainError:
    return DomainError("config.unavailable", f"本地配置不可用（{detail}）", status=503)
