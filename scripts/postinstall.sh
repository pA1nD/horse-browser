#!/usr/bin/env bash
# npm postinstall for @pa1nd/horse-browser.
#
# Runs the package's own install.sh in "npm mode" (HORSE_FROM_NPM) so all the
# battle-tested setup — fetch Chrome for Testing, write ~/.config/horse-browser/config
# pointing at THIS install's extension/, sync the bh_open helpers if browser-harness is
# present — lives in exactly one place. npm mode differs only in what npm already owns
# or what a silent install shouldn't touch: it skips the ~/.local/bin launcher symlink
# (npm's "bin" field handles it), skips the ~/.claude SKILL symlink, and doesn't launch
# the browser / smoke-test on install (HORSE_SKIP_LAUNCH).
#
# Policy: browser-harness is a HARD prerequisite — if it's missing we FAIL the npm install
# loudly (horse-browser can't drive the browser without it). The stable SKILL copy must
# exist afterwards too, or we fail. Softer hiccups (an offline Chrome fetch) still degrade
# to a printed next-step — `horse-browser update` re-fetches Chrome later.
set -u

PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PKG" || exit 0   # cwd = package root so `npx @puppeteer/browsers` resolves the bundled fetcher

# HARD prerequisite — browser-harness must be installed first. Fail the npm install loudly
# (don't degrade) so it's unmistakable: horse-browser can't drive the browser without it.
if ! command -v browser-harness >/dev/null 2>&1; then
  echo "" >&2
  echo "✗ @pa1nd/horse-browser requires browser-harness, which isn't installed." >&2
  echo "  Install it first, then reinstall:" >&2
  echo "      uv tool install browser-harness       # or: pipx install browser-harness" >&2
  echo "      npm i -g @pa1nd/horse-browser" >&2
  echo "  (https://github.com/browser-use/browser-harness)" >&2
  exit 1
fi

echo "horse-browser · setting up (Chrome for Testing + config)…"
# install.sh writes the stable SKILL copy up front (before the Chrome fetch), so it exists
# even if Chrome degrades — a failed Chrome fetch is soft, finished by 'horse-browser update'.
HORSE_FROM_NPM=1 HORSE_SKIP_LAUNCH=1 bash "$PKG/install.sh" || \
  echo "horse-browser · Chrome fetch didn't complete — run 'horse-browser update' to finish it." >&2

# HARD requirement — the CLAUDE.md @-import depends on this stable copy existing.
SKILL_COPY="$HOME/.config/horse-browser/skill.md"
if [ ! -f "$SKILL_COPY" ]; then
  echo "" >&2
  echo "✗ horse-browser: required SKILL copy missing ($SKILL_COPY) — install aborted." >&2
  echo "  Re-run:  bash \"$PKG/claude-md.sh\" skill" >&2
  exit 1
fi

cat <<EOF

horse-browser installed. Next:
  • Start it (launches the browser, no focus steal):   horse-browser
  • Sign into your apps once — logins persist for every agent.
  • Teach agents the bh_open discipline — add this line to your ~/.claude/CLAUDE.md
    (a stable path, independent of your Node version):
      @~/.config/horse-browser/skill.md
EOF
exit 0
