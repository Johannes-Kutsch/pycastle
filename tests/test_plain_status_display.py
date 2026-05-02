from pycastle.status_display import PlainStatusDisplay, StatusDisplay


def test_plain_status_display_satisfies_protocol() -> None:
    assert isinstance(PlainStatusDisplay(), StatusDisplay)
