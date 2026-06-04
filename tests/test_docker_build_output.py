import pytest

from pycastle.services._docker_build_output import (
    FINAL_OUTCOME_EXAMPLES,
    BuildOutcome,
    DockerBuildOutputInterpreter,
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


def test_interpreter_signals_rebuild_start_on_first_executed_buildkit_layer():
    interpreter = DockerBuildOutputInterpreter()

    signals = [
        interpreter.observe_line("#1 [1/2] FROM python:3.12\n"),
        interpreter.observe_line("#1 CACHED\n"),
        interpreter.observe_line("#2 [2/2] COPY . .\n"),
        interpreter.observe_line("#2 DONE 2.5s\n"),
    ]

    assert signals == [False, False, False, True]
    assert interpreter.final_outcome == BuildOutcome.REBUILT


def test_interpreter_signals_rebuild_start_on_first_non_cache_classic_step_body():
    interpreter = DockerBuildOutputInterpreter()

    signals = [
        interpreter.observe_line("Step 1/2 : FROM python:3.12\n"),
        interpreter.observe_line(" ---> Using cache\n"),
        interpreter.observe_line(" ---> abc123\n"),
        interpreter.observe_line("Step 2/2 : COPY . .\n"),
        interpreter.observe_line(" ---> Running in 789abc\n"),
    ]

    assert signals == [False, False, False, False, True]
    assert interpreter.final_outcome == BuildOutcome.REBUILT


def test_interpreter_ignores_internal_buildkit_done_before_first_executed_layer():
    interpreter = DockerBuildOutputInterpreter()

    signals = [
        interpreter.observe_line("#1 [internal] load .dockerignore\n"),
        interpreter.observe_line("#1 DONE 0.1s\n"),
        interpreter.observe_line("#2 [1/2] FROM python:3.12\n"),
        interpreter.observe_line("#2 CACHED\n"),
        interpreter.observe_line("#3 [2/2] COPY . .\n"),
        interpreter.observe_line("#3 DONE 0.5s\n"),
    ]

    assert signals == [False, False, False, False, False, True]
    assert interpreter.final_outcome == BuildOutcome.REBUILT


@pytest.mark.parametrize(
    ("lines", "outcome"),
    [
        pytest.param(
            [
                "#1 [1/2] FROM python:3.12\n",
                "#1 CACHED\n",
                "#2 [2/2] RUN pip install requests\n",
                "#2 CACHED\n",
            ],
            BuildOutcome.FULL_CACHE_HIT,
            id="buildkit-all-cached",
        ),
        pytest.param(
            [
                "Step 1/2 : FROM python:3.12\n",
                " ---> Using cache\n",
                " ---> abc123\n",
                "Step 2/2 : RUN pip install requests\n",
                " ---> Using cache\n",
                " ---> def456\n",
            ],
            BuildOutcome.FULL_CACHE_HIT,
            id="classic-all-cached",
        ),
    ],
)
def test_interpreter_all_cached_output_never_signals_rebuild_start(lines, outcome):
    interpreter = DockerBuildOutputInterpreter()

    signals = [interpreter.observe_line(line) for line in lines]

    assert signals == [False] * len(lines)
    assert interpreter.final_outcome == outcome


def test_interpreter_signals_rebuild_start_only_once_per_build():
    interpreter = DockerBuildOutputInterpreter()

    signals = [
        interpreter.observe_line("#1 [1/3] FROM python:3.12\n"),
        interpreter.observe_line("#1 CACHED\n"),
        interpreter.observe_line("#2 [2/3] RUN apt-get install -y git\n"),
        interpreter.observe_line("#2 DONE 4.2s\n"),
        interpreter.observe_line("#3 [3/3] COPY . .\n"),
        interpreter.observe_line("#3 DONE 0.5s\n"),
        interpreter.observe_line("#4 exporting to image\n"),
        interpreter.observe_line("#4 DONE 0.3s\n"),
    ]

    assert signals == [False, False, False, True, False, False, False, False]
    assert interpreter.final_outcome == BuildOutcome.REBUILT
