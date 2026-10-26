-- rex-goto: fuzzy-search every Rex pane's scrollback and agent transcript. See README.md for setup.
local M = {}

-- Rex may load this without a file name, so setup also takes the checkout as opts.root. Only an absolute
-- module path gives a usable root: a relative one would resolve against the new pane's directory.
local default_root = (debug.getinfo(1, "S").source or ""):match("^@(/.*)/lua/[^/]*$")

-- Options that only the Lua side reads; the rest become the script's long flags (_ becomes -).
local lua_only = { action = true, python = true, bounds = true, root = true }

-- The client is the app that shows the chosen pane's session when the picker goes to it.
local function command_for(opts, ctx)
  local root = opts.root or default_root
  if not root then
    error("rex-goto: pass root, the checkout directory, to setup", 0)
  end
  local command = { opts.python or "python3", root .. "/rex_goto.py" }
  local names = {}
  for name in pairs(opts) do
    if not lua_only[name] then
      names[#names + 1] = name
    end
  end
  table.sort(names)
  for _, name in ipairs(names) do
    table.insert(command, "--" .. name:gsub("_", "-"))
    table.insert(command, tostring(opts[name]))
  end
  if ctx.client_id then
    table.insert(command, "--client")
    table.insert(command, ctx.client_id)
  end
  table.insert(command, ctx.session_id)
  table.insert(command, ctx.block_id)
  return command
end
M.command_for = command_for

-- Defines the action (default name "rex_goto"); bind it like any other action.
function M.setup(opts)
  opts = opts or {}
  rex.action({
    name = opts.action or "rex_goto",
    title = "Search All Panes and Agent Transcripts",
    keywords = "rex-goto search find grep fuzzy fzf scrollback history transcript agent pi claude codex goto jump",
    run = function(ctx)
      if not ctx.block_id then
        error("no focused pane", 0)
      end
      local _, err = rex.session.new_layer({
        bounds = opts.bounds or { 0.05, 0.05, 0.9, 0.9 },
        focus = true,
        layout = rex.layout.block({
          flavor = "com.superlogical.terminal.shell",
          options = { command = command_for(opts, ctx) },
        }),
      })
      if err ~= nil then
        error(err, 0)
      end
    end,
  })
end

return M
