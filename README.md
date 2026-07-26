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
- 🌐 **URL読み上げ**: `vvread url https://...` でWebページ本文を読み上げ
- 💻 **パイプ連携**: `echo "テキスト" | vvread` パイプを繋いで受け取ったテキストの読み上げ
- 📢 **Claude Code 連携**: Stop hook で Claude Code の最終応答を自動読み上げ
- ⚡ **prefetch**: 長文を分割し、再生中に次の文節を先行して合成
- 🔁 **セッション割り込み**: 新しい応答が来たら旧応答の再生は次境界で停止
- 💾 **wav キャッシュ**: 短い定型文はキャッシュ利用で合成スキップ
- 🧹 **誤読対策**: 数字+助数詞・漢数字日付・ASCII 単位・パス省略・ハッシュ省略・同形異音漢字の整形パイプライン
- 🔤 **英単語のカナ化**: e2k（任意）+ 内蔵辞書で `Docker` → `ドッカー` のような変換
- 🖥️ **メニューバー UI**（macOS、任意）: `vvread menubar` で状態表示・読み上げ/キューモードのトグル・一時ミュート・停止/クリア・デフォルト設定（話者ほか5パラメータ）をメニューバーから操作

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

`vvread setup` を実行すると、まず engine / e2k / hook の現在状態が表示されます。
その後、対話形式で以下を確認・設定します:
- VOICEVOX Engine の URL（デフォルト `http://127.0.0.1:50021`）
- e2k（英語カナ変換ライブラリ）をインストールするか
- Claude Code の Stop hook を登録するか（scope 選択あり）

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
vvread "テキスト"                       # 読み上げ
vvread "はやく読んで" --speed 1.8       # 速度指定（0.5–2.0）
vvread file README.md                   # ファイルを読み上げ
vvread url https://example.com          # URLのWebページ本文を読み上げ
cat build.log | vvread                  # パイプ入力
vvread stop                             # 再生停止
vvread doctor                           # ヘルスチェック
```

---

## 6. 設定

詳細な設定リファレンスは [`CONFIGURATION.md`](CONFIGURATION.md) を参照してください。

### 基本設定例

```jsonc
{
  "voicevox": {
    "engines": ["http://127.0.0.1:50021"],
    "speaker": 3,
    "speed": 1.5
  }
}
```

設定ファイル（`vvread.settings.json`）をプロジェクトルートに置くか、`vvread config` コマンドで編集してください。

### 複数エンジンの並列利用（v0.3.0）

VOICEVOX Engine を複数台起動して並列合成できます。詳細は [`CONFIGURATION.md` — マルチエンジン設定](CONFIGURATION.md#マルチエンジン設定v030) を参照してください。

---

## 7. メニューバー UI（macOS、任意）

> 対応 OS: **macOS のみ**。他 OS で実行すると案内メッセージ付きで終了します（CLI 本体には影響しません）。

`vvread menubar` は、これまで紹介したコマンドをメニューバーの GUI から操作できる常駐アプリです。VOICEVOX Engine と直接通信するのではなく上記の `vvread` コマンド自体を裏で呼び出すだけなので、鳴らない・止まらないときのトラブルシューティングは CLI と同じ（`vvread doctor` / `vvread status` を確認）です。

### 起動

```bash
vvread menubar
```

メニューバーにアイコンが現れ、状態に応じて切り替わります: 🔊 待機中 / ▶ 再生中 / 🔇 オフ / 🤫 ミュート中 / ⚠ 状態取得エラー。すでに起動中の場合は「vvread menubar は既に起動中です」と表示されて 2 個目は拒否されます。

初回起動時にメニューバー用ライブラリ（rumps）が未導入だと、導入コマンド（`uv sync`）を案内して終了します。

### メニュー構成

メニューを開くと、上から状態表示 → トグル操作 → 一括操作 → 終了の順に並びます:

```
状態行 / キュー行 / エラー・警告行（action の失敗時・警告時のみ表示）
──────────────
読み上げ（トグル） / キューモード（トグル） / 一時ミュート ▸（5分 / 30分 / 1時間 / 解除）
──────────────
現在再生中を停止 / キューをクリア / デフォルト設定 ▸
──────────────
vvread menubarを終了
```

状態行は 🟢（稼働中。待機中・再生中のどちらでも同じ表示）/ 🟡（ミュート中。「HH:MM までミュート中」と解除予定の絶対時刻で表示）/ 🔴（停止中）/ ⚠（状態取得エラー。初回取得前は「状態不明」）の4パターンに統合して表示されます。

| 項目 | できること |
|---|---|
| 状態行 / キュー行 | 現在の状態（🟢/🟡/🔴/⚠）と、待機 / 再生中 / 失敗の件数を表示（操作なし） |
| 読み上げ | チェックマークで ON/OFF を表示するトグル。クリックで `vvread on` / `vvread off` と同じ切り替え |
| キューモード | チェックマークで ON/OFF を表示するトグル。クリックで `vvread queue on` / `vvread queue off` と同じ切り替え |
| 一時ミュート ▸ 5分 / 30分 / 1時間 / 解除 | `vvread mute` / `vvread unmute` と同じ。ミュート中は状態行に解除時刻も表示される |
| 現在再生中を停止 | 再生中の音を止め、待機中の発話も含めて全消去（`vvread stop` と同じ） |
| キューをクリア | 再生中の発話はそのまま、待機中の発話だけ消す（`vvread queue clear` と同じ） |
| デフォルト設定 ▸ 話者 / 音量 / スピード / 抑揚 / 句読点ポーズ / 最大チャンク数 | 各パラメータを選択式サブメニューから変更。選んだ値は `vvread config --set` でユーザースコープに書き込まれ、チェックマークで現在値を確認できる。話者サブメニューには「再読み込み」もある |
| vvread menubarを終了 | メニューバーアプリを終了する（読み上げ自体の設定・queue の内容は変わらない） |

状態表示は数秒おきに自動更新されます。話者一覧・6 設定（話者/音量/スピード/抑揚/句読点ポーズ/最大チャンク数）は起動時と「デフォルト設定 ▸ 話者 ▸ 再読み込み」を押したとき、および取得に失敗している間は自動で再試行（30秒おき）します。

### デフォルト設定を変えたのに反映されないとき

「デフォルト設定」から話者・音量・スピード・抑揚・句読点ポーズ・最大チャンク数のいずれかを選ぶと、全プロジェクト共通（ユーザースコープ）の設定に書き込まれます。プロジェクトごとの `vvread.settings.json` が同じ項目を上書きしている場合はメニューバーの選択が反映されないことがあり、その場合はどのパラメータでも共通してメニュー内のエラー・警告行に「他スコープの設定が優先されています」という警告が表示されます。該当プロジェクトの設定ファイルを確認してください。

### 終了のしかた

メニューの「vvread menubarを終了」を選ぶか、ターミナルから起動した場合は `Ctrl-C` でも終了できます。いずれも読み上げ機能自体（CLI・Stop hook）には影響しません。

### ログイン時の自動起動（LaunchAgent、任意）

`vvread setup` の対話プロンプトで「Enable menubar auto-start on login?」に Yes と答えるか、`vvread setup --with-menubar` を実行すると、ログイン時に `vvread menubar` が自動起動するようになります（`~/Library/LaunchAgents/com.vvread.menubar.plist` に登録）。解除する場合は `vvread uninstall --with-menubar` を実行してください。

---

## 8. Claude Code との連携

### 8-1. 別プロジェクトへの追加登録

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

### 8-2. MCP サーバーとして登録する（任意）

> **位置づけ**: MCP 連携は Stop hook / CLI の代替ではなく**追加機能**です。
> `mcp` パッケージを入れなくても既存の読み上げ機能はそのまま動きます。

MCP サーバーとして登録すると、Claude が長時間作業の途中で進捗・エラー・完了を
**自ら読み上げる**ことができます。ビルドやコードレビュー中に画面監視が不要になります。

追加される機能:
- `vvread_say`: 任意のタイミングでテキストを読み上げ（即時復帰）
- `vvread_stop`: 再生中の音声を停止
- `vvread_status`: 再生状態を確認
- `vvread_speakers`: 利用可能な話者一覧を取得
- `vvread_config_set`: 読み上げ設定を変更（許可キーのみ）

**インストールと登録**:

```bash
# 1. mcp パッケージをインストール（Python >=3.10 が必要）
uv sync --extra mcp

# 2. Claude Code に登録
claude mcp add --transport stdio --scope local vvread \
  -- /absolute/path/to/voiceClaude/bin/vvread mcp
claude mcp list   # vvread が表示されれば OK
```

詳細な使い方は **[MCP.md](MCP.md)** を参照してください。
外部イベント（CI 完了・監視アラート等）を声で受け取る**チャネル連携**（実験的）も [MCP.md](MCP.md) の「チャネル連携」を参照してください。

---

## 9. トラブルシューティング

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

## 10. ライセンス / クレジット

- 本 CLI コード: **MIT License**（[`LICENSE`](LICENSE) 参照）
- VOICEVOX Engine / Core / 各音声ライブラリは含まれません。それぞれの規約に従ってください。
- 合成音声の利用条件は **選択した音声ライブラリの規約** に従います（例: ずんだもん は東北イタコ・ずんだもんプロジェクト規約、四国めたん は SSS LLC 規約）。
- 本プロジェクトの一部はClaude Codeの支援を受けて開発されています。コードの確認・保守はプロジェクト作者が行っています。

---

## 11. CHANGELOG

[`CHANGELOG.md`](CHANGELOG.md) 参照。Keep a Changelog 形式 + semver。



#### 注意事項

- **subcommand 名はテキストとして扱われません。** `vvread doctor` は doctor コマンドを実行します。"doctor" という文字列を読み上げたい場合は `vvread say "doctor"` と明示してください。
- **オプションはテキストの後ろに指定してください。** `vvread "こんにちは" --speaker 8` は動作しますが、`vvread --speaker 8 "こんにちは"` は非対応です。
- **対応するのは明示的なパイプ入力のみです。** `cat file | vvread` は動作しますが、リダイレクト（`vvread < file`）は非対応です。ファイルを渡す場合は `vvread file <path>` を使用してください。