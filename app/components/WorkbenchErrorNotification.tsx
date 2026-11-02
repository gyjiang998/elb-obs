'use client'

import { useEffect, useRef, useState } from 'react'
import { publishWorkbenchNotification } from '../lib/workbench-notifications'

type NoticeLike = {
  tone?: string
  text?: string
  message?: string
  code?: string
  errorCode?: string
  error_code?: string
  operation?: string
  context?: Record<string, unknown>
} | null | undefined

/**
 * Sends transient page errors to the Desktop viewport notification center.
 * Web pages without an installed center retain their existing inline error.
 */
export function useWorkbenchErrorNotification(notice: NoticeLike, onDismiss: () => void, source: string): boolean {
  const dismissRef = useRef(onDismiss)
  dismissRef.current = onDismiss
  // Error metadata is supplemental to the message identity. Some callers
  // construct a fresh context object on every render, so depending on it would
  // republish the same notification after setForwardedKey triggers a render.
  // Keep the latest metadata in a ref and publish it only when the stable error
  // key changes.
  const metadataRef = useRef({
    operation: notice?.operation,
    errorCode: notice?.errorCode ?? notice?.error_code ?? notice?.code,
    context: notice?.context,
  })
  metadataRef.current = {
    operation: notice?.operation,
    errorCode: notice?.errorCode ?? notice?.error_code ?? notice?.code,
    context: notice?.context,
  }
  const tone = notice?.tone ?? ''
  const message = notice?.text ?? notice?.message ?? ''
  const key = tone === 'error' && message ? `${source}\u0000${message}` : ''
  const currentKeyRef = useRef(key)
  currentKeyRef.current = key
  const [forwardedKey, setForwardedKey] = useState('')

  useEffect(() => {
    if (!key) {
      setForwardedKey('')
      return
    }
    const metadata = metadataRef.current
    const handled = publishWorkbenchNotification({
      tone: 'error',
      text: message,
      source,
      operation: metadata.operation,
      errorCode: metadata.errorCode,
      context: metadata.context,
      dedupeKey: key,
      // A sticky notification can outlive the page state that created it.
      // Closing an older card must never clear a newer error or interrupt a
      // retry that is already running on the same page.
      onDismiss: () => {
        if (currentKeyRef.current === key) dismissRef.current()
      },
    })
    setForwardedKey(handled ? key : '')
  }, [key, message, source])

  return Boolean(key && forwardedKey === key)
}

