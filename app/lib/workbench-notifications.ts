'use client'

export const workbenchNotificationEvent = 'heterogeneous-workbench:notification'

export type WorkbenchNotificationTone = 'error' | 'warning' | 'success' | 'info'

export type WorkbenchRecoveryDescriptor = {
  operationAttemptId: string
  operationKey: string
  label: string
  scopes: Array<'device' | 'server'>
  selectedDevice?: string
  planId?: string
  planRevision?: string
  validateContext?: () => void | Promise<void>
  retry: (context: {
    repairRequestId: string
    retryCount: 1
    retryPlanId?: string
    retryPlanRevision?: string
  }) => unknown | Promise<unknown>
}

export type WorkbenchNotificationDetail = {
  tone: WorkbenchNotificationTone
  text: string
  source?: string
  page?: string
  pageLabel?: string
  operation?: string
  errorCode?: string
  context?: Record<string, unknown>
  dedupeKey?: string
  timeoutMs?: number
  onDismiss?: () => void
  recovery?: WorkbenchRecoveryDescriptor
  handled?: boolean
}

export function notificationText(value: unknown, fallback = '操作失败'): string {
  const raw = value instanceof Error ? value.message : String(value || fallback)
  return raw
    .replace(/^Error invoking remote method '[^']+': Error:\s*/i, '')
    .replace(/^Error:\s*/i, '')
    .trim() || fallback
}

export function publishWorkbenchNotification(detail: Omit<WorkbenchNotificationDetail, 'handled'>): boolean {
  if (typeof window === 'undefined') return false
  const payload: WorkbenchNotificationDetail = {
    ...detail,
    text: notificationText(detail.text, detail.tone === 'error' ? '操作失败' : '操作提示'),
    handled: false,
  }
  window.dispatchEvent(new CustomEvent<WorkbenchNotificationDetail>(workbenchNotificationEvent, { detail: payload }))
  return payload.handled === true
}

