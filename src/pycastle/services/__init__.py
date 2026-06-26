from __future__ import annotations

from typing import TYPE_CHECKING

from .runtime_services import (
    AgentService,
    ClaudeService,
    CodexService,
    OpenCodeService,
    ToolPolicy,
)

if TYPE_CHECKING:
    from .docker_service import DockerService
    from .git_service import (
        GitCommandError,
        GitNotFoundError,
        GitService,
        GitServiceError,
        GitTimeoutError,
        OperatorActionableGitError,
        UnrelatedHistoriesError,
    )
    from .github_service import (
        GithubAPIError,
        GithubAuthError,
        GithubNetworkError,
        OperatorActionableGithubError,
        GithubService,
        GithubServiceError,
    )
    from .reset_time_parser import ResetTimeSyntaxMode
    from .service_registry import ServiceRegistry

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
    "OperatorActionableGitError",
    "UnrelatedHistoriesError",
    "GithubAPIError",
    "GithubAuthError",
    "GithubNetworkError",
    "OperatorActionableGithubError",
    "GithubService",
    "GithubServiceError",
    "OpenCodeService",
    "ResetTimeSyntaxMode",
    "ServiceRegistry",
    "ToolPolicy",
    "parse_reset_time",
]


def __getattr__(name: str):
    if name == "DockerService":
        from .docker_service import DockerService

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
        from . import git_service

        return getattr(git_service, name)
    if name in {
        "GithubAPIError",
        "GithubAuthError",
        "GithubNetworkError",
        "OperatorActionableGithubError",
        "GithubService",
        "GithubServiceError",
    }:
        from . import github_service

        return getattr(github_service, name)
    if name in {"ResetTimeSyntaxMode", "parse_reset_time"}:
        from . import reset_time_parser

        return getattr(reset_time_parser, name)
    if name in {"ClaudeService", "CodexService", "OpenCodeService", "ToolPolicy"}:
        from . import runtime_services

        return getattr(runtime_services, name)
    if name == "ServiceRegistry":
        from .service_registry import ServiceRegistry

        return ServiceRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
