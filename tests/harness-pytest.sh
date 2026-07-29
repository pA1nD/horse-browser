#!/usr/bin/env bash
# tests/harness-pytest.sh — run the vendored harness's python suite (harness/tests)
# through the harness's own venv, self-bootstrapping both the venv and pytest.
# Part of `npm test`: the shell suites cover the launcher/browser end; this covers
# the daemon/helpers unit tests that previously only ran when invoked by hand.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/harness/.venv/bin/python"

"$ROOT/bin/horse-browser" harness-setup >/dev/null

# pytest is the pyproject's optional [test] extra, not a runtime dep — install on demand.
if ! "$PY" -m pytest --version >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install --quiet --python "$PY" pytest
  else
    "$PY" -m pip install --quiet pytest
  fi
fi

exec "$PY" -m pytest "$ROOT/harness/tests" -q
