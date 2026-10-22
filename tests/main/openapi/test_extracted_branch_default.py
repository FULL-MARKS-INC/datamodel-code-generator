"""A model extracted from one branch of a field's union keeps the field's default off its root."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

import msgspec
import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaValidationError
from pydantic import TypeAdapter, ValidationError

from tests.conftest import assert_output
from tests.main.conftest import DATA_PATH, OPEN_API_DATA_PATH, _generated_model, run_main_and_assert

if TYPE_CHECKING:
    from pathlib import Path

INPUT = OPEN_API_DATA_PATH / "extracted_branch_default.yaml"
PAYLOADS = DATA_PATH / "payloads" / "extracted_branch_default" / "model.json"
OUTPUT_MODEL_TYPES = (
    "pydantic_v2.BaseModel",
    "pydantic_v2.dataclass",
    "dataclasses.dataclass",
    pytest.param(
        "typing.TypedDict",
        marks=pytest.mark.skipif(
            sys.version_info < (3, 12), reason="Pydantic validates typing.TypedDict from Python 3.12 on"
        ),
    ),
    "msgspec.Struct",
)
OPTION_SETS = {
    "default": (),
    "field_constraints": ("--field-constraints",),
    "annotated": ("--use-annotated",),
    "strict_nullable": ("--strict-nullable", "--field-constraints"),
}


def _verdict(model: Any, output_model_type: str, document: object) -> str:
    """Return whether a generated model accepts a JSON document, with the first error type where Pydantic rejects it."""
    text = json.dumps(document)
    try:
        if output_model_type == "msgspec.Struct":
            msgspec.json.decode(text, type=model)
        else:
            TypeAdapter(model).validate_json(text)
    except ValidationError as error:
        return f"rejected ({error.errors()[0]['type']})"
    except msgspec.ValidationError:
        return "rejected"
    return "accepted"


def _root_model_verdict(output_file: Path) -> str:
    """Return the error type of building the extracted ``Weight`` model without its value, which must be rejected."""
    with (
        _generated_model(output_file, "extracted_branch_default_weight", "Weight") as weight,
        pytest.raises(ValidationError) as error,
    ):
        weight()
    return f"rejected ({error.value.errors()[0]['type']})"


@pytest.mark.parametrize("output_model_type", OUTPUT_MODEL_TYPES)
@pytest.mark.parametrize("option_set", OPTION_SETS)
def test_extracted_branch_default(output_file: Path, output_model_type: str, option_set: str) -> None:
    """Leave the default with the field that declares it and keep the extracted branch's value required."""
    run_main_and_assert(
        input_path=INPUT,
        output_path=output_file,
        input_file_type="openapi",
        extra_args=["--formatters", "builtin", "--output-model-type", output_model_type, *OPTION_SETS[option_set]],
    )
    payloads = json.loads(PAYLOADS.read_text())
    validator = Draft202012Validator(yaml.safe_load(INPUT.read_text())["components"]["schemas"]["Model"])
    validator.validate(payloads["valid"])
    for change in payloads["invalid"]:
        with pytest.raises(SchemaValidationError):
            validator.validate({**payloads["valid"], **change})
    with _generated_model(output_file, "extracted_branch_default_model", "Model") as model:
        verdicts = [
            f"empty: {_verdict(model, output_model_type, {})}",
            f"valid: {_verdict(model, output_model_type, payloads['valid'])}",
        ]
        verdicts.extend(
            f"invalid {', '.join(change)}: {_verdict(model, output_model_type, {**payloads['valid'], **change})}"
            for change in payloads["invalid"]
        )
    if output_model_type == "pydantic_v2.BaseModel" and option_set != "default":
        verdicts.append(f"Weight(): {_root_model_verdict(output_file)}")
    assert_output("".join(f"{verdict}\n" for verdict in verdicts), name="payloads")
