"""Shared single-target configuration, and the mapping of validation errors onto diagnostics."""

from __future__ import annotations

import keyword
import re
from collections.abc import Mapping
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final, TypeAlias

from pydantic import AfterValidator, BaseModel, ConfigDict, InstanceOf, ValidationError, ValidationInfo

from datamodel_code_generator._api_types import APIGenerationError, Diagnostic, DiagnosticStage, OperationRef
from datamodel_code_generator._target_unicode import NEWEST_TARGET, is_identifier

if TYPE_CHECKING:
    from collections.abc import Callable

    from datamodel_code_generator._format_types import PythonVersion

_KEYED: Final = frozenset({
    "mapping",
    "error_events",
    "handler_modes",
    "body_modes",
    "primary_responses",
    "operation_names",
    "router_names",
    "parameter_names",
})
_TAGS: Final = ("kind", "in", "from")
_QUOTED: Final = re.compile(r"'([^']*)'")
_NOUNS: Final = {"list_type": "a list", "too_short": "a nonempty list", "bool_type": "a boolean"}


class Refused(ValueError):  # noqa: N818
    """Refuse a value with diagnostics that are complete."""

    def __init__(self, problems: list[Diagnostic]) -> None:
        """Keep the diagnostics."""
        super().__init__("refused")
        self.problems = problems


Selector: TypeAlias = str | InstanceOf[OperationRef]


class Refusal(ValueError):  # noqa: N818
    """Refuse a value; `at` extends its path, and a `bare` message names no path."""

    def __init__(
        self,
        message: str,
        *,
        at: str = "",
        code: str = "E_CONFIG_VALUE",
        stage: DiagnosticStage = "config",
        bare: bool = False,
    ) -> None:
        """Keep the message, the path extension, the diagnostic code and stage."""
        super().__init__(message)
        self.message, self.at, self.code, self.stage, self.bare = message, at, code, stage, bare


def valid(test: Callable[[Any], bool], message: str) -> AfterValidator:
    """Return a validator refusing the values the test rejects with `<path> <message>`."""

    def check(value: Any) -> Any:
        if not test(value):
            raise Refusal(message)
        return value

    return AfterValidator(check)


def version(info: ValidationInfo) -> PythonVersion:
    """Return the target Python version a validation checks names against."""
    return (info.context or {}).get("target", NEWEST_TARGET)


def choices(values: tuple[str, ...] | list[str]) -> str:
    """Spell quoted choices as `'a', 'b' or 'c'`."""
    quoted = [repr(value) for value in values]
    return quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} or {quoted[-1]}"


def _members(node: object) -> Mapping[object, object]:
    return vars(node) if isinstance(node, BaseModel) else node if isinstance(node, Mapping) else {}  # ty: ignore[invalid-return-type]


def _path(root: str, value: object, loc: tuple[int | str, ...]) -> str:
    """Spell a location as an option path, leaving out the tags a union adds to it and the keys of free mappings."""
    text, node, keyed, tagged = root, value, False, None
    for part in loc:
        members = _members(node)
        if isinstance(part, int):
            text += f"[{part}]"
            node = node[part] if isinstance(node, list | tuple) and part < len(node) else None
        elif keyed:
            text, node = f"{text}[{part!r}]", members.get(part)
        elif tagged is not node and any(members.get(key) == part for key in _TAGS):
            tagged = node
            continue
        elif part in members:
            text, node = f"{text}.{part}" if text else part, members[part]
        else:
            continue
        keyed = isinstance(part, str) and not keyed and part in _KEYED
    return text


def diagnostics(error: ValidationError, value: object, root: str = "") -> list[Diagnostic]:
    """Map the errors of a validation of a value, found below the path `root`, onto diagnostics."""
    result: list[Diagnostic] = []
    for item in error.errors(include_url=False):
        loc, context = item["loc"], item.get("ctx", {})
        path, parent = _path(root, value, loc), _path(root, value, loc[:-1])
        code, stage, bare = "E_CONFIG_VALUE", "config", False
        match item["type"]:
            case "missing":
                message = f"{parent} needs {loc[-1]!r}" if parent else f"needs {loc[-1]!r}"
            case "extra_forbidden" | "invalid_key":
                message, code, path = (
                    f"{parent} has no key {loc[-1]!r}",
                    "E_CONFIG_UNKNOWN",
                    path if isinstance(loc[-1], str) else parent,
                )
            case "union_tag_not_found" | "union_tag_invalid" if tags := _QUOTED.findall(context["discriminator"]):
                base, path = path, f"{path}.{tags[-1]}" if path else tags[-1]
                if item["type"] == "union_tag_not_found":
                    message = f"{base} needs {tags[-1]!r}"
                else:
                    message = f"{path} must be {choices(_QUOTED.findall(context['expected_tags']))}"
            case "literal_error":
                message = f"{path} must be {context['expected']}"
            case "value_error" if isinstance(refused := context["error"], Refused):
                result.extend(refused.problems)
                continue
            case "value_error" if isinstance(refusal := context["error"], Refusal):
                path = (path + refusal.at).removeprefix(".")
                message, code, stage, bare = refusal.message, refusal.code, refusal.stage, refusal.bare
                message = message if bare else f"{path} {message}"
            case "value_error":
                message = f"{path} {context['error']}"
            case kind if kind in _NOUNS:
                message = f"{path} must be {_NOUNS[kind]}"
            case _:
                message = f"{path} must be an object"
        result.append(Diagnostic(code=code, severity="error", stage=stage, message=message, option_path=path or None))
    return result


def _dotted(value: str, info: ValidationInfo) -> str:
    target = version(info)
    if not value or not all(is_identifier(part, target) and not keyword.iskeyword(part) for part in value.split(".")):
        msg = "must be a dotted Python import path"
        raise Refusal(msg)
    return value


Dotted = Annotated[str, AfterValidator(_dotted)]


class Node(BaseModel):
    """A frozen record of a configuration that refuses unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True, protected_namespaces=())


class TargetConfig(Node):
    """Hold the settings every single target shares; concrete targets add their own fields.

    A target that serves its source document reads no local file the document would publish. Names are checked as
    identifiers of the newest target until `check_target` checks them against the target.
    """

    model_config = ConfigDict(revalidate_instances="always")

    output: Path
    package: Dotted
    model_package: Dotted

    _option_prefix: ClassVar[str | None] = None
    _serves_source: ClassVar[bool] = False

    def __init__(self, /, **data: Any) -> None:
        """Reject values and combinations the shared contract does not allow, in field order."""
        try:
            type(self).__pydantic_validator__.validate_python(data, self_instance=self, context={})
        except ValidationError as error:
            raise APIGenerationError(tuple(diagnostics(error, data)), option_prefix=self._option_prefix) from None

    def check_target(self, target_python_version: PythonVersion) -> None:
        """Reject the values the target Python version does not allow, such as names that are not its identifiers."""
        try:
            type(self).__pydantic_validator__.validate_python(self, context={"target": target_python_version})
        except ValidationError as error:
            raise APIGenerationError(tuple(diagnostics(error, self)), option_prefix=self._option_prefix) from None


__all__ = ("Dotted", "Node", "Refusal", "TargetConfig", "choices", "diagnostics", "valid", "version")
