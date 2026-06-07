"""Shared pytest fixtures for the FishFinder security suite.

The headline fixture here boots the real Flask app on a random free port
bound to 127.0.0.1 and tears it down at the end of the session, so the
integration and load layers can probe it as a black box over HTTP.

SAFETY INVARIANT
----------------
The target host is hard-coded to 127.0.0.1 and asserted at import time
(`_assert_loopback_only`). It is deliberately NOT configurable via env var,
CLI flag, or fixture parameter. The load layer can generate genuinely
abusive traffic; making the target host configurable would let a stray
config point that traffic at an external host. The only legal target for
this suite is the loopback interface on this machine.
"""

import os
import socket
import sys
import threading
import time
from contextlib import closing

import pytest

# --------------------------------------------------------------------------
# Hard safety gate: the suite may only ever target loopback.
# --------------------------------------------------------------------------
TARGET_HOST = "127.0.0.1"  # not configurable, by design — see module docstring.


def _assert_loopback_only(host):
    """Fail loudly if anything tries to point the suite off-loopback."""
    assert host == "127.0.0.1", (
        f"security suite target host must be 127.0.0.1, got {host!r}. "
        "This is a hard safety invariant — load tests can generate abusive "
        "traffic and must never reach an external host."
    )


# Enforced once at collection time, before any server starts or any test runs.
_assert_loopback_only(TARGET_HOST)

# Make the project root importable so `import app` resolves regardless of the
# directory pytest is invoked from.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _free_port():
    """Ask the OS for an unused TCP port on loopback, then release it.

    We bind to 127.0.0.1 (never 0.0.0.0) so the probe socket — and the
    server that follows — are reachable only from this machine.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind((TARGET_HOST, 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _wait_until_up(host, port, timeout=15.0):
    """Block until the server accepts a TCP connection, or time out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.1)
    return False


@pytest.fixture(scope="session")
def base_url():
    """Boot the real Flask app on a random loopback port for the session.

    Yields the base URL (e.g. http://127.0.0.1:54321). The server runs in a
    daemon thread via Werkzeug's `make_server`, which gives us a clean
    `shutdown()` for teardown (unlike `app.run`, which has no programmatic
    stop). Disk side effects (the NOAA raster cache) are NOT relocated here;
    integration tests must avoid endpoints that write to disk, and the load
    layer documents the disk risk explicitly.
    """
    _assert_loopback_only(TARGET_HOST)

    from werkzeug.serving import make_server

    # Import lazily so a static-only run never imports the app or its deps.
    import app as fishfinder_app

    port = _free_port()
    server = make_server(TARGET_HOST, port, fishfinder_app.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    if not _wait_until_up(TARGET_HOST, port):
        server.shutdown()
        thread.join(timeout=5)
        pytest.fail(f"Flask test server did not come up on {TARGET_HOST}:{port}")

    url = f"http://{TARGET_HOST}:{port}"
    try:
        yield url
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def app_instance():
    """The raw Flask `app` object, for tests that prefer Werkzeug's built-in
    test client over a live socket (faster, no port, no threading)."""
    import app as fishfinder_app
    return fishfinder_app.app


@pytest.fixture()
def client(app_instance):
    """A Flask test client. Convenient for black-box request/response checks
    that don't need a real listening socket."""
    app_instance.config.update(TESTING=True)
    with app_instance.test_client() as c:
        yield c
