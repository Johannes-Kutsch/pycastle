import dataclasses

from ..display.status_display import StatusDisplay
from .implement import branch_for


@dataclasses.dataclass
class MergeProgressReporter:
    status_display: StatusDisplay
    completed_total: int
    merge_done: int
    close_done: int = 0
    remove_done: int | None = None

    def render(self) -> None:
        message = f"merging {self.merge_done}/{self.completed_total} branches"
        if self.close_done or self.remove_done is not None:
            message = (
                f"{message}, closing {self.close_done}/{self.completed_total} issues"
            )
        if self.remove_done is not None:
            message = f"{message}, removing {self.remove_done}/{self.completed_total} worktrees"
        self.status_display.update_phase("Merge", message)

    def update_merge_done(self, merge_done: int) -> None:
        self.merge_done = merge_done
        self.render()

    def update_close_done(self, close_done: int) -> None:
        self.close_done = close_done
        self.render()

    def update_remove_done(self, remove_done: int | None) -> None:
        self.remove_done = remove_done
        self.render()


def build_merge_close_message(
    deleted: list[str],
    *,
    completed_conflicts: list[dict] | None = None,
    pending_conflicts: list[dict] | None = None,
) -> str:
    if not deleted:
        message = "Execution complete, 0 branch(es) merged and deleted"
    else:
        header = f"Execution complete, {len(deleted)} branch(es) merged and deleted:"
        lines = "\n".join(f"  Deleted merged branch: {b}" for b in deleted)
        message = f"{header}\n{lines}"

    completed_conflicts = completed_conflicts or []
    pending_conflicts = pending_conflicts or []
    if completed_conflicts:
        completed_lines = "\n".join(
            f"  Completed conflict branch: {branch_for(issue['number'])}"
            for issue in completed_conflicts
        )
        message = f"{message}\nCompleted conflict branches:\n{completed_lines}"
    if pending_conflicts:
        pending_lines = "\n".join(
            f"  Pending conflict branch: {branch_for(issue['number'])}"
            for issue in pending_conflicts
        )
        message = f"{message}\nPending conflict branches:\n{pending_lines}"
    return message
