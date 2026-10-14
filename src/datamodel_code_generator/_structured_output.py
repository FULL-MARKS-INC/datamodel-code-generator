"""Structured JSON output payloads for the CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.json_schema import SkipJsonSchema

from datamodel_code_generator.prompt import PromptPayload

if TYPE_CHECKING:
    from pydantic.json_schema import JsonDict

JSON_SCHEMA_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


class GeneratedFilePayload(BaseModel):
    """One generated file emitted by --output-format json."""

    model_config = ConfigDict(extra="forbid")

    path: str | None
    content: str


_Absent: TypeAlias = SkipJsonSchema[None]
_TARGET_DESCRIPTION = "The package and runtime dependencies of a server or client generation; absent for models only."


def _absent_when_unset(schema: JsonDict) -> None:
    """Leave the default out of an optional key's schema: the payload omits the key rather than printing null."""
    schema.pop("default")


class TargetPayload(BaseModel):
    """The package a server or client generation renders and the requirements it needs at run time."""

    model_config = ConfigDict(extra="forbid")

    package: str
    dependencies: list[str]


class GenerationPayload(BaseModel):
    """Structured JSON payload emitted by generation mode --output-format json."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    format: Literal["json"]
    kind: Literal["generation"]
    output: str | None
    files: list[GeneratedFilePayload]
    target: TargetPayload | _Absent = Field(
        default=None, description=_TARGET_DESCRIPTION, json_schema_extra=_absent_when_unset
    )


CommandOutputKind: TypeAlias = Literal[
    "pyproject-config",
    "cli-command",
    "deprecations",
    "experimental",
]


class PyprojectConfigOutputPayload(BaseModel):
    """Structured JSON payload emitted by --generate-pyproject-config."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    format: Literal["json"]
    kind: Literal["pyproject-config"]
    content: str
    config: dict[str, Any]


class CliCommandOutputPayload(BaseModel):
    """Structured JSON payload emitted by --generate-cli-command."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    format: Literal["json"]
    kind: Literal["cli-command"]
    content: str
    config: dict[str, Any]
    arguments: list[str]


class DeprecationItemPayload(BaseModel):
    """One deprecation entry emitted by --list-deprecations --output-format json."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    target: str
    message: str
    warning_since: str
    removal_version: str | None
    replacement: str | None
    status: str
    warning_category: str
    note: str | None


class DeprecationsOutputPayload(BaseModel):
    """Structured JSON payload emitted by --list-deprecations."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    format: Literal["json"]
    kind: Literal["deprecations"]
    content: str
    items: list[DeprecationItemPayload]


class ExperimentalItemPayload(BaseModel):
    """One experimental feature entry emitted by --list-experimental --output-format json."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    target: str
    message: str
    since_version: str
    tracking_issue: str | None
    note: str | None


class ExperimentalOutputPayload(BaseModel):
    """Structured JSON payload emitted by --list-experimental."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    format: Literal["json"]
    kind: Literal["experimental"]
    content: str
    items: list[ExperimentalItemPayload]


class CheckDifferencePayload(BaseModel):
    """One generated-output difference emitted by --check or --diff-against."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["changed", "missing", "extra", "added", "removed"]
    path: str
    message: str | None = None
    diff: str | None = None


class CheckOutputPayload(BaseModel):
    """Structured JSON payload emitted by generated-output comparison commands."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    format: Literal["json"]
    kind: Literal["check", "input-diff"]
    success: bool
    content: str
    differences: list[CheckDifferencePayload]
    target: TargetPayload | _Absent = Field(
        default=None, description=_TARGET_DESCRIPTION, json_schema_extra=_absent_when_unset
    )


class BatchJobPayload(BaseModel):
    """One named job result emitted by batch generation."""

    model_config = ConfigDict(extra="forbid")

    name: str
    result: GenerationPayload | CheckOutputPayload


class BatchOutputPayload(BaseModel):
    """Structured JSON payload emitted by --job/--all-jobs with --output-format json."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    format: Literal["json"]
    kind: Literal["batch"]
    jobs: list[BatchJobPayload]


CommandOutputPayload: TypeAlias = (
    PyprojectConfigOutputPayload | CliCommandOutputPayload | DeprecationsOutputPayload | ExperimentalOutputPayload
)
StructuredOutputPayload: TypeAlias = Annotated[
    GenerationPayload
    | PyprojectConfigOutputPayload
    | CliCommandOutputPayload
    | DeprecationsOutputPayload
    | ExperimentalOutputPayload
    | CheckOutputPayload
    | BatchOutputPayload
    | PromptPayload,
    Field(discriminator="kind"),
]

_DEPRECATION_ITEMS_ADAPTER = TypeAdapter(list[DeprecationItemPayload])
_EXPERIMENTAL_ITEMS_ADAPTER = TypeAdapter(list[ExperimentalItemPayload])
_STRUCTURED_OUTPUT_ADAPTER = TypeAdapter(StructuredOutputPayload)


def _dump_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _dump_payload(payload: GenerationPayload | CheckOutputPayload) -> str:
    """Dump a payload, leaving out the target of a model-only run."""
    return _dump_json(payload.model_dump(mode="json", exclude={"target"} if payload.target is None else None))


def _schema_with_metadata(schema: dict[str, Any], *, title: str, description: str) -> dict[str, Any]:
    schema = dict(schema)
    schema.setdefault("title", title)
    schema.setdefault("description", description)
    return {"$schema": JSON_SCHEMA_DRAFT_2020_12, **schema}


def generation_output_json(
    files: list[GeneratedFilePayload], *, output: str | None, target: TargetPayload | None = None
) -> str:
    return _dump_payload(
        GenerationPayload(version=1, format="json", kind="generation", output=output, files=files, target=target)
    )


def command_output_json(
    kind: CommandOutputKind,
    content: str,
    *,
    config: dict[str, Any] | None = None,
    items: list[dict[str, Any]] | None = None,
    arguments: list[str] | None = None,
) -> str:
    payload: CommandOutputPayload
    if kind == "pyproject-config":
        payload = PyprojectConfigOutputPayload(
            version=1,
            format="json",
            kind=kind,
            content=content,
            config=config or {},
        )
    elif kind == "cli-command":
        payload = CliCommandOutputPayload(
            version=1,
            format="json",
            kind=kind,
            content=content,
            config=config or {},
            arguments=arguments or [],
        )
    elif kind == "deprecations":
        payload = DeprecationsOutputPayload(
            version=1,
            format="json",
            kind=kind,
            content=content,
            items=_DEPRECATION_ITEMS_ADAPTER.validate_python(items or []),
        )
    else:
        payload = ExperimentalOutputPayload(
            version=1,
            format="json",
            kind=kind,
            content=content,
            items=_EXPERIMENTAL_ITEMS_ADAPTER.validate_python(items or []),
        )
    return _dump_json(payload.model_dump(mode="json"))


def check_output_json(
    *,
    success: bool,
    content: str,
    differences: list[CheckDifferencePayload],
    kind: Literal["check", "input-diff"] = "check",
    target: TargetPayload | None = None,
) -> str:
    return _dump_payload(
        CheckOutputPayload(
            version=1,
            format="json",
            kind=kind,
            success=success,
            content=content,
            differences=differences,
            target=target,
        )
    )


def generation_output_json_schema() -> str:
    return _dump_json(
        _schema_with_metadata(
            GenerationPayload.model_json_schema(mode="serialization"),
            title="GenerationPayload",
            description="Structured JSON payload emitted by generation mode --output-format json.",
        )
    )


def structured_output_json_schema() -> str:
    return _dump_json(
        _schema_with_metadata(
            _STRUCTURED_OUTPUT_ADAPTER.json_schema(mode="serialization"),
            title="StructuredOutputPayload",
            description="Schema for all structured JSON outputs emitted by the CLI.",
        )
    )
