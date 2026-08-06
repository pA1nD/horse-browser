#!/usr/bin/env bash
# claude-md.sh — front door for the rule file, kept for clones and for muscle memory.
#
# The rule (~/.claude/rules/horse-browser.md) is ON by default and kept current by the
# launcher's reconcile_external on every run, alongside the two hook files — so there is
# nothing to "install" any more, and nothing to re-run after an upgrade. This script exists
# because `./claude-md.sh apply` is in the README, in older setup notes, and in habit.
#
# One implementation, in bin/horse-browser (`rule`), which is where the config toggle and the
# reconciler already live. Two copies of "what the rule file should contain" is how the
# launcher once ended up wiring nothing at all.
#
#   ./claude-md.sh on | apply    manage the rule file (the default state)
#   ./claude-md.sh off           remove it and stop rewriting it
#   ./claude-md.sh print         the exact content, to stdout
#   ./claude-md.sh check         is the installed copy current? (exit 0=yes, 1=drifted)
#   ./claude-md.sh status        on/off, and where it goes
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$HERE/bin/horse-browser" rule "${@:-status}"
