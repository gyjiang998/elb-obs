'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArchiveRestore, ArrowDownToLine, Check, CircleAlert, Copy, FileText, HardDrive, LoaderCircle, RefreshCw, Search, Server, Trash2, Upload, X } from 'lucide-react'
import { api, apiBase, apiErrorFromPayload, formatBytes } from '../lib/api'
import { errorText, text, timestamp, type Notice } from '../lib/parallel-modeling'
import { useWorkbenchErrorNotification } from './WorkbenchErrorNotification'
import '../parallel-modeling.css'
import '../calibration-storage.css'

type TraceCopy = { copy_id: string; kind: string; label: string; bridge_id?: string; local_path?: string; storage_host?: string; connection_mode?: string; status: string; can_download: boolean; can_delete: boolean; size_bytes?: number }
type TraceEntry = { trace_id: string; name: string; run_id?: string; plan_id?: string; step_id?: string; step_title?: string; device_serial?: string; created_at?: string; size_bytes: number; sha256?: string; locations: TraceCopy[]; preferred_copy_id?: string }
type TraceCatalog = { items: TraceEntry[]; bridges?: Record<string, unknown>[]; summary?: { trace_count: number; available_copy_count: number }; diagnostics?: Record<string, unknown>[] }
const copyStatus = (status: string) => ({ available: '文件可用', external: '采集端原目录', missing: '文件不存在', deleted: '已删除', offline: '主机离线', unknown: '待确认', active: '采集中', busy: '正在使用' })[status] ?? status

export function TraceLibraryStudio({ onNavigateCapture, onNavigateImport }: { onNavigateCapture: () => void; onNavigateImport: () => void }) {
  const [catalog, setCatalog] = useState<TraceCatalog | null>(null)
  const [selectedId, setSelectedId] = useState('')
  const [query, setQuery] = useState('')
  const [location, setLocation] = useState('all')
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState<Notice | null>(null)
  const [deletion, setDeletion] = useState<{ trace: TraceEntry; copy: TraceCopy } | null>(null)
  const packageInput = useRef<HTMLInputElement | null>(null)
  const accept = useCallback((result: TraceCatalog) => {
    setCatalog(result)
    setSelectedId(current => {
      if (result.items.some(item => item.trace_id === current)) return current
      const available = result.items.filter(item => item.locations.some(copy => copy.status === 'available'))
      return available.length === 1 ? available[0].trace_id : ''
    })
  }, [])
  const refresh = useCallback(async (remote = false) => {
    setBusy('refresh')
    try {
      const result = await api<TraceCatalog>(`/v1/trace-library${remote ? '/refresh' : ''}`, remote ? { method: 'POST', body: '{}' } : {})
      accept(result)
      const diagnostics = result.diagnostics?.filter(item => ['failed', 'offline', 'unavailable', 'timed_out'].includes(text(item.status))) ?? []
      setNotice(diagnostics.length ? { tone: 'info', text: diagnostics.map(item => text(item.message, '部分主机尚未完成同步，已保留上次文件记录。')).join('；') } : null)
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }, [accept])
  useEffect(() => { void refresh() }, [refresh])
  const items = useMemo(() => (catalog?.items ?? []).filter(item => {
    const matchesText = [item.name, item.run_id, item.step_title, item.device_serial, ...item.locations.map(copy => copy.local_path)].some(value => value?.toLowerCase().includes(query.toLowerCase()))
    return matchesText && (location === 'all' || item.locations.some(copy => location === 'windows' ? ['windows_bridge', 'windows_local'].includes(copy.kind) : !['windows_bridge', 'windows_local'].includes(copy.kind)))
  }), [catalog, query, location])
  const selected = catalog?.items.find(item => item.trace_id === selectedId)
  const copies = catalog?.items.flatMap(item => item.locations) ?? []
  const availableBytes = (catalog?.items ?? []).reduce((total, item) => total + item.locations.filter(copy => copy.status === 'available').reduce((size, copy) => size + (copy.size_bytes ?? item.size_bytes ?? 0), 0), 0)

  async function copyPath(path: string) {
    try { await navigator.clipboard.writeText(path); setNotice({ tone: 'success', text: '已复制文件路径，可在对应主机的分析工具中打开。' }) }
    catch { setNotice({ tone: 'info', text: '浏览器未开放剪贴板，可直接选中文件路径复制。' }) }
  }
  async function removeCopy() {
    if (!deletion) return
    const { trace, copy } = deletion
    setBusy('delete')
    try {
      await api(`/v1/trace-library/${encodeURIComponent(trace.trace_id)}/copies/${encodeURIComponent(copy.copy_id)}`, { method: 'DELETE' })
      accept(await api<TraceCatalog>('/v1/trace-library'))
      setDeletion(null)
      setNotice({ tone: 'success', text: `已删除“${trace.name}”的${copy.label}副本，保留文件记录。` })
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy('') }
  }
  async function importCapturePackage(file: File) {
    setBusy('package-import')
    try {
      const response = await fetch(`${apiBase}/v1/trace-library/import-capture-package`, {
        method: 'POST', body: file,
        headers: { 'Content-Type': 'application/zip', 'X-Capture-Filename': file.name },
      })
      const payload: unknown = await response.json().catch(() => ({}))
      if (!response.ok) throw apiErrorFromPayload(payload, response.status)
      const result = payload as { run?: { run_id?: string }; catalog?: TraceCatalog; analysis_ready_count?: number; dataset_imports?: { status?: string; step_title?: string; error?: { message?: string } }[] }
      if (result.catalog) accept(result.catalog); else await refresh()
      const ready = result.analysis_ready_count ?? 0
      const rejected = (result.dataset_imports ?? []).filter(item => item.status === 'not_analyzable')
      setNotice({ tone: ready > 0 ? 'success' : rejected.length ? 'info' : 'success', text: ready > 0
        ? `本地采集包已登记${result.run?.run_id ? `：${result.run.run_id}` : ''}。已自动识别 Trace、HiLog、PMU 与清单，并创建 ${ready} 个负载数据集，可直接进入负载特征分析。`
        : `本地采集包已登记${result.run?.run_id ? `：${result.run.run_id}` : ''}。Trace、HiLog、PMU 与清单已整体保存${rejected.length ? `；其中 ${rejected.length} 个步骤不满足 BuildItem 建模条件，可在文件详情中继续查看原始数据` : ''}。` })
    } catch (error) { setNotice({ tone: 'error', text: errorText(error) }) }
    finally { setBusy(''); if (packageInput.current) packageInput.current.value = '' }
  }

  const errorForwarded = useWorkbenchErrorNotification(notice, () => setNotice(null), 'trace-library')
  return <div className="parallel-studio trace-library-studio">
    <div className="pm-heading"><div><h2 className="pm-page-title" tabIndex={0} title="按采样步骤管理原始文件与存储副本，可在本地工具中分析并按需清理。">Trace 管理</h2></div><div className="pm-toolbar"><input ref={packageInput} type="file" accept=".zip,.capture.zip,application/zip" hidden onChange={(event) => { const file = event.target.files?.[0]; if (file) void importCapturePackage(file) }} /><button className="quiet-button" disabled={Boolean(busy)} onClick={() => packageInput.current?.click()}>{busy === 'package-import' ? <LoaderCircle size={14} className="spin" /> : <ArchiveRestore size={14} />}导入本地采集包</button><button className="quiet-button" onClick={onNavigateImport}><Upload size={14} />导入 Trace</button><button className="quiet-button" onClick={onNavigateCapture}>前往负载采样</button><button className="quiet-button" disabled={Boolean(busy)} onClick={() => void refresh(true)}>{busy === 'refresh' ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}刷新文件位置</button></div></div>
    {notice && !errorForwarded && <div className={`pm-notice ${notice.tone}`} role="status">{notice.tone === 'success' ? <Check size={15} /> : <CircleAlert size={15} />}<span>{notice.text}</span><button aria-label="关闭提示" onClick={() => setNotice(null)}><X size={14} /></button></div>}
    <div className="cs-summary"><div><strong>{catalog?.items.length ?? '—'}</strong><span>Trace 记录</span></div><div><strong>{copies.filter(copy => copy.status === 'available').length}</strong><span>可用副本</span></div><div><strong>{formatBytes(availableBytes)}</strong><span>可用副本总大小</span></div><div><strong>{copies.filter(copy => ['windows_bridge', 'windows_local'].includes(copy.kind) && ['available', 'external'].includes(copy.status)).length}</strong><span>采集端路径</span></div></div>
    <div className="cs-storage-policy"><HardDrive size={18} /><p><strong>原始文件保留在采集端</strong><span>采集端保留原始目录；导入本地采样包后，当前工作台保存一份独立分析副本。可按位置下载或清理受管副本。</span></p></div>
    <section className="pm-section"><div className="pm-toolbar"><label className="pm-field"><span>查找文件、步骤或设备</span><div className="cs-search"><Search size={15} /><input aria-label="查找 Trace" value={query} onChange={event => setQuery(event.target.value)} placeholder="文件名 / 采集批次 / 步骤 / 设备" /></div></label><label className="pm-field" style={{ maxWidth: 220 }}><span>存储位置</span><select value={location} onChange={event => setLocation(event.target.value)}><option value="all">全部位置</option><option value="windows">采集端</option><option value="platform">当前工作台</option></select></label></div>
      {!catalog && busy ? <div className="pm-skeleton"><LoaderCircle size={18} className="spin" />正在读取 Trace 记录…</div> : <div className="cs-library-grid">
        <div className="cs-file-list" aria-label="Trace 文件列表">{items.length ? items.map(item => <button key={item.trace_id} className={item.trace_id === selectedId ? 'selected' : ''} onClick={() => setSelectedId(item.trace_id)}><FileText size={19} /><div><strong>{item.name}</strong><span>{item.step_title || item.step_id || '导入文件'} · {formatBytes(item.size_bytes ?? 0)}</span><small>{timestamp(item.created_at)} · {item.locations.filter(copy => copy.status === 'available').length} 个可用副本</small></div></button>) : <div className="pm-empty"><FileText size={24} />{query || location !== 'all' ? '没有匹配的 Trace' : '采集或导入后，原始 Trace 会显示在这里。'}</div>}</div>
        {selected ? <div className="cs-file-detail"><div className="pm-section-heading"><div><FileText size={17} /><h3>{selected.name}</h3></div><span className="pm-badge">{formatBytes(selected.size_bytes ?? 0)}</span></div><dl className="cs-metadata"><div><dt>采集步骤</dt><dd>{selected.step_title || selected.step_id || '外部导入'}</dd></div><div><dt>设备</dt><dd>{selected.device_serial || '未记录'}</dd></div>{selected.run_id && <div><dt>采集批次</dt><dd>{selected.run_id}</dd></div>}{selected.sha256 && <div><dt>SHA-256</dt><dd className="cs-hash">{selected.sha256}</dd></div>}</dl>
          <div className="cs-copy-list">{selected.locations.map(copy => <div className="cs-copy" key={copy.copy_id}><div className="pm-section-heading"><div>{['windows_bridge', 'windows_local'].includes(copy.kind) ? <HardDrive size={16} /> : <Server size={16} />}<h4>{copy.label}</h4>{selected.preferred_copy_id === copy.copy_id && <span className="pm-badge">优先位置</span>}</div><span className={`pm-badge ${copy.status === 'available' ? 'good' : copy.status === 'deleted' ? '' : 'warn'}`}>{copyStatus(copy.status)}</span></div>{copy.local_path && <code className="cs-path">{copy.local_path}</code>}<p className="pm-hint">{copy.kind === 'windows_bridge' ? '在采集电脑的本地分析工具中打开以上路径。' : copy.kind === 'windows_local' ? '这是采样包记录的采集端原始目录；工作台不会远程修改该文件。' : '可下载到当前电脑，也可在文件所在主机中打开。'}{copy.storage_host ? ` 主机：${copy.storage_host}` : ''}</p><div className="pm-actions">{copy.local_path && <button className="quiet-button" onClick={() => void copyPath(copy.local_path!)}><Copy size={13} />复制路径</button>}{copy.can_download && <a className="quiet-button cs-download" href={`${apiBase}/v1/trace-library/${encodeURIComponent(selected.trace_id)}/download?copy_id=${encodeURIComponent(copy.copy_id)}`} download><ArrowDownToLine size={13} />下载原始文件</a>}{copy.can_delete && <button className="quiet-button cs-delete" disabled={Boolean(busy)} onClick={() => setDeletion({ trace: selected, copy })}><Trash2 size={13} />删除此副本</button>}</div></div>)}</div>
        </div> : <div className="pm-empty">选择一条 Trace 查看文件位置。</div>}
      </div>}
    </section>
    {deletion && <div className="cs-modal-backdrop"><div className="cs-modal" role="dialog" aria-modal="true" aria-labelledby="trace-delete-title"><h3 id="trace-delete-title">删除原始文件副本</h3><p>将删除 <strong>{deletion.trace.name}</strong> 在 <strong>{deletion.copy.label}</strong> 的文件。</p><code className="cs-path">{deletion.copy.local_path}</code><p className="pm-hint">这会释放该位置的磁盘空间。其他副本与已有分析记录保留；此文件将不能再用于下载或重新解析。</p><div className="pm-actions"><button className="quiet-button" disabled={Boolean(busy)} onClick={() => setDeletion(null)}>取消</button><button className="primary-button cs-danger" disabled={Boolean(busy)} onClick={() => void removeCopy()}>{busy === 'delete' ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />}确认删除此副本</button></div></div></div>}
  </div>
}
