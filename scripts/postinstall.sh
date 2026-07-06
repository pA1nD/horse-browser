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
# This must NEVER fail `npm install`: a missing browser-harness, an offline Chrome fetch,
# or any hiccup degrades to a printed next-step, not a broken install. `horse-browser
# update` re-fetches Chrome later; re-running setup syncs helpers once browser-harness is in.
set -u

PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PKG" || exit 0   # cwd = package root so `npx @puppeteer/browsers` resolves the bundled fetcher

echo "horse-browser · setting up (Chrome for Testing + config)…"
HORSE_FROM_NPM=1 HORSE_SKIP_LAUNCH=1 bash "$PKG/install.sh" || \
  echo "horse-browser · setup didn't fully complete — run 'horse-browser update' to fetch Chrome." >&2

cat <<EOF

horse-browser installed. Next:
  • Start it (launches the browser, no focus steal):   horse-browser
  • Sign into your apps once — logins persist for every agent.
  • Prereq for driving it: browser-harness (a Python tool) —
      uv tool install browser-harness      # or: pipx install browser-harness
  • Teach agents the bh_open discipline — add this line to your ~/.claude/CLAUDE.md
    (a stable path, independent of your Node version):
      @~/.config/horse-browser/skill.md
EOF
exit 0
