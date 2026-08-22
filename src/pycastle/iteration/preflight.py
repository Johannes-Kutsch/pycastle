import asyncio
import dataclasses
import hashlib
from pathlib import Path
from typing import Protocol, cast

from agent_runtime.errors import AgentCredentialFailureError, HardAgentError

from pycastle import _time as _time_module
from pycastle.agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    IssueOutput,
)
from pycastle.agents.runner import AgentRunnerProtocol, RunRequest
from pycastle.config import Config, StageOverride
from pycastle.diagnostic_issue_report_validation import (
    DiagnosticIssueReportValidationAFK,
    DiagnosticIssueReportValidationHITL,
    validate_diagnostic_issue_report,
)
from pycastle.diagnostic_mount_fallback import (
    DiagnosticMountFallbackIssue,
    decide_diagnostic_mount_dispatch,
)
from pycastle.display.status_display import StatusDisplay
from pycastle.errors import (
    AgentFailedError,
    AgentTimeoutError,
    ModelNotAvailableError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
    WorktreeError,
    WorktreeTimeoutError,
)
from pycastle.infrastructure.preflight_failure_interpreter import (
    MissingDeclaredPythonToolDecision,
    OrdinaryPreflightFailureDecision,
    PreflightFailureDecision,
    interpret_preflight_command_failures,
)
from pycastle.infrastructure.worktree import (
    SandboxWorktreeIntent,
    reusable_sandbox_worktree,
    reusable_sandbox_worktree_identity,
)
from pycastle.iteration._fingerprint import prepare_fingerprint_gate
from pycastle.iteration._utils import (
    _advance_branch_ref_through_gate,
    _wait_for_operating_branch_release,
)
from pycastle.managed_worktree_mount_policy import (
    ManagedWorktreeMountRejected,
    decide_managed_worktree_mount,
    describe_managed_worktree_mount_rejection,
    should_reject_managed_worktree_mount,
)
from pycastle.prompts.dispatch import build_prompt_invocation
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.prompts.scope_args import (
    build_divergence_scope_args,
    build_preflight_scope_args,
)
from pycastle.services import (
    GitCommandError,
    GithubService,
    GitService,
    ServiceRegistry,
)
from pycastle.services.git_service import OperatingBranchCheckedOutError
from pycastle.session import RoleSession


def _diverge_sandbox_fingerprint(safe_sha: str, branch: str) -> str:
    return hashlib.sha256(f"{safe_sha}\n{branch}".encode()).hexdigest()


@dataclasses.dataclass(frozen=True)
class PreflightReady:
    sha: str


@dataclasses.dataclass(frozen=True)
class PreflightHITL:
    sha: str
    issue_number: int


@dataclasses.dataclass(frozen=True)
class PreflightAFK:
    sha: str
    issue_number: int


type PreflightResult = PreflightReady | PreflightHITL | PreflightAFK


class _PreflightDeps(Protocol):
    git_svc: GitService
    github_svc: GithubService
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path


class BranchRefreshBoundary:
    _DIVERGE_SANDBOX_INTENT = SandboxWorktreeIntent.DIVERGENCE

    async def pull_with_resolution(self, deps: _PreflightDeps) -> None:
        """Operating-branch refresh per ADR 0062, escalating to the divergence-resolver on divergence."""
        from pycastle.services._operating_branch_refresh import OperatingBranchDiverged

        branch = deps.cfg.operating_branch
        while True:
            try:
                relation = deps.git_svc.refresh_operating_branch(deps.repo_root, branch)
            except OperatingBranchCheckedOutError:
                await _wait_for_operating_branch_release(deps, "Preflight")
                continue

            if not isinstance(relation, OperatingBranchDiverged):
                return

            pull_exc = GitCommandError(
                f"operating branch {branch!r} has diverged from origin", returncode=1
            )
            current_sha = deps.git_svc.get_branch_sha(deps.repo_root, branch)
            sandbox_identity = reusable_sandbox_worktree_identity(
                self._DIVERGE_SANDBOX_INTENT,
                deps.repo_root,
            )
            fingerprint = _diverge_sandbox_fingerprint(current_sha, branch)
            role_session = RoleSession(
                sandbox_identity.path, AgentRole.DIVERGENCE_RESOLVER
            )
            prepare_fingerprint_gate(role_session, fingerprint)
            try:
                async with reusable_sandbox_worktree(
                    self._DIVERGE_SANDBOX_INTENT,
                    sha=current_sha,
                    deps=deps,
                ) as sandbox_path:
                    mount_decision = decide_managed_worktree_mount(
                        repo_root=deps.repo_root,
                        mount_path=sandbox_path,
                        caller="Divergence Resolver",
                        role=AgentRole.DIVERGENCE_RESOLVER.value,
                    )
                    if isinstance(
                        mount_decision, ManagedWorktreeMountRejected
                    ) and should_reject_managed_worktree_mount(mount_decision):
                        raise SetupPhaseError(  # noqa: TRY301  # raise inside try is intentional: exits async-with resource cleanup
                            AgentRole.DIVERGENCE_RESOLVER.value,
                            describe_managed_worktree_mount_rejection(mount_decision),
                        )
                    role_session.write_fingerprint(fingerprint)
                    await deps.agent_runner.run(
                        RunRequest(
                            name="Divergence Resolver",
                            prompt=build_prompt_invocation(
                                PromptTemplate.DIVERGENCE_RESOLVE,
                                build_divergence_scope_args(branch=branch),
                            ),
                            mount_path=sandbox_path,
                            role=AgentRole.DIVERGENCE_RESOLVER,
                            service=deps.cfg.merge_override.service,
                            status_display=deps.status_display,
                            work_body="Resolving divergence",
                        )
                    )
                    await _advance_branch_ref_through_gate(
                        deps, "Preflight", branch, sandbox_identity.branch
                    )
                    role_session.discard()
            except AgentCredentialFailureError:
                raise
            except (
                SetupPhaseError,
                WorktreeError,
                WorktreeTimeoutError,
                AgentTimeoutError,
                TransientAgentError,
                HardAgentError,
                AgentFailedError,
                UsageLimitError,
                ModelNotAvailableError,
                GitCommandError,
                OSError,
            ):
                raise pull_exc from None
            return


class PreflightCache:
    """Single-slot, process-scoped cache for preflight verdicts.

    Constructed once in orchestrator.run() outside the iteration loop so its slot
    survives iteration reconstruction.  All callers serialise via the internal lock.
    """

    def __init__(self) -> None:
        self._verdict: PreflightResult | None = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._branch_refresh = BranchRefreshBoundary()

    def _resolved_preflight_issue_override(self, deps: _PreflightDeps) -> StageOverride:
        registry = cast(
            "ServiceRegistry | None", getattr(deps, "service_registry", None)
        )
        override = deps.cfg.preflight_issue_override
        if registry is None:
            return override
        return registry.resolve(override, _time_module.now_local())

    async def _handle_failure(
        self,
        failure: OrdinaryPreflightFailureDecision,
        deps: _PreflightDeps,
        mount_path: Path,
        sha: str,
    ) -> PreflightHITL | PreflightAFK:
        mount_decision = decide_diagnostic_mount_dispatch(
            repo_root=deps.repo_root,
            mount_path=mount_path,
            caller="Pre-Flight Reporter",
            diagnostic_role=AgentRole.PREFLIGHT_ISSUE.value,
            role_name=AgentRole.PREFLIGHT_ISSUE.value,
            original_failure_summary=(
                f"Preflight check {failure.check_name!r} failed while running "
                f"{failure.command!r}."
            ),
            github_svc=deps.github_svc,
        )
        if isinstance(mount_decision, DiagnosticMountFallbackIssue):
            return PreflightHITL(sha=sha, issue_number=mount_decision.issue_number)
        override = self._resolved_preflight_issue_override(deps)
        agent_result = await deps.agent_runner.run(
            RunRequest(
                name="Pre-Flight Reporter",
                prompt=build_prompt_invocation(
                    PromptTemplate.PREFLIGHT_ISSUE,
                    build_preflight_scope_args(
                        check_name=failure.check_name,
                        command=failure.command,
                        output=failure.output,
                    ),
                ),
                mount_path=mount_path,
                role=AgentRole.PREFLIGHT_ISSUE,
                model=override.model,
                effort=override.effort,
                service=override.service,
                status_display=deps.status_display,
                work_body=f"reporting {failure.check_name} issue",
            )
        )
        if not isinstance(agent_result, IssueOutput):
            raise RuntimeError(
                f"Preflight-issue agent returned unexpected output type: {type(agent_result).__name__}"
            )
        validation = validate_diagnostic_issue_report(
            caller="Pre-Flight Reporter",
            issue_output=agent_result,
            cfg=deps.cfg,
            filed_issue_reader=deps.github_svc,
        )
        if isinstance(validation, DiagnosticIssueReportValidationHITL):
            return PreflightHITL(sha=sha, issue_number=validation.issue_number)
        if not isinstance(validation, DiagnosticIssueReportValidationAFK):
            raise TypeError(
                "exhaustive: only HITL or AFK remain after isinstance check above"
            )
        return PreflightAFK(sha=sha, issue_number=validation.issue_number)

    @staticmethod
    def _setup_error_for_missing_declared_tool(
        decision: MissingDeclaredPythonToolDecision,
    ) -> SetupPhaseError:
        return SetupPhaseError(
            "preflight",
            "Missing expected preflight tool "
            f"'{decision.tool}' declared in "
            f"{decision.dependency_source}.",
            command=decision.command,
            output=decision.output,
        )

    @staticmethod
    def _resolve_failure_decision(
        decisions: tuple[PreflightFailureDecision, ...],
    ) -> OrdinaryPreflightFailureDecision:
        first_decision = decisions[0]
        if isinstance(first_decision, MissingDeclaredPythonToolDecision):
            raise PreflightCache._setup_error_for_missing_declared_tool(
                first_decision
            )  # helper method call in raise is clearer than inner-function abstraction here
        if not isinstance(first_decision, OrdinaryPreflightFailureDecision):
            raise TypeError(
                "exhaustive: only MissingDeclaredPythonTool or Ordinary at this point"
            )
        return first_decision

    async def pull_with_resolution(self, deps: _PreflightDeps) -> None:
        await self._branch_refresh.pull_with_resolution(deps)

    async def get_safe_sha(self, deps: _PreflightDeps) -> PreflightResult:
        from pycastle.infrastructure.worktree import detached_transient_worktree

        async with self._lock:
            await _wait_for_operating_branch_release(deps, "Preflight")
            try:
                await self._branch_refresh.pull_with_resolution(deps)
            except GitCommandError as pull_exc:
                if "diverged" not in str(pull_exc).lower():
                    deps.status_display.print(
                        "Preflight",
                        "git fetch failed — remote branch is unreachable or has irreconcilable conflicts. "
                        "Resolve manually and retry.",
                        style="error",
                    )
                raise
            sha = deps.git_svc.get_branch_sha(deps.repo_root, deps.cfg.operating_branch)

            if self._verdict is not None and self._verdict.sha == sha:
                return self._verdict

            async with detached_transient_worktree(
                "preflight-sandbox",
                sha=sha,
                deps=deps,
            ) as mount_path:
                failures = await deps.agent_runner.run_preflight(
                    name="Preflight Agent",
                    mount_path=mount_path,
                    status_display=deps.status_display,
                    work_body="Checking",
                )

                result: PreflightResult
                if failures:
                    decision = self._resolve_failure_decision(
                        interpret_preflight_command_failures(deps.repo_root, failures)
                    )
                    try:
                        result = await self._handle_failure(
                            decision,
                            deps,
                            mount_path,
                            sha,
                        )
                    except AgentOutputProtocolError as parse_exc:
                        raise RuntimeError(str(parse_exc)) from parse_exc
                else:
                    result = PreflightReady(sha=sha)

                self._verdict = result
                return result
