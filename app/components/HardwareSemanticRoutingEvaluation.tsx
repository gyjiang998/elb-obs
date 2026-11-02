'use client'

import { Check, FlaskConical, LoaderCircle, Play, Save, ShieldAlert } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import { buildHardwareSemanticRoutingPromptPlan } from '../lib/hardware-semantic-routing'
import { createDefaultBrowserModelConfig, modelConfigChangedEvent, readBrowserModelConfig, type BrowserModelConfig } from '../lib/model-config'
import { requestModelChatCompletion, type ModelTokenUsage } from '../lib/model-client'
import { parseModelJson } from '../lib/reasoning-prompt'
import type { TargetPlatformProfile } from '../lib/target-platform-profile'
import type { JsonRecord } from '../lib/types'

type EvaluationCase = {
  case_id: string
  title_zh: string
  category: string
  partition: string
  family_id: string
  variation_axis: string
}

type EvaluationSuite = {
  suite_id: string
  suite_version: string
  purpose_zh: string
  case_count: number
  categories: Record<string, number>
  partitions: Record<string, number>
  cases: EvaluationCase[]
}

type CaseRun = {
  status: 'idle' | 'running' | 'completed' | 'failed'
  contextSha256?: string
  result?: JsonRecord
  score?: JsonRecord
  error?: string
  tokenUsage?: ModelTokenUsage
}

type PreparedCase = { case: EvaluationCase; context: JsonRecord }

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value ? value : fallback
}

function percent(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '—'
}

function categoryLabel(category: string): string {
  const labels: Record<string, string> = {
    npu_positive: 'NPU 正例', gpu_positive: 'GPU 正例', dsp_positive: 'DSP 正例', media_positive: '媒体正例',
    negative_ipc: 'IPC 反例', negative_lock: '锁反例', negative_dependency: '依赖反例', negative_io: 'I/O 反例', negative_ambiguous: '语义缺失反例',
  }
  return labels[category] ?? category
}

export function HardwareSemanticRoutingEvaluation({ targetPlatformProfile }: { targetPlatformProfile: TargetPlatformProfile }) {
  const [suite, setSuite] = useState<EvaluationSuite | null>(null)
  const [partition, setPartition] = useState('smoke')
  const [runs, setRuns] = useState<Record<string, CaseRun>>({})
  const [modelConfig, setModelConfig] = useState<BrowserModelConfig>(() => createDefaultBrowserModelConfig())
  const [error, setError] = useState('')
  const [report, setReport] = useState<JsonRecord | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      const response = await api<{ suite: EvaluationSuite }>('/v1/skill-evaluations/hardware-semantic-routing')
      setSuite(response.suite)
      setError('')
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '无法读取模型语义路由评测集。')
    }
  }, [])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    const listener = (event: Event) => {
      const value = (event as CustomEvent<BrowserModelConfig>).detail
      setModelConfig(value && typeof value === 'object' ? value : readBrowserModelConfig())
    }
    setModelConfig(readBrowserModelConfig())
    window.addEventListener(modelConfigChangedEvent, listener)
    return () => window.removeEventListener(modelConfigChangedEvent, listener)
  }, [])

  const visibleCases = useMemo(() => suite?.cases.filter((item) => partition === 'all' || item.partition === partition) ?? [], [partition, suite])
  const running = Object.values(runs).some((run) => run.status === 'running')
  const completed = Object.values(runs).filter((run) => run.status === 'completed').length

  const runCase = useCallback(async (item: EvaluationCase) => {
    setRuns((current) => ({ ...current, [item.case_id]: { status: 'running' } }))
    setReport(null)
    try {
      const prepared = await api<PreparedCase>(`/v1/skill-evaluations/hardware-semantic-routing/cases/${item.case_id}/prepare`, {
        method: 'POST', body: JSON.stringify({ target_platform_profile: targetPlatformProfile }),
      })
      const plan = buildHardwareSemanticRoutingPromptPlan(
        prepared.context,
        1024 * 1024,
        64 * 1024,
      )
      const completion = await requestModelChatCompletion(modelConfig, plan.messages, { maxTokens: plan.tokenBudget.outputReserveTokens })
      const rawOutput = completion.choices?.[0]?.message?.content ?? ''
      const result = parseModelJson(rawOutput)
      const scored = await api<{ score: JsonRecord }>(`/v1/skill-evaluations/hardware-semantic-routing/cases/${item.case_id}/score`, {
        method: 'POST',
        body: JSON.stringify({ context_sha256: text(prepared.context.context_sha256), result, target_platform_profile: targetPlatformProfile }),
      })
      setRuns((current) => ({
        ...current,
        [item.case_id]: { status: 'completed', contextSha256: text(prepared.context.context_sha256), result, score: scored.score, tokenUsage: completion.tokenUsage },
      }))
    } catch (runError) {
      setRuns((current) => ({ ...current, [item.case_id]: { status: 'failed', error: runError instanceof Error ? runError.message : '语义路由评测失败。' } }))
    }
  }, [modelConfig, targetPlatformProfile])

  const runVisible = useCallback(async () => {
    for (const item of visibleCases) await runCase(item)
  }, [runCase, visibleCases])

  const saveReport = useCallback(async () => {
    const results = Object.entries(runs).flatMap(([caseId, run]) => run.status === 'completed' && run.contextSha256 && run.result
      ? [{ case_id: caseId, context_sha256: run.contextSha256, result: run.result }]
      : [])
    if (results.length === 0) return
    setSaving(true)
    try {
      const response = await api<{ report: JsonRecord }>('/v1/skill-evaluations/hardware-semantic-routing/report', {
        method: 'POST', body: JSON.stringify({ results, model_name: modelConfig.model, target_platform_profile: targetPlatformProfile }),
      })
      setReport(response.report)
      setError('')
    } catch (reportError) {
      setError(reportError instanceof Error ? reportError.message : '无法保存泛化评测报告。')
    } finally {
      setSaving(false)
    }
  }, [modelConfig.model, runs, targetPlatformProfile])

  return <section className="work-section hardware-semantic-routing-evaluation">
    <div className="section-heading"><div><span className="eyebrow">冻结泛化评测</span><h2>模型语义路由评测</h2></div><span className="hardware-semantic-routing-state ready">Agent Engine 已就绪</span></div>
    <p className="skill-catalog-intro">同一模型分别运行函数改名、控制流等价改写、领域语义缺失和 IPC/锁/I/O 反例。规则基线不进入任何模型请求；正例看选择召回，反例看保守拒绝，变体族看不变性。</p>
    {error && <div className="hardware-semantic-routing-feedback error"><ShieldAlert size={15} /><span>{error}</span></div>}
    <div className="hardware-semantic-routing-evaluation-actions">
      <select value={partition} onChange={(event) => setPartition(event.target.value)} aria-label="评测切分">
        <option value="smoke">Smoke 回归</option><option value="development">开发集</option><option value="validation">验证集</option><option value="holdout">冻结盲测</option><option value="all">全部样本</option>
      </select>
      <button className="primary-button compact" type="button" disabled={running || visibleCases.length === 0} onClick={() => void runVisible()} title="使用 Kimi Agent Engine 依次运行可见泛化样本"><Play size={14} />运行 {visibleCases.length} 条</button>
      <button className="quiet-button" type="button" disabled={saving || completed === 0} onClick={() => void saveReport()} title="汇总已完成样本的泛化指标并保存审计报告">{saving ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}保存报告</button>
    </div>
    {suite && <div className="hardware-semantic-routing-summary"><div><span>样本</span><strong>{suite.case_count} 条</strong></div><div><span>当前切分</span><strong>{visibleCases.length} 条</strong></div><div><span>已完成</span><strong>{completed} 条</strong></div><div><span>边界</span><strong>模型输入无规则路由</strong></div></div>}
    <div className="table-shell hardware-semantic-routing-evaluation-table"><div className="table-header"><span>样本</span><span>变体</span><span>状态</span><span>结果</span><span /></div>{visibleCases.map((item) => {
      const run = runs[item.case_id]
      const score = record(run?.score)
      return <div className="table-row" key={item.case_id}><div><strong>{item.title_zh}</strong><span>{categoryLabel(item.category)} · {item.family_id}</span></div><span>{item.variation_axis}</span><span>{run?.status === 'running' ? '推理中' : run?.status === 'completed' ? '已评分' : run?.status === 'failed' ? '失败' : '未运行'}</span><span>{run?.status === 'completed' ? `${text(score.assessment)} · ${text(score.model_decision)}` : run?.error || '—'}</span><button className="icon-button" type="button" disabled={run?.status === 'running'} title="运行此条模型语义路由泛化样本" onClick={() => void runCase(item)}>{run?.status === 'running' ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}</button></div>
    })}</div>
    {report && <div className="hardware-semantic-routing-result"><div className="hardware-semantic-routing-result-heading"><Check size={16} /><div><span>泛化报告</span><strong>模型语义路由指标</strong></div></div><div className="hardware-semantic-routing-summary"><div><span>选择召回</span><strong>{percent(report.selection_recall)}</strong></div><div><span>拒绝特异性</span><strong>{percent(report.rejection_specificity)}</strong></div><div><span>平衡准确率</span><strong>{percent(report.balanced_accuracy)}</strong></div><div><span>变体不变性</span><strong>{percent(report.transformation_family_invariance)}</strong></div></div><p>{text(report.statement_zh)}</p></div>}
    <div className="hardware-semantic-routing-note"><FlaskConical size={15} /><span>“语义完全删去”的通用数值循环以保守不路由为正确结果；它防止系统把任何循环都猜成 DSP、GPU 或 NPU。</span></div>
  </section>
}
