"""Helper configuration of a client target: its JSON value and the validation that normalizes it.

The JSON value is an object mapping each helper's name to its definition. Validation gives every diagnostic and the
normalized metadata of each helper. Selectors and request targets are the runtime records generated helpers use.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Discriminator,
    Field,
    Strict,
    Tag,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from typing_extensions import Self

from datamodel_code_generator._api_types import Diagnostic, OperationRef, SchemaRef
from datamodel_code_generator._client.naming import folded, helper_name_problem, token
from datamodel_code_generator._runtime.model_codecs.media import normalized
from datamodel_code_generator._runtime.protocols.records import (
    BodySelector,
    BodyTarget,
    HeaderSelector,
    ParameterTarget,
    QuerystringTarget,
)
from datamodel_code_generator._target_config import Node, Refusal, diagnostics, valid
from datamodel_code_generator._target_documents import canonical_bytes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from datamodel_code_generator._api_types import DiagnosticStage
    from datamodel_code_generator._format_types import PythonVersion

__all__ = (
    "Helper",
    "HelperKind",
    "Link",
    "validate",
)

HelperKind: TypeAlias = Literal[
    "pagination", "polling", "sse", "ndjson", "websocket", "webhook", "cache", "resumable_upload"
]
Tree: TypeAlias = "dict[str, Any]"

_STATE_SETS: Final = ("pending", "succeeded", "failed", "cancelled")
_RENAMED: Final = {"in_": "location"}
_FRAMES: Final = {"json": None, "utf8": "text", "bytes": "binary"}
_after = model_validator(mode="after")


class _Omitted:
    def __deepcopy__(self, memo: object) -> _Omitted:
        return self


OMITTED: Final[Any] = _Omitted()
NONE: Final[Any] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Link:
    """An operation a helper sends, and the request targets it writes into that operation."""

    at: str
    ref: OperationRef
    targets: tuple[tuple[str, Mapping[str, Any]], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Helper:
    """One validated helper: its normalized metadata, operation links from its entry on, and schema references."""

    name: str
    kind: HelperKind
    enabled: bool
    at: str
    tree: Mapping[str, Any]
    links: tuple[Link, ...]
    schemas: tuple[tuple[str, SchemaRef], ...]


def _encodable(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _literal(value: object) -> object:
    """Return a JSON literal, refusing other types, non-finite numbers, and text that is not UTF-8."""
    pending = [value]
    while pending:
        match item := pending.pop():
            case None | bool() | int():
                continue
            case float() if isfinite(item):
                continue
            case str() if _encodable(item):
                continue
            case str():
                problem = "has text that is not UTF-8"
            case list():
                pending.extend(item)
                continue
            case dict() if all(_encodable(key) for key in item):
                pending.extend(item.values())
                continue
            case _:
                problem = "must be a JSON value"
        raise Refusal(problem)
    return value


def _pointer(value: object) -> object:
    try:
        BodySelector(pointer=value)  # ty: ignore[invalid-argument-type]
    except (TypeError, ValueError):
        msg = "must be an RFC 6901 pointer"
        raise Refusal(msg) from None
    if not _encodable(value):
        msg = "must be a UTF-8 string"
        raise Refusal(msg)
    return value


def _shorthand(value: object) -> object:
    return {"pointer": _pointer(value)} if isinstance(value, str) else value


def _line(value: object) -> object:
    if isinstance(value, str) and "\n" in value:
        msg = "must not contain a line feed"
        raise Refusal(msg)
    return value


def _media(value: object) -> object:
    if isinstance(value, str) and (media_type := normalized(value)) is not None:
        return media_type
    msg = "must be a media type"
    raise Refusal(msg)


def _seconds(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value) and value > 0:
        return float(value)
    msg = "must be positive finite seconds"
    raise Refusal(msg)


def _vary(value: object) -> object:
    if value == "*":
        msg = "must name a header, not '*'"
        raise Refusal(msg)
    return value


def _distinct(what: str, *, fold: bool = False) -> AfterValidator:
    """Refuse a list that repeats an item, compared by its JSON form, and by its lowercase form when it folds."""

    def check(values: list[Any]) -> list[Any]:
        seen: set[bytes] = set()
        for index, item in enumerate(values):
            plain = item.model_dump(by_alias=True) if isinstance(item, BaseModel) else item
            if (known := canonical_bytes(plain.lower() if fold else plain)) in seen:
                msg = f"repeats {what}"
                raise Refusal(msg, at=f"[{index}]")
            seen.add(known)
        return values

    return AfterValidator(check)


def _record(record: Callable[..., object], **values: object) -> None:
    try:
        record(**values)
    except ValueError as error:
        raise Refusal(str(error)) from None


def _conflict(message: str, at: str = "") -> Refusal:
    return Refusal(message, at=at, code="E_CONFIG_CONFLICT")


def _tagged(names: tuple[str, ...], present: str, absent: str, other: str | None = None) -> Discriminator:
    """Tell the union member of a mapping by whether it holds one of the names; `other` is the tag of non-mappings."""
    return Discriminator(
        lambda value: (
            (present if any(name in value for name in names) else absent) if isinstance(value, Mapping) else other
        )
    )


Flag = Annotated[Any, valid(lambda value: type(value) is bool, "must be a boolean")]
Yes = Annotated[Any, valid(lambda value: value is True, "must be true")]
Text = Annotated[Any, valid(_encodable, "must be a UTF-8 string")]
Nonempty = Annotated[Any, valid(lambda value: _encodable(value) and value, "must be a nonempty UTF-8 string")]
Line = Annotated[Nonempty, AfterValidator(_line)]
Visible = Annotated[
    Any,
    valid(
        lambda value: isinstance(value, str) and value and all("!" <= char <= "~" for char in value),
        "must be nonempty visible ASCII text",
    ),
]
Prefix = Annotated[
    Any,
    valid(
        lambda value: isinstance(value, str) and all("!" <= char <= "~" for char in value), "must be visible ASCII text"
    ),
]
Header = Annotated[Any, valid(token, "must be a header name")]
MaybeHeader = Annotated[Any, valid(lambda value: value is None or token(value), "must be a header name")]
Subprotocol = Annotated[Any, valid(token, "must be a subprotocol token")]
VaryName = Annotated[Any, AfterValidator(_vary), valid(token, "must be a header name")]
Media = Annotated[Any, AfterValidator(_media)]
Natural = Annotated[Any, valid(lambda value: type(value) is int and value >= 0, "must be an integer of at least 0")]
Positive = Annotated[Any, valid(lambda value: type(value) is int and value > 0, "must be a positive integer")]
Seconds = Annotated[Any, AfterValidator(_seconds)]
Status = Annotated[
    Any,
    valid(lambda value: type(value) is int and 100 <= value <= 599, "must be a status from 100 to 599"),  # noqa: PLR2004
]
Pointer = Annotated[Any, AfterValidator(_pointer)]
Value = Annotated[Any, AfterValidator(_literal)]


class _Reference(Node):
    pointer: Pointer
    document: Text = OMITTED


class _OperationNode(_Reference):
    """An operation reference."""


class _SchemaNode(_Reference):
    """A schema reference."""


Operation = Annotated[_OperationNode, BeforeValidator(_shorthand)]


class _Recorded(Node):
    """A selector or request target that the runtime record of the same form also checks."""

    record: ClassVar[Callable[..., object]]

    @_after
    def _form(self) -> Self:
        fields = self.record.__dataclass_fields__  # ty: ignore[unresolved-attribute]
        _record(self.record, **{name: value for key, value in self if (name := _RENAMED.get(key, key)) in fields})
        return self


class _Body(_Recorded):
    record: ClassVar = BodySelector
    from_: Literal["body"] = Field(alias="from")
    pointer: Text


class _BoundHeader(_Recorded):
    record: ClassVar = HeaderSelector
    from_: Literal["header"] = Field(alias="from")
    name: Text
    occurrence: Literal["single", "all"] = "single"


class _Header(_BoundHeader):
    @_after
    def _single(self) -> Self:
        if self.occurrence == "all":
            msg = "can be 'all' only in a binding value"
            raise Refusal(msg, at=".occurrence")
        return self


class _Status(Node):
    from_: Literal["status"] = Field(alias="from")


Selector = Annotated[_Body | _Header | _Status, Field(discriminator="from_")]
BoundSelector = Annotated[_Body | _BoundHeader | _Status, Field(discriminator="from_")]


class _Parameter(_Recorded):
    record: ClassVar = ParameterTarget
    in_: Literal["path", "query", "header", "cookie"] = Field(alias="in")
    name: Text


class _Querystring(_Recorded):
    record: ClassVar = QuerystringTarget
    in_: Literal["querystring"] = Field(alias="in")
    name: Text
    pointer: Text


class _BodyTarget(_Recorded):
    record: ClassVar = BodyTarget
    in_: Literal["body"] = Field(alias="in")
    pointer: Text


Target = Annotated[_Parameter | _Querystring | _BodyTarget, Field(discriminator="in_")]


class _Literal(Node):
    literal: Value


class _Sourced(Node):
    source: Literal["input", "initial", "previous"]
    selector: BoundSelector


Bound = Annotated[
    Annotated[_Literal, Tag("bound-literal")] | Annotated[_Sourced, Tag("bound-source")],
    _tagged(("literal",), "bound-literal", "bound-source"),
]


class _Binding(Node):
    target: Target
    value: Bound


Bindings = Annotated[list[_Binding], Strict()]


class _Missing(Node):
    kind: Literal["missing"]


class _Null(Node):
    kind: Literal["null"]


class _EndValue(Node):
    kind: Literal["value"]
    value: Value

    @_after
    def _not_null(self) -> Self:
        if self.value is None:
            msg = "is null; use kind 'null'"
            raise Refusal(msg, at=".value")
        return self


End = Annotated[_Missing | _Null | _EndValue, Field(discriminator="kind")]
Ends = Annotated[list[End], Strict(), Field(min_length=1), _distinct("an end condition")]
Statuses = Annotated[list[Status], Strict(), Field(min_length=1), _distinct("a status")]
States = Annotated[list[Value], Strict(), _distinct("a state")]
RequiredStates = Annotated[States, Field(min_length=1)]


class _Cursor(Node):
    kind: Literal["cursor"]
    read: Selector
    write: Target
    end: Ends
    empty_string: Literal["value", "end"]

    @_after
    def _not_empty(self) -> Self:
        for index, end in enumerate(self.end):
            if isinstance(end, _EndValue) and end.value == "":  # noqa: PLC1901
                msg = "ends at the empty string, which empty_string decides alone"
                raise _conflict(msg, f".end[{index}]")
        return self


class _Step(Node):
    literal: Positive = OMITTED
    page_items_count: Yes = OMITTED

    @_after
    def _one(self) -> Self:
        if len(self.model_fields_set) != 1:
            msg = "needs exactly one of 'literal' and 'page_items_count'"
            raise Refusal(msg)
        return self


class _Counted(Node):
    kind: Literal["offset", "page"]
    write: Target
    first: Natural
    step: _Step
    has_more: Selector = OMITTED
    total: Selector = OMITTED

    @_after
    def _one_end(self) -> Self:
        if "has_more" in self.model_fields_set and "total" in self.model_fields_set:
            msg = "needs has_more or total, not both"
            raise _conflict(msg)
        if not {"has_more", "total"} & self.model_fields_set:
            msg = "needs has_more or total"
            raise Refusal(msg)
        return self


class _NextUrl(Node):
    kind: Literal["next_url"]
    read: Selector
    end: Ends
    repeat_request_body: Flag = False


class _Link(Node):
    kind: Literal["link"]
    header: Header
    rel: Nonempty = "next"


Continuation = Annotated[_Cursor | _Counted | _NextUrl | _Link, Field(discriminator="kind")]


class _Pagination(Node):
    kind: Literal["pagination"]
    enabled: Flag = True
    operation: Operation
    items: _Body
    item_schema: _SchemaNode
    continuation: Continuation
    bindings: Bindings = NONE


class _RemoteCancel(Node):
    operation: Operation
    bindings: Bindings = NONE


class _Interval(Node):
    seconds: Seconds = 1.0
    retry_after_header: MaybeHeader = None


class _Immediate(Node):
    statuses: Statuses
    selector: Selector
    schema_: _SchemaNode = Field(alias="schema")


class _Inline(Node):
    kind: Literal["inline"]
    selector: Selector
    schema_: _SchemaNode = Field(alias="schema")


class _OperationResult(Node):
    kind: Literal["operation"]
    operation: Operation
    bindings: Bindings = NONE


class _NoResult(Node):
    kind: Literal["none"]


Result = Annotated[_Inline | _OperationResult | _NoResult, Field(discriminator="kind")]


class _Polling(Node):
    kind: Literal["polling"]
    enabled: Flag = True
    create: Operation
    accepted_statuses: Statuses
    poll: Operation
    bindings: Bindings
    state: Selector
    pending: RequiredStates
    succeeded: RequiredStates
    failed: States = NONE
    cancelled: States = NONE
    result: Result
    interval: _Interval = _Interval()
    remote_cancel: _RemoteCancel = OMITTED
    immediate_result: _Immediate = OMITTED
    expires_at: Selector = OMITTED

    @_after
    def _disjoint(self) -> Self:
        """Refuse state sets that share a value, and immediate statuses the helper accepts."""
        owners: dict[bytes, str] = {}
        for name in _STATE_SETS:
            for index, state in enumerate(getattr(self, name)):
                if (other := owners.setdefault(canonical_bytes(state), name)) != name:
                    msg = f"is a state that the {other} states also list"
                    raise _conflict(msg, f".{name}[{index}]")
        if self.immediate_result is not OMITTED and (
            shared := sorted(set(self.immediate_result.statuses) & set(self.accepted_statuses))
        ):
            msg = f"lists the accepted statuses {shared}"
            raise _conflict(msg, ".immediate_result.statuses")
        return self


class _UploadCreate(Node):
    operation: Operation
    size: Target = OMITTED
    session_url: Selector = OMITTED
    expires_at: Selector = OMITTED


class _UploadProbe(Node):
    operation: Operation
    bindings: Bindings = NONE
    remote_offset: Selector


class _Checksum(Node):
    target: Target
    algorithm: Literal["md5", "sha1", "sha256", "sha512"]
    encoding: Literal["base64", "hex"] = "base64"
    algorithm_prefix: Flag = False


class _UploadAppend(Node):
    operation: Operation
    bindings: Bindings = NONE
    offset: Target
    length: Target = OMITTED
    checksum: _Checksum = OMITTED


class _ByLength(Node):
    kind: Literal["length"]


class _ByOperation(Node):
    kind: Literal["operation"]
    operation: Operation
    bindings: Bindings = NONE
    result_schema: _SchemaNode


class _Upload(Node):
    kind: Literal["resumable_upload"]
    enabled: Flag = True
    profile: Literal["offset"]
    create: _UploadCreate
    probe: _UploadProbe
    append: _UploadAppend
    partial_commit: Literal["allowed", "forbidden"]
    completion: Annotated[_ByLength | _ByOperation, Field(discriminator="kind")]
    abort: _RemoteCancel = OMITTED


class _Eof(Node):
    kind: Literal["eof"]


class _Sentinel(Node):
    kind: Literal["sentinel"]
    value: Nonempty


class _LineSentinel(Node):
    kind: Literal["sentinel"]
    value: Line


class _EventType(Node):
    kind: Literal["event_type"]
    value: Nonempty


class _BodyDiscriminator(Node):
    from_: Literal["body"] = Field(alias="from")
    pointer: Pointer


class _EventTypeDiscriminator(Node):
    from_: Literal["event_type"] = Field(alias="from")


Schemas = dict[Nonempty, _SchemaNode]


class _EventMappingBase(Node):
    discriminator: Any
    mapping: Annotated[Schemas, Field(min_length=1)]


class _EventMapping(_EventMappingBase):
    discriminator: _BodyDiscriminator


class _SseEventMapping(_EventMappingBase):
    discriminator: Annotated[_EventTypeDiscriminator | _BodyDiscriminator, Field(discriminator="from_")]


EventSchema = Annotated[
    Annotated[_EventMapping, Tag("event-mapping")] | Annotated[_SchemaNode, Tag("event-schema")],
    _tagged(("discriminator", "mapping"), "event-mapping", "event-schema", "event-schema"),
]
SseEventSchema = Annotated[
    Annotated[_SseEventMapping, Tag("event-mapping")] | Annotated[_SchemaNode, Tag("event-schema")],
    _tagged(("discriminator", "mapping"), "event-mapping", "event-schema", "event-schema"),
]


class _ResumeOff(Node):
    enabled: Annotated[Any, valid(lambda value: value is False, "must be a boolean")] = False


class _ResumeOn(Node):
    enabled: Literal[True]
    write: Target
    reopen_operation: Operation
    delivery: Literal["at_least_once"]
    bindings: Bindings = NONE
    reconnect_on: Annotated[
        list[Literal["transport_interruption", "incomplete_eof"]],
        Strict(),
        Field(min_length=1),
        _distinct("a reconnect reason"),
    ] = ("transport_interruption",)  # ty: ignore[invalid-assignment]
    expires_at: Selector = OMITTED


class _ResumeEventId(_ResumeOn):
    cursor: Literal["event_id"]


class _ResumeCursor(_ResumeOn):
    cursor: Selector
    missing: Literal["inherit", "error"]
    null: Literal["clear", "error"]


def _resume_tag(*, ids: bool) -> Discriminator:
    def tag(value: object) -> str:
        if not isinstance(value, Mapping) or value.get("enabled", False) is not True:
            return "resume-off"
        return "resume-id" if ids and value.get("cursor") == "event_id" else "resume-cursor"

    return Discriminator(tag)


Resume = Annotated[
    Annotated[_ResumeOff, Tag("resume-off")] | Annotated[_ResumeCursor, Tag("resume-cursor")], _resume_tag(ids=False)
]
SseResume = Annotated[
    Annotated[_ResumeOff, Tag("resume-off")]
    | Annotated[_ResumeCursor, Tag("resume-cursor")]
    | Annotated[_ResumeEventId, Tag("resume-id")],
    _resume_tag(ids=True),
]


class _Stream(Node):
    kind: str
    enabled: Flag = True
    operation: Operation
    media: Media
    event_schema: Any
    completion: Any
    unknown: Literal["error", "raw"] = "error"
    error_events: Schemas = Field(default_factory=dict)
    resume: Any = _ResumeOff()

    @_after
    def _events(self) -> Self:
        """Refuse raw and error events without a discriminator, and an error event that is also an event."""
        mapping = self.event_schema.mapping if isinstance(self.event_schema, _EventMappingBase) else None
        if self.unknown == "raw" and mapping is None:
            msg = "can be 'raw' only with a discriminator"
            raise _conflict(msg, ".unknown")
        if self.error_events and mapping is None:
            msg = "need a discriminator"
            raise _conflict(msg, ".error_events")
        if mapping and (shared := next((key for key in self.error_events if key in mapping), None)):
            msg = "is also an event of the event_schema mapping"
            raise _conflict(msg, f".error_events[{shared!r}]")
        return self


class _Sse(_Stream):
    kind: Literal["sse"]
    event_schema: SseEventSchema
    completion: Annotated[_Eof | _Sentinel | _EventType, Field(discriminator="kind")]
    resume: SseResume = _ResumeOff()


class _Ndjson(_Stream):
    kind: Literal["ndjson"]
    event_schema: EventSchema
    completion: Annotated[_Eof | _LineSentinel, Field(discriminator="kind")]
    final_line: Literal["require_newline", "allow_eof"]
    resume: Resume = _ResumeOff()


class _Message(Node):
    codec: Literal["json", "utf8", "bytes"]
    frame: Literal["text", "binary"] = OMITTED
    schema_: _SchemaNode = Field(alias="schema", default=OMITTED)

    @_after
    def _framed(self) -> Self:
        """Give the message the frame its codec defaults to, and refuse a schema beyond JSON and a mismatched frame."""
        if (self.codec == "json") != ("schema_" in self.model_fields_set):
            if self.codec == "json":
                msg = "needs 'schema' for JSON messages"
                raise Refusal(msg)
            msg = "applies only to JSON messages"
            raise _conflict(msg, ".schema")
        fixed = _FRAMES[self.codec]
        if "frame" not in self.model_fields_set:
            self.__dict__["frame"] = fixed or "text"
        elif fixed is not None and self.frame != fixed:
            msg = f"must be {fixed!r} for {self.codec} messages"
            raise _conflict(msg, ".frame")
        return self


class _WebSocket(Node):
    kind: Literal["websocket"]
    enabled: Flag = True
    operation: Operation
    subprotocols: Annotated[list[Subprotocol], Strict(), _distinct("a subprotocol")] = NONE
    send: _Message
    receive: _Message


class _Cache(Node):
    kind: Literal["cache"]
    enabled: Flag = True
    operation: Operation
    validator: Literal["etag", "last_modified", "both"]
    authenticated: Flag
    statuses: Statuses = (200,)  # ty: ignore[invalid-assignment]
    vary_allowlist: Annotated[list[VaryName], Strict(), _distinct("a header name", fold=True)] = NONE


class _Standard(Node):
    kind: Literal["standard_webhooks"]


class _Stripe(Node):
    kind: Literal["stripe_style"]
    header: Header = "Stripe-Signature"


class _BodyHmac(Node):
    kind: Literal["body_hmac"]
    header: Header
    encoding: Literal["hex", "base64"] = "hex"
    prefix: Prefix = ""
    algorithm: Literal["hmac-sha256", "hmac-sha512"] = "hmac-sha256"


class _PublicKey(Node):
    kind: Literal["ed25519", "rsa-pss-sha256"]
    header: Header
    encoding: Literal["hex", "base64", "base64url"] = "hex"
    prefix: Prefix = ""


class _Adapter(Node):
    kind: Literal["adapter"]
    timestamp: Literal["required", "none"]
    delivery_id: Literal["required", "none"]


class _Unsigned(Node):
    kind: Literal["none"]


class _Webhook(Node):
    kind: Literal["webhook"]
    enabled: Flag = True
    event_schema: EventSchema
    signature: Annotated[
        _Standard | _Stripe | _BodyHmac | _PublicKey | _Adapter | _Unsigned, Field(discriminator="kind")
    ]


_HELPERS: Final = TypeAdapter(
    Annotated[
        _Pagination | _Polling | _Sse | _Ndjson | _WebSocket | _Webhook | _Cache | _Upload, Field(discriminator="kind")
    ]
)


def _tree(value: object, at: str, schemas: list[tuple[str, SchemaRef]]) -> object:
    """Spell a validated helper as its normalized settings, collecting the schema references it holds."""
    match value:
        case _OperationNode():
            return OperationRef(pointer=value.pointer, document=None if value.document is OMITTED else value.document)
        case _SchemaNode():
            schemas.append((
                at,
                ref := SchemaRef(pointer=value.pointer, document=None if value.document is OMITTED else value.document),
            ))
            return ref
        case BaseModel():
            return {
                key: _tree(item, f"{at}.{key}", schemas)
                for name, field in type(value).model_fields.items()
                if (item := getattr(value, name)) is not OMITTED and (key := field.alias or name)
            }
        case list() | tuple():
            return type(value)(_tree(item, f"{at}[{index}]", schemas) for index, item in enumerate(value))
        case dict():
            return {key: _tree(item, f"{at}[{key!r}]", schemas) for key, item in value.items()}
    return value


def validate(
    tree: Mapping[str, object], target_python_version: PythonVersion
) -> tuple[tuple[Helper, ...], list[Diagnostic]]:
    """Validate a helper configuration object, returning its valid helpers and every problem in declaration order.

    Helper names are checked as identifiers of the target Python version.
    """
    names: list[tuple[str, str]] = []
    helpers: list[Helper] = []
    problems: list[Diagnostic] = []
    for name, body in tree.items():
        here = f"protocols[{name!r}]"
        if (problem := helper_name_problem(name, target_python_version)) is not None:
            problems.append(_problem("E_CONFIG_VALUE", here, problem))
        elif other := next(
            (other for key, other in names if _collides(folded(name, target_python_version), key)), None
        ):
            problems.append(_problem("E_NAME_COLLISION", here, f"The helper names {other!r} and {name!r} collide"))
        else:
            names.append((folded(name, target_python_version), name))
        if isinstance(body, Mapping) and body.get("kind") == "resumable_upload" and body.get("profile") == "parts":
            message = f"The parts profile of the resumable_upload helper {name!r} is not supported yet"
            problems.append(_problem("E_CLIENT_UNSUPPORTED", f"{here}.profile", message, stage="target"))
            continue
        try:
            model = _HELPERS.validate_python(body)
        except ValidationError as error:
            problems.extend(diagnostics(error, body, here))
            continue
        schemas: list[tuple[str, SchemaRef]] = []
        settings: Tree = _tree(model, here, schemas)  # ty: ignore[invalid-assignment]
        helpers.append(
            Helper(
                name=name,
                kind=model.kind,
                enabled=model.enabled,
                at=here,
                tree=settings,
                links=tuple(_links(model.kind, settings, here)),
                schemas=tuple(schemas),
            )
        )
    return tuple(helpers), problems


def _collides(name: str, other: str) -> bool:
    return name == other or name.startswith(f"{other}.") or other.startswith(f"{name}.")


def _problem(code: str, at: str, message: str, *, stage: DiagnosticStage = "config") -> Diagnostic:
    return Diagnostic(code=code, severity="error", stage=stage, message=message, option_path=at)


def _targets(bindings: list[Tree], at: str) -> tuple[tuple[str, Tree], ...]:
    return tuple((f"{at}[{index}].target", binding["target"]) for index, binding in enumerate(bindings))


def _links(kind: str, tree: Tree, at: str) -> Iterator[Link]:
    """Yield the operations a valid helper sends, its entry operation first, with the targets each one takes."""
    match kind:
        case "pagination":
            continuation = tree["continuation"]
            written = ((f"{at}.continuation.write", continuation["write"]),) if "write" in continuation else ()
            yield Link(
                at=f"{at}.operation",
                ref=tree["operation"],
                targets=(*written, *_targets(tree["bindings"], f"{at}.bindings")),
            )
        case "webhook":
            return
        case "websocket":
            yield Link(at=f"{at}.operation", ref=tree["operation"])
        case "cache":
            yield Link(at=f"{at}.operation", ref=tree["operation"])
        case "resumable_upload":
            yield from _upload_links(tree, at)
        case "polling":
            yield Link(at=f"{at}.create", ref=tree["create"])
            yield Link(at=f"{at}.poll", ref=tree["poll"], targets=_targets(tree["bindings"], f"{at}.bindings"))
            if (result := tree["result"])["kind"] == "operation":
                where = f"{at}.result"
                targets = _targets(result["bindings"], f"{where}.bindings")
                yield Link(at=f"{where}.operation", ref=result["operation"], targets=targets)
            if (cancel := tree.get("remote_cancel")) is not None:
                where = f"{at}.remote_cancel"
                targets = _targets(cancel["bindings"], f"{where}.bindings")
                yield Link(at=f"{where}.operation", ref=cancel["operation"], targets=targets)
        case _:
            yield Link(at=f"{at}.operation", ref=tree["operation"])
            if (resume := tree["resume"])["enabled"]:
                where = f"{at}.resume"
                targets = ((f"{where}.write", resume["write"]), *_targets(resume["bindings"], f"{where}.bindings"))
                yield Link(at=f"{where}.reopen_operation", ref=resume["reopen_operation"], targets=targets)


def _upload_links(tree: Tree, at: str) -> Iterator[Link]:
    """Yield the operations an upload helper sends, its create operation first, with the targets each one takes."""
    create = tree["create"]
    sized = ((f"{at}.create.size", create["size"]),) if "size" in create else ()
    yield Link(at=f"{at}.create.operation", ref=create["operation"], targets=sized)
    probe = tree["probe"]
    yield Link(
        at=f"{at}.probe.operation", ref=probe["operation"], targets=_targets(probe["bindings"], f"{at}.probe.bindings")
    )
    append = tree["append"]
    written = tuple((f"{at}.append.{name}", append[name]) for name in ("offset", "length") if name in append)
    if (checksum := append.get("checksum")) is not None:
        written = (*written, (f"{at}.append.checksum.target", checksum["target"]))
    yield Link(
        at=f"{at}.append.operation",
        ref=append["operation"],
        targets=(*written, *_targets(append["bindings"], f"{at}.append.bindings")),
    )
    for name in ("completion", "abort"):
        if (part := tree.get(name)) is not None and "operation" in part:
            where = f"{at}.{name}"
            yield Link(
                at=f"{where}.operation", ref=part["operation"], targets=_targets(part["bindings"], f"{where}.bindings")
            )
