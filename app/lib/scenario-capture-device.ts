import type { CaptureDevice } from './parallel-modeling'

type RecordValue = Record<string, unknown>
export type CaptureTarget = { connection_mode: 'local' | 'bridge'; bridge_id: string; device_serial: string }
export type CaptureRoute = { connection_mode: 'auto' | 'local' | 'bridge'; bridge_id: string }
export type CaptureRouteOption = CaptureRoute & { display_name: string; connection_state?: string }
export type CaptureDeviceCommand = { action: 'select'; target: CaptureTarget | null } | { action: 'route'; route: CaptureRoute } | { action: 'refresh'; all?: boolean }
export type CaptureDeviceSelectionView = {
  devices: CaptureDevice[]; states: Record<string, CaptureDeviceStatus>; selected: CaptureTarget | null
  route: CaptureRoute; routes: CaptureRouteOption[]; discovering: boolean; locked: boolean
  note: string; error: string; diagnostics: RecordValue[]
}
export const captureDeviceCommandEvent = 'heterogeneous-scenario-capture-device-command'
export const captureDevicePreferenceKey = 'heterogeneous-scenario-capture-device-v1'
export type CaptureDeviceStatus = 'connected' | 'offline' | 'unconfirmed'
const object = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
const string = (value: unknown): string => typeof value === 'string' ? value.trim() : ''
const own = (value: RecordValue, key: string) => Object.prototype.hasOwnProperty.call(value, key)
export const captureTargetKey = (target: CaptureTarget) => JSON.stringify([target.connection_mode, target.bridge_id, target.device_serial])
export const captureRouteKey = (route: CaptureRoute) => JSON.stringify([route.connection_mode, route.bridge_id])

export function captureRoute(value: unknown): CaptureRoute | null {
  const source = object(value)
  const mode = source.connection_mode
  const bridge = string(source.bridge_id)
  if ((mode !== 'auto' && mode !== 'local' && mode !== 'bridge') || (mode === 'bridge' && !bridge)) return null
  return { connection_mode: mode, bridge_id: mode === 'bridge' ? bridge : '' }
}

export function captureSelectionView(value: unknown): CaptureDeviceSelectionView {
  const source = object(value)
  const devices = (Array.isArray(source.devices) ? source.devices : []).flatMap(item => {
    const target = captureTarget(item)
    return target ? [{ ...target, display_name: string(object(item).display_name) || target.device_serial,
      connection_label: string(object(item).connection_label) || (target.connection_mode === 'local' ? '本机 HDC 直连' : 'Windows Bridge') }] : []
  })
  const routes = (Array.isArray(source.routes) ? source.routes : []).flatMap(item => {
    const route = captureRoute(item)
    return route ? [{ ...route, display_name: string(object(item).display_name) || route.bridge_id || '本机 HDC 直连', connection_state: string(object(item).connection_state) }] : []
  })
  const states: Record<string, CaptureDeviceStatus> = {}
  for (const [key, state] of Object.entries(object(source.states))) {
    if (state === 'connected' || state === 'offline' || state === 'unconfirmed') states[key] = state
  }
  return { devices, states, selected: captureTarget(source.selected), route: captureRoute(source.route) ?? { connection_mode: 'auto', bridge_id: '' },
    routes: routes.length ? routes : [{ connection_mode: 'auto', bridge_id: '', display_name: '自动发现全部连接' }, { connection_mode: 'local', bridge_id: '', display_name: '本机 HDC 直连' }],
    discovering: source.discovering === true, locked: source.locked === true, note: string(source.note), error: string(source.error),
    diagnostics: (Array.isArray(source.diagnostics) ? source.diagnostics : []).map(object) }
}

export function dispatchCaptureDeviceCommand(command: CaptureDeviceCommand): void {
  window.dispatchEvent(new CustomEvent(captureDeviceCommandEvent, { detail: command }))
}

export function captureTarget(value: unknown): CaptureTarget | null {
  const source = object(value)
  const mode = source.connection_mode
  const serial = string(source.device_serial)
  const bridge = string(source.bridge_id)
  if (!serial || (mode !== 'local' && mode !== 'bridge') || (mode === 'bridge' && !bridge)) return null
  return { connection_mode: mode, bridge_id: mode === 'local' ? '' : bridge, device_serial: serial }
}

function targetFromContext(value: unknown): CaptureTarget | null {
  const source = object(value)
  if (own(source, 'scenario_capture_target')) return captureTarget(source.scenario_capture_target)
  if (own(source, 'selected_device')) return captureTarget(source.selected_device)
  return captureTarget(source)
}

/** A submitted task owns its snapshot; an unselected capture page never inherits another workspace's Bridge. */
export function scenarioCaptureRuntimeBinding(page: RecordValue, focused?: { kind: string; requestContext?: RecordValue }): RecordValue {
  const scoped = focused ? focused.kind === 'scenario_capture' : page.active_view === 'scenario_capture'
  if (!scoped) return {}
  const request = focused?.requestContext
  const source = request && Object.keys(request).length ? request : object(page.scenario_capture)
  const target = targetFromContext(source)
  return {
    device_execution_mode: target ? target.connection_mode === 'bridge' ? 'remote_bridge' : 'local_hdc' : 'unselected',
    device_selector: target ? { ...target } : {},
    selected_device_bridge: target?.connection_mode === 'bridge' ? { bridge_id: target.bridge_id } : null,
    scenario_capture_selection_required: !target,
    ...(target ? { scenario_capture_target: { ...target } } : {}),
  }
}

/** A failed probe is uncertainty, whereas an empty successful target list is evidence of disconnection. */
export function captureDiscoveryState(device: CaptureTarget, devices: CaptureDevice[], response: RecordValue): CaptureDeviceStatus {
  if (devices.some(item => captureTargetKey(item) === captureTargetKey(device))) return 'connected'
  const diagnostics = Array.isArray(response.diagnostics) ? response.diagnostics.map(object) : []
  const route = diagnostics.find(item => item.connection_mode === device.connection_mode
    && (device.connection_mode === 'local' || item.bridge_id === device.bridge_id))
  return route && ['ready', 'empty', 'offline'].includes(string(route.status)) ? 'offline' : 'unconfirmed'
}

export function mergeCaptureDiscovery(previous: CaptureDevice[], previousStates: Record<string, CaptureDeviceStatus>,
  incoming: CaptureDevice[], response: RecordValue, requested: CaptureRoute | null): { devices: CaptureDevice[]; states: Record<string, CaptureDeviceStatus> } {
  const merged = new Map(previous.map(item => [captureTargetKey(item), item]))
  incoming.forEach(item => merged.set(captureTargetKey(item), item))
  const states = { ...previousStates }
  for (const device of merged.values()) {
    if (!requested || (device.connection_mode === requested.connection_mode && device.bridge_id === requested.bridge_id)) {
      states[captureTargetKey(device)] = captureDiscoveryState(device, incoming, response)
    }
  }
  return { devices: [...merged.values()], states }
}
