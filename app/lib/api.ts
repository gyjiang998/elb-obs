// Same-origin is portable across Linux servers and Windows browsers. A split
// origin remains available for development through the explicit build value.
// `typeof process` keeps this shared module usable in the Electron renderer,
// where esbuild deliberately does not inject a Node.js process global.
export const apiBase = typeof process !== 'undefined' ? process.env.NEXT_PUBLIC_PLATFORM_API_BASE ?? '' : ''

export type PlatformTextResponse = { text: string; contentType?: string; filename?: string }
export type PlatformRequestContext = { owner?: string }
export type PlatformTransport = {
  request<T>(path: string, init?: RequestInit, context?: PlatformRequestContext): Promise<T>
  requestText?(path: string, init?: RequestInit, context?: PlatformRequestContext): Promise<PlatformTextResponse>
}
export type PlatformApi = <T>(path: string, init?: RequestInit) => Promise<T>

let platformTransport: PlatformTransport | null = null

/**
 * Install the host transport used by shared pages. The Web app leaves this
 * unset and keeps same-origin HTTP; Desktop installs an SSH-backed adapter.
 */
export function installPlatformTransport(transport: PlatformTransport | null): () => void {
  platformTransport = transport
  return () => {
    if (platformTransport === transport) platformTransport = null
  }
}

export class ApiError extends Error {
  readonly code: string
  readonly details: Record<string, unknown>
  readonly statusCode: number | null

  constructor(message: string, code = 'REQUEST_FAILED', details: Record<string, unknown> = {}, statusCode: number | null = null) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.details = details
    this.statusCode = statusCode
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function validationErrorSummary(value: unknown): string {
  const errors = Array.isArray(value) ? value.map(asRecord) : []
  const first = errors[0]
  if (!first) return ''
  const field = typeof first.field === 'string'
    ? first.field
    : Array.isArray(first.loc)
      ? first.loc.filter((item) => item !== 'body').map(String).join('.')
      : ''
  const context = asRecord(first.context ?? first.ctx)
  const maxLength = typeof context.max_length === 'number' ? context.max_length : null
  const actualLength = typeof context.actual_length === 'number' ? context.actual_length : null
  if (maxLength !== null && actualLength !== null) {
    return `${field || '请求字段'}最多允许 ${maxLength} 项，实际为 ${actualLength} 项`
  }
  const message = typeof first.message === 'string' ? first.message : typeof first.msg === 'string' ? first.msg : ''
  return [field, message].filter(Boolean).join('：')
}

export function apiErrorFromPayload(payload: unknown, statusCode: number, fallback = `Request failed (${statusCode})`): ApiError {
  const root = asRecord(payload)
  const error = asRecord(root.error)
  const details = asRecord(error.details)
  const validationErrors = details.validation_errors ?? root.detail
  const validationSummary = validationErrorSummary(validationErrors)
  const message = typeof error.message === 'string' && error.message
    ? validationSummary ? `${error.message} ${validationSummary}。` : error.message
    : validationSummary ? `请求字段校验失败：${validationSummary}。` : fallback
  const code = typeof error.code === 'string' && error.code
    ? error.code
    : validationSummary ? 'REQUEST_VALIDATION_FAILED' : 'REQUEST_FAILED'
  return new ApiError(
    message,
    code,
    Object.keys(details).length > 0 ? details : validationSummary ? { validation_errors: validationErrors } : {},
    statusCode,
  )
}

export async function api<T>(path: string, init: RequestInit = {}, context: PlatformRequestContext = {}): Promise<T> {
  if (platformTransport) return platformTransport.request<T>(path, init, context)
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(`${apiBase}${path}`, { ...init, headers })
  const payload: unknown = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw apiErrorFromPayload(payload, response.status)
  }
  return payload as T
}

export async function apiText(path: string, init: RequestInit = {}, context: PlatformRequestContext = {}): Promise<PlatformTextResponse> {
  if (platformTransport?.requestText) return platformTransport.requestText(path, init, context)
  if (platformTransport) {
    const value = await platformTransport.request<PlatformTextResponse>(path, init, context)
    if (!value || typeof value.text !== 'string') throw new ApiError('平台没有返回文本内容', 'INVALID_TEXT_RESPONSE')
    return value
  }
  const response = await fetch(`${apiBase}${path}`, init)
  if (!response.ok) throw apiErrorFromPayload(await response.json().catch(() => ({})), response.status)
  return {
    text: await response.text(),
    contentType: response.headers.get('Content-Type') ?? undefined,
    filename: response.headers.get('Content-Disposition')?.match(/filename="?([^";]+)/)?.[1],
  }
}

/** Bind requests to their owning page so Desktop can prioritize visible work. */
export function platformApiFor(owner: string): PlatformApi {
  const context = { owner }
  return <T>(path: string, init: RequestInit = {}) => api<T>(path, init, context)
}

export function platformTextApiFor(owner: string): typeof apiText {
  const context = { owner }
  return (path: string, init: RequestInit = {}) => apiText(path, init, context)
}

export async function upload<T>(path: string, formData: FormData): Promise<T> {
  return api<T>(path, { method: 'POST', body: formData })
}

export type UploadProgress = {
  loaded: number
  total: number
  ratio: number | null
  phase: 'uploading' | 'processing'
}

export function uploadWithProgress<T>(
  path: string,
  formData: FormData,
  onProgress: (progress: UploadProgress) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    request.open('POST', `${apiBase}${path}`)
    request.upload.onprogress = (event) => {
      onProgress({
        loaded: event.loaded,
        total: event.lengthComputable ? event.total : 0,
        ratio: event.lengthComputable && event.total > 0 ? event.loaded / event.total : null,
        phase: 'uploading',
      })
    }
    request.upload.onload = () => {
      onProgress({ loaded: 0, total: 0, ratio: 1, phase: 'processing' })
    }
    request.onerror = () => reject(new ApiError('Trace 上传连接失败', 'TRACE_UPLOAD_NETWORK_FAILED'))
    request.onabort = () => reject(new ApiError('Trace 上传已取消', 'TRACE_UPLOAD_ABORTED'))
    request.onload = () => {
      let payload: unknown = {}
      try { payload = JSON.parse(request.responseText) as unknown } catch { /* Keep the HTTP fallback below. */ }
      if (request.status < 200 || request.status >= 300) {
        reject(apiErrorFromPayload(payload, request.status || 500))
        return
      }
      resolve(payload as T)
    }
    request.send(formData)
  })
}

export function formatBytes(value?: number): string {
  if (!value || value < 1024) return `${value ?? 0} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = value / 1024
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[index]}`
}

export async function digest(value: unknown): Promise<string> {
  const text = JSON.stringify(value)
  const encoded = new TextEncoder().encode(text)
  if (globalThis.crypto?.subtle) {
    const bytes = await globalThis.crypto.subtle.digest('SHA-256', encoded)
    return Array.from(new Uint8Array(bytes), (item) => item.toString(16).padStart(2, '0')).join('')
  }
  return Array.from(encoded, (item) => item.toString(16).padStart(2, '0')).join('').slice(0, 64).padEnd(64, '0')
}
