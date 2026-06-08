"""Dependency audit against a static, offline known-vulnerable-version list.

WHAT THIS CHECKS
    Parses requirements.txt and, for a curated set of packages with
    well-known CVEs, flags any whose *minimum allowed* version sits below the
    first patched release. Also asserts every dependency declares a version
    constraint at all (an unpinned `pkg` floats to whatever PyPI serves and
    can silently regress).

    The vulnerability list is hard-coded from publicly documented CVEs — there
    is NO network call, no PyPI lookup, no `pip` subprocess. It is necessarily
    non-exhaustive; it encodes the CVEs relevant to this project's declared
    dependencies as of the suite's last update. Re-audit when bumping deps.

WHY IT MATTERS
    A `>=` floor that admits a known-vulnerable version means a fresh install
    *can* resolve to the vulnerable build — the lockfile-free reality of a
    plain requirements.txt. Raising the floor to the patched release is a
    one-character fix, and surfacing it here keeps a known-CVE dependency from
    shipping unnoticed.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sources as S  # noqa: E402


# package(lower) -> (first_patched_version, "CVE id — one-line summary")
# Curated, offline, non-exhaustive. Each entry: the minimum allowed version
# must be >= first_patched, or the dependency is flagged.
_KNOWN_VULNERABLE = {
    "requests": ("2.32.4", "CVE-2024-47081 — .netrc credentials leaked to a "
                            "maliciously-crafted URL; fixed in 2.32.4"),
    "flask":    ("2.3.2",  "CVE-2023-30861 — session cookie cached/served to "
                           "the wrong client under certain proxy setups; "
                           "fixed in 2.3.2"),
    "pillow":   ("10.3.0", "CVE-2024-28219 — buffer overflow in _imagingcms "
                           "via crafted image; fixed in 10.3.0"),
    "werkzeug": ("3.0.6",  "CVE-2024-49767 — multipart form parsing resource "
                           "exhaustion; fixed in 3.0.6"),
    "urllib3":  ("2.2.2",  "CVE-2024-37891 — Proxy-Authorization header "
                           "retained across cross-host redirect; fixed in 2.2.2"),
}


def _parse_requirements():
    """Return [(raw_line, name_lower, op, version_tuple_or_None)] for each
    requirement line. Ignores comments, blanks, and -r/-c includes."""
    out = []
    text = S.read_text(S.REQUIREMENTS_TXT)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Drop inline comment and environment markers.
        line_nocomment = line.split("#", 1)[0].split(";", 1)[0].strip()
        # name[extras]<op><version>
        m = re.match(
            r"""^([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*"""
            r"""(==|>=|~=|>|<=|<|!=)?\s*([0-9][0-9A-Za-z.\-_*]*)?""",
            line_nocomment,
        )
        if not m:
            out.append((line, line_nocomment.lower(), None, None))
            continue
        name = m.group(1).lower()
        op = m.group(2)
        ver = _version_tuple(m.group(3)) if m.group(3) else None
        out.append((line, name, op, ver))
    return out


def _version_tuple(ver):
    """Coarse numeric version tuple, e.g. '2.32.3' -> (2, 32, 3). Non-numeric
    components (rc/post/wildcards) stop the parse — good enough for the
    minimum-floor comparison this audit needs."""
    parts = []
    for chunk in ver.split("."):
        m = re.match(r"^(\d+)", chunk)
        if not m:
            break
        parts.append(int(m.group(1)))
    return tuple(parts) if parts else None


def test_no_known_vulnerable_minimum_versions():
    """No dependency's minimum allowed version falls below the first patched
    release for a known CVE in the curated list.

    A `>=X` (or `==X`) floor that admits a vulnerable build is the finding;
    the fix is to raise the floor to the patched version named in the message.
    """
    reqs = {name: (op, ver) for _raw, name, op, ver in _parse_requirements()}
    findings = []
    for pkg, (patched_str, note) in _KNOWN_VULNERABLE.items():
        if pkg not in reqs:
            continue  # not a direct dependency; transitive pins aren't audited here
        op, ver = reqs[pkg]
        patched = _version_tuple(patched_str)
        # The lowest version this spec admits. For >=/==/~=/> the floor is the
        # stated version; we conservatively treat the stated version as the min.
        if ver is None or patched is None:
            continue
        if ver < patched:
            findings.append(
                f"{pkg} pinned '{op or ''}{'.'.join(map(str, ver))}' admits a "
                f"vulnerable version — {note}")
    assert not findings, (
        "dependency floor(s) below a known-CVE patch level:\n"
        + "\n".join(findings)
    )


def test_all_dependencies_have_version_constraints():
    """Every requirement names a version constraint.

    An unpinned dependency resolves to whatever PyPI serves at install time,
    so a future release can introduce a regression or vulnerability with no
    code change here. A constraint (even just `>=`) makes the floor explicit
    and auditable.
    """
    unpinned = [raw for raw, _name, op, _ver in _parse_requirements()
                if op is None]
    assert not unpinned, (
        "dependency line(s) without a version constraint:\n"
        + "\n".join(unpinned)
    )
