"""Shared, side-effect-free source enumeration for the static security layer.

Every static test reasons about the FishFinder *application* source — never
the security suite's own files, never the IDE/agent harness config, never the
build/cache detritus. Centralising the file-walk here keeps that scope
identical across all the category test modules, so one test can't accidentally
scan a directory another deliberately excludes (a real hazard: these very test
files contain secret-looking regex literals, so a secrets scan that walked
`security/` would flag itself).

This module is import-only and performs NO writes, network, or process work.
It is named with a leading underscore so pytest does not collect it as a test
module. The static test modules import it; if your pytest import mode can't
resolve the sibling import, each test still degrades to its own inline copy of
`PROJECT_ROOT` — but in practice the `static/` package (it has __init__.py) is
importable here.
"""

import os

# security/static/_sources.py  ->  security/static  ->  security  ->  <root>
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SECURITY_DIR = os.path.dirname(_THIS_DIR)
PROJECT_ROOT = os.path.dirname(SECURITY_DIR)

APP_PY = os.path.join(PROJECT_ROOT, "app.py")
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "templates")
TOOLS_DIR = os.path.join(PROJECT_ROOT, "tools")
REQUIREMENTS_TXT = os.path.join(PROJECT_ROOT, "requirements.txt")

# Directory names pruned from every walk. `security` keeps the suite from
# scanning itself; `.claude` is the agent/IDE harness (developer-local command
# allowlist, not shipped application config); the rest are caches/VCS/binaries.
_EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", "env",
    ".idea", ".vscode",
    "security",       # the suite itself — its files contain secret-like regexes
    ".claude",        # agent/IDE harness config, not application config
    "img",            # cached NOAA rasters + binary assets
    "reports",
})


def _rel(path):
    """Project-relative path for readable failure messages."""
    return os.path.relpath(path, PROJECT_ROOT)


def read_text(path):
    """Read a file as UTF-8 text, tolerating odd bytes. Read-only."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def iter_app_files(extensions):
    """Yield absolute paths of application files whose extension is in
    `extensions` (a set of lowercase suffixes WITH the dot, e.g. {'.py'}).

    Walks the whole project root but prunes `_EXCLUDED_DIRS` so the security
    suite, the agent harness, caches, and binary asset dirs are never scanned.
    Deterministic order so failure output is stable run-to-run.
    """
    exts = {e.lower() for e in extensions}
    out = []
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        # Prune excluded dirs in place so os.walk doesn't descend into them.
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIRS)
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            # `.env`, `.env.local`, etc. have no "extension" by splitext, so
            # match them by name prefix as well.
            if ext in exts or (".env" in exts and name.startswith(".env")):
                out.append(os.path.join(dirpath, name))
    return sorted(set(out))


def python_app_files():
    """Every application .py file (app.py, src/, tools/, view_spot.py, …)."""
    return iter_app_files({".py"})


def js_app_files():
    """Every application JavaScript file under static/ (and anywhere else)."""
    return iter_app_files({".js", ".mjs"})
