"""Black-box HTTP integration security tests.

These exercise the live application over HTTP through the `client` fixture
(see security/conftest.py), which boots Flask on a random localhost port.
They probe real request/response behavior: input validation, rate-limit
headers, security headers, error handling, endpoint exposure.

Run only with the --integration flag via run_security.py. They are skipped
by default so the always-on static layer stays fast and dependency-free.
"""
