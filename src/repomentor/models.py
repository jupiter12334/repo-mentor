"""规范、模型回复与验收结果的数据结构。"""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Rule:
    id: str
    repo_id: str
    version: int
    requirement: str
    paths: list[str] = field(default_factory=lambda: ["**"])
    condition: str = ""
    exceptions: str = ""
    source: str = "人工录入"
    status: str = "candidate"
    mandatory: bool = False
    checker: dict[str, Any] | None = None
    kind: str = "requirement"
    parent_id: str | None = None
    parent_version: int | None = None
    relations: list[dict] = field(default_factory=list)
    provenance: dict | None = None

    @classmethod
    def from_dict(cls, data: dict):
        """读取旧规范时忽略已废弃字段，保留规范正文与版本。"""
        return cls(**{key: value for key, value in data.items() if key in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Reply:
    content: list[dict]
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    displayed_text: str = ""


@dataclass
class Selection:
    selected: list[str]
    uncertain: list[str]
    backend: str
    milliseconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_reason: str | None = None
    cached: bool = False
