import pytest

from pycastle import execution_contracts
from pycastle.agents import output_protocol


@pytest.mark.parametrize(
    "name",
    [
        "PrepareSessionAdapter",
        "PreparedSession",
        "RunSessionPlan",
        "WorkInvocationRequest",
        "WorkOutputAdapter",
    ],
)
def test_execution_contracts_no_longer_exports_deleted_work_abstraction(
    name: str,
) -> None:
    assert not hasattr(execution_contracts, name)


def test_output_protocol_no_longer_exports_legacy_process_stream() -> None:
    assert not hasattr(output_protocol, "process_stream")
