# horse-browser (Codex)

<!-- Wire this into Codex by appending it to ~/.codex/AGENTS.md (global) or your
     project's AGENTS.md:  cat integrations/codex/AGENTS.md >> ~/.codex/AGENTS.md
     One-time setup: this repo's ./install.sh.

     Sandbox requirement (verified on codex-cli 0.142.5): the default seatbelt
     sandbox cannot drive the browser (no localhost, no ps, not even heredoc temp
     files — commands fail immediately and legibly). Two working setups:

       codex --sandbox danger-full-access

     or workspace-write with network AND the two horse-browser config dirs made
     writable (the daemon keeps its runtime files there — absolute paths, no ~):

       codex --sandbox workspace-write \
         -c sandbox_workspace_write.network_access=true \
         -c 'sandbox_workspace_write.writable_roots=["/Users/YOU/.config/browser-harness","/Users/YOU/.config/horse-browser"]'

     (equivalently persist both keys under [sandbox_workspace_write] in
     ~/.codex/config.toml). Network alone is NOT enough — the daemon fails to
     start without the writable roots. -->

A dedicated, persistent browser for agents. Your session automatically gets its own
coloured tab group and driver — nothing to configure. Drive it with heredoc scripts:

```bash
horse-browser <<'PY'
tid = open_tab("https://example.com")   # open into YOUR tab group, no focus steal
wait_for_load()
print(page_info())
PY
```

Rules:

- **Open tabs with `open_tab(url)`, never `open_tab` / bare `goto_url`** — bare
  `goto_url` navigates whatever tab is focused and clobbers other agents' (and the
  human's) work. To re-navigate your own tab: `switch_tab(tid)` then `goto_url(url)`.
- **Never launch a browser yourself** — `horse-browser` (bare, no stdin) brings the
  dedicated browser up, heals it if wedged, and no-ops if healthy.
- **Trusted input**: drive forms with `click(css)` / `type_into(css, text)` /
  `press("Enter")` — real key/mouse events, so page listeners actually run. Never
  `el.value = …` or `el.click()` in `js(...)` on anything that matters (login,
  checkout, bot-protected sites). Easy captcha gestures: `solve_challenge()`.
- **Screenshots**: `capture_screenshot()` returns a fresh PNG path per call.
- **Tab discipline**: `list_tabs()` shows your group; when a task ends, close what you
  opened via `cdp("Target.closeTarget", targetId=t["targetId"])` per tab.
