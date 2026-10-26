# rex-goto

Fuzzy search over every Rex pane's scrollback and agent transcript. See `README.md` for what it does and how users set it up.

## Layout

- `rex_goto.py`: everything but the action. Rex over the `rex` CLI (`terminal_blocks`, `scrollback`, `go_to`), agent detection (`process_agent`, `block_agent`, `resumed_id`), transcript matching and rendering (`assign_transcripts`, `render`), buffers and rows (`build`, `write_buffers`), and the fzf picker (`pick`). The `rows`, `cycle` and `preview` subcommands are what fzf calls back. Standard library only, Python 3.10+.
- `lua/rex_goto.lua`: the Rex module. `setup(opts)` defines the action, which opens the script in a floating layer; `command_for` turns options into the script's flags (option `foo_bar` becomes `--foo-bar`) and passes the client, session and block. It runs in Rex's server Lua (Lua 5.1, gopher-lua), so it must stay plain 5.1.
- `tests/test_rex_goto.py`: unittest. `PickTests` runs the whole picker against a fake `rex` and `fzf`; the rest are unit tests. `tests/test_lua.lua`: stubs the `rex` global and checks the action.

## Behavior to keep

- Each run writes its buffers to a private temporary directory, removed when the picker exits. A buffer file's first line is its title; its lines follow, so buffer line N is file line N+1.
- A row is `buffer, line, proximity, source, label, text`, tab-separated. fzf shows fields 5 and 6 and searches only field 6; keep `--with-nth` and `--nth` in step with the row.
- Agent detection follows Agentmux's `processAgent` and `blockAgent` (`agentmux/src/rex.mjs` in the dotfiles); keep them in step.
- Transcript matching is a best guess: an explicit conversation on the command line, else the agent's directory and start time. Don't read other programs' state to do it.
- New `setup` options need a matching argparse flag, a README row, and a test.

## Checks

```sh
python3 -m unittest discover -s tests -v
lua tests/test_lua.lua
ruff check . && ruff format --check .
```

For a live check, create a throwaway session (`rex new`), open the script with `rex api call session.new_layer` as the Lua action does, drive fzf with `rex send` and `rex send-key`, and destroy the session afterwards. Don't use the user's visible sessions.

## Commits and releases

- Use Conventional Commits: `<type>(<scope>): <description>` with `feat`, `fix`, `refactor`, `docs`, `test`, `chore` or `ci`.
- Each push to `main` runs CI; when it passes, `feat`, `fix` and breaking commits (`!` or `BREAKING CHANGE`) since the last tag produce a new tag and GitHub release. Other types don't release.
- Use generic names and fixtures in tests and docs; never reference real projects, hosts or people.
