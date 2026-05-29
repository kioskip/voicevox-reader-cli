# CLI リファレンス

> 英語版ドキュメントは準備中です。

---

## 発話系

| コマンド | 説明 |
|---|---|
| `vvread <text> [--speaker N]` | テキストを直接読み上げ |
| `vvread file <path> [--speaker N]` | ファイルの内容を読み上げ |
| `cat file \| vvread [--speaker N]` | stdin を読み上げ（パイプ入力のみ対応） |
| `vvread say <text> [--speaker N]` | テキストを合成して再生（互換形式） |
| `vvread synth <text> --output FILE [--speaker N]` | 合成のみ。wav を FILE に書き出す（再生しない） |
| `vvread play <wav>` | 既存 wav を再生 |
| `vvread on-stop` | Claude Code の Stop hook 用エントリ（手動では呼ばない） |

### 例

```bash
vvread "ビルドが完了しました"
vvread file /tmp/summary.txt
cat build.log | vvread
vvread synth "おはようございます" --output morning.wav --speaker 1
vvread play morning.wav
```

### 注意事項

- **サブコマンド名はテキストとして扱われない。** `vvread doctor` は doctor コマンドを実行する。「doctor」という単語を読み上げるには `vvread say "doctor"` とする。
- **オプションはテキストの後ろに書く。** `vvread "hello" --speaker 8` は動く。`vvread --speaker 8 "hello"` は動かない。
- **パイプ入力のみ対応。** `cat file | vvread` は動く。リダイレクト（`vvread < file`）は非対応 — `vvread file <path>` を使う。

---

## 制御系

| コマンド | 説明 |
|---|---|
| `vvread stop` | 再生中の音を即停止（次の発話は受け付ける） |
| `vvread mute <duration>` | 一定時間ミュート（例: `30s`, `5m`, `2h`） |
| `vvread off` | 永続オフ（`vvread on` まで） |
| `vvread on` | 復帰 |
| `vvread status` | 現状表示 |
| `vvread clean` | 一時 wav の削除（割り込みで消されなかった合成済み wav）と wav キャッシュのクリアを行う |

---

## セットアップ & hook

| コマンド | 説明 |
|---|---|
| `vvread setup [--yes]` | 対話セットアップ（engine 疎通確認 + e2k + Claude hook 登録） |
| `vvread install [--scope SCOPE] [--yes] [--dry-run]` | Claude Code hook を対話式（TTY）または `--yes` で非対話登録 |
| `vvread uninstall [--scope SCOPE]` | hook を解除 |
| `vvread speakers` | VOICEVOX Engine から利用可能な speaker/style ID 一覧を表示 |
| `vvread config [--set KEY=VALUE] [--json '{...}'] [--user-setting] [--list] [--create] [--dry-run]` / `vvread edit` | `vvread.settings.json` を編集。デフォルトは対話式（TTY 必須）。`--set`/`--json` で TTY 不要の非対話モードに切り替わる。`--user-setting` でユーザースコープのファイルを対象にする |
| `vvread doctor [--offline]` | ヘルスチェック |

### `--scope` の値

| Scope | 対象ファイル | 用途 |
|---|---|---|
| `project-local`（デフォルト） | `<cwd>/.claude/settings.local.json` | 通常は gitignore 対象。最も安全なデフォルト |
| `project` | `<cwd>/.claude/settings.json` | チームで共有したい場合 |
| `user` | `~/.claude/settings.json` | 全プロジェクトで有効化 |

> **ヒント**: Git リポジトリ外（ホームディレクトリなど）で対話実行した場合は、`user` scope が自動的に推奨されます。全プロジェクトで音声読み上げを有効にしたい場合に便利です。

```bash
vvread install                        # 対話式（scope・speaker を選択）
vvread install --yes                  # 非対話、project-local（デフォルト）
vvread install --scope user --yes     # 非対話、全プロジェクト対象
vvread speakers                       # speaker ID 一覧を確認
vvread config                               # 設定を対話編集（TTY）
vvread config --set voicevox.speakerId=3    # 非対話: 単一キーを設定
vvread config --json '{"voicevox.speakerId":3}' --user-setting  # ユーザースコープに設定
vvread config --list                        # 有効な全設定を一覧表示
```

`vvread config --list` の出力例:

```
voicevox.engineUrl	http://127.0.0.1:50021
voicevox.speaker	3
voicevox.speed	1.5
voicevox.maxChunks	0
```

### `vvread doctor` 出力例

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
[--] docker          : not installed (engine=existing なので任意)
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

NG があれば各行に `[NG]` + 復旧コマンド例が併記されます。
