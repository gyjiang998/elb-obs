/**
 * Turn an upstream SSE body into a cancellation-aware body for Next.js.
 *
 * A browser refresh normally cancels the downstream socket while the backend
 * stream is still open. Passing the undici body through directly makes that
 * expected lifecycle surface as Next's noisy "failed to pipe response" error.
 * This wrapper treats cancellation or an upstream reset as a clean SSE EOF;
 * EventSource clients reconnect from their sequence cursor.
 */
export function resilientSseProxyBody(
  upstream: ReadableStream<Uint8Array>,
  cancelUpstream: () => void,
): ReadableStream<Uint8Array> {
  const reader = upstream.getReader()
  let finished = false

  const finish = (controller?: ReadableStreamDefaultController<Uint8Array>) => {
    if (finished) return
    finished = true
    cancelUpstream()
    try {
      controller?.close()
    } catch {
      // The downstream may already be closed by the browser.
    }
  }

  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (finished) return
      try {
        const next = await reader.read()
        if (next.done) {
          finish(controller)
          return
        }
        controller.enqueue(next.value)
      } catch {
        // Backend restart and browser cancellation are recoverable for SSE.
        finish(controller)
      }
    },
    async cancel(reason) {
      finish()
      try {
        await reader.cancel(reason)
      } catch {
        // The upstream may already have reset its socket.
      }
    },
  })
}
