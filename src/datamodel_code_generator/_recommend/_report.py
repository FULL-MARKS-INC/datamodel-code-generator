"""Text and JSON output of option recommendations."""

from __future__ import annotations

import itertools
import json
import textwrap
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from datamodel_code_generator._recommend._rules import format_text, option_arguments
from datamodel_code_generator._recommend._signals import Signals

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from collections.abc import Set as AbstractSet

    from datamodel_code_generator._recommend import Citation, Disagreement, Option, Recommendation, Trial
    from datamodel_code_generator._recommend._known_issues import AppliedWorkaround, KnownIssue
    from datamodel_code_generator._recommend._metrics import NameChanges, NameMetrics, NameStability
    from datamodel_code_generator._recommend._trial import CandidateOutcome
    from datamodel_code_generator._structured_output import (
        RecommendCandidatePayload,
        RecommendCitationPayload,
        RecommendDisagreementPayload,
        RecommendKnownIssuePayload,
        RecommendNameChangesPayload,
        RecommendOptionPayload,
        RecommendTrialPayload,
        RecommendWorkaroundPayload,
    )

_WIDTH = 100
_DETAIL_INDENT = " " * 6
_EXAMPLE_INDENT = " " * 8
_TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("classes", "types"),
    ("problems", "problems"),
    ("numbered", "numbered"),
    ("long", "long"),
    ("doubled", "doubled"),
    ("displaced", "displaced"),
    ("median", "median_length"),
)
_SIGNAL_LINES: tuple[tuple[str, str], ...] = (
    ("components", "{components}, {operations} operations"),
    ("inline objects / enums", "{inline_objects} / {inline_enums}, nesting depth {max_inline_depth}"),
    ("shared property names", "{shared_inline_names} ({shared_inline_name_examples})"),
    ("inline names = component names", "{inline_names_equal_component}"),
    (
        "repeated inline objects / enums",
        (
            "{duplicate_inline_objects} of {duplicate_inline_shapes} shapes / "
            "{duplicate_inline_enums} of {duplicate_inline_enum_shapes} value sets"
        ),
    ),
    (
        "titled components",
        (
            "{titled_components}: {component_title_duplicate_groups} shared titles ({duplicate_title_examples}), "
            "{component_titles_equal_other_key} equal another key, {component_titles_equal_own_key} equal their own key"
        ),
    ),
    (
        "inline titles",
        "{inline_titled_objects} objects, most repeated {inline_title_max_repeat} ({inline_title_examples})",
    ),
    (
        "unions",
        "{unions} oneOf/anyOf, {discriminated_unions} with a discriminator, {inline_union_variants} inline variants",
    ),
    (
        "union branch tags",
        (
            "{inline_tagged_union_branches} tagged, {single_key_required_open} single required key, "
            "{tagged_behind_ref} tagged only through allOf $ref, {parent_copy_union_branches} copy their parent"
        ),
    ),
    ("component keys", "{key_style}; {dotted_keys} dotted in {dotted_key_namespaces} namespaces"),
)
_ROOT_LINE = ("root schema", "object model {root_is_object}, title words {root_title_words}")
_NO_SIGNALS = Signals(counts={}, facts={})
_EFFECTS: dict[str, str] = {
    "import-error": "fails at import",
    "generation-error": "stops generation",
    "wrong-type": "wrong type",
    "lost-constraint": "drops a constraint",
    "rejects-valid": "rejects valid data",
    "not-enforced": "does not check a constraint",
    "names": "class names",
    "cli-inexpressible": "command line",
}
_ATTRIBUTED_SYMPTOMS: dict[str, str] = {
    "doubled_union_variants": "doubled union variant name",
    "doubled_variant_children": "doubled variant property name",
}


def _wrap(text: str, indent: str = _DETAIL_INDENT) -> list[str]:
    return textwrap.wrap(text, width=_WIDTH, initial_indent=indent, subsequent_indent=indent, break_on_hyphens=False)


def _value_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _count(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 'es' if noun.endswith('s') else 's'}"


def _changes_text(changes: NameChanges) -> str:
    return (
        f"{changes.renamed} of {_count(changes.matched, 'class')} matched by schema location renamed "
        f"({changes.renumbered} only renumbered), {changes.removed} removed, {changes.added} added"
        f"{', e.g.' if changes.examples else ''}"
    )


def _example_lines(changes: NameChanges, indent: str = _EXAMPLE_INDENT) -> list[str]:
    return [f"{indent}{change.before} -> {change.after}   at {change.location}" for change in changes.examples]


def _effect_lines(option: Option) -> list[str]:
    if (effect := option.effect) is None:
        return []
    without, with_ = effect.before_metrics, effect.after_metrics
    text = (
        f"trial: {effect.before} -> {effect.after}: classes {without.types} -> {with_.types}, "
        f"problem names {without.problems} -> {with_.problems}; {_changes_text(effect.changes)}"
    )
    return [*_wrap(text), *_example_lines(effect.changes)]


def _issue_message(issue: KnownIssue, signals: Signals) -> str:
    return format_text(issue.message, signals)


def _setting_arguments(values: Mapping[str, Any]) -> str:
    """Return the CLI arguments of config values, or their pyproject.toml keys when no option expresses one."""
    arguments = list(itertools.starmap(option_arguments, values.items()))
    if all(argument is not None for argument in arguments):
        return " ".join(argument for parts in arguments for argument in parts or ())
    return ", ".join(
        f"{name.replace('_', '-')} = {_value_text(value)} in pyproject.toml" for name, value in values.items()
    )


def _option_lines(option: Option, signals: Signals, *, against: bool = False) -> list[str]:
    rule = option.decision.rule
    lines = [f"  {' '.join(option.arguments)}   [{rule.id}]", *_wrap(format_text(rule.reason, signals))]
    if against:
        lines.extend(_wrap(f"against: {format_text(rule.contested_reason, signals)}"))
    lines.extend(
        line
        for issue in option.caveats
        for line in _wrap(f"known issue #{issue.issue}: {_issue_message(issue, signals)}")
    )
    lines.extend(line for evidence in rule.evidence for line in _wrap(f"measured: {evidence}"))
    lines.extend(_effect_lines(option))
    return lines


def _metric_cells(metrics: NameMetrics, stability: NameStability | None) -> list[str]:
    cells = [
        f"{metrics.problems} ({metrics.problems_percent}%)" if name == "problems" else str(getattr(metrics, name))
        for _, name in _TABLE_COLUMNS
    ]
    return [*cells, *(() if stability is None else (str(stability.locations.renamed),))]


def _table_row(cells: Sequence[str], widths: Sequence[int]) -> str:
    return "  " + "  ".join(
        cell.ljust(width) if index == 0 else cell.rjust(width)
        for index, (cell, width) in enumerate(zip(cells, widths, strict=False))
    )


def _trial_footnotes(trial: Trial, *, stability: bool) -> list[str]:
    notes: list[str] = []
    if not trial.outcomes[0].candidate.selectable:
        notes.append(
            f"{trial.outcomes[0].candidate.label}: the configuration as set, without the preset; never selected"
        )
    if trial.remeasured:
        notes.append(
            f"the last {_count(trial.remeasured, 'row')}: the selected configuration without or with one option, "
            "to measure each option on it; never selected"
        )
    if stability:
        notes.append("renamed: classes a candidate renames between --diff-against and the input")
    notes.extend(
        f"{outcome.candidate.label}: with the workarounds of "
        f"{', '.join(f'#{issue}' for issue in outcome.candidate.workarounds)}"
        for outcome in trial.outcomes
        if outcome.candidate.workarounds
    )
    return [line for note in notes for line in _wrap(note, "  ")]


def _trial_lines(trial: Trial) -> list[str]:
    """Render the candidates' table and the changes from the current configuration."""
    stability = any(outcome.stability is not None for outcome in trial.outcomes)
    headers = ["candidate", *(header for header, _ in _TABLE_COLUMNS), *(("renamed",) if stability else ())]
    rows = [
        [
            outcome.candidate.label,
            *(() if outcome.metrics is None else _metric_cells(outcome.metrics, outcome.stability)),
        ]
        for outcome in trial.outcomes
    ]
    widths = [max(len(row[index]) for row in (headers, *rows) if index < len(row)) for index in range(len(headers))]
    lines = [
        "",
        f"Trial  {_count(len(trial.outcomes), 'candidate')} generated in memory without formatting; nothing written",
        _table_row(headers, widths),
    ]
    for outcome, row in zip(trial.outcomes, rows, strict=True):
        if outcome.error is not None:
            reason = f" ({outcome.error})" if outcome.candidate.rejection is not None else f": {outcome.error}"
            lines.append(f"{_table_row(row, widths)}  rejected{reason}")
        else:
            lines.append(_table_row(row, widths) + ("   selected" if outcome is trial.selected else ""))
    lines.extend(_trial_footnotes(trial, stability=stability))
    if trial.migration is not None and trial.selected is not None:
        labels = f"{trial.current.candidate.label} -> {trial.selected.candidate.label}"
        lines.extend((
            "",
            f"Changes from the current configuration ({labels})",
            *_wrap(_changes_text(trial.migration), "  "),
            *_example_lines(trial.migration, "    "),
        ))
    return lines


def _signal_lines(signals: Signals, input_file_type: str | None) -> list[str]:
    values = {**signals.counts, **{name: value or "none" for name, value in signals.facts.items()}}
    rows = [*_SIGNAL_LINES, _ROOT_LINE] if input_file_type == "jsonschema" else list(_SIGNAL_LINES)
    width = max(len(label) for label, _ in rows)
    scopes = f" (OpenAPI scopes: {signals.facts['scopes']})" if signals.facts["scopes"] else ""
    return [f"Schema  {signals.facts['schema']}{scopes}"] + [
        f"  {label.ljust(width)}  {template.format_map(values)}" for label, template in rows
    ]


def _flag(name: str) -> str:
    return f"--{name.replace('_', '-')}"


def _disagreement_text(disagreement: Disagreement, preset: str) -> str:
    from datamodel_code_generator._recommend._known_issues import cli_negation_issue  # noqa: PLC0415

    flag = _flag(disagreement.field)
    text = (
        f"{flag} is set to {_value_text(disagreement.configured)}; the rules suggest "
        f"{_value_text(disagreement.recommended)}"
    )
    match disagreement.kind:
        case "newer-preset":
            text = f"--preset {disagreement.configured}: a newer preset is available, {disagreement.recommended}"
        case "preset":
            citation = "" if (issue := cli_negation_issue()) is None else f" (#{issue.issue})"
            text = (
                f"{preset} sets {flag} to {_value_text(disagreement.configured)}; no option sets it to "
                f"{_value_text(disagreement.recommended)}{citation}"
            )
    return text if disagreement.decision is None else f"{text}   [{disagreement.decision.rule.id}]"


def _start_lines(recommendation: Recommendation, signals: Signals) -> list[str]:
    """Render the preset to start from and the options to add on top of it."""
    added = sum(len(option.values) for option in recommendation.recommendations)
    return [
        "",
        f"Start from  --preset {recommendation.preset}",
        *(
            f"  already sets {' '.join(option.arguments)}   [{option.decision.rule.id}]"
            for option in recommendation.covers
        ),
        "",
        f"Add ({added} option{'' if added == 1 else 's'})" if added else "Add  nothing on top of the preset",
        *(line for option in recommendation.recommendations for line in _option_lines(option, signals)),
    ]


def _workaround_lines(workarounds: Sequence[AppliedWorkaround], signals: Signals) -> list[str]:
    if not workarounds:
        return []
    added = sum(len(workaround.values) for workaround in workarounds)
    lines = ["", f"Work around known issues ({_count(added, 'option')})"]
    for workaround in workarounds:
        issue = workaround.issue
        changes = ", ".join(
            f"{name.replace('_', '-')} from {_setting_text(workaround.previous[name])} to {_value_text(value)}"
            for name, value in workaround.values.items()
        )
        lines.append(f"  {' '.join(workaround.arguments)}   [#{issue.issue} {issue.id}]")
        lines.extend(_wrap(f"changes {changes}: {_issue_message(issue, signals)}"))
    return lines


def _setting_text(value: Any) -> str:
    return "unset" if value is None else _value_text(value)


def _citation_text(citation: Citation, signals: Signals, worked_around: AbstractSet[str]) -> str:
    """Return a citation: the issue, its effect, and the message, or the options left out for a worked-around one.

    A trial count is shown only for a class-name metric that counts the names of that one issue.
    """
    issue = citation.issue
    effect = _EFFECTS[issue.effect]
    if citation.count is not None and (noun := _ATTRIBUTED_SYMPTOMS.get(cast("str", issue.symptom))):
        effect = f"{effect}; the selected candidate has {_count(citation.count, noun)}"
    head = f"#{issue.issue} {issue.id} ({effect})"
    if citation.dropped:
        left_out = ", ".join(" ".join(option.arguments) for option in citation.dropped)
        if issue.id in worked_around:
            return f"{head}: left out {left_out}, which keeps the workaround above from applying."
        return f"{head}: left out {left_out}. {_issue_message(issue, signals)}"
    text = f"{head}: {_issue_message(issue, signals)}"
    if citation.conflicts:
        others = ", ".join(f"#{other.issue}" for other in citation.conflicts)
        return f"{text} No single configuration avoids it together with {others}, so the command keeps it."
    if citation.avoided_by:
        return f"{text} {_setting_arguments(citation.avoided_by)} avoids it."
    if citation.dropping:
        flags = ", ".join(_flag(name) for name in citation.dropping)
        return f"{text} Removing {flags} from your configuration avoids it."
    return f"{text} No option avoids it." if issue.breaks else text


def _citation_lines(recommendation: Recommendation, signals: Signals) -> list[str]:
    if not recommendation.known_issues:
        return []
    worked_around = {workaround.issue.id for workaround in recommendation.workarounds}
    return [
        "",
        "Known issues",
        *(
            line
            for citation in recommendation.known_issues
            for line in textwrap.wrap(
                _citation_text(citation, signals, worked_around),
                width=_WIDTH,
                initial_indent="  ",
                subsequent_indent="      ",
                break_on_hyphens=False,
            )
        ),
    ]


def render_text(recommendation: Recommendation, *, command: str | None, pyproject: str | None) -> str:
    """Render a recommendation as the text report; with nothing to run, it shows the schema and the notes only."""
    signals = recommendation.signals or _NO_SIGNALS
    lines = [
        f"Option recommendations (experimental; {recommendation.mode} mode; rules {recommendation.rules_revision})",
        "",
    ]
    if recommendation.signals is None:
        lines.append(f"Schema  not analysed ({recommendation.input_file_type or recommendation.source} input)")
    else:
        lines.extend(_signal_lines(signals, recommendation.input_file_type))
    if recommendation.runnable:
        lines.extend(_start_lines(recommendation, signals))
    for title, options, against in (
        ("Left out after the trial (the rules recommend these)", recommendation.left_out, False),
        ("Consider (the schema also has signals against these)", recommendation.consider, True),
        ("Not recommended", recommendation.not_recommended, False),
    ):
        if options:
            lines.extend(("", title))
            lines.extend(line for option in options for line in _option_lines(option, signals, against=against))
    lines.extend(_workaround_lines(recommendation.workarounds, signals))
    if recommendation.trial is not None:
        lines.extend(_trial_lines(recommendation.trial))
    lines.extend(_citation_lines(recommendation, signals))
    if recommendation.disagreements:
        lines.extend(("", "Your configuration"))
        lines.extend(f"  {_disagreement_text(item, recommendation.preset)}" for item in recommendation.disagreements)
    if recommendation.notes:
        lines.extend(("", "Notes"))
        for note in recommendation.notes:
            lines.extend(
                textwrap.wrap(
                    note, width=_WIDTH, initial_indent="  - ", subsequent_indent="    ", break_on_hyphens=False
                )
            )
    if command is not None:
        lines.extend(("", "Command", f"  {command}"))
    if pyproject is not None:
        complete = recommendation.config_complete
        title = "pyproject.toml" if complete else "pyproject.toml: keys to add or change"
        body = pyproject.splitlines() if complete or recommendation.config_values else ["nothing to change"]
        lines.extend(("", title, *(f"  {line}" for line in body)))
    return "\n".join(lines) + "\n"


def _changes_payload(changes: NameChanges) -> RecommendNameChangesPayload:
    from datamodel_code_generator._structured_output import RecommendNameChangesPayload  # noqa: PLC0415

    return RecommendNameChangesPayload.model_validate(asdict(changes))


def _candidate_payload(outcome: CandidateOutcome) -> RecommendCandidatePayload:
    from datamodel_code_generator._structured_output import (  # noqa: PLC0415
        RecommendCandidatePayload,
        RecommendNameMetricsPayload,
        RecommendNameStabilityPayload,
    )

    candidate, metrics, stability = outcome.candidate, outcome.metrics, outcome.stability
    return RecommendCandidatePayload(
        label=candidate.label,
        preset=candidate.preset,
        set=dict(candidate.options),
        arguments=[
            argument for name, value in candidate.options.items() for argument in option_arguments(name, value) or ()
        ],
        selectable=candidate.selectable,
        workarounds=list(candidate.workarounds),
        error=outcome.error,
        metrics=None
        if metrics is None
        else RecommendNameMetricsPayload(**asdict(metrics), problems_percent=metrics.problems_percent),
        stability=None if stability is None else RecommendNameStabilityPayload.model_validate(asdict(stability)),
    )


def _trial_payload(trial: Trial | None) -> RecommendTrialPayload | None:
    from datamodel_code_generator._recommend._metrics import METRICS_VERSION  # noqa: PLC0415
    from datamodel_code_generator._structured_output import RecommendTrialPayload  # noqa: PLC0415

    if trial is None:
        return None
    return RecommendTrialPayload(
        metrics_version=METRICS_VERSION,
        candidates=[_candidate_payload(outcome) for outcome in trial.outcomes],
        ranking=[outcome.candidate.label for outcome in trial.ranking],
        selected=None if trial.selected is None else trial.selected.candidate.label,
        current=trial.current.candidate.label,
        migration=None if trial.migration is None else _changes_payload(trial.migration),
        dropped=trial.dropped,
        remeasured=trial.remeasured,
    )


def _option_payload(option: Option, signals: Signals) -> RecommendOptionPayload:
    from datamodel_code_generator._structured_output import (  # noqa: PLC0415
        RecommendOptionPayload,
        RecommendOptionTrialPayload,
    )

    rule = option.decision.rule
    effect = option.effect
    return RecommendOptionPayload(
        rule=rule.id,
        decision=rule.decision,
        set=dict(option.values),
        arguments=option.arguments,
        reason=format_text(rule.reason, signals),
        contested_reason=format_text(rule.contested_reason, signals) if option.decision.contested else None,
        known_issues=[_known_issue_payload(issue, signals) for issue in option.caveats],
        evidence=list(rule.evidence),
        trial=None
        if effect is None
        else RecommendOptionTrialPayload(
            before=effect.before,
            after=effect.after,
            changes=_changes_payload(effect.changes),
        ),
    )


def _known_issue_payload(issue: KnownIssue, signals: Signals) -> RecommendKnownIssuePayload:
    from datamodel_code_generator._structured_output import RecommendKnownIssuePayload  # noqa: PLC0415

    return RecommendKnownIssuePayload(
        issue=issue.issue, id=issue.id, effect=issue.effect, message=_issue_message(issue, signals)
    )


def _workaround_payload(workaround: AppliedWorkaround, signals: Signals) -> RecommendWorkaroundPayload:
    from datamodel_code_generator._structured_output import RecommendWorkaroundPayload  # noqa: PLC0415

    return RecommendWorkaroundPayload(
        known_issue=_known_issue_payload(workaround.issue, signals),
        set=dict(workaround.values),
        previous=dict(workaround.previous),
        arguments=workaround.arguments,
    )


def _citation_payload(citation: Citation, signals: Signals) -> RecommendCitationPayload:
    from datamodel_code_generator._structured_output import RecommendCitationPayload  # noqa: PLC0415

    return RecommendCitationPayload(
        known_issue=_known_issue_payload(citation.issue, signals),
        avoided_by=dict(citation.avoided_by),
        count=citation.count,
        dropped=[option.decision.rule.id for option in citation.dropped],
        dropping=list(citation.dropping),
        conflicts=[other.issue for other in citation.conflicts],
    )


def _disagreement_payload(disagreement: Disagreement) -> RecommendDisagreementPayload:
    from datamodel_code_generator._structured_output import RecommendDisagreementPayload  # noqa: PLC0415

    return RecommendDisagreementPayload(
        field=disagreement.field,
        kind=disagreement.kind,
        configured=disagreement.configured,
        recommended=disagreement.recommended,
        rule=None if disagreement.decision is None else disagreement.decision.rule.id,
    )


def _nested(table: tuple[str, ...], values: dict[str, Any]) -> dict[str, Any]:
    nested: dict[str, Any] = values
    for key in reversed(table):
        nested = {key: nested}
    return nested


def render_json(recommendation: Recommendation, *, content: str, arguments: list[str]) -> str:
    """Render a recommendation as the recommend-options JSON payload."""
    from datamodel_code_generator._structured_output import (  # noqa: PLC0415
        RecommendBaselinePayload,
        RecommendContextPayload,
        RecommendInputPayload,
        RecommendOptionsPayload,
    )

    signals = recommendation.signals or _NO_SIGNALS
    config = recommendation.config_values
    payload = RecommendOptionsPayload(
        version=1,
        format="json",
        kind="recommend-options",
        mode=recommendation.mode,
        rules_revision=recommendation.rules_revision,
        calibrated_presets=recommendation.calibrated_presets,
        input=RecommendInputPayload(
            source=recommendation.source,
            input_file_type=recommendation.input_file_type,
            document=signals.facts.get("schema"),
            analysed=recommendation.signals is not None,
        ),
        context=RecommendContextPayload(
            output_model_type=recommendation.output_model_type,
            target_python_version=recommendation.target_python_version,
        ),
        signals=dict(signals.counts),
        facts=dict(signals.facts),
        baseline=RecommendBaselinePayload(
            preset=recommendation.preset,
            family=recommendation.family,
            configured=recommendation.preset_configured,
            covers=[_option_payload(option, signals) for option in recommendation.covers],
        ),
        recommendations=[_option_payload(option, signals) for option in recommendation.recommendations],
        left_out=[_option_payload(option, signals) for option in recommendation.left_out],
        consider=[_option_payload(option, signals) for option in recommendation.consider],
        not_recommended=[_option_payload(option, signals) for option in recommendation.not_recommended],
        disagreements=[_disagreement_payload(item) for item in recommendation.disagreements],
        trial=_trial_payload(recommendation.trial),
        workarounds=[_workaround_payload(workaround, signals) for workaround in recommendation.workarounds],
        known_issues=[_citation_payload(citation, signals) for citation in recommendation.known_issues],
        notes=recommendation.notes,
        arguments=arguments,
        config=None
        if config is None
        else _nested(recommendation.table, {name.replace("_", "-"): value for name, value in sorted(config.items())}),
        config_complete=recommendation.config_complete,
        content=content,
    )
    return json.dumps(payload.model_dump(mode="json"), indent=2, ensure_ascii=False)
