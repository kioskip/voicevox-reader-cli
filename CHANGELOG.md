# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Dates use ISO 8601 (YYYY-MM-DD).

---

## [Unreleased]

### Added
### Changed
### Deprecated
### Removed
### Fixed
### Security

---

## [0.1.5] - 2026-05-15

### Fixed

- `vvread install` の speaker 選択リストで、左に表示される番号が表示順の連番になっていた問題を修正。正しい style ID が表示されるようになり、入力値と一致するようになった。
- `is_voiceclaude_hook()` の判定条件に `repo_root` パスを用いた過剰一致ロジックが含まれており、別ツールの hook を誤検知する潜在リスクがあった。該当条件を削除し、残る条件のみで全ケースをカバーするよう修正。
- legacy `on_stop.sh` hook が登録済みの環境で `vvread install` を実行すると「すでに設定済」と表示されてインストールがスキップされていた問題を修正。legacy hook を正しく検出し、アンインストール手順をユーザーへ案内するよう改善。
- `vvread install` 完了後に `vvread config` を実行すると「No vvread settings file found」エラーになる問題を修正。already-installed / fresh install（VOICEVOX 未起動）/ `--yes` 非対話パスのいずれでも `vvread.settings.json` が作成されるよう改善。

---

## [0.1.4] - 2026-05-12

### Changed

- `cmd/say.sh` の内部リファクタリング。引数パース・speaker 解決・synth/play チャンクヘルパーを専用ライブラリ（`lib/say_args.sh`、`lib/say_pipeline.sh`）に分離。ユーザー向け挙動の変更はなし。
- speaker ID 解決（`--speaker` フラグ / `VOICEVOX_SPEAKER` 環境変数 / デフォルト `3`）を `lib/voicevox.sh::voicevox_resolve_speaker` に集約。`vvread say` と `vvread synth` で共用するよう統一。
- Python ヘルパースクリプトの冗長な `sys.path.insert` を削除。`scripts/__init__.py` を追加（IDE・静的解析ツール向けのパッケージ認識改善）。
- VOICEVOX 合成パラメータのデフォルト値解決（`VOICEVOX_SPEED`、`VOICEVOX_PITCH` 等）を `lib/voicevox.sh` に集約。`vvread say` / `vvread synth` での重複コードを解消。

---

## [0.1.3] - 2026-05-11

### Changed

- `VOICEVOX_ENGINE_URL` / `VOICEVOX_ENGINE` の参照方法を統一。`cmd/on_stop.sh` に `VOICEVOX_ENGINE` フォールバックを追加し、デフォルト値から `/version` を除去して呼び出し側で付加するように変更。
- `lib/voicevox.sh` の `audio_query` / `synthesis` curl 呼び出しに `-m ${VOICEVOX_TIMEOUT:-30}` を追加。エンジン応答停止時の永久ブロックを防止。`VOICEVOX_TIMEOUT` 環境変数で上書き可能（デフォルト 30s）。
- `scripts/lib/os.sh` を新設し、OS 判定ヘルパーを一元管理。
- `scripts/lib_http.py` を新設。内部 HTTP GET ヘルパーを一本化。
- `scripts/lib_prompt.py` を新設。対話 prompt ヘルパーを共通モジュールに集約。
- `cmd/say.sh` の再生後の wait 終了コードをデバッグログに記録。preempt による中断と player の異常終了を区別できるよう改善。
- 依存チェック時の subprocess timeout を 5s → 1s に短縮。起動時間の改善。

### Fixed

- `sanitize.py` の定数名 `INLINE_CODE_LENGTH_LIMIT` を `INLINE_CODE_LIMIT` に統一。
- `VOICEVOX_MAX_CHARS` に負値が渡された場合の挙動を定義。`MAX_CHARS_LIMIT`（9999）にフォールバックし、stderr に警告を出力。

---

## [0.1.2] - 2026-05-10

### Added

- `vvread speakers`: list available VOICEVOX speaker/style IDs fetched from the Engine's `/speakers` API. Each character is shown on one line (`ID: Character / Style`). Requires a running VOICEVOX Engine; exits with a warning when the Engine is unreachable.
- `vvread config` / `vvread edit`: interactive editor for `vvread.settings.json`. Shows a description, hint, and example for each field before prompting. Editable fields: `engineUrl`, `speaker`, `volume`, `speed`, `pauseScale`, `pitch`, `intonation`, `inlineCodeLimit`, `chunkChars`, `chunkHardMax`, `maxChars`. Requires a TTY; creates a `.bak` before saving; preserves unknown keys.
- `vvread install` now runs interactively by default (TTY required). Prompts for: scope selection (with resolved path displayed), `.claude/` directory creation, and speaker selection (Normal style only). Exits immediately with guidance when a hook is already registered in the chosen scope. `--yes` preserves the previous non-interactive behaviour.
- `scripts/json_file.py`: shared atomic-write / backup utility used by `hook_install.py` and `config.py`.

### Changed

- `vvread install` scope names updated for clarity:

  | New name | Old name | Target file |
  |---|---|---|
  | `project-local` (default) | `project` | `<cwd>/.claude/settings.local.json` |
  | `project` | `project-shared` | `<cwd>/.claude/settings.json` |
  | `user` | `user` | `~/.claude/settings.json` |

### Deprecated

- `--scope project-shared` is now a deprecated alias for `--scope project`. A warning is printed to stderr; the behaviour is unchanged. Will be removed in a future version.

### Migration note (scope rename)

If you previously ran `vvread install --scope project` (which wrote to `settings.local.json`), use `--scope project-local` going forward.
If you previously ran `vvread install --scope project-shared` (which wrote to `settings.json`), use `--scope project` going forward.

---

## [0.1.1] - 2026-05-10

### Added

- New settings keys in `vvread.settings.json`: `voicevox.chunkChars`, `voicevox.chunkHardMax`, `voicevox.inlineCodeLimit` — chunking parameters are now configurable via settings file in addition to environment variables.
- `voicevox.maxChars: 0` now means "no limit" (internally capped at 9999 characters).
- New words in the English-to-katakana dictionary (`WORD_KANA`): `env`, `schema`, `speaker`.

### Fixed

- `vvread.settings.json` values were silently ignored for Python sub-processes (`sanitize`, `chunk-split`). `settings.py env` now outputs `export VAR=val` so configured values propagate correctly through bash `eval`. This means `maxChars` (and other settings) set in `vvread.settings.json` now take effect as expected.

---

## [0.1.0] - 2026-05-07

Initial public release.

### Added

- `vvread` CLI with subcommands: `say`, `synth`, `play`, `on-stop`, `install`, `uninstall`, `doctor`, `setup`, `status`, `stop`, `mute`, `off`, `on`, `clean`.
- Cross-platform support: macOS (first-class), Linux (first-class), WSL (treated as Linux). Windows + Git Bash is best-effort (CLI only, playback unsupported).
- VOICEVOX Engine connection modes: `existing` (default) and `docker`.
- Playback abstraction layer: `afplay` on macOS; `paplay` > `pw-play` > `aplay` > `play` (sox) > `ffplay` on Linux/WSL.
- OS-aware path resolver (`STATE_DIR` / `LOG_DIR` / `CACHE_DIR`) with env overrides (`VVREAD_STATE_DIR` / `VVREAD_LOG_DIR` / `VVREAD_CACHE_DIR`).
- Settings cascade: CLI option > environment variable > project (`<cwd>/vvread.settings.json`) > user (`~/Library/Application Support/vvread/settings.json` on macOS, `${XDG_CONFIG_HOME:-~/.config}/vvread/settings.json` on Linux/WSL) > default. JSONC line comments are supported.
- Claude Code Stop hook integration (`async: true`, `timeout: 600`) installable via `vvread install --scope {project|project-shared|user}`.
- Text sanitization pipeline: number + counter normalization, kanji homonym disambiguation, bare hashes, bare paths, ASCII unit handling, English-to-katakana conversion (with `e2k` when present, falling back to `WORD_KANA` dictionary + character-by-character).
- Wave cache for canned phrases (synthesis skipped on hit).
- Prefetch architecture for chunk-based synthesis (next chunk synthesised while current chunk is playing).
- Session-token preemption: a new response stops the previous response at the next chunk boundary.
- Diagnostics via `vvread doctor` covering OS, runtime, dependencies, engine reachability, hook registration, and effective settings with origin (env / project / user / default).
- Dependency catalog (`scripts/dependencies.py`) covering 16 entries across `runtime` / `setup` / `dev` / `publish` categories.
- Bash 3.2 compatibility rules and a strict-mode + shellcheck convention documented in [`doc/08-bash-rules.md`](https://github.com/kioskip/voicevox-reader-cli/blob/main/doc/08-bash-rules.md), enforced by `scripts/dev/lint.sh`.
- Pytest suite covering sanitize, cache key, settings cascade, dependency catalog, doctor, install/uninstall, and end-to-end command flows.

### Documentation

- English README ([`README.md`](README.md)) and Japanese mirror ([`README.ja.md`](README.ja.md)).
- Detailed Japanese docs under [`doc/`](https://github.com/kioskip/voicevox-reader-cli/tree/main/doc): setup, text pipeline, configuration, observability, cache, voice CLI, workflow, bash rules, settings, dependencies.
- Example settings file ([`vvread.settings.example.json`](vvread.settings.example.json)) covering every supported key with inline comments.

### Notes

- VOICEVOX Engine, VOICEVOX Core, and any voice libraries are **not bundled**. Users are responsible for installing them and complying with each library's terms of use.
- Linux / WSL desktop notifications (`notify-send`) are not yet implemented — failure notifications are macOS-only in v0.1 (`terminal-notifier` > `osascript` fallback).
