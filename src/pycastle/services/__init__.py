from __future__ import annotations

from typing import TYPE_CHECKING

from pycastle.services.runtime_services import (
    AgentService,
    ClaudeService,
    CodexService,
    OpenCodeService,
    ToolPolicy,
)

if TYPE_CHECKING:
    from pycastle.services.docker_service import DockerService
    from pycastle.services.git_service import (
        GitCommandError,
        GitNotFoundError,
        GitService,
        GitServiceError,
        GitTimeoutError,
        OperatorActionableGitError,
        UnrelatedHistoriesError,
    )
    from pycastle.services.github_service import (
        GithubAPIError,
        GithubAuthError,
        GithubNetworkError,
        GithubService,
        GithubServiceError,
        OperatorActionableGithubError,
    )
    from pycastle.services.service_registry import ServiceRegistry

__all__ = [
    "AgentService",
    "ClaudeService",
    "CodexService",
    "DockerService",
    "GitCommandError",
    "GitNotFoundError",
    "GitService",
    "GitServiceError",
    "GitTimeoutError",
    "GithubAPIError",
    "GithubAuthError",
    "GithubNetworkError",
    "GithubService",
    "GithubServiceError",
    "OpenCodeService",
    "OperatorActionableGitError",
    "OperatorActionableGithubError",
    "ServiceRegistry",
    "ToolPolicy",
    "UnrelatedHistoriesError",
]


def __getattr__(name: str) -> object:
    if name == "DockerService":
        from pycastle.services.docker_service import DockerService

        return DockerService
    if name in {
        "GitCommandError",
        "GitNotFoundError",
        "GitService",
        "GitServiceError",
        "GitTimeoutError",
        "OperatorActionableGitError",
        "UnrelatedHistoriesError",
    }:
        from pycastle.services import git_service

        return getattr(git_service, name)
    if name in {
        "GithubAPIError",
        "GithubAuthError",
        "GithubNetworkError",
        "OperatorActionableGithubError",
        "GithubService",
        "GithubServiceError",
    }:
        from pycastle.services import github_service

        return getattr(github_service, name)
    if name in {"ClaudeService", "CodexService", "OpenCodeService", "ToolPolicy"}:
        from pycastle.services import runtime_services

        return getattr(runtime_services, name)
    if name == "ServiceRegistry":
        from pycastle.services.service_registry import ServiceRegistry

        return ServiceRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
