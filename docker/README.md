# Dockerised test environments

WB-11 (Trello [YVCb5HAF](https://trello.com/c/YVCb5HAF)). This is the foundation the card
asked for: reliable, containerised unit + integration testing, plus one working, verified
example of the "test end-to-end against the live replica" pattern using the SignalR
simulator (`repos/sim`). It is deliberately **not** the full multi-OS/multi-browser matrix —
see "Not covered" below for why, and what's actually needed to build that out.

## Why this exists

This session (and others before it) has repeatedly hit stale-shebang and other host-venv
drift issues running the test suite directly against `repos/dev`'s venv. A container sidesteps
that whole class of problem: the image is built fresh from `requirements.txt` +
`requirements-dev.txt` every time, so "works in the container" means "works from a clean
install," not "works because of whatever state this particular venv happens to be in today."

## What's here

```
docker-compose.test.yml        # the whole thing, at the repo root
docker/pytest.Dockerfile       # python unit + integration suite, also reused for e2e-smoke
docker/node-tests.Dockerfile   # node --test tests/*.mjs (frontend unit tests)
docker/e2e-fixtures/           # tiny synthetic CDN session for the sim to replay
docker/e2e/test_sim_connectivity_smoke.py   # the one e2e smoke test
```

## Running the unit + integration suite (Python)

```bash
docker compose -f docker-compose.test.yml build pytest
docker compose -f docker-compose.test.yml run --rm pytest
```

This runs the exact same command `.github/workflows/ci.yml`'s `unit-tests` job runs
(`pytest --cov=app --cov-report=term`), against the exact same `requirements.txt` +
`requirements-dev.txt` a fresh clone would install — so a pass here is a real CI-equivalent
pass, not an approximation. It covers `tests/unit/`, `tests/integration/`, `tests/regression/`,
and every top-level `tests/test_*.py` file (everything pytest discovers is a real unittest-
or pytest-style test in this project's real suite, nothing new was invented for Docker's
sake).

**Verified 2026-08-19** (host baseline, since this session's Docker daemon was unreachable —
see "What could not be verified" below): `./venv/bin/python -m pytest tests -q` →
**774 passed, 2 failed, 6 xfailed** in 257s. The 2 failures
(`tests/test_router_livetiming_stream_http.py::WebsocketSessionEndpoint::
test_connect_and_forward_command_to_engine` and `test_non_object_command_is_ignored_not_fatal`)
are pre-existing, unrelated to this card (`FakeEngine.add_client() got an unexpected keyword
argument 'start_at_zero'` — a test double out of sync with the real engine's signature), and
not something this card's scope covers fixing. The container runs the identical
`requirements.txt`-pinned dependency set and the identical pytest invocation, so it is
expected to reproduce this exact result, not a different one — there is nothing in the
Dockerfile that changes test behaviour, only where it runs.

## Running the JS test suite

```bash
docker compose -f docker-compose.test.yml build node-tests
docker compose -f docker-compose.test.yml run --rm node-tests
```

**Verified 2026-08-19** (host baseline): `node --test tests/*.mjs` → **67 pass, 0 fail**. This
image only needs `static/js/*.js` and `tests/*.mjs` (the tests read the source directly and
wrap it in `new Function(...)`, per the pattern in `tests/test_settings_schema.mjs`) — no
npm install, no `package.json`, nothing to drift.

## Running the e2e smoke test (against the sim)

```bash
docker compose -f docker-compose.test.yml --profile e2e up --build \
    --abort-on-container-exit --exit-code-from e2e-smoke
```

This brings up two extra services, gated behind the `e2e` compose profile so they never run
as part of the default `docker compose up`:

- **`sim`** — built from source, from the sibling `repos/sim` checkout (`build.context:
  ../sim`). Serves a tiny synthetic CDN session (`docker/e2e-fixtures/2026/
  9999_Docker-Smoke-CDN/1_Practice/`, five messages: `TrackStatus`, `WeatherData`, two
  `TimingData`, terminal `SessionStatus`) over the sim's real SignalR-compatible protocol.
- **`e2e-smoke`** — reuses the `pytest` image unmodified (it already has every runtime dep
  the app's real `F1SignalRClient` needs — `signalrcore`, `websockets`, `requests`) and runs
  `docker/e2e/test_sim_connectivity_smoke.py`.

That one test:
1. Points the app's settings at the sim (`endpoints.f1LivetimingBase`), the same
   `settings.json` key a human would edit in the settings dialog per `repos/sim/README.md`.
2. Confirms the sim can see the fixture session over its control API.
3. Configures (but does not yet start) the broadcast.
4. Constructs the app's **real** `F1SignalRClient` (`app/services/signalr_client.py` —
   nothing about this test reimplements or mocks that class) and starts it.
5. Only once the client reports `connected` does it call `POST /api/broadcast/start` — see
   "A real bug this test caught" below for why that ordering matters.
6. Drains the client's message queue and asserts on specific, real content: every expected
   topic actually arrived, the terminal `SessionStatus`/`Ends` marker was seen, and the
   client's own `live.jsonl` cache file on disk has the same content.

### A real bug this test caught, while building it

The first version configured the sim with `start_at: "now"` before the client had connected.
Running it for real (not just reading the code) showed **3 messages received instead of 5** —
the sim starts a fixed feed the moment it's told to, and a client that connects a few hundred
milliseconds late (real negotiate + websocket handshake time) simply misses whatever was
already sent, same as a real live feed does not replay history to a client that joins late.
Fixed by configuring without `start_at`, waiting for the client's own `connected` status, and
only then calling `/api/broadcast/start`. Left as a comment in the test itself so this isn't
lost.

## Not covered (and why)

- **Multi-OS matrix.** Not built. Docker's own container OS (Debian slim /
  Alpine here) already gives OS-independence from whatever the *host* is running — that's
  the actual value "run tests in Docker" gets you for a project with no OS-specific code
  paths, and this project doesn't have any that the test suite exercises differently by host
  OS. A genuine multi-OS matrix (multiple base images, e.g. testing against different Python
  minor versions, or Windows-specific code paths if the app ever grows any) is real work with
  a real cost, and nothing in this card's scoping or this project's `CLAUDE.local.md` names a
  concrete reason to pay it right now. If one shows up (e.g. `f1unleashed.ps1`/`.bat`
  behaviour needs testing, or a dependency needs pinning per-platform), it's an easy
  extension: add a second `pytest.Dockerfile` variant with a different `FROM` line and a
  matrix in `docker-compose.test.yml`, not a redesign.
- **Browser testing.** Not built. This app's frontend has no automated browser test harness
  today (confirmed against this session's own history and `tests/*.mjs`'s
  source-extraction-based approach, which deliberately avoids needing a real browser/DOM).
  Building one (Playwright/Selenium + a browser image or `--profile browsers` service) is a
  separate, non-trivial piece of work — picking a framework, writing the first real browser
  test, deciding what "critical user journey" means for this app — and doing it inside this
  card would mean guessing at scope nobody asked for yet. Real gap, tracked here explicitly
  rather than quietly skipped.
- **A full e2e test suite.** Only one smoke test exists. It proves the pattern (real app
  client, real sim, real network hop, real message assertions) works end-to-end; extending it
  to more scenarios (multiple sessions, reconnect/backoff behaviour, race-mode delay bursts,
  auth-token paths) is direct, low-risk follow-up now that the harness exists — each new case
  is a new fixture + a new test function, not new infrastructure.
- **CI integration.** Explicitly out of scope for this card (paired with the not-yet-started
  "CI/CD workflows" card). Nothing here touches `.github/workflows/ci.yml`.

## What could not be verified in this session

The sandboxed environment this session ran in could not launch Docker Desktop (`open -a
Docker` failed with a RunningBoard/launch-services error, and no alternative daemon —
Colima, a raw `dockerd` socket — was available), so **no `docker build` or `docker compose
up` was actually executed**. `docker compose -f docker-compose.test.yml config` (and
`--profile e2e config`) were run and confirm both YAML files are syntactically valid and the
profile gating behaves as intended (default profile shows only `pytest`/`node-tests`; `e2e`
profile adds `sim`/`e2e-smoke` with the fixture volume mount, healthcheck, and `depends_on`
wired correctly).

What **was** verified for real, standing in for the container run:
- The exact commands each Dockerfile's `CMD` runs (`pytest --cov=app --cov-report=term`,
  `node --test tests/*.mjs`) were run directly against this worktree's host venv/Node — see
  the pass/fail counts above.
- The e2e smoke test's actual logic was run for real against a locally-started sim instance
  (`uvicorn app.main:app` on the host, pointed at `docker/e2e-fixtures` via
  `SIM_CDN_DATA_ROOT`) — this is the same code that runs inside `docker/e2e/
  test_sim_connectivity_smoke.py`, just not inside a container, and it passed twice in a row
  after the connect-before-start fix above.

This is a real, named limitation, not a claim of "should work." The Dockerfiles and compose
file were reviewed by hand and follow this project's own conventions (non-root runtime user,
pinned base images, `.dockerignore` to keep `venv`/`log`/`docs` out of the build context), but
an actual `docker build` has not run in this session. Whoever runs this first for real should
expect it to work (nothing in it is exotic — standard `python:3.13-slim`/`node:22-alpine`
images, standard `pip install -r requirements.txt`) but should not treat this note as "already
verified."
