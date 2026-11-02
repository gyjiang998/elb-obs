export type AgentActionRequest = {
  action_id?: string
  input?: Record<string, unknown>
}

export type ParsedAgentResponse = {
  reply?: string
  actions: AgentActionRequest[]
  parseStatus: 'strict' | 'recovered' | 'invalid'
  parseError?: string
}

export type AgentActionObservation = {
  actionId: string
  input: Record<string, unknown>
  result: string
  output?: unknown
  failed?: boolean
}

export type DeviceActionFact = {
  actionId: string
  status: 'succeeded' | 'warning' | 'failed' | 'deferred'
  summary: string
}

export type DeviceValidationFacts = {
  schemaVersion: 'device_validation_facts/v1'
  deviceSerial?: string
  probeArtifactId?: string
  profileIds: string[]
  stagedSourceArtifactId?: string
  buildResultArtifactId?: string
  deploymentArtifactId?: string
  deploymentVerified?: boolean
  deploymentFailureStage?: string
  experimentRunArtifactId?: string
  remoteBinaryPath?: string
  noConnectedDevice?: boolean
  profileUnavailable?: boolean
  experimentExecution?: {
    command: string
    remoteShellRunArtifactId?: string
    returnCode?: number
    status?: string
    outputExcerpt?: string
  }
  actionFacts: DeviceActionFact[]
}

export type DeviceValidationTerminal = {
  kind: 'remote_execution_observed' | 'environment_blocked'
  detail: string
  warning: boolean
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function number(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function normalizedActions(value: unknown): AgentActionRequest[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item): AgentActionRequest[] => {
    const action = asRecord(item)
    const actionId = text(action.action_id)
    if (!actionId) return []
    const input = asRecord(action.input)
    return [{ action_id: actionId, input }]
  })
}

function fencedJsonCandidate(content: string): string {
  const fenced = content.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1]
  if (fenced) return fenced.trim()
  const first = content.indexOf('{')
  return first >= 0 ? content.slice(first).trim() : content.trim()
}

function findTopLevelPropertyValueStart(content: string, expectedKey: string): number | null {
  let depth = 0
  let inString = false
  let escaped = false
  for (let index = 0; index < content.length; index += 1) {
    const character = content[index]
    if (inString) {
      if (escaped) escaped = false
      else if (character === '\\') escaped = true
      else if (character === '"') inString = false
      continue
    }
    if (character === '"') {
      const keyStart = index
      let cursor = index + 1
      let keyEscaped = false
      for (; cursor < content.length; cursor += 1) {
        const next = content[cursor]
        if (keyEscaped) {
          keyEscaped = false
          continue
        }
        if (next === '\\') {
          keyEscaped = true
          continue
        }
        if (next === '"') break
      }
      if (cursor >= content.length) return null
      const rawKey = content.slice(keyStart, cursor + 1)
      let key = ''
      try {
        key = JSON.parse(rawKey) as string
      } catch {
        key = ''
      }
      let after = cursor + 1
      while (/\s/.test(content[after] ?? '')) after += 1
      if (depth === 1 && key === expectedKey && content[after] === ':') {
        after += 1
        while (/\s/.test(content[after] ?? '')) after += 1
        return after
      }
      index = cursor
      continue
    }
    if (character === '{' || character === '[') depth += 1
    if (character === '}' || character === ']') depth = Math.max(0, depth - 1)
  }
  return null
}

function jsonStringAt(content: string, start: number): string | null {
  if (content[start] !== '"') return null
  let escaped = false
  for (let cursor = start + 1; cursor < content.length; cursor += 1) {
    const character = content[cursor]
    if (escaped) {
      escaped = false
      continue
    }
    if (character === '\\') {
      escaped = true
      continue
    }
    if (character !== '"') continue
    try {
      const value = JSON.parse(content.slice(start, cursor + 1))
      return typeof value === 'string' ? value : null
    } catch {
      return null
    }
  }
  return null
}

function balancedObjectEnd(content: string, start: number): number | null {
  if (content[start] !== '{') return null
  let depth = 0
  let inString = false
  let escaped = false
  for (let cursor = start; cursor < content.length; cursor += 1) {
    const character = content[cursor]
    if (inString) {
      if (escaped) escaped = false
      else if (character === '\\') escaped = true
      else if (character === '"') inString = false
      continue
    }
    if (character === '"') {
      inString = true
      continue
    }
    if (character === '{') depth += 1
    if (character === '}') {
      depth -= 1
      if (depth === 0) return cursor + 1
    }
  }
  return null
}

function recoveredActions(content: string): AgentActionRequest[] {
  const start = findTopLevelPropertyValueStart(content, 'actions')
  if (start === null || content[start] !== '[') return []
  const actions: AgentActionRequest[] = []
  let cursor = start + 1
  while (cursor < content.length) {
    while (/\s|,/.test(content[cursor] ?? '')) cursor += 1
    if (content[cursor] !== '{') break
    const end = balancedObjectEnd(content, cursor)
    if (end === null) break
    try {
      const parsed = JSON.parse(content.slice(cursor, end))
      actions.push(...normalizedActions([parsed]))
    } catch {
      // A partially emitted action must never be executed. Continue only when
      // the next complete object can be located after a comma.
    }
    cursor = end
  }
  return actions
}

export function parseAgentResponse(content: string): ParsedAgentResponse {
  const candidate = fencedJsonCandidate(content)
  if (!candidate) return { reply: content, actions: [], parseStatus: 'invalid', parseError: '模型没有返回 JSON 对象。' }
  try {
    const parsed = asRecord(JSON.parse(candidate))
    return {
      reply: typeof parsed.reply === 'string' ? parsed.reply : content,
      actions: normalizedActions(parsed.actions),
      parseStatus: 'strict',
    }
  } catch (error) {
    const replyStart = findTopLevelPropertyValueStart(candidate, 'reply')
    const reply = replyStart === null ? null : jsonStringAt(candidate, replyStart)
    const actions = recoveredActions(candidate)
    const parseError = error instanceof Error ? error.message : '模型 JSON 解析失败。'
    if (reply !== null || actions.length > 0) {
      return {
        reply: reply ?? '模型响应的外层 JSON 不完整；平台已恢复其中结构完整的 action 并继续执行。',
        actions,
        parseStatus: 'recovered',
        parseError,
      }
    }
    return { reply: content, actions: [], parseStatus: 'invalid', parseError }
  }
}

export function createDeviceValidationFacts(): DeviceValidationFacts {
  return {
    schemaVersion: 'device_validation_facts/v1',
    profileIds: [],
    actionFacts: [],
  }
}

export function normalizeDeviceValidationFacts(value: unknown): DeviceValidationFacts {
  const record = asRecord(value)
  const actionFacts = Array.isArray(record.actionFacts) ? record.actionFacts.flatMap((item): DeviceActionFact[] => {
    const fact = asRecord(item)
    const actionId = text(fact.actionId)
    const status = fact.status
    const summary = text(fact.summary)
    return actionId && summary && (status === 'succeeded' || status === 'warning' || status === 'failed' || status === 'deferred')
      ? [{ actionId, status, summary }]
      : []
  }).slice(-32) : []
  const execution = asRecord(record.experimentExecution)
  const historicalDeploymentVerified = typeof record.deploymentVerified === 'boolean'
    ? record.deploymentVerified
    : Boolean(text(record.deploymentArtifactId) && text(record.remoteBinaryPath))
  return {
    schemaVersion: 'device_validation_facts/v1',
    ...(text(record.deviceSerial) ? { deviceSerial: text(record.deviceSerial) } : {}),
    ...(text(record.probeArtifactId) ? { probeArtifactId: text(record.probeArtifactId) } : {}),
    profileIds: Array.isArray(record.profileIds) ? record.profileIds.filter((item): item is string => typeof item === 'string' && item.trim().length > 0).slice(-12) : [],
    ...(text(record.stagedSourceArtifactId) ? { stagedSourceArtifactId: text(record.stagedSourceArtifactId) } : {}),
    ...(text(record.buildResultArtifactId) ? { buildResultArtifactId: text(record.buildResultArtifactId) } : {}),
    ...(text(record.deploymentArtifactId) ? { deploymentArtifactId: text(record.deploymentArtifactId) } : {}),
    ...(historicalDeploymentVerified ? { deploymentVerified: true } : typeof record.deploymentVerified === 'boolean' ? { deploymentVerified: false } : {}),
    ...(text(record.deploymentFailureStage) ? { deploymentFailureStage: text(record.deploymentFailureStage) } : {}),
    ...(text(record.experimentRunArtifactId) ? { experimentRunArtifactId: text(record.experimentRunArtifactId) } : {}),
    ...(text(record.remoteBinaryPath) ? { remoteBinaryPath: text(record.remoteBinaryPath) } : {}),
    ...(record.noConnectedDevice === true ? { noConnectedDevice: true } : {}),
    ...(record.profileUnavailable === true ? { profileUnavailable: true } : {}),
    ...(text(execution.command) ? {
      experimentExecution: {
        command: text(execution.command),
        ...(text(execution.remoteShellRunArtifactId) ? { remoteShellRunArtifactId: text(execution.remoteShellRunArtifactId) } : {}),
        ...(number(execution.returnCode) === undefined ? {} : { returnCode: number(execution.returnCode) }),
        ...(text(execution.status) ? { status: text(execution.status) } : {}),
        ...(text(execution.outputExcerpt) ? { outputExcerpt: text(execution.outputExcerpt).slice(0, 1600) } : {}),
      },
    } : {}),
    actionFacts,
  }
}

function observationStatus(observation: AgentActionObservation): DeviceActionFact['status'] {
  const output = asRecord(observation.output)
  const status = text(output.status)
  if (observation.failed || status === 'failed') return 'failed'
  if (status === 'warning' || status === 'compile_failed' || status === 'dependency_pull_failed' || status === 'deployment_failed') return 'warning'
  return 'succeeded'
}

function appendActionFact(facts: DeviceValidationFacts, observation: AgentActionObservation): void {
  facts.actionFacts = [
    ...facts.actionFacts,
    {
      actionId: observation.actionId,
      status: observationStatus(observation),
      summary: observation.result.slice(0, 900),
    },
  ].slice(-32)
}

export function updateDeviceValidationFacts(
  current: DeviceValidationFacts,
  observations: AgentActionObservation[],
): DeviceValidationFacts {
  const facts = normalizeDeviceValidationFacts(current)
  for (const observation of observations) {
    appendActionFact(facts, observation)
    const output = asRecord(observation.output)
    const status = text(output.status)
    if (observation.actionId === 'device.discover') {
      const serial = text(output.selected_device_serial)
      if (serial) facts.deviceSerial = serial
      if (number(output.device_count) === 0) facts.noConnectedDevice = true
    }
    if (observation.actionId === 'device.probe') {
      const serial = text(output.device_serial)
      const probeArtifactId = text(output.probe_artifact_id)
      if (serial) facts.deviceSerial = serial
      if (probeArtifactId && observationStatus(observation) !== 'failed') facts.probeArtifactId = probeArtifactId
      if (observationStatus(observation) === 'failed' && /DEVICE_NOT_CONNECTED|设备已断开|设备未连接/i.test(`${text(output.error_code)} ${text(output.error)} ${observation.result}`)) {
        facts.noConnectedDevice = true
      }
    }
    if (observation.actionId === 'device.describe_experiment_profiles' || observation.actionId === 'build.describe_profile') {
      const profileId = text(observation.input.profile_id)
      const profiles = Array.isArray(output.profiles) ? output.profiles : []
      const returnedIds = profiles.map(asRecord).map((profile) => text(profile.profile_id)).filter(Boolean)
      facts.profileIds = [...new Set([...facts.profileIds, ...returnedIds, ...(profileId && profiles.length > 0 ? [profileId] : [])])].slice(-12)
      if (profileId && observationStatus(observation) !== 'failed' && profiles.length === 0) facts.profileUnavailable = true
    }
    if (observation.actionId === 'device.stage_experiment_source') {
      const stagedSourceArtifactId = text(output.staged_source_artifact_id)
      if (stagedSourceArtifactId) facts.stagedSourceArtifactId = stagedSourceArtifactId
    }
    if (observation.actionId === 'build.stage_source') {
      const stagedSourceArtifactId = text(output.source_artifact_id)
      if (stagedSourceArtifactId) facts.stagedSourceArtifactId = stagedSourceArtifactId
    }
    if (observation.actionId === 'build.create') {
      const buildResultArtifactId = text(output.build_result_artifact_id)
      if (buildResultArtifactId) facts.buildResultArtifactId = buildResultArtifactId
    }
    if (observation.actionId === 'device.build_upload_experiment') {
      const experimentRunArtifactId = text(output.experiment_run_artifact_id)
      const deployment = asRecord(output.deployment)
      const remoteBinaryPath = text(output.remote_binary_path) || text(deployment.remote_binary_path)
      const experimentStatus = text(output.experiment_status)
      if (experimentRunArtifactId) facts.experimentRunArtifactId = experimentRunArtifactId
      if (remoteBinaryPath) facts.remoteBinaryPath = remoteBinaryPath
      if (experimentStatus === 'dependency_pull_failed') facts.profileUnavailable = true
    }
    if (observation.actionId === 'device.deploy_artifact') {
      const deploymentArtifactId = text(output.deployment_artifact_id)
      const remoteBinaryPath = text(output.remote_path)
      if (deploymentArtifactId) facts.deploymentArtifactId = deploymentArtifactId
      if (output.deployment_verified === true && remoteBinaryPath && observationStatus(observation) !== 'failed') {
        facts.deploymentVerified = true
        facts.remoteBinaryPath = remoteBinaryPath
        delete facts.deploymentFailureStage
      } else if (output.deployment_verified === false || observationStatus(observation) === 'failed') {
        facts.deploymentVerified = false
        facts.deploymentFailureStage = text(output.stage) || 'device_deployment'
        delete facts.remoteBinaryPath
      }
    }
    if (observation.actionId === 'device.execute_shell') {
      const command = text(observation.input.command)
      const remoteShellRunArtifactId = text(output.remote_shell_run_artifact_id)
      const execution = asRecord(output.execution)
      const hdcExecution = asRecord(execution.execution)
      const outputExcerpt = text(output.output_excerpt) || text(execution.output_excerpt) || text(hdcExecution.stdout) || text(hdcExecution.stderr)
      const returnCode = number(output.return_code) ?? number(execution.return_code) ?? number(hdcExecution.return_code)
      const ranStagedBinary = Boolean(facts.remoteBinaryPath && command.includes(facts.remoteBinaryPath))
      if (ranStagedBinary) {
        facts.experimentExecution = {
          command,
          ...(remoteShellRunArtifactId ? { remoteShellRunArtifactId } : {}),
          ...(returnCode === undefined ? {} : { returnCode }),
          ...(status ? { status } : {}),
          ...(outputExcerpt ? { outputExcerpt: outputExcerpt.slice(0, 1600) } : {}),
        }
      }
    }
  }
  return facts
}

export function deviceActionDependencyIssue(
  action: AgentActionRequest,
  facts: DeviceValidationFacts,
): string | null {
  const actionId = text(action.action_id)
  const input = asRecord(action.input)
  if (actionId === 'device.stage_experiment_source' && !facts.probeArtifactId) {
    return 'device.stage_experiment_source 依赖上一轮真实 device.probe 返回的 probe_artifact_id；本轮尚未取得，已延后。'
  }
  if (actionId === 'device.build_upload_experiment') {
    const stagedSourceArtifactId = text(input.staged_source_artifact_id)
    if (!facts.stagedSourceArtifactId) return 'device.build_upload_experiment 依赖上一轮真实 stage 返回的 staged_source_artifact_id；本轮尚未取得，已延后。'
    if (stagedSourceArtifactId !== facts.stagedSourceArtifactId) return 'device.build_upload_experiment 只能使用本次任务已观察到的 staged_source_artifact_id；模型提供的引用未通过依赖校验，未执行。'
  }
  if (actionId === 'device.execute_shell' && !facts.remoteBinaryPath) {
    return 'device.execute_shell 的本轮目标实验尚未取得真实 remote_binary_path；请先完成构建上传，已延后。'
  }
  return null
}

export function deviceValidationTerminal(facts: DeviceValidationFacts): DeviceValidationTerminal | null {
  if (facts.experimentExecution) {
    const returnCode = facts.experimentExecution.returnCode
    const warning = returnCode !== undefined && returnCode !== 0
    return {
      kind: 'remote_execution_observed',
      detail: warning
        ? `已取得已上传实验的手机执行返回，退出码为 ${returnCode}；应依据 stdout/stderr 判断下一步，不可宣称功能通过。`
        : '已取得已上传实验的手机执行返回；功能等价、目标单元参与和性能仍需依据程序输出分别判断。',
      warning,
    }
  }
  if (facts.noConnectedDevice) {
    return {
      kind: 'environment_blocked',
      detail: '真实设备发现或探测已证明当前没有可用连接，任务停在环境阻塞状态。',
      warning: true,
    }
  }
  if (facts.profileUnavailable) {
    return {
      kind: 'environment_blocked',
      detail: '真实 profile 或其运行库准备结果表明本轮没有可执行构建面，任务停在环境阻塞状态。',
      warning: true,
    }
  }
  return null
}

export function deviceValidationContinuationMessage(facts: DeviceValidationFacts, parseStatus: ParsedAgentResponse['parseStatus']): string {
  const terminal = deviceValidationTerminal(facts)
  return JSON.stringify({
    schema_version: 'device_validation_completion_gate/v1',
    parse_status: parseStatus,
    observed_facts: {
      device_serial: facts.deviceSerial ?? null,
      probe_artifact_id: facts.probeArtifactId ?? null,
      profile_ids: facts.profileIds,
      staged_source_artifact_id: facts.stagedSourceArtifactId ?? null,
      experiment_run_artifact_id: facts.experimentRunArtifactId ?? null,
      remote_binary_path: facts.remoteBinaryPath ?? null,
      experiment_execution: facts.experimentExecution ?? null,
      recent_actions: facts.actionFacts.slice(-8),
    },
    completion_invariant: '真机验证不能因为“等待下一轮”而结束。每个 action 已在返回给你前同步完成；只有取得已上传实验的手机执行返回，或真实设备/profile 环境明确阻塞时，才可返回空 actions 形成终态。',
    instruction: terminal
      ? `已观察到允许终止的事实：${terminal.detail}。请据此形成分层结论。`
      : '当前尚未形成可终止的真机事实。不要重述等待，也不要猜测 ID；请用上述真实结果调用下一项参数完整的已注册 action，或先用最小行动消除当前不确定性。',
  })
}
