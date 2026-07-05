# horse-browser (Codex)

<!-- Wire this into Codex by appending it to ~/.codex/AGENTS.md (global) or your
     project's AGENTS.md:  cat integrations/codex/AGENTS.md >> ~/.codex/AGENTS.md
     Requires a session that can reach localhost (the default seatbelt sandbox
     cannot): run codex with network-enabled workspace-write or
     `--sandbox danger-full-access`. One-time setup: this repo's ./install.sh. -->

A dedicated, persistent browser for agents. Your session automatically gets its own
coloured tab group and driver — nothing to configure. Drive it with heredoc scripts:

```bash
horse-browser <<'PY'
tid = bh_open("https://example.com")   # open into YOUR tab group, no focus steal
wait_for_load()
print(page_info())
PY
```

Rules:

- **Open tabs with `bh_open(url)`, never `new_tab` / bare `goto_url`** — bare
  `goto_url` navigates whatever tab is focused and clobbers other agents' (and the
  human's) work. To re-navigate your own tab: `bh_switch_tab(tid)` then `goto_url(url)`.
- **Never launch a browser yourself** — `horse-browser` (bare, no stdin) brings the
  dedicated browser up, heals it if wedged, and no-ops if healthy.
- **Trusted input**: drive forms with `click(css)` / `type_into(css, text)` /
  `press("Enter")` — real key/mouse events, so page listeners actually run. Never
  `el.value = …` or `el.click()` in `js(...)` on anything that matters (login,
  checkout, bot-protected sites). Easy captcha gestures: `solve_challenge()`.
- **Screenshots**: `capture_screenshot()` returns a fresh PNG path per call.
- **Tab discipline**: `bh_list()` shows your group; when a task ends, close what you
  opened via `cdp("Target.closeTarget", targetId=t["targetId"])` per tab.
