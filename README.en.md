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
| **macOS** (Intel / Apple Silicon) | ✅ first-class | Plays through `afplay`. |
| **Linux** (Ubuntu / Debian / Arch, etc.) | ✅ first-class | Auto-selects in this order: `paplay` > `pw-play` > `aplay` > `play` (sox) > `ffplay`. |
| **WSL2** | ✅ first-class | Treated as Linux. Audio output via WSLg. |
| **Windows + Git Bash** | ⚠️ best-effort | No playback (no player binary). CLI-only (`vvread synth`, etc.). |
| **Windows native (PowerShell / cmd.exe)** | ❌ unsupported | Use WSL2 or Git Bash. |

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

### 1. Start VOICEVOX Engine

```bash
# Using Docker (recommended):
docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest

# Or launch the VOICEVOX GUI app.
```

### 2. Set up vvread

```bash
# Run inside the project you want to enable
vvread setup
```

`vvread setup` first displays a status summary showing the current state of engine / e2k / hook.
It then interactively confirms:
- VOICEVOX Engine URL (default `http://127.0.0.1:50021`)
- Whether to install e2k (English-to-kana converter)
- Whether to register the Claude Code Stop hook (with scope selection)

### 3. Test it

```bash
vvread "テスト"    # smoke test
vvread doctor      # health check
```

After this, starting Claude Code will automatically speak each response aloud.

---

## 5. CLI reference

Detailed command references are currently available in Japanese:
- [COMMANDS.md](COMMANDS.md)
- [CONFIGURATION.md](CONFIGURATION.md)

### Common commands

```bash
vvread "text"                  # speak text
vvread file README.md          # read a file aloud
cat build.log | vvread         # pipe input
vvread stop                    # stop playback
vvread doctor                  # health check
```

---

## 6. Configuration

Detailed configuration references are currently available in Japanese:
- [CONFIGURATION.md](CONFIGURATION.md)

### Basic example

```jsonc
{
  "voicevox": {
    "engineUrl": "http://127.0.0.1:50021",
    "speaker": 3,
    "speed": 1.5
  }
}
```

Place `vvread.settings.json` in your project root, or use `vvread config` to edit it interactively.

---

## 7. Claude Code integration

### 7-1. Adding to another project

> **Note**: If you ran `vvread setup`, hook registration was already completed during setup.
> You do not need to run `vvread install` again for the same project.
>
> Use `vvread install` when you want to add the hook to a different project or to all projects (user scope).

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
