# rex-goto

[![CI](https://github.com/sfrieds3/rex-goto/actions/workflows/ci.yml/badge.svg)](https://github.com/sfrieds3/rex-goto/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/sfrieds3/rex-goto?sort=semver)](https://github.com/sfrieds3/rex-goto/releases)

Fuzzy-search every pane in [Rex](https://www.superlogical.com/rex) at once. rex-goto gathers the scrollback of each terminal pane in every session, plus the transcript of each Pi, Claude Code or Codex agent running in one, and lists the lines in [fzf](https://github.com/junegunn/fzf). The preview shows the match in its buffer, centred and scrollable. Open the whole buffer at that line, or go to the pane it came from.

```text
┌──────────────────────────────────────────────────────────────────────┐
│ all/all> trace                                                       │
│   3/5120 ─────────────────────────────────────────────────────────── │
│ ▌ ops:2 scroll    Traceback (most recent call last):                 │
│   dev:1 claude    ▌ tool: Bash                                       │
│ ╭──────────────────────────────────────────────────────────────────╮ │
│ │ ops · window 2 logs · scrollback · block:…                 41/90 │ │
│ │ 40 $ ./run-job                                                   │ │
│ │ 41 Traceback (most recent call last):                            │ │
│ │ 42   File "job.py", line 12, in <module>                         │ │
│ ╰──────────────────────────────────────────────────────────────────╯ │
└──────────────────────────────────────────────────────────────────────┘
```

## Features

- Searches every placed terminal pane in every Rex session. Floating layers, this picker's among them, are left out.
- Agent panes also get their transcript: user and assistant turns, tool calls and tool results (each result clipped to 200 lines), so you can find what scrolled away or never showed on screen.
- Rows start with the pane the picker opened from, then its window, its session, and the rest; within a pane, the newest line comes first. fzf matches only the line's text, not the `session:window source` label.
- The preview shows the whole buffer with line numbers and the match in reverse video, centred. Shift-Up/Down scroll it.
- Ctrl-R narrows the scope to this session or this window; Ctrl-S narrows the source to scrollback or agent transcripts. The list reloads in place.

## Requirements

- Rex with Lua configuration (`~/.config/rex/init.lua`)
- Python 3.10 or newer (standard library only)
- [fzf](https://github.com/junegunn/fzf) 0.45 or newer (for `transform`) and `rex` on the pane's `PATH` (Rex adds `rex` to the panes it starts)

## Install

Clone the repository, or download a [release](https://github.com/sfrieds3/rex-goto/releases):

```sh
git clone https://github.com/sfrieds3/rex-goto ~/code/rex-goto
```

Load it from `~/.config/rex/init.lua` and bind the `rex_goto` action:

```lua
local root = os.getenv("HOME") .. "/code/rex-goto"
package.path = root .. "/lua/?.lua;" .. package.path
require("rex_goto").setup({ root = root })

rex.bind("cmd+shift+f", "rex_goto")
```

Then run `rex config reload`. The action also shows up in the command palette as "Search All Panes and Agent Transcripts". It opens fzf in a floating layer over the current tab; when you finish, the layer closes.

## Usage

| Key | Action |
| --- | --- |
| Enter | open the buffer at the matching line in `editor` (`nvim -R`), inside the layer |
| Alt-Enter, Ctrl-O | go to the pane: show its session and window, and focus it |
| Ctrl-Y | copy the line |
| Ctrl-R | next scope: all, session, window |
| Ctrl-S | next source: all, scroll, agent |
| Shift-Up/Down | scroll the preview |
| Ctrl-/ | toggle the preview |
| Esc, Ctrl-C | cancel |

fzf's search syntax works as usual: start a term with `'` for an exact match, or `^`/`$` to anchor it.

### Which transcript belongs to a pane

rex-goto finds the outermost agent in each pane's process chain, the same way Agentmux does, then picks its transcript:

1. If the agent's command line names a conversation (`claude --resume ID`, `claude --session-id ID`, `codex resume ID`, `pi --session FILE`), that one.
2. Otherwise, a transcript started in the agent's working directory and written since the agent started. When several agents share a directory, the most recently started one takes the most recently written transcript.

Transcripts are read from `~/.pi/agent/sessions`, `~/.claude/projects` and `~/.codex/sessions`; `PI_AGENT_DIR` (or `PI_CODING_AGENT_DIR`), `CLAUDE_CONFIG_DIR` and `CODEX_HOME` move them. Subagent transcripts and `codex exec` runs are skipped.

## Configuration

Pass options to `setup`:

```lua
require("rex_goto").setup({
  root = root,
  scope = "session",
  max_lines = 5000,
  preview_window = "right,55%",
  editor = "less -R",
})
```

| Option | Default | Notes |
| --- | --- | --- |
| `root` | | Checkout directory. Required: Rex doesn't give modules their file name. |
| `action` | `rex_goto` | Name of the action to define. Call `setup` twice for two variants. |
| `python` | `python3` | Interpreter for `rex_goto.py`. |
| `bounds` | `{ 0.05, 0.05, 0.9, 0.9 }` | Layer position and size as fractions of the window: x, y, width, height. |
| `scope` | `all` | Starting scope: `all`, `session` or `window`. |
| `source` | `all` | Starting source: `all`, `scroll` or `agent`. |
| `max_lines` | `20000` | Newest lines kept per buffer. |
| `transcripts` | `true` | Search agent transcripts. `false` searches scrollback only. |
| `editor` | `nvim -R` | Command Enter runs as `editor +LINE FILE`. `$REX_GOTO_EDITOR` changes the default. |
| `clip_tool` | | Copy command. Without it, `pbcopy`, then `wl-copy` (with `WAYLAND_DISPLAY`) or `xclip` (with `DISPLAY`), else OSC 52. |
| `fzf_tool` | `fzf` | fzf command. |
| `fzf_layout` | `reverse` | fzf `--layout`. |
| `fzf_unset_default_opts` | `true` | Ignore `FZF_DEFAULT_OPTS` and `FZF_DEFAULT_OPTS_FILE`. |
| `preview_window` | `down,60%` | Position and size part of fzf's `--preview-window`; rex-goto adds the header line and the centring. |

## Development

```sh
python3 -m unittest discover -s tests -v
lua tests/test_lua.lua
ruff check . && ruff format --check .
```

## License

MIT
