'use client'

import { LoaderCircle, RefreshCw } from 'lucide-react'
import { captureRouteKey, captureTargetKey, type CaptureDeviceCommand, type CaptureDeviceSelectionView } from '../lib/scenario-capture-device'

const statuses = { connected: '已连接', offline: '离线', unconfirmed: '待确认' }
const diagnosticStatuses: Record<string, string> = { ready: '已发现设备', empty: '未发现手机', offline: '采集端离线', unavailable: '不可用', failed: '检测失败', timed_out: '检测超时', not_probed: '尚未检测' }
function endpointLabel(value: string | undefined, fallback = '远程采集端'): string {
  const label = value || fallback
  if (/^windows hdc device bridge$/i.test(label) || /^windows device bridge$/i.test(label)) return 'Windows 采集端'
  if (/^windows bridge(?=\s*(?:·|$))/i.test(label)) return label.replace(/^windows bridge/i, 'Windows 采集端')
  if (/^device bridge$/i.test(label) || /^bridge$/i.test(label)) return '远程采集端'
  return label
}

/** Both entry points operate on the capture page's single device selection. */
export function CaptureDevicePicker({ view, onCommand, compact = false }: { view: CaptureDeviceSelectionView; onCommand: (command: CaptureDeviceCommand) => void; compact?: boolean }) {
  const selectedKey = view.selected ? captureTargetKey(view.selected) : ''
  const selected = view.devices.find(device => captureTargetKey(device) === selectedKey)
  const state = selectedKey ? view.states[selectedKey] ?? 'unconfirmed' : null
  const route = view.routes.find(item => captureRouteKey(item) === captureRouteKey(view.route))
  const diagnostics = view.diagnostics.filter(item => view.route.connection_mode === 'auto' || item.connection_mode === view.route.connection_mode && (view.route.connection_mode !== 'bridge' || item.bridge_id === view.route.bridge_id))
  const failed = diagnostics.some(item => ['timed_out', 'failed', 'not_probed'].includes(String(item.status)))
  const timedOut = diagnostics.some(item => item.status === 'timed_out')
  const empty = diagnostics.some(item => item.status === 'empty')
  const emptyLabel = view.discovering ? '正在读取手机列表…' : timedOut ? '手机列表读取超时' : failed ? '手机列表读取失败' : empty ? '当前连接未发现手机' : route?.connection_state === 'connected' ? '采集端已在线，尚未取得手机列表' : '尚未发现设备'
  const label = compact ? '助手采样设备' : '采样设备'
  const routeLabel = compact ? '助手设备连接路径' : '设备连接路径'
  return <div className={compact ? 'capture-device-picker compact' : 'capture-device-picker'}>
    <div className="pm-device-card" aria-live="polite">
      <div className="pm-device-card-heading"><span>{compact ? '与负载采样同步' : '采样设备'}</span>{view.discovering ? <LoaderCircle size={14} className="spin" /> : <span className={`pm-badge ${state === 'connected' ? 'good' : 'warn'}`}>{state ? statuses[state] : view.devices.length ? '待选择' : route?.connection_state === 'connected' ? '采集端在线' : '等待连接'}</span>}</div>
      {view.selected ? <><strong>{selected?.display_name ?? view.selected.device_serial}</strong>{selected?.display_name !== view.selected.device_serial && <small>{view.selected.device_serial}</small>}<span className="pm-hint">{view.selected.connection_mode === 'local' ? '本机 HDC 直连' : endpointLabel(selected?.connection_label, '远程采集端')}</span></> : <p>{view.devices.length ? '请选择本次使用的设备' : '可先选择连接路径，再刷新设备'}</p>}
    </div>
    <label className="pm-field" style={{ marginTop: 12 }}><span>连接路径</span><select aria-label={routeLabel} value={captureRouteKey(view.route)} disabled={view.locked} onChange={event => { const route = view.routes.find(item => captureRouteKey(item) === event.target.value); if (route) onCommand({ action: 'route', route }) }}>
      {view.routes.map(route => <option key={captureRouteKey(route)} value={captureRouteKey(route)}>{route.connection_mode === 'bridge' ? endpointLabel(route.display_name) : route.display_name}{route.connection_mode === 'bridge' ? route.connection_state === 'connected' ? ' · 在线' : route.connection_state === 'offline' ? ' · 离线' : ' · 待确认' : ''}</option>)}
    </select></label>
    <label className="pm-field" style={{ marginTop: 10 }}><span>采样设备</span><select aria-label={label} value={selectedKey} disabled={view.locked || !view.devices.length} onChange={event => onCommand({ action: 'select', target: view.devices.find(device => captureTargetKey(device) === event.target.value) ?? null })}>
      <option value="">{view.devices.length ? '请选择本次使用的设备' : emptyLabel}</option>
      {view.selected && !selected && <option value={selectedKey}>{view.selected.device_serial} · 待确认</option>}
      {view.devices.map(device => <option key={captureTargetKey(device)} value={captureTargetKey(device)}>{device.display_name} · {device.connection_mode === 'local' ? device.connection_label : endpointLabel(device.connection_label)}{device.display_name !== device.device_serial ? ` · ${device.device_serial}` : ''} · {statuses[view.states[captureTargetKey(device)] ?? 'unconfirmed']}</option>)}
    </select></label>
    <div className="pm-actions"><button type="button" className="quiet-button" onClick={() => onCommand({ action: 'refresh' })} disabled={view.locked || view.discovering}><RefreshCw size={14} />{compact ? '刷新设备' : '刷新当前连接'}</button><button type="button" className="quiet-button" onClick={() => onCommand({ action: 'refresh', all: true })} disabled={view.locked || view.discovering}>{compact ? '全部连接' : '刷新全部设备'}</button></div>
    <p className="pm-hint" role={view.error ? 'status' : undefined}>{view.locked ? '采样期间保留当前设备；已提交任务使用提交时的设备。' : view.error || view.note || '选择连接路径和设备后，助手与负载采样使用同一目标。'}</p>
    {view.diagnostics.length > 0 && <details className="pm-details" open={!view.devices.length && !view.discovering}><summary>设备发现诊断</summary>{view.diagnostics.map((item, index) => <p className="pm-hint" key={index}><strong>{item.connection_mode === 'local' ? '本机 HDC' : endpointLabel(view.routes.find(route => route.bridge_id === item.bridge_id && route.connection_mode === 'bridge')?.display_name)}</strong> · {diagnosticStatuses[String(item.status)] ?? String(item.status ?? '')}<br />{String(item.message ?? '')}{item.code ? `（${String(item.code)}）` : ''}</p>)}</details>}
    {!view.locked && !view.devices.length && <p className="pm-hint">{route?.connection_state === 'connected' ? '采集端已连接。可刷新当前连接，并查看设备发现诊断。' : '请确认手机与本机或远程采集端连接，也可下载本地采样包执行。'}</p>}
  </div>
}
