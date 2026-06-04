import asyncio
import dataclasses
import re
from pathlib import Path
from typing import Protocol, Sequence, TypeAlias, cast

from ..agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    IssueOutput,
)
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..prompts.pipeline import PromptTemplate
from ..prompts.scope_args import (
    build_divergence_scope_args,
    build_preflight_scope_args,
)
from ..services import (
    GitCommandError,
    GitService,
    GithubService,
    ServiceRegistry,
    UnrelatedHistoriesError,
)
from ..session import RoleSession
from ..errors import SetupPhaseError
from ..issue_readiness import (
    diagnostic_issue_readiness_error,
    resolve_issue_readiness,
)
from ..display.status_display import StatusDisplay
from ._utils import _wait_for_clean_working_tree
from ..preflight_tool_failure_analysis import (
    analyze_preflight_command_failures,
    OrdinaryCheckFailure,
    PreflightCommandFailure,
)
from .. import _time as _time_module


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


PreflightResult: TypeAlias = PreflightReady | PreflightHITL | PreflightAFK


class _PreflightDeps(Protocol):
    git_svc: GitService
    github_svc: GithubService
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path


def validate_issue_report(
    *,
    caller: str,
    issue_output: IssueOutput,
    cfg: Config,
    github_svc: GithubService,
) -> str:
    reported_readiness = resolve_issue_readiness(
        {"labels": list(issue_output.labels)},
        cfg,
    )
    if reported_readiness.is_hitl_exempt:
        return "hitl"
    filed_issue = github_svc.get_issue(issue_output.number)
    filed_labels = (
        filed_issue["labels"] if "labels" in filed_issue else list(issue_output.labels)
    )
    filed_issue_with_labels = {
        **filed_issue,
        "labels": filed_labels,
    }
    filed_readiness = resolve_issue_readiness(filed_issue_with_labels, cfg)
    readiness_error = diagnostic_issue_readiness_error(
        caller=caller,
        issue_number=issue_output.number,
        issue_labels=filed_issue_with_labels["labels"],
        readiness=filed_readiness,
    )
    if readiness_error is not None:
        raise RuntimeError(readiness_error)
    return "afk"


def strip_stale_blocker_refs(issues: list[dict]) -> list[dict]:
    open_numbers = {i["number"] for i in issues}
    result = []
    for issue in issues:
        body = issue.get("body") or ""
        lines = body.splitlines()
        cleaned = []
        for line in lines:
            if re.search(r"blocked\s+by\s+#\d+", line, re.IGNORECASE):
                refs = {int(m) for m in re.findall(r"#(\d+)", line)}
                if refs.isdisjoint(open_numbers):
                    continue
            cleaned.append(line)
        result.append({**issue, "body": "\n".join(cleaned)})
    return result


class BranchRefreshBoundary:
    """Refresh the current branch, preserving preflight's existing recovery flow."""

    _DIVERGE_SANDBOX = "pycastle/diverge-sandbox"

    @staticmethod
    def _try_recover_unrelated_histories(deps: _PreflightDeps) -> bool:
        """Resync to origin if local has no commits ahead; otherwise emit guidance.

        Returns True when recovery succeeded and the caller should treat the pull
        as if it had succeeded. Returns False to signal the caller must re-raise.
        """
        branch = deps.git_svc.get_current_branch(deps.repo_root)
        remote_ref = f"origin/{branch}"
        ahead = deps.git_svc.count_commits_ahead(deps.repo_root, remote_ref)
        if ahead == 0:
            deps.git_svc.hard_reset_to(deps.repo_root, remote_ref)
            deps.status_display.print(
                "Preflight",
                f"Upstream history was rewritten. Local branch resynced to {remote_ref}.",
            )
            return True
        subjects = deps.git_svc.get_local_only_commit_subjects(
            deps.repo_root, remote_ref
        )
        if subjects:
            shown = subjects[:10]
            commit_list = "\n".join(f"  • {s}" for s in shown)
            if len(subjects) > len(shown):
                commit_list += f"\n  … and {len(subjects) - len(shown)} more"
        else:
            commit_list = f"  ({ahead} commit(s))"
        deps.status_display.print(
            "Preflight",
            f"Upstream history was rewritten but local branch has {ahead} "
            f"commit(s) not present on {remote_ref}.\n"
            f"Pycastle cannot determine whether these are lost work or "
            f"logically-equivalent pre-rewrite copies.\n"
            f"Local-only commits:\n{commit_list}\n"
            f"To recover manually once you have confirmed nothing is lost:\n"
            f"  git fetch origin && git reset --hard {remote_ref}",
            style="error",
        )
        return False

    async def pull_with_resolution(self, deps: _PreflightDeps) -> None:
        """Pull from origin, escalating to the divergence-resolver agent on textual conflict."""
        from ..infrastructure.worktree import managed_worktree, worktree_identity

        try:
            deps.git_svc.pull_with_merge_fallback(deps.repo_root)
        except UnrelatedHistoriesError:
            if self._try_recover_unrelated_histories(deps):
                return
            raise
        except GitCommandError as pull_exc:
            if "conflict" not in str(pull_exc).lower():
                raise
            branch = deps.git_svc.get_current_branch(deps.repo_root)
            current_sha = deps.git_svc.get_head_sha(deps.repo_root)
            sandbox_identity = worktree_identity(self._DIVERGE_SANDBOX, deps.repo_root)
            try:
                async with managed_worktree(
                    identity=sandbox_identity,
                    sha=current_sha,
                    delete_branch_on_teardown=True,
                    deps=deps,
                ) as sandbox_path:
                    await deps.agent_runner.run(
                        RunRequest(
                            name="Divergence Resolver",
                            template=PromptTemplate.DIVERGENCE_RESOLVE,
                            mount_path=sandbox_path,
                            role=AgentRole.DIVERGENCE_RESOLVER,
                            scope_args=build_divergence_scope_args(branch=branch),
                            service=deps.cfg.merge_override.service,
                            status_display=deps.status_display,
                            work_body="Resolving divergence",
                        )
                    )
                    deps.git_svc.fast_forward_branch(
                        deps.repo_root, branch, self._DIVERGE_SANDBOX
                    )
                    RoleSession(sandbox_path, AgentRole.DIVERGENCE_RESOLVER).discard()
            except Exception:
                raise pull_exc from None


class PreflightCache:
    """Single-slot, process-scoped cache for preflight verdicts.

    Constructed once in orchestrator.run() outside the iteration loop so its slot
    survives iteration reconstruction.  All callers serialise via the internal lock.
    """

    def __init__(self) -> None:
        self._verdict: PreflightResult | None = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._branch_refresh = BranchRefreshBoundary()

    def _resolved_preflight_issue_override(self, deps: _PreflightDeps):
        registry = cast(ServiceRegistry | None, getattr(deps, "service_registry", None))
        override = deps.cfg.preflight_issue_override
        if registry is None:
            return override
        return registry.resolve(override, _time_module.now_local())

    def _preflight_command_failures(
        self,
        failures: Sequence[tuple[str, str, str]],
    ) -> tuple[PreflightCommandFailure, ...]:
        return tuple(
            PreflightCommandFailure(
                check_name=check_name,
                command=command,
                output=output,
            )
            for check_name, command, output in failures
        )

    async def _handle_failure(
        self,
        failures: tuple[OrdinaryCheckFailure, ...],
        deps: _PreflightDeps,
        mount_path: Path,
        sha: str,
    ) -> PreflightHITL | PreflightAFK:
        failure = failures[0].failure
        override = self._resolved_preflight_issue_override(deps)
        agent_result = await deps.agent_runner.run(
            RunRequest(
                name="Pre-Flight Reporter",
                template=PromptTemplate.PREFLIGHT_ISSUE,
                mount_path=mount_path,
                role=AgentRole.PREFLIGHT_ISSUE,
                scope_args=build_preflight_scope_args(
                    check_name=failure.check_name,
                    command=failure.command,
                    output=failure.output,
                ),
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
        validation = validate_issue_report(
            caller="Pre-Flight Reporter",
            issue_output=agent_result,
            cfg=deps.cfg,
            github_svc=deps.github_svc,
        )
        if validation == "hitl":
            return PreflightHITL(sha=sha, issue_number=agent_result.number)
        return PreflightAFK(sha=sha, issue_number=agent_result.number)

    async def pull_with_resolution(self, deps: _PreflightDeps) -> None:
        await self._branch_refresh.pull_with_resolution(deps)

    async def get_safe_sha(self, deps: _PreflightDeps) -> PreflightResult:
        from ..infrastructure.worktree import transient_worktree

        async with self._lock:
            await _wait_for_clean_working_tree(deps, "Preflight")
            try:
                await self._branch_refresh.pull_with_resolution(deps)
            except UnrelatedHistoriesError:
                raise
            except GitCommandError as pull_exc:
                if "conflict" not in str(pull_exc).lower():
                    deps.status_display.print(
                        "Preflight",
                        "git pull failed — remote branch is unreachable or has irreconcilable conflicts. "
                        "Resolve manually and retry.",
                        style="error",
                    )
                raise
            sha = deps.git_svc.get_head_sha(deps.repo_root)

            if self._verdict is not None and self._verdict.sha == sha:
                return self._verdict

            async with transient_worktree(
                "preflight-sandbox", sha=sha, deps=deps
            ) as mount_path:
                failures = await deps.agent_runner.run_preflight(
                    name="Preflight Agent",
                    mount_path=mount_path,
                    stage="PREFLIGHT",
                    status_display=deps.status_display,
                    work_body="Checking",
                )

                result: PreflightResult
                if failures:
                    command_failures = self._preflight_command_failures(failures)
                    analysis = analyze_preflight_command_failures(
                        deps.repo_root, command_failures
                    )
                    if isinstance(analysis, SetupPhaseError):
                        raise analysis
                    ordinary_failures: tuple[OrdinaryCheckFailure, ...] = analysis
                    try:
                        result = await self._handle_failure(
                            ordinary_failures,
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
