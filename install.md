# Installing test-brave

Prereqs: macOS, [Brave Browser](https://brave.com) at `/Applications/Brave Browser.app`.

## 1. Clone

```bash
git clone https://github.com/pa1nd/test-brave ~/pro/test-brave
```

## 2. Launch

```bash
~/pro/test-brave/bin/test-brave
```

This opens Brave on a dedicated profile (`~/.config/test-brave`) with remote
debugging on `:9223` and the tab-grouper extension loaded. Point your CDP
client at it:

```bash
export BU_CDP_URL=http://127.0.0.1:9223
```

The extension loads via `--load-extension` at launch — no manual
`brave://extensions` step. (Brave may show a "developer mode extensions"
notice; that's expected for an unpacked extension.)

## 3. (Optional) wire it into browser-harness

To get `bh_open` / `bh_switch_tab` / `bh_list` on every browser-harness call,
copy the helper into browser-harness's auto-loaded workspace file:

```bash
cp ~/pro/test-brave/agent-helpers/bh.py \
   ~/Developer/browser-harness/agent-workspace/agent_helpers.py
```

(Symlink instead if you'd rather track updates: `ln -sf …/agent-helpers/bh.py …/agent_helpers.py`.)

Then **always open tabs with `bh_open(url)`**, never raw `new_tab` / `goto_url`
— see [SKILL.md](SKILL.md).

## Updating

```bash
cd ~/pro/test-brave && git pull --ff-only
```

Reload the extension afterward (relaunch `bin/test-brave`, or hit reload in
`brave://extensions`). If you copied `bh.py`, re-copy it; if you symlinked, it's
already current.
