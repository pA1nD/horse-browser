#!/usr/bin/env bash
# tests/harness-pytest.sh — run the vendored harness's python suite (harness/tests)
# through the harness's own venv, self-bootstrapping both the venv and pytest.
# Part of `npm test`: the shell suites cover the launcher/browser end; this covers
# the daemon/helpers unit tests that previously only ran when invoked by hand.
set -euo pipefail

# A test run must never reach the operator's ~/.claude or ~/.grok. 16 of 19 suites once
# lacked this, so `npm test` from ANY clone wired that clone's path into the real global
# settings.json — which is how a build agent's throwaway checkout came to leave a dead
# hook behind that failed every Bash call on the machine. external-state.sh is the one
# suite that unsets this, against temp paths of its own.
export HORSE_BROWSER_NO_RECONCILE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/harness/.venv/bin/python"

"$ROOT/bin/horse-browser" harness-setup >/dev/null

# The test deps are the pyproject's optional [test] extra, not runtime deps: the venv
# deliberately holds only cdp-use/websockets, because the harness package itself is imported
# over PYTHONPATH so an npm update can swap the code in place with no reinstall.
#
# Read the list FROM pyproject instead of naming packages here, and install UNCONDITIONALLY.
# Both matter, and the old code got both wrong: it hardcoded "pytest" and skipped the install
# whenever pytest already imported — so pillow (added to the extra later) was never installed.
# A machine that happened to have pillow passed; a fresh export failed collection for the whole
# suite on conftest's `from PIL import Image`. Installing is a fast no-op when satisfied, and is
# allowed to fail so an offline box with the deps present still runs.
extras="$(python3 - "$ROOT/harness/pyproject.toml" <<'PY'
import re, sys
try: src = open(sys.argv[1]).read()
except OSError: src = ""
m = re.search(r'^\s*test\s*=\s*\[([^\]]*)\]', src, re.M)
print(" ".join(re.findall(r'"([^"]+)"', m.group(1))) if m else "pytest pillow")
PY
)"
# shellcheck disable=SC2086
if command -v uv >/dev/null 2>&1; then
  uv pip install --quiet --python "$PY" $extras || true
else
  "$PY" -m pip install --quiet $extras || true
fi

exec "$PY" -m pytest "$ROOT/harness/tests" -q
