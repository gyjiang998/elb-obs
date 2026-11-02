import type { JsonRecord } from './types'
import { randomUuid } from './random-id'

const actionEventName = 'reasoning-lab-action'
const resultEventName = 'reasoning-lab-action-result'

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/**
 * Runs one registered, asynchronous ReasoningLab action and waits for its
 * factual outcome. Device execution remains limited to registered recipes or
 * immutable, template-materialized Adapter artifacts.
 */
export function requestReasoningLabAction(detail: JsonRecord, timeoutMs = 300_000): Promise<JsonRecord> {
  if (typeof window === 'undefined') return Promise.reject(new Error('页面操作桥只能在浏览器中运行。'))
  const requestId = randomUuid()
  return new Promise((resolve, reject) => {
    let timeoutId = 0
    const cleanup = () => {
      window.removeEventListener(resultEventName, listener)
      window.clearTimeout(timeoutId)
    }
    const listener = (event: Event) => {
      const response = record((event as CustomEvent<unknown>).detail)
      if (text(response.request_id) !== requestId) return
      cleanup()
      const error = text(response.error)
      if (error) {
        reject(new Error(error))
        return
      }
      resolve(record(response.result))
    }
    timeoutId = window.setTimeout(() => {
      cleanup()
      reject(new Error('页面助手等待安全操作结果超时；页面状态可能仍在刷新，请查看当前工作台后继续。'))
    }, Math.max(5_000, timeoutMs))
    window.addEventListener(resultEventName, listener)
    window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent(actionEventName, { detail: { ...detail, request_id: requestId } }))
    }, 0)
  })
}

export function respondReasoningLabAction(requestId: unknown, result: JsonRecord): void {
  const normalizedRequestId = text(requestId)
  if (!normalizedRequestId || typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(resultEventName, { detail: { request_id: normalizedRequestId, result } }))
}

export function rejectReasoningLabAction(requestId: unknown, error: unknown): void {
  const normalizedRequestId = text(requestId)
  if (!normalizedRequestId || typeof window === 'undefined') return
  const message = error instanceof Error ? error.message : text(error) || '页面操作未完成。'
  window.dispatchEvent(new CustomEvent(resultEventName, { detail: { request_id: normalizedRequestId, error: message } }))
}
