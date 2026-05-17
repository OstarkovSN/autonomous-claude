---
name: autonomous-session
description: Compact, clear, or exit your own session via the autonomous-claude wrapper. Use only at genuine task-completion moments when the user has confirmed work is done or context is full after a finished task.
---

# autonomous-session

You are running inside the `autonomous-claude` PTY wrapper. It ships a single
binary, `autonomous-claude-ctl`, that you call from your Bash tool.

## The four commands

```
autonomous-claude-ctl compact
autonomous-claude-ctl compact "<focus instructions>"
autonomous-claude-ctl clear
autonomous-claude-ctl exit
```

That's it. No JSON, no quoting, no socat. The controller resolves the
correct socket from `$AUTONOMOUS_CLAUDE_PID`, refuses if anything looks
wrong, exits **0 on success** and **non-zero with a stderr message on
failure**. Treat a non-zero exit as "nothing happened" and tell the user
what stderr said.

## When to use

Only at genuine task-completion moments:

- A multi-step task is finished, the user has confirmed it, and your context
  is meaningfully full from prior work.
- The user has explicitly indicated work is done.
- You are switching to a fresh, unrelated problem and a `compact` would
  preserve only the cross-cutting facts.

**Do not** preemptively compact mid-task, "just in case," or to make your
context feel cleaner. You are destroying working memory the user is
depending on.

## How it actually fires

The controller writes the slash-command keystrokes into Claude Code's input
field. While you are mid-turn — which you always are when calling this —
those keystrokes are **queued**. They run as the *next* user turn, after
your current turn completes. Practical implications:

- Exit code 0 means "queued," not "compacted." You do **not** have a fresh
  context yet. Anything you do after this call still uses the old context
  and will be discarded.
- After firing, **end your turn**. Emit one short closing line and stop.
  Anything you generate after the controller call is wasted work.
- Never call `compact` and then try to start more work in the same response.
  Treat the controller call as the last thing you do.

## Required workflow

1. **Announce intent in chat first.** One sentence: "Task is done — running
   `autonomous-claude-ctl compact` now." This is the user's only intervention
   window; the call is not reversible.
2. **Run the command.** Single Bash tool call, no flags:

   ```bash
   autonomous-claude-ctl compact
   ```

   Or with focus instructions:

   ```bash
   autonomous-claude-ctl compact "keep API contract and the open bug list"
   ```

3. **Check the exit code.** Non-zero → read stderr, report it, stop. Do
   not retry — the wrapper's refusal is intentional.
4. **End your turn.** One short closing line, then stop. The queued slash
   command will run after your turn ends.

## Targeting the right session

The controller derives the socket path from `$AUTONOMOUS_CLAUDE_PID` and
cross-checks it against `$AUTONOMOUS_CLAUDE_SOCK`. If either is unset, or
they disagree, the controller refuses. This protects against:

- Sessions that were resumed (new wrapper, new PID).
- Other concurrent `autonomous-claude` sessions on the same machine.
- Stale discovery files from prior wrappers.

**Forbidden** — never do any of these:

- `ps`, `pgrep`, `pidof`, or `ls /tmp/autonomous-claude-*.sock` to "find"
  a wrapper. You will hit a different user's or different session's wrapper
  and compact / exit the wrong thing.
- Reading `~/.claude/state/autonomous-claude.sock.path` and using its
  contents. That file is for external tooling, not in-session lookup.
- Constructing the socket path yourself from a PID you guessed.
- Trying multiple sockets and using whichever responds.

The controller is the only correct way. If it refuses, that refusal is
correct.

## Troubleshooting

| stderr message                                  | Meaning                                                                          |
| ----------------------------------------------- | -------------------------------------------------------------------------------- |
| `not inside an autonomous-claude session`       | `$AUTONOMOUS_CLAUDE_PID/SOCK` unset. Tell the user; stop.                        |
| `PID/SOCK mismatch — refusing`                  | Env vars inconsistent. Report values verbatim; stop.                             |
| `wrapper socket missing`                        | Wrapper died. Tell the user; stop.                                               |
| `connect failed`                                | Socket exists but isn't accepting. Wrapper hung; tell the user.                  |
| `timed out waiting for reply`                   | Wrapper accepted but didn't reply in 5s. Likely wrapper bug; tell the user.     |
| `wrapper refused command: ...`                  | The wrapper validated and rejected. Report the inner error; do not retry.      |

If `autonomous-claude-ctl: command not found`, the binary isn't on PATH.
That is a setup issue for the user, not something to work around — tell
them.
