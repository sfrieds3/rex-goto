import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

import rex_goto

ID_A = "11111111-1111-4111-8111-111111111111"
ID_B = "22222222-2222-4222-8222-222222222222"
ID_C = "33333333-3333-4333-8333-333333333333"


def write_jsonl(path, records, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as out:
        for record in records:
            out.write(json.dumps(record) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class AgentTests(unittest.TestCase):
    def test_process_agent(self):
        self.assertEqual(rex_goto.process_agent({"name": "claude"}), "claude")
        self.assertEqual(rex_goto.process_agent({"name": "node", "argv0": "/usr/bin/codex"}), "codex")
        self.assertEqual(rex_goto.process_agent({"name": "node", "script": "/lib/pi-coding-agent/dist/cli.js"}), "pi")
        self.assertIsNone(rex_goto.process_agent({"name": "zsh"}))
        self.assertIsNone(rex_goto.process_agent(None))

    def test_outermost_agent_owns_the_block(self):
        process = {
            "foreground": {"pid": 30, "name": "codex", "cwd": "/work/inner"},
            "ancestors": [{"pid": 20, "name": "claude", "cwd": "/work/app"}, {"pid": 10, "name": "zsh"}],
        }
        self.assertEqual(rex_goto.block_agent(process), {"agent": "claude", "pid": 20, "cwd": "/work/app"})
        self.assertIsNone(rex_goto.block_agent({"foreground": {"pid": 5, "name": "zsh"}}))

    def test_resumed_id(self):
        self.assertEqual(rex_goto.resumed_id("claude", ["claude", "--resume", ID_A]), ID_A)
        self.assertEqual(rex_goto.resumed_id("claude", ["claude", "-r", ID_A]), ID_A)
        self.assertEqual(rex_goto.resumed_id("claude", ["claude", f"--session-id={ID_A}"]), ID_A)
        self.assertEqual(rex_goto.resumed_id("codex", ["codex", "resume", ID_B]), ID_B)
        self.assertEqual(rex_goto.resumed_id("pi", ["node", "pi", "--session", "/s/x.jsonl"]), "/s/x.jsonl")
        self.assertIsNone(rex_goto.resumed_id("claude", ["claude"]))


class RenderTests(unittest.TestCase):
    def test_claude(self):
        records = [
            {"type": "user", "cwd": "/w", "message": {"role": "user", "content": "fix the build"}},
            {"type": "user", "isMeta": True, "message": {"content": "hidden"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "secret"},
                        {"type": "text", "text": "Running tests."},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "make test"}},
                    ]
                },
            },
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "1 failed\nok"}]}},
            {"type": "assistant", "isSidechain": True, "message": {"content": "subagent"}},
        ]
        self.assertEqual(
            rex_goto.render("claude", records),
            [
                "▌ user",
                "fix the build",
                "",
                "▌ assistant",
                "Running tests.",
                "",
                "▌ tool: Bash",
                '{"command": "make test"}',
                "",
                "▌ result",
                "1 failed",
                "ok",
                "",
            ],
        )

    def test_roles_follow_each_line(self):
        records = [
            {"type": "user", "message": {"content": "fix it"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": "ls"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "a\nb"}]}},
            {"type": "assistant", "message": {"content": "done"}},
        ]
        roles = []
        lines = rex_goto.render("claude", records, roles)
        self.assertEqual(len(roles), len(lines))
        self.assertEqual(
            list(zip(lines, roles, strict=True)),
            [
                ("▌ user", "user"),
                ("fix it", "user"),
                ("", "user"),
                ("▌ tool: Bash", "tool"),
                ("ls", "tool"),
                ("", "tool"),
                ("▌ result", "tool"),
                ("a", "tool"),
                ("b", "tool"),
                ("", "tool"),
                ("▌ assistant", "assistant"),
                ("done", "assistant"),
                ("", "assistant"),
            ],
        )

    def test_pi(self):
        records = [
            {"type": "session", "id": ID_A, "cwd": "/w"},
            {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "name": "read", "arguments": {"path": "a.txt"}}],
                },
            },
            {"type": "message", "message": {"role": "toolResult", "toolName": "read", "content": "contents"}},
        ]
        self.assertEqual(
            rex_goto.render("pi", records),
            ["▌ user", "hello", "", "▌ tool: read", '{"path": "a.txt"}', "", "▌ result: read", "contents", ""],
        )

    def test_codex(self):
        records = [
            {"type": "session_meta", "payload": {"id": ID_B, "cwd": "/w"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "list files"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": "ls -a"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "a\nb"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Two files."}},
        ]
        self.assertEqual(
            rex_goto.render("codex", records),
            [
                "▌ user",
                "list files",
                "",
                "▌ tool: shell",
                "ls -a",
                "",
                "▌ result",
                "a",
                "b",
                "",
                "▌ assistant",
                "Two files.",
                "",
            ],
        )

    def test_long_tool_output_is_clipped(self):
        output = "\n".join(f"row {index}" for index in range(rex_goto.TOOL_LINES + 5))
        records = [{"type": "user", "message": {"content": [{"type": "tool_result", "content": output}]}}]
        lines = rex_goto.render("claude", records)
        self.assertEqual(lines[-2], "… 5 more lines")
        self.assertEqual(len(lines), rex_goto.TOOL_LINES + 3)


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.roots = {
            "pi": (os.path.join(self.root, "pi"), 1),
            "claude": (os.path.join(self.root, "claude"), 1),
            "codex": (os.path.join(self.root, "codex"), 3),
        }
        self.now = time.time()

    def claude(self, name, cwd, mtime, **extra):
        path = os.path.join(self.root, "claude", "-w-app", f"{name}.jsonl")
        record = {"type": "user", "cwd": cwd, "sessionId": name, **extra, "message": {"content": "hi"}}
        write_jsonl(path, [record], mtime)
        return path

    def test_newest_agent_takes_the_newest_transcript(self):
        older = self.claude(ID_A, "/w/app", self.now - 50)
        newer = self.claude(ID_B, "/w/app", self.now - 5)
        self.claude(ID_C, "/w/app", self.now - 9999)  # written before either agent started
        agents = [
            {"key": "first", "agent": "claude", "cwd": "/w/app", "started": self.now - 100},
            {"key": "second", "agent": "claude", "cwd": "/w/app", "started": self.now - 60},
        ]
        self.assertEqual(rex_goto.assign_transcripts(agents, self.roots), {"second": newer, "first": older})

    def test_other_directories_and_sidechains_do_not_match(self):
        self.claude(ID_A, "/w/other", self.now)
        self.claude(ID_B, "/w/app", self.now, isSidechain=True)
        agents = [{"key": 1, "agent": "claude", "cwd": "/w/app", "started": self.now - 100}]
        self.assertEqual(rex_goto.assign_transcripts(agents, self.roots), {})

    def test_resumed_conversation_wins(self):
        resumed = self.claude(ID_A, "/w/app", self.now - 9999)
        self.claude(ID_B, "/w/app", self.now)
        agents = [{"key": 1, "agent": "claude", "cwd": "/w/app", "started": self.now - 10, "resumed": ID_A}]
        self.assertEqual(rex_goto.assign_transcripts(agents, self.roots), {1: resumed})

    def test_codex_and_pi_layouts(self):
        codex = os.path.join(self.root, "codex", "2026", "01", "02", f"rollout-2026-01-02T00-00-00-{ID_B}.jsonl")
        write_jsonl(codex, [{"type": "session_meta", "payload": {"id": ID_B, "cwd": "/w/app"}}], self.now)
        exec_run = os.path.join(self.root, "codex", "2026", "01", "02", f"rollout-x-{ID_C}.jsonl")
        write_jsonl(exec_run, [{"type": "session_meta", "payload": {"cwd": "/w/app", "source": "exec"}}], self.now + 1)
        pi = os.path.join(self.root, "pi", "--w-app--", f"2026_{ID_A}.jsonl")
        write_jsonl(pi, [{"type": "session", "id": ID_A, "cwd": "/w/app"}], self.now)
        agents = [
            {"key": "c", "agent": "codex", "cwd": "/w/app", "started": self.now - 10},
            {"key": "p", "agent": "pi", "cwd": "/w/app", "started": self.now - 10},
        ]
        self.assertEqual(rex_goto.assign_transcripts(agents, self.roots), {"c": codex, "p": pi})


class RowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        pane = {"session_id": "s", "block_id": "b"}
        buffers = [
            {"id": 0, "kind": "scroll", "label": "dev:1 scroll", "title": "t0", "lines": ["one", "", "two\tx"]},
            {"id": 1, "kind": "agent", "label": "dev:1 claude", "title": "t1", "lines": ["▌ user", "hi"]},
            {"id": 2, "kind": "scroll", "label": "ops:1 scroll", "title": "t2", "lines": ["far"]},
        ]
        for buffer, proximity in zip(buffers, (rex_goto.BLOCK, rex_goto.BLOCK, rex_goto.OTHER), strict=True):
            buffer.update(pane=pane, proximity=proximity)
        self.rows = list(rex_goto.write_buffers(self.directory, buffers))
        with open(os.path.join(self.directory, "rows.tsv"), "w", encoding="utf-8") as out:
            out.writelines(row + "\n" for row in self.rows)

    def test_rows_newest_first_without_blank_lines(self):
        self.assertEqual(
            self.rows,
            [
                "0\t3\t0\tscroll\tdev:1 scroll\ttwo    x",
                "0\t1\t0\tscroll\tdev:1 scroll\tone",
                "1\t2\t0\tagent\tdev:1 claude\thi",
                "1\t1\t0\tagent\tdev:1 claude\t▌ user",
                "2\t1\t3\tscroll\tops:1 scroll\tfar",
            ],
        )

    def rows_for(self, state):
        rex_goto.write_state(self.directory, state)
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            rex_goto.cmd_rows(self.directory)
        return [row.split("\t")[5] for row in out.getvalue().splitlines()]

    def test_scope_and_source(self):
        self.assertEqual(self.rows_for({"scope": "all", "source": "all"}), ["two    x", "one", "hi", "▌ user", "far"])
        self.assertEqual(self.rows_for({"scope": "window", "source": "all"}), ["two    x", "one", "hi", "▌ user"])
        self.assertEqual(self.rows_for({"scope": "all", "source": "agent"}), ["hi", "▌ user"])
        self.assertEqual(self.rows_for({"scope": "all", "source": "scroll"}), ["two    x", "one", "far"])

    def test_roles(self):
        directory = tempfile.mkdtemp()
        buffer = {
            "id": 0,
            "kind": "agent",
            "label": "dev:1 pi",
            "title": "t",
            "lines": ["▌ user", "ask", "▌ assistant", "answer", "▌ tool: bash", "ls"],
            "roles": ["user", "user", "assistant", "assistant", "tool", "tool"],
            "pane": {},
            "proximity": rex_goto.BLOCK,
        }
        rows = list(rex_goto.write_buffers(directory, [buffer]))
        self.assertEqual([rex_goto.split_row(row)[3] for row in rows][:2], ["agent:tool", "agent:tool"])
        texts = {
            source: [
                rex_goto.split_row(row)[5] for row in rows if rex_goto.keep(row, {"scope": "all", "source": source})
            ]
            for source in ("agent", "user", "assistant", "tool", "scroll")
        }
        self.assertEqual(texts["agent"], ["ls", "▌ tool: bash", "answer", "▌ assistant", "ask", "▌ user"])
        self.assertEqual(texts["user"], ["ask", "▌ user"])
        self.assertEqual(texts["assistant"], ["answer", "▌ assistant"])
        self.assertEqual(texts["tool"], ["ls", "▌ tool: bash"])
        self.assertEqual(texts["scroll"], [])
        self.assertEqual(rex_goto.SOURCES, ("all", "scroll", "agent", "user", "assistant", "tool"))

    def test_dedupe_keeps_the_newest_copy(self):
        directory = tempfile.mkdtemp()
        buffer = {
            "id": 0,
            "kind": "scroll",
            "label": "dev:1 scroll",
            "title": "t",
            "lines": ["$ make", "ok", "$ make", "ok", "done"],
            "pane": {},
            "proximity": rex_goto.BLOCK,
        }
        rows = [rex_goto.split_row(row) for row in rex_goto.write_buffers(directory, [buffer])]
        self.assertEqual([(row[1], row[5]) for row in rows], [("5", "done"), ("4", "ok"), ("3", "$ make")])
        # The buffer keeps every line, so the preview and the editor still show the earlier copies.
        with open(os.path.join(directory, "0.txt")) as source:
            self.assertEqual(source.read().splitlines()[1:], buffer["lines"])
        rows = list(rex_goto.write_buffers(directory, [buffer], dedupe=False))
        self.assertEqual(len(rows), 5)

    def test_dedupe_keeps_a_line_once_per_role(self):
        buffer = {
            "id": 0,
            "kind": "agent",
            "label": "dev:1 pi",
            "title": "t",
            "lines": ["▌ user", "ok", "▌ assistant", "ok", "▌ user", "ok"],
            "roles": ["user", "user", "assistant", "assistant", "user", "user"],
            "pane": {},
            "proximity": rex_goto.BLOCK,
        }
        rows = [rex_goto.split_row(row) for row in rex_goto.write_buffers(tempfile.mkdtemp(), [buffer])]
        self.assertEqual(
            [(row[1], row[3], row[5]) for row in rows],
            [
                ("6", "agent:user", "ok"),
                ("5", "agent:user", "▌ user"),
                ("4", "agent:assistant", "ok"),
                ("3", "agent:assistant", "▌ assistant"),
            ],
        )

    def test_cycle(self):
        rex_goto.write_state(self.directory, {"scope": "all", "source": "all"})
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            rex_goto.cmd_cycle(self.directory, "scope")
        self.assertTrue(out.getvalue().startswith("change-prompt(session/all> )+reload:"))
        self.assertIn(" rows ", out.getvalue())
        self.assertEqual(rex_goto.read_state(self.directory)["scope"], "session")
        for expected in ("window", "all"):
            with mock.patch("sys.stdout", new=io.StringIO()):
                rex_goto.cmd_cycle(self.directory, "scope")
            self.assertEqual(rex_goto.read_state(self.directory)["scope"], expected)

    def test_label_search_changes_the_fields_in_place(self):
        rex_goto.write_state(self.directory, rex_goto.initial_state())
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            rex_goto.cmd_cycle(self.directory, "fields")
        self.assertEqual(out.getvalue().strip(), "change-nth(1..)+change-prompt(all/all+label> )")
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            rex_goto.cmd_cycle(self.directory, "fields")
        self.assertEqual(out.getvalue().strip(), "change-nth(2)+change-prompt(all/all> )")

    def test_source_cycle_goes_through_the_roles(self):
        rex_goto.write_state(self.directory, rex_goto.initial_state())
        seen = []
        for _ in rex_goto.SOURCES:
            with mock.patch("sys.stdout", new=io.StringIO()):
                rex_goto.cmd_cycle(self.directory, "source")
            seen.append(rex_goto.read_state(self.directory)["source"])
        self.assertEqual(seen, ["scroll", "agent", "user", "assistant", "tool", "all"])

    def test_dedupe_flag(self):
        self.assertTrue(rex_goto.parse_args(["s", "b"]).dedupe)
        self.assertFalse(rex_goto.parse_args(["--dedupe", "false", "s", "b"]).dedupe)
        self.assertEqual(rex_goto.parse_args(["--source", "user", "s", "b"]).source, "user")

    def test_old_state_files_get_the_new_defaults(self):
        rex_goto.write_state(self.directory, {"scope": "window", "source": "agent"})
        self.assertEqual(rex_goto.read_state(self.directory), {"scope": "window", "source": "agent", "fields": "text"})

    def test_preview_marks_the_line(self):
        out = io.StringIO()
        rex_goto.cmd_preview(self.directory, "0", "3", out)
        self.assertEqual(
            out.getvalue().split("\n"),
            ["\033[1mt0\033[0m", "\033[2m1\033[0m one", "\033[2m2\033[0m ", "\033[7m3 two    x\033[0m", ""],
        )


def pane(session, block, proximity=rex_goto.OTHER, window=1):
    return {
        "session_id": session,
        "session": session,
        "window_id": f"{session}-w{window}",
        "window": window,
        "window_label": "",
        "block_id": block,
        "proximity": proximity,
    }


class ConcurrencyTests(unittest.TestCase):
    """Each lookup waits at a barrier that only opens once every lookup has started, so serial calls time out."""

    def test_sessions_are_looked_up_at_once(self):
        sessions = [{"session_id": f"s{index}", "label": f"s{index}"} for index in range(3)]
        barrier = threading.Barrier(2 * len(sessions), timeout=5)

        def api(method, **params):
            if method == "session.list":
                return {"sessions": sessions}
            barrier.wait()
            if method == "session.view":
                return {"windows": [{"window_id": "w", "label": ""}]}
            return {"blocks": [{"block_id": params["session_id"] + "b", "window_id": "w", "flavor": rex_goto.TERMINAL}]}

        with mock.patch.object(rex_goto, "api", side_effect=api):
            panes = rex_goto.terminal_blocks("s1", "s1b")
        self.assertEqual([(p["block_id"], p["proximity"]) for p in panes][0], ("s1b", rex_goto.BLOCK))
        self.assertEqual(len(panes), 3)

    def test_panes_are_collected_at_once_and_streamed_in_order(self):
        panes = [pane("dev", f"b{index}") for index in range(10)]
        barrier = threading.Barrier(2 * len(panes), timeout=5)

        def lines(pane, max_lines):
            barrier.wait()
            return [pane["block_id"]]

        def agent(pane):
            barrier.wait()

        with (
            mock.patch.object(rex_goto, "pane_lines", side_effect=lines),
            mock.patch.object(rex_goto, "pane_agent", side_effect=agent),
        ):
            chunks = list(rex_goto.build(tempfile.mkdtemp(), panes, 100))
        self.assertEqual([[row.split("\t")[5] for row in chunk] for chunk in chunks], [[p["block_id"]] for p in panes])

    def test_labels_are_padded_before_any_pane_is_in(self):
        panes = [pane("dev", "b1"), pane("operations", "b2", window=12)]
        directory = tempfile.mkdtemp()
        with (
            mock.patch.object(rex_goto, "pane_lines", return_value=["x"]),
            mock.patch.object(rex_goto, "pane_agent", return_value=None),
        ):
            rows = [row for chunk in rex_goto.build(directory, panes, 100) for row in chunk]
        labels = [row.split("\t")[4] for row in rows]
        self.assertEqual(len({len(label) for label in labels}), 1)
        self.assertEqual(len(labels[0]), len("operations:12 claude"))
        with open(os.path.join(directory, "1.json")) as source:
            self.assertEqual(json.load(source), {"pane": panes[1], "kind": "scroll"})

    def test_no_transcripts_skips_the_process_lookup(self):
        with (
            mock.patch.object(rex_goto, "pane_lines", return_value=["x"]),
            mock.patch.object(rex_goto, "pane_agent") as agent,
        ):
            list(rex_goto.build(tempfile.mkdtemp(), [pane("dev", "b1")], 100, transcripts=False))
        agent.assert_not_called()


FAKE_REX = """\
#!/usr/bin/env python3
import json, os, sys
args = [arg for arg in sys.argv[1:] if not arg.startswith("--autostart")]
if args[:1] == ["--timeout"]:
    args = args[2:]
with open(os.environ["FAKE_REX_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
fixture = json.load(open(os.environ["FAKE_REX_FIXTURE"]))
if args[:2] == ["api", "call"]:
    params = json.loads(args[3])
    key = args[2] + ("" if args[2] == "session.list" else ":" + params["session_id"])
    print(json.dumps(fixture[key]))
elif args[:2] == ["block", "call"]:
    print(json.dumps(fixture[args[5] + ":" + args[6]]))
"""

FAKE_FZF = """\
#!/usr/bin/env python3
import os, sys
rows = sys.stdin.read().splitlines()
open(os.environ["FAKE_FZF_ARGS"], "w").write("\\n".join(sys.argv[1:]))
open(os.environ["FAKE_FZF_ROWS"], "w").write("\\n".join(rows))
# Each query picks the first row whose text holds it; several, split by |, stand for a multi-selection.
queries = os.environ["FAKE_FZF_QUERY"].split("|")
print(os.environ["FAKE_FZF_KEY"])
for query in queries:
    print(next(row for row in rows if query in row.split("\\t")[5]))
"""


class PickTests(unittest.TestCase):
    """The whole picker against a fake rex and fzf: two sessions, one pane running Claude."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.paths = {name: os.path.join(self.root, name) for name in ("log", "fixture", "args", "rows", "edited")}
        for name, body in (("rex", FAKE_REX), ("fzf", FAKE_FZF)):
            path = os.path.join(self.root, name)
            with open(path, "w") as out:
                out.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        claude = {"pid": os.getpid(), "name": "claude", "cwd": "/w/app"}
        fixture = {
            "session.list": {"sessions": [{"session_id": "s1", "label": "dev"}, {"session_id": "s2", "label": "ops"}]},
            "session.view:s1": {"windows": [{"window_id": "w1", "label": "edit"}]},
            "session.view:s2": {"windows": [{"window_id": "w2", "label": ""}]},
            "session.list_blocks:s1": {
                "blocks": [
                    {"block_id": "b1", "window_id": "w1", "flavor": "com.superlogical.terminal.shell"},
                    {"block_id": "b2", "window_id": "w1", "flavor": "com.superlogical.terminal.shell"},
                    {"block_id": "picker", "flavor": "com.superlogical.terminal.shell", "placement": "layer"},
                ]
            },
            "session.list_blocks:s2": {
                "blocks": [{"block_id": "b3", "window_id": "w2", "flavor": "com.superlogical.terminal.shell"}]
            },
            "b1:format": {"content": "$ make\nbuild ok\n"},
            "b1:process": {"foreground": {"pid": 1, "name": "zsh"}},
            "b2:format": {"content": "claude ui\n"},
            "b2:process": {"foreground": claude},
            "b3:format": {"content": "Traceback (most recent call last)\n  boom\n"},
            "b3:process": {"foreground": {"pid": 2, "name": "zsh"}},
        }
        with open(self.paths["fixture"], "w") as out:
            json.dump(fixture, out)
        self.transcript = os.path.join(self.root, "claude", "projects", "-w-app", f"{ID_A}.jsonl")
        write_jsonl(
            self.transcript,
            [{"type": "user", "cwd": "/w/app", "message": {"content": "where is the needle"}}],
        )
        editor = os.path.join(self.root, "editor")
        with open(editor, "w") as out:
            out.write(f'#!/bin/sh\nprintf "%s\\n" "$1" > {self.paths["edited"]}\ncat "$2" >> {self.paths["edited"]}\n')
        os.chmod(editor, 0o755)
        self.editor = editor

    def run_pick(self, query, key="enter"):
        env = {
            "FAKE_REX_LOG": self.paths["log"],
            "FAKE_REX_FIXTURE": self.paths["fixture"],
            "FAKE_FZF_ARGS": self.paths["args"],
            "FAKE_FZF_ROWS": self.paths["rows"],
            "FAKE_FZF_QUERY": query,
            "FAKE_FZF_KEY": key,
            "CLAUDE_CONFIG_DIR": os.path.join(self.root, "claude"),
            "PI_AGENT_DIR": os.path.join(self.root, "pi"),
            "CODEX_HOME": os.path.join(self.root, "codex"),
        }
        options = rex_goto.parse_args(
            ["--fzf-tool", os.path.join(self.root, "fzf"), "--editor", self.editor, "--client", "client:c", "s1", "b1"]
        )
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(rex_goto, "REX", os.path.join(self.root, "rex")),
            mock.patch.object(rex_goto, "started_at", return_value=0),
        ):
            self.assertEqual(rex_goto.pick(options), 0)

    def calls(self):
        with open(self.paths["log"]) as log:
            return [json.loads(line) for line in log]

    def test_rows_come_nearest_first_with_transcripts(self):
        self.run_pick("build ok")
        with open(self.paths["rows"]) as source:
            rows = [row.split("\t") for row in source.read().splitlines()]
        self.assertEqual(
            [(row[2], row[3], row[4].rstrip(), row[5]) for row in rows],
            [
                ("0", "scroll", "dev:1 scroll", "build ok"),
                ("0", "scroll", "dev:1 scroll", "$ make"),
                ("1", "scroll", "dev:1 scroll", "claude ui"),
                ("1", "agent:user", "dev:1 claude", "where is the needle"),
                ("1", "agent:user", "dev:1 claude", "▌ user"),
                ("3", "scroll", "ops:1 scroll", "  boom"),
                ("3", "scroll", "ops:1 scroll", "Traceback (most recent call last)"),
            ],
        )
        self.assertFalse(any("picker" in call for call in self.calls()), "layers aren't searched")

    def test_enter_opens_the_buffer_at_the_line(self):
        self.run_pick("boom")
        with open(self.paths["edited"]) as source:
            edited = source.read().splitlines()
        # Line 1 of the file is the buffer's title, so line 2 of the scrollback is line 3 of the file.
        self.assertEqual(edited[0], "+3")
        self.assertEqual(edited[1], "ops · window 1 · scrollback · b3")
        self.assertEqual(edited[3], "  boom")

    def test_enter_on_a_transcript_opens_the_transcript(self):
        self.run_pick("needle")
        with open(self.paths["edited"]) as source:
            edited = source.read().splitlines()
        self.assertEqual(edited[0], "+3")
        self.assertEqual(edited[1], f"dev · window 1 edit · claude · b2 · {self.transcript}")
        self.assertEqual(edited[3], "where is the needle")

    def test_copy_copies_every_selected_line(self):
        with mock.patch.object(rex_goto, "copy") as copy:
            self.run_pick("boom|build ok", key="ctrl-y")
        copy.assert_called_once_with("  boom\nbuild ok", "")

    def test_go_to_focuses_the_pane(self):
        self.run_pick("boom", key="alt-enter")
        self.assertEqual(
            self.calls()[-2:],
            [["-C", "client:c", "do", "session.select", "session_id=s2", "window_id=w2"], ["focus", "-s", "s2", "b3"]],
        )

    def test_fzf_opens_before_the_panes_are_listed(self):
        started = []

        def listing(*args):
            started.append(popen.called)
            raise RuntimeError("rex is down")

        fzf = mock.Mock()
        with (
            mock.patch.object(rex_goto.subprocess, "Popen", return_value=fzf) as popen,
            mock.patch.object(rex_goto, "terminal_blocks", side_effect=listing),
            self.assertRaises(RuntimeError),
        ):
            rex_goto.pick(rex_goto.parse_args(["s1", "b1"]))
        self.assertEqual(started, [True])
        fzf.terminate.assert_called_once()

    def test_fzf_searches_only_the_text(self):
        self.run_pick("boom")
        with open(self.paths["args"]) as source:
            args = source.read().split("\n")
        self.assertIn("--with-nth=5,6", args)
        self.assertIn("--nth=2", args)
        self.assertIn("--multi", args)
        self.assertFalse(any(arg.startswith("--expect") for arg in args), "--expect can't share keys with binds")
        # Every accept key prints its name first, as --expect did; only copy keeps the selection.
        self.assertIn("--bind=enter:clear-multi+print(enter)+accept", args)
        self.assertIn("--bind=ctrl-o:clear-multi+print(ctrl-o)+accept", args)
        self.assertIn("--bind=ctrl-y:print(ctrl-y)+accept", args)
        header = next(arg for arg in args if arg.startswith("--header="))
        for key, _ in rex_goto.KEYS:
            self.assertIn(key, header)
        self.assertTrue(any(arg.startswith("--preview-window=") and arg.endswith(",~1,+{2}+1/2") for arg in args))


class CopyTests(unittest.TestCase):
    def test_clip_tool(self):
        with mock.patch("subprocess.run") as run:
            rex_goto.copy("text", "~/bin/copy --flag")
        self.assertEqual(run.call_args.args[0], [os.path.expanduser("~/bin/copy"), "--flag"])
        self.assertEqual(run.call_args.kwargs["input"], "text")


if __name__ == "__main__":
    unittest.main()
