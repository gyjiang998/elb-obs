const backendOrigin = process.env.HETERO_NEXT_API_PROXY_TARGET ?? 'http://127.0.0.1:8006'
const upstreamUrl = `${backendOrigin.replace(/\/$/, '')}/v1/model-relay/chat-completions/stream`

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

function relayFailure(message: string): Response {
  return Response.json({
    error: {
      code: 'MODEL_RELAY_LOCAL_PROXY_UNAVAILABLE',
      message,
      details: { upstream: backendOrigin },
    },
  }, { status: 502 })
}

export async function POST(request: Request): Promise<Response> {
  let upstream: Response
  try {
    upstream = await fetch(upstreamUrl, {
      method: 'POST',
      headers: {
        'Accept': 'text/event-stream',
        'Content-Type': request.headers.get('content-type') || 'application/json',
      },
      body: await request.arrayBuffer(),
      cache: 'no-store',
      signal: request.signal,
    })
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    return relayFailure(`无法连接本机模型中继：${detail}`)
  }

  const headers = new Headers()
  headers.set('Content-Type', upstream.headers.get('content-type') || 'text/event-stream; charset=utf-8')
  headers.set('Cache-Control', 'no-cache, no-transform')
  headers.set('X-Accel-Buffering', 'no')

  if (!upstream.body) {
    return relayFailure('本机模型中继没有返回可读取的流。')
  }
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers,
  })
}
