"""Input-validation coverage on request-reading routes (AST analysis).

WHAT THIS CHECKS
    For every Flask handler in app.py that reads untrusted request data
    (`request.args`, `request.values`, `request.form`, `request.json`,
    `request.get_json(...)`, or `request.data`), the handler's own source must
    show validation/clamping before that data flows onward: a `_validate_*` /
    `_valid_*` helper call, an explicit `400` response, a try/except around the
    numeric coercion, or a bounds/range comparison.

    It also asserts the shared validation helpers (`_validate_tile_request`,
    `_validate_search_area`, and the `_valid_lat`/`_valid_lng`/`_finite`
    primitives) still exist, since the per-route checks lean on them.

WHY IT MATTERS
    Every value crossing the network boundary feeds bbox math, an NOAA fetch
    grid dimension, or a numpy allocation. Unvalidated, a NaN/inf coordinate
    surfaces as a 500 deep in numpy, and an oversized `size`/`resolution`
    becomes an unbounded fetch/allocation (memory-amplification DoS). Catching
    a handler that reads `request.*` without a nearby guard flags exactly the
    spot where that boundary check could be missing.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sources as S  # noqa: E402


_REQUEST_READS = (
    "request.args", "request.values", "request.form",
    "request.json", "request.get_json", "request.data",
)

# Signals that a handler validates/clamps what it read.
_VALIDATION_SIGNALS = (
    "_validate_", "_valid_lat", "_valid_lng", "_finite",
    ", 400", ",400", "status=400",
    "try:", "except",          # guarded numeric coercion
    "out of range", "invalid",  # explicit error messaging
)


def _app_src():
    return S.read_text(S.APP_PY)


def _route_handler_sources():
    """Yield (func_name, [routes], source_segment) for each routed handler."""
    src = _app_src()
    tree = ast.parse(src, filename=S.APP_PY)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        routes = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                    and dec.func.attr == "route" and dec.args \
                    and isinstance(dec.args[0], ast.Constant):
                routes.append(dec.args[0].value)
        if routes:
            yield node.name, routes, (ast.get_source_segment(src, node) or "")


def test_request_reading_routes_validate_input():
    """Every handler that reads request.args/json/form/etc. contains a visible
    validation or clamping signal in its own body.

    Handlers that take only URL-converter path params (already type/range
    constrained by Flask's `<int:>`) and don't touch `request.*` are out of
    scope — they're validated by the converter plus their explicit bounds
    helper, checked elsewhere.
    """
    offenders = []
    checked = 0
    for name, routes, body in _route_handler_sources():
        if not any(tok in body for tok in _REQUEST_READS):
            continue
        checked += 1
        if not any(sig in body for sig in _VALIDATION_SIGNALS):
            offenders.append(f"{name} -> {', '.join(routes)}")
    # Guard against the introspection silently matching nothing (e.g. a refactor
    # that renames request access) — at least the inspector + spotfinder routes
    # read request data today.
    assert checked >= 1, (
        "no request-reading handlers detected — the AST scan may be stale "
        "against app.py's current request API usage."
    )
    assert not offenders, (
        "request-reading handler(s) with no visible input validation:\n"
        + "\n".join(offenders)
    )


def test_validation_helpers_exist():
    """The shared validation helpers the routes depend on are defined in app.py.

    If one is removed/renamed, the per-route validation above can pass on a
    stale string match while the actual guard is gone, so we assert the
    helpers are really present as functions.
    """
    tree = ast.parse(_app_src(), filename=S.APP_PY)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    required = {"_validate_tile_request", "_validate_search_area",
                "_valid_lat", "_valid_lng", "_finite"}
    missing = sorted(required - defined)
    assert not missing, (
        f"expected validation helper(s) missing from {S._rel(S.APP_PY)}: "
        + ", ".join(missing)
    )


def test_content_length_cap_configured():
    """MAX_CONTENT_LENGTH is set so oversized POST bodies are rejected before
    being buffered into memory.

    `/spotfinder/run` is the only body-accepting route; a missing cap lets a
    multi-megabyte payload be read in full — a cheap memory-amplification.
    """
    src = _app_src()
    assert "MAX_CONTENT_LENGTH" in src, (
        f"MAX_CONTENT_LENGTH not configured in {S._rel(S.APP_PY)}; request "
        "bodies are unbounded."
    )
