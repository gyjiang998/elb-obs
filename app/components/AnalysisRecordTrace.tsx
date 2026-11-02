'use client'

import { Activity, ArrowDownToLine, ArrowRight, Bot, Braces, CheckCircle2, CircleAlert, Clock3, FileInput, FileOutput, FileText, Gauge, LoaderCircle, MessageSquareText, ShieldAlert, Wrench, XCircle } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import { analysisTraceDownloadUrl, type AnalysisExecutionView, type AnalysisModelStep, type AnalysisModelToolCall, type AnalysisVisualEvent } from '../lib/analysis-observability'
import type { AnalysisDeviceValidationRecord, AnalysisInferenceRecord, TraceAnalysisRecord } from '../lib/analysis-history'
import { formatTokenCount } from '../lib/token-budget'
import type { Artifact, JsonRecord } from '../lib/types'

type TranscriptArtifact = Artifact & {
  context_pack?: JsonRecord
  prompt?: JsonRecord
  model_response?: JsonRecord
  outcome?: JsonRecord
  timeline?: JsonRecord[]
  target_platform_profile?: JsonRecord
  model_usage?: JsonRecord
  token_budget?: JsonRecord
}

type TranscriptLoad =
  | { state: 'loading' }
  | { state: 'ready'; artifact: TranscriptArtifact }
  | { state: 'failed'; message: string }

type DeviceArtifactLoad =
  | { state: 'loading' }
  | { state: 'ready'; artifact: Artifact }
  | { state: 'failed'; message: string }

type ExecutionTraceLoad =
  | { state: 'loading' }
  | { state: 'ready'; trace: AnalysisExecutionView }
  | { state: 'failed'; message: string }

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function text(value: unknown, fallback = '—'): string {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

function formattedTime(value: string | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(parsed)
}

function statusLabel(status: AnalysisInferenceRecord['status'] | string): string {
  return ({ running: '推理中', succeeded: '已完成', failed: '失败', blocked: '未调用' })[status] ?? status
}

function deviceValidationStatusLabel(status: AnalysisDeviceValidationRecord['status']): string {
  return ({ planned: '等待用户确认', evidence_required: '仍需证据', passed: '功能验证通过', unsupported: '当前设备不支持该受控图', failed: '功能验证未通过' })[status] ?? status
}

function deviceUnitLabel(value: string): string {
  return ({ npu: 'NPU', gpu: 'GPU', dsp: 'DSP', media: '媒体硬件编解码' })[value] ?? value
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function targetProfileLabel(value: unknown): string {
  const profile = asRecord(value)
  const chipset = asRecord(profile.chipset)
  const operatingSystem = asRecord(profile.operating_system)
  const chipsetLabel = text(chipset.model_label, text(chipset.family_label, '目标芯片待确认'))
  const osLabel = text(operatingSystem.version_label, text(operatingSystem.family_label, '目标系统待确认'))
  return `${chipsetLabel} · ${osLabel}`
}

function InferenceFallback({ inference }: { inference: AnalysisInferenceRecord }) {
  return <details className="analysis-record-fallback">
    <summary>历史摘要{inference.transcriptArtifactId ? '（完整工件暂不可读取）' : '（此旧记录尚未保存完整 transcript）'}</summary>
    <dl>
      <div><dt>Context</dt><dd><code>{inference.context.contextArtifactId}</code></dd></div>
      <div><dt>SHA</dt><dd><code>{inference.context.contextSha256}</code></dd></div>
      <div><dt>源码</dt><dd>{inference.context.source.path || '未绑定源码'}{inference.context.source.lineStart ? `:${inference.context.source.lineStart}` : ''}</dd></div>
      <div><dt>检索</dt><dd>{inference.context.retrieval.method} · {inference.context.evidence.length} 条证据</dd></div>
      <div><dt>Skill 配置</dt><dd>{inference.context.skillProfile?.profileId ?? '旧记录未保存'}</dd></div>
    </dl>
    {inference.summary && <p>{inference.summary}</p>}
    {inference.error && <p className="analysis-record-error">{inference.error}</p>}
  </details>
}

function TranscriptDetails({ artifact, inference }: { artifact: TranscriptArtifact; inference: AnalysisInferenceRecord }) {
  const context = asRecord(artifact.context_pack)
  const prompt = asRecord(artifact.prompt)
  const response = asRecord(artifact.model_response)
  const outcome = asRecord(artifact.outcome)
  const tokenUsage = asRecord(artifact.model_usage)
  const tokenBudget = asRecord(artifact.token_budget)
  const timeline = Array.isArray(artifact.timeline) ? artifact.timeline.map(asRecord) : []
  const profile = artifact.target_platform_profile ?? context.target_platform_profile ?? inference.context.targetPlatformProfile
  const skillProfile = asRecord(context.skill_profile)
  const inputTokens = typeof tokenUsage.inputTokens === 'number' ? tokenUsage.inputTokens : inference.tokenUsage?.inputTokens
  const outputTokens = typeof tokenUsage.outputTokens === 'number' ? tokenUsage.outputTokens : inference.tokenUsage?.outputTokens
  const cachedInputTokens = typeof tokenUsage.cachedInputTokens === 'number' ? tokenUsage.cachedInputTokens : inference.tokenUsage?.cachedInputTokens
  const cacheHitRate = typeof tokenUsage.cacheHitRate === 'number'
    ? tokenUsage.cacheHitRate
    : typeof cachedInputTokens === 'number' && typeof inputTokens === 'number' && inputTokens > 0
      ? cachedInputTokens / inputTokens
      : null
  return <div className="analysis-transcript-details">
    <div className="analysis-transcript-facts">
      <div><span>目标画像</span><strong>{targetProfileLabel(profile)}</strong><small>用户选择，非设备探测</small></div>
      <div><span>Context SHA</span><strong><code>{text(artifact.context_sha256, inference.context.contextSha256)}</code></strong></div>
      <div><span>结果工件</span><strong><code>{text(artifact.result_artifact_id, inference.resultArtifactId ?? '未生成')}</code></strong></div>
      <div><span>模型</span><strong>{text(asRecord(artifact.model).name, inference.model.name)}</strong><small>{text(asRecord(artifact.model).provider, inference.model.provider)} · Skill {text(skillProfile.profile_id, inference.context.skillProfile?.profileId ?? '旧记录未保存')}</small></div>
      <div><span>Token</span><strong>Input {formatTokenCount(inputTokens)} · Output {formatTokenCount(outputTokens)}</strong><small>{typeof cachedInputTokens === 'number' ? `缓存 ${formatTokenCount(cachedInputTokens)} · 命中 ${(Math.min(1, cacheHitRate ?? 0) * 100).toFixed(1)}%` : '模型服务未返回缓存用量'}；上下文 {formatTokenCount(typeof tokenBudget.inputTokens === 'number' ? tokenBudget.inputTokens : inference.tokenBudget?.inputTokens)} / {formatTokenCount(typeof tokenBudget.contextWindowTokens === 'number' ? tokenBudget.contextWindowTokens : inference.tokenBudget?.contextWindowTokens)}</small></div>
    </div>

    <details className="analysis-transcript-disclosure" open>
      <summary>完整审计上下文（不发送给模型）</summary>
      <pre>{safeJson(context)}</pre>
    </details>
    <details className="analysis-transcript-disclosure">
      <summary>发送给模型的提示词（语义上下文）</summary>
      <div className="analysis-prompt-block"><strong>系统约束</strong><pre>{text(prompt.system, '未记录')}</pre></div>
      <div className="analysis-prompt-block"><strong>用户上下文</strong><pre>{text(prompt.user, '未记录')}</pre></div>
    </details>
    <details className="analysis-transcript-disclosure">
      <summary>模型原始回答</summary>
      <pre>{text(response.raw_output, '模型未返回可展示文本。')}</pre>
    </details>
    <details className="analysis-transcript-disclosure" open>
      <summary>契约校验与结果状态</summary>
      <pre>{safeJson(outcome)}</pre>
    </details>
    <details className="analysis-transcript-disclosure">
      <summary>本轮时间线（{timeline.length} 步）</summary>
      {timeline.length === 0 ? <p>该 transcript 未携带阶段时间线。</p> : <ol className="analysis-transcript-timeline">
        {timeline.map((entry, index) => <li className={`analysis-transcript-timeline-row ${text(entry.status, 'succeeded')}`} key={`${text(entry.timestamp)}-${index}`}>
          <div><Clock3 size={13} /><span>{formattedTime(text(entry.timestamp, ''))}</span></div>
          <strong>{text(entry.title)}</strong><span>{text(entry.detail)}</span><small>{text(entry.stage)} · {text(entry.status)}</small>
        </li>)}
      </ol>}
    </details>
  </div>
}

function DeviceValidationDetails({
  validation,
  plan,
  run,
}: {
  validation: AnalysisDeviceValidationRecord
  plan?: DeviceArtifactLoad
  run?: DeviceArtifactLoad
}) {
  const planArtifact = plan?.state === 'ready' ? plan.artifact : null
  const runArtifact = run?.state === 'ready' ? run.artifact : null
  const recipe = asRecord(planArtifact?.recipe_contract)
  const execution = asRecord(runArtifact?.execution)
  const evidence = asRecord(runArtifact?.evidence_calibration)
  return <li className={`analysis-device-validation ${validation.status}`}>
    <div className="analysis-device-validation-meta"><span>{deviceUnitLabel(validation.requestedUnit)}</span><strong>{deviceValidationStatusLabel(validation.status)}</strong><small>{formattedTime(validation.updatedAt)}</small></div>
    <div className="analysis-device-validation-body">
      <p>{validation.summary}</p>
      <dl>
        <div><dt>设备</dt><dd><code>{validation.deviceSerial}</code></dd></div>
        <div><dt>Probe</dt><dd><code>{validation.probeArtifactId}</code></dd></div>
        <div><dt>方案</dt><dd><code>{validation.planArtifactId}</code></dd></div>
        {validation.runArtifactId && <div><dt>运行</dt><dd><code>{validation.runArtifactId}</code></dd></div>}
      </dl>
      {plan?.state === 'loading' && <div className="analysis-transcript-loading"><LoaderCircle size={14} className="spin" />正在读取真机验证方案…</div>}
      {plan?.state === 'failed' && <div className="analysis-transcript-unavailable"><FileText size={14} /><span>{plan.message}</span></div>}
      {planArtifact && <div className="analysis-device-validation-contract"><strong>配方范围</strong><span>{text(recipe.execution_scope_zh, text(planArtifact.planning_reason_zh, '方案未携带可展示范围。'))}</span>{Array.isArray(recipe.does_not_prove_zh) && <small>不证明：{(recipe.does_not_prove_zh as unknown[]).map((item) => text(item, '')).filter(Boolean).join('；')}</small>}</div>}
      {run?.state === 'loading' && <div className="analysis-transcript-loading"><LoaderCircle size={14} className="spin" />正在读取真机运行工件…</div>}
      {run?.state === 'failed' && <div className="analysis-transcript-unavailable"><FileText size={14} /><span>{run.message}</span></div>}
      {runArtifact && <>
        <div className="analysis-device-validation-contract"><strong>运行结果</strong><span>{text(runArtifact.summary_zh, '无运行摘要。')}</span><small>退出码 {typeof execution.return_code === 'number' ? String(execution.return_code) : '—'} · 时长 {typeof execution.duration_ms === 'number' ? `${execution.duration_ms.toFixed(1)} ms` : '—'}</small></div>
        <details className="analysis-transcript-disclosure"><summary>受控运行输出</summary><pre>{text(execution.stdout, '无标准输出。')}{text(execution.stderr, '') ? `\n\n[stderr]\n${text(execution.stderr, '')}` : ''}</pre></details>
        <p className="analysis-device-validation-boundary">{text(evidence.recommendation_effect, '此结果不会自动改变策略结论。')}</p>
      </>}
    </div>
  </li>
}

type ExecutionTraceTab = 'model' | 'tools' | 'timeline' | 'raw'

function elapsed(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (value < 1000) return `${Math.round(value)} ms`
  if (value < 60_000) return `${(value / 1000).toFixed(1)} s`
  if (value >= 86_400_000) {
    const days = Math.floor(value / 86_400_000)
    const hours = Math.floor((value % 86_400_000) / 3_600_000)
    return `${days} 天${hours > 0 ? ` ${hours} 小时` : ''}`
  }
  if (value >= 3_600_000) {
    const hours = Math.floor(value / 3_600_000)
    const minutes = Math.floor((value % 3_600_000) / 60_000)
    return `${hours} 小时${minutes > 0 ? ` ${minutes} 分` : ''}`
  }
  const minutes = Math.floor(value / 60_000)
  const seconds = Math.round((value % 60_000) / 1000)
  return `${minutes} min ${seconds} s`
}

function between(start: string, end: string): string {
  const startMs = Date.parse(start)
  const endMs = Date.parse(end)
  return Number.isFinite(startMs) && Number.isFinite(endMs) ? elapsed(Math.max(0, endMs - startMs)) : '进行中'
}

function modelStepName(step: AnalysisModelStep): string {
  const request = asRecord(step.model_request)
  return text(request.modelAlias, text(request.model, 'Agent 模型'))
}

function toolDisplayName(value: string): string {
  const platformName = value.match(/__(reasoning|device|build|workspace|knowledge|application_flow|code_graph)[^_]*_(.*)$/)?.[2]
  return (platformName || value)
    .replace(/^mcp__[^_]+__/, '')
    .replace(/_-?[a-f0-9]{6,}$/i, '')
    .replaceAll('_', '.')
}

function operationLabel(value: string): string {
  const known: Record<string, string> = {
    'analysis.record.start': '创建分析记录',
    'analysis.hotspot.update': '绑定热点代码',
    'analysis.inference.record': '登记推理轮次',
    'analysis.inference.update': '更新推理结果',
    'analysis.device_validation.record': '登记真机验证',
    'run.created': '创建 Agent Run',
    'page.session_bound': '绑定页面 Session',
    'page.session_adopted': '恢复历史 Session',
    'terminal.command_submitted': '提交页面命令',
    'ui.action_requested': 'Agent 请求页面工具',
    'ui.action_result': '页面工具返回 Agent',
    'tool.call': '调用工具',
    'tool.result': '工具返回',
    'engine.session_ready': '原生 Agent Session 就绪',
    'terminal.session_attached': 'PTY 已连接',
    'terminal.session_exited': 'PTY 已退出',
    'run.state_changed': 'Run 状态变化',
  }
  return known[value] ?? value
}

function eventTone(status: string): 'running' | 'succeeded' | 'failed' | 'observed' {
  if (['failed', 'error', 'cancelled'].includes(status)) return 'failed'
  if (['running', 'started', 'waiting_for_ui', 'waiting_for_tool', 'waiting_for_device', 'compacting'].includes(status)) return 'running'
  if (['succeeded', 'completed', 'ready'].includes(status)) return 'succeeded'
  return 'observed'
}

function ModelStepView({ step, index, defaultInput }: { step: AnalysisModelStep; index: number; defaultInput: string }) {
  const input = step.input || (index === 0 ? defaultInput : '')
  const usage = asRecord(step.usage)
  const latency = asRecord(step.latency)
  return <li className={`execution-model-step ${eventTone(step.status)}`}>
    <div className="execution-model-rail"><span>{index + 1}</span></div>
    <div className="execution-model-main">
      <header>
        <div><Bot size={16} /><strong>模型推理 Step {index + 1}</strong><span>{modelStepName(step)}</span></div>
        <div><em>{step.status === 'running' ? '进行中' : step.status === 'failed' ? '失败' : '完成'}</em><time>{between(step.started_at, step.ended_at)}</time></div>
      </header>
      <div className="execution-model-metrics">
        <span>Input {formatTokenCount(typeof usage.input_tokens === 'number' ? usage.input_tokens : undefined)}</span>
        <span>Output {formatTokenCount(typeof usage.output_tokens === 'number' ? usage.output_tokens : undefined)}</span>
        <span>缓存 {formatTokenCount(typeof usage.cached_input_tokens === 'number' ? usage.cached_input_tokens : undefined)}</span>
        <span>首 Token {elapsed(typeof latency.first_token_ms === 'number' ? latency.first_token_ms : null)}</span>
      </div>
      <div className="execution-model-sections">
        <details open={index === 0}>
          <summary><FileInput size={14} /><span>本步输入</span><small>{input ? `${input.length.toLocaleString()} 字符` : '工具结果续接'}</small></summary>
          <pre>{input || '本步由上一工具结果继续，原生日志未重复写入完整消息数组。'}</pre>
        </details>
        {step.reasoning && <details>
          <summary><Activity size={14} /><span>推理过程</span><small>{step.reasoning.length.toLocaleString()} 字符</small></summary>
          <div className="execution-reasoning-text">{step.reasoning}</div>
        </details>}
        {step.output && <details open>
          <summary><FileOutput size={14} /><span>模型回答</span><small>{step.output.length.toLocaleString()} 字符</small></summary>
          <div className="execution-answer-text">{step.output}</div>
        </details>}
      </div>
      {step.tools.length > 0 && <div className="execution-step-tools">
        <strong><Wrench size={14} />本步工具调用</strong>
        {step.tools.map((tool) => <ToolCallView tool={tool} key={tool.tool_call_id || `${tool.name}:${tool.started_at}`} compact />)}
      </div>}
    </div>
  </li>
}

function ToolCallView({ tool, compact = false }: { tool: AnalysisModelToolCall; compact?: boolean }) {
  const failed = tool.status === 'failed'
  return <details className={`execution-tool-call ${failed ? 'failed' : tool.status === 'running' ? 'running' : 'succeeded'} ${compact ? 'compact' : ''}`}>
    <summary>
      {failed ? <XCircle size={14} /> : tool.status === 'running' ? <LoaderCircle size={14} className="spin" /> : <CheckCircle2 size={14} />}
      <code>{toolDisplayName(tool.name)}</code>
      <span>{failed ? '失败' : tool.status === 'running' ? '执行中' : '成功'}</span>
      <time>{between(tool.started_at, tool.ended_at)}</time>
    </summary>
    <div className="execution-tool-detail">
      <div><strong>调用参数</strong><pre>{safeJson(tool.arguments)}</pre></div>
      <div><strong>真实返回</strong><pre>{tool.result === null || tool.result === undefined ? '尚未返回' : safeJson(tool.result)}</pre></div>
    </div>
  </details>
}

function EventRow({ event }: { event: AnalysisVisualEvent }) {
  const tone = eventTone(event.status)
  const message = text(event.payload.message, text(asRecord(event.payload.error).message, ''))
  return <li className={`execution-event-row ${tone}`}>
    <div className="execution-event-dot" />
    <div className="execution-event-time">{formattedTime(event.occurred_at)}</div>
    <div className="execution-event-source">{event.source === 'browser' ? '页面' : 'Agent Run'}</div>
    <div className="execution-event-operation"><strong>{operationLabel(event.operation)}</strong><code>{event.operation}</code>{message && <small>{message}</small>}</div>
    <span className="execution-event-status">{event.status}</span>
    <details><summary aria-label="查看事件详情"><Braces size={14} /></summary><pre>{safeJson(event.payload)}</pre></details>
  </li>
}

function ExecutionTraceVisualization({ trace }: { trace: AnalysisExecutionView }) {
  const [tab, setTab] = useState<ExecutionTraceTab>('model')
  const tools = trace.model_transcript.steps.flatMap((step) => step.tools)
  const noisyOperations = new Set(['run.task_context_updated', 'terminal.session_attached', 'engine.session_ready'])
  const timeline = trace.events.filter((event) => !noisyOperations.has(event.operation))
  const cacheRate = trace.summary.cache_hit_rate
  return <>
    <div className="execution-coverage" aria-label="执行记录覆盖率">
      <span className={trace.coverage.workspace_snapshot ? 'available' : 'missing'}>{trace.coverage.workspace_snapshot ? <CheckCircle2 size={13} /> : <CircleAlert size={13} />}工作台快照</span>
      <span className={trace.coverage.agent_run ? 'available' : 'missing'}>{trace.coverage.agent_run ? <CheckCircle2 size={13} /> : <CircleAlert size={13} />}Agent Run</span>
      <span className={trace.coverage.native_model_transcript ? 'available' : 'missing'}>{trace.coverage.native_model_transcript ? <CheckCircle2 size={13} /> : <CircleAlert size={13} />}模型原生记录</span>
      {!trace.coverage.native_model_transcript && <small>当前 Agent Adapter 未提供可解析的原生模型日志；页面与 Run 事件仍可审计。</small>}
    </div>
    <div className="execution-metric-band">
      <div><Clock3 size={15} /><span>模型活跃时长</span><strong>{elapsed(trace.summary.duration_ms)}</strong><small>记录跨度 {elapsed(trace.summary.record_span_ms)}</small></div>
      <div><Bot size={15} /><span>模型 Step</span><strong>{trace.summary.model_step_count}</strong></div>
      <div><Wrench size={15} /><span>工具调用</span><strong>{trace.summary.tool_success_count} / {trace.summary.tool_call_count}</strong><small>{trace.summary.tool_failure_count > 0 ? `${trace.summary.tool_failure_count} 次失败` : '全部成功'}</small></div>
      <div><Gauge size={15} /><span>Token</span><strong>{formatTokenCount(trace.summary.input_tokens)} / {formatTokenCount(trace.summary.output_tokens)}</strong><small>缓存命中 {cacheRate === null ? '未返回' : `${(cacheRate * 100).toFixed(1)}%`}</small></div>
    </div>
    <div className="execution-trace-tabs" role="tablist" aria-label="执行 Trace 视图">
      <button type="button" className={tab === 'model' ? 'active' : ''} onClick={() => setTab('model')}><MessageSquareText size={14} />模型推理 <span>{trace.model_transcript.steps.length}</span></button>
      <button type="button" className={tab === 'tools' ? 'active' : ''} onClick={() => setTab('tools')}><Wrench size={14} />工具调用 <span>{tools.length}</span></button>
      <button type="button" className={tab === 'timeline' ? 'active' : ''} onClick={() => setTab('timeline')}><Activity size={14} />完整时间线 <span>{timeline.length}</span></button>
      <button type="button" className={tab === 'raw' ? 'active' : ''} onClick={() => setTab('raw')}><Braces size={14} />原始数据</button>
    </div>
    {tab === 'model' && <div className="execution-tab-panel">
      {trace.model_transcript.system_prompt && <details className="execution-system-prompt"><summary>查看本 Session 系统提示词</summary><pre>{trace.model_transcript.system_prompt}</pre></details>}
      {trace.model_transcript.steps.length === 0
        ? <div className="event-empty">没有可解析的模型原生 Step。可切换“完整时间线”查看页面和 Agent Run 事件。</div>
        : <ol className="execution-model-list">{trace.model_transcript.steps.map((step, index) => <ModelStepView key={step.step_id || `${step.agent_id}:${step.step}`} step={step} index={index} defaultInput={trace.model_transcript.turn_prompt} />)}</ol>}
    </div>}
    {tab === 'tools' && <div className="execution-tab-panel execution-tool-list">
      {tools.length === 0 ? <div className="event-empty">本次原生模型记录中没有工具调用。</div> : tools.map((tool) => <ToolCallView tool={tool} key={tool.tool_call_id || `${tool.name}:${tool.started_at}`} />)}
    </div>}
    {tab === 'timeline' && <div className="execution-tab-panel">
      <div className="execution-timeline-note">默认折叠重复的上下文同步与 PTY 挂载事件，共隐藏 {trace.events.length - timeline.length} 条；下载 JSONL 可查看底层原始记录。</div>
      <ol className="execution-event-list">{timeline.map((event) => <EventRow event={event} key={`${event.source}:${event.event_id}`} />)}</ol>
    </div>}
    {tab === 'raw' && <div className="execution-tab-panel execution-raw-sources">
      <p>原始数据保留用于机器复核；日常排查优先使用前三个可视化视图。</p>
      <details><summary>覆盖率与数据源</summary><pre>{safeJson({ coverage: trace.coverage, agent_session: trace.agent_session, raw_sources: trace.raw_sources })}</pre></details>
      <details><summary>合并事件（{trace.events.length} 条）</summary><pre>{safeJson(trace.events)}</pre></details>
    </div>}
  </>
}

export function AnalysisRecordTrace({ analysis, onLoadToWorkbench }: { analysis: TraceAnalysisRecord; onLoadToWorkbench: () => void }) {
  const [transcripts, setTranscripts] = useState<Record<string, TranscriptLoad>>({})
  const [deviceArtifacts, setDeviceArtifacts] = useState<Record<string, DeviceArtifactLoad>>({})
  const [executionTrace, setExecutionTrace] = useState<ExecutionTraceLoad>({ state: 'loading' })
  const isManualCodeRecord = analysis.trace.inputKind === 'manual_code'
  const transcriptIds = analysis.inferences.map((inference) => inference.transcriptArtifactId).filter((value): value is string => Boolean(value))
  const transcriptKey = transcriptIds.join('|')
  const deviceValidations = useMemo(
    () => [...(analysis.deviceValidations ?? [])].sort((left, right) => right.updatedAt.localeCompare(left.updatedAt)),
    [analysis.deviceValidations],
  )
  const deviceArtifactKey = deviceValidations.map((item) => `${item.planArtifactId}:${item.runArtifactId ?? ''}`).join('|')

  useEffect(() => {
    let cancelled = false
    setExecutionTrace({ state: 'loading' })
    void api<AnalysisExecutionView>(`/v1/projects/local-default/analysis-executions/${encodeURIComponent(analysis.analysisId)}`)
      .then((trace) => {
        if (!cancelled) setExecutionTrace({ state: 'ready', trace })
      })
      .catch((error) => {
        if (!cancelled) setExecutionTrace({ state: 'failed', message: error instanceof Error ? error.message : '无法读取执行 Trace。' })
      })
    return () => { cancelled = true }
  }, [analysis.analysisId, analysis.updatedAt])

  useEffect(() => {
    let cancelled = false
    const ids = transcriptKey ? Array.from(new Set(transcriptKey.split('|'))) : []
    if (ids.length === 0) {
      setTranscripts({})
      return () => { cancelled = true }
    }
    setTranscripts(Object.fromEntries(ids.map((id) => [id, { state: 'loading' } as TranscriptLoad])))
    void Promise.all(ids.map(async (id) => {
      try {
        const response = await api<{ artifact: TranscriptArtifact }>(`/v1/projects/local-default/reasoning-transcripts/${encodeURIComponent(id)}`)
        return [id, { state: 'ready', artifact: response.artifact } as TranscriptLoad] as const
      } catch (error) {
        return [id, { state: 'failed', message: error instanceof Error ? error.message : '无法读取 transcript 工件。' } as TranscriptLoad] as const
      }
    })).then((entries) => {
      if (!cancelled) setTranscripts(Object.fromEntries(entries))
    })
    return () => { cancelled = true }
  // A record can be updated after an inference completes; the artifact IDs are
  // the immutable identity that decides whether a reload is necessary.
  }, [analysis.analysisId, transcriptKey])

  useEffect(() => {
    let cancelled = false
    const requests = deviceValidations.flatMap((validation) => [
      { key: `plan:${validation.planArtifactId}`, endpoint: `/v1/projects/local-default/device-validation-plans/${encodeURIComponent(validation.planArtifactId)}` },
      ...(validation.runArtifactId ? [{ key: `run:${validation.runArtifactId}`, endpoint: `/v1/projects/local-default/device-validation-runs/${encodeURIComponent(validation.runArtifactId)}` }] : []),
    ])
    if (requests.length === 0) {
      setDeviceArtifacts({})
      return () => { cancelled = true }
    }
    setDeviceArtifacts(Object.fromEntries(requests.map((item) => [item.key, { state: 'loading' } as DeviceArtifactLoad])))
    void Promise.all(requests.map(async (item) => {
      try {
        const response = await api<{ artifact: Artifact }>(item.endpoint)
        return [item.key, { state: 'ready', artifact: response.artifact } as DeviceArtifactLoad] as const
      } catch (error) {
        return [item.key, { state: 'failed', message: error instanceof Error ? error.message : '无法读取真机验证工件。' } as DeviceArtifactLoad] as const
      }
    })).then((entries) => {
      if (!cancelled) setDeviceArtifacts(Object.fromEntries(entries))
    })
    return () => { cancelled = true }
  }, [analysis.analysisId, deviceArtifactKey, deviceValidations])

  const orderedInferences = [...analysis.inferences].sort((left, right) => left.startedAt.localeCompare(right.startedAt))
  const trace = executionTrace.state === 'ready' ? executionTrace.trace : null
  const workspaceSnapshot = asRecord(asRecord(trace?.workspace).snapshot)
  const profile = orderedInferences.find((item) => item.context.targetPlatformProfile)?.context.targetPlatformProfile
    ?? workspaceSnapshot.target_platform_profile
  const skillProfile = [...orderedInferences].reverse().find((item) => item.context.skillProfile)?.context.skillProfile
  const workspaceSkillProfileId = text(workspaceSnapshot.selected_skill_profile_id, '')

  return <div className="workspace-scroll analysis-record-view">
    <section className="work-section analysis-record-heading">
      <div className="section-heading"><div><span className="eyebrow">优化分析记录</span><h2>完整推理审计</h2></div><button className="quiet-button" type="button" onClick={onLoadToWorkbench}><ArrowRight size={15} />装载此条记录至推理工作台</button></div>
      <p>一条记录对应一次真实 Trace 选择或一次手动代码输入；其中每一轮策略推理都保留不可变 context、提示词、模型原始回答、契约处理与存档时间线。模型 API 密钥不会进入此视图或后端 artifact。</p>
      <div className="analysis-record-overview table-shell">
        <div><span>{isManualCodeRecord ? '代码输入' : 'Trace'}</span><strong>{analysis.trace.label}</strong><small><code>{analysis.trace.artifactId}</code></small></div>
        <div><span>热点</span><strong>{analysis.hotspot?.eventName ?? '尚未选择'}</strong><small>{analysis.hotspot?.sourceKind === 'pasted_code' ? '用户粘贴代码' : analysis.hotspot?.sourceKind === 'trace_source' ? '真实 Trace 源码解析' : '待解析'}</small></div>
        <div><span>目标画像</span><strong>{profile ? targetProfileLabel(profile) : '尚未准备策略上下文'}</strong><small>用户选择，非设备探测 · Skill {skillProfile?.profileId || workspaceSkillProfileId || '旧记录未保存'}</small></div>
        <div><span>推理轮数</span><strong>{orderedInferences.length} 次</strong><small>更新于 {formattedTime(analysis.updatedAt)}</small></div>
      </div>
    </section>

    <section className="work-section analysis-record-execution-trace">
      <div className="section-heading">
        <div><span className="eyebrow">执行可观测性</span><h3>{trace ? `${trace.summary.model_step_count} 次模型推理 · ${trace.summary.tool_call_count} 次工具调用` : '分析执行 Trace'}</h3></div>
        {trace?.coverage.browser_events && <a className="quiet-button" href={analysisTraceDownloadUrl(analysis.analysisId)} download><ArrowDownToLine size={14} />下载页面 JSONL</a>}
      </div>
      <p>合并展示页面操作、Agent Run、模型每步输入与输出、推理文本以及工具调用真实结果。敏感配置会在后端存档前脱敏。</p>
      {executionTrace.state === 'loading' && <div className="analysis-transcript-loading"><LoaderCircle size={14} className="spin" />正在读取执行 Trace…</div>}
      {executionTrace.state === 'failed' && <div className="analysis-transcript-unavailable"><FileText size={14} /><span>{executionTrace.message}</span></div>}
      {trace && <ExecutionTraceVisualization trace={trace} />}
    </section>

    <section className="work-section analysis-record-rounds">
      <div className="section-heading"><div><span className="eyebrow">推理轮次</span><h3>{orderedInferences.length > 0 ? `${orderedInferences.length} 次可复查推理` : '尚未执行模型推理'}</h3></div></div>
      {orderedInferences.length === 0 ? <div className="event-empty">此{isManualCodeRecord ? '代码输入' : ' Trace'}已进入记录，但尚未准备或执行策略推理。</div> : <ol className="analysis-round-list">
        {orderedInferences.map((inference, index) => {
          const load = inference.transcriptArtifactId ? transcripts[inference.transcriptArtifactId] : undefined
          return <li className={`analysis-round ${inference.status}`} key={inference.inferenceId}>
            <div className="analysis-round-meta"><span>第 {index + 1} 轮</span><strong>{inference.strategyLabel}</strong><small>{formattedTime(inference.startedAt)}</small><em>{statusLabel(inference.status)}</em></div>
            <div className="analysis-round-body">
              <div className="analysis-round-summary"><div>{inference.status === 'succeeded' ? <CheckCircle2 size={16} /> : inference.status === 'failed' ? <ShieldAlert size={16} /> : <CircleAlert size={16} />}<strong>{inference.summary ?? (inference.status === 'running' ? '正在执行模型推理。' : '尚无结果摘要。')}</strong></div><span>{inference.context.targetPlatformProfile ? targetProfileLabel(inference.context.targetPlatformProfile) : '旧记录未包含目标设备与系统画像'} · Skill {inference.context.skillProfile?.profileId ?? '旧记录未保存'}</span></div>
              {load?.state === 'loading' && <div className="analysis-transcript-loading"><LoaderCircle size={15} className="spin" />正在读取不可变 transcript 工件…</div>}
              {load?.state === 'failed' && <div className="analysis-transcript-unavailable"><FileText size={15} /><span>{load.message}</span></div>}
              {load?.state === 'ready' ? <TranscriptDetails artifact={load.artifact} inference={inference} /> : <InferenceFallback inference={inference} />}
            </div>
          </li>
        })}
      </ol>}
    </section>

    <section className="work-section analysis-record-device-validations">
      <div className="section-heading"><div><span className="eyebrow">真机验证</span><h3>{deviceValidations.length > 0 ? `${deviceValidations.length} 条设备证据记录` : '尚未进行真机验证'}</h3></div></div>
      {deviceValidations.length === 0
        ? <div className="event-empty">本轮尚未生成真机验证方案。设备能力观察、功能配方与性能收益会在这里分层保存，不会覆盖策略推理结论。</div>
        : <ol className="analysis-device-validation-list">{deviceValidations.map((validation) => <DeviceValidationDetails
          key={validation.validationId}
          validation={validation}
          plan={deviceArtifacts[`plan:${validation.planArtifactId}`]}
          run={validation.runArtifactId ? deviceArtifacts[`run:${validation.runArtifactId}`] : undefined}
        />)}</ol>}
    </section>
  </div>
}
