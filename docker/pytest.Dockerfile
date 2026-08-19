# WB-11 (Trello YVCb5HAF) — Python unit + integration test suite, containerised.
#
# Mirrors .github/workflows/ci.yml's unit-tests job: same pip install, same
# pytest invocation. A pass in here is the same pass CI would report, run in
# a clean environment instead of whatever state the host venv happens to be
# in — this project's own host venv has hit stale-shebang issues repeatedly
# (this same work session), which is exactly the class of problem a fresh
# container sidesteps.
#
# Also reused (unmodified) as the base image for the e2e-smoke service in
# docker-compose.test.yml — it already has the app's real runtime deps
# (signalrcore, websockets, requests) needed to drive F1SignalRClient
# against the sim.
FROM python:3.13-slim

WORKDIR /app

# build-essential covers the rare case a dependency has no prebuilt wheel for
# this image's arch (Pillow/numpy/pandas normally ship one, but this keeps
# the image resilient rather than failing opaquely on an arch without one).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY . .

# Isolated data home: the suite must never touch a host install's real
# settings.json / livetiming cache. tests/__init__.py already redirects
# F1_DATA_HOME to a tempdir for unittest-style discovery; this is the
# equivalent default for the container's own environment (e2e-smoke
# overrides it per-run in docker-compose.test.yml).
ENV F1_DATA_HOME=/tmp/f1unleashed-test-data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Non-root runtime user (Do: "no dependency upgrades... no root runtime
# user" per this project's DevOps conventions).
RUN useradd --create-home --uid 1000 tester \
    && mkdir -p "$F1_DATA_HOME" \
    && chown -R tester:tester /app "$F1_DATA_HOME"
USER tester

CMD ["pytest", "--cov=app", "--cov-report=term"]
