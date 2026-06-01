# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Dates use ISO 8601 (YYYY-MM-DD).

---

## [Unreleased]

### Added
### Changed
### Fixed

---

## [0.3.0] - 2026-06-01

### Added

- **マルチエンジン並列合成 / Prefetch**: 複数の VOICEVOX Engine に chunk を round-robin で分散し、synth/play をオーバーラップさせる Producer/Consumer アーキテクチャを実装した。M=1 でも再生中に次 chunk の合成が進む (prefetch)。
- `voicevox.engines` 設定スキーマを追加。`engineUrl` から自動派生、`vvread config --json` 経由で複数エンジン URL を設定可能。`VOICEVOX_ENGINES='url1;url2'` 形式の env 設定にも対応。
- `voicevox_synthesize` に engine URL 引数を追加。省略時は従来の env fallback を維持（後方互換）。
- `vvread doctor` が `voicevox.engines` に列挙した全 URL の疎通確認を実施するようになった。一部到達不可は WARN、全到達不可は ERROR で exit 1。

---

## [0.2.8] - 2026-05-31

### Added

- `vvread config`: 対話中に `N` を入力するとそのキーを JSONC コメントとして書き出すようになった。カスケード上「未設定」扱いとなり user/default 設定に委ねられる (B-119)

### Changed

- `vvread setup`: scope 選択 UI を `vvread install` と同じ番号メニュー方式に統一した (U-113)
- `vvread setup`: 対話 yes/no プロンプトを `lib_prompt.prompt_yn` に統一した (U-114)
- `scripts/lib_git.py`: `_in_git_repo()` を独立モジュールとして切り出し、`setup.py` / `hook_install.py` から import するよう変更した (U-115)
- `scripts/hook_install.py`: Engine 疎通確認を `lib_http.http_get` に一本化し、`urllib` 直接依存を削除した (U-116)

### Fixed

- `scripts/voice.sh`: ヘッダーコメント・usage メッセージに残存していた旧 CLI 名 `voice` を全 6 箇所 `vvread` に修正した。`vvread on` / `vvread off` の出力メッセージを日本語化した (F-111)

---

## [0.2.7] - 2026-05-30


### Added

- `vvread setup`: 実行冒頭に engine / e2k / hook の現在状態サマリを表示するようになった。実行前に何が設定済みか一目で確認できる (U-112)
- `vvread setup` / `vvread install`: Git リポジトリ外で対話実行した場合、`user` scope を推奨する警告と導線を追加した (U-105)

### Changed

- `doc/00-project-policy.md`: `install` コマンドを維持する理由（pre-commit / husky 慣習）を設計方針に明文化

---

## [0.2.6] - 2026-05-29

### Added

- `scripts/hook_status.py`: hook 登録状態の判定ロジックを `hook_install.py` から独立モジュールとして分離。`config.py` と `hook_install.py` の両方が参照し、import の向きを一方向に固定した (U-111)

### Changed

- `bin/vvread` / `scripts/setup.py` / `scripts/hook_install.py` / `doc/01-setup.md` / `README.md` / `README.ja.md` / `publish/README.md` / `publish/README.en.md`: `vvread setup`（初回セットアップ一括）と `vvread install`（別プロジェクトへの追加登録）の役割の違いをヘルプ・README・doc で明記した (U-111)

### Fixed

- `scripts/config.py`: `vvread config` で Ctrl+C 時に `KeyboardInterrupt` のトレースバックが出力される問題を修正。`__main__` で catch し「キャンセルしました。」を stderr 出力して exit 0 で終了する (U-109)
- `scripts/config.py`: `vvread.settings.json` が存在しない場合に `vvread config` が即 FAIL する問題を修正。modern hook が登録済みなら project settings を自動作成するフローへ進む。legacy hook なら移行案内、hook 未登録なら `vvread setup` / `vvread install` / `--create` の 3 択を案内する (F-112)

---

## [0.2.5] - 2026-05-28

### Added

- `cache_key.py` / `cache_patterns.py`: 定型フレーズ（「了解しました」「以上です」等）の wav キャッシュ読み書きを実装。キャッシュヒット時は合成をスキップして既存 wav を再生し、初回のみ合成結果を自動保存する。キャッシュキーはテキスト正規化後の文字列 + speaker ID + 合成パラメータ（speed / pitch / intonation 等）から SHA-256 で生成する (T-008, R-104)
- `VVREAD_CACHE_FIRST_CHUNK_RAW` 環境変数 / `cache.firstChunkRaw` 設定キー: 1st chunk をキャッシュキー計算なしで raw テキストのまま wav キャッシュに登録するオプションを追加。定型的な冒頭フレーズの初回合成レイテンシを削減できる (T-011)
- `tests/test_cache_key.py`: `cache_key.py` / `cache_patterns.py` のユニットテストを新規作成。パターン追加時は positive + negative の両ケースを追加する規約を整備 (R-112)

### Changed

- `vvread clean`: `CACHE_DIR` 下の wav ファイルも削除対象に追加。従来は STATE_DIR / LOG_DIR のみが対象だったが、キャッシュ肥大化時に `vvread clean` 一発でリセットできるようになった (T-012)

---

## [0.2.4] - 2026-05-19

### Fixed

- `bin/vvread`: 無効な cwd（削除済みディレクトリ等）から `vvread off` / `on` 等の制御コマンドを実行した際に出る複数行の `shell-init` / `chdir` エラーを解消。直接実行・symlink 経由インストール（`~/.local/bin/vvread`）の両方に対応。5 行 → 1 行に削減（残 1 行は bash 起動時 C 初期化のため不可避） (U-107, F-110, U-108)
- `kana_dict.py`: `.lua` 拡張子（`ドットルア`）と `critical`（`クリティカル`）の読み仮名を追加 (F-109)
- `TestSessionTokenPreemption` flaky を解消。synthesis 検知条件を強化し、`marker_dir` で再生開始確認、`date +%s%N` の macOS 非互換も修正 (F-108)
- テストスイート全 35 ファイルのポーリング統一・環境変数隔離・timeout 補完 (R-114)

---

## [0.2.3] - 2026-05-18

### Added

- `publish/COMMANDS.md`: CLI リファレンス（日本語）を独立ファイルとして新規作成。`publish/README.md` §5 の詳細リファレンスを分離し、`vvread config --list` の出力例も追記。
- `publish/CONFIGURATION.md`: 設定リファレンス（日本語）を独立ファイルとして新規作成。`publish/README.md` §6 の詳細内容を分離。

### Changed

- `publish/README.md` / `publish/README.en.md`: Quick Start（§4）に VOICEVOX Engine 起動コマンド（`docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest`）を直書きし、§3 へのスクロールバックが不要になった。`vvread setup` の対話内容も 3 行で明記。
- `publish/README.md` / `publish/README.en.md`: サポート環境テーブルを正確化。Linux・WSL2 を一級対応、Git Bash を best-effort、Windows native を対象外と明記（従来は「Linux 未テスト」などの不正確な表記が残っていた）。§5/§6 を `COMMANDS.md` / `CONFIGURATION.md` へのリンクに軽量化。
- `README.md` / `README.ja.md`: 開発者・メンテナ向けに全面刷新。エンドユーザー向けのインストール手順を削除し、ワークフロー・リリース手順・ドキュメント索引・開発コマンドを追加。冒頭に利用者向け README への導線を追記。

### Fixed

- `README.ja.md` / `doc/01-setup.md` / `doc/04-observability.md`: `--scope project-shared`（deprecated alias）の表記を `project` / `project-local` / `user` に修正。
- `tests/test_cmd_say.py`: preemption テストの固定 `time.sleep` をポーリングベースに変更し、全件テスト実行時の flaky を解消。

---

## [0.2.2] - 2026-05-17

### Added

- `voicevox.maxChunks` / `VOICEVOX_MAX_CHUNKS`: チャンク生成数の上限を設定できるようになった。`0` は無制限(デフォルト)、正値でその数を超えたチャンクを「以下省略」付加で打ち切る。`maxChars`(テキスト全体文字数上限)とは独立した制御。負値は警告後 `0` にフォールバック。
- `vvread config --list`: 設定可能な全キーと現在の cascade 解決値を非対話で一覧表示する。非TTY環境でも実行可能。
- `constants.TRUNCATION_SUFFIX`: 省略通知「(以下省略)」を定数化。`maxChars` と `maxChunks` の両方で共有し、将来の文言変更を一箇所で管理できるようにした。

---

## [0.2.1] - 2026-05-16

### Added

- `vvread config --set KEY=VALUE` / `--json '{...}'`: TTY 不要の非対話モードを追加。スクリプト・CI・Claude Code Stop hook からの設定変更が可能になった。ファイル不在時は `{}` から自動作成するため `--create` は不要。
- `vvread config --user-setting`: プロジェクト設定の代わりにユーザースコープ設定ファイルを対象にするオプションを追加。`--set`/`--json` との組み合わせで非対話にユーザー設定を変更できる。
- `vvread config --dry-run` が非対話モード（`--set`/`--json`）でも有効に。差分サマリを表示し、ファイルは変更しない。

### Changed

- `vvread config` の出力をシンプル化。成功時は `Updated: <path>` のみを表示し、終了コードを 0（成功）/ 1（失敗）に統一した。

---

## [0.2.0] - 2026-05-16

### Added

- `vvread <text> [--speaker N]`: root コマンドで直接テキストを読み上げられるようになった。`vvread say "..."` と等価なショートハンド。
- `vvread file <path> [--speaker N]`: ファイルの内容を読み上げる新 subcommand を追加。ファイル検証（存在・読み取り可否・空ファイル）を行ったうえで `cmd/say.sh` に委譲する。
- `cat file | vvread [--speaker N]`: パイプ入力（stdin）を自動検出して読み上げる。`[ -p /dev/stdin ]` で named pipe のみ判定するため、redirect や pytest 環境では誤検知しない。

### Changed

- **【変更】** `vvread` に存在しないコマンド名を渡しても、エラーではなくテキストとして読み上げるようになった。たとえば `vvread typo` は "typo" を読み上げる。コマンド名を誤入力してもエラーにならない点に注意。
- `bin/vvread` の dispatch ロジックを全面改訂。`_is_subcommand()` ヘルパーを新設し、既知の subcommand と直接テキスト入力・stdin の3経路を明確に分離。
- README / help を新 CLI 形式に更新。subcommand 名との衝突・option 順序・redirect 非対応の3点の注意事項を追記。
- README から「v0.1/v0.2 候補」等のバージョン表記および付録セクションを削除。

---

## [0.1.6] - 2026-05-15

### Added

- `vvread config --create`: 設定ファイルが存在しない場合に空の `{}` で新規作成してから編集を開始するオプションを追加。`vvread install` で設定ファイルが作られなかった場合の後処理に利用できる。

### Changed

- `vvread install` を2段階フローに再設計。Step 1 で現在の hook 登録状態（user / project / project-local スコープ）を表示してから Step 2 でスコープ・speaker 選択を行う。既に登録済みのスコープを再確認した上で意図したスコープを選びやすくなった。
- `vvread install` で複数スコープに hook が登録されている場合（例: user + project-local）、完了後に重複登録の注意文言と片方を削除するコマンド例（`vvread uninstall --scope <scope>`）を表示するようになった。
- `vvread config` のエラーメッセージを日本語化。TTY が利用できない場合と設定ファイルが存在しない場合のエラーに対処方法を含む案内文を表示するよう改善。

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
