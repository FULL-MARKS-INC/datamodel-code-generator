"""The tag plan (home H11): the union sites the tag passes read and the run-scoped ledger of what they recorded.

A union site is a field whose extras carry a discriminator object naming a property; :func:`iter_union_sites` is the
one match on ``extras["discriminator"]`` that the tag passes share, and each caller passes its own models, so it keeps
its order. :class:`TagPlan` holds what the tag passes of one parse recorded, in place of state spread over the
parser and the run; :meth:`TagPlan.reset` forgets it at the start of L4. The L3 reuse keys
(:func:`declared_union_tags`) and the L4 layout prediction (:func:`possible_tag_names`) are read here too.

Each pass keeps its slot in the pipeline, so today's timing stays; these rows name it until a decision after the module
loop replaces them:

- ``legacy-decide-in-union-module``: a union is tagged when its module is processed, reading the variants of later
  modules before their reuse, collapse and renames; msgspec unions that a struct of a later module may still tag wait
  in :attr:`TagPlan.deferred_msgspec` while :attr:`TagPlan.defers_related` holds.
- ``legacy-tag-from-earlier-union``: a variant keeps the tags of the first union that tags it, and its own tag includes
  a tag an earlier union wrote (:attr:`TagPlan.first_tags`).
- ``legacy-inline-variants-at-first-ask``: which inline variants take the tag they inherit is decided once, at the
  first collapse or apply that asks, over the parsed models as they are then (:attr:`TagPlan.inline_variants`).
- ``legacy-predicted-tag-names``: msgspec layout flattening at L4 reads a superset of the tag fields L5 writes
  (:func:`possible_tag_names`, with the mapping-path row ``legacy-any-key``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import chain
from typing import TYPE_CHECKING, Any, TypeAlias, final

from datamodel_code_generator._records import record
from datamodel_code_generator.model._decisions.tag_value import mapping_path_keys, same_tags, schema_path_keys
from datamodel_code_generator.model.base import DataModel, linearize_data_models
from datamodel_code_generator.parser._passes._shared import (
    _format_tags,
    _get_mapping_discriminator_values,
    _get_schema_path,
    _iter_discriminator_data_types,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from datamodel_code_generator._internal_utils import HashableComparable
    from datamodel_code_generator.model.base import DataModelFieldBase
    from datamodel_code_generator.parser._passes._shared import DiscriminatorValue

__all__ = [
    "DiscriminatorTag",
    "DiscriminatorUnion",
    "InlineVariantTags",
    "TagPlan",
    "UnionSite",
    "VariantTags",
    "declared_union_tags",
    "iter_union_sites",
    "possible_tag_names",
    "union_site",
]

InlineVariantTags: TypeAlias = "Mapping[int, tuple[tuple[DataModelFieldBase, DiscriminatorValue], ...]]"
"""The fields with one tag of each inline variant that may take the tag it inherits, by variant id, with their tags."""

DiscriminatorTag: TypeAlias = "tuple[DataModel, DataModelFieldBase, DiscriminatorValue | None]"
"""A msgspec struct a union tags, its tag field and the tag."""

DiscriminatorUnion: TypeAlias = "tuple[DataModel, list[DiscriminatorTag], str, dict[str, str]]"
"""A msgspec union to tag: its holder, the tags of its structs, the ``tag_field`` and the mapping."""


@final
@record(eq=False)
class UnionSite:
    """A field whose discriminator object names a property: the holder, the field and the discriminator."""

    model: DataModel
    field: DataModelFieldBase
    property_name: str
    """The property the discriminator names, as the schema writes it until L5 writes the Python name in its place."""
    discriminator: Mapping[str, Any]

    @property
    def mapping(self) -> dict[str, str]:
        """The mapping of the discriminator, empty where it declares none."""
        return self.discriminator.get("mapping", {})


def union_site(model: DataModel, field: DataModelFieldBase) -> UnionSite | None:
    """Return the site a field of a model is, or None where its extras carry no discriminator naming a property."""
    match field.extras.get("discriminator"):
        case {"propertyName": str() as property_name} as discriminator if property_name:
            return UnionSite(model, field, property_name, discriminator)
        case _:
            return None


def iter_union_sites(models: Iterable[DataModel]) -> Iterator[UnionSite]:
    """Yield the union sites of the models, field by field in the order of ``models``."""
    for model in models:
        for field_ in model.fields:
            if (site := union_site(model, field_)) is not None:
                yield site


@dataclass(slots=True)
class VariantTags:
    """The tags a variant keeps from the first union that tagged it, and the other tags unions map it to."""

    variant: DataModel
    property_name: str
    kept: list[DiscriminatorValue]
    owner: str
    others: dict[str, list[DiscriminatorValue]]

    def warning_messages(self) -> Iterator[str]:
        """Describe the tags that the generated variant cannot carry."""
        if self.others:
            name = self.variant.class_name
            unions = ", ".join(f"{union} ({_format_tags(tags)})" for union, tags in self.others.items())
            yield (
                f"{name} is a variant of discriminated unions that map {self.property_name!r} to different tags: "
                f"it keeps {_format_tags(self.kept)} from {self.owner}, so these unions cannot decode it: {unions}."
            )


@final
@dataclass(slots=True)
class TagPlan:
    """What the tag passes of one parse recorded, by the ids of the models and fields they read.

    L3 replaces :attr:`reuse_snapshot` in every parse; :meth:`reset` forgets the rest at the start of L4.
    """

    reuse_snapshot: dict[DataModel, tuple[HashableComparable, ...]] = field(default_factory=dict)
    """The field index, property and tags of each discriminated union of each model, before reuse merges any."""
    inline_variants: InlineVariantTags | None = None
    """The tag fields of the inline variants that may take the tag they inherit, once the first union asks."""
    first_tags: dict[tuple[int, str], VariantTags] = field(default_factory=dict)
    """The tags of discriminated union variants, by variant id and tag property."""
    reused_tag_fields: dict[int, DataModelFieldBase] = field(default_factory=dict)
    """The tag fields that a union with a reused variant annotates with a new tag, by field id."""
    deferred_msgspec: list[DiscriminatorUnion] = field(default_factory=list)
    """The msgspec unions whose tags wait until every module is processed."""
    defers_related: bool = False
    """Whether modules remain whose unions may tag a struct, so the unions such a struct blocks wait."""
    opted_out: list[DataModel] = field(default_factory=list)
    """The structs below tagged bases that L6 leaves untagged."""

    def reset(self) -> None:
        """Forget what the passes of an earlier parse recorded after L3."""
        self.inline_variants = None
        self.first_tags.clear()
        self.reused_tag_fields.clear()
        self.deferred_msgspec.clear()
        self.defers_related = False
        self.opted_out.clear()

    def record_reused_tag_field(self, field_: DataModelFieldBase, *, annotates_tag: bool) -> None:
        """Remember a field that a union with a reused variant annotates with a new tag, else forget it.

        Such unions were left untagged before, so the ``Literal`` that only their tags import must not rename a model.
        """
        if annotates_tag:
            self.reused_tag_fields[id(field_)] = field_
        else:
            self.reused_tag_fields.pop(id(field_), None)

    def record_variant_tags(  # noqa: PLR0913
        self,
        variant: DataModel,
        union_name: str,
        property_name: str,
        values: list[DiscriminatorValue],
        *,
        union_tags: Callable[[], list[DiscriminatorValue]],
        declares_tag: Callable[[], bool],
    ) -> None:
        """Remember the tags a union maps a variant to, unless the variant declares its own tag.

        The values of a later union come from the tag an earlier union wrote, so the tags it maps the variant to are
        derived again (``union_tags``) from its mapping or the schema name.
        """
        if (recorded := self.first_tags.get(key := (id(variant), property_name))) is not None:
            if not same_tags(values, recorded.kept, "legacy-python-equality"):
                recorded.others.setdefault(recorded.owner, recorded.kept)
                recorded.kept, recorded.owner = values, union_name
                recorded.others = {
                    union: tags
                    for union, tags in recorded.others.items()
                    if not same_tags(tags, values, "legacy-python-equality")
                }
            elif not same_tags(tags := union_tags(), values, "legacy-python-equality"):
                recorded.others.setdefault(union_name, tags)
            return
        if not declares_tag():
            self.first_tags[key] = VariantTags(variant, property_name, values, union_name, {})

    def record_collapsed_tags(
        self,
        variant: DataModel,
        union_name: str,
        property_name: str,
        values: list[DiscriminatorValue],
        tags: list[DiscriminatorValue],
    ) -> None:
        """Remember the tags a union maps a variant to when collapsed reuse replaced the model the union names.

        The union maps the removed duplicate to ``tags``, which its own schema, the mapping or its name give and the
        kept model need not carry. They replace the tags derived from the kept model for that union.
        """
        key = (id(variant), property_name)
        if not same_tags(tags, values, "legacy-python-equality"):
            self.first_tags.setdefault(key, VariantTags(variant, property_name, values, union_name, {})).others[
                union_name
            ] = tags
        elif (recorded := self.first_tags.get(key)) is not None:
            recorded.others.pop(union_name, None)


def declared_union_tags(model: DataModel) -> tuple[HashableComparable, ...]:
    """Return the field index, property and the tags of each variant of each discriminated union of a model.

    Unions that render alike decode other tags when their discriminators name another property or map their
    variants to other tags. The tags a mapping gives the variants are listed by variant position, so unions that
    map the same tags to variants that reuse merges match whatever schemas their mappings name. Every discriminator
    object counts, whether it names a property or not.
    """
    if not (
        discriminators := [
            (index, discriminator, field_.data_type.data_types)
            for index, field_ in enumerate(model.fields)
            if isinstance(discriminator := field_.extras.get("discriminator"), dict)
        ]
    ):
        return ()
    from datamodel_code_generator._internal_utils import to_hashable  # noqa: PLC0415

    return tuple(
        (
            index,
            str(discriminator.get("propertyName")),
            tuple(
                to_hashable(_get_mapping_discriminator_values(source, mapping))
                if (mapping := discriminator.get("mapping"))
                and (reference := data_type.reference)
                and isinstance(source := reference.source, DataModel)
                else ()
                for data_type in data_types
            ),
        )
        for index, discriminator, data_types in discriminators
    )


def possible_tag_names(
    sites: Iterable[UnionSite],
    layout_models: list[DataModel],
    field_name: Callable[[str, str | None], str],
) -> dict[int, frozenset[str]]:
    """Return, by model whose layout counts, the names of the fields that the discriminator step may make tags.

    It tags the variants that the unions of ``sites`` list, however nested or aliased, or that their mappings name,
    by the property name or the field name it resolves to (``field_name`` of the property and a class name, or
    None). A struct inheriting from a tagged struct takes its tag field as a tag too. The mapped paths are indexed
    once by every key a model path can match them by (row ``legacy-any-key``).
    """
    listed: defaultdict[int, set[str]] = defaultdict(set)
    mapped: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for site in sites:
        property_name = site.property_name
        for reference in (
            data_type.reference
            for data_type, _, _ in _iter_discriminator_data_types(site.field.data_type.all_data_types)
            if data_type.reference
        ):
            listed[id(reference.source)].add(property_name)
        if isinstance(mapping := site.discriminator.get("mapping"), dict):
            for path in mapping.values():
                for key in mapping_path_keys(str(path), rule="legacy-any-key"):
                    mapped[key].add(property_name)
    variant_names: dict[int, set[str]] = {}
    for model in layout_models:
        property_names = set(listed.get(id(model), ()))
        for reference in (model.reference, *(base.reference for base in model.base_classes if base.reference)):
            for key in schema_path_keys(_get_schema_path(reference)):
                property_names.update(mapped.get(key, ()))
        variant_names[id(model)] = {
            name
            for property_name in property_names
            for class_name in (None, model.class_name)
            for name in (property_name, field_name(property_name, class_name))
        }
    return {
        id(model): frozenset(
            chain.from_iterable(variant_names.get(id(ancestor), ()) for ancestor in linearize_data_models([model]))
        )
        for model in layout_models
    }
