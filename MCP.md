# vvread MCP サーバー連携ガイド

**標準の読み上げ（Stop hook）は、Claude の応答が終わってから読み上げます。**
MCP 連携を入れると、Claude が**作業の途中で自分から**声をかけられるようになります。

| | 読み上げのタイミング |
|---|---|
| Stop hook（標準） | 応答が完了したあと |
| MCP 連携 | 作業中の任意のタイミング |

たとえば、こんなことができます:

- 5 分かかるビルドの**完了を声で知らせる** — 画面を見ていなくても気づける
- テスト失敗など**ブロッキングなエラーだけ**を要点で読み上げる
- 「終わったら教えて」と一言頼むだけで、長い処理の節目を声で通知
- その場で**話者や読み上げ速度を変更**（「ずんだもんにして」）

MCP は任意機能です。導入しても従来の CLI 操作と Stop hook はそのまま動きます。

---

## 前提条件

- VOICEVOX Engine が起動していること（`vvread doctor` で確認できます）
- Python 3.10 以上

---

## インストール

```bash
cd /path/to/voiceClaude
uv sync --extra mcp
```

---

## Claude Code への登録

```bash
claude mcp add --transport stdio --scope local vvread \
  -- /absolute/path/to/voiceClaude/bin/vvread mcp
```

登録確認:

```bash
claude mcp list   # vvread が表示されれば OK
```

### `.mcp.json` を使う方法

プロジェクトのルートに `.mcp.json` を置いても登録できます。

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

> **注意: 動作未検証**
> Claude Desktop は `CLAUDE_PROJECT_DIR` 環境変数が保証されないため、
> project settings の書き込み先が不定になる可能性があります。

参考（自己責任でお試しください）:

`~/Library/Application Support/Claude/claude_desktop_config.json` に以下を追加。

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

## Stop hook から移行する場合

すでに `vvread install` で Stop hook を使っている場合、MCP は**置き換えではなく追加**です。両者は独立して動きます。

| | 読み上げる内容 | タイミング |
|---|---|---|
| Stop hook | 応答の**全文** | 応答が完了した**あと**（自動） |
| MCP | Claude が選んだ**要点だけ** | 作業の**途中**（Claude の判断） |

話者・速度などの設定はどちらも同じ `vvread.settings.json` を見るため、**設定の移行は不要**です。

- **完全に MCP へ切り替えたい**（全文の自動読み上げをやめ、要点だけにする）場合は、`vvread uninstall` で Stop hook を解除してください。
- **併用したい**（完了時は全文、作業中は要点）場合は、そのままで両方動きます。

現在の登録状態は `vvread doctor` の hooks セクションで確認できます。

---

## ツール一覧

各ツールには MCP の ToolAnnotations が設定されています（Claude がツールを選ぶ手掛かりになります）:

- 🟢 **read-only** — 状態を変えない（`vvread_status` / `vvread_speakers`）
- 🟡 **状態変更** — 再生の開始・停止（`vvread_say` / `vvread_stop`）
- 🔴 **destructive** — 設定を永続的に変更（`vvread_config_set`）

### `vvread_say(text, speaker?)` 🟡

テキストを VOICEVOX で読み上げます。バックグラウンドで起動し、即座に返ります。

```
vvread_say("ビルドが完了しました")
vvread_say("エラーが発生しました。ログを確認してください", speaker=3)
```

### `vvread_stop()` 🟡

現在再生中の音声を停止します。

### `vvread_status()` 🟢

再生状態を確認します。`"state: idle"` または `"state: playing (pid=1234)"` を返します。

### `vvread_speakers()` 🟢

設定済みの VOICEVOX Engine から利用可能な話者一覧を取得します。

```json
[
  {
    "name": "ずんだもん",
    "styles": [
      {"id": 3, "name": "ノーマル"},
      {"id": 1, "name": "あまあま"}
    ]
  }
]
```

### `vvread_config_set(key, value)` 🔴

`vvread.settings.json`（プロジェクトスコープ）の設定を変更します。

**ユーザーが明示的に変更を依頼した場合のみ使用してください。自律的な変更は禁止です。**

許可キー:

| キー | 型 | 範囲 | 説明 |
|---|---|---|---|
| `voicevox.speaker` | int | 0〜9999 | 話者 ID |
| `voicevox.speed` | float | 0.5〜2.0 | 読み上げ速度 |
| `voicevox.pitch` | float | -0.15〜0.15 | 音高 |
| `voicevox.intonation` | float | 0.0〜2.0 | 抑揚 |
| `voicevox.volume` | float | 0.0〜2.0 | 音量 |

```
vvread_config_set("voicevox.speaker", "1")
vvread_config_set("voicevox.speed", "1.3")
```

---

## 自然言語での利用例

Claude に以下のように話しかけることで MCP ツールを呼び出せます:

- 「長い処理の途中で、重要な進捗だけ声で知らせて」
- 「エラーが起きたら要点だけ読み上げて」
- 「作業が完了したら声で知らせて」
- 「利用できる話者を確認して、ずんだもんに変更して」
- 「読み上げ速度を 1.3 に変更して」

---

## receiver 連携（実験的・research preview）

> **⚠️ 実験的機能です**
> Claude Code の **Channels（research preview）** を利用します。起動に `--dangerously-load-development-channels` フラグが必須です。**E2E 動作は実機で検証済み**（2026/06/06）ですが、仕様は予告なく変わる可能性があります。

ここまでは Claude **自身の作業**を声にする機能でした。**receiver 連携は、Claude の外で起きたこと**を声に変えます。

CI がコケた・デプロイが終わった・監視がアラートを上げた——こうした**外部イベント**を Claude Code セッションに送り込むと、Claude が内容を 1〜2 文に要約して読み上げます。別のターミナルや管理画面に張り付いて結果を待つ必要がなくなります。

たとえば:

- GitHub Actions の成否を `curl` 一発で声に
- サーバー監視のアラートを作業中の耳へ
- 数十分かかるバッチ処理の終了通知

### 仕組み

```
外部イベント
  → HTTP POST :8788      (receiver/server.ts)
  → notifications/claude/channel
  → <channel> タグとして Claude の会話に届く
  → Claude が 1〜2 文に要約
  → vvread_say → VOICEVOX で再生
```

### 前提条件

- Claude Code 2.1.80 以上
- claude.ai または Console API key での認証（**Amazon Bedrock / Google Vertex では不可**）
- Team / Enterprise プランでは管理者が `channelsEnabled` を有効化していること
- Bun 1.2 以上

### セットアップ

`vvread setup --with-receiver` で依存確認 + 現在のプロジェクトへの登録をまとめて実行できます（手動でも可）:

```bash
# まとめてセットアップ（bun 依存確認 + local 登録。既登録は上書きしない）
vvread setup --with-receiver

# あるいは手動で:
# 1. 依存をインストール
cd /path/to/voiceClaude/receiver
bun install

# 2. 現在のプロジェクトへ local 登録（local scope のみ。.mcp.json は変更しない）
claude mcp add --transport stdio --scope local vvread-receiver \
  -- bun /path/to/voiceClaude/receiver/server.ts

# 3. 開発チャネルを読み込んで Claude Code を起動
claude --dangerously-load-development-channels server:vvread-receiver
```

> **キュー再生モードを推奨**: receiver の要約発話と Stop hook の応答全文が二重に鳴る／要約が途中で切られるのを防ぐため、`vvread queue on` を有効にしてください。割り込まず順番に再生し、全文を優先しつつ要約を活かします。

起動後、Claude Code 内で `/mcp` を実行し、`vvread` と `vvread-receiver` が connected であることを確認します。
ポートは `VVREAD_RECEIVER_PORT`（デフォルト `8788`）で変更できます。

### イベントを送る

```bash
curl -X POST http://localhost:8788 -d "ビルドが完了しました"
```

HTTP 応答コード:

| コード | 意味 |
|---|---|
| 202 | 受理（通知を書き込んだことのみ保証。**音声再生までは保証しない**） |
| 400 | 本文が空 |
| 403 | Origin ヘッダあり、または Host が許可リスト外（下記参照） |
| 405 | POST 以外のメソッド |
| 413 | 本文が大きすぎる（16 KiB 超） |
| 503 | チャネル未接続（Claude Code 未起動など） |

### セキュリティ・信頼モデル

- イベント本文は**信頼できないデータ**として扱われます。サーバーの固定 `instructions` により、Claude は本文内の命令に従わず、コマンド実行・ファイル変更・秘密の開示を行いません。CI 結果・監視アラート・完了通知のみを要約します。通知本文はランダムなフェンス（`<<<VVREAD-DATA-{uuid}>>> ... <<<END-VVREAD-DATA-{uuid}>>>`）で囲んだ上でモデルへ渡され、フェンス内は常に逐語データとして扱われます（prompt injection 対策）。
- 権限は **`mcp__vvread__vvread_say` のみ許可**することを推奨します。`mcp__vvread__*` の一括許可は `vvread_config_set`（設定の永続変更）まで含むため避けてください。
- HTTP リスナーは **localhost（127.0.0.1）のみ** にバインドします。加えて、`Origin` ヘッダを持つリクエスト（ブラウザ発 `fetch`/XHR は必ず送信）と、`Host` ヘッダが `127.0.0.1` / `localhost` / `[::1]`（+ 任意ポート）以外のリクエストは 403 で拒否します。悪意ある Web ページからの CSRF や DNS rebinding でこのエンドポイントを叩かれる経路を塞ぐためです。`curl` 等の CLI クライアントは通常 Origin を送らないため影響ありません。

### 現状の制約

認証・送信元 allowlist・イベント重複排除・リモート CI からローカル PC への到達経路・セッション未起動時のキューイングは**未実装**です（今後対応予定）。

---

## トラブルシューティング

### MCP ツールが Claude Code に表示されない

```bash
claude mcp list   # vvread が表示されるか確認
vvread doctor     # [mcp] セクションで package の有無を確認
```

### 音声が再生されない

```bash
vvread doctor     # VOICEVOX Engine の接続状況を確認
tail -f "$(scripts/paths.py log)/speak.log"   # ログを確認
```

### `uv sync --extra mcp` が失敗する

```bash
python3 --version   # Python 3.10 以上が必要
uv --version        # uv がインストール済みか確認
```

---

詳細なセットアップ手順は [`README.md` の「3. インストール」](README.md#3-インストール) を参照してください。
