'use client'

import { Archive, CircleAlert, Cpu, LoaderCircle, Plus, RefreshCw, RotateCcw, Save, Smartphone, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { platformApiFor } from '../lib/api'
import type { JsonRecord } from '../lib/types'
import { focusWorkflowAction } from '../lib/workflow-focus'
import { errorText, metric, numeric, record, records, text, timestamp, type ModelingCatalog, type Notice } from '../lib/parallel-modeling'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'
import '../parallel-modeling.css'

type HardwareProfile = JsonRecord & {
  id: string
  name: string
  builtin?: boolean
  archived?: boolean
  status?: string
  source?: string | JsonRecord
  core_groups?: JsonRecord[]
  compatible_model_package_id?: string
  model_package_id?: string
  model_status?: string
  profile_sha256?: string
  created_at?: string
  display_source?: string
  display_source_label?: string
  display_provenance?: JsonRecord
}

const api = platformApiFor('hardware-models')

type LocalDevice = { serial: string; name: string }
type EditablePhysicalCore = {
  id: string
  logicalCpuIds: number[]
  representativeLogicalCpuId: number
  topologySource?: string
}
type EditableGroup = {
  id: string
  proxyKind: 'big' | 'middle' | 'small'
  name: string
  physical: string
  cpus: string
  opps: string
  physicalCoreCountSource?: string
  sourcePolicyId?: string
  physicalTopology?: EditablePhysicalCore[]
}

const emptyGroups: EditableGroup[] = [
  { id: 'big', proxyKind: 'big', name: '大核', physical: '', cpus: '', opps: '' },
  { id: 'middle', proxyKind: 'middle', name: '中核', physical: '', cpus: '', opps: '' },
  { id: 'small', proxyKind: 'small', name: '小核', physical: '', cpus: '', opps: '' },
]

function integerList(value: string, label: string): number[] {
  const rows = value.split(/[\s,，]+/).map(item => item.trim()).filter(Boolean).map(Number)
  if (!rows.length || rows.some(item => !Number.isInteger(item) || item < 0)) throw new Error(`${label}需要填写非负整数，并以逗号分隔。`)
  return [...new Set(rows)]
}

function mhzList(value: string, label: string): number[] {
  const rows = value.split(/[\s,，]+/).map(item => item.trim()).filter(Boolean).map(Number)
  if (!rows.length || rows.some(item => !Number.isFinite(item) || item <= 0)) throw new Error(`${label}需要填写正数 MHz，并以逗号分隔。`)
  return [...new Set(rows.map(item => Math.round(item * 1000)))].sort((a, b) => a - b)
}

function sourceLabel(profile: HardwareProfile): string {
  if (profile.display_source === 'bundled_offline_calibration') return profile.display_source_label || '平台离线实测'
  if (profile.builtin) return '平台内置'
  const source = typeof profile.source === 'string' ? profile.source : text(record(profile.source).kind)
  return source === 'local_hdc' ? '本机 HDC 提取' : '用户添加'
}

function modelStatus(profile: HardwareProfile): { label: string; tone: string; detail: string } {
  if (profile.model_status === 'topology_proxy_uncalibrated') return { label: '拓扑迁移 · 待校准', tone: 'warn', detail: '核组与 OPP 来自该设备，性能系数继承参考模型；可用于条件仿真，仍需实测校准。' }
  if (profile.compatible_model_package_id || profile.model_package_id) return { label: '模型可用', tone: 'good', detail: '存在与此硬件配置严格绑定的并行代价模型。' }
  return { label: '缺少代价模型', tone: 'error', detail: '该配置只能保存与绑定负载，完成校准前不能用于性能寻优。' }
}

function normalizeDevices(value: unknown): LocalDevice[] {
  const rows = Array.isArray(value) ? value : Array.isArray(record(value).devices) ? record(value).devices as unknown[] : []
  return rows.map(item => {
    const row = record(item)
    const serial = typeof item === 'string' ? item : text(row.serial ?? row.deviceSerial ?? row.device_serial ?? row.id)
    return { serial, name: text(row.name ?? row.model, serial) }
  }).filter(item => item.serial)
}

function draftGroups(value: JsonRecord): EditableGroup[] {
  const groups = records(value.cpu_groups ?? value.core_groups)
  if (!groups.length) return emptyGroups.map(item => ({ ...item }))
  return groups.map((group, index) => {
    const id = text(group.id ?? group.kind, `cluster_${index}`)
    const proxy = text(group.proxy_kind, ['big', 'middle', 'small'].includes(id) ? id : 'middle')
    const cpus = Array.isArray(group.logical_cpu_ids) ? group.logical_cpu_ids : []
    const opps = Array.isArray(group.opps_khz) ? group.opps_khz : []
    const physicalTopology = records(group.physical_core_topology).map((core, coreIndex) => {
      const logicalCpuIds = Array.isArray(core.logical_cpu_ids)
        ? core.logical_cpu_ids.map(Number).filter(Number.isSafeInteger)
        : []
      return {
        id: text(core.id, `core_${coreIndex}`),
        logicalCpuIds,
        representativeLogicalCpuId: numeric(core.representative_logical_cpu_id) ?? logicalCpuIds[0],
        topologySource: text(core.topology_source) || undefined,
      }
    }).filter(core => core.logicalCpuIds.length && Number.isSafeInteger(core.representativeLogicalCpuId))
    return {
      id,
      proxyKind: (['big', 'middle', 'small'].includes(proxy) ? proxy : 'middle') as EditableGroup['proxyKind'],
      name: text(group.name, `CPU 核组 ${index + 1}`),
      physical: String(numeric(group.physical_core_count) ?? cpus.length ?? ''),
      cpus: cpus.join(', '),
      opps: opps.map(item => numeric(item) !== null ? Number(item) / 1000 : item).join(', '),
      physicalCoreCountSource: text(group.physical_core_count_source) || undefined,
      sourcePolicyId: text(group.source_policy_id) || undefined,
      physicalTopology: physicalTopology.length ? physicalTopology : undefined,
    }
  })
}

function groupLogicalCpuIds(group: JsonRecord): number[] {
  return Array.isArray(group.logical_cpu_ids) ? group.logical_cpu_ids.map(Number).filter(Number.isSafeInteger) : []
}

function groupPhysicalTopology(group: JsonRecord): JsonRecord[] {
  return records(group.physical_core_topology)
}

function physicalCoreMapLabel(core: JsonRecord, index: number): string {
  const members = Array.isArray(core.logical_cpu_ids) ? core.logical_cpu_ids.map(Number).filter(Number.isSafeInteger) : []
  return `P${index} → CPU ${members.join(' / ')}`
}

function notifyWorkspaceUpdated() {
  window.dispatchEvent(new CustomEvent('parallel-workspace-updated', { detail: { source: 'hardware-profiles' } }))
}

export function HardwareModelsStudio({ onListLocalDevices, onExtractLocalHardware, onNavigateModeling, initialDraft, draftRevision = 0 }: {
  onListLocalDevices?: () => Promise<unknown>
  onExtractLocalHardware?: (deviceSerial: string) => Promise<JsonRecord>
  onNavigateModeling?: () => void
  initialDraft?: JsonRecord
  draftRevision?: number
}) {
  const [profiles, setProfiles] = useState<HardwareProfile[]>([])
  const [catalog, setCatalog] = useState<ModelingCatalog | null>(null)
  const [devices, setDevices] = useState<LocalDevice[]>([])
  const [selectedDevice, setSelectedDevice] = useState('')
  const [name, setName] = useState('')
  const [groups, setGroups] = useState<EditableGroup[]>(emptyGroups.map(item => ({ ...item })))
  const [extracted, setExtracted] = useState<JsonRecord | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState<Notice | null>(null)
  const saveAction = useRef<HTMLButtonElement | null>(null)

  const activeProfiles = useMemo(() => profiles.filter(item => !item.archived), [profiles])
  const archivedProfiles = useMemo(() => profiles.filter(item => item.archived), [profiles])
  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [profileResult, catalogResult] = await Promise.all([
        api<{ profiles: HardwareProfile[] }>('/v1/parallel-modeling/hardware-profiles'),
        api<ModelingCatalog>('/v1/parallel-modeling/catalog'),
      ])
      setProfiles(profileResult.profiles ?? [])
      setCatalog(catalogResult)
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    if (!initialDraft || !Object.keys(initialDraft).length) return
    setExtracted(initialDraft)
    setName(text(initialDraft.name, '本机设备硬件配置'))
    setGroups(draftGroups(initialDraft))
    setNotice({ tone: 'info', text: '已从本地设备读取核组、CPU 与 OPP。请核对名称后保存到硬件模型库。' })
  }, [initialDraft, draftRevision])

  async function discover() {
    if (!onListLocalDevices) return
    setBusy('discover'); setNotice(null)
    try {
      const rows = normalizeDevices(await onListLocalDevices())
      setDevices(rows); setSelectedDevice(current => rows.some(item => item.serial === current) ? current : rows.length === 1 ? rows[0].serial : '')
      setNotice({ tone: rows.length ? 'success' : 'info', text: rows.length ? `发现 ${rows.length} 台本地 HDC 设备。` : 'HDC 已返回，当前没有连接设备。' })
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }

  async function extract() {
    if (!selectedDevice || !onExtractLocalHardware) return
    setBusy('extract'); setNotice(null)
    try {
      const result = await onExtractLocalHardware(selectedDevice)
      const profile = record(result.profile ?? result)
      if (!records(profile.cpu_groups ?? profile.core_groups).length) throw new Error('设备读取结果未包含可用的 CPU 频率域。')
      setExtracted(profile); setName(text(profile.name, devices.find(item => item.serial === selectedDevice)?.name ?? '本机设备硬件配置')); setGroups(draftGroups(profile))
      setNotice({ tone: 'success', text: '硬件拓扑与 OPP 已提取。保存后会生成明确标注为待校准的拓扑迁移模型。' })
      focusWorkflowAction(saveAction)
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }

  function updateGroup(index: number, field: keyof EditableGroup, value: string) {
    setGroups(current => current.map((item, offset) => offset === index
      ? {
          ...item,
          [field]: value,
          ...(['physical', 'cpus'].includes(field)
            ? { physicalTopology: undefined, physicalCoreCountSource: 'manual' }
            : {}),
        }
      : item))
  }

  function addGroup() {
    setGroups(current => {
      let index = current.length
      while (current.some(group => group.id === `cluster_${index}`)) index += 1
      return [...current, { id: `cluster_${index}`, proxyKind: 'middle', name: `CPU 核组 ${index + 1}`, physical: '', cpus: '', opps: '' }]
    })
  }

  function removeGroup(index: number) {
    setGroups(current => current.length > 1 ? current.filter((_item, offset) => offset !== index) : current)
    if (groups.length <= 1) setNotice({ tone: 'info', text: '硬件配置至少需要一个 CPU 频率域。' })
  }

  async function save() {
    setBusy('save'); setNotice(null)
    try {
      if (!name.trim()) throw new Error('请填写硬件配置名称。')
      const core_groups = groups.map(group => {
        const count = Number(group.physical)
        if (!Number.isInteger(count) || count <= 0) throw new Error(`${group.name}物理核心数需要为正整数。`)
        const logical_cpu_ids = integerList(group.cpus, `${group.name}逻辑 CPU`)
        const opps_khz = mhzList(group.opps, `${group.name} OPP`)
        return {
          id: group.id,
          kind: group.id,
          proxy_kind: group.proxyKind,
          name: group.name,
          physical_core_count: count,
          logical_cpu_count: logical_cpu_ids.length,
          logical_cpu_ids,
          ...(group.physicalTopology ? {
            physical_core_representative_cpu_ids: group.physicalTopology.map(core => core.representativeLogicalCpuId),
            physical_core_topology: group.physicalTopology.map(core => ({
              id: core.id,
              logical_cpu_ids: core.logicalCpuIds,
              representative_logical_cpu_id: core.representativeLogicalCpuId,
              ...(core.topologySource ? { topology_source: core.topologySource } : {}),
            })),
          } : {}),
          opps_khz,
          min_khz: opps_khz[0],
          max_khz: opps_khz.at(-1),
          ...(group.physicalCoreCountSource ? { physical_core_count_source: group.physicalCoreCountSource } : {}),
          ...(group.sourcePolicyId ? { source_policy_id: group.sourcePolicyId } : {}),
        }
      })
      const source = text(extracted?.source, extracted ? 'local_hdc' : 'manual')
      const payload = { ...(extracted ?? {}), id: undefined, name: name.trim(), source, cpu_groups: undefined, core_groups }
      const saved = await api<HardwareProfile>('/v1/parallel-modeling/hardware-profiles', { method: 'POST', body: JSON.stringify(payload) })
      setExtracted(null); setName(''); setGroups(emptyGroups.map(item => ({ ...item })))
      await refresh()
      notifyWorkspaceUpdated()
      setNotice({ tone: 'success', text: `已保存「${saved.name}」。负载现在可以绑定该配置并使用对应的拓扑迁移模型寻优。` })
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }

  async function archive(profile: HardwareProfile) {
    if (profile.builtin) return
    if (!window.confirm(`归档「${profile.name}」？归档后它不会用于新的负载绑定或寻优，可在本页恢复。`)) return
    setBusy(`archive:${profile.id}`); setNotice(null)
    try {
      await api(`/v1/parallel-modeling/hardware-profiles/${encodeURIComponent(profile.id)}/archive`, { method: 'POST', body: '{}' })
      await refresh(); notifyWorkspaceUpdated(); setNotice({ tone: 'success', text: `已归档「${profile.name}」。已有数据和结果仍保留原配置引用。` })
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }

  async function restore(profile: HardwareProfile) {
    setBusy(`restore:${profile.id}`); setNotice(null)
    try {
      await api(`/v1/parallel-modeling/hardware-profiles/${encodeURIComponent(profile.id)}/restore`, { method: 'POST', body: '{}' })
      await refresh(); notifyWorkspaceUpdated(); setNotice({ tone: 'success', text: `已恢复「${profile.name}」，可重新用于负载绑定与寻优。` })
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }

  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'hardware-models')
  return <div className="workspace-scroll parallel-studio hardware-models-studio">
    <header className="pm-heading"><div><h2 className="pm-page-title" tabIndex={0} title="从本地设备读取 CPU 拓扑、频率域与 OPP，或手动登记用于并行寻优的硬件配置。">硬件模型</h2></div><div className="pm-inline"><button className="quiet-button" type="button" onClick={() => void refresh()} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} />刷新</button>{onNavigateModeling && <button className="quiet-button" type="button" onClick={onNavigateModeling}>返回并行建模</button>}</div></header>
    {notice && !errorForwarded && <div className={`pm-notice ${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}><CircleAlert size={15} /><span>{notice.text}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示">×</button></div>}

    {onListLocalDevices && onExtractLocalHardware && <section className="pm-section"><div className="pm-section-heading"><div><Smartphone size={17} /><h3>从本地设备提取</h3></div><span className="pm-badge active">只读 HDC</span></div>
      <div className="hardware-extract-row"><label className="pm-field"><span>本机 HDC 设备</span><select value={selectedDevice} onChange={event => setSelectedDevice(event.target.value)}><option value="">{devices.length ? '选择设备' : '请先读取设备'}</option>{devices.map(device => <option key={device.serial} value={device.serial}>{device.name} · {device.serial}</option>)}</select></label><button className="quiet-button" type="button" disabled={Boolean(busy)} onClick={() => void discover()}>{busy === 'discover' ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}读取设备</button><button className="primary-button" type="button" disabled={Boolean(busy) || !selectedDevice} onClick={() => void extract()}>{busy === 'extract' ? <LoaderCircle size={14} className="spin" /> : <Cpu size={14} />}提取硬件配置</button></div><p className="pm-hint hardware-extract-hint">读取 sysfs 中的 CPU 频率域、核拓扑与可用频点，不执行负载。</p>
    </section>}

    <section className="pm-section"><div className="pm-section-heading"><div><Plus size={17} /><h3>{extracted ? '核对并保存提取结果' : '添加硬件配置'}</h3></div><div className="pm-inline">{extracted && <span className="pm-badge active">本机提取草稿</span>}<button className="quiet-button" type="button" onClick={addGroup}><Plus size={13} />添加核组</button></div></div>
      <label className="pm-field"><span>配置名称</span><input value={name} onChange={event => setName(event.target.value)} placeholder="例如 Kirin 测试机" /></label>
      <div className="hardware-group-grid">{groups.map((group, index) => <div className="pm-box hardware-group-card" key={group.id}>
        <div className="hardware-card-heading"><h4>{group.name}</h4><span className="pm-inline"><span className="pm-badge">{group.id}</span><button className="hardware-group-delete" type="button" aria-label={`删除${group.name}`} onClick={() => removeGroup(index)} disabled={groups.length <= 1}><Trash2 size={13} /></button></span></div>
        <label className="pm-field"><span>核组名称</span><input value={group.name} onChange={event => updateGroup(index, 'name', event.target.value)} /></label>
        <label className="pm-field"><span>代理核型</span><select value={group.proxyKind} onChange={event => updateGroup(index, 'proxyKind', event.target.value)}><option value="small">小核系数</option><option value="middle">中核系数</option><option value="big">大核系数</option></select></label>
        <label className="pm-field"><span>物理核心数</span><input type="number" min={1} value={group.physical} onChange={event => updateGroup(index, 'physical', event.target.value)} /></label>
        <label className="pm-field"><span>逻辑 CPU ID</span><input value={group.cpus} onChange={event => updateGroup(index, 'cpus', event.target.value)} placeholder="例如 4, 5, 6" /></label>
        {group.physicalTopology && <div className="hardware-core-map"><span>物理核 → 逻辑 CPU</span><div>{group.physicalTopology.map((core, coreIndex) => <code key={core.id} title={core.topologySource}>{`P${coreIndex} → CPU ${core.logicalCpuIds.join(' / ')}`}</code>)}</div></div>}
        <label className="pm-field"><span>可用 OPP（MHz）</span><textarea rows={2} value={group.opps} onChange={event => updateGroup(index, 'opps', event.target.value)} placeholder="例如 600, 1200, 1800, 2400" /></label>
      </div>)}</div>
      <div className="pm-note" style={{ marginTop: 12 }}><CircleAlert size={15} /><span>自动提取与手动添加只建立硬件配置。平台会生成兼容的拓扑迁移模型以打通寻优，性能比例仍继承参考模型，并在所有结果中标为待实测校准。</span></div>
      <div className="pm-actions"><button ref={saveAction} className="primary-button pm-step-action" type="button" disabled={Boolean(busy)} onClick={() => void save()}>{busy === 'save' ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}保存硬件配置</button></div>
    </section>

    <section className="pm-section"><div className="pm-section-heading"><div><Cpu size={17} /><h3>已登记硬件模型</h3></div><span className="pm-badge">{activeProfiles.length} 项</span></div>
      {loading && !profiles.length ? <div className="pm-skeleton"><LoaderCircle size={18} className="spin" />正在读取硬件配置与兼容代价模型目录…</div> : <div className="hardware-profile-list">{activeProfiles.map(profile => {
        const state = modelStatus(profile)
        const groups = profile.core_groups ?? []
        const model = catalog?.model_packages.find(item => item.id === (profile.compatible_model_package_id ?? profile.model_package_id))
        const physicalCount = groups.reduce((sum, group) => sum + (numeric(group.physical_core_count) ?? 0), 0)
        const logicalCount = groups.reduce((sum, group) => sum + groupLogicalCpuIds(group).length, 0)
        return <article className="pm-box hardware-profile-card" key={profile.id}>
          <div className="hardware-card-heading"><div><strong>{profile.name}</strong><small>{sourceLabel(profile)} · {timestamp(profile.created_at)}</small></div><span className={`pm-badge ${state.tone}`}>{state.label}</span></div>
          <div className="hardware-count-summary" title="物理核用于计算独立并行资源；逻辑 CPU 是操作系统用于调度和绑核的 CPU ID。"><strong>{physicalCount} 个物理核</strong><span>{logicalCount} 个逻辑 CPU</span></div>
          <div className="hardware-topology">{groups.map(group => {
            const logicalIds = groupLogicalCpuIds(group)
            const topology = groupPhysicalTopology(group)
            return <div key={text(group.id ?? group.kind)}><span>{text(group.name, text(group.kind))}</span><strong>{metric(group.physical_core_count, 0)} 物理核 · {logicalIds.length} 逻辑 CPU</strong><small>{Array.isArray(group.opps_khz) ? group.opps_khz.length : 0} 档 OPP · {numeric(group.max_khz) !== null ? metric(Number(group.max_khz) / 1000, 0) : '—'} MHz</small>{topology.length > 0 && <div className="hardware-core-map compact">{topology.map((core, index) => <code key={text(core.id, String(index))}>{physicalCoreMapLabel(core, index)}</code>)}</div>}</div>
          })}</div>
          <p className="pm-hint">{state.detail}</p><div className="hardware-profile-footer"><code>{profile.id}</code><span>{model?.name ?? profile.compatible_model_package_id ?? '无兼容模型'}</span>{!profile.builtin && <button className="quiet-button" type="button" disabled={Boolean(busy)} onClick={() => void archive(profile)}>{busy === `archive:${profile.id}` ? <LoaderCircle size={13} className="spin" /> : <Archive size={13} />}归档</button>}</div>
        </article>
      })}{!activeProfiles.length && <div className="pm-empty"><Cpu size={22} />尚未登记硬件模型。</div>}</div>}
      {archivedProfiles.length > 0 && <details className="pm-details hardware-archive-history"><summary>已归档配置 · {archivedProfiles.length} 项</summary><div className="hardware-archive-list">{archivedProfiles.map(profile => <div className="pm-source-card" key={profile.id}><span><strong>{profile.name}</strong><small>{profile.id} · {timestamp(profile.archived_at)}</small></span><button className="quiet-button" type="button" disabled={Boolean(busy)} onClick={() => void restore(profile)}>{busy === `restore:${profile.id}` ? <LoaderCircle size={13} className="spin" /> : <RotateCcw size={13} />}恢复</button></div>)}</div></details>}
    </section>
  </div>
}
