# 設定リファレンス

> 英語版ドキュメントは準備中です。

---

## 設定ファイル / 優先順位

```
CLIオプション > 環境変数 > project settings > user settings > default
```

設定ファイルは **`vvread.settings.json`** に書き込みます（JSONC 行コメント `//` 対応）。

### 探索順

| 種類 | パス | 用途 |
|---|---|---|
| **project** | `<cwd>/vvread.settings.json` | プロジェクト固有の発話パラメータ等 |
| **user** | macOS: `~/Library/Application Support/vvread/settings.json`<br/>Linux/WSL: `${XDG_CONFIG_HOME:-~/.config}/vvread/settings.json` | 全プロジェクト共通の既定 |

### 設定例

リポ同梱の `vvread.settings.example.json` をコピーして編集してください。最小例:

```jsonc
{
  "voicevox": {
    "engines": ["http://127.0.0.1:50021"],  // 接続先 URL（配列形式が標準）
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

`vvread config --list` で有効な全キー・現在値を確認できます。`vvread doctor` で設定元（env / project / user / default）も含めて確認できます。

---

## 環境変数一覧

`vvread.settings.json` のキーは環境変数でも指定できます。優先順位は **環境変数 > project settings > user settings**。

### 接続

| 変数 | 既定 | 説明 |
|---|---|---|
| `VOICEVOX_ENGINE_URL` | (engines[0] から派生) | VOICEVOX Engine の base URL（legacy alias、runtime export 互換用） |
| `VOICEVOX_ENGINES` | `http://127.0.0.1:50021` | 並列合成に使うエンジン URL を `;` 区切りで指定。設定方法は「マルチエンジン設定」を参照 |

### 発話パラメータ

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
| `VOICEVOX_MAX_CHUNKS` | `0` | 生成するチャンク数の上限。`0` で上限なし（デフォルト）。超過分は「以下省略」を付加して打ち切る |
| `VOICEVOX_CHUNK_CHARS` | `200` | 2 チャンク目以降の目安文字数 |
| `VOICEVOX_CHUNK_HARD_MAX` | `400` | チャンクの強制分割上限 |
| `VOICEVOX_INLINE_CODE_LIMIT` | `25` | インラインコードの最大長。超えると「コマンド」等に短縮 |

### ログ / 通知

| 変数 | 既定 | 説明 |
|---|---|---|
| `VOICEVOX_LOG_LEVEL` | `INFO` | `OFF` / `INFO` / `DEBUG` |
| `VOICEVOX_LOG_MAX_BYTES` | `10485760` (10 MiB) | 超えたら 1 世代 rotate |
| `VOICEVOX_NOTIFY_COOLDOWN` | `60` | 失敗通知の最小間隔(秒) |

### キャッシュ

| 変数 | 既定 | 説明 |
|---|---|---|
| `VVREAD_CACHE_FIRST_CHUNK_RAW` | `true` | `true` の場合、1st chunk をキャッシュキー計算なしで raw テキストのまま wav キャッシュに登録する |
| `VVREAD_CACHE_FIRST_CHUNK_RAW_MAX_CHARS` | `100` | raw キャッシュを適用する 1st chunk のテキスト文字数上限。超えると通常キャッシュに fallback |
| `VVREAD_CACHE_TTL_DAYS` | `0`（無効） | 整数。wav キャッシュの TTL（日数）。0 = 自動削除しない。対応設定キー: `cache.ttlDays` |
| `VVREAD_CACHE_CLEANUP_INTERVAL_HOURS` | `24` | 整数。TTL クリーンアップの実行間隔（時間）。0 = `vvread say` 毎回。対応設定キー: `cache.cleanupIntervalHours` |

### キュー再生モード

| 変数 | 既定 | 説明 |
|---|---|---|
| `VVREAD_SAY_QUEUE` | (未設定) | `1`=この発話を queue / `0`=preempt。優先順位: per-call `--queue`/`--no-queue` > 本変数 > `vvread queue on` フラグ > 既定 preempt |
| `VVREAD_QUEUE_MAX` | `64` | pending 上限。手動発話は full なら reject、自動通知は最古を drop。1 以上の整数のみ |
| `VVREAD_QUEUE_FAILED_MAX` | `32` | failed 退避エントリの上限。超過時は退避の瞬間に最古を 1 件自動削除。1 以上の整数のみ |

### パス上書き

| 変数 | 用途 |
|---|---|
| `VVREAD_STATE_DIR` | session.id / playing.pid / disabled / mute_until / 一時 wav |
| `VVREAD_LOG_DIR` | `speak.log` |
| `VVREAD_CACHE_DIR` | wav キャッシュ |

### OS 別の既定パス

| OS | state | log | cache |
|---|---|---|---|
| macOS | `~/Library/Application Support/vvread/` | `~/Library/Logs/vvread/` | `~/Library/Caches/vvread/` |
| Linux / WSL | `${XDG_STATE_HOME:-~/.local/state}/vvread/` | `${XDG_STATE_HOME:-~/.local/state}/vvread/logs/` | `${XDG_CACHE_HOME:-~/.cache}/vvread/` |

---

## マルチエンジン設定（v0.3.0）

複数の VOICEVOX Engine を並列利用することで、音声合成と再生をオーバーラップさせて応答時間を改善できます。

### 設定方法

```bash
vvread config --json '{"voicevox":{"engines":["http://127.0.0.1:50021","http://127.0.0.1:50022"]}}'
```

`vvread.settings.json` に直接記述する場合：

```json
{
  "voicevox": {
    "engines": [
      "http://127.0.0.1:50021",
      "http://127.0.0.1:50022"
    ]
  }
}
```

### 動作

- エンジン M 台に chunk を round-robin で分散。各エンジンは最大 1 合成を同時担当
- M=1 でも再生中に次 chunk の合成が進む（prefetch 効果）
- 合成失敗時は同一エンジンへ 1 回 retry。エンジン停止時の自動切り替えは対応予定

### 前提

複数エンジンは同一バージョン・同一音声ライブラリの互換構成である必要があります。wav キャッシュはエンジン間で共有されます（異種エンジンは非対応）。

### 確認

```bash
vvread doctor
# VOICEVOX Engines: [OK] http://127.0.0.1:50021
#                   [OK] http://127.0.0.1:50022
```
