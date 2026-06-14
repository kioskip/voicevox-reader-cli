const MAX_BODY_BYTES = 16 * 1024

export type Notify = (content: string, path: string) => Promise<void>

export function createHttpServer(port: number, notify: Notify) {
  return Bun.serve({
    port,
    hostname: '127.0.0.1',
    async fetch(req: Request) {
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
