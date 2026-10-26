-- Run from the repository root with lua5.1, luajit or nvim -l: lua tests/test_lua.lua
local defined, layers = {}, {}
rex = {
  action = function(def)
    defined[def.name] = def
  end,
  layout = {
    block = function(block)
      return { block = block }
    end,
  },
  session = {
    new_layer = function(args)
      layers[#layers + 1] = args
      return { block_ids = { "block:picker" } }
    end,
  },
}
package.path = "lua/?.lua;" .. package.path
local rex_goto = require("rex_goto")

local function same(actual, expected)
  assert(#actual == #expected, ("length %d, expected %d: %s"):format(#actual, #expected, table.concat(actual, " ")))
  for index = 1, #expected do
    assert(actual[index] == expected[index], ("[%d] %s, expected %s"):format(index, tostring(actual[index]), expected[index]))
  end
end

local ctx = { session_id = "session:a", block_id = "block:b", client_id = "client:c" }

-- Flags come out sorted; Lua-only options are left out and values become strings. The client comes last.
same(
  rex_goto.command_for({
    root = "/opt/rex-goto",
    action = "find",
    python = "/usr/bin/python3",
    bounds = { 0, 0, 1, 1 },
    scope = "session",
    max_lines = 500,
    transcripts = false,
  }, ctx),
  {
    "/usr/bin/python3", "/opt/rex-goto/rex_goto.py",
    "--max-lines", "500",
    "--scope", "session",
    "--transcripts", "false",
    "--client", "client:c",
    "session:a", "block:b",
  }
)

-- Without a client there is no --client flag.
same(rex_goto.command_for({ root = "/r" }, { session_id = "s", block_id = "b" }), { "python3", "/r/rex_goto.py", "s", "b" })

-- Loaded by a relative path, the module can't know its root and asks for one.
local ok, err = pcall(rex_goto.command_for, {}, ctx)
assert(not ok and err:find("pass root"), "a relative load needs root")

-- Loaded by an absolute path, the module finds its own root.
local checkout = assert(os.getenv("PWD"), "run from the repository root")
local absolute = dofile(checkout .. "/lua/rex_goto.lua")
same(absolute.command_for({}, { session_id = "s", block_id = "b" }), { "python3", checkout .. "/rex_goto.py", "s", "b" })

rex_goto.setup({ root = "/r" })
local action = assert(defined.rex_goto, "setup defines the rex_goto action")
assert(not pcall(action.run, {}), "fails without a focused pane")
action.run(ctx)
local layer = assert(layers[1], "the action opens a layer")
assert(layer.focus, "the layer takes focus")
same(layer.layout.block.options.command, { "python3", "/r/rex_goto.py", "--client", "client:c", "session:a", "block:b" })

rex_goto.setup({ root = "/r", action = "find" })
assert(defined.find, "action renames the action")

print("ok")
