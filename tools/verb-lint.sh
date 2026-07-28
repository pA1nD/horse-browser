#!/usr/bin/env bash
#
# verb-lint.sh — find STALE horse-browser verb names across the fleet.
#
# horse-browser renamed many Python "verbs". The old names still work (deprecation
# aliases warn + forward), but callers should migrate. Scripts across the fleet hard-
# code these verb names as an implicit API. This lint finds the stale callers.
#
# Usage:
#   tools/verb-lint.sh                 # scan the default roots (see ROOTS below)
#   tools/verb-lint.sh /path [/path…]  # scan given roots instead
#
# Output: real stale callers, grouped by file, as
#   path:line: OLD -> NEW   | <offending line, trimmed>
# then a separate "likely-doc" section (prose/catalog/docstring mentions it skipped).
#
# Exit status: non-zero if any REAL stale caller is found, zero if clean (CI-usable).
# likely-doc matches never affect the exit status. Set VERB_LINT_DOCS=0 to hide them.
#
# Portable: bash 3.2 (stock macOS) + ripgrep, falling back to grep when rg is absent.
#
set -u

# ─────────────────────────────────────────────────────────────────────────────
# RENAME MAP — one "OLD NEW" pair per line. Add new renames here as they land.
# ─────────────────────────────────────────────────────────────────────────────
RENAME_MAP='
bh_open        open_tab
bh_list        list_tabs
bh_switch_tab  switch_tab
new_tab        open_tab
click_at_xy    click_xy
fill_input     type_into
press_key      press
hb_type_secret type_secret
hb_type_totp   type_totp
hb_get_secret  get_secret
hb_get_totp    get_totp
hb_creds       creds
hb_scroll_to   scroll_to
hb_shot        shot
'

# Scan roots (override by passing paths as arguments).
if [ "$#" -gt 0 ]; then
  ROOTS=("$@")
else
  ROOTS=("$HOME/pro" "$HOME/.config/browser-harness/agent-workspace")
fi

# This script's own path — skip it so the RENAME_MAP below never self-reports.
SELF=$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")

# Directories never worth scanning.
EXCLUDES="node_modules .venv venv .git __pycache__ dist build .next coverage"

# File extensions to scan: source (where callers live) + docs (for the likely-doc
# bucket). Data files (.json/.jsonl) and everything else are skipped as noise.
EXTS="py sh js mjs cjs jsx ts tsx md mdx txt"

# Skip files bigger than this (KB): stale callers are small scripts; big files are
# generated/minified bundles — slow to grep and pure noise. rg gets the same cap.
MAXKB=2048

# ── parse the rename map into parallel arrays (no assoc arrays — bash 3.2) ─────
OLDS=(); NEWS=(); ALT=""
while read -r old new; do
  [ -z "${old:-}" ] && continue
  OLDS+=("$old"); NEWS+=("$new")
  ALT="${ALT:+$ALT|}$old"
done <<EOF
$RENAME_MAP
EOF

# ── run the search into a temp file ──────────────────────────────────────────
# Primary: a REAL ripgrep binary (instant, .gitignore-aware — this is the CI path).
# Fallback: portable `find -prune` + `grep`, scoped to EXTS so it stays quick even
# without rg. NB: on a Claude Code shell, `rg`/`grep`/`find` are shell *functions*
# that don't exist inside a plain-bash script — so we test for a real rg binary and
# otherwise drive the real find/grep directly.
TMP=$(mktemp "${TMPDIR:-/tmp}/verb-lint.XXXXXX") || exit 2
trap 'rm -f "$TMP"' EXIT

if command -v rg >/dev/null 2>&1; then
  args=()
  for d in $EXCLUDES; do args+=(-g "!**/$d/**"); done
  for e in $EXTS;     do args+=(-g "*.$e");       done
  rg -n --no-heading -w --max-filesize "${MAXKB}K" "${args[@]}" \
     -e "($ALT)" "${ROOTS[@]}" >"$TMP" 2>/dev/null
else
  # build the find predicate from EXCLUDES / EXTS
  FIND_ARGS=("${ROOTS[@]}" '(')
  first=1
  for d in $EXCLUDES; do [ $first = 1 ] || FIND_ARGS+=(-o); first=0; FIND_ARGS+=(-name "$d"); done
  FIND_ARGS+=(')' -prune -o -type f -size "-${MAXKB}k" '(')
  first=1
  for e in $EXTS; do [ $first = 1 ] || FIND_ARGS+=(-o); first=0; FIND_ARGS+=(-name "*.$e"); done
  FIND_ARGS+=(')' -print0)
  find "${FIND_ARGS[@]}" 2>/dev/null | xargs -0 grep -IEnwH -e "$ALT" >"$TMP" 2>/dev/null
fi

# ── helpers (fork-free: set globals, never called via $(…) in the hot loop) ───
BQ=$(printf '\140')          # a literal backtick, safely (no command substitution)
QCLASS="[${BQ}\"']"          # char class: backtick / double / single quote

# word-boundary membership: does OLD appear as a whole token in the content?
has_word() {
  local toks=" ${2//[^A-Za-z0-9_]/ } "
  case "$toks" in *" $1 "*) return 0;; *) return 1;; esac
}

# classify (old,new,content,ext) -> sets VERDICT to: drop | doc | real
classify() {
  local old=$1 new=$2 c=$3 ext=$4 t
  VERDICT=real

  # DROP — legitimate shim DEFINITIONS, never real callers:
  #   a rename-map pair  "OLD": "NEW"   (helpers.py _RENAMED, broker _renamed map)
  if [[ $c =~ ${QCLASS}${old}${QCLASS}[[:space:]]*:[[:space:]]*${QCLASS}${new}${QCLASS} ]]; then
    VERDICT=drop; return; fi
  #   a def/function that DEFINES the old name (an un-migrated source copy)
  if [[ $c =~ (^|[^A-Za-z0-9_])(def|function)[[:space:]]+${old}([^A-Za-z0-9_]|$) ]]; then
    VERDICT=drop; return; fi

  # LIKELY-DOC — mentions to surface separately, not real callers:
  case "$ext" in md|mdx|markdown|txt|json|jsonl) VERDICT=doc; return;; esac       # docs / data
  case "$c" in *"->"*|*"→"*) VERDICT=doc; return;; esac                           # migration prose
  t=${c#"${c%%[![:space:]]*}"}                                                    # ltrim
  case "$t" in \#*|//*|\**|/\**|"<!--"*) VERDICT=doc; return;; esac               # comment line
  if [[ $c =~ ${QCLASS}${old} ]]; then VERDICT=doc; return; fi                    # `OLD` / 'OLD' reference
  if [[ $c =~ ${old}\([\"\'][\<\{] ]]; then VERDICT=doc; return; fi               # example call OLD("<…"/"{…"
  if [[ ! $c =~ ${old}\( ]]; then                                               # bare mention (not a call)
    [[ $c =~ (^|[^A-Za-z0-9_])import([^A-Za-z0-9_]) ]] && { VERDICT=real; return; }
    VERDICT=doc; return
  fi
}

# ── scan + classify ──────────────────────────────────────────────────────────
real_hits=(); doc_hits=()
n=${#OLDS[@]}

while IFS= read -r rec; do
  [ -z "$rec" ] && continue
  path=${rec%%:*}; rest=${rec#*:}
  [ "$path" = "$SELF" ] && continue          # never lint this script's own rename map
  lineno=${rest%%:*}; content=${rest#*:}
  content=${content:0:400}   # cap first: a stale CALLER is a short line; long lines are minified/data noise
  ext=${path##*.}; [ "$ext" = "$path" ] && ext=""

  i=0
  while [ "$i" -lt "$n" ]; do
    old=${OLDS[$i]}; new=${NEWS[$i]}; i=$((i + 1))
    has_word "$old" "$content" || continue
    classify "$old" "$new" "$content" "$ext"
    [ "$VERDICT" = drop ] && continue
    trimmed=${content#"${content%%[![:space:]]*}"}; trimmed=${trimmed:0:200}
    row=$(printf '%s\t%06d\t%s -> %s\t%s' "$path" "$lineno" "$old" "$new" "$trimmed")
    if [ "$VERDICT" = real ]; then real_hits+=("$row"); else doc_hits+=("$row"); fi
  done
done < "$TMP"

# ── print, grouped by file ───────────────────────────────────────────────────
print_group() {   # reads sorted TAB rows: path\tline\tOLD -> NEW\tcontent
  local last="" path line pair body
  while IFS=$'\t' read -r path line pair body; do
    line=$((10#$line))
    [ "$path" != "$last" ] && { [ -n "$last" ] && echo; last=$path; }
    printf '%s:%s: %-26s | %s\n' "$path" "$line" "$pair" "$body"
  done
}

if [ "${#real_hits[@]}" -gt 0 ]; then
  echo "STALE horse-browser verb callers (${#real_hits[@]}):"
  echo
  printf '%s\n' "${real_hits[@]}" | LC_ALL=C sort | print_group
else
  echo "clean — no stale horse-browser verb callers."
fi

if [ "${VERB_LINT_DOCS:-1}" != 0 ] && [ "${#doc_hits[@]}" -gt 0 ]; then
  echo
  echo "── likely-doc (excluded — prose / docstrings / catalogs; review, don't fail CI) ──"
  echo
  printf '%s\n' "${doc_hits[@]}" | LC_ALL=C sort | print_group
fi

[ "${#real_hits[@]}" -eq 0 ]
