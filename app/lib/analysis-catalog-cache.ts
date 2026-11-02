'use client'

import { api } from './api'

type CacheEntry = {
  expiresAt: number
  request: Promise<unknown>
}

type CacheOptions = {
  force?: boolean
  ttlMs?: number
}

const PAGE_SESSION_TTL_MS = Number.POSITIVE_INFINITY
const catalogCache = new Map<string, CacheEntry>()

/**
 * Coalesce read-only catalogue requests across workbench components. Failed
 * requests are evicted so a later click can retry instead of replaying an old
 * network error. Keep this cache demand-driven: Skill text and prompt
 * contracts ship in the page bundle, while full accelerator API record sets
 * must never be fetched merely because the workbench mounted.
 */
export function loadAnalysisCatalog<T>(path: string, options: CacheOptions = {}): Promise<T> {
  const now = Date.now()
  const current = catalogCache.get(path)
  if (!options.force && current && current.expiresAt > now) {
    return current.request as Promise<T>
  }

  const request = api<T>(path).catch((error) => {
    if (catalogCache.get(path)?.request === request) catalogCache.delete(path)
    throw error
  })
  catalogCache.set(path, {
    expiresAt: now + (options.ttlMs ?? PAGE_SESSION_TTL_MS),
    request,
  })
  return request
}

export function hardwareApiRecordsPath(unitId: string): string {
  if (unitId === 'npu') return '/v1/hardware-kb/npu-api/records'
  if (unitId.startsWith('custom:')) {
    return `/v1/custom-api-kbs/${encodeURIComponent(unitId.slice('custom:'.length))}`
  }
  return `/v1/hardware-kb/accelerator-api/${encodeURIComponent(unitId)}/records`
}

export function loadHardwareApiRecords<T>(unitId: string, options: CacheOptions = {}): Promise<T> {
  return loadAnalysisCatalog<T>(hardwareApiRecordsPath(unitId), options)
}

export function loadCustomApiCatalog<T>(options: CacheOptions = {}): Promise<T> {
  return loadAnalysisCatalog<T>('/v1/custom-api-kbs', options)
}

export function invalidateAnalysisCatalog(path?: string): void {
  if (path) {
    catalogCache.delete(path)
    return
  }
  catalogCache.clear()
}
