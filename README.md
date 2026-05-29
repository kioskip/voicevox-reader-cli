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

- 🗣️ **コマンドで読み上げ**: `vvread "読み上げます"` で読み上げ
- 🗒️ **ファイルの読み込み**: `vvread file FILENAME` でファイルを読み上げ
- 💻 **パイプ連携**: `echo "テキスト" | vvread` パイプを繋いで受け取ったテキストの読み上げ
- 📢 **Claude Code 連携**: Stop hook で Claude Code の最終応答を自動読み上げ
- ⚡ **prefetch**: 長文を分割し、再生中に次の文節を先行して合成
- 🔁 **セッション割り込み**: 新しい応答が来たら旧応答の再生は次境界で停止
- 💾 **wav キャッシュ**: 短い定型文はキャッシュ利用で合成スキップ
- 🧹 **誤読対策**: 数字+助数詞・漢数字日付・ASCII 単位・パス省略・ハッシュ省略・同形異音漢字の整形パイプライン
- 🔤 **英単語のカナ化**: e2k（任意）+ 内蔵辞書で `Docker` → `ドッカー` のような変換

---

## 2. サポート環境

| OS / Shell | 対応度 | 備考 |
|---|---|---|
| **macOS**(Intel / Apple Silicon) | ✅ 一級対応 | 合成した wav を `afplay` で再生 |
| **Linux**(Ubuntu / Debian / Arch 等) | ✅ 一級対応 | `paplay` > `pw-play` > `aplay` > `play`(sox) > `ffplay` の優先順で自動選択 |
| **WSL2** | ✅ 一級対応 | Linux と同一扱い。WSLg 経由で音声出力 |
| **Windows + Git Bash** | ⚠️ best-effort | 再生不可（player バイナリ無し）。CLI 操作（`vvread synth` 等）のみ可 |
| **Windows native (PowerShell / cmd.exe)** | ❌ 対象外 | WSL2 または Git Bash を推奨 |

### 依存

| 必須/任意 | 依存 | 用途 | 備考 |
|---|---|---|---|
| 必須 | `bash` 3.2+ | スクリプト実行 | macOS の `/bin/bash` 3.2 で動くよう実装 |
| 必須 | `python3` 3.10+ | sanitize / cache_key / parse_transcript | CI matrix = 3.10 + 3.12 |
| 必須 | `curl` | VOICEVOX API 呼び出し | |
| 必須 | **VOICEVOX Engine** | 音声合成本体 | 別途インストール（後述「VOICEVOX Engine の準備」参照） |
| 任意 | `jq` | settings.json マージ / 発話パラメータ tuning | 無ければ Python `json` に fallback |
| 任意 | `docker` + `docker compose` | VOICEVOX Engine のコンテナ起動 | `vvread setup --engine docker` 利用時に便利 |
| 任意 | `e2k` Python package | 英単語のカナ化精度向上 | 無ければ辞書 + 逐字 fallback |
| 任意 | `terminal-notifier` | 失敗時のデスクトップ通知 | macOS のみ |

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

### 1. VOICEVOX Engine を起動する

```bash
# Docker を使う場合（推奨）:
docker run --rm -p 50021:50021 voicevox/voicevox_engine:cpu-latest

# または VOICEVOX GUI アプリを起動する。
```

### 2. vvread をセットアップする

```bash
# 有効にしたいプロジェクト内で実行
vvread setup
```

`vvread setup` の対話では以下を聞かれます:
- VOICEVOX Engine の URL（デフォルト `http://127.0.0.1:50021`）
- 音声合成の話者 ID
- Claude Code の Stop hook を登録するか

### 3. 動作確認

```bash
vvread "テスト"       # 読み上げ確認
vvread doctor         # ヘルスチェック
```

これで Claude Code を起動すると、応答が自動で読み上げられます。

---

## 5. CLI リファレンス

詳細なコマンドリファレンスは [`COMMANDS.md`](COMMANDS.md) を参照してください。

### よく使うコマンド

```bash
vvread "テキスト"              # 読み上げ
vvread file README.md          # ファイルを読み上げ
cat build.log | vvread         # パイプ入力
vvread stop                    # 再生停止
vvread doctor                  # ヘルスチェック
```

---

## 6. 設定

詳細な設定リファレンスは [`CONFIGURATION.md`](CONFIGURATION.md) を参照してください。

### 基本設定例

```jsonc
{
  "voicevox": {
    "engineUrl": "http://127.0.0.1:50021",
    "speaker": 3,
    "speed": 1.5
  }
}
```

設定ファイル（`vvread.settings.json`）をプロジェクトルートに置くか、`vvread config` コマンドで編集してください。

---

## 7. Claude Code との連携

### 7-1. 別プロジェクトへの追加登録

> **注意**: `vvread setup` を実施した環境では、セットアップ中に Claude Code hook の登録まで完了しています。
> 同じプロジェクトで再度 `vvread install` を実行する必要はありません。
>
> 別のプロジェクトや全プロジェクト共通（user scope）に hook を追加したい場合は `vvread install` を使います。

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



#### 注意事項

- **subcommand 名はテキストとして扱われません。** `vvread doctor` は doctor コマンドを実行します。"doctor" という文字列を読み上げたい場合は `vvread say "doctor"` と明示してください。
- **オプションはテキストの後ろに指定してください。** `vvread "こんにちは" --speaker 8` は動作しますが、`vvread --speaker 8 "こんにちは"` は非対応です。
- **対応するのは明示的なパイプ入力のみです。** `cat file | vvread` は動作しますが、リダイレクト（`vvread < file`）は非対応です。ファイルを渡す場合は `vvread file <path>` を使用してください。