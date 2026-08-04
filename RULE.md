# Browsing

A dedicated Chrome you drive over CDP, via `horse-browser`. It's **shared**: other
agents drive their own tabs in it, and your operator is signed into real accounts here.
Two things follow — both handled by the substrate, you don't manage them:

- **Your tab is yours.** Your session is pinned to its own tab; you can't land on
  another agent's page, nor they on yours.
- **No focus theft.** A call that would yank the browser in front of whatever app your
  operator is using (e.g. `Target.activateTarget`) is fulfilled *without* that side
  effect — the tab still activates, the operator keeps their focus.

## Drive it

```
horse-browser <<'PY'
tid = open_tab("https://example.com")   # your tab, in your colour group; no focus steal
wait_for_load()
print(page_info())
PY
```

Go through `horse-browser` — never launch Chrome yourself. At end of task, prune your
tabs: `for t in list_tabs(): cdp("Target.closeTarget", targetId=t["targetId"])`.

## Verbs — the paved path (everything else: raw `cdp`)

`open_tab(url)` open/reuse your tab · `goto_url(url)` navigate it · `list_tabs()` your tabs ·
`page_info()` url/title/viewport · `js(expr)` run JS · `capture_screenshot()` PNG of your
tab · `click(css)`/`click_xy(x,y)` trusted click · `type_into(css,text)` trusted type ·
`press(key)` named key · `press_hold(t,s)`/`drag(t,to=)` challenge gestures ·
`solve_challenge()` classify+solve the easy ones · `wait_for_load()` · `cdp(method,**p)` raw CDP.

## Sharp edges — non-obvious on a shared browser (the rest, compose from raw CDP freely)

- **Input must fire real events.** `type_into`/`click`/`press` send real key/mouse events,
  so the page's listeners run — submit enables, autocomplete fires, React/Vue state updates.
  `el.value=…`, `el.click()`, and `Input.insertText` fire *nothing*: the value *looks* set
  while the page's logic never ran (dead submit, stale state). Breaks plain sites too, not
  just defended ones.
- **Screenshots.** `capture_screenshot()` shoots your tab reliably even backgrounded or
  occluded. A raw `Page.captureScreenshot` on the shared daemon session can hang on an
  occluded window or return a 2×2 — use the helper, or attach a fresh session and pass
  `fromSurface=True`.
- **Challenges: solve, don't reload.** A press-hold / slide / checkbox is a reputation
  checkpoint. `solve_challenge()` (or a trusted gesture) banks IP + cookie trust that pays
  forward across the session; reloading to reroll banks nothing and reads as a bot.
- **Your tab is foreground only while you're driving it.** The page is told it's focused for
  as long as you keep calling, then goes hidden about a minute after you stop (and drops its
  🐴) so an abandoned tab doesn't burn CPU forever. Your next call takes it back before the
  page is touched, so this is invisible — *except* if you wait with a bare `sleep`: the page
  throttles its timers while you do. Poll with `wait_for_load()` / `wait_for_network_idle()`,
  which renew it, or raise `HORSE_BROWSER_FOCUS_TTL`.

## Make it yours — you can write verbs

The verbs above are a floor, not a ceiling. A raw-CDP sequence you'll reuse becomes a named
verb: add it to `agent_helpers.py` in your workspace
(`~/.config/browser-harness/agent-workspace/`). It loads last, so it always wins, and it's
there next session. Agents rarely do this — do it.

## Site skills — a per-site playbook

A site's selectors, quirks, and your own helpers for it live in a skill file:
`<workspace>/domain-skills/<domain.tld>/<name>.md`. On navigation the harness tells you when a
host has one — read it before working the page. Visited a site a few times with no skill while
building reusable things for it? Write one. Host is `domain.tld` (www/subdomain/path ignored).

Full manual — every verb's CDP recipe, extension internals, challenge playbook, gotchas:
`horse-browser skill`.
