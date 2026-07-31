"""Issue 2007: _execute_runtime_request propagates non-OSError from session teardown."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from pycastle.agents.output_protocol import AgentRole
from pycastle.execution_contracts import (
    RuntimeInvocationDependencies,
    RuntimeInvocationRequest,
    RuntimeRunSession,
)
from pycastle.runtime import _execute_runtime_request
from pycastle.services.runtime_services import AgentService


class _ExplodingExitSession:
    def __exit__(self, *_args) -> None:
        raise RuntimeError("teardown failed")


def _make_request(tmp_path: Path) -> RuntimeInvocationRequest:
    mount_path = tmp_path / "repo" / "pycastle" / ".worktrees" / "issue-2007"
    mount_path.mkdir(parents=True)

    service = MagicMock(spec=AgentService)
    service.name = "codex"

    prepared_session = MagicMock()
    prepared_session.provider_state_dir_container_path = None

    exploding_session = _ExplodingExitSession()

    deps = RuntimeInvocationDependencies(
        container_workspace="/workspace",
        timeout_retries=0,
        stage_key_for_role=lambda _role: None,
        prepare_session=lambda _: prepared_session,
        build_session=lambda *_: exploding_session,
        build_runner=lambda *_: MagicMock(),
        get_git_identity=lambda: (_ for _ in ()).throw(ValueError("fail fast")),
    )

    run_session = RuntimeRunSession(
        mount_path=mount_path,
        role=AgentRole.IMPLEMENTER,
        session_namespace="",
        service=service,
        container_workspace="/workspace",
    )

    return RuntimeInvocationRequest(
        name="Test Agent",
        mount_path=mount_path,
        role=AgentRole.IMPLEMENTER,
        service=service,
        model="gpt-5.5",
        effort="medium",
        output_adapter=MagicMock(),
        dependencies=deps,
        run_session=run_session,
    )


def test_execute_runtime_request_propagates_non_oserror_from_session_teardown(tmp_path):
    request = _make_request(tmp_path)

    with pytest.raises(RuntimeError, match="teardown failed"):
        asyncio.run(_execute_runtime_request(request))
