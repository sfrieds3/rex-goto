#!/usr/bin/env python3
"""Fuzzy-search the scrollback and agent transcripts of every Rex pane, then open or go to the match.

usage: rex_goto.py [options] SESSION BLOCK     # the picker; SESSION and BLOCK are the pane it opened from
       rex_goto.py rows DIR                     # fzf reload: the rows for the current scope and source
       rex_goto.py cycle DIR scope|source       # fzf transform: next scope or source, then reload
       rex_goto.py preview DIR BUFFER LINE      # fzf preview: BUFFER with LINE marked
"""

import argparse
import concurrent.futures
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

REX = os.environ.get("REX_BIN") or "rex"
TIMEOUT = os.environ.get("REX_GOTO_TIMEOUT", "2s")
TERMINAL = "com.superlogical.terminal"
SCOPES = ("all", "session", "window")
SOURCES = ("all", "scroll", "agent")
# Proximity to the pane the picker opened from, which orders rows and backs the scopes.
BLOCK, WINDOW, SESSION, OTHER = range(4)
SCOPE_LIMIT = {"window": WINDOW, "session": SESSION, "all": OTHER}
# A transcript written up to this long before ps says its agent started still counts as the agent's.
START_SLACK = 5
TOOL_LINES = 200
HELP = (
    "enter=open  alt-enter/ctrl-o=go to pane  ctrl-y=copy  ctrl-r=scope  ctrl-s=source  "
    "shift-up/down=scroll preview  ctrl-/=preview"
)


# Rex


def rex(*args):
    result = subprocess.run(
        [REX, "--autostart=false", "--timeout", TIMEOUT, *args],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"rex {args[0]} failed")
    return json.loads(result.stdout or "null")


def rex_run(*args):
    result = subprocess.run([REX, "--autostart=false", *args], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"rex {args[0]} failed")


def api(method, **params):
    return rex("api", "call", method, json.dumps(params)) or {}


def block_call(session, block, method, params=None):
    return rex("block", "call", "-s", session, "-b", block, method, json.dumps(params or {})) or {}


def is_terminal(entry):
    kind = str(entry.get("flavor") or entry.get("creator_name") or "")
    # Layers, this picker's among them, aren't placed.
    return kind.startswith(TERMINAL) and entry.get("placement", "placed") == "placed"


def terminal_blocks(origin_session, origin_block):
    """Every placed terminal block in every session, nearest the origin first."""
    found = []
    origin_window = None
    for session in api("session.list").get("sessions") or []:
        session_id = session.get("session_id")
        if not session_id:
            continue
        windows = {}
        for index, window in enumerate(api("session.view", session_id=session_id).get("windows") or [], 1):
            windows[window.get("window_id")] = (index, window.get("label") or "")
        for entry in api("session.list_blocks", session_id=session_id).get("blocks") or []:
            if not entry.get("block_id") or not is_terminal(entry):
                continue
            index, label = windows.get(entry.get("window_id"), (0, ""))
            pane = {
                "session_id": session_id,
                "session": session.get("label") or session_id,
                "window_id": entry.get("window_id") or "",
                "window": index,
                "window_label": label,
                "block_id": entry["block_id"],
            }
            if entry["block_id"] == origin_block:
                origin_window = pane["window_id"]
            found.append(pane)
    for pane in found:
        if pane["block_id"] == origin_block:
            pane["proximity"] = BLOCK
        elif pane["session_id"] == origin_session and pane["window_id"] == origin_window:
            pane["proximity"] = WINDOW
        elif pane["session_id"] == origin_session:
            pane["proximity"] = SESSION
        else:
            pane["proximity"] = OTHER
    return sorted(found, key=lambda pane: pane["proximity"])


def scrollback(pane):
    return block_call(pane["session_id"], pane["block_id"], "format", {"unwrap": True, "trim": True}).get("content", "")


def go_to(pane, client):
    """Shows the pane's session and window in the app, then focuses the pane, as Agentmux does."""
    if client:
        select = ["-C", client, "do", "session.select", f"session_id={pane['session_id']}"]
        rex_run(*select, *([f"window_id={pane['window_id']}"] if pane.get("window_id") else []))
    rex_run("focus", "-s", pane["session_id"], pane["block_id"])


# Agents

AGENT_SCRIPTS = (
    ("pi", re.compile(r"/(?:pi-coding-agent/dist/cli\.js|pi)$")),
    ("codex", re.compile(r"/(?:codex/bin/codex\.js|codex)$")),
    ("claude", re.compile(r"/(?:claude-code/cli\.js|claude)$")),
)
AGENT_NAMES = {"pi": "pi", "claude": "claude", "claude-code": "claude", "codex": "codex"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


# Same rules as Agentmux's processAgent: Pi runs as node, so argv0 and the script count too.
def process_agent(proc):
    if not isinstance(proc, dict):
        return None
    for name in (proc.get("name"), os.path.basename(str(proc.get("argv0") or "")).lstrip("-")):
        if name in AGENT_NAMES:
            return AGENT_NAMES[name]
    script = str(proc.get("script") or "")
    return next((agent for agent, pattern in AGENT_SCRIPTS if pattern.search(script)), None)


def block_agent(process):
    """The outermost agent in a block's process chain, which owns it; nested agents are its tools."""
    chain = [process.get("foreground"), *(process.get("ancestors") or [])]
    owner = None
    for proc in chain:
        agent = process_agent(proc)
        if agent and isinstance(proc.get("pid"), int):
            owner = {"agent": agent, "pid": proc["pid"], "cwd": proc.get("cwd") or ""}
    if owner and not owner["cwd"]:
        owner["cwd"] = (process.get("foreground") or {}).get("cwd") or ""
    return owner


def ps(pid, field):
    result = subprocess.run(
        ["ps", "-o", f"{field}=", "-p", str(pid)],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
        timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def started_at(pid):
    try:
        return time.mktime(time.strptime(" ".join(ps(pid, "lstart").split()), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return 0


def resumed_id(agent, argv):
    """The conversation an agent's command line names: claude --resume ID, codex resume ID, pi --session FILE."""
    flags = {"claude": ("--resume", "-r", "--session-id"), "codex": ("resume",), "pi": ("--session", "--resume")}[agent]
    for index, arg in enumerate(argv[:-1]):
        if arg in flags:
            return argv[index + 1]
    for arg in argv:
        for flag in flags:
            if arg.startswith(flag + "="):
                return arg.split("=", 1)[1]
    return None


def agent_roots():
    home = os.path.expanduser("~")
    pi = os.environ.get("PI_AGENT_DIR") or os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(home, ".pi", "agent")
    claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
    codex = os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")
    return {
        "pi": (os.path.join(pi, "sessions"), 1),
        "claude": (os.path.join(claude, "projects"), 1),
        "codex": (os.path.join(codex, "sessions"), 3),
    }


def transcript_files(root, depth):
    """(path, mtime) for each .jsonl under ROOT, DEPTH directories down at most, skipping subagent transcripts."""
    found = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return found
    for entry in entries:
        try:
            if entry.is_dir() and depth > 0 and entry.name != "subagents":
                found += transcript_files(entry.path, depth - 1)
            elif entry.is_file() and entry.name.endswith(".jsonl"):
                found.append((entry.path, entry.stat().st_mtime))
        except OSError:
            pass
    return found


def read_records(path, limit=None):
    records = []
    try:
        with open(path, encoding="utf-8", errors="replace") as source:
            for line in source:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
                if limit and len(records) >= limit:
                    break
    except OSError:
        pass
    return records


def transcript_cwd(agent, path):
    """The working directory a transcript was started in, or None for one that isn't a top-level chat."""
    for record in read_records(path, 20):
        if not isinstance(record, dict):
            continue
        if agent == "pi" and record.get("type") == "session":
            return record.get("cwd")
        if agent == "codex" and record.get("type") == "session_meta":
            payload = record.get("payload") or {}
            # Subagent and `codex exec` runs aren't chats a pane shows.
            source = payload.get("source")
            return None if isinstance(source, dict) or source == "exec" else payload.get("cwd")
        if agent == "claude":
            if record.get("isSidechain"):
                return None
            if record.get("cwd"):
                return record["cwd"]
    return None


def assign_transcripts(agents, roots=None):
    """Matches each agent block to its transcript, or None.

    An agent whose command line names a conversation gets that one. The rest, grouped by agent and working
    directory, take the transcripts written since they started: the newest agent the most recently written.
    """
    roots = roots or agent_roots()
    files = {name: transcript_files(*roots[name]) for name in {agent["agent"] for agent in agents}}
    result, claimed = {}, set()
    for agent in agents:
        wanted = agent.get("resumed")
        if not wanted:
            continue
        for path, _ in files[agent["agent"]]:
            if path == wanted or (UUID.fullmatch(wanted) and wanted in os.path.basename(path)):
                result[agent["key"]] = path
                claimed.add(path)
                break
    cwds = {}
    for agent in sorted(agents, key=lambda agent: agent.get("started", 0), reverse=True):
        if agent["key"] in result:
            continue
        candidates = [
            (path, mtime)
            for path, mtime in files[agent["agent"]]
            if path not in claimed and mtime >= agent.get("started", 0) - START_SLACK
        ]
        for path, _ in sorted(candidates, key=lambda item: item[1], reverse=True):
            if path not in cwds:
                cwds[path] = transcript_cwd(agent["agent"], path)
            if cwds[path] and os.path.realpath(cwds[path]) == os.path.realpath(agent["cwd"] or "/"):
                result[agent["key"]] = path
                claimed.add(path)
                break
    return result


def text_of(content, kinds=("text", "input_text", "output_text")):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = (text_of(part, kinds) for part in content if isinstance(part, (dict, str)))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict) and content.get("type") in kinds:
        return str(content.get("text") or "")
    return ""


def clip(text, limit=TOOL_LINES):
    lines = str(text).splitlines()
    if len(lines) > limit:
        return [*lines[:limit], f"… {len(lines) - limit} more lines"]
    return lines


def compact(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def render(agent, records):
    """A transcript as plain lines: a ▌ heading per turn, tool call or tool result, then its text."""
    lines = []

    def turn(heading, body, limit=None):
        body = clip(body, limit) if limit else str(body).splitlines()
        if any(line.strip() for line in body):
            lines.extend([f"▌ {heading}", *body, ""])

    for record in records:
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        if agent == "pi" and kind == "message":
            message = record.get("message") or {}
            role = message.get("role")
            if role == "toolResult":
                turn(f"result: {message.get('toolName') or 'tool'}", text_of(message.get("content")), TOOL_LINES)
                continue
            if role in ("user", "assistant"):
                turn(role, text_of(message.get("content")))
                for part in message.get("content") if isinstance(message.get("content"), list) else []:
                    if isinstance(part, dict) and part.get("type") == "toolCall":
                        turn(f"tool: {part.get('name') or 'tool'}", compact(part.get("arguments")), TOOL_LINES)
        elif agent == "claude" and kind in ("user", "assistant"):
            if record.get("isSidechain") or record.get("isMeta"):
                continue
            content = (record.get("message") or {}).get("content")
            turn(kind, text_of(content))
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    turn(f"tool: {part.get('name') or 'tool'}", compact(part.get("input")), TOOL_LINES)
                elif part.get("type") == "tool_result":
                    turn("result", text_of(part.get("content")), TOOL_LINES)
        elif agent == "codex" and kind in ("event_msg", "response_item"):
            payload = record.get("payload") or {}
            what = payload.get("type")
            if kind == "event_msg" and what == "user_message":
                turn("user", payload.get("message") or "")
            elif kind == "event_msg" and what == "agent_message":
                turn("assistant", payload.get("message") or "")
            elif kind == "response_item" and what in ("function_call", "custom_tool_call"):
                turn(f"tool: {payload.get('name') or 'tool'}", payload.get("arguments") or payload.get("input") or "")
            elif kind == "response_item" and what in ("function_call_output", "custom_tool_call_output"):
                output = payload.get("output")
                if isinstance(output, dict):
                    output = output.get("content") or output.get("output") or ""
                turn("result", text_of(output), TOOL_LINES)
    return lines


# Buffers and rows


def clean(line):
    return line.rstrip("\r").replace("\t", "    ")


def write_buffers(directory, buffers):
    """Writes each buffer and yields its rows, newest line first:
    buffer, line, proximity, source, label, text. fzf searches only the text."""
    for buffer in buffers:
        lines = [clean(line) for line in buffer["lines"]]
        with open(os.path.join(directory, f"{buffer['id']}.txt"), "w", encoding="utf-8") as out:
            out.write(buffer["title"] + "\n")
            out.write("\n".join(lines) + "\n")
        for number in range(len(lines), 0, -1):
            text = lines[number - 1]
            if text.strip():
                yield "\t".join(
                    (str(buffer["id"]), str(number), str(buffer["proximity"]), buffer["kind"], buffer["label"], text)
                )


def label_for(pane, source):
    return f"{pane['session']}:{pane['window']} {source}"


def collect(pane, max_lines):
    """The pane's scrollback buffer, and its agent if one runs there."""
    lines = scrollback(pane).rstrip("\n").split("\n")[-max_lines:]
    agent = None
    with contextlib.suppress(RuntimeError):
        agent = block_agent(block_call(pane["session_id"], pane["block_id"], "process"))
    if agent:
        agent["started"] = started_at(agent["pid"])
        agent["resumed"] = resumed_id(agent["agent"], shlex.split(ps(agent["pid"], "args")))
    return lines, agent


def pane_title(pane, source):
    window = f" {pane['window_label']}" if pane["window_label"] else ""
    return f"{pane['session']} · window {pane['window']}{window} · {source} · {pane['block_id']}"


def build(directory, panes, max_lines, transcripts=True):
    """Collects every pane in parallel, matches agents to transcripts, writes the buffers, then yields their rows,
    nearest pane first."""
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        collected = list(pool.map(lambda pane: (pane, *collect(pane, max_lines)), panes))
    for index, (_, _, agent) in enumerate(collected):
        if agent:
            agent["key"] = index
    found = assign_transcripts([agent for _, _, agent in collected if agent]) if transcripts else {}
    meta = {}
    buffers = []
    for index, (pane, lines, agent) in enumerate(collected):
        scroll_id = len(buffers)
        buffers.append(
            {
                "id": scroll_id,
                "pane": pane,
                "kind": "scroll",
                "label": label_for(pane, "scroll"),
                "title": pane_title(pane, "scrollback"),
                "lines": lines,
                "proximity": pane["proximity"],
            }
        )
        path = found.get(index) if agent else None
        if path:
            agent_lines = render(agent["agent"], read_records(path))[-max_lines:]
            buffers.append(
                {
                    "id": len(buffers),
                    "pane": pane,
                    "kind": "agent",
                    "label": label_for(pane, agent["agent"]),
                    "title": f"{pane_title(pane, agent['agent'])} · {path}",
                    "lines": agent_lines,
                    "proximity": pane["proximity"],
                }
            )
    for buffer in buffers:
        meta[str(buffer["id"])] = {"pane": buffer["pane"], "kind": buffer["kind"]}
    with open(os.path.join(directory, "buffers.json"), "w", encoding="utf-8") as out:
        json.dump(meta, out)
    width = max((len(buffer["label"]) for buffer in buffers), default=0)
    for buffer in buffers:
        buffer["label"] = buffer["label"].ljust(width)
    yield from write_buffers(directory, buffers)


def read_state(directory):
    try:
        with open(os.path.join(directory, "state.json"), encoding="utf-8") as source:
            return json.load(source)
    except (OSError, ValueError):
        return {"scope": "all", "source": "all"}


def write_state(directory, state):
    with open(os.path.join(directory, "state.json"), "w", encoding="utf-8") as out:
        json.dump(state, out)


def keep(row, state):
    _, _, proximity, kind, _, _ = row.split("\t", 5)
    return int(proximity) <= SCOPE_LIMIT[state["scope"]] and state["source"] in ("all", kind)


def prompt(state):
    return f"{state['scope']}/{state['source']}> "


def cmd_rows(directory):
    state = read_state(directory)
    with open(os.path.join(directory, "rows.tsv"), encoding="utf-8") as source:
        for row in source:
            if keep(row, state):
                sys.stdout.write(row)


def cmd_cycle(directory, what):
    state = read_state(directory)
    values = SCOPES if what == "scope" else SOURCES
    state[what] = values[(values.index(state[what]) + 1) % len(values)]
    write_state(directory, state)
    # reload: runs to the end of the action list, so it goes last.
    print(f"change-prompt({prompt(state)})+reload:{self_command('rows', directory)}")


def cmd_preview(directory, buffer, line, out=sys.stdout):
    """The buffer's title, then its lines numbered, LINE in reverse video; fzf's preview window centres LINE."""
    try:
        with open(os.path.join(directory, f"{int(buffer)}.txt"), encoding="utf-8") as source:
            title = source.readline().rstrip("\n")
            lines = source.read().split("\n")
    except (OSError, ValueError):
        return
    line = int(line)
    width = len(str(len(lines)))
    out.write(f"\033[1m{title}\033[0m\n")
    for number, text in enumerate(lines[:-1] if lines and lines[-1] == "" else lines, 1):
        if number == line:
            out.write(f"\033[7m{number:>{width}} {text}\033[0m\n")
        else:
            out.write(f"\033[2m{number:>{width}}\033[0m {text}\n")


# Picker


def self_command(*args):
    return " ".join(shlex.quote(part) for part in (sys.executable, os.path.realpath(__file__), *args))


def fzf_command(options, directory):
    state = {"scope": options.scope, "source": options.source}
    return [
        *shlex.split(options.fzf_tool),
        "--delimiter=\t",
        "--with-nth=5,6",
        "--nth=2",
        "--no-hscroll",
        "--tiebreak=index",
        f"--layout={options.fzf_layout}",
        f"--prompt={prompt(state)}",
        f"--header={HELP}",
        "--expect=enter,alt-enter,ctrl-o,ctrl-y",
        f"--preview={self_command('preview', directory)} {{1}} {{2}}",
        f"--preview-window={options.preview_window},~1,+{{2}}+1/2",
        f"--bind=ctrl-r:transform:{self_command('cycle', directory, 'scope')}",
        f"--bind=ctrl-s:transform:{self_command('cycle', directory, 'source')}",
        "--bind=ctrl-/:toggle-preview",
    ]


def copy(text, clip_tool):
    """Copies TEXT with CLIP_TOOL, pbcopy, wl-copy or xclip, else OSC 52."""
    if clip_tool:
        command = shlex.split(os.path.expanduser(clip_tool))
    elif shutil.which("pbcopy"):
        command = ["pbcopy"]
    elif os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        command = ["wl-copy"]
    elif os.environ.get("DISPLAY") and shutil.which("xclip"):
        command = ["xclip", "-selection", "clipboard"]
    else:
        import base64

        with open("/dev/tty", "w") as tty:
            tty.write(f"\033]52;c;{base64.b64encode(text.encode()).decode()}\a")
            tty.flush()
        time.sleep(0.1)
        return
    subprocess.run(command, input=text, text=True, check=False, start_new_session=True)


def fzf_env(options):
    env = dict(os.environ)
    if options.fzf_unset_default_opts:
        env.pop("FZF_DEFAULT_OPTS", None)
        env.pop("FZF_DEFAULT_OPTS_FILE", None)
    return env


def pick(options):
    directory = tempfile.mkdtemp(prefix="rex-goto-")
    try:
        write_state(directory, {"scope": options.scope, "source": options.source})
        state = read_state(directory)
        panes = terminal_blocks(options.session, options.block)
        fzf = subprocess.Popen(
            fzf_command(options, directory),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=fzf_env(options),
        )
        with open(os.path.join(directory, "rows.tsv"), "w", encoding="utf-8") as rows:
            try:
                for row in build(directory, panes, options.max_lines, options.transcripts):
                    rows.write(row + "\n")
                    if keep(row, state):
                        fzf.stdin.write(row + "\n")
            except BrokenPipeError:
                pass
        with contextlib.suppress(BrokenPipeError):
            fzf.stdin.close()
        output = fzf.stdout.read()
        if fzf.wait() not in (0, 1, 130):
            raise RuntimeError(f"fzf failed with status {fzf.returncode}")
        lines = output.split("\n")
        if len(lines) < 2 or not lines[1]:
            return 0
        key, row = lines[0], lines[1]
        buffer, line, _, _, _, text = row.split("\t", 5)
        with open(os.path.join(directory, "buffers.json"), encoding="utf-8") as source:
            pane = json.load(source)[buffer]["pane"]
        if key == "ctrl-y":
            copy(text, options.clip_tool)
        elif key in ("alt-enter", "ctrl-o"):
            go_to(pane, options.client)
        else:
            path = os.path.join(directory, f"{buffer}.txt")
            # The buffer file starts with its title, so the match is one line further down.
            subprocess.run([*shlex.split(options.editor), f"+{int(line) + 1}", path], check=False)
        return 0
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def boolean(value):
    return value.lower() in ("1", "true", "yes", "on")


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="rex_goto.py", description=__doc__.split("\n")[0])
    parser.add_argument("--client", default="", help="Rex client to show the chosen pane's session in")
    parser.add_argument("--scope", choices=SCOPES, default="all")
    parser.add_argument("--source", choices=SOURCES, default="all")
    parser.add_argument("--max-lines", type=int, default=20000, help="newest lines kept per buffer")
    parser.add_argument("--transcripts", type=boolean, default=True, help="search agent transcripts too")
    parser.add_argument("--editor", default=os.environ.get("REX_GOTO_EDITOR") or "nvim -R")
    parser.add_argument("--clip-tool", default="")
    parser.add_argument("--fzf-tool", default="fzf")
    parser.add_argument("--fzf-layout", default="reverse")
    parser.add_argument("--fzf-unset-default-opts", type=boolean, default=True)
    parser.add_argument("--preview-window", default="down,60%")
    parser.add_argument("session")
    parser.add_argument("block")
    return parser.parse_args(argv)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else ""
    if command == "rows" and len(argv) == 2:
        return cmd_rows(argv[1]) or 0
    if command == "cycle" and len(argv) == 3 and argv[2] in ("scope", "source"):
        return cmd_cycle(argv[1], argv[2]) or 0
    if command == "preview" and len(argv) == 4:
        return cmd_preview(*argv[1:]) or 0
    options = parse_args(argv)
    try:
        return pick(options)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"rex-goto: {exc}", file=sys.stderr)
        time.sleep(2)
        return 1


if __name__ == "__main__":
    sys.exit(main())
