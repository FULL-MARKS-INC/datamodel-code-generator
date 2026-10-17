"""Known generation issues that option recommendations cite, and the config values that avoid them.

The entries ship as data (``resources/recommender_known_issues.json``). Each one names its GitHub issue, what the
defect does to the generated code, when a configuration hits it (schema signals and config values), the config values
that avoid it, and the repro that the retirement guard generates to check that the issue still occurs. An issue that
breaks the output (it fails at import, or generation stops) is avoided: the configuration takes the workaround, or a
configuration whose options cause it is rejected. Every other issue is cited.
"""

from __future__ import annotations

from dataclasses import dataclass
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
SymptomMetric: TypeAlias = Literal["numbered", "doubled", "displaced", "union_variants_numbered"]
BREAKING_EFFECTS: frozenset[str] = frozenset({"import-error", "generation-error"})


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Workaround(_Data):
    """Config values that avoid an issue, when their condition matches the configuration."""

    when: Condition | None = None
    set: dict[str, Any] = Field(min_length=1)


class Repro(_Data):
    """A generation that shows an issue, which the retirement guard runs.

    ``input`` is a file under tests/data/recommend_options/known_issues, generated with ``args`` and ``--output`` set
    to ``output``. While the issue occurs, ``file`` (a path in the run directory, or ``stderr``) contains ``shows``,
    or matches it as a regular expression when ``regex`` is set.
    """

    input: str
    args: tuple[str, ...]
    output: str | None = "model.py"
    file: str = "model.py"
    shows: str
    regex: bool = False


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
    """Known issues with their workarounds as pyproject.toml spells them, and the config fields they test."""

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
    fields = frozenset(test.config for issue in data.issues for test in config_tests(issue))
    return LoadedIssues(data.issues, {issue.id: issue for issue in data.issues}, workarounds, fields)


def issue_values(config: Any) -> dict[str, Any]:
    """Return the config values the known issues test, as pyproject.toml spells them."""
    return {name: toml_value(getattr(config, name)) for name in load_known_issues().fields}


@dataclass(frozen=True, slots=True)
class AppliedWorkaround:
    """The config values a configuration takes to avoid a known issue, and their command-line arguments."""

    issue: KnownIssue
    values: Mapping[str, OptionValue]
    arguments: list[str]


@dataclass(frozen=True, slots=True)
class Hit:
    """A known issue a configuration has, and the config values that would avoid it, if any."""

    issue: KnownIssue
    avoided_by: Mapping[str, OptionValue]


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
    def stops_generation(self) -> KnownIssue | None:
        """Return an issue that stops generation of this configuration, so it need not be generated."""
        issues = (self.blocking, *(hit.issue for hit in self.hits))
        return next((issue for issue in issues if issue and issue.effect == "generation-error"), None)


@dataclass(frozen=True, slots=True)
class _Configuration:
    """Signal counts and the config values of a base configuration with option values on top."""

    counts: Mapping[str, int]
    values_of: Callable[[Mapping[str, OptionValue]], Mapping[str, Any]]

    def avoiding(
        self, issue: KnownIssue, values: Mapping[str, Any], options: Mapping[str, OptionValue]
    ) -> list[Mapping[str, OptionValue]]:
        """Return the workarounds of an issue that apply to a configuration and avoid the issue on top of options."""
        return [
            workaround
            for condition, workaround in load_known_issues().workarounds[issue.id]
            if (condition is None or matches(condition, self.counts, values))
            and not issue.applies(self.counts, self.values_of({**options, **workaround}))
        ]


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
    breaks the output takes the first workaround that avoids it and keeps the values the user set and earlier
    workarounds set. When only workarounds that change one of the options would avoid it, or nothing avoids it and
    only the options cause it, the issue blocks the configuration. Any other issue that breaks the output is a hit,
    like every other issue that matches, except names issues; a hit names the workaround that would avoid it.
    """
    configuration = _Configuration(counts, values_of)
    issues = load_known_issues().issues
    applied: dict[str, OptionValue] = {}
    workarounds: list[AppliedWorkaround] = []
    hits: list[Hit] = []
    handled: set[str] = set()
    values = values_of(options)
    while (
        issue := next(
            (item for item in issues if item.breaks and item.id not in handled and item.applies(counts, values)), None
        )
    ) is not None:
        handled.add(issue.id)
        avoiding = configuration.avoiding(issue, values, {**options, **applied})
        allowed = (
            [workaround for workaround in avoiding if not workaround.keys() & {*explicit, *applied}] if avoid else []
        )
        if (
            usable := next(
                (item for item in allowed if all(options.get(name, value) == value for name, value in item.items())),
                None,
            )
        ) is not None:
            applied.update(usable)
            arguments = [argument for name, value in usable.items() for argument in option_arguments(name, value) or ()]
            workarounds.append(AppliedWorkaround(issue, dict(usable), arguments))
            values = values_of({**options, **applied})
            continue
        if allowed or (avoid and not issue.applies(counts, values_of(applied))):
            return IssueCheck(tuple(workarounds), issue, tuple(hits), values)
        hits.append(Hit(issue, dict(avoiding[0]) if avoiding else {}))
    for issue in issues:
        if not issue.breaks and issue.when is not None and issue.effect != "names" and issue.applies(counts, values):
            avoiding = configuration.avoiding(issue, values, {**options, **applied})
            hits.append(Hit(issue, dict(avoiding[0]) if avoiding else {}))
    return IssueCheck(tuple(workarounds), None, tuple(hits), values)


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
