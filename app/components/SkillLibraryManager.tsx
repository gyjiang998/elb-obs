'use client'

import {
  Check,
  FileCode2,
  LoaderCircle,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  RotateCcw,
  Save,
  Search,
  Undo2,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../lib/api'
import type {
  ManagedSkillDetail,
  ManagedSkillSummary,
  SkillLibraryPresentation,
  UserSkillManagementLibrary,
} from '../lib/user-skill-library'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'

type Draft = {
  skillId: string
  label: string
  description: string
  promptSlot: string
  content: string
  changeSummary: string
  versionId: string
  origin: 'server' | 'user' | 'new'
}

type OriginFilter = 'all' | 'user' | 'server'

const emptyDraft: Draft = {
  skillId: '',
  label: '',
  description: '',
  promptSlot: '',
  content: '',
  changeSummary: '',
  versionId: '',
  origin: 'new',
}

function fallbackContent(item: SkillLibraryPresentation): string {
  return [
    `# ${item.label_zh}`,
    '',
    item.description_zh,
    '',
    item.execution ? `- 执行方式：${item.execution}` : '',
    item.input ? `- 输入：${item.input}` : '',
    item.output ? `- 输出：${item.output}` : '',
    item.boundary ? `- 证据边界：${item.boundary}` : '',
  ].filter(Boolean).join('\n')
}

function formatTime(value?: string | null): string {
  if (!value) return ''
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', { hour12: false })
}

function editableDraftFingerprint(draft: Draft): string {
  return JSON.stringify({
    skillId: draft.skillId,
    label: draft.label,
    description: draft.description,
    promptSlot: draft.promptSlot,
    content: draft.content,
  })
}

export function SkillLibraryManager({
  profileId,
  presentations,
  onLibraryChanged,
  onOpenWorkspace,
  focusSkillId = '',
}: {
  profileId: string
  presentations: SkillLibraryPresentation[]
  onLibraryChanged: () => Promise<void> | void
  onOpenWorkspace: (view: string) => void
  focusSkillId?: string
}) {
  const [library, setLibrary] = useState<UserSkillManagementLibrary | null>(null)
  const [selectedSkillId, setSelectedSkillId] = useState('')
  const [draft, setDraft] = useState<Draft>(emptyDraft)
  const [baselineDraft, setBaselineDraft] = useState<Draft>(emptyDraft)
  const [query, setQuery] = useState('')
  const [originFilter, setOriginFilter] = useState<OriginFilter>('all')
  const [listCollapsed, setListCollapsed] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)
  const listRowsRef = useRef<HTMLDivElement | null>(null)
  const rowRefs = useRef(new Map<string, HTMLButtonElement>())

  const presentationById = useMemo(
    () => new Map(presentations.map((item) => [item.skill_id, item])),
    [presentations],
  )
  const managedById = useMemo(
    () => new Map((library?.skills ?? []).map((item) => [item.skill_id, item])),
    [library],
  )
  const skillEntries = useMemo(() => {
    const ids = new Set([...presentations.map((item) => item.skill_id), ...(library?.skills ?? []).map((item) => item.skill_id)])
    return Array.from(ids).map((skillId) => {
      const managed = managedById.get(skillId)
      const presentation = presentationById.get(skillId)
      const userNamed = managed?.effective_origin === 'user' || !presentation
      const label = userNamed ? managed?.label_zh || presentation?.label_zh || skillId : presentation?.label_zh || managed?.label_zh || skillId
      const description = userNamed
        ? managed?.description_zh || presentation?.description_zh || ''
        : presentation?.description_zh || managed?.description_zh || ''
      return { skillId, managed, presentation, label, description }
    }).sort((left, right) => {
        const leftUser = left.managed?.effective_origin === 'user' ? 0 : 1
        const rightUser = right.managed?.effective_origin === 'user' ? 0 : 1
        return leftUser - rightUser || left.label.localeCompare(right.label, 'zh-CN')
      })
  }, [library, managedById, presentationById, presentations])
  const skillCounts = useMemo(() => ({
    all: skillEntries.length,
    user: skillEntries.filter((item) => item.managed?.effective_origin === 'user').length,
    server: skillEntries.filter((item) => item.managed?.effective_origin !== 'user').length,
  }), [skillEntries])
  const visibleSkills = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase()
    return skillEntries.filter((item) => {
      const effectiveOrigin = item.managed?.effective_origin === 'user' ? 'user' : 'server'
      if (originFilter !== 'all' && effectiveOrigin !== originFilter) return false
      return !normalized || `${item.skillId} ${item.label} ${item.description}`.toLocaleLowerCase().includes(normalized)
    })
  }, [originFilter, query, skillEntries])
  const dirty = useMemo(
    () => editableDraftFingerprint(draft) !== editableDraftFingerprint(baselineDraft),
    [baselineDraft, draft],
  )

  const loadCatalog = useCallback(async () => {
    if (!profileId) return null
    const value = await api<UserSkillManagementLibrary>(`/v1/user-skill-library?profile_id=${encodeURIComponent(profileId)}`)
    setLibrary(value)
    return value
  }, [profileId])

  const applyDetail = useCallback((
    detail: ManagedSkillDetail,
    presentation?: SkillLibraryPresentation,
    requestedVersionId?: string,
  ) => {
    const skill = detail.skill
    const origin = skill.origin === 'user' ? 'user' : 'server'
    const nextDraft: Draft = {
      skillId: skill.skill_id,
      label: skill.label_zh || presentation?.label_zh || skill.skill_id,
      description: skill.description_zh || presentation?.description_zh || '',
      promptSlot: skill.prompt_slot || presentation?.prompt_slot || '',
      content: skill.content || skill.prompt || presentation?.fallback_content || (presentation ? fallbackContent(presentation) : ''),
      changeSummary: '',
      versionId: requestedVersionId || skill.version_id || (origin === 'server' ? 'server' : ''),
      origin,
    }
    setDraft(nextDraft)
    setBaselineDraft(nextDraft)
  }, [])

  const openSkill = useCallback(async (skillId: string, versionId = '', managedOverride?: ManagedSkillSummary) => {
    const presentation = presentationById.get(skillId)
    const managed = managedOverride ?? managedById.get(skillId)
    setSelectedSkillId(skillId)
    setLoading(true)
    setNotice(null)
    try {
      if (managed) {
        const suffix = versionId ? `&version_id=${encodeURIComponent(versionId)}` : ''
        const detail = await api<ManagedSkillDetail>(`/v1/user-skill-library/skills/${encodeURIComponent(skillId)}?profile_id=${encodeURIComponent(profileId)}${suffix}`)
        applyDetail(detail, presentation, versionId || managed.effective_version_id || '')
      } else if (presentation) {
        const nextDraft: Draft = {
          skillId,
          label: presentation.label_zh,
          description: presentation.description_zh,
          promptSlot: presentation.prompt_slot || '',
          content: presentation.fallback_content || fallbackContent(presentation),
          changeSummary: '',
          versionId: 'server',
          origin: 'server',
        }
        setDraft(nextDraft)
        setBaselineDraft(nextDraft)
      }
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Skill 内容加载失败' })
    } finally {
      setLoading(false)
    }
  }, [applyDetail, managedById, presentationById, profileId])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    void loadCatalog().then((value) => {
      if (cancelled || !value) return
      const first = value.skills[0]?.skill_id || presentations[0]?.skill_id || ''
      if (first) void openSkill(first, '', value.skills.find((item) => item.skill_id === first))
    }).catch((error) => {
      if (!cancelled) setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Skill 仓加载失败' })
    }).finally(() => {
      if (!cancelled) setLoading(false)
    })
    return () => { cancelled = true }
  // openSkill depends on the catalog it is about to replace. Profile changes
  // intentionally restart from the first current entry.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadCatalog, profileId])

  useEffect(() => {
    const refresh = () => { void loadCatalog() }
    window.addEventListener('skill-library-updated', refresh)
    const timer = window.setInterval(refresh, 30000)
    return () => {
      window.removeEventListener('skill-library-updated', refresh)
      window.clearInterval(timer)
    }
  }, [loadCatalog])

  useEffect(() => {
    if (!focusSkillId || !profileId) return
    setOriginFilter('all')
    setQuery('')
    void openSkill(focusSkillId)
  }, [focusSkillId, openSkill, profileId])

  useEffect(() => {
    if (!dirty) return
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => window.removeEventListener('beforeunload', warnBeforeUnload)
  }, [dirty])

  useEffect(() => {
    if (!selectedSkillId) return
    window.requestAnimationFrame(() => {
      const container = listRowsRef.current
      const row = rowRefs.current.get(selectedSkillId)
      if (!container || !row) return
      const containerRect = container.getBoundingClientRect()
      const rowRect = row.getBoundingClientRect()
      container.scrollTop += rowRect.top - containerRect.top - 6
    })
  }, [originFilter, query, selectedSkillId, visibleSkills])

  function confirmDiscardChanges(): boolean {
    return !dirty || window.confirm('当前 Skill 有未保存修改。放弃这些修改并继续吗？')
  }

  function requestOpenSkill(skillId: string, versionId = '', managedOverride?: ManagedSkillSummary) {
    if (skillId === selectedSkillId && !versionId) return
    if (!confirmDiscardChanges()) return
    void openSkill(skillId, versionId, managedOverride)
  }

  function startNewSkill() {
    if (!confirmDiscardChanges()) return
    setSelectedSkillId('')
    setDraft(emptyDraft)
    setBaselineDraft(emptyDraft)
    setNotice(null)
  }

  function restoreUnsavedDraft() {
    setDraft({ ...baselineDraft })
    setNotice({ tone: 'success', text: '已撤销本次尚未保存的修改。' })
  }

  async function saveDraft() {
    setSaving(true)
    setNotice(null)
    try {
      const response = await api<{ version: { version_id: string } }>('/v1/user-skill-library/versions', {
        method: 'POST',
        body: JSON.stringify({
          skill_id: draft.skillId.trim(),
          label_zh: draft.label.trim(),
          description_zh: draft.description.trim(),
          content: draft.content,
          prompt_slot: draft.promptSlot.trim(),
          change_summary_zh: draft.changeSummary.trim(),
          profile_id: profileId,
        }),
      })
      const updated = await loadCatalog()
      await onLibraryChanged()
      window.dispatchEvent(new CustomEvent('skill-library-updated', { detail: { skill_id: draft.skillId, version_id: response.version.version_id } }))
      await openSkill(draft.skillId, response.version.version_id, updated?.skills.find((item) => item.skill_id === draft.skillId))
      setNotice({ tone: 'success', text: '新的自编辑版本已保存并热加载。' })
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Skill 保存失败' })
    } finally {
      setSaving(false)
    }
  }

  async function activateVersion(versionId: string | null) {
    if (!draft.skillId) return
    if (!confirmDiscardChanges()) return
    setSaving(true)
    setNotice(null)
    try {
      await api(`/v1/user-skill-library/skills/${encodeURIComponent(draft.skillId)}/activation`, {
        method: 'POST',
        body: JSON.stringify({ version_id: versionId, profile_id: profileId }),
      })
      const updated = await loadCatalog()
      await onLibraryChanged()
      window.dispatchEvent(new CustomEvent('skill-library-updated', { detail: { skill_id: draft.skillId, version_id: versionId } }))
      if (versionId || managedById.get(draft.skillId)?.has_server_version) {
        await openSkill(draft.skillId, versionId || 'server', updated?.skills.find((item) => item.skill_id === draft.skillId))
      } else {
        setSelectedSkillId('')
        setDraft(emptyDraft)
        setBaselineDraft(emptyDraft)
      }
      setNotice({ tone: 'success', text: versionId ? '自编辑版本已设为当前版本。' : '后续任务将使用平台原始版本。' })
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Skill 版本切换失败' })
    } finally {
      setSaving(false)
    }
  }

  const selectedManaged: ManagedSkillSummary | undefined = managedById.get(selectedSkillId)
  const selectedPresentation = presentationById.get(selectedSkillId)
  const selectedVersion = selectedManaged?.versions.find((item) => item.version_id === draft.versionId)
  const contentLineCount = draft.content ? draft.content.split(/\r?\n/).length : 0
  const canSave = !saving && dirty && Boolean(draft.skillId.trim() && draft.label.trim() && draft.content.trim())

  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'skill-library')
  return (
    <section
      className={`skill-manager ${listCollapsed ? 'list-collapsed' : ''}`}
      aria-label="Skill 版本管理"
      onKeyDown={(event) => {
        if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 's') {
          event.preventDefault()
          if (canSave) void saveDraft()
        }
      }}
    >
      <aside className="skill-manager-list">
        <div className="skill-manager-toolbar">
          <label><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、ID 或能力" /></label>
          <button className="icon-button" type="button" title="新建自编辑 Skill" aria-label="新建自编辑 Skill" onClick={startNewSkill}><Plus size={16} /></button>
        </div>
        <div className="skill-manager-filters" role="tablist" aria-label="Skill 来源筛选">
          {([
            ['all', '全部'],
            ['user', '自编辑'],
            ['server', '平台'],
          ] as Array<[OriginFilter, string]>).map(([filter, label]) => <button
            key={filter}
            type="button"
            role="tab"
            aria-selected={originFilter === filter}
            className={originFilter === filter ? 'selected' : ''}
            onClick={() => setOriginFilter(filter)}
          ><span>{label}</span><em>{skillCounts[filter]}</em></button>)}
        </div>
        <div className="skill-manager-count"><span>{visibleSkills.length} 个结果</span><small>自编辑版本优先热加载</small></div>
        <div className="skill-manager-rows" ref={listRowsRef}>
          {visibleSkills.map((item) => {
            const active = item.skillId === selectedSkillId
            const user = item.managed?.effective_origin === 'user'
            const userVersionCount = item.managed?.user_version_count ?? 0
            return <button
              className={`skill-manager-row ${active ? 'selected' : ''}`}
              type="button"
              key={item.skillId}
              ref={(node) => {
                if (node) rowRefs.current.set(item.skillId, node)
                else rowRefs.current.delete(item.skillId)
              }}
              onClick={() => requestOpenSkill(item.skillId)}
            >
              <span className="skill-manager-row-icon"><FileCode2 size={16} /></span>
              <span className="skill-manager-row-copy"><strong>{item.label}</strong><small>{item.description || '未填写能力说明'}</small><code>{item.skillId}</code></span>
              <span className="skill-manager-row-meta"><em className={user ? 'user' : 'server'}>{user ? '自编辑' : '平台'}</em>{userVersionCount > 0 && <small>{userVersionCount} 版</small>}</span>
            </button>
          })}
          {visibleSkills.length === 0 && <div className="skill-manager-empty"><Search size={18} /><strong>没有匹配的 Skill</strong><span>调整关键词或来源筛选。</span></div>}
        </div>
      </aside>

      <div className="skill-manager-editor">
        <header className="skill-manager-editor-head">
          <div className="skill-manager-editor-title">
            <button className="icon-button" type="button" title={listCollapsed ? '显示 Skill 列表' : '折叠 Skill 列表，扩大编辑区'} aria-label={listCollapsed ? '显示 Skill 列表' : '折叠 Skill 列表'} onClick={() => setListCollapsed((current) => !current)}>{listCollapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}</button>
            <div><span className="eyebrow">{draft.origin === 'new' ? '新建 Skill' : draft.origin === 'user' ? '用户自编辑版本' : '平台原始版本'}</span><h3>{draft.label || '新建 Skill'}</h3></div>
          </div>
          <div className="skill-manager-editor-status">
            {dirty && <span className="skill-unsaved-badge">未保存</span>}
            {draft.skillId && <span className={`skill-origin-badge ${draft.origin}`}>{draft.origin === 'user' ? '自编辑' : '平台基线'}</span>}
          </div>
        </header>

        {loading ? <div className="skill-manager-loading"><LoaderCircle size={18} className="spin" />正在读取 Skill</div> : <>
          <div className="skill-manager-editor-scroll">
            <div className="skill-editor-fields identity">
              <label><span>Skill ID</span><input value={draft.skillId} disabled={draft.origin !== 'new'} onChange={(event) => setDraft((current) => ({ ...current, skillId: event.target.value.toLocaleLowerCase().replace(/[^a-z0-9-]/g, '') }))} placeholder="例如 cache-locality-analysis" /></label>
              <label><span>名称</span><input value={draft.label} onChange={(event) => setDraft((current) => ({ ...current, label: event.target.value }))} placeholder="Skill 名称" /></label>
            </div>
            <label className="skill-editor-field"><span>一句话能力</span><input value={draft.description} onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))} placeholder="说明它解决什么问题，以及何时加载" /></label>

            {selectedManaged && <div className="skill-version-strip">
              <label><span>查看版本</span><select value={draft.versionId || selectedManaged.effective_version_id || ''} onChange={(event) => requestOpenSkill(draft.skillId, event.target.value)}>
                {selectedManaged.versions.map((version) => <option key={version.version_id} value={version.version_id}>{version.origin === 'user' ? '自编辑' : '平台'} · {version.version}{version.active ? ' · 当前' : ''}</option>)}
              </select></label>
              <div><span>内容</span><strong>{(selectedVersion?.content_chars ?? draft.content.length).toLocaleString()} 字</strong></div>
              <div><span>保存时间</span><strong>{formatTime(selectedVersion?.created_at) || '随平台发布'}</strong></div>
              {selectedVersion && !selectedVersion.active && selectedVersion.origin === 'user' && <button className="quiet-button" type="button" disabled={saving} onClick={() => void activateVersion(selectedVersion.version_id)}><Check size={14} />设为当前</button>}
            </div>}

            <label className="skill-editor-field content"><span className="skill-editor-content-head"><span>Skill 内容</span><small>{contentLineCount.toLocaleString()} 行 · {draft.content.length.toLocaleString()} 字符</small></span><textarea value={draft.content} onChange={(event) => setDraft((current) => ({ ...current, content: event.target.value }))} spellCheck={false} /></label>
            <label className="skill-editor-field"><span>版本说明</span><input value={draft.changeSummary} onChange={(event) => setDraft((current) => ({ ...current, changeSummary: event.target.value }))} placeholder="本次修改解决的问题" /></label>
            <p className="skill-editor-save-policy">保存会创建并立即热加载一个自编辑版本，不覆盖平台基线。可通过版本选择器检查历史并随时恢复。</p>

            {selectedPresentation && <details className="skill-runtime-contract"><summary>运行契约</summary><dl>
              <div><dt>执行</dt><dd>{selectedPresentation.execution || '由 Agent 按 Skill 内容执行'}</dd></div>
              <div><dt>输入</dt><dd>{selectedPresentation.input || '由任务上下文决定'}</dd></div>
              <div><dt>输出</dt><dd>{selectedPresentation.output || '由 Skill 契约决定'}</dd></div>
              <div><dt>边界</dt><dd>{selectedPresentation.boundary || '不得把假设升级为事实'}</dd></div>
            </dl></details>}

            {notice && !errorForwarded && <div className={`skill-editor-notice ${notice.tone}`}>{notice.text}</div>}
          </div>
          <footer className="skill-manager-editor-actions">
            {dirty && <button className="quiet-button" type="button" disabled={saving} onClick={restoreUnsavedDraft}><Undo2 size={14} />撤销修改</button>}
            {draft.skillId && selectedManaged?.active_user_version_id && <button className="quiet-button" type="button" disabled={saving} onClick={() => void activateVersion(null)}><RotateCcw size={14} />{selectedManaged.has_server_version ? '恢复平台版本' : '停用此 Skill'}</button>}
            {selectedPresentation?.target_view && <button className="quiet-button" type="button" onClick={() => onOpenWorkspace(selectedPresentation.target_view || 'skills')}>打开工作区</button>}
            <button className="primary-button" type="button" title="保存为新版本（Ctrl/Command + S）" disabled={!canSave} onClick={() => void saveDraft()}>{saving ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}保存为新版本</button>
          </footer>
        </>}
      </div>
    </section>
  )
}
