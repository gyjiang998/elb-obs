type DeliverySpec = {
  label: string
  identifierPaths: string[][]
}

export type ArtifactDelivery = {
  actionId: string
  identifier: string
  label: string
  summary?: string
}

const deliverySpecs: Record<string, DeliverySpec> = {
  'reasoning.run_hardware_semantic_route': {
    label: '硬件单元路由结果',
    identifierPaths: [['routing_result_artifact_id']],
  },
  'reasoning.run_strategy': {
    label: '硬件机会推理结果',
    identifierPaths: [['result_artifact_id']],
  },
  'reasoning.run_blind_analysis': {
    label: '独立盲推基线',
    identifierPaths: [['result_artifact_id']],
  },
  'reasoning.compare_blind_flow': {
    label: '盲推对比结果',
    identifierPaths: [['comparison_artifact_id']],
  },
  'application_flow.publish_candidates': {
    label: '应用流程候选',
    identifierPaths: [['artifact', 'artifact_id']],
  },
  'application_flow.save_discovery': {
    label: '应用流程候选',
    identifierPaths: [['artifact', 'artifact_id']],
  },
  'application_flow.save_scenario': {
    label: '应用流程阶段图',
    identifierPaths: [['artifact', 'artifact_id']],
  },
  'application_flow.compile_mermaid': {
    label: '应用流程阶段图',
    identifierPaths: [['artifact', 'artifact_id']],
  },
  'application_flow.enrich_trace_anchors': {
    label: '源码 Trace 锚点分析',
    identifierPaths: [['artifact', 'artifact_id']],
  },
  'application_flow.apply_runtime_trace_alignment': {
    label: 'Trace 校准与分析预览',
    identifierPaths: [['artifact', 'artifact_id']],
  },
  'code_graph.publish_report': {
    label: '代码知识报告',
    identifierPaths: [['report', 'artifact_id']],
  },
  'experimental_flow.publish_result': {
    label: 'Playground 流程分析',
    identifierPaths: [['result', 'artifact_id']],
  },
  'knowledge.build_custom_api_kb': {
    label: '自建硬件 API 知识库',
    identifierPaths: [['knowledge_base', 'kb_id']],
  },
  'skills.save_user_version': {
    label: '自编辑 Skill 版本',
    identifierPaths: [['version', 'version_id']],
  },
  'experience.save_review': {
    label: '经验演进评审',
    identifierPaths: [['review_artifact_id']],
  },
  'platform.record_experiment_findings': {
    label: '真机实验发现',
    identifierPaths: [['experiment_learning_report_artifact_id']],
  },
  'device.collect_artifact': {
    label: '负载采样工件',
    identifierPaths: [['collection_artifact_id']],
  },
}

const deliveryLedgerKey = 'heterogeneous-enable-web:artifact-delivery-ledger/v1'
const permissionPromptedAtKey = 'heterogeneous-enable-web:artifact-notification-permission-prompted-at/v1'
const deliveryLedgerLimit = 200
const deliveryLedgerMaxAgeMs = 14 * 24 * 60 * 60 * 1000
const permissionPromptRetryMs = 7 * 24 * 60 * 60 * 1000

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function stringAtPath(value: unknown, path: string[]): string {
  let current: unknown = value
  for (const segment of path) current = recordValue(current)[segment]
  return typeof current === 'string' ? current.trim() : ''
}

function compactSummary(value: unknown): string | undefined {
  const result = recordValue(value)
  const candidates = [result.summary_zh, result.summary, result.message]
  for (const candidate of candidates) {
    if (typeof candidate !== 'string') continue
    const compacted = candidate.replace(/\s+/g, ' ').trim()
    if (compacted) return compacted.slice(0, 140)
  }
  return undefined
}

export function artifactDeliveryFromToolResult(actionId: string, result: unknown): ArtifactDelivery | null {
  const spec = deliverySpecs[actionId]
  if (!spec) return null
  const identifier = spec.identifierPaths
    .map((path) => stringAtPath(result, path))
    .find(Boolean)
  if (!identifier) return null
  return {
    actionId,
    identifier,
    label: spec.label,
    summary: compactSummary(result),
  }
}

function pageIsFocused(): boolean {
  return typeof document !== 'undefined'
    && document.visibilityState === 'visible'
    && document.hasFocus()
}

function readDeliveryLedger(now: number): Record<string, number> {
  if (typeof window === 'undefined') return {}
  try {
    const parsed = recordValue(JSON.parse(window.localStorage.getItem(deliveryLedgerKey) ?? '{}'))
    return Object.fromEntries(Object.entries(parsed).flatMap(([key, rawTimestamp]) => {
      const timestamp = typeof rawTimestamp === 'number' ? rawTimestamp : Number(rawTimestamp)
      return Number.isFinite(timestamp) && now - timestamp < deliveryLedgerMaxAgeMs
        ? [[key, timestamp]]
        : []
    }))
  } catch {
    return {}
  }
}

function claimDelivery(delivery: ArtifactDelivery): boolean {
  if (typeof window === 'undefined') return false
  const now = Date.now()
  const key = `${delivery.actionId}:${delivery.identifier}`
  const ledger = readDeliveryLedger(now)
  if (key in ledger) return false
  const entries = Object.entries({ ...ledger, [key]: now })
    .sort((left, right) => right[1] - left[1])
    .slice(0, deliveryLedgerLimit)
  try {
    window.localStorage.setItem(deliveryLedgerKey, JSON.stringify(Object.fromEntries(entries)))
  } catch {
    // In-memory delivery events still render when browser persistence is unavailable.
  }
  return true
}

export class ArtifactDeliveryNotifier {
  private baseTitle = ''
  private pendingCount = 0
  private permissionRequested = false
  private attached = false
  private readonly seenDeliveries = new Set<string>()

  private readonly markVisible = () => {
    if (!pageIsFocused()) return
    this.pendingCount = 0
    if (this.baseTitle) document.title = this.baseTitle
  }

  attach(): () => void {
    if (typeof window === 'undefined' || this.attached) return () => undefined
    this.attached = true
    this.baseTitle = document.title.replace(/^\(\d+\) 新交付 · /, '') || '异构使能优化'
    window.addEventListener('focus', this.markVisible)
    document.addEventListener('visibilitychange', this.markVisible)
    return () => this.detach()
  }

  detach(): void {
    if (typeof window === 'undefined' || !this.attached) return
    window.removeEventListener('focus', this.markVisible)
    document.removeEventListener('visibilitychange', this.markVisible)
    this.attached = false
    if (this.baseTitle) document.title = this.baseTitle
  }

  preparePermission(): void {
    if (typeof window === 'undefined' || this.permissionRequested) return
    this.permissionRequested = true
    if (!window.isSecureContext || !('Notification' in window) || Notification.permission !== 'default') return
    const now = Date.now()
    try {
      const promptedAt = Number(window.localStorage.getItem(permissionPromptedAtKey) ?? 0)
      if (Number.isFinite(promptedAt) && promptedAt > 0 && now - promptedAt < permissionPromptRetryMs) return
      window.localStorage.setItem(permissionPromptedAtKey, String(now))
    } catch {
      // The browser can still show the one-time permission prompt without persistence.
    }
    try {
      void Notification.requestPermission().catch(() => undefined)
    } catch {
      // The title marker remains available when the browser declines the permission request.
    }
  }

  announce(delivery: ArtifactDelivery): void {
    if (typeof window === 'undefined') return
    const deliveryKey = `${delivery.actionId}:${delivery.identifier}`
    if (this.seenDeliveries.has(deliveryKey)) return
    this.seenDeliveries.add(deliveryKey)
    if (!claimDelivery(delivery)) return
    if (pageIsFocused()) {
      this.markVisible()
      return
    }
    this.pendingCount += 1
    const title = this.baseTitle || document.title
    document.title = `(${this.pendingCount}) 新交付 · ${title}`
    if (!window.isSecureContext || !('Notification' in window) || Notification.permission !== 'granted') return
    try {
      const notification = new Notification(`异构使能优化：${delivery.label}`, {
        body: delivery.summary || '交付件已生成，返回页面查看。',
        icon: '/favicon.svg',
        tag: `artifact-delivery:${delivery.actionId}:${delivery.identifier}`,
      })
      notification.onclick = () => {
        window.focus()
        notification.close()
      }
    } catch {
      // The title marker is the fallback for browsers that reject page notifications.
    }
  }
}
