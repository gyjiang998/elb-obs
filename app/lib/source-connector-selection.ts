const storageKey = 'heterogeneous-enablement.selected-source-connector.v1'

export function readSourceConnectorId(): string {
  if (typeof window === 'undefined') return ''
  try {
    return window.localStorage.getItem(storageKey)?.trim() ?? ''
  } catch {
    return ''
  }
}

export function writeSourceConnectorId(connectorId: string): void {
  if (typeof window === 'undefined') return
  try {
    if (connectorId.trim()) window.localStorage.setItem(storageKey, connectorId.trim())
    else window.localStorage.removeItem(storageKey)
  } catch {
    // Browser storage can be disabled; the in-memory selection still works.
  }
}
