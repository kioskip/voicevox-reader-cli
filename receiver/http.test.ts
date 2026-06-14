import { describe, it, expect } from 'bun:test'
import { createHttpServer, type Notify } from './http'

const noop: Notify = async () => {}

describe('createHttpServer', () => {
  it('GET → 405', async () => {
    const srv = createHttpServer(0, noop)
    const res = await fetch(`http://127.0.0.1:${srv.port}/`, { method: 'GET' })
    expect(res.status).toBe(405)
    srv.stop(true)
  })

  it('POST 空 body → 400', async () => {
    const srv = createHttpServer(0, noop)
    const res = await fetch(`http://127.0.0.1:${srv.port}/`, {
      method: 'POST',
      body: '',
    })
    expect(res.status).toBe(400)
    srv.stop(true)
  })

  it('POST 空白のみ body → 400', async () => {
    const srv = createHttpServer(0, noop)
    const res = await fetch(`http://127.0.0.1:${srv.port}/`, {
      method: 'POST',
      body: '   \n  ',
    })
    expect(res.status).toBe(400)
    srv.stop(true)
  })

  it('POST 16KiB 超過 → 413', async () => {
    const srv = createHttpServer(0, noop)
    const res = await fetch(`http://127.0.0.1:${srv.port}/`, {
      method: 'POST',
      body: 'x'.repeat(16 * 1024 + 1),
    })
    expect(res.status).toBe(413)
    srv.stop(true)
  })

  it('正常 POST → 202 + notify 呼び出し', async () => {
    let receivedContent = ''
    let receivedPath = ''
    const capture: Notify = async (content, path) => {
      receivedContent = content
      receivedPath = path
    }
    const srv = createHttpServer(0, capture)
    const res = await fetch(`http://127.0.0.1:${srv.port}/events`, {
      method: 'POST',
      body: 'CI が main ブランチで失敗しました',
    })
    expect(res.status).toBe(202)
    expect(await res.text()).toBe('accepted\n')
    expect(receivedContent).toBe('CI が main ブランチで失敗しました')
    expect(receivedPath).toBe('/events')
    srv.stop(true)
  })

  it('notify エラー → 503', async () => {
    const throwing: Notify = async () => {
      throw new Error('not connected')
    }
    const srv = createHttpServer(0, throwing)
    const res = await fetch(`http://127.0.0.1:${srv.port}/`, {
      method: 'POST',
      body: 'test',
    })
    expect(res.status).toBe(503)
    srv.stop(true)
  })

  it('port 0 → ランダム空きポートで起動（port 競合なし）', () => {
    const srv = createHttpServer(0, noop)
    expect(srv.port).toBeGreaterThan(0)
    srv.stop(true)
  })
})
