# FishFinder Security Test Suite

A three-layer security harness for FishFinder. The structure and report
pipeline are in place; the individual tests are written against the findings
in [`SECURITY_THREAT_MODEL.md`](../SECURITY_THREAT_MODEL.md).

One command runs the suite and produces a timestamped, self-contained HTML
report under `security/reports/` — the artifact you review before every
deploy.

```
python security/run_security.py                   # static layer only
python security/run_security.py --integration     # + black-box HTTP layer
python security/run_security.py --load --confirm   # + abuse/load layer (see warning)
python security/run_security.py --integration --notes "rc2, gates prod deploy"
```

## Setup

```
pip install -r security/requirements.txt
# The integration and load layers boot the real app, so also install:
pip install -r requirements.txt
```

## The three layers

### 1. `static/` — static analysis (always runs)

Source- and config-level checks that need **no running server**. They read
the code and dependency manifests and assert on shape. Coverage is grouped by
category, one file per area:

- `test_static.py` — server bind stays `127.0.0.1`, `debug=False`, no
  hardcoded secrets in `src/`, rate-limit decorators on expensive routes,
  no bare `print()`, and the raster/Spotfinder size caps are sane.
- `test_secrets.py` — full-tree credential scan (.py/.js/.html/config/`.env`)
  for provider key/token/JWT/PEM/connection-string formats and secret-named
  literals in code.
- `test_flask_config.py` — `DEBUG` unreachable by any path, `SECRET_KEY` not a
  weak literal, and `SESSION_COOKIE_SECURE/HTTPONLY/SAMESITE` set safely.
- `test_headers.py` — CSP (no `unsafe-eval`, `object-src 'none'`), XCTO,
  XFO, Referrer-Policy, COOP present; no wildcard CORS.
- `test_dangerous_patterns.py` — no `eval`/`exec`/`os.system`/`shell=True`/
  `pickle.loads`/unsafe-`yaml`/string-built SQL (Python) or `eval`/
  `new Function`/`document.write` (JS).
- `test_rate_limiting.py` — every route covered by an explicit limit, an
  explicit exempt, or the global `default_limits`; expensive routes tighter.
- `test_input_validation.py` — every `request.*`-reading handler shows a
  validation/clamping guard; the shared validators and body-size cap exist.
- `test_dependencies.py` — `requirements.txt` floors audited against a static,
  offline known-CVE list; every dependency carries a version constraint.

The suite-only file `_sources.py` centralises the app-source file walk (and
deliberately excludes `security/` and `.claude/` so the scanners never flag
themselves or the agent harness).

Fast, deterministic, dependency-light. **Safe in CI** — this is the layer
that always runs.

**The static layer has no skipped tests by design.** Every test runs
unconditionally and either passes or fails — no `@pytest.mark.skip`, no
`@pytest.mark.xfail`. A test that checks for a hardening fix which is not yet
implemented is *expected* to FAIL, and that visible FAILED is the point: it
tracks what still needs fixing. PASSED means the code is clean; FAILED means a
fix is still owed. **The report is the backlog.**

**Run directly:** `python security/run_security.py`

### 2. `integration/` — black-box HTTP (opt-in)

Exercises the **live application over HTTP**. The `client`/`base_url`
fixtures in `conftest.py` boot Flask on a random **loopback** port and tear
it down at session end. These tests probe runtime behavior:

- input validation returns clean 400s (bad coords, out-of-range resolution)
- rate-limit headers are emitted; limits actually trigger
- security headers are present on real responses
- unknown sources are rejected, not silently substituted
- error responses don't leak stack traces

**Run:** `python security/run_security.py --integration`

> Integration tests avoid endpoints that write to the NOAA disk cache where
> possible. They run against a real server but are **not** abusive — keep
> them safe for routine pre-deploy use and CI-with-a-server.

### 3. `load/` — abuse simulation (manual only, never CI)

Simulates a hostile caller to verify the **abuse-resistance fixes** hold:
request floods, oversized `resolution`/`size` amplifiers, disk-cache growth,
Spotfinder compute amplification. This is how you prove the §2/§6 remediation
items in the threat model actually work.

**Run:** `python security/run_security.py --load --confirm`

`--load` alone is refused; `--confirm` is a required second safety gate.

---

> ## ⚠️ DANGER — read before running the load layer
>
> The load layer generates **genuinely abusive traffic against the target**.
> It is destructive by design and has two standing prerequisites:
>
> 1. **Run it only AFTER the abuse-resistance fixes are in place.** Running
>    it against an unfixed server doesn't test a defense — it just performs
>    the attack. The point is to confirm a fix holds, so the fix must exist
>    first.
>
> 2. **NEVER run it before the disk-cache quota fix is implemented**
>    (threat model item #5: *"Unbounded, never-expiring disk cache writable
>    by anonymous tile requests → disk-exhaustion DoS"*). Until
>    `src/LayerGeneration.py` enforces a quota/eviction on `img/raster/`,
>    a tile-harvest load test will **write unbounded data to disk and can
>    fill the volume**, taking down the app and possibly the OS. There is no
>    automatic cleanup of `img/raster/`.
>
> Additional guardrails:
> - The target host is **hard-coded to `127.0.0.1`** in `conftest.py` and
>   asserted at import — the load layer can never reach an external host,
>   by construction. Do not make it configurable.
> - Never run this layer in CI. `run_security.py` requires both `--load`
>   and `--confirm`; CI must pass neither.
> - Run it on a machine where filling the disk and pinning CPU are
>   acceptable, and watch `img/raster/` size while it runs.

---

## The report

Every run writes `security/reports/security-report-<timestamp>.html`:

- a **summary banner** with pass/fail/error/skip counts per layer plus an
  overall ALL CLEAR / FAILURES PRESENT verdict
- a **color-coded table** of every test with its status and any failure
  message
- a **notes/context** section (populate with `--notes "..."`)

The report is self-contained (inline CSS, no external assets) so it can be
archived or attached to a deploy ticket. `security/reports/` is gitignored.

The runner exits non-zero if any executed layer has a failing/error test, so
it can gate a deploy script.

## Layout

```
security/
  run_security.py      master runner + HTML report generator
  conftest.py          pytest fixtures; hard-coded 127.0.0.1 safety invariant
  requirements.txt     suite-only deps (pytest, pytest-json-report, requests)
  static/              static analysis tests (no server)
  integration/         black-box HTTP tests (server auto-booted)
  load/                abuse simulation (manual; --load --confirm only)
  reports/             generated HTML (gitignored)
  README.md
```

## Adding a test

Drop a `test_*.py` into the appropriate layer directory. The runner discovers
it automatically — no registration needed. Use the `client` fixture for fast
in-process request/response checks, or `base_url` for tests that need a real
listening socket.
