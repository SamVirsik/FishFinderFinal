"""Static secret-scanning across the whole application source tree.

WHAT THIS CHECKS
    No credential material is baked into any shipped source file — Python,
    JavaScript, HTML templates, JSON/YAML/TOML/INI config, or `.env*`. Two
    complementary passes:

      1. Provider/format signatures (AWS keys, Google API keys, GitHub/Slack/
         Stripe tokens, JWTs, PEM private-key blocks, `user:pass@host`
         connection strings). Run over every text file type, because these
         formats are unambiguous wherever they appear.

      2. A "secret-named variable assigned a string literal" heuristic
         (`api_key = "..."`, `password = '...'`). Run only over code (.py/.js),
         where an assignment is meaningful — HTML attributes like
         `name="api_key"` are not credentials and would false-positive.

WHY IT MATTERS
    A committed key is the highest-severity, lowest-effort finding in any
    audit: it leaks the moment the repo is shared, survives `git rm` in
    history, and grants whatever the key grants. FishFinder proxies only
    public NOAA endpoints (no auth), so the *correct* state is zero secrets —
    any hit is either a real leak or a basemap/API key that must move to the
    environment.

SCOPE
    The security suite's own files and the `.claude/` agent harness are
    excluded (see `_sources._EXCLUDED_DIRS`): the former contains these very
    regexes, the latter is developer-local command history, not app config.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sources as S  # noqa: E402


# --------------------------------------------------------------------------
# Pass 1 — universal provider/format signatures. Conservative patterns chosen
# to avoid matching the public NOAA URLs and uuid/hex constants that legitimately
# live in this codebase.
# --------------------------------------------------------------------------
_SIGNATURE_PATTERNS = {
    "AWS access key id":        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "AWS secret access key":    re.compile(r"""(?i)aws_secret_access_key\s*[=:]\s*['"][A-Za-z0-9/+=]{40}['"]"""),
    "Google API key":           re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    "GitHub token":             re.compile(r"\bgh[opsu]_[0-9A-Za-z]{36,}\b"),
    "Slack token":              re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
    "Stripe live secret key":   re.compile(r"\bsk_live_[0-9A-Za-z]{16,}\b"),
    "PEM private key block":    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    "JSON Web Token":           re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "URL-embedded credentials": re.compile(r"\b(?:postgres|postgresql|mysql|mongodb|redis|amqp|ftp)://[^\s'\"]+:[^\s'\"]+@"),
}

# File types worth scanning for raw secrets. Templates (.html) and config
# formats are included; .css is not (no credential ever lives there).
_TEXT_EXTS = {".py", ".js", ".mjs", ".html", ".htm", ".json",
              ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env"}

# --------------------------------------------------------------------------
# Pass 2 — code-only "secret-named var = string literal" heuristic.
# --------------------------------------------------------------------------
_SECRET_ASSIGN = re.compile(
    r"""(?ix)
    \b(secret_key|secretkey|client_secret|password|passwd|pwd|
       api_key|apikey|access_token|auth_token|refresh_token|
       private_key|aws_secret|db_password)\b
    \s*[:=]\s*
    ['"][^'"]{3,}['"]            # a non-trivial quoted literal (>=3 chars)
    """
)

# Assignments whose value is obviously a non-secret reference, not a literal
# credential, are ignored by construction: the regex requires a quoted literal,
# so `secret_key = os.environ[...]` or `token = uuid.uuid4().hex` never match.


def _scan(paths, patterns_named):
    """Return a list of "<relpath>:<lineno>: <line>" hits for the given
    {label: compiled_regex} map over `paths`. Read-only."""
    findings = []
    for path in paths:
        text = S.read_text(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, rx in patterns_named.items():
                if rx.search(line):
                    findings.append(
                        f"[{label}] {S._rel(path)}:{lineno}: {line.strip()[:160]}")
    return findings


def test_no_provider_credential_signatures():
    """No AWS/Google/GitHub/Slack/Stripe key, JWT, PEM private key, or
    credentialed connection string appears in any shipped text file.

    These formats are unmistakable, so a single hit is almost certainly a real
    leak. FishFinder needs no upstream credentials, so the expected count is 0.
    """
    findings = _scan(S.iter_app_files(_TEXT_EXTS), _SIGNATURE_PATTERNS)
    assert not findings, (
        "credential-shaped string(s) found in application source:\n"
        + "\n".join(findings)
    )


def test_no_secret_named_literals_in_code():
    """No secret-named variable (api_key, password, access_token, …) is
    assigned a hardcoded string literal anywhere in the Python or JavaScript.

    Such a literal should instead be read from `os.environ` (server) or
    injected at runtime (client). The pattern deliberately ignores
    environment reads and computed values — only quoted literals trip it.
    """
    code = S.python_app_files() + S.js_app_files()
    findings = _scan(code, {"hardcoded secret literal": _SECRET_ASSIGN})
    assert not findings, (
        "secret-named variable(s) assigned a string literal:\n"
        + "\n".join(findings)
    )


def test_no_committed_dotenv_files():
    """No `.env*` file is committed under the application tree.

    `.env` files hold exactly the credentials that belong in the process
    environment, never in the repo. Their presence is both a leak risk and a
    sign that real secrets may be tracked. (None is the expected, clean state;
    `.env.example` templates, if ever added, should be reviewed by hand.)
    """
    dotenvs = [S._rel(p) for p in S.iter_app_files({".env"})]
    assert not dotenvs, (
        "committed .env file(s) found (move secrets to the process "
        "environment and gitignore these):\n" + "\n".join(dotenvs)
    )
