"""FastAPI server target settings."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Final, Literal, TypeAlias

from pydantic import AfterValidator, Field, ValidationInfo

from datamodel_code_generator._target_config import Node, Refusal, Selector, TargetConfig, valid, version
from datamodel_code_generator._target_naming import explicit_name

Layout: TypeAlias = Literal["routers", "single"]
HandlerMode: TypeAlias = Literal["sync", "async"]
BodyMode: TypeAlias = Literal["typed", "request"]

_PARAMETER_LOCATIONS: Final = frozenset({"path", "query", "querystring", "header", "cookie"})
_MIN_STATUS: Final = 100
_MAX_STATUS: Final = 599


def _name(value: object, info: ValidationInfo) -> object:
    if not explicit_name(value, version(info)):
        msg = "must be a Python identifier"
        raise Refusal(msg)
    return value


def _location(value: object) -> bool:
    location, _, name = value.partition(":") if isinstance(value, str) else ("", "", "")
    return location in _PARAMETER_LOCATIONS and bool(name)


Name = Annotated[Any, AfterValidator(_name)]
ParameterKey = Annotated[Any, valid(_location, "must be a location, ':', and a name")]


class ResponseChoice(Node):
    """Name the declared response a bare handler return value takes."""

    status_code: Annotated[
        Any,
        valid(
            lambda value: type(value) is int and _MIN_STATUS <= value <= _MAX_STATUS, "must be a status from 100 to 599"
        ),
    ]
    media_type: Annotated[Any, valid(lambda value: isinstance(value, str), "must be a string")] | None = None


class FastAPIConfig(TargetConfig):
    """Settings of one FastAPI server target; model settings stay in the model configuration."""

    _option_prefix: ClassVar[str | None] = "server"
    _serves_source: ClassVar[bool] = True

    layout: Layout = "routers"
    handler_mode: HandlerMode = "sync"
    handler_modes: dict[Selector, HandlerMode] = Field(default_factory=dict)
    include_request: bool = False
    body_mode: BodyMode = "typed"
    body_modes: dict[Selector, BodyMode] = Field(default_factory=dict)
    primary_responses: dict[Selector, ResponseChoice] = Field(default_factory=dict)
    operation_names: dict[Selector, Name] = Field(default_factory=dict)
    router_names: dict[str, Name] = Field(default_factory=dict)
    parameter_names: dict[Selector, dict[ParameterKey, Name]] = Field(default_factory=dict)
