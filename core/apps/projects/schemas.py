from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


type ProjectKind = Literal["factor", "model", "optimize", "control", "execution"]


class ProjectEdit(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=200)

    @field_validator("name")
    @classmethod
    def valid_directory_name(cls, value: str) -> str:
        if value.startswith(".") or any(character in '/\\<>:"|?*' or ord(character) < 32 for character in value) or value.endswith("."):
            raise ValueError("项目名不能以点开头或结尾，也不能包含路径分隔符和特殊文件名字符")
        return value


class ProjectCreate(ProjectEdit):
    kind: ProjectKind
    scheme_version: str = Field(min_length=1, max_length=128)
    algo_version: str = Field(min_length=1, max_length=128)


class TemplateVersion(BaseModel):
    tag: str
    commit: str
    version: str | None = None


class ProjectRead(BaseModel):
    id: UUID
    name: str
    description: str
    kind: ProjectKind
    schemeVersion: str | None
    schemeCommit: str | None
    algoVersion: str
    algoCommit: str
    createdAt: datetime
    updatedAt: datetime
    archived: bool
    directory: str
    versions: list[dict[str, object]] = Field(default_factory=list)
