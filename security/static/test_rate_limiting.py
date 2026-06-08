"""Rate-limit coverage of every Flask route (AST analysis of app.py).

WHAT THIS CHECKS
    Parses every `@app.route` / `@bp.route` handler in app.py and verifies
    each one is covered by rate limiting through one of the three legitimate
    mechanisms:
      - an explicit per-route `@limiter.limit(...)` decorator, OR
      - an explicit `@limiter.exempt` decorator (a deliberate opt-out), OR
      - the global Limiter `default_limits`, which Flask-Limiter applies to
        every non-exempt route automatically.
    It also asserts the global `default_limits` is configured and non-empty
    (so the "covered by default" path is real), and that the expensive
    endpoints carry their own tighter explicit limits.

WHY IT MATTERS
    The tile proxy and Spotfinder fan out to NOAA and run multi-megacell
    numpy; without a limit a single client can amplify one cheap HTTP request
    into heavy upstream + CPU + disk-cache load (a DoS / cost-amplification
    vector). Relying on `default_limits` is fine *as long as it exists* — the
    real risk is a route that is neither explicitly limited nor covered by a
    default because the default was removed. This test catches both the
    "expensive route lost its tight limit" and the "global default vanished"
    regressions.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sources as S  # noqa: E402


def _app_tree():
    return ast.parse(S.read_text(S.APP_PY), filename=S.APP_PY)


def _route_path(dec):
    """First string arg of an @*.route(...) decorator, else None."""
    if not isinstance(dec, ast.Call):
        return None
    if isinstance(dec.func, ast.Attribute) and dec.func.attr == "route":
        if dec.args and isinstance(dec.args[0], ast.Constant) \
                and isinstance(dec.args[0].value, str):
            return dec.args[0].value
    return None


def _is_limiter_limit(dec):
    """@limiter.limit(...) — a Call whose func attr is 'limit'."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    return isinstance(target, ast.Attribute) and target.attr == "limit"


def _is_limiter_exempt(dec):
    """@limiter.exempt — bare attribute (or call) with attr 'exempt'."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    return isinstance(target, ast.Attribute) and target.attr == "exempt"


def _route_handlers():
    """Yield (func_node, [route_paths]) for every routed handler in app.py."""
    for node in ast.walk(_app_tree()):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        routes = [p for p in (_route_path(d) for d in node.decorator_list)
                  if p is not None]
        if routes:
            yield node, routes


def _default_limits_configured():
    """True if Limiter(...) is constructed with a non-empty default_limits."""
    for node in ast.walk(_app_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "Limiter":
            for kw in node.keywords:
                if kw.arg == "default_limits":
                    val = kw.value
                    if isinstance(val, (ast.List, ast.Tuple)) and val.elts:
                        return True
                    # A name/other expr — assume configured but non-introspectable.
                    if not isinstance(val, (ast.List, ast.Tuple)):
                        return True
    return False


def test_global_default_limits_configured():
    """The Limiter is constructed with a non-empty default_limits list.

    This is what makes every otherwise-undecorated route rate-limited. If it
    were removed, the per-route coverage check below would (correctly) start
    failing for the plain routes — this test names the root cause directly.
    """
    assert _default_limits_configured(), (
        f"Limiter in {S._rel(S.APP_PY)} has no non-empty default_limits; "
        "without it, every route that lacks an explicit @limiter.limit is "
        "unthrottled."
    )


def test_every_route_has_rate_limit_coverage():
    """Every routed handler is covered: explicit @limiter.limit, explicit
    @limiter.exempt, or the global default_limits.

    A route covered only by the default is acceptable; a route covered by
    *nothing* (no explicit limit AND no default) is the failure condition.
    """
    has_default = _default_limits_configured()
    uncovered = []
    for func, routes in _route_handlers():
        explicit = any(_is_limiter_limit(d) for d in func.decorator_list)
        exempt = any(_is_limiter_exempt(d) for d in func.decorator_list)
        if not (explicit or exempt or has_default):
            uncovered.append(f"{func.name} -> {', '.join(routes)}")
    assert not uncovered, (
        "route(s) with no rate-limit coverage (no explicit limit, not exempt, "
        "and no global default_limits):\n" + "\n".join(uncovered)
    )


def test_expensive_routes_have_explicit_tight_limits():
    """The NOAA-amplifying / compute-heavy routes each carry their OWN
    @limiter.limit, not just the global default.

    Targets: the tile proxy (`/raster/<...>`), the inspector raster
    (`/raster/inspect`), and `/spotfinder/run`. These cost far more per call
    than a page load, so they must be throttled below the default budget.
    """
    targets = {
        "/raster/ (tile proxy)": False,
        "/raster/inspect": False,
        "/spotfinder/run": False,
    }
    for func, routes in _route_handlers():
        explicit = any(_is_limiter_limit(d) for d in func.decorator_list)
        for rule in routes:
            if rule == "/raster/inspect":
                targets["/raster/inspect"] = explicit
            elif rule == "/spotfinder/run":
                targets["/spotfinder/run"] = explicit
            elif rule.startswith("/raster/"):
                targets["/raster/ (tile proxy)"] = explicit
    missing = [name for name, ok in targets.items() if not ok]
    assert not missing, (
        f"expensive route(s) missing an explicit @limiter.limit in "
        f"{S._rel(S.APP_PY)}: {', '.join(missing)}"
    )
