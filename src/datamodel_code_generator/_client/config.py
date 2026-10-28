"""Client target settings and the JSON form of their per-operation entries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final, Literal, TypeAlias

from pydantic import AfterValidator, BeforeValidator, Field, model_validator
from typing_extensions import Self

from datamodel_code_generator._api_types import Diagnostic
from datamodel_code_generator._client.naming import namespace_problem, token
from datamodel_code_generator._runtime.client.options import is_base_url
from datamodel_code_generator._runtime.model_codecs.media import normalize_media_type
from datamodel_code_generator._target_config import Node, Refusal, Refused, Selector, TargetConfig, valid, version
from datamodel_code_generator._target_naming import explicit_name

if TYPE_CHECKING:
    from pydantic import ValidationInfo

Transport: TypeAlias = Literal["httpx2"]
SignatureStyle: TypeAlias = Literal["explicit", "unpack"]
BodyArguments: TypeAlias = Literal["body", "both"]

OPTION_PREFIX: Final = "client"
_LOCATIONS: Final = ("path", "query", "querystring", "header", "cookie")
_MIN_REDIRECT: Final = 300
_MAX_REDIRECT: Final = 399


def absolute(value: object) -> bool:
    """Return whether a value is a base URL that generated clients accept, as their runtime checks it."""
    try:
        return isinstance(value, str) and is_base_url(value)
    except ValueError:
        return False


def _media(value: object) -> bool:
    try:
        normalize_media_type(value)  # ty: ignore[invalid-argument-type]
    except (TypeError, ValueError):
        return False
    return True


def _namespace(value: object, info: ValidationInfo) -> object:
    if (problem := namespace_problem(value, version(info))) is not None:
        raise Refusal(problem, bare=True)
    return value


def _identifier(value: object, info: ValidationInfo) -> object:
    if not explicit_name(value, version(info)):
        msg = "must be a Python identifier"
        raise Refusal(msg)
    return value


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _parameters(value: object) -> object:
    """Read the parameter names of a JSON object keyed by location and name, such as `query:limit`."""
    if isinstance(value, tuple):
        return value
    if not isinstance(value, Mapping):
        msg = "must be an object"
        raise Refusal(msg)
    records = []
    for key, python_name in value.items():
        location, separator, name = key.partition(":") if isinstance(key, str) else ("", "", "")
        if not separator or location not in _LOCATIONS:
            msg = f"key {key!r} must be a location (path, query, querystring, header, or cookie), ':', and a name"
            raise Refusal(msg)
        records.append({"in_": location, "name": name, "python_name": python_name})
    return tuple(records)


def _fields(value: object) -> object:
    """Read the body field names of a JSON object keyed by media type, then wire property."""
    if isinstance(value, tuple):
        return value
    if not isinstance(value, Mapping):
        msg = "must be an object"
        raise Refusal(msg)
    records = []
    for media_type, names in value.items():
        if not isinstance(names, Mapping):
            msg = "must be an object"
            raise Refusal(msg, at=f"[{media_type!r}]")
        records.extend(
            {"media_type": media_type, "name": name, "python_name": python_name} for name, python_name in names.items()
        )
    return tuple(records)


def _statuses(value: object) -> object:
    value = _tuple(value)
    if not (
        isinstance(value, tuple)
        and all(type(status) is int and _MIN_REDIRECT <= status <= _MAX_REDIRECT for status in value)
        and len(set(value)) == len(value)
    ):
        msg = "must be distinct statuses 300 to 399"
        raise Refusal(msg)
    return value


def _encodings(value: object) -> object:
    value = _tuple(value)
    if not (isinstance(value, tuple) and all(isinstance(item, str) and token(item) for item in value)):
        msg = "must be a tuple of HTTP tokens"
        raise Refusal(msg)
    value = tuple(item.lower() for item in value)
    if len(set(value)) != len(value):
        msg = "names a coding twice"
        raise Refusal(msg, code="E_CONFIG_CONFLICT")
    if any(item != "gzip" for item in value):
        msg = "may name only gzip, the builtin encoder"
        raise Refusal(msg)
    return value


Namespace = Annotated[Any, AfterValidator(_namespace)]
Identifier = Annotated[Any, AfterValidator(_identifier)]
HeaderName = Annotated[Any, valid(token, "must be a header name")]
MediaType = Annotated[Any, valid(_media, "must be a media type")]
BaseUrl = Annotated[Any, valid(absolute, "must be an absolute http or https URL without userinfo, query, or fragment")]


class ResourceName(Node):
    """Map one source tag to the resource namespace its operations join."""

    tag: str
    namespace: Namespace


class ParameterName(Node):
    """Name the Python argument of one effective parameter, selected by its declared location and name."""

    in_: Literal["path", "query", "querystring", "header", "cookie"]
    name: str
    python_name: Any

    @model_validator(mode="after")
    def _identifier(self, info: ValidationInfo) -> Self:
        if not explicit_name(self.python_name, version(info)):
            msg = "A parameter name needs a location, a wire name, and an identifier"
            raise Refusal(msg, bare=True)
        return self


class BodyFieldName(Node):
    """Name the keyword argument of one field of a body media type, selected by the media and its wire property."""

    media_type: Any
    name: Any
    python_name: Any

    @model_validator(mode="after")
    def _identifier(self, info: ValidationInfo) -> Self:
        if not (
            _media(self.media_type) and isinstance(self.name, str) and explicit_name(self.python_name, version(info))
        ):
            msg = "A body field name needs a media type, a wire property, and an identifier"
            raise Refusal(msg, bare=True)
        return self


class IdempotencyMetadata(Node):
    """An API's explicit idempotency key header."""

    header_name: HeaderName


class RuntimeOperationMetadata(Node):
    """Runtime facts of one operation that the source cannot declare."""

    request_id_header: HeaderName | None = None
    success_statuses: Annotated[Any, AfterValidator(_statuses)] = ()
    retry_safety: Literal["method_default", "idempotent", "never"] = "method_default"
    idempotency: IdempotencyMetadata | None = None
    retry_after_ms_header: HeaderName | None = None
    should_retry_header: HeaderName | None = None
    auth_challenge_less_401: Annotated[Any, valid(lambda value: type(value) is bool, "must be a boolean")] = False
    accepted_content_encodings: Annotated[Any, AfterValidator(_encodings)] = ()

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        retry, should = self.retry_after_ms_header, self.should_retry_header
        if isinstance(retry, str) and isinstance(should, str) and retry.lower() == should.lower():
            msg = "Retry control response headers must have distinct names"
            raise Refusal(msg, at=".should_retry_header", code="E_CONFIG_CONFLICT", bare=True)
        return self


class ClientOperationConfig(Node):
    """Settings of one operation, selected by its root use-site reference."""

    ref: Selector = Field(alias="@ref")
    resource: Namespace | None = None
    name: Identifier | None = None
    parameter_names: Annotated[tuple[ParameterName, ...], BeforeValidator(_parameters)] = ()
    request_media_type: MediaType | None = None
    response_media_type: MediaType | None = None
    runtime: RuntimeOperationMetadata = RuntimeOperationMetadata()
    description: Annotated[Any, valid(lambda value: isinstance(value, str), "must be a string")] | None = None
    body_arguments: (
        Annotated[
            Any,
            valid(
                lambda value: isinstance(value, str) and value in {"body", "both"}, "must be 'body', 'both', or None"
            ),
        ]
        | None
    ) = None
    body_field_names: Annotated[tuple[BodyFieldName, ...], BeforeValidator(_fields)] = ()

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        seen: set[tuple[str, str]] = set()
        for index, item in enumerate(self.body_field_names):
            if (key := (normalize_media_type(item.media_type), item.name)) in seen:
                msg = f"The {item.media_type} body field {item.name!r} is named twice"
                raise Refusal(msg, at=f".body_field_names[{index}]", bare=True)
            seen.add(key)
        return self


class ClientGenerationConfig(TargetConfig):
    """Settings of one client target; model settings stay in the model configuration."""

    _option_prefix: ClassVar[str | None] = OPTION_PREFIX

    transport: Transport = "httpx2"
    resource_names: tuple[ResourceName, ...] = ()
    operations: tuple[ClientOperationConfig, ...] = ()
    signature_style: SignatureStyle = "explicit"
    body_arguments: BodyArguments = "body"
    default_base_url: BaseUrl | None = None
    server_base_url: BaseUrl | None = None
    protocols: Mapping[str, object] | None = None

    @model_validator(mode="after")
    def _field_names(self) -> Self:
        """Refuse body field names for an operation that takes its body only."""
        if problems := [
            Diagnostic(
                code="E_CONFIG_VALUE",
                severity="error",
                stage="config",
                message="body_field_names need body_arguments 'both' for the operation",
                option_path=f"operations[{index}].body_field_names",
            )
            for index, item in enumerate(self.operations)
            if item.body_field_names and (item.body_arguments or self.body_arguments) != "both"
        ]:
            raise Refused(problems)
        return self

    @model_validator(mode="after")
    def _helpers(self, info: ValidationInfo) -> Self:
        """Validate the helper JSON object, loading the helper definitions only for a configuration with helpers."""
        if self.protocols is not None:
            from datamodel_code_generator._client.protocols import validate  # noqa: PLC0415

            if problems := validate(self.protocols, version(info))[1]:
                raise Refused(problems)
        return self
