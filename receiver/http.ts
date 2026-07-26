const MAX_BODY_BYTES = 16 * 1024

// Host ヘッダ許可リスト: 127.0.0.1 / localhost / [::1]（+ 任意ポート）のみ。
// DNS rebinding 攻撃（悪意ドメインが解決先を 127.0.0.1 に切り替える手口）を Host 検証で遮断する。
const TRUSTED_HOST_RE = /^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/i

export type Notify = (content: string, path: string) => Promise<void>

function isTrustedHost(hostHeader: string | null): boolean {
  return hostHeader !== null && TRUSTED_HOST_RE.test(hostHeader)
}

export function createHttpServer(port: number, notify: Notify) {
  return Bun.serve({
    port,
    hostname: '127.0.0.1',
    async fetch(req: Request) {
      // CSRF 対策: ブラウザが発行する fetch/XHR は Origin ヘッダを必ず送る（no-cors でも同様）。
      // curl 等の正当な CLI クライアントは Origin を送らないため、存在するだけで拒否してよい。
      if (req.headers.get('origin') !== null) {
        return new Response('forbidden\n', { status: 403 })
      }
      // DNS rebinding 対策: Host ヘッダがループバック表記でなければ拒否する。
      if (!isTrustedHost(req.headers.get('host'))) {
        return new Response('forbidden\n', { status: 403 })
      }

      if (req.method !== 'POST') {
        return new Response('POST only', { status: 405 })
      }
      const raw = await req.arrayBuffer()
      if (raw.byteLength === 0) {
        return new Response('empty body\n', { status: 400 })
      }
      if (raw.byteLength > MAX_BODY_BYTES) {
        return new Response('payload too large\n', { status: 413 })
      }
      const body = new TextDecoder().decode(raw)
      if (!body.trim()) {
        return new Response('empty body\n', { status: 400 })
      }
      try {
        await notify(body, new URL(req.url).pathname)
        // 202: notification を stdio transport へ書き込んだ。
        // Claude Code 側の処理完了や音声再生までは保証しない。
        return new Response('accepted\n', { status: 202 })
      } catch {
        return new Response('channel not ready\n', { status: 503 })
      }
    },
  })
}
