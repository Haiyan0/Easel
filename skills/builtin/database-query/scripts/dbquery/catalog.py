"""project-docs 目录读取与业务文档路径约束。

只接受显式配置的 ``project_docs_root/references/projects.yaml``；项目只按精确 ID
解析，别名仅用于人工匹配。业务文档必须位于该根目录内，拒绝绝对路径、路径穿越与
符号链接逃逸。
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from dbquery.errors import DomainError

logger = logging.getLogger(__name__)

ProjectId = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9-]{2,63}$")]
ConnectionId = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_-]{2,63}$")]
Text = Annotated[str, StringConstraints(strict=True, min_length=1)]


class _UniqueLoader(yaml.SafeLoader):  # type: ignore[misc]  # PyYAML 未提供类型标注
    """拒绝重复键，避免后一个值静默覆盖数据库绑定。"""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError("重复配置键")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


class ProjectEntry(BaseModel):
    """精确项目绑定；空绑定不会触发自动选库。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ProjectId
    company: Text
    name: Text
    aliases: tuple[Text, ...]
    document: Text
    connection_id: ConnectionId | None

    @field_validator("document")
    @classmethod
    def validate_document(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value.startswith("references/")
            or not value.endswith(".md")
            or "\\" in value
            or ":" in value
            or path.as_posix() != value
            or any(part in (".", "..") for part in value.split("/"))
        ):
            raise ValueError("业务文档路径无效")
        return value


class ProjectCatalog(BaseModel):
    """版本化目录；别名可重名，项目标识不可重名。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    projects: tuple[ProjectEntry, ...]

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("目录版本无效")
        return value

    @model_validator(mode="after")
    def unique_ids(self) -> "ProjectCatalog":
        ids = [project.id for project in self.projects]
        if len(set(ids)) != len(ids):
            raise ValueError("项目标识重复")
        return self


def load_catalog_text(text: str) -> ProjectCatalog:
    """失败不回显配置值或解析器诊断。"""
    try:
        return ProjectCatalog.model_validate(yaml.load(text, Loader=_UniqueLoader))
    except (yaml.YAMLError, ValidationError, ValueError, TypeError, RecursionError):
        raise _unavailable() from None


class ProjectDirectory:
    """一个显式配置的 project-docs 根目录。

    每次调用重新读取目录文件，不缓存部署快照；文件不可读、校验失败或资料缺失
    一律 fail-closed。
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def list(self) -> tuple[ProjectEntry, ...]:
        return self._load().projects

    def resolve(self, project_id: str) -> ProjectEntry:
        for project in self._load().projects:
            if project.id == project_id:
                return project
        raise DomainError("project.not_found", "项目不存在或未建档", status=404)

    def document_path(self, project: ProjectEntry) -> Path:
        """返回经真实路径核验的业务文档绝对路径。"""
        root = self._root.resolve()
        try:
            candidate = (root / project.document).resolve()
        except OSError:
            raise _unavailable() from None
        if not candidate.is_relative_to(root) or not candidate.is_file():
            logger.warning("业务文档不在受信目录内或不可读：project=%s", project.id)
            raise _unavailable()
        return candidate

    def _load(self) -> ProjectCatalog:
        path = self._root / "references" / "projects.yaml"
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning("项目目录不可读：%s", path)
            raise _unavailable() from None
        return load_catalog_text(text)


def _unavailable() -> DomainError:
    return DomainError("project.config_unavailable", "项目配置不可用", status=503)
