"""Static-analysis security tests for FishFinder.

These tests NEVER start the server, open a socket, make an HTTP request, or
write a file. They read the application source straight off disk and analyze
it — as plain text and, where it helps, via the `ast` module. The goal is to
assert on code *shape*: that the Flask bind is loopback-only, debug is off,
no secrets are baked into source, expensive routes are rate-limited, and the
various safety caps are set to sane values.

Every test runs unconditionally — no skips, no xfail suppression. A test that
checks for a hardening fix which is not yet implemented is *expected* to FAIL,
and that visible FAILED is how the suite tracks what still needs fixing. The
report is the backlog: PASSED means the code is clean, FAILED means a fix is
still owed.
"""

import ast
import os
import re


# --------------------------------------------------------------------------
# Path helpers. Everything is resolved relative to this file so the suite runs
# regardless of the directory pytest is invoked from. No file is ever written.
# --------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
_APP_PY = os.path.join(_PROJECT_ROOT, "app.py")
_SRC_DIR = os.path.join(_PROJECT_ROOT, "src")
_SPOTFINDER_PY = os.path.join(_SRC_DIR, "spotfinder.py")


def _read(path):
    """Read a source file as UTF-8 text. Read-only; no side effects."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _python_sources_under_src():
    """Yield (path, text) for every .py file under src/, plus app.py."""
    paths = [_APP_PY]
    for dirpath, _dirs, files in os.walk(_SRC_DIR):
        for name in files:
            if name.endswith(".py"):
                paths.append(os.path.join(dirpath, name))
    for p in sorted(set(paths)):
        yield p, _read(p)


def _rel(path):
    """Project-relative path for readable failure messages."""
    return os.path.relpath(path, _PROJECT_ROOT)


# --------------------------------------------------------------------------
# AST helpers — used by the rate-limit and heartbeat checks so we reason about
# real decorator/handler structure rather than fragile line matching.
# --------------------------------------------------------------------------
def _app_tree():
    return ast.parse(_read(_APP_PY), filename=_APP_PY)


def _decorator_route_path(dec):
    """If `dec` is an @app.route(...) / @bp.route(...) call, return its first
    string arg (the URL rule); else None."""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if isinstance(func, ast.Attribute) and func.attr == "route":
        if dec.args and isinstance(dec.args[0], ast.Constant) \
                and isinstance(dec.args[0].value, str):
            return dec.args[0].value
    return None


def _decorator_is_limiter_limit(dec):
    """True for @limiter.limit(...) (call) decorators."""
    if isinstance(dec, ast.Call):
        dec = dec.func
    return isinstance(dec, ast.Attribute) and dec.attr == "limit"


def _route_functions(tree):
    """Yield (func_node, [route_paths], [decorators]) for every function in
    app.py that carries at least one @*.route decorator."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        routes = [p for p in (_decorator_route_path(d) for d in node.decorator_list)
                  if p is not None]
        if routes:
            yield node, routes, node.decorator_list


# ==========================================================================
# Tests
# ==========================================================================

def test_no_debug_mode():
    """app.run / Flask must never be started with debug=True."""
    text = _read(_APP_PY)
    matches = re.findall(r"debug\s*=\s*True", text)
    assert not matches, (
        f"found debug=True in {_rel(_APP_PY)} "
        f"({len(matches)} occurrence(s)); debug mode exposes the Werkzeug "
        "console and must stay off."
    )


def test_no_external_bind():
    """The server must not bind to 0.0.0.0 (all interfaces)."""
    text = _read(_APP_PY)
    matches = re.findall(r"""host\s*=\s*['"]0\.0\.0\.0['"]""", text)
    assert not matches, (
        f"found host='0.0.0.0' in {_rel(_APP_PY)} "
        f"({len(matches)} occurrence(s)); the server must bind to loopback "
        "(127.0.0.1) only."
    )


def test_no_hardcoded_secrets():
    """No secret_key / password / api_key literals or long hex blobs assigned
    to a variable anywhere under src/ or in app.py."""
    # Assignment of a non-empty string literal to a secret-ish variable name.
    secret_assign = re.compile(
        r"""(?i)\b(secret_key|secretkey|password|passwd|pwd|"""
        r"""api_key|apikey|access_token|auth_token|private_key)\b"""
        r"""\s*=\s*['"][^'"]+['"]"""
    )
    # Any hex string longer than 20 chars assigned to a variable, e.g.
    #   key = "a3f9c2...."  (a baked-in token/digest).
    hex_assign = re.compile(r"""=\s*['"][0-9a-fA-F]{21,}['"]""")

    findings = []
    for path, text in _python_sources_under_src():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if secret_assign.search(line) or hex_assign.search(line):
                findings.append(f"{_rel(path)}:{lineno}: {line.strip()}")

    assert not findings, (
        "hardcoded secret(s) found:\n" + "\n".join(findings)
    )


def test_secret_key_from_environment():
    """Any SECRET_KEY the app configures must be read from the environment
    (os.environ / os.getenv), never a hardcoded literal.

    Currently app.py configures no SECRET_KEY at all, so this asserts that a
    SECRET_KEY is set AND sourced from the environment — expected to fail
    until that hardening lands."""
    text = _read(_APP_PY)
    secret_lines = [
        (i, ln) for i, ln in enumerate(text.splitlines(), start=1)
        if "SECRET_KEY" in ln or "secret_key" in ln
    ]
    assert secret_lines, (
        f"no SECRET_KEY configured in {_rel(_APP_PY)}; Flask sessions/signing "
        "should set one from os.environ/os.getenv."
    )
    env_re = re.compile(r"os\.environ|os\.getenv")
    bad = [f"{_rel(_APP_PY)}:{i}: {ln.strip()}"
           for i, ln in secret_lines if not env_re.search(ln)]
    assert not bad, (
        "SECRET_KEY is not read from the environment:\n" + "\n".join(bad)
    )


def test_no_print_statements():
    """Production code must not contain bare print( calls (use logging).

    Scans app.py and every .py under src/. Currently fails because app.py
    still has a couple of print()s in the spotfinder stream guard and the
    __main__ boot banner."""
    findings = []
    print_re = re.compile(r"(?<![\w.])print\s*\(")
    for path, text in _python_sources_under_src():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if print_re.search(line):
                findings.append(f"{_rel(path)}:{lineno}: {line.strip()}")
    assert not findings, (
        f"found {len(findings)} bare print( call(s) in production code:\n"
        + "\n".join(findings)
    )


def test_rate_limit_decorators_on_expensive_routes():
    """/raster/..., /raster/inspect, and /spotfinder/run must each carry a
    @limiter.limit decorator on their handler."""
    tree = _app_tree()
    # Map each expensive route to whether its handler has @limiter.limit.
    # We match the tile-raster route by prefix since its rule is templated
    # ("/raster/<string:source>/..."), while the other two are exact.
    targets = {
        "/raster/ (tile proxy)": False,
        "/raster/inspect": False,
        "/spotfinder/run": False,
    }
    for node, routes, decorators in _route_functions(tree):
        has_limit = any(_decorator_is_limiter_limit(d) for d in decorators)
        for rule in routes:
            if rule == "/raster/inspect":
                targets["/raster/inspect"] = has_limit
            elif rule == "/spotfinder/run":
                targets["/spotfinder/run"] = has_limit
            elif rule.startswith("/raster/") and rule != "/raster/inspect":
                targets["/raster/ (tile proxy)"] = has_limit

    missing = [name for name, ok in targets.items() if not ok]
    assert not missing, (
        f"in {_rel(_APP_PY)}, these expensive route(s) are missing a "
        f"@limiter.limit decorator: {', '.join(missing)}"
    )


def test_heartbeat_gated_in_prod():
    """The /heartbeat handler must be guarded by an autoshutdown env flag
    (FISHFINDER_AUTOSHUTDOWN, surfaced as the _AUTOSHUTDOWN check)."""
    tree = _app_tree()
    handler = None
    for node, routes, _dec in _route_functions(tree):
        if "/heartbeat" in routes:
            handler = node
            break
    assert handler is not None, (
        f"no /heartbeat route found in {_rel(_APP_PY)}"
    )
    body_src = ast.get_source_segment(_read(_APP_PY), handler) or ""
    assert ("FISHFINDER_AUTOSHUTDOWN" in body_src or "_AUTOSHUTDOWN" in body_src), (
        f"the /heartbeat handler in {_rel(_APP_PY)} has no autoshutdown "
        "env-variable guard (expected FISHFINDER_AUTOSHUTDOWN / _AUTOSHUTDOWN "
        "check); an ungated heartbeat could be driven by any client."
    )


def test_no_wildcard_cors():
    """app.py must not emit a wildcard CORS allow-origin header."""
    text = _read(_APP_PY)
    matches = re.findall(r"Access-Control-Allow-Origin['\"]?\s*[:,]\s*['\"]?\*", text)
    # Also catch the bare header string with a literal star anywhere.
    if "Access-Control-Allow-Origin" in text and "*" in text:
        star_lines = [
            ln.strip() for ln in text.splitlines()
            if "Access-Control-Allow-Origin" in ln and "*" in ln
        ]
    else:
        star_lines = []
    assert not matches and not star_lines, (
        f"wildcard CORS header found in {_rel(_APP_PY)}:\n"
        + "\n".join(star_lines or matches)
    )


def test_resolution_cap_is_sane():
    """_MAX_RASTER_RES must be 512 or lower to bound per-tile fetch size.

    Currently 2048, so this is expected to fail until the cap is tightened."""
    tree = _app_tree()
    value = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "_MAX_RASTER_RES":
                    if isinstance(node.value, ast.Constant):
                        value = node.value.value
    assert value is not None, (
        f"_MAX_RASTER_RES not found as a module-level constant in {_rel(_APP_PY)}"
    )
    assert value <= 512, (
        f"_MAX_RASTER_RES is {value} in {_rel(_APP_PY)}; must be 512 or lower "
        "to bound raster fetch dimensions."
    )


def test_max_total_cells_is_bounded():
    """MAX_TOTAL_CELLS must be 10,000,000 or lower to bound Spotfinder memory.

    Currently 50,000,000, so this is expected to fail until the cap is
    tightened."""
    tree = ast.parse(_read(_SPOTFINDER_PY), filename=_SPOTFINDER_PY)
    value = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "MAX_TOTAL_CELLS":
                    if isinstance(node.value, ast.Constant):
                        value = node.value.value
    assert value is not None, (
        f"MAX_TOTAL_CELLS not found as a module-level constant in "
        f"{_rel(_SPOTFINDER_PY)}"
    )
    assert value <= 10_000_000, (
        f"MAX_TOTAL_CELLS is {value} in {_rel(_SPOTFINDER_PY)}; must be "
        "10,000,000 or lower to bound Spotfinder working-set memory."
    )
