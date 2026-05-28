import asyncio
import dataclasses
import re
from pathlib import Path
from typing import Protocol, TypeAlias, cast

from ..agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    IssueOutput,
)
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..prompts.pipeline import PromptTemplate
from ..services import (
    GitCommandError,
    GitService,
    GithubService,
    ServiceRegistry,
    UnrelatedHistoriesError,
)
from ..errors import DockerError, SetupPhaseError
from ..session import RoleSession
from ..agents.classifier import WellFormed, classify_slice, slice_labels
from ..display.status_display import StatusDisplay
from ..infrastructure.preflight_tool_classifier import (
    MissingDeclaredTool,
    PreflightCommandFailure,
    classify_preflight_tool_failure,
    load_python_dependency_metadata,
)
from ._utils import _wait_for_clean_working_tree, is_well_formed_body
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
    if cfg.hitl_label in issue_output.labels:
        return "hitl"
    result = classify_slice({"labels": list(issue_output.labels)}, cfg)
    if not isinstance(result, WellFormed):
        expected = slice_labels(cfg)
        raise RuntimeError(
            f"{caller} filed issue #{issue_output.number} on the AFK branch "
            f"without exactly one slice-mode label — got labels={issue_output.labels!r}. "
            f"Expected exactly one of {sorted(expected)!r}."
        )
    filed_issue = github_svc.get_issue(issue_output.number)
    if not is_well_formed_body(filed_issue):
        raise RuntimeError(
            f"{caller} filed issue #{issue_output.number} whose body is "
            f"below the minimum length floor — body too short to be valid."
        )
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
        from ..infrastructure.worktree import managed_worktree

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
            try:
                async with managed_worktree(
                    "diverge-sandbox",
                    branch=self._DIVERGE_SANDBOX,
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
                            scope_args={"BRANCH": branch},
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

    def _raise_if_declared_tool_missing(
        self,
        failures: tuple[tuple[str, str, str], ...],
        project_root: Path,
    ) -> None:
        metadata = load_python_dependency_metadata(project_root)
        for check_name, command, output in failures:
            classification = classify_preflight_tool_failure(
                metadata,
                PreflightCommandFailure(
                    check_name=check_name,
                    command=command,
                    output=output,
                ),
            )
            if isinstance(classification, MissingDeclaredTool):
                raise SetupPhaseError(
                    "preflight",
                    "Missing expected preflight tool "
                    f"'{classification.tool}' declared in "
                    f"{classification.dependency_source}.",
                    command=command,
                    output=output,
                )

    async def _handle_failure(
        self,
        failures: tuple[tuple[str, str, str], ...],
        deps: _PreflightDeps,
        mount_path: Path,
        sha: str,
    ) -> PreflightHITL | PreflightAFK:
        check_name, command, output = failures[0]
        override = self._resolved_preflight_issue_override(deps)
        agent_result = await deps.agent_runner.run(
            RunRequest(
                name="Pre-Flight Reporter",
                template=PromptTemplate.PREFLIGHT_ISSUE,
                mount_path=mount_path,
                role=AgentRole.PREFLIGHT_ISSUE,
                scope_args={
                    "CHECK_NAME": check_name,
                    "COMMAND": command,
                    "OUTPUT": output,
                },
                model=override.model,
                effort=override.effort,
                service=override.service,
                status_display=deps.status_display,
                work_body=f"reporting {check_name} issue",
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
                try:
                    failures = await deps.agent_runner.run_preflight(
                        name="Preflight Agent",
                        mount_path=mount_path,
                        stage="PREFLIGHT",
                        status_display=deps.status_display,
                        work_body="Checking",
                    )
                except DockerError as exc:
                    raise SetupPhaseError("preflight", str(exc)) from exc

                result: PreflightResult
                if failures:
                    self._raise_if_declared_tool_missing(
                        tuple(failures), deps.repo_root
                    )
                    try:
                        result = await self._handle_failure(
                            tuple(failures), deps, mount_path, sha
                        )
                    except AgentOutputProtocolError as parse_exc:
                        raise RuntimeError(str(parse_exc)) from parse_exc
                else:
                    result = PreflightReady(sha=sha)

                self._verdict = result
                return result
