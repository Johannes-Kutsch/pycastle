from __future__ import annotations

from typing import TYPE_CHECKING

from .agent_service import (
    AgentService,
    AssistantTurn,
    ParsedTurn,
    PromptTokens,
    Result,
    UnsupportedTokens,
    UsageLimit,
)
from .provider_session_state import (
    ProviderSessionState,
    ProviderSessionStateRequest,
)

if TYPE_CHECKING:
    from .claude_service import ClaudeService
    from .codex_service import CodexService
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
    from .opencode_service import OpenCodeService
    from .reset_time_parser import ResetTimeSyntaxMode
    from pycastle_agent_runtime.service_registry import ServiceRegistry

__all__ = [
    "AgentService",
    "AssistantTurn",
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
    "ParsedTurn",
    "ProviderSessionState",
    "ProviderSessionStateRequest",
    "PromptTokens",
    "ResetTimeSyntaxMode",
    "Result",
    "ServiceRegistry",
    "UnsupportedTokens",
    "UsageLimit",
    "parse_reset_time",
]


def __getattr__(name: str):
    if name == "ClaudeService":
        from .claude_service import ClaudeService

        return ClaudeService
    if name == "CodexService":
        from .codex_service import CodexService

        return CodexService
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
    if name == "OpenCodeService":
        from .opencode_service import OpenCodeService

        return OpenCodeService
    if name in {"ResetTimeSyntaxMode", "parse_reset_time"}:
        from . import reset_time_parser

        return getattr(reset_time_parser, name)
    if name == "ServiceRegistry":
        from pycastle_agent_runtime.service_registry import ServiceRegistry

        return ServiceRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
