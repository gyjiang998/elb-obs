/** Page task transport helpers. Acceptance means PTY delivery, not Agent completion. */
export async function waitForAgentTarget<T>(
  inspect: () => { target: T | null; error?: string },
  options: { timeoutMs?: number; intervalMs?: number; signal?: AbortSignal } = {},
): Promise<T> {
  const deadline = Date.now() + (options.timeoutMs ?? 90_000)
  while (true) {
    if (options.signal?.aborted) throw new Error('页面已关闭，排队的 Agent 命令未发送。')
    const state = inspect()
    if (state.error) throw new Error(state.error)
    if (state.target) return state.target
    if (Date.now() >= deadline) throw new Error('等待 Agent 终端就绪超时，命令未发送；连接恢复后可重试。')
    await new Promise((resolve) => setTimeout(resolve, options.intervalMs ?? 100))
  }
}

/** Context and command form one ordered operation, alongside raw terminal input. */
export function enqueueAgentSubmission(
  chain: { current: Promise<void> },
  operation: () => Promise<void>,
): Promise<void> {
  const pending = chain.current.catch(() => undefined).then(operation)
  chain.current = pending.catch(() => undefined)
  return pending
}

type AgentSubmissionTarget = { generation: number; adapterId?: string; sessionId?: string; disposed?: boolean }

/** Bind an unsent intent to its target; first initialization may supply missing IDs. */
export function createAgentSubmissionGuard(inspect: () => AgentSubmissionTarget): () => void {
  const initial = inspect()
  const generation = initial.generation
  const initiallyDisposed = initial.disposed
  let adapterId = initial.adapterId
  let sessionId = initial.sessionId
  return () => {
    const current = inspect()
    if (current.disposed || initiallyDisposed) throw new Error('页面已关闭，排队的 Agent 命令未发送。')
    if (current.generation !== generation
      || (adapterId && current.adapterId !== adapterId)
      || (sessionId && current.sessionId !== sessionId)) {
      throw new Error('Agent 或会话已切换，原排队命令未发送；请在当前会话重新提交。')
    }
    adapterId ||= current.adapterId
    sessionId ||= current.sessionId
  }
}

/** A target change may reject pending work, never retroactively reject a sent command. */
export async function runGuardedAgentSubmission<T>(input: {
  assertCurrent: () => void
  ensureSession: () => Promise<T>
  syncContext: (session: T) => Promise<unknown>
  sendCommand: (session: T) => Promise<unknown>
}): Promise<void> {
  input.assertCurrent()
  const session = await input.ensureSession()
  input.assertCurrent()
  await input.syncContext(session)
  input.assertCurrent()
  await input.sendCommand(session)
}

export type ParallelAgentTaskKind = 'scenario_capture' | 'parallel_modeling' | 'workload_features' | 'optimization_calibration'

const cancelledSubmissions = new Set<string>()
export function cancelAgentTaskSubmission(requestId: string): void {
  if (requestId) cancelledSubmissions.add(requestId)
}
export function assertAgentTaskSubmissionActive(requestId: string): void {
  if (cancelledSubmissions.has(requestId)) throw new Error('本次助手请求已撤销，排队的命令未发送。')
}

const parallelTaskDefaults: Record<ParallelAgentTaskKind, { skill: string; label: string }> = {
  scenario_capture: { skill: 'scenario-step-capture', label: '生成负载采样计划' },
  parallel_modeling: { skill: 'builditem-parallel-modeling', label: '分析并行方案与核频推荐' },
  workload_features: { skill: '', label: '提取负载特征并分析性能瓶颈' },
  optimization_calibration: { skill: '', label: '分析模型校准方案与实测结果' },
}

export function parallelAgentTaskDetail(kind: ParallelAgentTaskKind, context: Record<string, unknown>, message: string, requestId: string) {
  const defaults = parallelTaskDefaults[kind]
  const skill = typeof context.required_skill_id === 'string' ? context.required_skill_id
    : typeof context.skill_id === 'string' ? context.skill_id : defaults.skill
  return { ...context, request_id: requestId, task_kind: kind, required_skill_id: skill.trim(), message,
    display_message: typeof context.display_message === 'string' ? context.display_message : defaults.label }
}

/** Let the caller save requestId before a synchronous receiver can reject it. */
export function dispatchDeferredAgentTask(target: EventTarget, detail: Record<string, unknown>): void {
  queueMicrotask(() => {
    if (!cancelledSubmissions.has(String(detail.request_id ?? ''))) target.dispatchEvent(new CustomEvent('page-agent-autonomous-task', { detail }))
  })
}
