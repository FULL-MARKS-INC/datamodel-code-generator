"""Schema signals for option recommendations: one walk over the loaded schema document."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, get_args

from datamodel_code_generator.reference import get_singular_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence
    from collections.abc import Set as AbstractSet

SignalName: TypeAlias = Literal[
    "components",
    "operations",
    "inline_objects",
    "inline_enums",
    "component_enums",
    "all_enums",
    "max_inline_depth",
    "shared_inline_names",
    "shared_inline_object_names",
    "shared_inline_enum_names",
    "inline_names_equal_component",
    "duplicate_inline_objects",
    "duplicate_inline_shapes",
    "duplicate_inline_enums",
    "duplicate_inline_enum_shapes",
    "parent_copy_union_branches",
    "titled_components",
    "component_title_duplicate_groups",
    "component_titles_in_duplicate_groups",
    "component_titles_equal_own_key",
    "component_titles_equal_other_key",
    "inline_titled_objects",
    "inline_title_max_repeat",
    "inline_titles_equal_component",
    "unions",
    "discriminated_unions",
    "inline_union_variants",
    "inline_object_union_variants",
    "inline_scalar_union_variants",
    "inline_titled_union_variants",
    "inline_tagged_union_branches",
    "tagged_behind_ref",
    "single_key_union_branches",
    "single_key_required_open",
    "single_key_object_branches",
    "single_value_enums",
    "single_value_enum_union_branches",
    "dotted_keys",
    "dotted_key_namespaces",
    "dotted_namespace_reference_cycles",
    "root_is_object",
    "root_title_words",
    "estimated_models",
    "integer_enums_with_null",
    "number_enums_with_null",
    "module_imports_named_like_fields",
    "inline_models_in_dotted_cycles",
    "unique_items_with_constrained_items",
    "objects_differing_in_unique_items",
    "discriminator_tags_with_pattern",
    "optional_enum_discriminator_tags",
    "optional_const_discriminator_tags",
    "defaulted_enum_discriminator_tags",
    "defaulted_const_discriminator_tags",
    "pattern_properties_model_values",
    "pattern_properties_with_additional",
    "number_multiple_of",
    "allof_primitive_constraints",
    "allof_properties",
    "allof_nested_unions",
    "allof_independent_bases",
    "constrained_primitive_branches",
    "unbounded_int32_int64",
    "prefix_items_not_fixed",
    "not_const_or_enum",
    "oneof_overlapping_branches",
    "singularized_compound_names",
]
FactName: TypeAlias = Literal[
    "schema",
    "shared_inline_name_examples",
    "inline_title_examples",
    "duplicate_title_examples",
    "key_style",
    "root_title",
    "scopes",
]
SchemaKind: TypeAlias = Literal["openapi", "jsonschema", "asyncapi"]
SIGNAL_NAMES: frozenset[str] = frozenset(get_args(SignalName))

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options", "trace"})
_SCHEMA_MAP_KEYWORDS = ("properties", "patternProperties", "dependentSchemas")
_SUBSCHEMA_KEYWORDS = (
    "allOf",
    "anyOf",
    "oneOf",
    "prefixItems",
    "items",
    "additionalItems",
    "additionalProperties",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
)
_SHAPE_IGNORED_KEYWORDS = frozenset({"description", "title", "example", "examples", "$comment"})
_CONSTRAINT_ONLY_KEYWORDS = frozenset({"type", "properties", "required", "description", "title", "$comment"})
_NORMALIZE = re.compile(r"[^a-z0-9]")
_EXAMPLE_LIMIT = 4
_TAG_REFERENCE_DEPTH = 5
_PRIMITIVE_TYPES = frozenset({"string", "integer", "number", "boolean"})
_BOUND_KEYWORDS = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"})
_CONSTRAINT_KEYWORDS = _BOUND_KEYWORDS | {"minLength", "maxLength", "pattern", "multipleOf"}
_UNION_KEYWORDS = ("oneOf", "anyOf")
_SIBLING_KEYWORDS = (*_SCHEMA_MAP_KEYWORDS, *_SUBSCHEMA_KEYWORDS)
_REQUIRED_ONLY_KEYWORDS = frozenset({"required", "description", "title", "$comment"})
_WIDE_INTEGER_FORMATS = frozenset({"int32", "int64"})
_NAME_WORDS = re.compile(r"[A-Z][a-z0-9]*|[a-z0-9]+")
_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True, slots=True)
class Signals:
    """Counts and example texts measured on one schema document, and the normalized names of its schemas."""

    counts: Mapping[str, int]
    facts: Mapping[str, str]
    component_names: frozenset[str] = frozenset()


def normalize_name(name: str) -> str:
    """Return a name reduced to lowercase letters and digits, as class names compare after case conversion."""
    return _NORMALIZE.sub("", name.lower())


def _module(key: str) -> str:
    return key.rpartition(".")[0]


def _is_object(node: Mapping[str, Any]) -> bool:
    return isinstance(properties := node.get("properties"), dict) and bool(properties)


def _is_enum(node: Mapping[str, Any]) -> bool:
    return isinstance(values := node.get("enum"), list) and any(value is not None for value in values)


def _is_null(node: Mapping[str, Any]) -> bool:
    return node.get("type") in ("null", ["null"]) or node.get("enum") == [None]


def _single_literal(node: object) -> tuple[str, object] | None:
    """Return the form and value of a schema that allows exactly one value."""
    if not isinstance(node, dict):
        return None
    if "const" in node:
        return "const", node["const"]
    if isinstance(values := node.get("enum"), list) and len(values) == 1:
        return "enum", values[0]
    return None


def _type(node: Mapping[str, Any]) -> str | None:
    """Return the type of a schema that declares a single type."""
    return kind if isinstance(kind := node.get("type"), str) else None


def _constrained_primitive(node: object) -> bool:
    """Return whether a schema is a primitive type with its own constraints and no reference."""
    return (
        isinstance(node, dict)
        and "$ref" not in node
        and _type(node) in _PRIMITIVE_TYPES
        and not _CONSTRAINT_KEYWORDS.isdisjoint(node)
    )


def _null_enum_type(node: Mapping[str, Any]) -> str | None:
    """Return integer or number for an enum of that type which lists null without being nullable."""
    values = node.get("enum")
    if not isinstance(values, list) or None not in values or node.get("nullable") is True:
        return None
    return kind if (kind := _type(node)) in {"integer", "number"} else None


def _fixed_prefix_items(node: Mapping[str, Any], prefix_items: list[Any]) -> bool:
    """Return whether an array with prefixItems has exactly as many items as it lists, which becomes a tuple."""
    return node.get("minItems") == node.get("maxItems") == len(prefix_items)


def _class_words(name: str) -> list[str]:
    return _NAME_WORDS.findall("".join(word[:1].upper() + word[1:] for word in _SEPARATORS.split(name)))


def _singularizes_compound(name: str) -> bool:
    """Return whether singularizing a compound property name as one word differs from singularizing its last word."""
    if len(words := _class_words(name)) < 2:  # noqa: PLR2004
        return False
    whole = "".join(words)
    return get_singular_name(whole) != "".join(words[:-1]) + get_singular_name(words[-1])


def _subschemas(node: Mapping[str, Any]) -> Iterator[object]:
    """Yield the subschemas of a schema object that are not keyed by a name."""
    for keyword in _SUBSCHEMA_KEYWORDS:
        value = node.get(keyword)
        yield from value if isinstance(value, list) else (value,)


def _key_style(key: str) -> str:
    if "." in key:
        return "dotted"
    if "-" in key:
        return "kebab-case"
    if "_" in key:
        return "snake_case"
    if key[:1].isupper():
        return "PascalCase"
    return "camelCase" if any(char.isupper() for char in key) else "lowercase"


def _example_text(counter: Counter[str]) -> str:
    return ", ".join(f"{name} x{count}" for name, count in counter.most_common(_EXAMPLE_LIMIT))


def _resolve_local(document: Mapping[str, Any], ref: object) -> object:
    """Return the schema a local JSON Pointer reference points at, or None."""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    node: object = document
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _items(value: object) -> Iterable[tuple[str, Any]]:
    return value.items() if isinstance(value, dict) else ()


def _list(value: object) -> list[dict[str, Any]]:
    """Return the schema objects of a list of subschemas, or none."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _list_of(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _declares_properties(node: object) -> bool:
    """Return whether a schema declares properties itself or in an inline allOf member."""
    return isinstance(node, dict) and (
        _is_object(node) or any("$ref" not in member and _is_object(member) for member in _list(node.get("allOf")))
    )


def _ancestors(document: Mapping[str, Any], ref: str) -> set[str]:
    """Return the references a schema extends through allOf, transitively."""
    seen: set[str] = set()
    pending = [ref]
    while pending:
        target = _resolve_local(document, pending.pop())
        parents = {member["$ref"] for member in _list(_get(target, "allOf")) if isinstance(member.get("$ref"), str)}
        pending.extend(parents - seen)
        seen |= parents
    return seen


def _field_owners(document: Mapping[str, Any], ref: str) -> frozenset[str]:
    """Return the schemas that declare the properties a referenced schema has: itself and what it extends."""
    return frozenset(
        owner for owner in (ref, *_ancestors(document, ref)) if _declares_properties(_resolve_local(document, owner))
    )


def _root_model(node: object) -> bool:
    """Return whether a named schema becomes a root model: a primitive, an array, or a union, not an object or enum."""
    return (
        isinstance(node, dict)
        and not _is_object(node)
        and not _is_enum(node)
        and "allOf" not in node
        and (_type(node) in {*_PRIMITIVE_TYPES, "array"} or any(keyword in node for keyword in _UNION_KEYWORDS))
    )


def _unwrap(node: object) -> object:
    """Return the schema of an AsyncAPI multi-format schema object, or the schema itself."""
    if isinstance(node, dict) and ("schemaFormat" in node or node.keys() == {"schema"}):
        return node.get("schema")
    return node


def _uses_definitions(kind: SchemaKind) -> bool:
    """Return whether named schemas live in definitions/$defs, as in JSON Schema and Swagger 2.0 read as JSON Schema."""
    return kind == "jsonschema"


def _components(document: Mapping[str, Any], kind: SchemaKind) -> dict[str, Any]:
    """Return the named schemas of a document: $defs and definitions, or components/schemas."""
    if _uses_definitions(kind):
        return {key: value for keyword in ("$defs", "definitions") for key, value in _items(document.get(keyword))}
    components = document.get("components")
    return {
        key: _unwrap(value)
        for key, value in _items(components.get("schemas") if isinstance(components, dict) else None)
    }


def _component_refs(kind: SchemaKind, components: Iterable[str]) -> dict[str, str]:
    prefixes = ("#/$defs/", "#/definitions/") if _uses_definitions(kind) else ("#/components/schemas/",)
    return {f"{prefix}{key}": key for key in components for prefix in prefixes}


def _media_schemas(key: str, container: object) -> Iterator[tuple[str, object]]:
    """Yield the media type schemas of a request body or response."""
    content = container.get("content") if isinstance(container, dict) else None
    for media_type, media in _items(content):
        yield f"{key} {media_type}", media.get("schema") if isinstance(media, dict) else None


def _path_selected(path: str, include_paths: Sequence[str]) -> bool:
    slashed = path if path.startswith("/") else f"/{path}"
    return not include_paths or any(
        fnmatch(slashed, pattern if pattern.startswith("/") else f"/{pattern}") for pattern in include_paths
    )


def _operation_schemas(
    document: Mapping[str, Any], scopes: AbstractSet[str], include_paths: Sequence[str]
) -> Iterator[tuple[str, object]]:
    """Yield the request, response, parameter, and request body schemas the OpenAPI scopes generate."""
    api = "api" in scopes
    for section in ("paths", "webhooks"):
        items = document.get(section) if api or section in scopes else None
        for path, item in _items(items):
            if section == "webhooks" or _path_selected(path, include_paths):
                for method, operation in _items(item):
                    yield from _operation_parts(f"{path} {method}", operation)
    components = document.get("components")
    bodies = (
        components.get("requestBodies") if isinstance(components, dict) and (api or "requestbodies" in scopes) else None
    )
    for name, body in _items(bodies):
        yield from _media_schemas(name, body)


def _operation_parts(key: str, operation: object) -> Iterator[tuple[str, object]]:
    """Yield the request body, response, and parameter schemas of one operation.

    Generation parses every parameter's schema of the operations it walks, so inline enums and objects of parameters
    are models whether or not the parameters scope also emits the operation's parameters model.
    """
    if not isinstance(operation, dict) or key.rpartition(" ")[2] not in _HTTP_METHODS:
        return
    yield from _media_schemas(f"{key} request", operation.get("requestBody"))
    for status, response in _items(operation.get("responses")):
        yield from _media_schemas(f"{key} {status}", response)
    fields = {
        name: parameter.get("schema")
        for parameter in operation.get("parameters") or ()
        if isinstance(parameter, dict) and isinstance(name := parameter.get("name"), str)
    }
    yield f"{key} parameters", {"type": "object", "properties": fields} if fields else None


def _message_schemas(document: Mapping[str, Any]) -> Iterator[tuple[str, object]]:
    """Yield the payload and header schemas of AsyncAPI messages in components and channels."""
    components = document.get("components")
    components = components if isinstance(components, dict) else {}
    messages = list(_items(components.get("messages")))
    for name, channel in [*_items(document.get("channels")), *_items(components.get("channels"))]:
        messages.extend(
            (f"{name} {operation}", _operation_message(channel, operation)) for operation in ("publish", "subscribe")
        )
        messages.extend((f"{name} {key}", message) for key, message in _items(_get(channel, "messages")))
    for name, message in messages:
        variants = _get(message, "oneOf")
        for index, variant in enumerate(variants if isinstance(variants, list) else [message]):
            for part in ("payload", "headers"):
                yield f"{name} {index} {part}", _unwrap(_get(variant, part))


def _get(node: object, key: str) -> object:
    return node.get(key) if isinstance(node, dict) else None


def _operation_message(channel: object, operation: str) -> object:
    return _get(_get(channel, operation), "message")


def _count_operations(document: Mapping[str, Any], kind: SchemaKind) -> int:
    """Count OpenAPI path operations, or AsyncAPI channel and top-level operations."""
    if kind == "openapi":
        return sum(method in _HTTP_METHODS for _, item in _items(document.get("paths")) for method, _ in _items(item))
    return len(dict(_items(document.get("operations")))) + sum(
        isinstance(_get(channel, operation), dict)
        for _, channel in _items(document.get("channels"))
        for operation in ("publish", "subscribe")
    )


def _schema_label(document: Mapping[str, Any], kind: SchemaKind) -> str:
    if "swagger" in document:
        return f"Swagger {document['swagger']}"
    if kind == "jsonschema":
        schema_uri = document.get("$schema")
        return f"JSON Schema ({schema_uri})" if isinstance(schema_uri, str) else "JSON Schema"
    return f"{'OpenAPI' if kind == 'openapi' else 'AsyncAPI'} {document.get(kind)}"


class _Walker:
    """Collect the counts of one document in a single pass over its named schemas."""

    def __init__(self, document: Mapping[str, Any], refs: Mapping[str, str]) -> None:
        self.document = document
        self.refs = refs
        self.counts: Counter[str] = Counter()
        self.name_parents: defaultdict[str, set[str]] = defaultdict(set)
        self.object_name_parents: defaultdict[str, set[str]] = defaultdict(set)
        self.enum_name_parents: defaultdict[str, set[str]] = defaultdict(set)
        self.inline_titles: Counter[str] = Counter()
        self.shape_ids: dict[object, int] = {}
        self.object_shapes: Counter[int] = Counter()
        self.enum_shapes: Counter[int] = Counter()
        self.references: defaultdict[str, set[str]] = defaultdict(set)
        self.max_depth = 0
        self.property_names: defaultdict[str, set[str]] = defaultdict(set)
        self.inline_models: Counter[str] = Counter()
        self.unique_shapes: set[int] = set()
        self.shape_memo: dict[tuple[int, bool], int] = {}
        self.objects: list[Mapping[str, Any]] = []
        self.unique_items = False

    def shape(self, node: object, *, loose: bool = False) -> int:
        """Return an interned id of a schema's structure, ignoring documentation keywords.

        A loose shape also ignores ``uniqueItems``, so it equals the shape of the schema without them. The shapes of
        the document's objects and lists are remembered, since enclosing schemas take them again.
        """
        container = isinstance(node, dict | list)
        if container and (known := self.shape_memo.get((id(node), loose))) is not None:
            return known
        children: list[int] = []
        if isinstance(node, dict):
            items = [
                (name, self.shape(value, loose=loose))
                for name, value in sorted(node.items())
                if name not in _SHAPE_IGNORED_KEYWORDS
                and not name.startswith("x-")
                and not (loose and name == "uniqueItems")
            ]
            key: object = tuple(items)
            children = [child for _, child in items]
        elif isinstance(node, list):
            children = [self.shape(value, loose=loose) for value in node]
            key = ("l", *children)
        else:
            key = ("v", type(node).__name__, repr(node))
        shape = self.shape_ids.setdefault(key, len(self.shape_ids))
        if not loose and (
            (isinstance(node, dict) and "uniqueItems" in node) or not self.unique_shapes.isdisjoint(children)
        ):
            self.unique_shapes.add(shape)
        if container:
            self.shape_memo[id(node), loose] = shape
        return shape

    def unique_item_variants(self) -> int:
        """Count the groups of object schemas that differ only in uniqueItems; a schema without any counts none."""
        groups: defaultdict[int, set[int]] = defaultdict(set)
        for node in self.objects if self.unique_items else ():
            shape = self.shape(node)
            groups[self.shape(node, loose=True) if shape in self.unique_shapes else shape].add(shape)
        return sum(len(group) > 1 for group in groups.values())

    def inspect(self, node: Mapping[str, Any]) -> None:
        """Count the constructs of one schema object that known generation issues depend on."""
        counts = self.counts
        if (kind := _null_enum_type(node)) is not None:
            counts[f"{kind}_enums_with_null"] += 1
        self.unique_items = self.unique_items or "uniqueItems" in node
        items = node.get("items")
        counts["unique_items_with_constrained_items"] += node.get("uniqueItems") is True and _constrained_primitive(
            items
        )
        counts["constrained_primitive_branches"] += _constrained_primitive(items) + sum(
            _constrained_primitive(branch) for keyword in _UNION_KEYWORDS for branch in _list(node.get(keyword))
        )
        counts["number_multiple_of"] += node.get("type") == "number" and "multipleOf" in node
        counts["unbounded_int32_int64"] += (
            node.get("type") == "integer"
            and node.get("format") in _WIDE_INTEGER_FORMATS
            and _BOUND_KEYWORDS.isdisjoint(node)
        )
        if isinstance(prefix_items := node.get("prefixItems"), list):
            counts["prefix_items_not_fixed"] += not _fixed_prefix_items(node, prefix_items)
        counts["not_const_or_enum"] += isinstance(negated := node.get("not"), dict) and not {
            "const",
            "enum",
        }.isdisjoint(negated)
        if isinstance(patterns := node.get("patternProperties"), dict) and patterns:
            counts["pattern_properties_model_values"] += sum(
                isinstance(value, dict) and isinstance(value.get("$ref"), str) for value in patterns.values()
            )
            counts["pattern_properties_with_additional"] += isinstance(node.get("additionalProperties"), dict)
        if (
            "discriminator" not in node
            and len(branches := [b for b in _list(node.get("oneOf")) if not _is_null(b)]) > 1
        ):
            counts["oneof_overlapping_branches"] += any(
                not branch.keys() <= _REQUIRED_ONLY_KEYWORDS for branch in branches
            )
        if members := _list(node.get("allOf")):
            self.inspect_all_of(members)
        if isinstance(discriminator := node.get("discriminator"), dict) and isinstance(
            name := discriminator.get("propertyName"), str
        ):
            self.inspect_discriminator(node, name)

    def inspect_all_of(self, members: list[dict[str, Any]]) -> None:
        """Count an allOf that nests a union, merges constrained primitives, or extends two independent bases."""
        counts = self.counts
        inline = [member for member in members if "$ref" not in member]
        counts["allof_nested_unions"] += any(keyword in member for member in inline for keyword in _UNION_KEYWORDS)
        counts["allof_primitive_constraints"] += (
            len(inline) == len(members)
            and any(_type(member) in _PRIMITIVE_TYPES for member in inline)
            and any(not _CONSTRAINT_KEYWORDS.isdisjoint(member) for member in inline)
            and not any(_is_object(member) or "allOf" in member or "items" in member for member in inline)
            and not any(keyword in member for member in inline for keyword in _UNION_KEYWORDS)
        )
        owners = [_field_owners(self.document, ref) for member in members if isinstance(ref := member.get("$ref"), str)]
        counts["allof_independent_bases"] += any(
            first - second and second - first for first in owners for second in owners
        )

    def inspect_discriminator(self, node: Mapping[str, Any], name: str) -> None:
        """Count the discriminator properties of union branches that keep a pattern or are optional single values.

        Optional single values count only in a union that generation discriminates: one with a mapping, or whose
        branches all have distinct single-value tags. A tag with a default counts apart, by its form.
        """
        counts = self.counts
        tags = [
            self.tag_property(branch, name)
            for keyword in _UNION_KEYWORDS
            for branch in _list(node.get(keyword))
            if not _is_null(branch)
        ]
        literals = [None if schema is None else _single_literal(schema) for schema, _ in tags]
        values = {json.dumps(literal[1], sort_keys=True) for literal in literals if literal is not None}
        discriminated = isinstance(node["discriminator"].get("mapping"), dict) or len(values) == len(tags)
        for (schema, required), literal in zip(tags, literals, strict=True):
            if schema is None:
                continue
            counts["discriminator_tags_with_pattern"] += "pattern" in schema
            if discriminated and not required and literal is not None:
                counts[f"{'defaulted' if 'default' in schema else 'optional'}_{literal[0]}_discriminator_tags"] += 1

    def tag_property(self, branch: Mapping[str, Any], name: str, depth: int = 0) -> tuple[dict[str, Any] | None, bool]:
        """Return the schema of a property a branch declares, directly or through $ref and allOf, and if required."""
        if depth > _TAG_REFERENCE_DEPTH:
            return None, False
        if isinstance(target := _resolve_local(self.document, branch.get("$ref")), dict):
            return self.tag_property(target, name, depth + 1)
        properties = branch.get("properties")
        schema = properties.get(name) if isinstance(properties, dict) else None
        required = name in _list_of(branch.get("required"))
        for member in _list(branch.get("allOf")):
            found, member_required = self.tag_property(member, name, depth + 1)
            schema = found if schema is None else schema
            required = required or member_required
        if isinstance(schema, dict) and isinstance(resolved := _resolve_local(self.document, schema.get("$ref")), dict):
            schema = resolved
        return (schema if isinstance(schema, dict) else None), required

    def count_single_value_enum(self, node: Mapping[str, Any]) -> None:
        """Count a schema whose enum has exactly one value and that declares no const."""
        values = node.get("enum")
        self.counts["single_value_enums"] += isinstance(values, list) and len(values) == 1 and "const" not in node

    def record_reference(self, node: Mapping[str, Any], component: str) -> bool:
        """Record a reference to a named schema; return whether the node is only a reference.

        A reference with subschemas beside it, such as a oneOf or properties, is walked like any schema.
        """
        if not isinstance(ref := node.get("$ref"), str):
            return False
        if (target := self.refs.get(ref)) is not None:
            self.references[component].add(target)
        return not any(keyword in node for keyword in _SIBLING_KEYWORDS)

    def walk_named(self, key: str, node: object) -> None:
        """Walk one named schema; the schema itself is the first parent model.

        Subschemas reached without a property (array items, union branches, allOf parts) are named after the
        schema itself, so they get an empty name and do not count as names shared with other parents.
        """
        if isinstance(node, dict) and not self.record_reference(node, key):
            self.count_single_value_enum(node)
            self.inspect(node)
            if _is_object(node):
                self.objects.append(node)
            self.walk_children(node, parent=key, name="", depth=0, component=key)

    def record_name(self, name: str, parent: str, parents: defaultdict[str, set[str]]) -> None:
        """Record the parent of an inline model named after a property."""
        if name:
            self.name_parents[name].add(parent)
            parents[name].add(parent)

    def walk(self, node: object, *, parent: str, name: str, depth: int, component: str) -> None:
        """Walk one inline schema reached under a parent model through a property or keyword."""
        if not isinstance(node, dict) or _is_null(node) or self.record_reference(node, component):
            return
        self.count_single_value_enum(node)
        self.inspect(node)
        if _is_enum(node):
            self.counts["inline_enums"] += 1
            self.inline_models[component] += 1
            self.record_name(name, parent, self.enum_name_parents)
            self.enum_shapes[self.shape(node)] += 1
        if _is_object(node):
            self.counts["inline_objects"] += 1
            self.inline_models[component] += 1
            self.record_name(name, parent, self.object_name_parents)
            self.objects.append(node)
            self.object_shapes[self.shape(node)] += 1
            if isinstance(title := node.get("title"), str) and title.strip():
                self.counts["inline_titled_objects"] += 1
                self.inline_titles[title.strip()] += 1
            depth += 1
            self.max_depth = max(self.max_depth, depth)
            parent = f"{parent}/{name}"
        self.walk_children(node, parent=parent, name=name, depth=depth, component=component)

    def walk_children(self, node: Mapping[str, Any], *, parent: str, name: str, depth: int, component: str) -> None:
        """Walk the subschemas of one schema object."""
        for member_name, member in _items(node.get("properties")):
            self.inspect_property(str(member_name), member, component)
        for keyword in _SCHEMA_MAP_KEYWORDS:
            members = node.get(keyword)
            for member_name, member in members.items() if isinstance(members, dict) else ():
                self.walk(
                    member, parent=parent, name=normalize_name(str(member_name)), depth=depth, component=component
                )
        for keyword in ("oneOf", "anyOf"):
            if isinstance(branches := node.get(keyword), list):
                self.count_variants(node, [branch for branch in branches if isinstance(branch, dict)])
        for member in _subschemas(node):
            self.walk(member, parent=parent, name=name, depth=depth, component=component)

    def inspect_property(self, name: str, node: object, component: str) -> None:
        """Record a property name of a named schema; count allOf properties and compound plural array names."""
        self.property_names[component].add(normalize_name(name))
        if not isinstance(node, dict) or "$ref" in node:
            return
        self.counts["allof_properties"] += any(
            "$ref" in member or _is_object(member) for member in _list(node.get("allOf"))
        )
        items = node.get("items")
        self.counts["singularized_compound_names"] += (
            isinstance(items, dict)
            and "$ref" not in items
            and (_is_object(items) or _is_enum(items))
            and _singularizes_compound(name)
        )

    def count_variants(self, node: Mapping[str, Any], branches: list[dict[str, Any]]) -> None:
        """Count one oneOf/anyOf: its inline variants, and the literal tags and keys that could name them."""
        real = [branch for branch in branches if not _is_null(branch)]
        if len(real) > 1:
            self.counts["unions"] += 1
            self.counts["discriminated_unions"] += "discriminator" in node
            for branch in real:
                if "$ref" in branch:
                    continue
                self.counts["inline_union_variants"] += 1
                self.counts["parent_copy_union_branches"] += self.copies_parent(node, branch)
                self.counts["inline_titled_union_variants"] += isinstance(branch.get("title"), str)
                is_object = _is_object(branch) or isinstance(branch.get("allOf"), list)
                self.counts["inline_object_union_variants" if is_object else "inline_scalar_union_variants"] += 1
        self.count_tags([branch for branch in branches if "$ref" not in branch])

    def count_tags(self, branches: list[dict[str, Any]]) -> None:
        """Count inline branches named by a distinct literal tag or a single required key, as generation names them."""
        values: defaultdict[str, list[str]] = defaultdict(list)
        for branch in branches:
            for name, (_, value) in self.tags(branch).items():
                values[name].append(json.dumps(value, sort_keys=True))
        distinct = {name for name, items in values.items() if len(set(items)) == len(items)}
        for branch in branches:
            if not isinstance(properties := branch.get("properties"), dict):
                self.counts["tagged_behind_ref"] += self.tagged_behind_ref(branch)
                continue
            if forms := {form for name, (form, _) in self.tags(branch).items() if name in distinct}:
                self.counts["inline_tagged_union_branches"] += 1
                self.counts["single_value_enum_union_branches"] += forms == {"enum"}
            else:
                self.counts["tagged_behind_ref"] += self.tagged_behind_ref(branch)
            if len(properties) == 1 and "oneOf" not in branch and "anyOf" not in branch:
                self.counts["single_key_union_branches"] += 1
                ((key, value),) = properties.items()
                named_by_key = key in (branch.get("required") or ()) and _single_literal(value) is None
                self.counts["single_key_required_open"] += named_by_key
                self.counts["single_key_object_branches"] += (
                    named_by_key and isinstance(value, dict) and "$ref" not in value and _is_object(value)
                )

    @staticmethod
    def copies_parent(node: Mapping[str, Any], branch: Mapping[str, Any]) -> bool:
        """Return whether a branch only constrains properties of the object that holds the union.

        Generation copies the holder's properties into a model for each such branch.
        """
        parent_properties = node.get("properties")
        properties = branch.get("properties", {})
        return (
            isinstance(parent_properties, dict)
            and bool(parent_properties)
            and branch.keys() <= _CONSTRAINT_ONLY_KEYWORDS
            and ("required" in branch or "properties" in branch)
            and isinstance(properties, dict)
            and all(value is True or value == {} or name in parent_properties for name, value in properties.items())
        )

    @staticmethod
    def tags(branch: Mapping[str, Any]) -> dict[str, tuple[str, object]]:
        """Return the literal tag properties declared directly on one schema."""
        properties = branch.get("properties")
        return {
            name: literal
            for name, value in (properties.items() if isinstance(properties, dict) else ())
            if (literal := _single_literal(value)) is not None
        }

    def tagged_behind_ref(self, branch: Mapping[str, Any], depth: int = 0) -> bool:
        """Return whether a schema the branch references or merges through allOf declares a literal tag."""
        if depth > _TAG_REFERENCE_DEPTH:
            return False
        groups = [branch.get("allOf")]
        if isinstance(target := _resolve_local(self.document, branch.get("$ref")), dict):
            if self.tags(target):
                return True
            groups.append(target.get("allOf"))
        return any(
            isinstance(member, dict) and (bool(self.tags(member)) or self.tagged_behind_ref(member, depth + 1))
            for group in groups
            if isinstance(group, list)
            for member in group
        )


def _reachable(start: str, edges: Mapping[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        for target in edges.get(stack.pop(), ()):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


def _reference_cycles(references: Mapping[str, set[str]]) -> set[frozenset[str]]:
    """Return the groups of two or more dotted-key namespaces whose schemas reference each other in a cycle."""
    edges: defaultdict[str, set[str]] = defaultdict(set)
    for source, targets in references.items():
        edges[_module(source)].update(_module(target) for target in targets)
    reach = {module: _reachable(module, edges) for module in edges}
    groups = {
        frozenset({module, *(other for other in reach[module] if module in reach.get(other, ()))}) for module in reach
    }
    return {group for group in groups if len(group) > 1}


def _shadowed_module_imports(walker: _Walker, components: Mapping[str, Any]) -> int:
    """Count modules that import a dotted-key module of root models only, and have a field named like it.

    The import is aliased after the field; once --collapse-root-models writes the root models in place, the module is
    not generated, but the aliased import stays.
    """
    namespaces: defaultdict[str, list[str]] = defaultdict(list)
    for key in components:
        namespaces[_module(key)].append(key)
    vanishing = {
        namespace
        for namespace, keys in namespaces.items()
        if namespace and all(_root_model(components[key]) for key in keys)
    }
    count = 0
    for module, keys in namespaces.items():
        names = set().union(*(walker.property_names[key] for key in keys))
        imported = {_module(target) for key in keys for target in walker.references[key]} & vanishing - {module}
        count += sum(normalize_name(namespace.rpartition(".")[2]) in names for namespace in imported)
    return count


def _count_modules(counts: Counter[str], walker: _Walker, components: Mapping[str, Any], dotted: list[str]) -> None:
    """Count the dotted-key namespaces in reference cycles, their inline models, and imports a field name shadows."""
    cycles = _reference_cycles(walker.references) if dotted else set()
    cyclic = set().union(*cycles)
    counts["dotted_namespace_reference_cycles"] = len(cycles)
    counts["inline_models_in_dotted_cycles"] = sum(
        walker.inline_models[key] for key in dotted if _module(key) in cyclic
    )
    counts["module_imports_named_like_fields"] = _shadowed_module_imports(walker, components) if dotted else 0


def _count_titles(counts: Counter[str], titles: Mapping[str, str], components: Iterable[str]) -> Counter[str]:
    """Count component titles that collide or repeat keys; return the shared titles with their group sizes."""
    keys = {normalize_name(key) for key in components}
    groups = Counter(normalize_name(title) for title in titles.values())
    shared = {title: count for title, count in groups.items() if count > 1}
    counts["titled_components"] = len(titles)
    counts["component_title_duplicate_groups"] = len(shared)
    counts["component_titles_in_duplicate_groups"] = sum(shared.values())
    counts["component_titles_equal_own_key"] = sum(
        normalize_name(title) == normalize_name(key) for key, title in titles.items()
    )
    counts["component_titles_equal_other_key"] = sum(
        (normalized := normalize_name(title)) != normalize_name(key) and normalized in keys
        for key, title in titles.items()
    )
    return Counter({
        title: shared[normalize_name(title)] for title in titles.values() if normalize_name(title) in shared
    })


def _count_names(counts: Counter[str], walker: _Walker, components: Iterable[str]) -> Counter[str]:
    """Count inline model names shared by parents or taken by components; return the shared names."""
    keys = {normalize_name(key) for key in components}
    shared = Counter({name: len(parents) for name, parents in walker.name_parents.items() if len(parents) > 1})
    counts["shared_inline_names"] = len(shared)
    counts["shared_inline_object_names"] = sum(len(parents) > 1 for parents in walker.object_name_parents.values())
    counts["shared_inline_enum_names"] = sum(len(parents) > 1 for parents in walker.enum_name_parents.values())
    counts["inline_names_equal_component"] = sum(name in keys for name in walker.name_parents)
    for repeats, shapes, groups in (
        ("duplicate_inline_objects", walker.object_shapes, "duplicate_inline_shapes"),
        ("duplicate_inline_enums", walker.enum_shapes, "duplicate_inline_enum_shapes"),
    ):
        repeated = [count for count in shapes.values() if count > 1]
        counts[repeats] = sum(repeated)
        counts[groups] = len(repeated)
    return shared


def _walk_components(walker: _Walker, components: Mapping[str, Any], roots: Iterable[str] | None) -> dict[str, Any]:
    """Walk the named schemas generation emits: all of them, or those the roots reference when roots are given."""
    pending = (
        sorted(components)
        if roots is None
        else sorted({target for root in roots for target in walker.references[root]})
    )
    walked: dict[str, Any] = {}
    while pending:
        if (key := pending.pop()) not in walked:
            walked[key] = components[key]
            walker.walk_named(key, components[key])
            pending.extend(sorted(walker.references[key] - walked.keys()))
    return dict(sorted(walked.items()))


def measure_signals(
    document: Mapping[str, Any],
    kind: SchemaKind,
    *,
    openapi_scopes: AbstractSet[str] = frozenset({"schemas"}),
    include_paths: Sequence[str] = (),
) -> Signals:
    """Measure the naming-relevant signals of one OpenAPI, AsyncAPI, or JSON Schema document.

    For OpenAPI, the scopes select what is measured as generation would emit it: component schemas with the schemas
    scope (otherwise only those the operations reference), and request, response, and parameter schemas with the
    path scopes.
    """
    all_components = _components(document, kind)
    walker = _Walker(document, _component_refs(kind, all_components))
    counts = walker.counts
    extra: list[tuple[str, object]] = []
    if kind == "openapi":
        extra = list(_operation_schemas(document, openapi_scopes, include_paths))
    elif kind == "asyncapi":
        extra = list(_message_schemas(document))
    for key, node in extra:
        walker.walk_named(key, node)
    whole = kind != "openapi" or bool({"schemas", "api"} & openapi_scopes)
    components = _walk_components(walker, all_components, None if whole else [key for key, _ in extra])
    named = {key: node for key, node in components.items() if isinstance(node, dict)}
    counts["component_enums"] = sum(_is_enum(node) for node in named.values())
    titles = {
        key: title.strip()
        for key, node in named.items()
        if isinstance(title := node.get("title"), str) and title.strip()
    }
    root_title = ""
    if kind == "jsonschema":
        root = {key: value for key, value in document.items() if key not in {"definitions", "$defs"}}
        walker.inspect(root)
        walker.walk_children(root, parent="#", name="", depth=0, component="")
        counts["root_is_object"] = _is_object(root) or root.get("type") == "object"
        root_title = title.strip() if isinstance(title := root.get("title"), str) else ""
        counts["root_title_words"] = len(root_title.split())
    counts["operations"] = _count_operations(document, kind)
    counts["components"] = len(components)
    counts["all_enums"] = counts["inline_enums"] + counts["component_enums"]
    counts["max_inline_depth"] = walker.max_depth
    shared_names = _count_names(counts, walker, components)
    shared_titles = _count_titles(counts, titles, components)
    counts["inline_title_max_repeat"] = max(walker.inline_titles.values(), default=0)
    component_words = {normalize_name(name) for name in (*components, *titles.values())}
    counts["inline_titles_equal_component"] = sum(
        count for title, count in walker.inline_titles.items() if normalize_name(title) in component_words
    )
    dotted = [key for key in components if "." in key]
    counts["dotted_keys"] = len(dotted)
    counts["dotted_key_namespaces"] = len({_module(key) for key in dotted})
    _count_modules(counts, walker, components, dotted)
    counts["objects_differing_in_unique_items"] = walker.unique_item_variants()
    counts["estimated_models"] = (
        counts["components"] + counts["inline_objects"] + counts["inline_enums"] + counts["inline_union_variants"]
    )
    styles = Counter(_key_style(key) for key in components).most_common(1)
    return Signals(
        counts={name: int(counts[name]) for name in sorted(SIGNAL_NAMES)},
        facts={
            "schema": _schema_label(document, kind),
            "scopes": ", ".join(sorted(openapi_scopes)) if kind == "openapi" else "",
            "shared_inline_name_examples": _example_text(shared_names),
            "inline_title_examples": _example_text(walker.inline_titles),
            "duplicate_title_examples": _example_text(shared_titles),
            "key_style": "".join(f"{style} ({count} of {len(components)})" for style, count in styles),
            "root_title": root_title,
        },
        component_names=frozenset(normalize_name(key) for key in all_components),
    )
