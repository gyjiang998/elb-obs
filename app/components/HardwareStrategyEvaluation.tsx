'use client'

import { Check, CircleStop, FlaskConical, LoaderCircle, Play, RotateCcw, Save, ShieldAlert } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../lib/api'
import { BrowserModelConfig, createDefaultBrowserModelConfig, modelConfigChangedEvent, readBrowserModelConfig } from '../lib/model-config'
import { requestModelChatCompletion, type ModelTokenUsage } from '../lib/model-client'
import { buildStrategyPromptPlan, parseModelJson } from '../lib/reasoning-prompt'
import type { TargetPlatformProfile } from '../lib/target-platform-profile'
import type { JsonRecord } from '../lib/types'

type EvaluationCase = {
  case_id: string
  title_zh: string
  category: string
  partition: string
  family_id: string
  variation_axis: string
  public_input?: JsonRecord
}

type EvaluationSuite = {
  suite_id: string
  suite_version: string
  purpose_zh: string
  case_count: number
  categories: Record<string, number>
  partitions: Record<string, number>
  families: Record<string, number>
  evaluation_policy?: JsonRecord
  strategy_contract: { skill_id?: string; skill_version?: string }
  cases: EvaluationCase[]
}

type CaseRunStatus = 'idle' | 'running' | 'completed' | 'failed'
type CaseRun = {
  status: CaseRunStatus
  contextSha256?: string
  modelEvidenceCount?: number
  result?: JsonRecord
  score?: JsonRecord
  rawOutput?: string
  tokenUsage?: ModelTokenUsage
  error?: string
}

type PreparedCaseResponse = {
  suite_id: string
  suite_version: string
  case: EvaluationCase
  context: JsonRecord
}

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function stringValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value ? value : fallback
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function scoreValue(run?: CaseRun): number | null {
  return numberValue(record(run?.score).score)
}

function statusLabel(status: CaseRunStatus): string {
  return ({ idle: '未运行', running: '推理中', completed: '已评分', failed: '失败' })[status]
}

function categoryLabel(category: string): string {
  const labels: Record<string, string> = {
    npu_positive: 'NPU 正例',
    gpu_positive: 'GPU 正例',
    dsp_positive: 'DSP 正例',
    media_positive: '媒体正例',
    cost_boundary: '成本边界',
    negative_ipc: 'IPC 反例',
    negative_lock: '锁反例',
    negative_io: 'I/O 反例',
    negative_dependency: '依赖反例',
    negative_branchy: '分支反例',
    cpu_parallel_positive: 'CPU 并行正例',
    cpu_parallel_boundary: 'CPU 并行边界',
  }
  return labels[category] ?? category
}

function partitionLabel(partition: string): string {
  return ({
    smoke: 'Smoke 回归',
    development: '开发集',
    validation: '验证集',
    holdout: '冻结盲测',
    all: '全部切分',
  })[partition] ?? partition
}

function percent(value: unknown): string {
  const numeric = numberValue(value)
  return numeric === null ? '—' : `${(numeric * 100).toFixed(1)}%`
}

export function HardwareStrategyEvaluation({ targetPlatformProfile }: { targetPlatformProfile: TargetPlatformProfile }) {
  const [suite, setSuite] = useState<EvaluationSuite | null>(null)
  const [selectedCaseId, setSelectedCaseId] = useState('')
  const [runs, setRuns] = useState<Record<string, CaseRun>>({})
  const [modelConfig, setModelConfig] = useState<BrowserModelConfig>(() => createDefaultBrowserModelConfig())
  const [catalogError, setCatalogError] = useState('')
  const [selectedPartition, setSelectedPartition] = useState('smoke')
  const [reportState, setReportState] = useState<{ artifactId?: string; message?: string; error?: string; report?: JsonRecord }>({})
  const cancellationRef = useRef(false)

  const loadSuite = useCallback(async () => {
    try {
      const response = await api<{ suite: EvaluationSuite }>('/v1/skill-evaluations/hardware-kb')
      setSuite(response.suite)
      setSelectedCaseId((current) => current || response.suite.cases[0]?.case_id || '')
      setCatalogError('')
    } catch (error) {
      setCatalogError(error instanceof Error ? error.message : '无法读取硬件策略评测集。')
    }
  }, [])

  useEffect(() => { void loadSuite() }, [loadSuite])

  useEffect(() => {
    const onConfigChange = (event: Event) => {
      const detail = (event as CustomEvent<BrowserModelConfig>).detail
      setModelConfig(detail && typeof detail === 'object' ? detail : readBrowserModelConfig())
    }
    setModelConfig(readBrowserModelConfig())
    window.addEventListener(modelConfigChangedEvent, onConfigChange)
    return () => window.removeEventListener(modelConfigChangedEvent, onConfigChange)
  }, [])

  const selectedCase = suite?.cases.find((item) => item.case_id === selectedCaseId) ?? null
  const visibleCases = useMemo(() => (
    suite?.cases.filter((item) => selectedPartition === 'all' || item.partition === selectedPartition) ?? []
  ), [selectedPartition, suite])
  const completedRuns = useMemo(
    () => Object.values(runs).filter((item) => item.status === 'completed'),
    [runs],
  )
  const averageScore = completedRuns.length
    ? completedRuns.reduce((sum, item) => sum + (scoreValue(item) ?? 0), 0) / completedRuns.length
    : null
  const running = Object.values(runs).some((item) => item.status === 'running')
  const completedInScope = visibleCases.filter((item) => runs[item.case_id]?.status === 'completed').length
  const runScopeLabel = selectedPartition === 'all'
    ? `全部 ${visibleCases.length} 条`
    : `${partitionLabel(selectedPartition)} ${visibleCases.length} 条`

  const runCase = useCallback(async (item: EvaluationCase) => {
    setRuns((current) => ({ ...current, [item.case_id]: { ...current[item.case_id], status: 'running', error: undefined } }))
    setReportState({})
    try {
      const prepared = await api<PreparedCaseResponse>(`/v1/skill-evaluations/hardware-kb/cases/${item.case_id}/prepare`, {
        method: 'POST',
        body: JSON.stringify({ target_platform_profile: targetPlatformProfile }),
      })
      const context = prepared.context
      const promptPlan = buildStrategyPromptPlan(
        context,
        'hardware_kb',
        1024 * 1024,
        64 * 1024,
      )
      const completion = await requestModelChatCompletion(
        modelConfig,
        [
          { role: 'system', content: promptPlan.systemPrompt },
          { role: 'user', content: promptPlan.userPrompt },
        ],
        { maxTokens: promptPlan.tokenBudget.outputReserveTokens },
      )
      const rawOutput = completion.choices?.[0]?.message?.content ?? ''
      if (!rawOutput.trim()) throw new Error('模型没有返回可评分文本。')
      const result = parseModelJson(rawOutput)
      const scored = await api<{ score: JsonRecord }>(`/v1/skill-evaluations/hardware-kb/cases/${item.case_id}/score`, {
        method: 'POST',
        body: JSON.stringify({
          context_sha256: context.context_sha256,
          result,
          target_platform_profile: targetPlatformProfile,
        }),
      })
      const modelContext = record(context.model_input_context)
      const evidence = Array.isArray(modelContext.context_records) ? modelContext.context_records : []
      setRuns((current) => ({
        ...current,
        [item.case_id]: {
          status: 'completed',
          contextSha256: stringValue(context.context_sha256),
          modelEvidenceCount: evidence.length,
          result,
          score: scored.score,
          rawOutput,
          tokenUsage: completion.tokenUsage,
        },
      }))
    } catch (error) {
      setRuns((current) => ({
        ...current,
        [item.case_id]: {
          ...current[item.case_id],
          status: 'failed',
          error: error instanceof Error ? error.message : '评测模型调用失败。',
        },
      }))
    }
  }, [modelConfig, targetPlatformProfile])

  const runAll = useCallback(async () => {
    if (!suite) return
    cancellationRef.current = false
    for (const item of visibleCases) {
      if (cancellationRef.current) break
      await runCase(item)
    }
  }, [runCase, suite, visibleCases])

  const saveReport = useCallback(async () => {
    if (!suite) return
    const completed = suite.cases
      .map((item) => ({ case: item, run: runs[item.case_id] }))
      .filter((item): item is { case: EvaluationCase; run: CaseRun & { result: JsonRecord; contextSha256: string } } => (
        item.run?.status === 'completed' && Boolean(item.run.result) && Boolean(item.run.contextSha256)
      ))
    if (completed.length === 0) {
      setReportState({ error: '至少完成一条模型评测后才能保存报告。' })
      return
    }
    try {
      const response = await api<{ artifact: { artifact_id: string }; report: JsonRecord }>('/v1/skill-evaluations/hardware-kb/report', {
        method: 'POST',
        body: JSON.stringify({
          model_name: modelConfig.model,
          target_platform_profile: targetPlatformProfile,
          results: completed.map(({ case: item, run }) => ({
            case_id: item.case_id,
            context_sha256: run.contextSha256,
            result: run.result,
          })),
        }),
      })
      setReportState({
        artifactId: response.artifact.artifact_id,
        report: response.report,
        message: `已保存 ${numberValue(record(response.report.coverage).completed_case_count) ?? completed.length} 条评测结果。`,
      })
    } catch (error) {
      setReportState({ error: error instanceof Error ? error.message : '无法保存评测报告。' })
    }
  }, [modelConfig.model, runs, suite, targetPlatformProfile])

  return <section className="work-section hardware-skill-evaluation">
    <div className="section-heading">
      <div><span className="eyebrow">历史基线</span><h2>规则/API 漏斗回归对照</h2></div>
      <div className="heading-actions">
        <span className="skill-evaluation-model-state ready">Agent Engine 已就绪</span>
        <button className="quiet-button" type="button" onClick={() => { cancellationRef.current = true }} disabled={!running} title="在当前模型调用结束后停止后续案例"><CircleStop size={14} />停止</button>
        <button className="quiet-button" type="button" onClick={() => { cancellationRef.current = true; setRuns({}); setReportState({}) }} disabled={running} title="清除本页浏览器中的评测结果"><RotateCcw size={14} />重置</button>
      </div>
    </div>
    <p className="skill-evaluation-note">{suite?.case_count ?? '—'} 条分层样本用于保留历史规则/API 漏斗的回归对照，覆盖 NPU、GPU、DSP、媒体 API、CPU 并行、IPC/锁/I/O/依赖反例与成本边界。它会显式使用静态目录选择，因此不能衡量生产模型语义路由的泛化能力，也不得作为生产目录选择证明。模型语义路由请使用下方的“模型语义路由评测”。</p>
    {catalogError && <div className="skill-evaluation-feedback error"><ShieldAlert size={15} /><span>{catalogError}</span></div>}
    {suite && <>
      <div className="skill-evaluation-overview">
        <div><span>当前范围</span><strong>{runScopeLabel}</strong><small>{suite.suite_id} · v{suite.suite_version}</small></div>
        <div><span>已评分</span><strong>{completedInScope} / {visibleCases.length}</strong><small>{averageScore === null ? '等待模型调用' : `诊断均分 ${averageScore.toFixed(1)}（非发布目标）`}</small></div>
        <div><span>策略版本</span><strong>{suite.strategy_contract.skill_version ?? '—'}</strong><small>{suite.strategy_contract.skill_id}</small></div>
        <div className="skill-evaluation-actions">
          <select value={selectedPartition} onChange={(event) => {
            const partition = event.target.value
            setSelectedPartition(partition)
            const next = suite.cases.find((item) => partition === 'all' || item.partition === partition)
            if (next) setSelectedCaseId(next.case_id)
          }} disabled={running} aria-label="选择评测切分">
            <option value="smoke">Smoke 回归 · {suite.partitions.smoke ?? 0} 条</option>
            <option value="development">开发集 · {suite.partitions.development ?? 0} 条</option>
            <option value="validation">验证集 · {suite.partitions.validation ?? 0} 条</option>
            <option value="holdout">冻结盲测 · {suite.partitions.holdout ?? 0} 条</option>
            <option value="all">全部切分 · {suite.case_count} 条</option>
          </select>
          <select value={selectedCaseId} onChange={(event) => setSelectedCaseId(event.target.value)} disabled={running} aria-label="选择评测案例">
            {visibleCases.map((item) => <option value={item.case_id} key={item.case_id}>{categoryLabel(item.category)} · {item.title_zh}</option>)}
          </select>
          <button className="primary-button compact" type="button" onClick={() => selectedCase && void runCase(selectedCase)} disabled={!selectedCase || running}><Play size={14} />运行当前</button>
          <button className="quiet-button" type="button" onClick={() => void runAll()} disabled={running || visibleCases.length === 0}><FlaskConical size={14} />运行 {runScopeLabel}</button>
          <button className="quiet-button" type="button" onClick={() => void saveReport()} disabled={running || completedRuns.length === 0}><Save size={14} />保存报告</button>
        </div>
      </div>
      {reportState.message && <div className="skill-evaluation-feedback success"><Check size={15} /><span>{reportState.message} <code>{reportState.artifactId}</code></span></div>}
      {reportState.error && <div className="skill-evaluation-feedback error"><ShieldAlert size={15} /><span>{reportState.error}</span></div>}
      {reportState.report && (() => {
        const quality = record(reportState.report?.quality_summary)
        const dimensions = record(quality.quality_dimensions)
        const hardware = record(quality.hardware)
        const parallel = record(quality.cpu_parallel)
        const bands = record(quality.quality_bands)
        return <div className="skill-evaluation-quality" aria-label="评测质量摘要">
          <div><span>硬件选择召回</span><strong>{percent(hardware.recall)}</strong><small>拒绝特异度 {percent(hardware.specificity)}</small></div>
          <div><span>CPU 并行判断</span><strong>{percent(parallel.balanced_accuracy)}</strong><small>召回 {percent(parallel.recall)} · 特异度 {percent(parallel.specificity)}</small></div>
          <div><span>结论校准</span><strong>{percent(dimensions.recommendation_calibration_rate)}</strong><small>作用域 {percent(dimensions.scope_disclosure_rate)} · 验证 {percent(dimensions.verification_action_rate)}</small></div>
          <div><span>质量状态</span><strong>{stringValue(bands.overall_semantic, '待评估')}</strong><small>安全校准 {stringValue(bands.safety_calibration, '待评估')}</small></div>
        </div>
      })()}
      <div className="table-shell skill-evaluation-table">
        <div className="table-header"><span>案例</span><span>切分</span><span>类别</span><span>模型上下文</span><span>诊断</span><span>Token</span><span /></div>
        {visibleCases.map((item) => {
          const run = runs[item.case_id] ?? { status: 'idle' as const }
          const score = scoreValue(run)
          const scoreRecord = record(run.score)
          const rawChecks = scoreRecord.checks
          const checks: JsonRecord[] = Array.isArray(rawChecks) ? rawChecks.map(record) : []
          const publicInput = record(item.public_input)
          const traceMetrics = record(publicInput.trace_metrics)
          return <details className={`table-row skill-evaluation-row ${run.status}`} key={item.case_id} open={item.case_id === selectedCaseId && run.status !== 'idle'}>
            <summary>
              <div><strong>{item.title_zh}</strong><span><code>{item.case_id}</code></span></div>
              <span>{partitionLabel(item.partition)}</span>
              <span>{categoryLabel(item.category)}</span>
              <span>{run.modelEvidenceCount === undefined ? '—' : `${run.modelEvidenceCount} 条证据`}</span>
              <span className={score === null ? '' : scoreRecord.passed === true ? 'score-pass' : 'score-fail'}>{run.status === 'running' ? <><LoaderCircle size={14} className="spin" />推理中</> : `${statusLabel(run.status)}${score === null ? '' : ` · ${score.toFixed(1)}`}`}</span>
              <span>{run.tokenUsage ? `${(run.tokenUsage.inputTokens / 1000).toFixed(1)}k / ${(run.tokenUsage.outputTokens / 1000).toFixed(1)}k` : '—'}</span>
              <button className="icon-button" type="button" title="运行此评测案例" aria-label={`运行 ${item.title_zh}`} disabled={running} onClick={(event) => { event.preventDefault(); event.stopPropagation(); void runCase(item) }}><Play size={14} /></button>
            </summary>
            <div className="skill-evaluation-detail">
              <details>
                <summary>查看公开代码与 Trace 输入</summary>
                <p>函数提示：<code>{stringValue(publicInput.function_hint, '—')}</code> · 模块：<code>{stringValue(publicInput.module, '—')}</code> · 语义族：<code>{item.family_id}</code> · 次数：<code>{numberValue(traceMetrics.occurrence_count) ?? '—'}</code></p>
                <pre>{stringValue(publicInput.code, '无公开代码。')}</pre>
              </details>
              {run.error && <p className="skill-evaluation-error">{run.error}</p>}
              {run.contextSha256 && <p>Context：<code>{run.contextSha256}</code></p>}
              {checks.length > 0 && <ul>{checks.map((check) => <li key={stringValue(check.check_id)} className={check.passed === true ? 'pass' : 'fail'}><strong>{check.passed === true ? '通过' : '未通过'}</strong><span>{stringValue(check.detail)}</span></li>)}</ul>}
              {run.rawOutput && <details><summary>查看模型原始回答</summary><pre>{run.rawOutput}</pre></details>}
            </div>
          </details>
        })}
      </div>
    </>}
  </section>
}
