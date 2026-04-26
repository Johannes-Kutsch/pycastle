class PycastleError(RuntimeError):
    pass


class DockerError(PycastleError):
    pass


class DockerTimeoutError(DockerError, TimeoutError):
    pass


class WorktreeError(PycastleError):
    pass


class WorktreeTimeoutError(WorktreeError, TimeoutError):
    pass


class BranchCollisionError(WorktreeError):
    pass


class AgentTimeoutError(PycastleError, TimeoutError):
    pass


class PreflightError(PycastleError):
    def __init__(self, failures: list[tuple[str, str, str]]):
        self.failures = failures
        super().__init__(f"Pre-flight failed: {failures}")
