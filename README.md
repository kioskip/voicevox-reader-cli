# vvread

`vvread`は **VOICEVOX HTTP APIを叩いて任意のテキストを読み上げるCLI** です。
作成の経緯としては、VOICEVOXを使って、Claude Codeの処理結果全文を読み上げようとしたところ、メモリ不足やコマンドをそのまま読みあげるなど聞き取りづらくなったので、スムーズに読み上げるための中間処理を行う目的で作成しました。

> English 英語版: [`README.en.md`](README.en.md)

---

## ⚠️ VOICEVOX 規約に関する注意事項（必ず読むこと）

このツールは **VOICEVOX Engine / VOICEVOX Core / 各音声ライブラリ を一切同梱しません**。

- VOICEVOX Engineの入手・起動・利用規約遵守はユーザの責任で行ってください。
- 合成された音声の利用には、選択した音声ライブラリ（キャラクタ）ごとの規約（クレジット表記の要否・商用利用条件など）が適用されます。
- 本CLIはHTTP APIクライアントに過ぎません。VOICEVOX 公式: https://voicevox.hiroshiba.jp/

---

## 1. 概要

### こんな人向け

- Claude Codeを使っていて、応答を聞き流したい人
- ターミナル上のテキストをVOICEVOXで読み上げたい人

### 主な特徴

- **読み上げ**: VOICEVOXで合成したwavファイルをそのまま読み上げ。
- **分割読み込み**: 長文を分割し、再生中に次を合成して準備。
- **セッション割り込み**: 新しい応答が来たら旧応答の再生は次境界で停止。
- **wavキャッシュ**: 短い定型文（"完了しました" 等）はキャッシュして、合成スキップ
- **誤読対策**: 数字+助数詞・漢数字日付・ASCII 単位・パス省略・ハッシュ省略・同形異音漢字の整形パイプライン
- **英単語のカナ化**: e2k（任意）+ 内蔵辞書で `Docker` → `ドッカー` のような変換

---

## 2. サポート環境

| OS / Shell | 対応度 | 備考 |
|---|---|---|
| **macOS**(Intel / Apple Silicon) | 対応（テスト済み） | 合成した wav を `afplay` で再生 |
| **Linux / WSL2**(Ubuntu / Debian / Arch 等) | 対応（未テスト） | `paplay` > `pw-play` > `aplay` > `play`(sox) > `ffplay` の優先順で自動選択。WSL2 は WSLg 経由で音声出力 |

### 依存

| 依存 | 用途 | 備考 |
|---|---|---|
| `bash` 3.2+ | スクリプト実行 | macOS の `/bin/bash` (3.2) で動くよう実装 |
| `python3` 3.10+ | sanitize / cache_key / parse_transcript | CI matrix = 3.10 + 3.12 |
| `curl` | VOICEVOX API 呼び出し | |
| **VOICEVOX Engine** | 音声合成本体 | 別途インストール（後述「VOICEVOX Engine の準備」参照） |

### 任意依存

| 依存 | 影響 |
|---|---|
| `jq` | settings.json マージ / 発話パラメータ tuning が高速化（無ければ Python `json` フォールバック） |
| `docker` + `docker compose` | VOICEVOX Engine をコンテナで起動するのに便利 |
| `e2k`(Python pkg) | 英単語のカナ化精度向上（無ければ辞書 + 逐字 fallback） |
| `terminal-notifier`(macOS) | 失敗時のデスクトップ通知（macOS のみ） |

`vvread doctor` で全依存とその検出結果、OS 別インストールヒントが確認できます。

---

## 3. インストール

### 3-1. 手動インストール

```bash
git clone https://github.com/kioskip/voicevox-reader-cli.git ~/.local/share/vvread
ln -s ~/.local/share/vvread/bin/vvread ~/.local/bin/vvread
# ~/.local/bin が PATH に無ければ追加
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc  # or ~/.zshrc
exec $SHELL -l
vvread doctor
```

### 3-2. VOICEVOX Engine の準備

本 CLI は VOICEVOX Engine を同梱しないため、別途用意が必要です。例:

- Docker: `docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest`
- デスクトップ版: https://voicevox.hiroshiba.jp/ からダウンロード

既定 URL: `http://127.0.0.1:50021`（`VOICEVOX_ENGINE_URL` または `vvread.settings.json` で上書き可）。

---

## 4. クイックスタート

```bash
# 0. VOICEVOX Engine を先に起動しておく（3-2 参照）

# 1. インストール後、対話セットアップ
# ※ 有効にしたいプロジェクト内で実行
vvread setup

# 2. 動作確認
vvread say "テスト"

# 3. ヘルスチェック
vvread doctor
```

これで Claude Code を起動すると、応答が自動で読み上げられます。

---

## 5. CLI リファレンス

### 5-1. 発話系

| コマンド | 説明 |
|---|---|
| `vvread say <text> [--speaker N]` | テキストを 1 度合成して再生 |
| `vvread synth <text> --output FILE [--speaker N]` | 合成のみ。wav を FILE に書き出す（再生しない） |
| `vvread play <wav>` | 既存 wav を再生 |
| `vvread on-stop` | Claude Code の Stop hook 用エントリ（手動では呼ばない） |

#### 例

```bash
vvread say "ビルドが完了しました"
vvread synth "おはようございます" --output morning.wav --speaker 1
vvread play morning.wav
```

### 5-2. 制御系

| コマンド | 説明 |
|---|---|
| `vvread stop` | 再生中の音を即停止（次の発話は受け付ける） |
| `vvread mute <duration>` | 一定時間ミュート（例: `30s`, `5m`, `2h`） |
| `vvread off` | 永続オフ（`vvread on` まで） |
| `vvread on` | 復帰 |
| `vvread status` | 現状表示 |
| `vvread clean` | 合成後に割り込みがあり消されなかった一時wavを掃除 |

### 5-3. セットアップ & hook

| コマンド | 説明 |
|---|---|
| `vvread setup [--yes]` | 対話セットアップ（engine 疎通確認 + e2k + Claude hook 登録） |
| `vvread install [--scope SCOPE] [--dry-run]` | Claude Code hook を `settings.json` に登録 |
| `vvread uninstall [--scope SCOPE]` | hook を解除 |
| `vvread doctor [--offline]` | ヘルスチェック |

### 5-4. `vvread doctor` 出力例

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

---

## 6. 設定

### 6-1. 設定ファイル / 優先順位

```
CLIオプション > 環境変数 > project settings > user settings > default
```

設定ファイルは **`vvread.settings.json`** に書き込みます（JSONC 行コメント `//` 対応）。

#### 探索順

| 種類 | パス | 用途 |
|---|---|---|
| **project** | `<cwd>/vvread.settings.json` | プロジェクト固有の発話パラメータ等 |
| **user** | macOS: `~/Library/Application Support/vvread/settings.json`<br/>Linux/WSL: `${XDG_CONFIG_HOME:-~/.config}/vvread/settings.json` | 全プロジェクト共通の既定 |

#### 設定例

リポ同梱の [`vvread.settings.example.json`](vvread.settings.example.json) をコピーして編集してください。最小例:

```jsonc
{
  "voicevox": {
    "engineUrl": "http://127.0.0.1:50021",
    "speaker": 3,
    "speed": 1.5,
    "maxChars": 500,   // 0 = 上限なし (内部 cap: 9999)
    "chunkChars": 200,
    "chunkHardMax": 400,
    "inlineCodeLimit": 25
  },
  "log": {
    "level": "INFO"
  }
}
```

`vvread doctor` で有効な全キー・現在値・設定元（env / project / user / default）を確認できます。

### 6-2. 環境変数一覧

`vvread.settings.json` のキーは環境変数でも指定できます。優先順位は **環境変数 > project settings > user settings**。

#### 接続

| 変数 | 既定 | 説明 |
|---|---|---|
| `VOICEVOX_ENGINE_URL` | `http://127.0.0.1:50021` | VOICEVOX Engine の base URL |

#### 発話パラメータ

| 変数 | 既定 | 説明 |
|---|---|---|
| `VOICEVOX_SPEAKER` | `3` | 話者 ID（`vvread doctor` で確認可能） |
| `VOICEVOX_SPEED` | `1.5` | 速度倍率 |
| `VOICEVOX_PITCH` | `0` | ピッチ |
| `VOICEVOX_INTONATION` | `1.0` | イントネーション |
| `VOICEVOX_VOLUME` | `1.0` | 音量 |
| `VOICEVOX_PAUSE_SCALE` | `1.0` | ポーズ長倍率 |
| `VOICEVOX_PRE_PHONEME` | `0` | 発話前無音(秒) |
| `VOICEVOX_POST_PHONEME` | `0` | 発話後無音(秒) |
| `VOICEVOX_MAX_CHARS` | `500` | 入力 text の最大文字数（超過分は打ち切り）。`0` で上限なし（内部 cap: 9999） |
| `VOICEVOX_CHUNK_CHARS` | `200` | 2 チャンク目以降の目安文字数 |
| `VOICEVOX_CHUNK_HARD_MAX` | `400` | チャンクの強制分割上限 |
| `VOICEVOX_INLINE_CODE_LIMIT` | `25` | インラインコードの最大長。超えると「コマンド」等に短縮 |

#### ログ / 通知

| 変数 | 既定 | 説明 |
|---|---|---|
| `VOICEVOX_LOG_LEVEL` | `INFO` | `OFF` / `INFO` / `DEBUG` |
| `VOICEVOX_LOG_MAX_BYTES` | `10485760` (10 MiB) | 超えたら 1 世代 rotate |
| `VOICEVOX_NOTIFY_COOLDOWN` | `60` | 失敗通知の最小間隔(秒) |

#### パス上書き

| 変数 | 用途 |
|---|---|
| `VVREAD_STATE_DIR` | session.id / playing.pid / disabled / mute_until / 一時 wav |
| `VVREAD_LOG_DIR` | `speak.log` |
| `VVREAD_CACHE_DIR` | wav キャッシュ |

#### OS 別の既定パス

| OS | state | log | cache |
|---|---|---|---|
| macOS | `~/Library/Application Support/vvread/` | `~/Library/Logs/vvread/` | `~/Library/Caches/vvread/` |
| Linux / WSL | `${XDG_STATE_HOME:-~/.local/state}/vvread/` | `${XDG_STATE_HOME:-~/.local/state}/vvread/logs/` | `${XDG_CACHE_HOME:-~/.cache}/vvread/` |

---

## 7. Claude Code との連携

### 7-1. 自動セットアップ

```bash
vvread install --scope user
```

これで `~/.claude/settings.json` の `hooks.Stop[].hooks[]` に以下が追加されます:

```json
{
  "type": "command",
  "command": "/Users/foo/.local/bin/vvread on-stop",
  "async": true,
  "timeout": 600
}
```

> ⚠️ `async: true` は **Claude Code 2.1.110+ 必須**。古い版では同期実行になり次プロンプトが待たされます。`vvread doctor` は古い版を検出した場合に warning を出します。

**`timeout: 600`（秒）を既定**とする理由: 長文応答（5000 字レベル）では合成・再生に 5 分超かかる場合があります。`async: true` で本体は待たないため、長めに取って音切れを避ける方が UX 上有利です。

---

## 8. トラブルシューティング

### 音が出ない

```bash
vvread doctor    # まずここから
vvread status    # 現在の状態確認
```

よくある原因:
- VOICEVOX Engine が起動していない → 起動（Docker / desktop app）してから `vvread doctor` を再実行
- `~/.local/bin` が PATH に無い → `~/.bashrc` / `~/.zshrc` に追記
- macOS で通知権限が付与されていない → `terminal-notifier` のインストールを推奨

### 音が二重に鳴る / 古い音声が混入する

```bash
vvread stop    # 即停止
vvread clean   # 残留ファイル削除
```

### Claude Code が次のプロンプトを待ってしまう

`vvread doctor` で `hook async=true` を確認してください。`async` が無効だと hook が同期実行になります。

---

## 9. ライセンス / クレジット

- 本 CLI コード: **MIT License**（[`LICENSE`](LICENSE) 参照）
- VOICEVOX Engine / Core / 各音声ライブラリは含まれません。それぞれの規約に従ってください。
- 合成音声の利用条件は **選択した音声ライブラリの規約** に従います（例: ずんだもん は東北イタコ・ずんだもんプロジェクト規約、四国めたん は SSS LLC 規約）。
- 本プロジェクトの一部はClaude Codeの支援を受けて開発されています。コードの確認・保守はプロジェクト作者が行っています。

---

## 10. CHANGELOG

[`CHANGELOG.md`](CHANGELOG.md) 参照。Keep a Changelog 形式 + semver。
