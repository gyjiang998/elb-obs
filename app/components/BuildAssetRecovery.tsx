'use client'

import { useEffect } from 'react'

const NEXT_STATIC_ASSET_PATH = '/_next/static/'
const RECOVERY_QUERY_KEY = '_asset_reload'
const RECOVERY_SESSION_KEY = 'hetero:asset-reload-at'
const RECOVERY_GUARD_MS = 30_000
const RECOVERY_SETTLE_MS = 10_000

function containsChunkLoadFailure(value: unknown): boolean {
  const text = value instanceof Error ? `${value.name}: ${value.message}` : String(value ?? '')
  return /ChunkLoadError|Loading chunk [^ ]+ failed|Failed to fetch dynamically imported module/i.test(text)
}

function recoverFromStaleBuild(): void {
  const now = Date.now()
  const lastAttempt = Number(window.sessionStorage.getItem(RECOVERY_SESSION_KEY) ?? 0)
  if (Number.isFinite(lastAttempt) && now - lastAttempt < RECOVERY_GUARD_MS) return

  window.sessionStorage.setItem(RECOVERY_SESSION_KEY, String(now))
  const url = new URL(window.location.href)
  url.searchParams.set(RECOVERY_QUERY_KEY, String(now))
  window.location.replace(url.toString())
}

export function BuildAssetRecovery() {
  useEffect(() => {
    const handleResourceError = (event: Event) => {
      const target = event.target
      const assetUrl = target instanceof HTMLScriptElement
        ? target.src
        : target instanceof HTMLLinkElement
          ? target.href
          : ''
      if (assetUrl.includes(NEXT_STATIC_ASSET_PATH)) recoverFromStaleBuild()
    }
    const handleRuntimeError = (event: ErrorEvent) => {
      if (containsChunkLoadFailure(event.error ?? event.message)) recoverFromStaleBuild()
    }
    const handleRejectedPromise = (event: PromiseRejectionEvent) => {
      if (containsChunkLoadFailure(event.reason)) recoverFromStaleBuild()
    }

    window.addEventListener('error', handleResourceError, true)
    window.addEventListener('error', handleRuntimeError)
    window.addEventListener('unhandledrejection', handleRejectedPromise)

    const settleTimer = window.setTimeout(() => {
      window.sessionStorage.removeItem(RECOVERY_SESSION_KEY)
      const url = new URL(window.location.href)
      if (!url.searchParams.has(RECOVERY_QUERY_KEY)) return
      url.searchParams.delete(RECOVERY_QUERY_KEY)
      window.history.replaceState(window.history.state, '', url.toString())
    }, RECOVERY_SETTLE_MS)

    return () => {
      window.clearTimeout(settleTimer)
      window.removeEventListener('error', handleResourceError, true)
      window.removeEventListener('error', handleRuntimeError)
      window.removeEventListener('unhandledrejection', handleRejectedPromise)
    }
  }, [])

  return null
}
