"""Process-level suppression of urllib3 GC noise at interpreter shutdown.

docker-py keeps `urllib3.response.HTTPResponse` objects alive past
`DockerSession.__exit__` (held transitively by docker-py internals and
executor thread frames). They are GC'd at interpreter shutdown, after their
underlying socket file has already been closed, producing a benign but ugly
`ValueError: I/O operation on closed file` traceback. Eager cleanup inside
`__exit__` was tried (#487, #496) and proven insufficient; suppressing the
specific event at process scope is the honest fix.
"""

import sys


def install_urllib3_shutdown_hook():
    """Install the suppression hook and return the previously installed hook."""
    import urllib3.response

    prior = sys.unraisablehook

    def _hook(unraisable) -> None:
        exc_type = unraisable.exc_type
        exc_value = unraisable.exc_value
        obj = unraisable.object
        if (
            exc_type is ValueError
            and isinstance(obj, urllib3.response.HTTPResponse)
            and str(exc_value).startswith("I/O operation on closed file")
        ):
            tb = unraisable.exc_traceback
            files: set[str] = set()
            while tb is not None:
                files.add(tb.tb_frame.f_code.co_filename.replace("\\", "/"))
                tb = tb.tb_next
            if any(f.endswith("urllib3/response.py") for f in files) and any(
                f.endswith("http/client.py") for f in files
            ):
                return
        prior(unraisable)

    sys.unraisablehook = _hook
    return prior
