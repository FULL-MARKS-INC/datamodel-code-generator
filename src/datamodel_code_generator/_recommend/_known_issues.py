"""Known generation issues that option recommendations cite, and the config values that avoid them.

The entries ship as data (``resources/recommender_known_issues.json``). Each one names its GitHub issue, what the
defect does to the generated code, when a configuration hits it (schema signals and config values), the config values
that avoid it, and the repro that the retirement guard generates to check that the issue still occurs. An issue that
breaks the output (it fails at import, or generation stops) is avoided: the configuration takes the workaround, or a
configuration whose options cause it is rejected. Every other issue is cited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from datamodel_code_generator._recommend._rules import (
    Condition,
    ConfigCondition,
    IssueId,
    OptionValue,
    leaves,
    matches,
    option_arguments,
    toml_value,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from collections.abc import Set as AbstractSet

    from datamodel_code_generator._recommend._metrics import NameMetrics

__all__ = [
    "AppliedWorkaround",
    "Hit",
    "IssueCheck",
    "KnownIssue",
    "check_configuration",
    "cli_negation_issue",
    "config_tests",
    "issue_values",
    "load_known_issues",
    "names_issues",
]

KNOWN_ISSUES_RESOURCE = "recommender_known_issues.json"
Effect: TypeAlias = Literal[
    "import-error",
    "generation-error",
    "wrong-type",
    "lost-constraint",
    "rejects-valid",
    "not-enforced",
    "names",
    "cli-inexpressible",
]
SymptomMetric: TypeAlias = Literal[
    "numbered", "doubled_union_variants", "doubled_variant_children", "displaced", "union_variants_numbered"
]
BREAKING_EFFECTS: frozenset[str] = frozenset({"import-error", "generation-error"})


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Workaround(_Data):
    """Config values that avoid an issue, when their condition matches the configuration."""

    when: Condition | None = None
    set: dict[str, Any] = Field(min_length=1)


class Repro(_Data):
    """A generation that shows an issue, which the retirement guard runs.

    ``input`` is a file under tests/data/recommend_options/known_issues, run with ``args`` and ``--output`` set to
    ``output``. While the issue occurs, ``file`` (a path in the run directory, ``stdout``, or ``stderr``) contains
    ``shows`` (a regular expression when ``regex`` is set) and lacks at least one of the texts in ``lacks``, which a
    fix would add.
    """

    input: str
    args: tuple[str, ...]
    output: str | None = "model.py"
    file: str = "model.py"
    shows: str | None = None
    regex: bool = False
    lacks: tuple[str, ...] = ()


class KnownIssue(_Data):
    """One known issue of generation and how a recommendation treats it.

    Names issues are cited from their ``symptom`` metric in the trial, or from ``when`` without one; every other
    issue has a ``when``.
    """

    id: IssueId
    issue: int = Field(gt=0)
    effect: Effect
    when: Condition | None = None
    symptom: SymptomMetric | None = None
    workarounds: tuple[Workaround, ...] = ()
    message: str
    repro: Repro

    @property
    def breaks(self) -> bool:
        """Return whether the issue breaks the output: it fails at import, or generation stops."""
        return self.effect in BREAKING_EFFECTS

    def applies(self, counts: Mapping[str, int], values: Mapping[str, Any]) -> bool:
        """Return whether the issue's condition matches signal counts and config values."""
        return self.when is None or matches(self.when, counts, values)


class KnownIssuesData(_Data):
    """The known-issue file."""

    format_version: Literal[1]
    issues: tuple[KnownIssue, ...]


@dataclass(frozen=True, slots=True)
class LoadedIssues:
    """Known issues with their workarounds as pyproject.toml spells them, and the config fields they test or set."""

    issues: tuple[KnownIssue, ...]
    by_id: Mapping[str, KnownIssue]
    workarounds: Mapping[str, tuple[tuple[Condition | None, Mapping[str, OptionValue]], ...]]
    fields: frozenset[str]


def _pyproject_values(values: Mapping[str, Any]) -> dict[str, OptionValue]:
    """Return config values validated by the config model, as pyproject.toml spells them."""
    from datamodel_code_generator.base_config import BaseGenerateConfig  # noqa: PLC0415

    validated = BaseGenerateConfig.model_validate(values)
    return {name: toml_value(getattr(validated, name)) for name in values}


def config_tests(issue: KnownIssue) -> Iterator[ConfigCondition]:
    """Yield the config tests of an issue's condition and its workarounds' conditions."""
    for condition in (issue.when, *(workaround.when for workaround in issue.workarounds)):
        yield from (leaf for leaf in leaves(condition) if isinstance(leaf, ConfigCondition))


@cache
def load_known_issues() -> LoadedIssues:
    """Load the shipped known issues, validating their workaround values through the config model.

    The consistency of the entries with the rules, the option registry, and the command-line options is checked by
    the recommender's tests.
    """
    resource = files("datamodel_code_generator.resources").joinpath(KNOWN_ISSUES_RESOURCE)
    data = KnownIssuesData.model_validate_json(resource.read_text(encoding="utf-8"))
    workarounds = {
        issue.id: tuple((workaround.when, _pyproject_values(workaround.set)) for workaround in issue.workarounds)
        for issue in data.issues
    }
    fields = frozenset({
        *(test.config for issue in data.issues for test in config_tests(issue)),
        *(name for values in workarounds.values() for _, workaround in values for name in workaround),
    })
    return LoadedIssues(data.issues, {issue.id: issue for issue in data.issues}, workarounds, fields)


def issue_values(config: Any) -> dict[str, Any]:
    """Return the config values the known issues test or set, as pyproject.toml spells them."""
    return {name: toml_value(getattr(config, name)) for name in load_known_issues().fields}


@dataclass(frozen=True, slots=True)
class AppliedWorkaround:
    """The config values a configuration takes to avoid a known issue.

    ``arguments`` express them on the command line, and ``previous`` are the values the configuration had before.
    """

    issue: KnownIssue
    values: Mapping[str, OptionValue]
    arguments: list[str]
    previous: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Hit:
    """A known issue a configuration has, and what would avoid it.

    ``avoided_by`` are config values that avoid it without causing an issue that breaks the output; ``dropping`` are
    the fields the user set whose removal avoids it; ``conflicts`` are issues that break the output and that no single
    configuration avoids together with this one.
    """

    issue: KnownIssue
    avoided_by: Mapping[str, OptionValue] = field(default_factory=dict)
    dropping: tuple[str, ...] = ()
    conflicts: tuple[KnownIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class IssueCheck:
    """The known issues of one configuration.

    ``workarounds`` are the values it takes to avoid issues that break the output; ``blocking`` is such an issue that
    its options cause, or keep from being avoided; ``hits`` are the issues it still has, except names issues, which
    the report finds from the trial; ``values`` are its config values with the workarounds.
    """

    workarounds: tuple[AppliedWorkaround, ...]
    blocking: KnownIssue | None
    hits: tuple[Hit, ...]
    values: Mapping[str, Any]

    @property
    def rejection(self) -> KnownIssue | None:
        """Return the issue that rejects this configuration in a trial, if any.

        That is an issue its options cause, one that stops generation, or one that conflicts with another issue that
        breaks the output.
        """
        conflicted = (hit.issue for hit in self.hits if hit.conflicts or hit.issue.effect == "generation-error")
        return self.blocking or next(conflicted, None)


@dataclass(frozen=True, slots=True)
class _Configuration:
    """Signal counts and the config values of a base configuration with option values on top."""

    counts: Mapping[str, int]
    values_of: Callable[[Mapping[str, OptionValue]], Mapping[str, Any]]

    def breaking(self, values: Mapping[str, Any]) -> set[str]:
        """Return the ids of the issues that break the output of a configuration."""
        return {issue.id for issue in load_known_issues().issues if issue.breaks and issue.applies(self.counts, values)}

    def avoiding(
        self, issue: KnownIssue, values: Mapping[str, Any], options: Mapping[str, OptionValue]
    ) -> tuple[list[Mapping[str, OptionValue]], set[str]]:
        """Return the workarounds that avoid an issue on top of options without breaking the output in a new way.

        Also return the issues that break the output only with the other workarounds that avoid it.
        """
        before = self.breaking(values)
        safe: list[Mapping[str, OptionValue]] = []
        introduced: set[str] = set()
        for condition, workaround in load_known_issues().workarounds[issue.id]:
            if condition is not None and not matches(condition, self.counts, values):
                continue
            after = self.values_of({**options, **workaround})
            if issue.applies(self.counts, after):
                continue
            if new := self.breaking(after) - before:
                introduced |= new
            else:
                safe.append(workaround)
        return safe, introduced

    def dropping(
        self, issue: KnownIssue, options: Mapping[str, OptionValue], explicit: AbstractSet[str]
    ) -> tuple[str, ...]:
        """Return a switch the user set whose removal avoids the issue without breaking the output in a new way."""
        from datamodel_code_generator.config import GenerateConfig  # noqa: PLC0415

        before = self.breaking(self.values_of(options))
        for name in sorted({test.config for test in config_tests(issue)} & explicit):
            if (default := GenerateConfig.model_fields[name].default) is not None and default is not False:
                continue
            after = self.values_of({**options, name: default})
            if not issue.applies(self.counts, after) and not self.breaking(after) - before:
                return (name,)
        return ()


@dataclass(slots=True)
class _Avoidance:
    """One pass that avoids the issues breaking a configuration; ``conflicts`` are pairs it found to exclude."""

    applied: dict[str, OptionValue] = field(default_factory=dict)
    setters: dict[str, str] = field(default_factory=dict)
    workarounds: list[AppliedWorkaround] = field(default_factory=list)
    conflicts: set[str] = field(default_factory=set)


def check_configuration(
    counts: Mapping[str, int],
    values_of: Callable[[Mapping[str, OptionValue]], Mapping[str, Any]],
    options: Mapping[str, OptionValue],
    explicit: AbstractSet[str],
    *,
    avoid: bool = True,
) -> IssueCheck:
    """Check a configuration, ``options`` on top of its base, against the known issues.

    ``values_of`` returns the config values of the base with option values on top. With ``avoid``, each issue that
    breaks the output takes the first workaround that avoids it, keeps the values the user set, and breaks nothing
    else. Issues whose workarounds exclude each other, by setting one field differently or by breaking the output in
    another way, take none of them and are hits that name each other. When only workarounds that change one of the
    options would avoid an issue, or nothing avoids it and only the options cause it, the issue blocks the
    configuration. Any other issue that matches is a hit, except names issues.
    """
    configuration = _Configuration(counts, values_of)
    excluded: set[str] = set()
    while True:
        result = _avoid(configuration, options, explicit, excluded, avoid=avoid)
        if isinstance(result, IssueCheck):
            return result
        excluded |= result


def _avoid(
    configuration: _Configuration,
    options: Mapping[str, OptionValue],
    explicit: AbstractSet[str],
    excluded: AbstractSet[str],
    *,
    avoid: bool,
) -> IssueCheck | set[str]:
    """Avoid the breaking issues of a configuration, leaving ``excluded`` ones unavoided.

    Return the check, or the ids of issues whose workarounds turn out to exclude each other.
    """
    counts, values_of = configuration.counts, configuration.values_of
    data = load_known_issues()
    state = _Avoidance()
    hits: list[Hit] = []
    handled: set[str] = set()
    values = values_of(options)
    while (
        issue := next(
            (item for item in data.issues if item.breaks and item.id not in handled and item.applies(counts, values)),
            None,
        )
    ) is not None:
        handled.add(issue.id)
        safe, introduced = configuration.avoiding(issue, values, {**options, **state.applied})
        allowed = [item for item in safe if not item.keys() & explicit] if avoid and issue.id not in excluded else []
        clashing = {
            state.setters[name]
            for item in allowed
            for name, value in item.items()
            if state.applied.get(name, value) != value
        }
        usable = [
            item for item in allowed if all(state.applied.get(name, value) == value for name, value in item.items())
        ]
        if usable_item := next(
            (item for item in usable if all(options.get(name, value) == value for name, value in item.items())), None
        ):
            state.applied.update(usable_item)
            state.setters.update(dict.fromkeys(usable_item, issue.id))
            arguments = [
                argument for name, value in usable_item.items() for argument in option_arguments(name, value) or ()
            ]
            previous = {name: values[name] for name in usable_item}
            state.workarounds.append(AppliedWorkaround(issue, dict(usable_item), arguments, previous))
            values = values_of({**options, **state.applied})
            continue
        if not usable and (partners := (clashing | introduced) if avoid and issue.id not in excluded else set()):
            return {issue.id, *partners}
        if usable or (avoid and not issue.applies(counts, values_of(state.applied))):
            return IssueCheck(tuple(state.workarounds), issue, tuple(hits), values)
        hits.append(_hit(configuration, issue, {**options, **state.applied}, explicit, excluded))
    hits.extend(
        _hit(configuration, issue, {**options, **state.applied}, explicit, frozenset())
        for issue in data.issues
        if not issue.breaks and issue.when is not None and issue.effect != "names" and issue.applies(counts, values)
    )
    return IssueCheck(tuple(state.workarounds), None, tuple(hits), values)


def _hit(
    configuration: _Configuration,
    issue: KnownIssue,
    options: Mapping[str, OptionValue],
    explicit: AbstractSet[str],
    excluded: AbstractSet[str],
) -> Hit:
    """Return a hit with what would avoid the issue: a safe workaround, or dropping a value the user set."""
    issues = load_known_issues().by_id
    values = configuration.values_of(options)
    conflicts = tuple(
        issues[other]
        for other in sorted(excluded - {issue.id})
        if issue.id in excluded and issues[other].applies(configuration.counts, values)
    )
    if conflicts:
        return Hit(issue, conflicts=conflicts)
    safe, _ = configuration.avoiding(issue, values, options)
    if safe:
        return Hit(issue, avoided_by=dict(safe[0]))
    return Hit(issue, dropping=configuration.dropping(issue, options, explicit) if issue.breaks else ())


def names_issues(
    counts: Mapping[str, int], values: Mapping[str, Any], metrics: NameMetrics | None
) -> list[tuple[KnownIssue, int | None]]:
    """Return the names issues a configuration shows, with the symptom count the trial measured.

    An issue with a symptom shows only in a trial, when its class-name metric is not zero; one without a symptom shows
    when its condition matches.
    """
    shown: list[tuple[KnownIssue, int | None]] = []
    for issue in load_known_issues().issues:
        if issue.effect != "names" or not issue.applies(counts, values):
            continue
        if issue.symptom is None:
            shown.append((issue, None))
        elif metrics is not None and (count := getattr(metrics, issue.symptom)):
            shown.append((issue, count))
    return shown


def cli_negation_issue() -> KnownIssue | None:
    """Return the known issue that cites options without a --no-* form, while it is open."""
    return next((issue for issue in load_known_issues().issues if issue.effect == "cli-inexpressible"), None)
