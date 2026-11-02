const analysisRouteParameter = 'analysis_id'

export function normalizeAnalysisRouteId(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const analysisId = value.trim()
  return analysisId && analysisId.length <= 180 && /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(analysisId)
    ? analysisId
    : null
}

export function readAnalysisRouteId(): string | null {
  if (typeof window === 'undefined') return null
  return normalizeAnalysisRouteId(new URLSearchParams(window.location.search).get(analysisRouteParameter))
}

export function writeAnalysisRouteId(value: string | null | undefined): string | null {
  const analysisId = normalizeAnalysisRouteId(value)
  if (typeof window === 'undefined') return analysisId
  const url = new URL(window.location.href)
  if (analysisId) url.searchParams.set(analysisRouteParameter, analysisId)
  else url.searchParams.delete(analysisRouteParameter)
  const next = `${url.pathname}${url.search}${url.hash}`
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`
  if (next !== current) window.history.replaceState(window.history.state, '', next)
  return analysisId
}

export function clearAnalysisRouteId() {
  return writeAnalysisRouteId(null)
}
