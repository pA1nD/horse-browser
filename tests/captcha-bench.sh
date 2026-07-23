#!/usr/bin/env bash
# tests/captcha-bench.sh — drive horse-browser against public captcha PLAYGROUNDS (official
# vendor demo widgets) and report, per vendor, whether our realness + challenge-solving stack
# clears it. On-demand, NOT a CI gate: captchas are reputation/IP-based and non-deterministic,
# and hammering live vendors is reputation-affecting — run this to validate, don't loop it.
#
# What "pass" means per class:
#   managed / score (Turnstile managed, reCAPTCHA v3)  -> a token appears with NO interaction;
#                                                          this is what our fingerprint stack buys
#   gesture (checkbox, slider, press-&-hold)           -> solve_challenge() performs the gesture
#   perception (image grid, distorted text, audio)     -> ESCALATE by design — we do NOT solve these
#
# Usage: tests/captcha-bench.sh   (add HB_CAPTCHA_SOLVE=1 to attempt gesture solves on the
#        checkbox demos — off by default to avoid poking live vendors on every run).
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HB="${HB:-$HERE/../bin/horse-browser}"
[ -x "$HB" ] || HB="$(command -v horse-browser || true)"
[ -x "$HB" ] || { echo "FATAL: horse-browser not found (set HB=…)"; exit 1; }
"$HB" >/dev/null 2>&1 || { echo "FATAL: browser did not come up"; exit 1; }

say() { printf '%s\n' "$*"; }
say "horse-browser captcha bench — $(date '+%Y-%m-%d %H:%M')"
say "(managed/score should auto-pass; gesture is attempted; perception escalates by design)"
say ""

SOLVE="${HB_CAPTCHA_SOLVE:-}"
"$HB" <<PY 2>&1 | grep -E "^  "
import time, json

SOLVE = bool("$SOLVE")

# vendor, url, class, and a JS probe returning the pass-token (or "") for that vendor
SITES = [
  ("Cloudflare Turnstile (managed)", "https://demo.turnstile.workers.dev/", "managed",
   "(document.querySelector('[name=cf-turnstile-response]')||{}).value||''"),
  ("reCAPTCHA v3 (score)", "https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php", "score",
   "(document.querySelector('#g-recaptcha-response,[name=g-recaptcha-response]')||{}).value||''"),
  ("reCAPTCHA v2 (checkbox)", "https://www.google.com/recaptcha/api2/demo", "gesture",
   "(document.querySelector('#g-recaptcha-response,[name=g-recaptcha-response]')||{}).value||''"),
  ("hCaptcha (checkbox)", "https://accounts.hcaptcha.com/demo", "gesture",
   "(document.querySelector('[name=h-captcha-response]')||{}).value||''"),
]

tid = None
passed = attempted = escalated = 0
for name, url, klass, token_js in SITES:
    try:
        if tid is None: tid = bh_open(url)
        else: bh_switch_tab(tid); goto_url(url)
        wait_for_load(); time.sleep(6)
        tok = js(token_js) or ""
        if tok:
            print(f"  ✓ {name:34} {klass:8} PASSED (token present, no interaction)")
            passed += 1
            continue
        # no token yet
        if klass in ("managed", "score"):
            print(f"  ✗ {name:34} {klass:8} no token — reputation/IP may be flagged")
            continue
        # gesture class: classify, optionally solve
        cls = solve_challenge(act=False) if 'solve_challenge' in globals() else 'no-solver'
        short = cls.split(' ')[0] if cls else '?'
        if SOLVE and 'solve_challenge' in globals():
            res = solve_challenge(act=True)
            time.sleep(4)
            tok2 = js(token_js) or ""
            if tok2:
                print(f"  ✓ {name:34} {klass:8} SOLVED via gesture ({short})")
                passed += 1
            elif str(res).startswith(('vision:', 'escalate:')):
                print(f"  △ {name:34} {klass:8} ESCALATE — perception step ({str(res)[:40]})")
                escalated += 1
            else:
                print(f"  ✗ {name:34} {klass:8} gesture didn't clear ({str(res)[:40]})")
                attempted += 1
        else:
            print(f"  △ {name:34} {klass:8} present, classified {short} (set HB_CAPTCHA_SOLVE=1 to attempt)")
            attempted += 1
    except Exception as e:
        print(f"  ✗ {name:34} ERROR {str(e)[:50]}")

print(f"  ── auto-passed (managed/score): {passed}   attempted/present: {attempted}   escalated: {escalated}")
if tid:
    try: cdp("Target.closeTarget", targetId=tid)
    except Exception: pass
PY
