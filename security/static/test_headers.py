"""Security-response-header checks (static read of app.py).

WHAT THIS CHECKS
    The hardening headers the viewer relies on are present in the
    `@app.after_request` handler (`_security_headers`) and the CSP builder
    (`_content_security_policy`), and that no header is set to an unsafe
    wildcard/permissive value:

      - Content-Security-Policy is emitted, names a real default-src, locks
        object-src to 'none', and does NOT contain 'unsafe-eval' or a bare
        wildcard default.
      - X-Content-Type-Options: nosniff
      - X-Frame-Options: a non-wildcard value (DENY/SAMEORIGIN)
      - Referrer-Policy and Cross-Origin-Opener-Policy are set.
      - No wildcard Access-Control-Allow-Origin.

WHY IT MATTERS
    These headers are the browser-enforced backstop against the classes of
    attack the server can't see: CSP contains injected-script execution and
    data exfiltration; X-Content-Type-Options stops MIME-confusion script
    execution; X-Frame-Options/frame-ancestors stop clickjacking; a wildcard
    CORS origin would let any site read authenticated responses. They are set
    once, globally, so a single missing line silently drops protection for
    every route — exactly the kind of regression a static check catches.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sources as S  # noqa: E402


def _app_text():
    return S.read_text(S.APP_PY)


def test_csp_header_present_and_named():
    """A Content-Security-Policy header is attached to responses and the
    policy declares an explicit default-src (not a wildcard)."""
    text = _app_text()
    assert "Content-Security-Policy" in text, (
        f"no Content-Security-Policy header set in {S._rel(S.APP_PY)}"
    )
    assert "default-src" in text, (
        "CSP does not declare a default-src directive; without it the policy "
        "has no fallback and most fetch directives are unrestricted."
    )
    assert "default-src '*'" not in text and "default-src *" not in text, (
        "CSP default-src is a wildcard — that disables the policy's value."
    )


def test_csp_forbids_unsafe_eval_and_object_src():
    """The CSP must not allow 'unsafe-eval' and must lock object-src to 'none'.

    'unsafe-eval' re-enables string-to-code execution (the sink CSP exists to
    cut off); object-src 'none' kills the legacy <object>/<embed> plugin
    vector. Both are present/absent by policy in `_content_security_policy`.
    """
    text = _app_text()
    assert "unsafe-eval" not in text, (
        "CSP contains 'unsafe-eval' — this defeats the policy's protection "
        "against injected code execution."
    )
    assert "object-src 'none'" in text, (
        "CSP does not set object-src 'none'; plugin/embedded-object vectors "
        "remain open."
    )


def test_x_content_type_options_nosniff():
    """X-Content-Type-Options: nosniff is set on every response so the browser
    won't MIME-sniff a response into an executable type."""
    text = _app_text()
    assert "X-Content-Type-Options" in text and "nosniff" in text, (
        f"X-Content-Type-Options: nosniff not set in {S._rel(S.APP_PY)}"
    )


def test_x_frame_options_not_wildcard():
    """X-Frame-Options is set to a real anti-framing value (DENY/SAMEORIGIN),
    not ALLOWALL — clickjacking defense."""
    text = _app_text()
    assert "X-Frame-Options" in text, (
        f"X-Frame-Options not set in {S._rel(S.APP_PY)}"
    )
    assert "DENY" in text or "SAMEORIGIN" in text, (
        "X-Frame-Options is present but not DENY/SAMEORIGIN; it must restrict "
        "framing to defend against clickjacking."
    )
    assert "ALLOWALL" not in text, (
        "X-Frame-Options: ALLOWALL permits framing by any origin — clickjackable."
    )


def test_referrer_and_coop_headers_present():
    """Referrer-Policy and Cross-Origin-Opener-Policy are set.

    Referrer-Policy stops leaking full URLs (which can carry coordinates or
    tokens) to third-party origins; COOP isolates the browsing context so a
    cross-origin opener can't reach into the window.
    """
    text = _app_text()
    missing = [h for h in ("Referrer-Policy", "Cross-Origin-Opener-Policy")
               if h not in text]
    assert not missing, (
        f"missing security header(s) in {S._rel(S.APP_PY)}: "
        + ", ".join(missing)
    )


def test_no_wildcard_cors_origin():
    """No response sets Access-Control-Allow-Origin to '*'.

    A wildcard ACAO would let any web origin read this server's responses. The
    app proxies NOAA same-origin and needs no cross-origin sharing at all.
    """
    text = _app_text()
    offenders = [
        ln.strip() for ln in text.splitlines()
        if "Access-Control-Allow-Origin" in ln and "*" in ln
    ]
    assert not offenders, (
        "wildcard CORS Access-Control-Allow-Origin found:\n"
        + "\n".join(offenders)
    )
