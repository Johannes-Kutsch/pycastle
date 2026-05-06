import http.client
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import urllib3.response

from pycastle._shutdown_hook import install_urllib3_shutdown_hook


@pytest.fixture
def restore_unraisablehook():
    saved = sys.unraisablehook
    yield
    sys.unraisablehook = saved


def _make_traceback(filenames: list[str]):
    """Build a chain of frame.tb_next stand-ins each reporting one filename."""
    nxt = None
    for fname in reversed(filenames):
        code = SimpleNamespace(co_filename=fname)
        frame = SimpleNamespace(f_code=code)
        nxt = SimpleNamespace(tb_frame=frame, tb_next=nxt)
    return nxt


def _make_unraisable(
    exc_type: type, exc_value: BaseException, obj: object, files: list[str]
):
    return SimpleNamespace(
        exc_type=exc_type,
        exc_value=exc_value,
        exc_traceback=_make_traceback(files),
        err_msg=None,
        object=obj,
    )


def test_install_returns_prior_hook_and_replaces_it(restore_unraisablehook):
    sentinel = MagicMock()
    sys.unraisablehook = sentinel

    prior = install_urllib3_shutdown_hook()

    assert prior is sentinel
    assert sys.unraisablehook is not sentinel


def test_matching_urllib3_io_error_is_suppressed(restore_unraisablehook, capsys):
    prior = MagicMock()
    sys.unraisablehook = prior
    install_urllib3_shutdown_hook()

    response = urllib3.response.HTTPResponse.__new__(urllib3.response.HTTPResponse)
    event = _make_unraisable(
        ValueError,
        ValueError("I/O operation on closed file."),
        response,
        [urllib3.response.__file__, http.client.__file__],
    )

    sys.unraisablehook(event)

    prior.assert_not_called()
    captured = capsys.readouterr()
    assert captured.err == ""


def test_non_matching_event_delegates_to_prior_hook(restore_unraisablehook):
    prior = MagicMock()
    sys.unraisablehook = prior
    install_urllib3_shutdown_hook()

    event = _make_unraisable(
        RuntimeError,
        RuntimeError("something else"),
        object(),
        ["/some/random/module.py"],
    )

    sys.unraisablehook(event)

    prior.assert_called_once_with(event)


def test_non_urllib3_value_error_delegates_to_prior_hook(restore_unraisablehook):
    prior = MagicMock()
    sys.unraisablehook = prior
    install_urllib3_shutdown_hook()

    event = _make_unraisable(
        ValueError,
        ValueError("I/O operation on closed file."),
        object(),
        [urllib3.response.__file__, http.client.__file__],
    )

    sys.unraisablehook(event)

    prior.assert_called_once_with(event)
