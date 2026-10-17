"""Trial naming: generate candidate configurations up to their model metadata and compare the class names.

A candidate is a complete generation config. It runs generate()'s own pipeline without formatting or output, so its
class names are the names a user gets. Candidates are ranked like the tuned naming benchmark: rejected candidates
last, then fewer problem names, fewer renamed locations against a previous version of the input, a shorter median
name, fewer added options, and the option spelling.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from datamodel_code_generator import Error, _collect_model_metadata
from datamodel_code_generator._recommend._metrics import name_metrics, name_records, name_stability

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path
    from urllib.parse import ParseResult

    from datamodel_code_generator._recommend._metrics import NameMetrics, NameRecord, NameStability, SchemaVocabulary
    from datamodel_code_generator._recommend._rules import OptionValue
    from datamodel_code_generator._shared_types import DefaultPutDict
    from datamodel_code_generator.enums import InputFileType

    TrialInput = Path | str | ParseResult | Mapping[str, Any]

__all__ = ["FULL_TRIAL_MAX_MODELS", "CandidateOutcome", "TrialCandidate", "full_trial_by_default", "run_trial"]

FULL_TRIAL_MAX_MODELS: Final = 3000


def full_trial_by_default(*estimated_models: int) -> bool:
    """Return whether a recommendation without a mode compares candidates, from the inputs' estimated model counts.

    Each candidate is a full generation of every input, so the estimate of a previous input counts too. The limit is
    a model count, never a time, so the decision does not depend on the machine.
    """
    return sum(estimated_models) <= FULL_TRIAL_MAX_MODELS


@dataclass(frozen=True, slots=True)
class TrialCandidate:
    """A labelled generation config, with the options it adds on top of its preset as ``pyproject.toml`` spells them.

    ``preset`` is None for the configuration as set without a preset. A candidate that is not ``selectable`` is
    measured and ranked but never chosen.
    """

    label: str
    config: Any
    preset: str | None
    options: Mapping[str, OptionValue]
    selectable: bool = True


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    """The class names one candidate generates, or the error that rejects it."""

    candidate: TrialCandidate
    records: tuple[NameRecord, ...]
    metrics: NameMetrics | None
    stability: NameStability | None
    error: str | None

    @property
    def score(self) -> tuple[int, int, int, int, int, str]:
        """Return the ranking key; lower is better."""
        if self.metrics is None:
            return 1, 0, 0, 0, 0, ""
        options = self.candidate.options
        return (
            0,
            self.metrics.problems,
            0 if self.stability is None else self.stability.locations.renamed,
            self.metrics.median_length,
            len(options),
            " ".join(f"{name}={value}" for name, value in sorted(options.items())),
        )


def _run_candidate(  # noqa: PLR0913
    input_: TrialInput,
    candidate: TrialCandidate,
    config: Any,
    *,
    vocabulary: SchemaVocabulary,
    previous_input: Path | None,
    remote_text_cache: DefaultPutDict[str, str],
) -> tuple[CandidateOutcome, InputFileType | None]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            metadata, input_file_type = _collect_model_metadata(input_, config, remote_text_cache=remote_text_cache)
            records = name_records(metadata)
            previous = (
                None
                if previous_input is None
                else name_records(
                    _collect_model_metadata(
                        previous_input,
                        config.model_copy(update={"input_file_type": input_file_type}),
                        remote_text_cache=remote_text_cache,
                    )[0]
                )
            )
    except Error as error:
        return CandidateOutcome(candidate, (), None, None, str(error)), None
    outcome = CandidateOutcome(
        candidate,
        records,
        name_metrics(records, vocabulary),
        None if previous is None else name_stability(previous, records),
        None,
    )
    return outcome, input_file_type


def run_trial(
    input_: TrialInput,
    candidates: Sequence[TrialCandidate],
    *,
    vocabulary: SchemaVocabulary,
    previous_input: Path | None,
    remote_text_cache: DefaultPutDict[str, str],
) -> tuple[CandidateOutcome, ...]:
    """Generate every candidate's class names for the input, in the given order.

    ``vocabulary`` comes from the input's loaded document. With ``previous_input``, every candidate also runs on that
    earlier version of the input and records the classes it renames between them. An input type left to detection
    is detected once, by the first generation that succeeds, like one generation run. Warnings of the candidate runs
    are not shown, and a candidate whose generation raises an error is rejected with its message; a remote lock error
    concerns the input, not a candidate, and propagates. Runs that share ``remote_text_cache`` fetch each remote
    document once.
    """
    outcomes: list[CandidateOutcome] = []
    detected: InputFileType | None = None
    for candidate in candidates:
        config = (
            candidate.config if detected is None else candidate.config.model_copy(update={"input_file_type": detected})
        )
        outcome, input_file_type = _run_candidate(
            input_,
            candidate,
            config,
            vocabulary=vocabulary,
            previous_input=previous_input,
            remote_text_cache=remote_text_cache,
        )
        outcomes.append(outcome)
        detected = detected or input_file_type
    return tuple(outcomes)
