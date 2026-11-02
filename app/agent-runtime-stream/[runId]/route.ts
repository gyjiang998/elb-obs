import { resilientSseProxyBody } from '../../lib/resilient-sse-proxy'

const backendOrigin = process.env.HETERO_NEXT_API_PROXY_TARGET ?? 'http://127.0.0.1:8006'

export const dynamic = 'force-dynamic'
export const runtime = 'nodejs'

export async function GET(
  request: Request,
  context: { params: Promise<{ runId: string }> },
) {
  const { runId } = await context.params
  const incoming = new URL(request.url)
  const upstream = new URL(`${backendOrigin.replace(/\/$/, '')}/v1/agent-runs/${encodeURIComponent(runId)}/events`)
  const after = incoming.searchParams.get('after')
  if (after) upstream.searchParams.set('after', after)
  const upstreamController = new AbortController()
  const abortUpstream = () => upstreamController.abort()
  const closeUpstream = () => {
    request.signal.removeEventListener('abort', abortUpstream)
    upstreamController.abort()
  }
  request.signal.addEventListener('abort', abortUpstream, { once: true })

  let response: Response
  try {
    response = await fetch(upstream, {
      method: 'GET',
      headers: {
        Accept: 'text/event-stream',
        'Accept-Encoding': 'identity',
        ...(request.headers.get('last-event-id') ? { 'Last-Event-ID': request.headers.get('last-event-id') as string } : {}),
      },
      cache: 'no-store',
      signal: upstreamController.signal,
    })
  } catch {
    request.signal.removeEventListener('abort', abortUpstream)
    return Response.json({ error: { code: 'AGENT_EVENT_STREAM_UNAVAILABLE', message: '无法连接后端 Agent 事件流。' } }, { status: 502 })
  }

  if (!response.ok || !response.body) {
    request.signal.removeEventListener('abort', abortUpstream)
    return new Response(await response.text(), {
      status: response.status,
      headers: { 'Content-Type': response.headers.get('content-type') ?? 'application/json' },
    })
  }

  return new Response(resilientSseProxyBody(response.body, closeUpstream), {
    status: response.status,
    headers: {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'Content-Encoding': 'identity',
      'X-Accel-Buffering': 'no',
    },
  })
}
