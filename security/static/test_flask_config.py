"""Flask security-configuration checks (read app.py as text + AST).

WHAT THIS CHECKS
    The Flask hardening knobs the OWASP/Flask security guidance calls for:
      - DEBUG can't be turned on through any code path (run kwarg, app.debug,
        app.config['DEBUG'], or a FLASK_DEBUG/ENV literal).
      - A SECRET_KEY, if configured at all, is not a weak/default literal.
      - SESSION_COOKIE_SECURE / _HTTPONLY / _SAMESITE are explicitly set to
        safe values.

WHY IT MATTERS
    `debug=True` exposes the interactive Werkzeug debugger — remote code
    execution to anyone who can reach a traceback. The session-cookie flags
    govern whether Flask's signed session cookie can be stolen over plain HTTP
    (Secure), read by injected JavaScript (HttpOnly), or sent cross-site
    (SameSite). Flask's defaults are *insecure* for two of the three
    (SESSION_COOKIE_SECURE defaults False; SAMESITE defaults None), so the
    safe values must be set explicitly.

    FishFinder does not currently use server-side sessions, so several of
    these are forward-looking hardening rather than an active hole — but the
    suite's contract is "FAILED == a fix is still owed", and setting these
    defensively means any future `session[...]` use is secure-by-default
    instead of silently shipping insecure cookies. Each failure below is a
    concrete one-line config add in app.py.
"""

import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sources as S  # noqa: E402


def _app_text():
    return S.read_text(S.APP_PY)


def _config_string_values(key):
    """Return every literal value assigned to app.config['<key>'] (or
    .setdefault) in app.py, as a list of (lineno, repr-of-value).

    Uses both AST (for app.config['X'] = <const>) and a regex sweep (for
    app.config.update(X=...) styles) so we don't miss an assignment form.
    """
    text = _app_text()
    values = []

    tree = ast.parse(text, filename=S.APP_PY)
    for node in ast.walk(tree):
        # app.config['KEY'] = <value>
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Attribute)
                        and tgt.value.attr == "config"):
                    sub = tgt.slice
                    if isinstance(sub, ast.Constant) and sub.value == key:
                        if isinstance(node.value, ast.Constant):
                            values.append((node.lineno, node.value.value))
                        else:
                            values.append((node.lineno, "<non-literal>"))
        # app.config.update(KEY=<value>) / app.config.update({'KEY': ...})
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "update"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "config"):
                for kw in node.keywords:
                    if kw.arg == key and isinstance(kw.value, ast.Constant):
                        values.append((node.lineno, kw.value.value))
    return values


def test_debug_not_enabled_anywhere():
    """No code path can start the server in debug mode.

    Covers the run kwarg (`debug=True`), the attribute form (`app.debug =
    True`), the config form (`app.config['DEBUG'] = True`), and a truthy
    FLASK_DEBUG / ENV='development' literal. The interactive debugger is RCE
    for anyone who can trigger a traceback.
    """
    text = _app_text()
    bad = []
    patterns = [
        (r"debug\s*=\s*True", "debug=True kwarg/attr"),
        (r"""\.config\[['\"]DEBUG['\"]\]\s*=\s*True""", "config['DEBUG']=True"),
        (r"""(?i)FLASK_DEBUG['\"]?\s*[:=]\s*['\"]?1""", "FLASK_DEBUG=1"),
        (r"""(?i)\.config\[['\"]ENV['\"]\]\s*=\s*['\"]development""", "ENV=development"),
    ]
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rx, label in patterns:
            if re.search(rx, line):
                bad.append(f"{S._rel(S.APP_PY)}:{lineno}: [{label}] {line.strip()}")
    assert not bad, "debug mode reachable:\n" + "\n".join(bad)


# A non-exhaustive set of values that must never be used as a production
# signing key. Empty string and obvious placeholders included.
_WEAK_SECRET_VALUES = {
    "", "dev", "development", "secret", "secret_key", "secretkey",
    "changeme", "change-me", "password", "test", "testing", "key",
    "flask", "supersecret", "your-secret-key", "todo", "xxx", "123456",
}


def test_secret_key_not_weak_value():
    """If a SECRET_KEY is configured as a literal, it must not be a known
    weak/default value.

    A predictable signing key lets an attacker forge any signed cookie or
    token. (When the value is read from the environment instead of a literal,
    this test passes — environment sourcing is verified separately in
    test_static.test_secret_key_from_environment.)
    """
    values = _config_string_values("SECRET_KEY")
    # Also catch the attribute form `app.secret_key = "..."`.
    for lineno, line in enumerate(_app_text().splitlines(), start=1):
        m = re.search(r"""secret_key\s*=\s*['"]([^'"]*)['"]""", line, re.I)
        if m:
            values.append((lineno, m.group(1)))

    weak = [
        f"{S._rel(S.APP_PY)}:{lineno}: SECRET_KEY = {val!r}"
        for lineno, val in values
        if isinstance(val, str) and val.strip().lower() in _WEAK_SECRET_VALUES
    ]
    assert not weak, (
        "weak/default SECRET_KEY literal(s) configured:\n" + "\n".join(weak)
    )


def test_session_cookie_secure_set():
    """SESSION_COOKIE_SECURE must be explicitly True.

    Flask defaults this to False, so the signed session cookie would be sent
    over plain HTTP and is trivially sniffable. Setting it True restricts the
    cookie to HTTPS.
    """
    values = _config_string_values("SESSION_COOKIE_SECURE")
    assert any(v is True for _ln, v in values), (
        f"SESSION_COOKIE_SECURE is not set to True in {S._rel(S.APP_PY)}; "
        "Flask defaults it to False (cookie sent over plain HTTP). Add "
        "app.config['SESSION_COOKIE_SECURE'] = True."
    )


def test_session_cookie_httponly_set():
    """SESSION_COOKIE_HTTPONLY must be set True so injected JavaScript cannot
    read the session cookie via document.cookie.

    Flask's default is already True, so this guards against a regression that
    turns it off — and makes the intent explicit in source.
    """
    values = _config_string_values("SESSION_COOKIE_HTTPONLY")
    # A regression would be an explicit False; absence keeps Flask's safe
    # default, but the suite's contract wants this set explicitly.
    assert values and all(v is not False for _ln, v in values) \
        and any(v is True for _ln, v in values), (
        f"SESSION_COOKIE_HTTPONLY is not explicitly True in {S._rel(S.APP_PY)}; "
        "set app.config['SESSION_COOKIE_HTTPONLY'] = True so an XSS payload "
        "can't read the session cookie."
    )


def test_session_cookie_samesite_set():
    """SESSION_COOKIE_SAMESITE must be set to 'Lax' or 'Strict'.

    Flask defaults SameSite to None, which permits the cookie on cross-site
    requests and leaves a CSRF surface. 'Lax' (or 'Strict') closes it.
    """
    values = _config_string_values("SESSION_COOKIE_SAMESITE")
    ok = any(isinstance(v, str) and v.lower() in ("lax", "strict")
             for _ln, v in values)
    assert ok, (
        f"SESSION_COOKIE_SAMESITE is not set to 'Lax'/'Strict' in "
        f"{S._rel(S.APP_PY)}; Flask defaults it to None (CSRF surface). Add "
        "app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'."
    )
