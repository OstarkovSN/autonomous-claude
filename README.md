# autonomous-claude

A PTY wrapper around the `claude` CLI that exposes a Unix-domain socket so that
Claude — running inside the wrapped session — can call back into the wrapper
from his Bash tool to trigger `/compact`, `/clear`, `/exit`, or arbitrary
keystrokes. This gives a session **self-driven context management** until
Claude Code ships a built-in (`anthropics/claude-code` #39574, #19877, #27244).

The wrapper is otherwise a transparent passthrough: stdin, stdout, terminal
resize, exit code — all forwarded byte-for-byte.

## Architecture

```
your terminal
   ↕ stdio (raw mode)
autonomous-claude (wrapper)
   ├── PTY pair: master ↔ slave
   ├── child: `claude ...` (running on the slave PTY)
   │     └── Bash tool calls inherit AUTONOMOUS_CLAUDE_SOCK
   │         and can send JSON commands to it
   └── Unix-domain socket at /tmp/autonomous-claude-{pid}.sock
         accepts JSON commands → translates to keystrokes
         written to the PTY master FD
```

## Install

### With `uv` (recommended)

```bash
uv tool install .
# puts `autonomous-claude` on your PATH
```

### Manual (pip + symlink)

```bash
pip install ptyprocess
ln -s "$(pwd)/autonomous_claude.py" ~/.local/bin/autonomous-claude
chmod +x ~/.local/bin/autonomous-claude
```

### Skill install

```bash
mkdir -p ~/.claude/skills/autonomous-session
cp skills/autonomous-session/SKILL.md ~/.claude/skills/autonomous-session/SKILL.md
```

(Or symlink it so updates flow through.)

## Usage

`autonomous-claude` takes no flags of its own. Everything you'd pass to `claude`
goes straight through:

```bash
autonomous-claude
autonomous-claude --model opus
autonomous-claude --resume <session-id>
```

Inside the resulting session, Claude has two env vars set:

| Var                       | Value                                          |
| ------------------------- | ---------------------------------------------- |
| `AUTONOMOUS_CLAUDE_SOCK`  | absolute path to the control socket            |
| `AUTONOMOUS_CLAUDE_PID`   | wrapper PID (string)                           |

A discovery file is also written for cases where the env var doesn't survive:

```
~/.claude/state/autonomous-claude.sock.path
```

## Protocol

One JSON request per connection. Replies are one line, `{"ok": true}` or
`{"ok": false, "error": "..."}`. Connection closes after the reply.

```json
{"cmd": "compact"}
{"cmd": "compact", "instructions": "keep the API contract"}
{"cmd": "clear"}
{"cmd": "exit"}
{"cmd": "send_keys", "data": "hello\r"}
```

Keystroke translation:

| `cmd`                          | Bytes written to PTY master                                                              |
| ------------------------------ | ---------------------------------------------------------------------------------------- |
| `compact` (no args)            | `/compact\r` + post-compact nudge + `\r`                                                 |
| `compact` (with instructions)  | `ESC[200~/compact <args>ESC[201~\r` + post-compact nudge + `\r`                          |
| `clear`                        | `/clear\r`                                                                               |
| `exit`                         | `/exit\r`                                                                                |
| `send_keys`                    | `data.encode("utf-8")`                                                                   |

Two subtleties worth knowing:

- **Bracketed paste for `/compact <args>`.** Typing the slash command
  character-by-character lets the TUI's autocomplete intercept the space
  after `compact` as "confirm & submit," which fires bare `/compact` and
  dumps the instructions into the next input as plain text. Wrapping the
  whole line in `ESC[200~ … ESC[201~` (bracketed paste) bypasses
  autocomplete — the TUI receives one atomic paste event and parses the
  slash command at submission.
- **Post-compact nudge.** After `/compact` runs, Claude Code returns to an
  idle prompt and waits for the next user message. The wrapper queues a
  short follow-up (`Context was just compacted. Resume your previous task.`)
  into the input buffer; the TUI processes it once compaction finishes,
  giving Claude an unconditional resume signal.

The trailing `\r` (never `\n`) is what the TUI's input field treats as
submit. **No** leading `ESC` — that's the "interrupt generation" hotkey
in Claude Code's TUI, not "close autocomplete."

`send_keys.data` is restricted to **printable ASCII + `\r` + `\x1b`**. That
allows CSI sequences (`\x1b[A` etc.) but blocks Ctrl-C, NUL, tab, and `\n`.

## Invoking from inside Claude

Use the `autonomous-claude-ctl` binary (installed alongside the wrapper). It
does all the JSON/socket/guard work. Inside Claude's Bash tool:

```bash
autonomous-claude-ctl compact
autonomous-claude-ctl compact "keep API contract and open bugs"
autonomous-claude-ctl clear
autonomous-claude-ctl exit
autonomous-claude-ctl send-keys $'\e[A'   # up-arrow
```

The controller:

- Reads `$AUTONOMOUS_CLAUDE_PID` and `$AUTONOMOUS_CLAUDE_SOCK`, refuses if
  either is unset or they disagree (protects against resumed sessions,
  other concurrent wrappers, stale discovery files).
- Sends the JSON request, half-closes (`SHUT_WR`) so the wrapper dispatches
  immediately, reads the reply with a 5 s timeout.
- Exits 0 on `{"ok": true}`, exits non-zero with a one-line stderr message
  on any failure (refusal, timeout, dead wrapper, missing env).

Why a binary and not raw `socat`/`nc`: socat's bidirectional-close defaults
can stall when neither side fully closes the connection; `nc` flavors vary
(`-q`, `-N`, BSD vs GNU). A purpose-built client with explicit `SHUT_WR`
makes the protocol deterministic and the skill docs trivial.

The discovery file at `~/.claude/state/autonomous-claude.sock.path` exists
for external tooling that wants to know "is there an autonomous-claude on
this host." **It is not safe for in-session lookup** and the skill forbids
its use.

## Troubleshooting

**`AUTONOMOUS_CLAUDE_SOCK` is unset inside Claude's Bash tool.**
This is the zsh inherited-env quirk (`anthropics/claude-code` #32512). Always
invoke the socket call inside `bash -c '...'` — that bypasses interactive zsh
rc files that strip the environment. The skill examples already do this.

**The variable is missing even from `bash -c`.**
Check `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`. When set, Claude Code scrubs env
vars whose names look credential-shaped. `AUTONOMOUS_CLAUDE_SOCK` was named
specifically to avoid the `_KEY` / `_TOKEN` / `_SECRET` patterns. If you have
local overrides extending the scrub list, add an exception.

**`socat: command not found` from inside the session.**
Claude's Bash tool may resolve PATH differently from your interactive shell
(`anthropics/claude-code` #3991). Run `which socat` and `which nc` from inside
a session — that reflects what Claude actually sees. The skill falls back to
`nc -U` automatically; if both are missing, install `socat` system-wide.

**The wrapper exited but its socket / discovery file remain.**
Should not happen on clean exit (the wrapper unlinks both in a `finally`
block). If it does — usually because the wrapper was SIGKILLed — the next
`autonomous-claude` invocation will overwrite the discovery file, and stale
sockets in `/tmp` are harmless. Clean manually:

```bash
rm -f /tmp/autonomous-claude-*.sock ~/.claude/state/autonomous-claude.sock.path
```

**Terminal looks mangled after wrapper crash.**
Run `reset` or `stty sane`. The wrapper restores termios in a `finally` block,
but a hard kill skips it.

## Development

```bash
uv sync
uv run pytest                  # 31 tests, ~2s
uv run mypy autonomous_claude.py
```

Tests do not exercise a real `claude` binary; the integration tests spawn
`cat` and `env` as the child via the `AUTONOMOUS_CLAUDE_BINARY` env override.

## Non-goals

- No tmux/screen/expect — single PTY pair only.
- No signal-based command dispatch — Unix socket only.
- No TUI output parsing for prompt-readiness detection — the kernel's tty
  buffer handles ordering.
- No credential storage in env vars or protocol.
- Not a daemon — one wrapper process per session.

## License

MIT — see [LICENSE](LICENSE).
