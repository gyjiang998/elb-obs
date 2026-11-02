type WorkflowFocusTarget = { readonly current: HTMLElement | null }

export function focusWorkflowAction(target: WorkflowFocusTarget): void {
  if (typeof window === 'undefined') return
  window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
    const element = target.current
    if (!element) return
    element.scrollIntoView({ behavior: 'smooth', block: 'center' })
    const disabled = element.matches(':disabled, [aria-disabled="true"]')
    if (!disabled) element.focus({ preventScroll: true })
  }))
}
