from unittest.mock import MagicMock

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
        interpreter.observe_line("#1 [1/2] FROM python:3.12\n").rebuild_started,
        interpreter.observe_line("#1 CACHED\n").rebuild_started,
        interpreter.observe_line("#2 [2/2] COPY . .\n").rebuild_started,
        interpreter.observe_line("#2 DONE 2.5s\n").rebuild_started,
    ]

    assert signals == [False, False, False, True]
    assert interpreter.final_outcome == BuildOutcome.REBUILT


def test_interpreter_calls_rebuild_start_callback_once_on_first_executed_buildkit_layer():
    callback = MagicMock()
    interpreter = DockerBuildOutputInterpreter(on_rebuild_start=callback)

    for line in (
        "#1 [internal] load .dockerignore\n",
        "#1 DONE 0.1s\n",
        "#2 [1/3] FROM python:3.12\n",
        "#2 CACHED\n",
        "#3 [2/3] RUN apt-get install -y git\n",
        "#3 DONE 4.2s\n",
        "#4 [3/3] COPY . .\n",
        "#4 DONE 0.5s\n",
    ):
        interpreter.observe_line(line)

    assert interpreter.final_outcome == BuildOutcome.REBUILT
    callback.assert_called_once_with()


def test_interpreter_signals_rebuild_start_on_first_non_cache_classic_step_body():
    interpreter = DockerBuildOutputInterpreter()

    signals = [
        interpreter.observe_line("Step 1/2 : FROM python:3.12\n").rebuild_started,
        interpreter.observe_line(" ---> Using cache\n").rebuild_started,
        interpreter.observe_line(" ---> abc123\n").rebuild_started,
        interpreter.observe_line("Step 2/2 : COPY . .\n").rebuild_started,
        interpreter.observe_line(" ---> Running in 789abc\n").rebuild_started,
    ]

    assert signals == [False, False, False, False, True]
    assert interpreter.final_outcome == BuildOutcome.REBUILT


def test_interpreter_signals_rebuild_start_for_classic_step_body_after_blank_line():
    interpreter = DockerBuildOutputInterpreter()

    signals = [
        interpreter.observe_line("Step 1/2 : FROM python:3.12\n").rebuild_started,
        interpreter.observe_line(" ---> Using cache\n").rebuild_started,
        interpreter.observe_line(" ---> abc123\n").rebuild_started,
        interpreter.observe_line("Step 2/2 : COPY . .\n").rebuild_started,
        interpreter.observe_line("\n").rebuild_started,
        interpreter.observe_line(" ---> Running in 789abc\n").rebuild_started,
    ]

    assert signals == [False, False, False, False, False, True]
    assert interpreter.final_outcome == BuildOutcome.REBUILT


def test_interpreter_does_not_call_rebuild_start_callback_for_all_cached_classic_output():
    callback = MagicMock()
    interpreter = DockerBuildOutputInterpreter(on_rebuild_start=callback)

    for line in FINAL_OUTCOME_EXAMPLES["classic_all_cached"].lines:
        interpreter.observe_line(line)

    assert interpreter.final_outcome == BuildOutcome.FULL_CACHE_HIT
    callback.assert_not_called()


def test_interpreter_calls_rebuild_start_callback_for_classic_step_body_after_blank_line():
    callback = MagicMock()
    interpreter = DockerBuildOutputInterpreter(on_rebuild_start=callback)

    for line in (
        "Step 1/2 : FROM python:3.12\n",
        " ---> Using cache\n",
        " ---> abc123\n",
        "Step 2/2 : COPY . .\n",
        "\n",
        " ---> Running in 789abc\n",
        "Successfully built 789abc\n",
    ):
        interpreter.observe_line(line)

    assert interpreter.final_outcome == BuildOutcome.REBUILT
    callback.assert_called_once_with()


def test_interpreter_ignores_internal_buildkit_done_before_first_executed_layer():
    interpreter = DockerBuildOutputInterpreter()

    signals = [
        interpreter.observe_line("#1 [internal] load .dockerignore\n").rebuild_started,
        interpreter.observe_line("#1 DONE 0.1s\n").rebuild_started,
        interpreter.observe_line("#2 [1/2] FROM python:3.12\n").rebuild_started,
        interpreter.observe_line("#2 CACHED\n").rebuild_started,
        interpreter.observe_line("#3 [2/2] COPY . .\n").rebuild_started,
        interpreter.observe_line("#3 DONE 0.5s\n").rebuild_started,
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

    signals = [interpreter.observe_line(line).rebuild_started for line in lines]

    assert signals == [False] * len(lines)
    assert interpreter.final_outcome == outcome


def test_interpreter_signals_rebuild_start_only_once_per_build():
    interpreter = DockerBuildOutputInterpreter()

    signals = [
        interpreter.observe_line("#1 [1/3] FROM python:3.12\n").rebuild_started,
        interpreter.observe_line("#1 CACHED\n").rebuild_started,
        interpreter.observe_line(
            "#2 [2/3] RUN apt-get install -y git\n"
        ).rebuild_started,
        interpreter.observe_line("#2 DONE 4.2s\n").rebuild_started,
        interpreter.observe_line("#3 [3/3] COPY . .\n").rebuild_started,
        interpreter.observe_line("#3 DONE 0.5s\n").rebuild_started,
        interpreter.observe_line("#4 exporting to image\n").rebuild_started,
        interpreter.observe_line("#4 DONE 0.3s\n").rebuild_started,
    ]

    assert signals == [False, False, False, True, False, False, False, False]
    assert interpreter.final_outcome == BuildOutcome.REBUILT


def test_interpreter_exposes_initial_terse_progress_text():
    interpreter = DockerBuildOutputInterpreter()

    assert interpreter.initial_progress_text == "preparing…"


def test_interpreter_emits_buildkit_step_progress_transitions():
    interpreter = DockerBuildOutputInterpreter()

    transitions = [
        interpreter.observe_line("#1 [1/3] FROM python:3.12\n").progress_text,
        interpreter.observe_line("#1 CACHED\n").progress_text,
        interpreter.observe_line("#2 [2/4] RUN apt-get install -y git\n").progress_text,
        interpreter.observe_line("#2 DONE 4.2s\n").progress_text,
        interpreter.observe_line("#3 [3/9] COPY . .\n").progress_text,
    ]

    assert transitions == [
        "Step 1/3",
        None,
        "Step 2/3",
        None,
        "Step 3/3",
    ]


def test_interpreter_emits_classic_step_progress_transitions():
    interpreter = DockerBuildOutputInterpreter()

    transitions = [
        interpreter.observe_line("Step 1/2 : FROM python:3.12\n").progress_text,
        interpreter.observe_line(" ---> Using cache\n").progress_text,
        interpreter.observe_line(" ---> abc123\n").progress_text,
        interpreter.observe_line("Step 2/2 : COPY . .\n").progress_text,
        interpreter.observe_line(" ---> Running in 789abc\n").progress_text,
    ]

    assert transitions == [
        "Step 1/2",
        None,
        None,
        "Step 2/2",
        None,
    ]


@pytest.mark.parametrize(
    "line",
    [
        "#4 exporting to image\n",
        "#4 naming to docker.io/library/img:latest\n",
        "#4 unpacking to docker.io/library/img:latest\n",
        "#4 manifest sha256:abc123\n",
        "#4 pushing layers\n",
    ],
)
def test_interpreter_emits_exporting_progress_after_step_progress(line):
    interpreter = DockerBuildOutputInterpreter()
    interpreter.observe_line("#1 [1/2] FROM python:3.12\n")
    interpreter.observe_line("#1 CACHED\n")
    interpreter.observe_line("#2 [2/2] COPY . .\n")
    interpreter.observe_line("#2 DONE 0.5s\n")

    transition = interpreter.observe_line(line).progress_text

    assert transition == "exporting…"


def test_interpreter_suppresses_duplicate_progress_transitions_for_repeated_steps():
    interpreter = DockerBuildOutputInterpreter()

    transitions = [
        interpreter.observe_line("#1 [1/2] FROM python:3.12\n").progress_text,
        interpreter.observe_line("#1 [1/2] FROM python:3.12\n").progress_text,
        interpreter.observe_line("#2 [2/2] COPY . .\n").progress_text,
        interpreter.observe_line("#2 [2/2] COPY . .\n").progress_text,
        interpreter.observe_line("#3 exporting to image\n").progress_text,
        interpreter.observe_line("#3 exporting to image\n").progress_text,
    ]

    assert transitions == [
        "Step 1/2",
        None,
        "Step 2/2",
        None,
        "exporting…",
        None,
    ]


def test_interpreter_treats_repeated_classic_step_headers_as_one_cached_build_step():
    interpreter = DockerBuildOutputInterpreter()

    transitions = [
        interpreter.observe_line("Step 1/2 : FROM python:3.12\n").progress_text,
        interpreter.observe_line("Step 1/2 : FROM python:3.12\n").progress_text,
        interpreter.observe_line(" ---> Using cache\n").progress_text,
        interpreter.observe_line(" ---> abc123\n").progress_text,
        interpreter.observe_line("Step 2/2 : COPY . .\n").progress_text,
        interpreter.observe_line("Step 2/2 : COPY . .\n").progress_text,
        interpreter.observe_line(" ---> Using cache\n").progress_text,
        interpreter.observe_line(" ---> def456\n").progress_text,
    ]

    assert transitions == [
        "Step 1/2",
        None,
        None,
        None,
        "Step 2/2",
        None,
        None,
        None,
    ]
    assert interpreter.final_outcome == BuildOutcome.FULL_CACHE_HIT


@pytest.mark.parametrize(
    "example",
    [
        pytest.param(example, id=example_name)
        for example_name, example in FINAL_OUTCOME_EXAMPLES.items()
    ],
)
def test_interpreter_success_progress_text_matches_final_build_outcome(example):
    interpreter = DockerBuildOutputInterpreter()

    for line in example.lines:
        interpreter.observe_line(line)

    expected = (
        "up to date" if example.outcome == BuildOutcome.FULL_CACHE_HIT else "completed"
    )
    assert interpreter.success_progress_text == expected


def test_interpreter_defaults_empty_output_to_rebuilt():
    interpreter = DockerBuildOutputInterpreter()

    assert interpreter.final_outcome == BuildOutcome.REBUILT
    assert interpreter.success_progress_text == "completed"
