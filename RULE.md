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
tid = bh_open("https://example.com")   # your tab, in your colour group; no focus steal
wait_for_load()
print(page_info())
PY
```

Go through `horse-browser` — never launch Chrome yourself. At end of task, prune your
tabs: `for t in bh_list(): cdp("Target.closeTarget", targetId=t["targetId"])`.

## Verbs — the paved path (everything else: raw `cdp`)

`bh_open(url)` open/reuse your tab · `goto_url(url)` navigate it · `bh_list()` your tabs ·
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

Full manual — every verb's CDP recipe, extension internals, challenge playbook, gotchas:
`horse-browser skill`.
