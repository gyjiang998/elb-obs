'use client'

import {
  BrainCircuit,
  Check,
  ChevronRight,
  CircleAlert,
  FileWarning,
  FlaskConical,
  FolderGit2,
  History,
  LoaderCircle,
  RefreshCw,
  Save,
  Search,
  Settings2,
  Sparkles,
  Wrench,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import { dispatchAgentCommand } from '../lib/agent-command-bridge'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'

type ExperienceSource = {
  source_type: 'agent_run' | 'analysis_trace' | 'experiment_learning_report'
  source_id: string
  title: string
  created_at?: string
  updated_at?: string
  status?: string
  summary?: string
  environment?: Record<string, unknown>
  event_count?: number
}

type ResolutionOption = {
  resolution_id: string
  kind: string
  title: string
  rationale: string
  operator_steps: string[]
  agent_steps: string[]
  prerequisites: string[]
  risks: string[]
  verification: string
  target_skill_ids: string[]
  recommended: boolean
}

type ExperienceCase = {
  case_id: string
  recurrence_key: string
  title: string
  category: string
  symptom: string
  failure_stage?: string
  root_cause_hypothesis: string
  confidence: 'low' | 'medium' | 'high'
  trigger_conditions: string[]
  applicability_scope: string
  generalization_boundary: string
  affected_skill_ids: string[]
  resolution_options: ResolutionOption[]
  status: string
  selected_resolution_id?: string | null
  operator_note?: string
  observation_count?: number
  updated_at?: string
}

type ExperienceCatalog = {
  schema_version: string
  revision: number
  case_count: number
  review_count: number
  status_summary: Record<string, number>
  cases: ExperienceCase[]
}

type SourceCatalog = {
  schema_version: string
  count: number
  sources: ExperienceSource[]
}

const statusLabels: Record<string, string> = {
  observed: '待研判',
  triaged: '已研判',
  action_planned: '待执行',
  resolved: '已解决',
  verified: '已验证',
  dismissed: '已忽略',
}

const categoryLabels: Record<string, string> = {
  model_behavior: '模型行为',
  agent_framework: 'Agent 框架',
  workflow_orchestration: '工作流编排',
  skill_design: 'Skill 设计',
  host_environment: '宿主环境',
  sdk_or_toolchain: 'SDK / 工具链',
  runtime_library: '运行库',
  source_repository: '代码仓',
  device_or_os: '设备 / 系统',
  network_or_provider: '网络 / 模型服务',
  evidence_contract: '证据契约',
  other: '其他',
}

const resolutionLabels: Record<string, string> = {
  environment_config: '调整环境配置',
  repository_change: '调整代码仓',
  sdk_or_library_change: '更换 SDK / 运行库',
  device_change: '更换设备',
  runtime_adapter_change: '修改 Runtime Adapter',
  workflow_change: '优化工作流',
  skill_revision: '优化 Skill',
  code_fix: '修复代码',
  additional_evidence: '补充证据',
  no_action: '暂不处理',
}

const sourceLabels: Record<string, string> = {
  agent_run: 'Agent Run',
  analysis_trace: '分析 Trace',
  experiment_learning_report: '真机学习记录',
}

function formatTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function sourceKey(source: ExperienceSource) {
  return `${source.source_type}:${source.source_id}`
}

function environmentSummary(source: ExperienceSource) {
  const environment = source.environment ?? {}
  return [
    environment.model_name,
    environment.agent_display_name,
    environment.agent_engine_version,
    environment.host_os && environment.host_architecture
      ? `${environment.host_os}/${environment.host_architecture}`
      : environment.host_os,
    environment.chipset,
    environment.operating_system,
  ].filter((item): item is string => typeof item === 'string' && item.trim().length > 0).join(' · ') || '环境身份未完整记录'
}

export function ExperienceEvolutionStudio({
  onOpenView,
  onOpenSkill,
}: {
  onOpenView: (view: 'reasoning' | 'sources' | 'skills') => void
  onOpenSkill: (skillId: string) => void
}) {
  const [sources, setSources] = useState<ExperienceSource[]>([])
  const [catalog, setCatalog] = useState<ExperienceCatalog | null>(null)
  const [selectedSources, setSelectedSources] = useState<Set<string>>(new Set())
  const [selectedCaseId, setSelectedCaseId] = useState('')
  const [query, setQuery] = useState('')
  const [sourceQuery, setSourceQuery] = useState('')
  const [selectedResolutionId, setSelectedResolutionId] = useState('')
  const [operatorNote, setOperatorNote] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [agentPending, setAgentPending] = useState(false)
  const [notice, setNotice] = useState<{ tone: 'success' | 'error' | 'info'; text: string } | null>(null)

  const refresh = useCallback(async () => {
    const [sourceValue, catalogValue] = await Promise.all([
      api<SourceCatalog>('/v1/experience-evolution/sources?limit=120'),
      api<ExperienceCatalog>('/v1/experience-evolution'),
    ])
    setSources(sourceValue.sources)
    setCatalog(catalogValue)
    setSelectedCaseId((current) => current && catalogValue.cases.some((item) => item.case_id === current)
      ? current
      : catalogValue.cases[0]?.case_id ?? '')
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    void refresh().catch((error) => {
      if (!cancelled) setNotice({ tone: 'error', text: error instanceof Error ? error.message : '经验演进数据加载失败' })
    }).finally(() => {
      if (!cancelled) setLoading(false)
    })
    const updated = () => {
      setAgentPending(false)
      void refresh()
    }
    window.addEventListener('experience-library-updated', updated)
    return () => {
      cancelled = true
      window.removeEventListener('experience-library-updated', updated)
    }
  }, [refresh])

  const visibleSources = useMemo(() => {
    const normalized = sourceQuery.trim().toLocaleLowerCase()
    return sources.filter((source) => !normalized || `${source.title} ${source.summary} ${source.source_id} ${environmentSummary(source)}`.toLocaleLowerCase().includes(normalized))
  }, [sourceQuery, sources])

  const visibleCases = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase()
    return (catalog?.cases ?? []).filter((item) => !normalized || `${item.title} ${item.recurrence_key} ${item.symptom} ${item.root_cause_hypothesis}`.toLocaleLowerCase().includes(normalized))
  }, [catalog, query])

  const selectedCase = useMemo(
    () => catalog?.cases.find((item) => item.case_id === selectedCaseId) ?? null,
    [catalog, selectedCaseId],
  )

  useEffect(() => {
    setSelectedResolutionId(selectedCase?.selected_resolution_id ?? selectedCase?.resolution_options.find((item) => item.recommended)?.resolution_id ?? '')
    setOperatorNote(selectedCase?.operator_note ?? '')
  }, [selectedCase])

  function toggleSource(source: ExperienceSource) {
    const key = sourceKey(source)
    setSelectedSources((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function distillSelected() {
    const selected = sources.filter((source) => selectedSources.has(sourceKey(source)))
    if (selected.length === 0) {
      setNotice({ tone: 'error', text: '请至少选择一条历史执行记录。' })
      return
    }
    const refs = selected.map((source) => ({ source_type: source.source_type, source_id: source.source_id }))
    const requestId = dispatchAgentCommand({
      displayMessage: `提炼 ${selected.length} 条历史执行记录中的踩坑与解决方法`,
      message: [
        '请使用“执行踩坑提炼” Skill 分析以下历史执行源。',
        `source_refs=${JSON.stringify(refs)}`,
        '请逐项核对工具事实和环境差异，提炼可复用问题类、根因假设、多个可选择处置及验证方法。',
        '最后必须调用 experience_save_review 保存结构化结果；不要自动修改环境、代码或现有 Skill。',
      ].join('\n'),
    })
    if (!requestId) {
      setNotice({ tone: 'error', text: '页面 Agent 尚未就绪。' })
      return
    }
    setAgentPending(true)
    setNotice({ tone: 'info', text: '已提交给页面 Agent；提炼结果落盘后会自动刷新。' })
  }

  async function saveTriage(status: string) {
    if (!selectedCase) return
    setSaving(true)
    setNotice(null)
    try {
      await api(`/v1/experience-evolution/cases/${encodeURIComponent(selectedCase.case_id)}/triage`, {
        method: 'POST',
        body: JSON.stringify({
          status,
          selected_resolution_id: selectedResolutionId,
          operator_note: operatorNote,
        }),
      })
      await refresh()
      setNotice({ tone: 'success', text: '处置选择与状态已保存。' })
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '处置保存失败' })
    } finally {
      setSaving(false)
    }
  }

  async function askAgentToApply() {
    if (!selectedCase || !selectedResolutionId) {
      setNotice({ tone: 'error', text: '请先选择一个处置方案。' })
      return
    }
    await saveTriage('action_planned')
    const requestId = dispatchAgentCommand({
      displayMessage: `执行经验案例“${selectedCase.title}”的所选处置`,
      message: [
        `请处理经验案例 ${selectedCase.case_id}，用户已选择处置 ${selectedResolutionId}。`,
        '先调用 experience_list_cases 读取当前案例与处置合同，再根据处置类型使用合适工具执行。',
        '不要把计划当作完成；执行后记录真实结果，并调用 experience_triage_case 更新为 resolved、verified 或保留 action_planned。',
        `用户备注：${operatorNote || '无'}`,
      ].join('\n'),
    })
    if (requestId) {
      setAgentPending(true)
      setNotice({ tone: 'info', text: '所选处置已交给页面 Agent 执行。' })
    }
  }

  function openResolutionTarget(option: ResolutionOption) {
    if (option.kind === 'skill_revision') {
      const target = option.target_skill_ids[0] || selectedCase?.affected_skill_ids[0]
      if (target) onOpenSkill(target)
      else onOpenView('skills')
      return
    }
    if (option.kind === 'repository_change' || option.kind === 'sdk_or_library_change' || option.kind === 'environment_config') {
      onOpenView('sources')
      return
    }
    onOpenView('reasoning')
  }

  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'experience-evolution')
  if (loading) return <div className="experience-loading"><LoaderCircle size={18} className="spin" />正在读取历史执行与经验案例</div>

  return <div className="experience-studio">
    <section className="experience-source-band">
      <header>
        <div><span className="eyebrow">历史证据源</span><h2>选择需要对比的执行记录</h2></div>
        <div className="experience-actions">
          <button className="quiet-button" type="button" onClick={() => void refresh()}><RefreshCw size={14} />刷新</button>
          <button className="primary-button" type="button" disabled={agentPending || selectedSources.size === 0} onClick={distillSelected}>
            {agentPending ? <LoaderCircle size={15} className="spin" /> : <Sparkles size={15} />}交给 Agent 提炼
          </button>
        </div>
      </header>
      <div className="experience-source-toolbar">
        <label><Search size={15} /><input value={sourceQuery} onChange={(event) => setSourceQuery(event.target.value)} placeholder="搜索 Run、错误阶段或环境" /></label>
        <span>已选 {selectedSources.size} / {visibleSources.length}</span>
      </div>
      <div className="experience-source-list" aria-label="历史执行证据源">
        {visibleSources.map((source) => {
          const selected = selectedSources.has(sourceKey(source))
          return <button type="button" className={`experience-source-row ${selected ? 'selected' : ''}`} key={sourceKey(source)} onClick={() => toggleSource(source)}>
            <span className="experience-source-check">{selected ? <Check size={13} /> : null}</span>
            <span><strong>{source.title}</strong><small>{sourceLabels[source.source_type]} · {environmentSummary(source)}</small></span>
            <span><em>{source.status || 'recorded'}</em><small>{source.event_count ?? 0} 条 · 创建于 {formatTime(source.created_at)}</small></span>
          </button>
        })}
        {visibleSources.length === 0 && <div className="experience-empty">没有可用的历史执行源。</div>}
      </div>
    </section>

    <section className="experience-case-workbench">
      <aside className="experience-case-list">
        <div className="experience-case-list-head">
          <div><span className="eyebrow">经验案例</span><strong>{catalog?.case_count ?? 0} 个踩坑</strong></div>
          <label><Search size={14} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选问题" /></label>
        </div>
        <div className="experience-case-rows">
          {visibleCases.map((item) => <button type="button" className={item.case_id === selectedCaseId ? 'selected' : ''} key={item.case_id} onClick={() => setSelectedCaseId(item.case_id)}>
            <span className="experience-case-icon">{item.category === 'skill_design' ? <BrainCircuit size={16} /> : <FileWarning size={16} />}</span>
            <span><strong>{item.title}</strong><small>{categoryLabels[item.category] || item.category} · {item.failure_stage || '跨阶段'}</small></span>
            <span><em>{statusLabels[item.status] || item.status}</em><small>{item.observation_count ?? 1} 次观察</small></span>
            <ChevronRight size={14} />
          </button>)}
          {visibleCases.length === 0 && <div className="experience-empty">尚未提炼经验案例。</div>}
        </div>
      </aside>

      <article className="experience-case-detail">
        {selectedCase ? <>
          <header>
            <div><span className="eyebrow">{categoryLabels[selectedCase.category] || selectedCase.category}</span><h2>{selectedCase.title}</h2><code>{selectedCase.recurrence_key}</code></div>
            <span className={`experience-status ${selectedCase.status}`}>{statusLabels[selectedCase.status] || selectedCase.status}</span>
          </header>

          <div className="experience-fact-grid">
            <section><span>可观察症状</span><p>{selectedCase.symptom}</p></section>
            <section><span>根因假设 · {selectedCase.confidence === 'high' ? '高' : selectedCase.confidence === 'medium' ? '中' : '低'}置信度</span><p>{selectedCase.root_cause_hypothesis}</p></section>
            <section><span>适用范围</span><p>{selectedCase.applicability_scope}</p></section>
            <section><span>泛化边界</span><p>{selectedCase.generalization_boundary}</p></section>
          </div>

          <div className="experience-trigger-strip"><strong>触发条件</strong>{selectedCase.trigger_conditions.map((item) => <span key={item}>{item}</span>)}</div>

          <section className="experience-resolution-section">
            <div className="experience-section-title"><div><span className="eyebrow">处置决策</span><h3>选择解决方法</h3></div><small>选择不会自动执行，执行与验证分别留痕</small></div>
            <div className="experience-resolution-list">
              {selectedCase.resolution_options.map((option) => {
                const selected = option.resolution_id === selectedResolutionId
                return <div className={`experience-resolution ${selected ? 'selected' : ''}`} key={option.resolution_id}>
                  <button type="button" className="experience-resolution-main" onClick={() => setSelectedResolutionId(option.resolution_id)}>
                    <span className="experience-radio">{selected ? <Check size={13} /> : null}</span>
                    <span><strong>{option.title}</strong><small>{resolutionLabels[option.kind] || option.kind}{option.recommended ? ' · 推荐' : ''}</small><p>{option.rationale}</p></span>
                  </button>
                  {selected && <div className="experience-resolution-expanded">
                    {option.operator_steps.length > 0 && <div><strong>用户操作</strong><ol>{option.operator_steps.map((item) => <li key={item}>{item}</li>)}</ol></div>}
                    {option.agent_steps.length > 0 && <div><strong>Agent 操作</strong><ol>{option.agent_steps.map((item) => <li key={item}>{item}</li>)}</ol></div>}
                    <div><strong>验证方法</strong><p>{option.verification}</p></div>
                    <button className="quiet-button" type="button" onClick={() => openResolutionTarget(option)}>{option.kind === 'skill_revision' ? <BrainCircuit size={14} /> : option.kind === 'repository_change' ? <FolderGit2 size={14} /> : <Settings2 size={14} />}{resolutionLabels[option.kind] || '打开相关工作区'}</button>
                  </div>}
                </div>
              })}
            </div>
          </section>

          <label className="experience-note"><span>处置备注</span><textarea value={operatorNote} onChange={(event) => setOperatorNote(event.target.value)} placeholder="记录环境路径、负责人、暂缓原因或验证结果" /></label>
          <footer className="experience-detail-actions">
            <button className="quiet-button" type="button" disabled={saving} onClick={() => void saveTriage('triaged')}><Save size={14} />保存选择</button>
            <button className="quiet-button" type="button" disabled={saving} onClick={() => void saveTriage('verified')}><FlaskConical size={14} />标记已验证</button>
            <button className="primary-button" type="button" disabled={saving || agentPending || !selectedResolutionId} onClick={() => void askAgentToApply()}>{agentPending ? <LoaderCircle size={15} className="spin" /> : <Wrench size={15} />}让 Agent 执行所选处置</button>
          </footer>
        </> : <div className="experience-case-placeholder"><History size={24} /><strong>选择一条经验案例</strong><p>查看证据分层、根因假设、适用范围和候选处置。</p></div>}
      </article>
    </section>

    {notice && !errorForwarded && <div className={`experience-notice ${notice.tone}`}>{notice.tone === 'error' ? <CircleAlert size={15} /> : <Check size={15} />}{notice.text}</div>}
  </div>
}
