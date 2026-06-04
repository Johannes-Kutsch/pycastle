from pycastle.services._docker_build_output import (
    BuildOutcome,
    interpret_final_build_outcome,
)


def test_interpreter_buildkit_all_cached_lines_return_full_cache_hit():
    buildkit_all_cached = [
        "#1 [1/2] FROM python:3.12\n",
        "#1 CACHED\n",
        "#2 [2/2] RUN pip install requests\n",
        "#2 CACHED\n",
    ]

    result = interpret_final_build_outcome(buildkit_all_cached)

    assert result == BuildOutcome.FULL_CACHE_HIT


def test_interpreter_buildkit_rebuilt_example_returns_rebuilt():
    from pycastle.services._docker_build_output import FINAL_OUTCOME_EXAMPLES

    example = FINAL_OUTCOME_EXAMPLES["buildkit_rebuilt"]

    result = interpret_final_build_outcome(example.lines)

    assert result == BuildOutcome.REBUILT


def test_interpreter_classic_all_cached_example_returns_full_cache_hit():
    from pycastle.services._docker_build_output import FINAL_OUTCOME_EXAMPLES

    example = FINAL_OUTCOME_EXAMPLES["classic_all_cached"]

    result = interpret_final_build_outcome(example.lines)

    assert result == BuildOutcome.FULL_CACHE_HIT


def test_interpreter_classic_mixed_example_returns_rebuilt():
    from pycastle.services._docker_build_output import FINAL_OUTCOME_EXAMPLES

    example = FINAL_OUTCOME_EXAMPLES["classic_mixed"]

    result = interpret_final_build_outcome(example.lines)

    assert result == BuildOutcome.REBUILT
