# vvread MCP Server Integration Guide

**The standard read-aloud (Stop hook) speaks only after Claude finishes responding.**
With MCP integration, Claude can speak up **on its own, mid-task**.

| | When it reads aloud |
|---|---|
| Stop hook (standard) | After a response completes |
| MCP integration | At any point during work |

For example:

- Announce the **completion of a 5-minute build** by voice — notice it without watching the screen
- Read out **only blocking errors** (like test failures) as a short summary
- Just say "tell me when it's done" and hear the milestones of a long task
- Change the **speaker or speed** on the spot ("switch to Zundamon")

MCP is optional. Installing it leaves the existing CLI commands and Stop hook fully working.

---

## Prerequisites

- VOICEVOX Engine running (check with `vvread doctor`)
- Python 3.10 or later

---

## Installation

```bash
cd /path/to/voiceClaude
uv sync --extra mcp
```

---

## Register with Claude Code

```bash
claude mcp add --transport stdio --scope local vvread \
  -- /absolute/path/to/voiceClaude/bin/vvread mcp
```

Verify:

```bash
claude mcp list   # should show "vvread"
```

### Using `.mcp.json`

Place `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "vvread": {
      "command": "/absolute/path/to/voiceClaude/bin/vvread",
      "args": ["mcp"]
    }
  }
}
```

### Claude Desktop

> **Note: Not verified**
> Claude Desktop does not guarantee `CLAUDE_PROJECT_DIR`, so the project settings write target may be undefined.

For reference (use at your own risk), add to
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "vvread": {
      "command": "/absolute/path/to/voiceClaude/bin/vvread",
      "args": ["mcp"]
    }
  }
}
```

---

## Migrating from the Stop hook

If you already use the Stop hook (via `vvread install`), MCP is an **addition, not a replacement** — the two run independently.

| | What it reads | When |
|---|---|---|
| Stop hook | The **full response** | **After** a response completes (automatic) |
| MCP | **Only the points Claude picks** | **Mid-task** (Claude's discretion) |

Both read the same `vvread.settings.json`, so speaker/speed settings **carry over with no migration**.

- To **switch fully to MCP** (stop the automatic full-text read-aloud, keep only the highlights), run `vvread uninstall` to remove the Stop hook.
- To **use both** (full text on completion, highlights during work), leave it as is — both stay active.

Check your current registration in the hooks section of `vvread doctor`.

---

## Tool Reference

Each tool carries MCP ToolAnnotations (hints that help Claude choose the right tool):

- 🟢 **read-only** — does not change state (`vvread_status` / `vvread_speakers`)
- 🟡 **state-changing** — starts or stops playback (`vvread_say` / `vvread_stop`)
- 🔴 **destructive** — persistently changes settings (`vvread_config_set`)

### `vvread_say(text, speaker?)` 🟡

Read text aloud via VOICEVOX. Starts playback in the background and returns immediately.

```
vvread_say("Build completed successfully")
vvread_say("An error occurred. Please check the logs.", speaker=3)
```

### `vvread_stop()` 🟡

Stop the current playback.

### `vvread_status()` 🟢

Check playback state. Returns `"state: idle"` or `"state: playing (pid=1234)"`.

### `vvread_speakers()` 🟢

Retrieve available speakers from the configured VOICEVOX Engine.

```json
[
  {
    "name": "Zundamon",
    "styles": [
      {"id": 3, "name": "Normal"},
      {"id": 1, "name": "Amaama"}
    ]
  }
]
```

### `vvread_config_set(key, value)` 🔴

Update a setting in `vvread.settings.json` (project scope).

**Use only when the user explicitly asks to change a setting. Do not change settings proactively.**

Allowed keys:

| Key | Type | Range | Description |
|---|---|---|---|
| `voicevox.speaker` | int | 0–9999 | Speaker ID |
| `voicevox.speed` | float | 0.5–2.0 | Reading speed |
| `voicevox.pitch` | float | -0.15–0.15 | Pitch |
| `voicevox.intonation` | float | 0.0–2.0 | Intonation |
| `voicevox.volume` | float | 0.0–2.0 | Volume |

```
vvread_config_set("voicevox.speaker", "1")
vvread_config_set("voicevox.speed", "1.3")
```

---

## Example Prompts

You can invoke MCP tools using natural language:

- "Notify me by voice when important progress happens during long tasks"
- "Read out only the key points when an error occurs"
- "Let me know by voice when the work is done"
- "Check the available speakers and switch to Zundamon"
- "Change the reading speed to 1.3"

---

## Receiver Integration (Experimental / Research Preview)

> **⚠️ Experimental feature**
> This uses Claude Code **Channels (research preview)**. It requires the `--dangerously-load-development-channels` flag to launch. **End-to-end operation has been verified on real hardware** (2026-06-06), but the specification may change without notice.

So far this guide has been about voicing Claude's **own work**. **Receiver integration voices things that happen outside Claude.**

A CI run failed, a deploy finished, a monitor fired an alert — push these **external events** into the Claude Code session, and Claude summarizes each in 1–2 sentences and reads it aloud. No more sitting on another terminal or dashboard waiting for a result.

For example:

- Turn a GitHub Actions pass/fail into voice with a single `curl`
- Bring server monitoring alerts to your ears mid-task
- Get a completion notice for a batch job that takes tens of minutes

### How it works

```
External event
  → HTTP POST :8788      (receiver/server.ts)
  → notifications/claude/channel
  → arrives in Claude's conversation as a <channel> tag
  → Claude summarizes it in 1–2 sentences
  → vvread_say → played back via VOICEVOX
```

### Prerequisites

- Claude Code 2.1.80 or later
- Authentication via claude.ai or a Console API key (**not available on Amazon Bedrock / Google Vertex**)
- On Team / Enterprise plans, an admin must enable `channelsEnabled`
- Bun 1.2 or later

### Setup

`vvread setup --with-receiver` checks dependencies and registers the server for the current project in one step (manual setup also works):

```bash
# One-step setup (verifies bun deps + local registration; never overwrites existing)
vvread setup --with-receiver

# Or manually:
# 1. Install dependencies
cd /path/to/voiceClaude/receiver
bun install

# 2. Register vvread-receiver for the current project (local scope only; .mcp.json is not modified)
claude mcp add --transport stdio --scope local vvread-receiver \
  -- bun /path/to/voiceClaude/receiver/server.ts

# 3. Launch Claude Code with the development channel loaded
claude --dangerously-load-development-channels server:vvread-receiver
```

> **Queue playback mode recommended**: To avoid the receiver summary and the Stop-hook full reply firing twice (or the summary being cut off mid-sentence), enable `vvread queue on`. It plays utterances in order without interruption, prioritizing the full reply while still voicing the summary.

After launching, run `/mcp` inside Claude Code and confirm that both `vvread` and `vvread-receiver` are connected.
The port can be changed via `VVREAD_RECEIVER_PORT` (default `8788`).

### Sending an event

```bash
curl -X POST http://localhost:8788 -d "Build completed"
```

HTTP response codes:

| Code | Meaning |
|---|---|
| 202 | Accepted (only guarantees the notification was written; **does not guarantee playback**) |
| 400 | Empty body |
| 405 | Non-POST method |
| 413 | Body too large (over 16 KiB) |
| 503 | Channel not connected (e.g. Claude Code not running) |

### Security & trust model

- Event bodies are treated as **untrusted data**. The server's fixed `instructions` ensure Claude does not follow commands inside the body and does not execute commands, modify files, or reveal secrets. It only summarizes CI results, monitoring alerts, and completion notices.
- We recommend granting **only `mcp__vvread__vvread_say`**. Avoid blanket-granting `mcp__vvread__*`, which would include `vvread_config_set` (persistent setting changes).
- The HTTP listener binds to **localhost (127.0.0.1) only**.

### Current limitations

Authentication, sender allowlist, event deduplication, a remote-CI-to-local-PC delivery path, and queuing while no session is running are **not implemented** (planned for the future).

---

## Troubleshooting

### MCP tools don't appear in Claude Code

```bash
claude mcp list   # check if "vvread" is listed
vvread doctor     # check [mcp] section for package status
```

### No audio playback

```bash
vvread doctor     # check VOICEVOX Engine connection
tail -f "$(scripts/paths.py log)/speak.log"   # check logs
```

### `uv sync --extra mcp` fails

```bash
python3 --version   # Python 3.10 or later required
uv --version        # check if uv is installed
```

---

For detailed setup instructions, see [`doc/01-setup.md`](../doc/01-setup.md).
