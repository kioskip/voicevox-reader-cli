# vvread

`vvread` is a **CLI that speaks arbitrary text through the VOICEVOX HTTP API**. It was created because attempting to read Claude Code's full responses aloud through VOICEVOX led to issues such as running out of memory and commands being read verbatim — so an intermediate processing layer was built to enable smooth playback.

>　日本語 Japanese: [`README.md`](README.md)

---

## ⚠️ About the VOICEVOX terms (please read first)

This tool **does not include VOICEVOX Engine, VOICEVOX Core, or any voice libraries** (Zundamon, Shikoku Metan, etc.).

- Installing, running, and complying with the VOICEVOX Engine terms of use is the user's responsibility.
- Use of generated audio is governed by the terms of the voice library (character) you choose, including any required attribution and any conditions on commercial use.
- This CLI is purely an HTTP API client. Official site: https://voicevox.hiroshiba.jp/

---

## 1. Overview

### Who this is for

- People using Claude Code who want to listen to responses in the background
- People who want to speak terminal text through VOICEVOX

### Highlights

- 📢 **Text-to-speech**: synthesises a wav via VOICEVOX and plays it directly.
- ⚡ **Prefetch**: long input is split into chunks; the next chunk is synthesised while the current one is playing.
- 🔁 **Session preemption**: when a new response arrives, playback of the previous response stops at the next chunk boundary.
- 💾 **Wav cache**: short canned phrases (e.g. "完了しました") skip synthesis entirely.
- 🧹 **Mispronunciation guards**: a sanitization pipeline for digits + counters, kanji-numeral dates, ASCII units, path elision, hash elision, and homographs (e.g. 「あの方」, 「最中」).
- 🔤 **English-to-katakana**: optional `e2k` plus a built-in dictionary turn things like `Docker` into `ドッカー`.

---

## 2. Supported environments

| OS / Shell | Support | Notes |
|---|---|---|
| **macOS** (Intel / Apple Silicon) | tested | Plays through `afplay`. |
| **Linux / WSL2** (Ubuntu / Debian / Arch, etc.) | untested | Auto-selects in this order: `paplay` > `pw-play` > `aplay` > `play` (sox) > `ffplay`. WSL2 routes audio through WSLg. |

### Required dependencies

| Dependency | Purpose | Notes |
|---|---|---|
| `bash` 3.2+ | Shell scripts | Designed to run on macOS's `/bin/bash` (3.2). |
| `python3` 3.10+ | sanitize / cache_key / parse_transcript | CI matrix = 3.10 + 3.12. |
| `curl` | VOICEVOX API calls | |
| **VOICEVOX Engine** | The synthesis backend itself | Install separately (see "Preparing the VOICEVOX Engine"). |

### Optional dependencies

| Dependency | What changes |
|---|---|
| `jq` | Faster `settings.json` merging and synthesis-parameter tuning (falls back to Python's `json` module). |
| `docker` + `docker compose` | Convenient way to run VOICEVOX Engine in a container. |
| `e2k` (Python pkg) | Better English-to-katakana accuracy (without it, the dictionary + character-by-character fallback is used). |
| `terminal-notifier` (macOS) | Desktop notifications on failure (macOS only). |

Run `vvread doctor` to inspect every dependency, its detection result, and an OS-specific install hint.

---

## 3. Installation

### 3-1. Manual install

```bash
git clone https://github.com/kioskip/voicevox-reader-cli.git ~/.local/share/vvread
ln -s ~/.local/share/vvread/bin/vvread ~/.local/bin/vvread
# Add ~/.local/bin to PATH if needed
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc  # or ~/.zshrc
exec $SHELL -l
vvread doctor
```

### 3-2. Preparing the VOICEVOX Engine

This CLI does not bundle the VOICEVOX Engine. Provide one yourself, e.g.:

- Docker: `docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest`
- Desktop app: download from https://voicevox.hiroshiba.jp/

Default URL: `http://127.0.0.1:50021` (override via `VOICEVOX_ENGINE_URL` or `vvread.settings.json`).

---

## 4. Quick start

```bash
# 0. Make sure VOICEVOX Engine is running (see 3-2)

# 1. After install, run the interactive setup
#    (run from inside the project you want to enable)
vvread setup

# 2. Smoke test
vvread "テスト"

# 3. Health check
vvread doctor
```

After this, starting Claude Code will automatically speak each response aloud.

---

## 5. CLI reference

### 5-1. Speech

| Command | Description |
|---|---|
| `vvread <text> [--speaker N]` | Synthesize and play text directly. |
| `vvread file <path> [--speaker N]` | Read a file aloud. |
| `cat file \| vvread [--speaker N]` | Read stdin aloud (piped input only). |
| `vvread say <text> [--speaker N]` | Same as above; legacy compatible form. |
| `vvread synth <text> --output FILE [--speaker N]` | Synthesize only. Writes wav to `FILE` (does not play). |
| `vvread play <wav>` | Play an existing wav file. |
| `vvread on-stop` | Entry point for Claude Code's Stop hook (do not invoke manually). |

#### Examples

```bash
vvread "ビルドが完了しました"
vvread file /tmp/summary.txt
cat build.log | vvread
vvread synth "おはようございます" --output morning.wav --speaker 1
vvread play morning.wav
```

#### Notes

- **Subcommand names are never treated as text.** `vvread doctor` runs the doctor command; to speak the word "doctor", use `vvread say "doctor"`.
- **Options must come after the text.** `vvread "hello" --speaker 8` works; `vvread --speaker 8 "hello"` does not.
- **Only explicit pipe input is detected.** `cat file | vvread` works; redirect (`vvread < file`) is not supported — use `vvread file <path>` instead.

### 5-2. Control

| Command | Description |
|---|---|
| `vvread stop` | Immediately stop the current playback (subsequent speech is still accepted). |
| `vvread mute <duration>` | Mute for a fixed duration (e.g. `30s`, `5m`, `2h`). |
| `vvread off` | Disable persistently (until `vvread on`). |
| `vvread on` | Re-enable. |
| `vvread status` | Show the current state. |
| `vvread clean` | Delete orphan temporary wav files (does not touch cache or log). |

### 5-3. Setup & hooks

| Command | Description |
|---|---|
| `vvread setup [--yes]` | Interactive setup (engine reachability check + e2k + Claude hook registration). |
| `vvread install [--scope SCOPE] [--yes] [--dry-run]` | Register the Claude Code hook interactively (TTY) or with `--yes` for non-interactive use. |
| `vvread uninstall [--scope SCOPE]` | Unregister the hook. |
| `vvread speakers` | List available speaker/style IDs from the VOICEVOX Engine. |
| `vvread config [--create] [--dry-run]` / `vvread edit` | Interactively edit `vvread.settings.json` (TTY required). `--create` creates the file if it doesn't exist before opening the editor; `--dry-run` previews changes without saving. |
| `vvread doctor [--offline]` | Health check. |

#### `--scope` values

| Scope | Target file | Use case |
|---|---|---|
| `project-local` (default) | `<cwd>/.claude/settings.local.json` | Gitignored by default; safest option. |
| `project` | `<cwd>/.claude/settings.json` | Use when sharing hook config with a team. |
| `user` | `~/.claude/settings.json` | Enables voiceClaude for every project. |

```bash
vvread install                        # interactive: prompts for scope + speaker
vvread install --yes                  # non-interactive, project-local (default)
vvread install --scope user --yes     # non-interactive, all projects
vvread speakers                       # list speaker IDs
vvread config                         # edit settings interactively
```

### 5-4. Sample `vvread doctor` output

```
$ vvread doctor
[OK] OS              : darwin (24.6.0)
[OK] shell           : bash 5.2.21
[OK] python          : 3.12.4
[OK] vvread PATH     : /Users/foo/.local/bin/vvread
[OK] VOICEVOX URL    : http://127.0.0.1:50021 (source: project settings)
[OK]   /version      : 0.21.1
[OK]   /speakers     : 81 entries
[OK]   speaker=3     : ずんだもん (ノーマル)
[OK] jq              : 1.7.1
[--] docker          : not installed (optional under engine=existing)
[OK] uv              : 0.4.18
[OK] e2k             : importable
[OK] hook (user)     : registered (1 entry)
[OK]   command       : /Users/foo/.local/bin/vvread on-stop
[--] hook (project)  : not registered
[OK] settings.json   : valid JSON, no duplicates
[OK] paths           : state=~/Library/Application Support/vvread/
                       log=~/Library/Logs/vvread/
                       cache=~/Library/Caches/vvread/
```

Failing rows are tagged `[NG]` and include a recovery command.

---

## 6. Configuration

### 6-1. Configuration files / precedence

```
CLI option > environment variable > project settings > user settings > default
```

The configuration file is **`vvread.settings.json`** (JSONC line comments `//` are supported).

#### Lookup order

| Kind | Path | Use case |
|---|---|---|
| **project** | `<cwd>/vvread.settings.json` | Project-specific synthesis parameters, etc. |
| **user** | macOS: `~/Library/Application Support/vvread/settings.json`<br/>Linux/WSL: `${XDG_CONFIG_HOME:-~/.config}/vvread/settings.json` | Defaults shared across all projects. |

#### Schema example

Copy [`vvread.settings.example.json`](vvread.settings.example.json) and edit. Minimal example:

```jsonc
{
  "voicevox": {
    "engineUrl": "http://127.0.0.1:50021",
    "speaker": 3,
    "speed": 1.5,
    "maxChars": 500,   // 0 = no limit (internally capped at 9999)
    "chunkChars": 200,
    "chunkHardMax": 400,
    "inlineCodeLimit": 25
  },
  "log": {
    "level": "INFO"
  }
}
```

Run `vvread doctor` to print every active key, its current value, and where it came from (env / project / user / default).

### 6-2. Environment variables

Every key in `vvread.settings.json` can also be set through an environment variable. Precedence: **env > project settings > user settings**.

#### Connection

| Variable | Default | Description |
|---|---|---|
| `VOICEVOX_ENGINE_URL` | `http://127.0.0.1:50021` | Base URL for VOICEVOX Engine. |

#### Synthesis parameters

| Variable | Default | Description |
|---|---|---|
| `VOICEVOX_SPEAKER` | `3` | Speaker ID (look up via `vvread doctor`). |
| `VOICEVOX_SPEED` | `1.5` | Speed multiplier. |
| `VOICEVOX_PITCH` | `0` | Pitch shift. |
| `VOICEVOX_INTONATION` | `1.0` | Intonation scale. |
| `VOICEVOX_VOLUME` | `1.0` | Volume. |
| `VOICEVOX_PAUSE_SCALE` | `1.0` | Pause-length scale. |
| `VOICEVOX_PRE_PHONEME` | `0` | Silence before speech (seconds). |
| `VOICEVOX_POST_PHONEME` | `0` | Silence after speech (seconds). |
| `VOICEVOX_MAX_CHARS` | `500` | Max input length; excess is truncated. `0` means no limit (internally capped at 9999). |
| `VOICEVOX_MAX_CHUNKS` | `0` | Max chunks to generate. `0` = no limit (default). Excess chunks are dropped with `(以下省略)` appended. |
| `VOICEVOX_CHUNK_CHARS` | `200` | Target chars per chunk (2nd chunk onward). |
| `VOICEVOX_CHUNK_HARD_MAX` | `400` | Hard maximum chars per chunk. |
| `VOICEVOX_INLINE_CODE_LIMIT` | `25` | Max inline-code length before abbreviating. |

#### Logging / notifications

| Variable | Default | Description |
|---|---|---|
| `VOICEVOX_LOG_LEVEL` | `INFO` | `OFF` / `INFO` / `DEBUG`. |
| `VOICEVOX_LOG_MAX_BYTES` | `10485760` (10 MiB) | Rotates one generation (`.1`) on overflow. |
| `VOICEVOX_NOTIFY_COOLDOWN` | `60` | Minimum interval (seconds) between failure notifications. |

#### Path overrides

| Variable | Purpose |
|---|---|
| `VVREAD_STATE_DIR` | session.id / playing.pid / disabled / mute_until / temp wav |
| `VVREAD_LOG_DIR` | `speak.log` |
| `VVREAD_CACHE_DIR` | wav cache |

#### Per-OS defaults

| OS | state | log | cache |
|---|---|---|---|
| macOS | `~/Library/Application Support/vvread/` | `~/Library/Logs/vvread/` | `~/Library/Caches/vvread/` |
| Linux / WSL | `${XDG_STATE_HOME:-~/.local/state}/vvread/` | `${XDG_STATE_HOME:-~/.local/state}/vvread/logs/` | `${XDG_CACHE_HOME:-~/.cache}/vvread/` |

---

## 7. Claude Code integration

### 7-1. Automatic setup

```bash
vvread install --scope user
```

This appends the following to `hooks.Stop[].hooks[]` in `~/.claude/settings.json`:

```json
{
  "type": "command",
  "command": "/Users/foo/.local/bin/vvread on-stop",
  "async": true,
  "timeout": 600
}
```

> ⚠️ `async: true` **requires Claude Code 2.1.110+**. On older versions it falls back to synchronous execution and the next prompt is blocked. `vvread doctor` warns when an older version is detected.

**Why `timeout: 600` (seconds)**: long responses (~5,000 characters) can take over five minutes to synthesise and play. Because `async: true` means Claude itself is not waiting, a generous timeout avoids cutoffs.

---

## 8. Troubleshooting

### No sound

```bash
vvread doctor          # start here
vvread status          # check current state
```

Common causes:
- VOICEVOX Engine isn't running → start it (Docker / desktop app) and re-run `vvread doctor`.
- `~/.local/bin` is not on PATH → add it to `~/.bashrc` or `~/.zshrc`.
- macOS notification permission was never granted → install `terminal-notifier`.

### Double speech / stale audio mixed in

```bash
vvread stop    # stop immediately
vvread clean   # remove orphans
```

### Claude Code is blocked waiting for the next prompt

Check `hook async=true` with `vvread doctor`. If `async` is disabled, the hook runs synchronously.

---

## 9. License / credits

- This CLI code: **MIT License** (see [`LICENSE`](LICENSE)).
- VOICEVOX Engine / Core / voice libraries are **not** included. Comply with their respective terms separately.
- Use of generated audio is governed by **the chosen voice library's terms** (e.g. Zundamon: the Tohoku Itako / Zundamon project terms; Shikoku Metan: SSS LLC's terms).
- Parts of this project were developed with the assistance of Claude Code. All code has been reviewed and is maintained by the project author.

---

## 10. CHANGELOG

See [`CHANGELOG.md`](CHANGELOG.md). Keep a Changelog format + semver.
