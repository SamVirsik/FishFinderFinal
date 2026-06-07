#!/usr/bin/env python3
"""FishFinder security test runner.

One command runs the security suite and produces a self-contained HTML
report you review before every deploy.

    python security/run_security.py                  # static layer only
    python security/run_security.py --integration    # + black-box HTTP layer
    python security/run_security.py --load --confirm  # + abuse/load layer

Layers
------
  static       Always runs. Source/config analysis, no server needed.
  integration  Runs with --integration. Black-box HTTP against a Flask
               server booted on a random loopback port (see conftest.py).
  load         Runs ONLY with both --load and --confirm. Abuse simulation;
               destructive (can fill disk, pin CPU). Never run in CI, and
               never before the disk-cache quota fix lands. See README.md.

Each layer is run as a separate pytest invocation with
`--json-report` (pytest-json-report). The per-layer JSON is collected,
merged, and rendered into a timestamped HTML report under
security/reports/.

Exit code is non-zero if any executed layer had a failing test, so this
can gate a deploy script.
"""

import argparse
import datetime as _dt
import html
import json
import os
import subprocess
import sys

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
SECURITY_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SECURITY_DIR)
REPORTS_DIR = os.path.join(SECURITY_DIR, "reports")

# Layer name -> subdirectory holding its test modules.
LAYER_DIRS = {
    "static": os.path.join(SECURITY_DIR, "static"),
    "integration": os.path.join(SECURITY_DIR, "integration"),
    "load": os.path.join(SECURITY_DIR, "load"),
}


# --------------------------------------------------------------------------
# Layer execution
# --------------------------------------------------------------------------
def run_layer(layer):
    """Run one pytest layer, returning a parsed json-report dict.

    Each layer writes its own JSON sidecar so a crash in one layer never
    destroys another's results. Returns a dict with at least the keys
    `layer`, `executed`, and (when executed) the raw pytest-json-report
    `summary` and `tests` lists. A layer with no test files yet is reported
    as executed-but-empty rather than an error.
    """
    layer_dir = LAYER_DIRS[layer]
    json_path = os.path.join(REPORTS_DIR, f"_raw_{layer}.json")

    if not os.path.isdir(layer_dir):
        return {"layer": layer, "executed": False,
                "reason": f"layer directory missing: {layer_dir}"}

    cmd = [
        sys.executable, "-m", "pytest", layer_dir,
        "-q",
        "--json-report",
        f"--json-report-file={json_path}",
        # Keep the suite running even when a layer has no tests yet.
        "-o", "addopts=",
    ]
    print(f"\n=== running layer: {layer} ===")
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT)

    # pytest exit code 5 == "no tests collected"; treat as an empty-but-OK
    # layer so the scaffolding runs green before any tests exist.
    if proc.returncode == 5:
        return {"layer": layer, "executed": True, "empty": True,
                "summary": {}, "tests": [], "exit_code": proc.returncode}

    if not os.path.exists(json_path):
        return {"layer": layer, "executed": True, "error": True,
                "reason": "pytest produced no json-report (is "
                          "pytest-json-report installed?)",
                "exit_code": proc.returncode, "summary": {}, "tests": []}

    with open(json_path, encoding="utf-8") as f:
        report = json.load(f)

    return {
        "layer": layer,
        "executed": True,
        "exit_code": proc.returncode,
        "summary": report.get("summary", {}),
        "tests": report.get("tests", []),
        "duration": report.get("duration"),
    }


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------
_STATUS_COLORS = {
    "passed": "#1f9d55",
    "failed": "#e3342f",
    "error": "#b91c1c",
    "skipped": "#b08900",
    "xfailed": "#6b7280",
    "xpassed": "#6b7280",
}


def _esc(s):
    return html.escape(str(s if s is not None else ""))


def _test_status(test):
    """pytest-json-report records per-phase outcomes; the call phase is the
    one that matters, but a setup/teardown error should surface too."""
    for phase in ("call", "setup", "teardown"):
        info = test.get(phase)
        if info and info.get("outcome") not in (None, "passed"):
            return info.get("outcome")
    return test.get("outcome", "unknown")


def _failure_message(test):
    """Pull the longrepr / crash message from whichever phase failed."""
    for phase in ("call", "setup", "teardown"):
        info = test.get(phase) or {}
        if info.get("outcome") in ("failed", "error"):
            crash = info.get("crash") or {}
            return crash.get("message") or info.get("longrepr") or ""
    return ""


def _layer_counts(layer_result):
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "other": 0}
    for t in layer_result.get("tests", []):
        st = _test_status(t)
        if st in counts:
            counts[st] += 1
        else:
            counts["other"] += 1
    return counts


def build_html(results, context_notes):
    """Render the merged layer results into a single self-contained HTML doc."""
    ts = _dt.datetime.now()
    totals = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "other": 0}

    banner_cards = []
    for r in results:
        counts = _layer_counts(r)
        for k in totals:
            totals[k] += counts.get(k, 0)
        if not r.get("executed"):
            state = f"not run — {_esc(r.get('reason', 'skipped'))}"
        elif r.get("error"):
            state = f"runner error — {_esc(r.get('reason', ''))}"
        elif r.get("empty"):
            state = "no tests defined yet"
        else:
            state = (f"{counts['passed']} pass · {counts['failed']} fail · "
                     f"{counts['error']} error · {counts['skipped']} skip")
        banner_cards.append(
            f'<div class="card"><h3>{_esc(r["layer"])}</h3>'
            f'<div class="cardstate">{state}</div></div>'
        )

    overall_fail = totals["failed"] + totals["error"]
    overall_class = "fail" if overall_fail else "pass"
    overall_label = "FAILURES PRESENT" if overall_fail else "ALL CLEAR"

    # Detail rows.
    rows = []
    for r in results:
        if not r.get("tests"):
            continue
        for t in sorted(r["tests"], key=lambda x: x.get("nodeid", "")):
            st = _test_status(t)
            color = _STATUS_COLORS.get(st, "#6b7280")
            msg = _failure_message(t) if st in ("failed", "error") else ""
            rows.append(
                "<tr>"
                f'<td class="layer">{_esc(r["layer"])}</td>'
                f'<td class="nodeid">{_esc(t.get("nodeid", ""))}</td>'
                f'<td><span class="badge" style="background:{color}">'
                f'{_esc(st)}</span></td>'
                f'<td class="msg"><pre>{_esc(msg)}</pre></td>'
                "</tr>"
            )
    if not rows:
        rows.append(
            '<tr><td colspan="4" class="empty">No tests have been '
            "implemented yet — this run only verified the scaffolding.</td></tr>"
        )

    notes_html = _esc(context_notes) if context_notes else (
        "No notes supplied for this run. Pass --notes \"...\" to annotate, "
        "e.g. the commit under test or the deploy it gates."
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FishFinder Security Report — {ts:%Y-%m-%d %H:%M}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; background: #0f1419; color: #e6e6e6; }}
  header {{ padding: 24px 32px; border-bottom: 1px solid #232a33; }}
  h1 {{ margin: 0 0 4px; font-size: 20px; }}
  .sub {{ color: #8b96a3; font-size: 13px; }}
  .overall {{ display: inline-block; margin-top: 12px; padding: 6px 14px;
             border-radius: 6px; font-weight: 700; letter-spacing: .5px; }}
  .overall.pass {{ background: #143; color: #6ee7a8; }}
  .overall.fail {{ background: #411; color: #ff9b95; }}
  .cards {{ display: flex; gap: 16px; flex-wrap: wrap; padding: 24px 32px; }}
  .card {{ background: #161c24; border: 1px solid #232a33; border-radius: 8px;
          padding: 16px 20px; min-width: 180px; }}
  .card h3 {{ margin: 0 0 6px; font-size: 13px; text-transform: uppercase;
             letter-spacing: .6px; color: #8b96a3; }}
  .cardstate {{ font-size: 14px; }}
  section {{ padding: 8px 32px 32px; }}
  h2 {{ font-size: 15px; border-bottom: 1px solid #232a33; padding-bottom: 6px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #1d242d;
           vertical-align: top; }}
  th {{ color: #8b96a3; font-weight: 600; }}
  td.layer {{ color: #8b96a3; white-space: nowrap; }}
  td.nodeid {{ font-family: ui-monospace, Menlo, monospace; color: #cdd6e0; }}
  .badge {{ color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px;
           text-transform: uppercase; letter-spacing: .4px; }}
  td.msg pre {{ margin: 0; white-space: pre-wrap; color: #ff9b95;
               font-family: ui-monospace, Menlo, monospace; font-size: 12px; }}
  td.empty {{ color: #8b96a3; font-style: italic; }}
  .notes {{ background: #161c24; border: 1px solid #232a33; border-radius: 8px;
           padding: 16px 20px; white-space: pre-wrap; }}
</style>
</head>
<body>
<header>
  <h1>FishFinder Security Report</h1>
  <div class="sub">Generated {ts:%Y-%m-%d %H:%M:%S} · {_esc(sys.platform)}</div>
  <div class="overall {overall_class}">{overall_label}</div>
  <div class="sub" style="margin-top:8px">
    {totals['passed']} passed · {totals['failed']} failed ·
    {totals['error']} error · {totals['skipped']} skipped
  </div>
</header>

<div class="cards">
  {''.join(banner_cards)}
</div>

<section>
  <h2>Test results</h2>
  <table>
    <thead><tr><th>Layer</th><th>Test</th><th>Status</th>
      <th>Failure message</th></tr></thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</section>

<section>
  <h2>Notes &amp; context</h2>
  <div class="notes">{notes_html}</div>
</section>
</body>
</html>"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Run the FishFinder security test suite and emit an "
                    "HTML report.")
    p.add_argument("--integration", action="store_true",
                   help="also run the black-box HTTP layer (Flask server is "
                        "booted automatically on a random loopback port).")
    p.add_argument("--load", action="store_true",
                   help="also run the abuse/load layer. Requires --confirm. "
                        "Destructive; never use in CI.")
    p.add_argument("--confirm", action="store_true",
                   help="safety gate that must accompany --load.")
    p.add_argument("--notes", default="",
                   help="free-text context recorded in the report (e.g. the "
                        "commit or deploy under test).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    os.makedirs(REPORTS_DIR, exist_ok=True)

    layers = ["static"]
    if args.integration:
        layers.append("integration")

    if args.load:
        if not args.confirm:
            print("ERROR: --load requires --confirm. The load layer generates "
                  "abusive traffic (disk-filling, CPU-pinning) and must never "
                  "run unintentionally. Re-run with --load --confirm once you "
                  "have read security/README.md.", file=sys.stderr)
            return 2
        layers.append("load")

    results = [run_layer(layer) for layer in layers]

    html_doc = build_html(results, args.notes)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(REPORTS_DIR, f"security-report-{ts}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    # Console summary.
    print("\n" + "=" * 60)
    failures = 0
    for r in results:
        counts = _layer_counts(r)
        failures += counts["failed"] + counts["error"]
        print(f"  {r['layer']:<12} "
              f"pass={counts['passed']} fail={counts['failed']} "
              f"error={counts['error']} skip={counts['skipped']}")
    print("=" * 60)
    print(f"Report: {out_path}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
