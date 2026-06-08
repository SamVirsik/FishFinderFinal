"""Dangerous-construct scan over the application source.

WHAT THIS CHECKS
    The classic code-execution and injection sinks are absent from shipped
    application code:
      - Python: eval(), exec(), os.system(), os.popen(), commands.*,
        subprocess(..., shell=True), pickle.loads on untrusted data,
        yaml.load without SafeLoader, and SQL built by string formatting
        (f-string / % / .format / concatenation into execute()).
      - JavaScript: eval(), new Function(), and document.write() of
        non-literal data.

WHY IT MATTERS
    Each of these turns attacker-controlled input into code or commands:
    eval/exec/new Function execute arbitrary code; os.system/shell=True is
    command injection; pickle.loads is deserialization RCE; string-built SQL
    is injection. FishFinder is a numpy/Flask app with no database and no
    shell-out in its request path, so the correct count is zero — any hit is a
    new sink that needs review. (The security *runner* legitimately uses
    subprocess to invoke pytest, but it lives under `security/` and is
    excluded from this scan by scope.)
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sources as S  # noqa: E402


# Python sinks. Patterns are line-oriented and intentionally specific to keep
# false positives (e.g. the word "evaluate", or "exec" inside "execute") out.
_PY_PATTERNS = {
    "eval()":               re.compile(r"(?<![\w.])eval\s*\("),
    "exec()":               re.compile(r"(?<![\w.])exec\s*\("),
    "os.system()":          re.compile(r"\bos\.system\s*\("),
    "os.popen()":           re.compile(r"\bos\.popen\s*\("),
    "commands module":      re.compile(r"\bcommands\.(getoutput|getstatusoutput)\s*\("),
    "subprocess shell=True": re.compile(r"shell\s*=\s*True"),
    "pickle.loads()":       re.compile(r"\bpickle\.loads\s*\("),
    "yaml.load (unsafe)":   re.compile(r"\byaml\.load\s*\((?!.*SafeLoader)"),
    "marshal.loads()":      re.compile(r"\bmarshal\.loads\s*\("),
}

# SQL-injection shapes: a query verb adjacent to a format/concatenation. We
# require an actual SQL keyword so ordinary string formatting isn't flagged.
_SQL_PATTERNS = {
    "f-string SQL":     re.compile(r"""(?i)f['"].*\b(select|insert|update|delete|drop|where|from)\b.*\{"""),
    "%-format SQL":     re.compile(r"""(?i)['"].*\b(select|insert|update|delete)\b.*['"]\s*%\s"""),
    ".format() SQL":    re.compile(r"""(?i)['"].*\b(select|insert|update|delete)\b.*['"]\s*\.format\s*\("""),
    "execute(+concat)": re.compile(r"""(?i)\.execute\s*\(\s*['"].*\b(select|insert|update|delete)\b.*['"]\s*[%+]"""),
}

# JavaScript sinks.
_JS_PATTERNS = {
    "eval()":          re.compile(r"(?<![\w.])eval\s*\("),
    "new Function()":  re.compile(r"\bnew\s+Function\s*\("),
    "document.write()": re.compile(r"\bdocument\.write(?:ln)?\s*\("),
}


def _scan(paths, patterns_named):
    findings = []
    for path in paths:
        for lineno, line in enumerate(S.read_text(path).splitlines(), start=1):
            stripped = line.lstrip()
            # Skip whole-line comments so a commented mention isn't flagged.
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            for label, rx in patterns_named.items():
                if rx.search(line):
                    findings.append(
                        f"[{label}] {S._rel(path)}:{lineno}: {line.strip()[:160]}")
    return findings


def test_no_python_code_execution_sinks():
    """No eval/exec/os.system/os.popen/shell=True/pickle.loads/unsafe-yaml in
    the application Python. Each is a direct code-execution or command-
    injection sink."""
    findings = _scan(S.python_app_files(), _PY_PATTERNS)
    assert not findings, (
        "dangerous Python construct(s) found:\n" + "\n".join(findings)
    )


def test_no_string_built_sql():
    """No SQL statement is assembled by f-string, %, .format(), or
    concatenation into .execute().

    The app has no database today; this guards the moment one is added against
    shipping an injectable query.
    """
    findings = _scan(S.python_app_files(), _SQL_PATTERNS)
    assert not findings, (
        "string-formatted SQL (injection risk) found:\n" + "\n".join(findings)
    )


def test_no_javascript_dynamic_code_sinks():
    """No eval(), new Function(), or document.write() in the client JavaScript.

    All three execute or inject strings as code/markup and are the standard
    DOM-XSS sinks; the viewer builds the DOM through the ArcGIS API and
    textContent instead.
    """
    findings = _scan(S.js_app_files(), _JS_PATTERNS)
    assert not findings, (
        "dangerous JavaScript construct(s) found:\n" + "\n".join(findings)
    )
