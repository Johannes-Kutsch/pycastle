import pytest

from pycastle.services._docker_build_output import (
    FINAL_OUTCOME_EXAMPLES,
    interpret_final_build_outcome,
)


@pytest.mark.parametrize(
    "example",
    [
        pytest.param(example, id=example_name)
        for example_name, example in FINAL_OUTCOME_EXAMPLES.items()
    ],
)
def test_final_outcome_examples_return_expected_build_outcome_when_streamed(
    example,
):
    result = interpret_final_build_outcome(line for line in example.lines)

    assert result == example.outcome


@pytest.mark.parametrize(
    "example",
    [
        pytest.param(example, id=example_name)
        for example_name, example in FINAL_OUTCOME_EXAMPLES.items()
    ],
)
def test_final_outcome_examples_preserve_string_input_behavior(example):
    result = interpret_final_build_outcome("".join(example.lines))

    assert result == example.outcome
