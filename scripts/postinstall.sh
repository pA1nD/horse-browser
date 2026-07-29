#!/usr/bin/env bash
# npm postinstall for @pa1nd/horse-browser.
#
# Runs the package's own install.sh in "npm mode" (HORSE_FROM_NPM) so all the
# battle-tested setup — build the vendored harness venv, fetch Chrome for Testing,
# write ~/.config/horse-browser/config pointing at THIS install's extension/ — lives
# in exactly one place. npm mode differs only in what npm already owns or what a
# silent install shouldn't touch: it skips the ~/.local/bin launcher symlink (npm's
# "bin" field handles it), skips ~/.claude, and doesn't launch the browser /
# smoke-test on install (HORSE_SKIP_LAUNCH).
#
# The harness is VENDORED (harness/horse_harness) — no browser-harness install
# needed; its venv needs uv or python3 ≥3.11 and is (re)built by install.sh, or
# self-heals on the first driver call. Softer hiccups (an offline Chrome fetch)
# degrade to a printed next-step — `horse-browser update` re-fetches Chrome later.
set -u

PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PKG" || exit 0   # cwd = package root so `npx @puppeteer/browsers` resolves the bundled fetcher

echo "horse-browser · setting up (harness venv + Chrome for Testing + config)…"
# install.sh writes the stable SKILL copy up front (before the Chrome fetch), so it exists
# even if Chrome degrades — a failed Chrome fetch is soft, finished by 'horse-browser update'.
HORSE_FROM_NPM=1 HORSE_SKIP_LAUNCH=1 bash "$PKG/install.sh" || \
  echo "horse-browser · Chrome fetch didn't complete — run 'horse-browser update' to finish it." >&2

cat <<EOF

horse-browser installed. Next:
  • Start it (launches the browser, no focus steal):   horse-browser
  • Already running an older version? Apply the update (new launch flags +
    extension) with a tab-preserving relaunch:          horse-browser relaunch
  • Sign into your apps once — logins persist for every agent.
  • Teach agents the paved path — register the rule file
    (~/.claude/rules/horse-browser.md, one self-contained file, no imports):
      horse-browser rule apply
EOF
exit 0
