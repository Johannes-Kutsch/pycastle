import asyncio
from unittest.mock import MagicMock

import pytest

from pycastle.agents._work_preparation import _prepare_work
from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.runner import RunRequest
from pycastle.config import Config
from pycastle.display.status_display import PlainStatusDisplay
from pycastle.errors import UsageLimitError
from pycastle.execution_contracts import CancellationToken
from pycastle.prompts.dispatch import PromptInvocation
from pycastle.prompts.pipeline import PromptRenderer, PromptTemplate
from pycastle.services import GitService
from pycastle.session import RunKind
from tests.support import RecordingStatusDisplay


class _FakeService:
    name = "codex"

    def is_available(self, now=None, *, model=None) -> bool:
        del now, model
        return True

    def state_dir_relpath(self, role, namespace=""):
        del role, namespace

    def auth_seed_action(self, provider_state_dir):
        del provider_state_dir

    def provider_auth(self):
        return None

    def valid_models(self):
        return frozenset({"gpt-5.5"})

    def build_env(self, state_dir_container_path=None, token=None):
        del state_dir_container_path, token
        return {}


class _FakeDockerSession:
    def __init__(self):
        self._container = type("Container", (), {"id": "container-123"})()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def exec_simple(self, command, timeout=None):
        del timeout, command
        return ""

    def exec_command(self, command):
        del command
        return ""


def _make_git_service():
    git_service = MagicMock(spec=GitService)
    git_service.get_user_name.return_value = "Test User"
    git_service.get_user_email.return_value = "test@example.com"
    return git_service


def _make_request(  # noqa: PLR0913
    mount_path,
    *,
    service="codex",
    model="gpt-5.5",
    effort="medium",
    role=AgentRole.IMPLEMENTER,
    token=None,
    status_display=None,
    issue_number="2482",
):
    return RunRequest(
        name=f"Test Agent #{issue_number}",
        prompt=PromptInvocation(
            template=PromptTemplate.IMPLEMENT_BEHAVIOR,
            scope_args={
                "ISSUE_NUMBER": issue_number,
                "ISSUE_TITLE": "Test",
                "ISSUE_BODY": "",
                "ISSUE_COMMENTS": "",
                "BRANCH": f"issue-{issue_number}",
                "INTERRUPTED_WORK": "",
                "OPERATING_BRANCH": "main",
            },
        ),
        mount_path=mount_path,
        role=role,
        model=model,
        effort=effort,
        service=service,
        token=token,
        status_display=status_display,
    )


def _call_prepare(request, service, tmp_path, *, git_service=None):
    if git_service is None:
        git_service = _make_git_service()
    cfg = Config(logs_dir=tmp_path / "logs")
    renderer = PromptRenderer(cfg)

    def build_session(mount_path, svc, state_dir_container_path):
        del mount_path, svc, state_dir_container_path
        return _FakeDockerSession()

    return _prepare_work(request, service, cfg, git_service, renderer, build_session)


def _mount(tmp_path, label="issue-2482"):
    p = tmp_path / "repo" / "pycastle" / ".worktrees" / label
    p.mkdir(parents=True)
    return p


# ── AC2: state_dir_relpath=None uses role_session.path ─────────────────────


def test_prepare_work_state_dir_relpath_none_uses_role_session_path(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482a")
    prep = _call_prepare(_make_request(mount_path), _FakeService(), tmp_path)
    assert prep.provider_state_dir == mount_path / ".pycastle-session" / "implementer"


# ── AC3: state_dir_relpath non-None uses mount_path / relpath ──────────────


def test_prepare_work_state_dir_relpath_present_uses_relpath(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482b")

    class _RealpathService(_FakeService):
        def state_dir_relpath(self, role, namespace=""):
            del role, namespace
            return ".pycastle-session/implementer/codex/"

    prep = _call_prepare(_make_request(mount_path), _RealpathService(), tmp_path)
    assert (
        prep.provider_state_dir
        == mount_path / ".pycastle-session" / "implementer" / "codex"
    )


# ── AC4: auth_seed_action applied before provider_auth ─────────────────────


def test_prepare_work_auth_seed_action_applied_before_provider_auth(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482c")
    call_order: list[str] = []

    class _AuthSeedAction:
        def apply(self):
            call_order.append("apply")

    class _AuthSeedService(_FakeService):
        def auth_seed_action(self, provider_state_dir):
            del provider_state_dir
            return _AuthSeedAction()

        def provider_auth(self):
            call_order.append("provider_auth")

    _call_prepare(_make_request(mount_path), _AuthSeedService(), tmp_path)
    assert call_order == ["apply", "provider_auth"]


def test_prepare_work_none_auth_seed_action_skips_apply_reads_provider_auth(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482c2")
    call_order: list[str] = []

    class _NoSeedService(_FakeService):
        def auth_seed_action(self, provider_state_dir):
            del provider_state_dir

        def provider_auth(self):
            call_order.append("provider_auth")

    _call_prepare(_make_request(mount_path), _NoSeedService(), tmp_path)
    assert call_order == ["provider_auth"]


# ── AC5: model/effort resolution ───────────────────────────────────────────


def test_prepare_work_empty_model_resolves_to_service_default(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482d")

    class _HaikuService(_FakeService):
        def valid_models(self):
            return frozenset({"haiku"})

    prep = _call_prepare(_make_request(mount_path, model=""), _HaikuService(), tmp_path)
    assert prep.resolved_model == "haiku"


def test_prepare_work_non_empty_model_carried_verbatim(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482e")
    prep = _call_prepare(
        _make_request(mount_path, model="gpt-5.5"), _FakeService(), tmp_path
    )
    assert prep.resolved_model == "gpt-5.5"


def test_prepare_work_empty_effort_defaults_to_medium(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482f")
    prep = _call_prepare(_make_request(mount_path, effort=""), _FakeService(), tmp_path)
    assert prep.resolved_effort == "medium"


def test_prepare_work_non_empty_effort_carried_verbatim(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482g")
    prep = _call_prepare(
        _make_request(mount_path, effort="high"), _FakeService(), tmp_path
    )
    assert prep.resolved_effort == "high"


def test_prepare_work_valid_models_error_propagates(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482h")

    class _FailingService(_FakeService):
        def valid_models(self):
            raise ValueError("models unavailable")

    with pytest.raises(ValueError, match="models unavailable"):
        _call_prepare(_make_request(mount_path, model=""), _FailingService(), tmp_path)


# ── AC6: color_key derivation ───────────────────────────────────────────────


def test_prepare_work_implementer_digit_issue_number_sets_color_key(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482i")
    prep = _call_prepare(
        _make_request(mount_path, role=AgentRole.IMPLEMENTER, issue_number="2482"),
        _FakeService(),
        tmp_path,
    )
    assert prep.color_key == 2482


def test_prepare_work_reviewer_digit_issue_number_sets_color_key(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482j")
    request = RunRequest(
        name="Review Agent #2482",
        prompt=PromptInvocation(
            template=PromptTemplate.REVIEW,
            scope_args={
                "ISSUE_NUMBER": "2482",
                "ISSUE_TITLE": "Test",
                "ISSUE_BODY": "",
                "ISSUE_COMMENTS": "",
                "BRANCH": "issue-2482",
                "INTERRUPTED_WORK": "",
                "OPERATING_BRANCH": "main",
            },
        ),
        mount_path=mount_path,
        role=AgentRole.REVIEWER,
        model="gpt-5.5",
        effort="medium",
        service="codex",
    )
    prep = _call_prepare(request, _FakeService(), tmp_path)
    assert prep.color_key == 2482


def test_prepare_work_planner_role_color_key_is_none(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482k")
    request = RunRequest(
        name="Plan Agent",
        prompt=PromptInvocation(
            template=PromptTemplate.PLAN,
            scope_args={
                "ALL_OPEN_ISSUES_JSON": "[]",
                "READY_FOR_AGENT_ISSUES_JSON": "[]",
            },
        ),
        mount_path=mount_path,
        role=AgentRole.PLANNER,
        model="gpt-5.5",
        effort="medium",
        service="codex",
    )
    prep = _call_prepare(request, _FakeService(), tmp_path)
    assert prep.color_key is None


def test_prepare_work_non_digit_issue_number_color_key_is_none(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482l")
    prep = _call_prepare(
        _make_request(
            mount_path, role=AgentRole.IMPLEMENTER, issue_number="not-a-number"
        ),
        _FakeService(),
        tmp_path,
    )
    assert prep.color_key is None


# ── AC7: cancelled token / unavailable service ─────────────────────────────


def test_prepare_work_raises_usage_limit_error_on_cancelled_token(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482m")
    token = CancellationToken()
    token.cancel()
    with pytest.raises(UsageLimitError):
        _call_prepare(_make_request(mount_path, token=token), _FakeService(), tmp_path)


def test_prepare_work_raises_usage_limit_error_on_unavailable_service(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482n")

    class _UnavailableService(_FakeService):
        def is_available(self, now=None, *, model=None):
            del now, model
            return False

    with pytest.raises(UsageLimitError):
        _call_prepare(_make_request(mount_path), _UnavailableService(), tmp_path)


def test_prepare_work_usage_limit_error_stage_key_matches_role(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482o")
    token = CancellationToken()
    token.cancel()
    with pytest.raises(UsageLimitError) as excinfo:
        _call_prepare(_make_request(mount_path, token=token), _FakeService(), tmp_path)
    assert excinfo.value.stage_key == "implement"


def test_prepare_work_guard_does_not_build_state_on_cancelled_token(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482o2")
    token = CancellationToken()
    token.cancel()
    build_session_calls: list[object] = []

    cfg = Config(logs_dir=tmp_path / "logs")
    renderer = PromptRenderer(cfg)

    def build_session(mount_path, svc, state_dir):
        build_session_calls.append((mount_path, svc, state_dir))
        return _FakeDockerSession()

    request = _make_request(mount_path, token=token)
    with pytest.raises(UsageLimitError):
        _prepare_work(
            request, _FakeService(), cfg, _make_git_service(), renderer, build_session
        )

    assert build_session_calls == []


# ── AC8: status_display defaulting ─────────────────────────────────────────


def test_prepare_work_none_status_display_becomes_plain_display(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482p")
    prep = _call_prepare(
        _make_request(mount_path, status_display=None), _FakeService(), tmp_path
    )
    assert isinstance(prep.status_display, PlainStatusDisplay)


def test_prepare_work_non_none_status_display_carried_verbatim(tmp_path):
    mount_path = _mount(tmp_path, "issue-2482q")
    display = RecordingStatusDisplay()
    prep = _call_prepare(
        _make_request(mount_path, status_display=display), _FakeService(), tmp_path
    )
    assert prep.status_display is display


# ── AC9: render_prompt closure ─────────────────────────────────────────────


def test_prepare_work_render_prompt_calls_render_prompt_invocation_with_request_prompt(
    tmp_path, monkeypatch
):
    mount_path = _mount(tmp_path, "issue-2482r")
    request = _make_request(mount_path)
    prep = _call_prepare(request, _FakeService(), tmp_path)

    render_calls: list[tuple] = []

    async def _fake_render(invocation, *, renderer, run_kind, exec_fn):
        del renderer, exec_fn
        render_calls.append((invocation, run_kind))
        return "rendered"

    monkeypatch.setattr(
        "pycastle.agents._work_preparation.render_prompt_invocation",
        _fake_render,
    )

    result = asyncio.run(prep.render_prompt(request, RunKind.FRESH))
    assert result == "rendered"
    assert len(render_calls) == 1
    assert render_calls[0][0] is request.prompt
    assert render_calls[0][1] is RunKind.FRESH


def test_prepare_work_render_prompt_uses_request_prompt_at_call_time(
    tmp_path, monkeypatch
):
    mount_path = _mount(tmp_path, "issue-2482r2")
    original_request = _make_request(mount_path)
    prep = _call_prepare(original_request, _FakeService(), tmp_path)

    # Create a different request with a different prompt (simulating _recover_stale_continuation)
    modified_prompt = PromptInvocation(
        template=PromptTemplate.IMPLEMENT_BEHAVIOR,
        scope_args={
            **original_request.prompt.scope_args,
            "INTERRUPTED_WORK": "some work",
        },
    )
    modified_request = RunRequest(
        **{
            f.name: getattr(original_request, f.name)
            for f in original_request.__dataclass_fields__.values()
        }
        | {"prompt": modified_prompt},
    )

    captured: list[PromptInvocation] = []

    async def _fake_render(invocation, *, renderer, run_kind, exec_fn):
        del renderer, exec_fn, run_kind
        captured.append(invocation)
        return "rendered"

    monkeypatch.setattr(
        "pycastle.agents._work_preparation.render_prompt_invocation",
        _fake_render,
    )

    asyncio.run(prep.render_prompt(modified_request, RunKind.FRESH))
    assert len(captured) == 1
    assert captured[0] is modified_request.prompt
    assert captured[0] is not original_request.prompt


# ── AC10: planned_protocol_reprompt closure ────────────────────────────────


def test_prepare_work_planned_protocol_reprompt_includes_parser_error(
    tmp_path, monkeypatch
):
    mount_path = _mount(tmp_path, "issue-2482s")
    request = RunRequest(
        name="Plan Agent",
        prompt=PromptInvocation(
            template=PromptTemplate.PLAN,
            scope_args={
                "ALL_OPEN_ISSUES_JSON": "[]",
                "READY_FOR_AGENT_ISSUES_JSON": "[]",
            },
        ),
        mount_path=mount_path,
        role=AgentRole.PLANNER,
        model="gpt-5.5",
        effort="medium",
        service="codex",
    )
    prep = _call_prepare(request, _FakeService(), tmp_path)

    monkeypatch.setattr(
        "pycastle.prompts.pipeline.PromptRenderer.render_expected_output_shape",
        lambda self, template, scope_args: "<plan>{...}</plan>",
    )

    result = prep.planned_protocol_reprompt("Plan JSON must be an object")
    assert "Plan JSON must be an object" in result
    assert "<plan>{...}</plan>" in result


def test_prepare_work_planned_protocol_reprompt_none_error_uses_unknown(
    tmp_path, monkeypatch
):
    mount_path = _mount(tmp_path, "issue-2482s2")
    request = RunRequest(
        name="Plan Agent",
        prompt=PromptInvocation(
            template=PromptTemplate.PLAN,
            scope_args={
                "ALL_OPEN_ISSUES_JSON": "[]",
                "READY_FOR_AGENT_ISSUES_JSON": "[]",
            },
        ),
        mount_path=mount_path,
        role=AgentRole.PLANNER,
        model="gpt-5.5",
        effort="medium",
        service="codex",
    )
    prep = _call_prepare(request, _FakeService(), tmp_path)

    monkeypatch.setattr(
        "pycastle.prompts.pipeline.PromptRenderer.render_expected_output_shape",
        lambda self, template, scope_args: "<plan>{...}</plan>",
    )

    result = prep.planned_protocol_reprompt(None)
    assert "unknown" in result
